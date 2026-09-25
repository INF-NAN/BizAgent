"""`workbench synth run`: step-by-step AWM synthesis with checkpoints, cache, ledger, validation.

Order and arguments mirror AWM's own pipeline (third_party/agent-world-model/awm/core/
pipeline.py:39-143); every step runs as ``python -m awm.cli gen <step> ...`` in a subprocess
(CLI verified in docs/RECON.md §1.1). Experimental settings are untouched: we only choose
paths, the scenario count (``--target_count``, and ``--limit`` for the task step) and the
verifier mode. Rules enforced here:

- DRY-RUN by default; ``--execute`` additionally requires the LLM env vars.
- The seed file is COPIED into the run dir first: `gen scenario` writes classification
  results back into its input (awm/core/scenario.py:642), which would otherwise modify the
  submodule (rule R5).
- Output must live under ``data/synth/`` and the manifest says ``origin: local-synth`` (R2).
- Each step runs in its own process group. SIGINT/SIGTERM stop that group and the groups of
  everything the step started (AWM starts test servers in their own sessions, awm/core/env.py:
  161-172), mark the step ``interrupted`` and exit; the same command resumes (ADR-022). A step
  that did not finish starts again without its outputs: they are moved to ``attempts/`` first.
- Budget stop: before a step and after it, the run's ledger cost is checked against
  ``synth.budget``; the proxy refuses to forward once it is reached (ADR-023).
- A step is judged from the ledger as well as from its exit code, because AWM turns failed LLM
  requests into empty replies and exits 0 (awm/gpt.py:195-206): a refused request fails the step;
  more requests lost to upstream errors than ``synth.max_failed_requests`` fail it; fewer mark it
  ``done_with_failures``, never ``done`` (ADR-024).
- ``--scenario-file`` starts at `gen task` with a hand-written scenario file instead of running
  `gen scenario`, which needs an embedding endpoint (awm/core/scenario.py:63; ADR-021). `gen task`
  reads only each line's ``name`` and ``description`` (awm/core/task.py:44-45, 126).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from workbench.config import Settings
from workbench.envs.procs import descendants, live_group_members
from workbench.subprocess_env import LLM_VARS, NETWORK_VARS, generated_code_env, pick
from workbench.synth.ledger import Ledger, Price, StepRequests, load_prices, step_requests
from workbench.synth.validate import ValidationReport, parse_check_all

STEPS = ("scenario", "task", "db", "sample", "spec", "env", "verifier")
REQUIRED_ENV = ("OPENAI_API_KEY", "AWM_SYN_OVERRIDE_MODEL", "EMBEDDING_OPENAI_API_KEY")
# Hand-written scenarios: AWM's normalized form (awm/tools.py:335-339) with a local_ prefix and
# no `_<number>` suffix. All 1000 official names are `<category>_<number>`, and two of them start
# with local_ as well (local_search_1, local_services_marketplace_1), so the prefix alone would not
# keep them apart (ADR-011, ADR-021).
LOCAL_SCENARIO_NAME = re.compile(r"local_[a-z0-9_]+")
OFFICIAL_SUFFIX = re.compile(r"_\d+$")
CommandRunner = Callable[[list[str], dict[str, str], Path], int]


class SynthError(RuntimeError):
    """Invalid synthesis request or failed step."""


class SynthInterrupted(SynthError):
    """SIGINT/SIGTERM during a step or the validation; the same command resumes (ADR-022)."""


class SynthBudgetExceeded(SynthError):
    """The run's ledger cost reached ``synth.budget`` (ADR-023)."""


class SynthUpstreamErrors(SynthError):
    """More requests of a step ended in upstream errors than ``synth.max_failed_requests`` (ADR-024)."""


@dataclass(frozen=True)
class StepPlan:
    name: str
    argv: list[str]
    outputs: list[str]


@dataclass
class RunState:
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> RunState:
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def _awm(*args: str) -> list[str]:
    return [sys.executable, "-m", "awm.cli", *args]


def load_scenario_file(path: Path, official_names: set[str] | None = None) -> list[dict[str, str]]:
    """Read a hand-written scenario file: official gen_scenario.jsonl format, local_ names only.

    ``official_names`` (from the official gen_scenario.jsonl, when present) are refused as well.
    """
    if not path.is_file():
        raise SynthError(f"scenario file not found: {path}")
    rows: list[dict[str, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SynthError(f"{path}:{n}: not JSON ({exc})") from exc
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "description"}
            or not all(isinstance(v, str) and v.strip() for v in row.values())
        ):
            expected = '{"name": str, "description": str} as in gen_scenario.jsonl'
            raise SynthError(f"{path}:{n}: expected {expected}")
        name = row["name"]
        if not LOCAL_SCENARIO_NAME.fullmatch(name) or OFFICIAL_SUFFIX.search(name):
            raise SynthError(
                f"{path}:{n}: name must be local_[a-z0-9_]+ without a _<number> suffix (got {name!r})"
            )
        if official_names and name in official_names:
            raise SynthError(f"{path}:{n}: {name!r} is an official scenario name")
        rows.append(row)
    if not rows:
        raise SynthError(f"{path}: no scenarios")
    return rows


def plan_steps(
    run_dir: Path,
    scenarios: int,
    num_tasks: int = 10,
    verifier_mode: str = "sql",
    *,
    skip_scenario: bool = False,
) -> list[StepPlan]:
    r = run_dir.resolve()
    db_dir = str(r / "databases")
    verifier_out = "gen_verifier.jsonl" if verifier_mode == "sql" else "gen_verifier.pure_code.jsonl"

    def p(name: str) -> str:
        return str(r / name)

    steps = [
        StepPlan(
            "scenario",
            _awm(
                "gen",
                "scenario",
                "--input_path",
                p("seed_scenario.jsonl"),
                "--output_path",
                p("gen_scenario.jsonl"),
                "--target_count",
                str(scenarios),
            ),
            ["gen_scenario.jsonl"],
        ),
        StepPlan(
            "task",
            _awm(
                "gen",
                "task",
                "--input",
                p("gen_scenario.jsonl"),
                "--output",
                p("gen_tasks.jsonl"),
                "--num_tasks",
                str(num_tasks),
                "--limit",
                str(scenarios),
            ),
            ["gen_tasks.jsonl"],
        ),
        StepPlan(
            "db",
            _awm(
                "gen",
                "db",
                "--input",
                p("gen_tasks.jsonl"),
                "--output",
                p("gen_db.jsonl"),
                "--database_dir",
                db_dir,
            ),
            ["gen_db.jsonl"],
        ),
        StepPlan(
            "sample",
            _awm(
                "gen",
                "sample",
                "--input_task",
                p("gen_tasks.jsonl"),
                "--input_db",
                p("gen_db.jsonl"),
                "--output",
                p("gen_sample.jsonl"),
                "--database_dir",
                db_dir,
            ),
            ["gen_sample.jsonl"],
        ),
        StepPlan(
            "spec",
            _awm(
                "gen",
                "spec",
                "--input_task",
                p("gen_tasks.jsonl"),
                "--input_db",
                p("gen_db.jsonl"),
                "--output",
                p("gen_spec.jsonl"),
            ),
            ["gen_spec.jsonl"],
        ),
        StepPlan(
            "env",
            _awm(
                "gen",
                "env",
                "--input_spec",
                p("gen_spec.jsonl"),
                "--input_db",
                p("gen_db.jsonl"),
                "--output",
                p("gen_envs.jsonl"),
                "--database_dir",
                db_dir,
            ),
            ["gen_envs.jsonl"],
        ),
        StepPlan(
            "verifier",
            _awm(
                "gen",
                "verifier",
                "--input_task",
                p("gen_tasks.jsonl"),
                "--output",
                p(verifier_out),
                "--database_dir",
                db_dir,
                "--mode",
                verifier_mode,
            ),
            [verifier_out],
        ),
    ]
    return [s for s in steps if not (skip_scenario and s.name == "scenario")]


def judge_step(outputs_ok: bool, requests: StepRequests, max_failed: int) -> tuple[str, str | None]:
    """A step's status and failure reason from its exit, its outputs and its ledger entries.

    Refused requests (budget stop, ADR-023) always fail the step. Requests lost to upstream errors
    fail it above ``max_failed``; up to it, the step is ``done_with_failures`` (ADR-024).
    """
    if requests.refused:
        return "failed", "budget"
    if requests.failed > max_failed:
        return "failed", "upstream_errors"
    if not outputs_ok:
        return "failed", None
    if requests.failed:
        return "done_with_failures", None
    return "done", None


def _counts(by_status: dict[str, int]) -> str:
    return ", ".join(f"{status}: {n}" for status, n in by_status.items())


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def stop_process_tree(root: int, grace_s: float = 5.0) -> list[int]:
    """Stop ``root``'s process group and the process groups of all its descendants (ADR-022).

    AWM starts the servers it tests in their own sessions (awm/core/env.py:161-172), so they are
    not in the step's group; they are found through the process tree while ``root`` still runs.
    Our own process group is never signalled. Returns the groups that were signalled.
    """
    own = os.getpgrp()
    groups = sorted({g for g in map(_pgid, [root, *descendants(root)]) if g is not None and g != own})
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in groups:
            with suppress(ProcessLookupError):
                os.killpg(group, sig)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and any(live_group_members(g) for g in groups):
            time.sleep(0.1)
        if not any(live_group_members(g) for g in groups):
            break
    return groups


def default_command_runner(argv: list[str], env: dict[str, str], log_path: Path) -> int:
    """Run one step in its own process group; if interrupted, stop it and what it started."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # its own process group, see stop_process_tree
        )
        try:
            return proc.wait()
        except BaseException:
            stop_process_tree(proc.pid)
            proc.wait()
            raise


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


@contextmanager
def interruptible() -> Iterator[None]:
    """Turn SIGTERM into KeyboardInterrupt (like SIGINT) while a run executes, so both clean up."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class SynthRunner:
    def __init__(
        self,
        settings: Settings,
        run_dir: Path,
        *,
        scenarios: int,
        num_tasks: int = 10,
        verifier_mode: str = "sql",
        command_runner: CommandRunner | None = None,
        environ: dict[str, str] | None = None,
        scenario_file: Path | None = None,
    ) -> None:
        out_root = settings.synth.out_dir.resolve()
        resolved = run_dir.resolve()
        if out_root not in resolved.parents:
            raise SynthError(f"synthesis output must live under {settings.synth.out_dir}/ (got {run_dir})")
        if scenarios < 1:
            raise SynthError("--scenarios must be >= 1")
        if verifier_mode not in ("sql", "code"):
            raise SynthError("--verifier-mode must be sql or code")
        official = settings.env.dataset_dir / "gen_scenario.jsonl"
        self.official_name_check = f"{official} (not present)"
        if scenario_file is not None:
            names = None
            if official.is_file():
                lines = official.read_text(encoding="utf-8").splitlines()
                names = {json.loads(line)["name"] for line in lines if line.strip()}
                self.official_name_check = f"{official} ({len(names)} names)"
            rows = load_scenario_file(scenario_file, names)
            if scenarios > len(rows):
                raise SynthError(f"--scenarios {scenarios} but {scenario_file} has {len(rows)} scenario(s)")
        self.settings = settings
        self.run_dir = run_dir
        self.scenarios = scenarios
        self.num_tasks = num_tasks
        self.verifier_mode = verifier_mode
        self.scenario_file = scenario_file
        self.plan = plan_steps(
            run_dir, scenarios, num_tasks, verifier_mode, skip_scenario=scenario_file is not None
        )
        # only `gen scenario` needs the embedding key (awm/core/scenario.py:63)
        self.required_env = tuple(
            k for k in REQUIRED_ENV if scenario_file is None or k != "EMBEDDING_OPENAI_API_KEY"
        )
        self._run = command_runner or default_command_runner
        self._environ = dict(os.environ if environ is None else environ)
        self.state_path = run_dir / "state.json"
        self.ledger = Ledger(run_dir / "ledger.jsonl")

    # ------------------------------------------------------------------ dry run
    def describe(self) -> dict[str, Any]:
        state = RunState.load(self.state_path)
        return {
            "run_dir": str(self.run_dir),
            "mode": "dry-run",
            "origin": "local-synth",
            "seed": (
                f"{self.scenario_file} (hand-written; copied to gen_scenario.jsonl, gen scenario skipped)"
                if self.scenario_file is not None
                else str(self.settings.upstream.awm_dir / "outputs" / "seed_scenario.jsonl")
                + " (copied into run dir)"
            ),
            "required_env": list(self.required_env),
            "missing_env": [k for k in self.required_env if not self._environ.get(k)],
            "budget": self._budget_view(),
            "max_failed_requests": self.settings.synth.max_failed_requests,
            "steps": [
                {
                    "name": s.name,
                    "argv": s.argv[2:],
                    "outputs": s.outputs,
                    "status": state.steps.get(s.name, {}).get("status", "pending"),
                }
                for s in self.plan
            ],
            "post": ["awm env reset_db (run databases)", "awm env check_all -> validation report"],
        }

    # ------------------------------------------------------------------ execute
    def _prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        source: dict[str, Any] = {"skipped_steps": []}
        if self.scenario_file is not None:
            data = self.scenario_file.read_bytes()
            dst = self.run_dir / "gen_scenario.jsonl"
            if dst.exists() and dst.read_bytes() != data:
                raise SynthError(f"{dst} exists and differs from {self.scenario_file}")
            dst.write_bytes(data)
            source = {
                "skipped_steps": ["scenario"],
                "scenario_file": str(self.scenario_file),
                "scenario_file_sha256": hashlib.sha256(data).hexdigest(),
                "official_name_check": self.official_name_check,
                "scenario_note": "hand-written local_ scenarios; gen scenario skipped (no embedding API)",
            }
        else:
            seed_dst = self.run_dir / "seed_scenario.jsonl"
            if not seed_dst.exists():
                shutil.copy2(self.settings.upstream.awm_dir / "outputs" / "seed_scenario.jsonl", seed_dst)
        manifest = {
            "origin": "local-synth",
            "official_data": False,
            "run_id": self.run_dir.name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "awm_sha": self.settings.upstream.awm_sha,
            "model": self._environ.get("AWM_SYN_OVERRIDE_MODEL"),
            "scenarios": self.scenarios,
            "num_tasks": self.num_tasks,
            "verifier_mode": self.verifier_mode,
            **source,
            "note": "locally synthesized; not part of AgentWorldModel-1K and never used for training here",
        }
        (self.run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def step_env(self, step: str, proxy_base: str | None) -> dict[str, str]:
        """Environment of one `awm gen` step: the allowlist plus the LLM settings (ADR-019).

        `gen env` and `gen verifier` also run the code they generate (awm/core/env.py:161-172,
        awm/core/verifier.py:104). Through the proxy a step gets a placeholder key and the
        upstream key stays in this process; without it, only the LLM and network variables.
        """
        env = generated_code_env(self._environ)
        if proxy_base:
            base = f"{proxy_base}/step/{step}/v1"
            env.update(pick(self._environ, ("AWM_SYN_OVERRIDE_MODEL",)))
            env.update(
                {
                    "AWM_SYN_LLM_PROVIDER": "openai",
                    "OPENAI_BASE_URL": base,
                    "OPENAI_API_KEY": "workbench-proxy",  # pragma: allowlist secret
                    "EMBEDDING_OPENAI_BASE_URL": base,
                    "EMBEDDING_OPENAI_API_KEY": "workbench-proxy",  # pragma: allowlist secret
                }
            )
        else:
            env.update(pick(self._environ, LLM_VARS + NETWORK_VARS))
        return env

    def _set_aside(self, step: str) -> str | None:
        """Move outputs left by an unfinished attempt of ``step`` to attempts/<step>.<n>/.

        Every attempt of a step starts without its outputs, so a rerun produces what an
        uninterrupted run would. This matters for `gen verifier`: it appends to its output
        (awm/core/verifier.py:172-176) and re-generates the rows that do not validate, while
        `awm verify` takes the first matching row (awm/tools.py:456-472), so a stale row would
        shadow the new one. Requests the earlier attempt already made are served by the proxy
        cache. Nothing is deleted; returns the directory relative to the run, or None.
        """
        plan = next(s for s in self.plan if s.name == step)
        stale = [o for o in plan.outputs if (self.run_dir / o).exists()]
        if not stale:
            return None
        n = 1
        while (self.run_dir / "attempts" / f"{step}.{n}").exists():
            n += 1
        dest = self.run_dir / "attempts" / f"{step}.{n}"
        dest.mkdir(parents=True)
        for name in stale:
            (self.run_dir / name).rename(dest / name)
        return str(dest.relative_to(self.run_dir))

    def _budget_view(self) -> dict[str, Any]:
        currency, prices = load_prices(self.settings.synth.pricing_file)
        spent, unpriced = self.ledger.spent(prices)
        view: dict[str, Any] = {"limit": self.settings.synth.budget, "currency": currency}
        view["spent_so_far"] = round(spent, 4)
        if unpriced:
            view["unpriced_models"] = unpriced
        return view

    def _budget_block(self, prices: dict[str, Price], currency: str) -> str | None:
        """Why no further step may start (budget reached, or it cannot be enforced), or None."""
        budget = self.settings.synth.budget
        if budget is None:
            return None
        model = self._environ.get("AWM_SYN_OVERRIDE_MODEL", "")
        if model not in prices:
            return f"model {model!r} has no price in {self.settings.synth.pricing_file}"
        spent, unpriced = self.ledger.spent(prices)
        if unpriced:
            return f"the ledger has models without a price ({', '.join(unpriced)})"
        if spent >= budget:
            return f"budget reached: spent {spent:.4f} {currency} of {budget} {currency}"
        return None

    def execute(self, proxy_base: str | None = None, validate: bool = True) -> dict[str, Any]:
        missing = [k for k in self.required_env if not self._environ.get(k)]
        if missing:
            raise SynthError(f"--execute needs {', '.join(missing)} (see .env.example)")
        currency, prices = load_prices(self.settings.synth.pricing_file)
        self._prepare()
        state = RunState.load(self.state_path)
        with interruptible():
            redo = False  # once a step runs, every later step runs too: its inputs may have changed
            for step in self.plan:
                finished = self._finished(state.steps.get(step.name, {}))
                if not redo and finished and all((self.run_dir / o).exists() for o in step.outputs):
                    continue  # checkpoint: resume after the last completed step
                redo = True
                if reason := self._budget_block(prices, currency):
                    raise SynthBudgetExceeded(
                        f"not starting step {step.name}: {reason}. Raise synth.budget "
                        "(e.g. WORKBENCH_SYNTH__BUDGET) or add the price, then run the same command again."
                    )
                aside = {"set_aside": moved} if (moved := self._set_aside(step.name)) else {}
                seen = len(self.ledger.entries())
                started = time.time()
                try:
                    rc = self._run(
                        step.argv,
                        self.step_env(step.name, proxy_base),
                        self.run_dir / "logs" / f"{step.name}.log",
                    )
                except KeyboardInterrupt as exc:
                    state.steps[step.name] = {
                        "status": "interrupted",
                        "returncode": None,
                        "seconds": round(time.time() - started, 1),
                        **aside,
                    }
                    state.save(self.state_path)
                    raise SynthInterrupted(
                        f"interrupted during step {step.name}; run the same command again to resume"
                    ) from exc
                requests = step_requests(self.ledger.entries()[seen:])
                limit = self.settings.synth.max_failed_requests
                outputs_ok = rc == 0 and all((self.run_dir / o).exists() for o in step.outputs)
                status, reason = judge_step(outputs_ok, requests, limit)
                entry: dict[str, Any] = {
                    "status": status,
                    "returncode": rc,
                    "seconds": round(time.time() - started, 1),
                    **aside,
                }
                if reason:
                    entry["reason"] = reason
                if requests.refused:
                    entry["refused_requests"] = requests.refused
                if requests.failed:
                    entry["failed_requests"] = requests.failed
                    entry["failed_by_status"] = requests.failed_by_status
                state.steps[step.name] = entry
                state.save(self.state_path)
                if reason == "budget":
                    budget = f"{self.settings.synth.budget} {currency}"
                    raise SynthBudgetExceeded(
                        f"step {step.name} failed: the proxy refused {requests.refused} request(s) at "
                        f"synth.budget {budget}. Raise it (e.g. WORKBENCH_SYNTH__BUDGET) and run the "
                        "same command again to resume."
                    )
                if reason == "upstream_errors":
                    raise SynthUpstreamErrors(
                        f"step {step.name} failed: {requests.failed} request(s) ended in an upstream error "
                        f"after every retry ({_counts(requests.failed_by_status)}), more than "
                        f"synth.max_failed_requests={limit}. Fix the upstream or raise the limit (e.g. "
                        "WORKBENCH_SYNTH__MAX_FAILED_REQUESTS), then run the same command again to resume; "
                        "requests already answered are replayed from the cache."
                    )
                if status == "failed":
                    raise SynthError(
                        f"step {step.name} failed (rc={rc}); see {self.run_dir / 'logs' / step.name}.log"
                    )
            result: dict[str, Any] = {"steps": state.steps}
            result["ledger"] = self.ledger.summary(prices, currency)
            (self.run_dir / "ledger_summary.json").write_text(
                json.dumps(result["ledger"], indent=2), encoding="utf-8"
            )
            if validate:
                try:
                    result["validation"] = self.validate().as_dict()
                except KeyboardInterrupt as exc:
                    raise SynthInterrupted(
                        "interrupted during the validation; run the same command again to redo it"
                    ) from exc
        return result

    def _finished(self, entry: dict[str, Any]) -> bool:
        """Whether a recorded step counts as finished under the current settings (ADR-024).

        ``done_with_failures`` counts only while its lost requests stay within
        ``synth.max_failed_requests``; lower the limit and the same command redoes the step.
        """
        if entry.get("status") == "done":
            return True
        limit = self.settings.synth.max_failed_requests
        return entry.get("status") == "done_with_failures" and int(entry.get("failed_requests", 0)) <= limit

    def validate(self) -> ValidationReport:
        # reset_db and check_all run generated SQL and server code and call no LLM: no keys.
        return validate_run(self.run_dir, self._run, generated_code_env(self._environ))


def validate_run(run_dir: Path, run: CommandRunner, env: dict[str, str]) -> ValidationReport:
    """reset_db into the run's databases dir, then check_all; parse into a report."""
    logs = run_dir / "logs"
    run(
        _awm(
            "env",
            "reset_db",
            "--input_db",
            str(run_dir / "gen_db.jsonl"),
            "--input_sample",
            str(run_dir / "gen_sample.jsonl"),
            "--database_dir",
            str(run_dir / "databases"),
        ),
        env,
        logs / "reset_db.log",
    )
    check_log = logs / "check_all.log"
    if check_log.exists():
        check_log.unlink()
    run(_awm("env", "check_all", "--input", str(run_dir / "gen_envs.jsonl")), env, check_log)
    report = parse_check_all(
        check_log.read_text(encoding="utf-8", errors="replace") if check_log.exists() else "", run_dir
    )
    state = RunState.load(run_dir / "state.json")
    report.request_failures = {
        name: {k: s[k] for k in ("status", "failed_requests", "failed_by_status") if k in s}
        for name, s in state.steps.items()
        if s.get("failed_requests")
    }
    (run_dir / "validation.json").write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    (run_dir / "validation.md").write_text(report.markdown(), encoding="utf-8")
    return report


class ProxyThread:
    """Runs the caching/ledger proxy (workbench.synth.proxy) in a background thread."""

    def __init__(self, app: Any, host: str, port: int) -> None:
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.base = f"http://{host}:{port}"

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise SynthError("proxy failed to start")
        return self.base

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
