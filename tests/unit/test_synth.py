from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from workbench.config import Settings
from workbench.synth.ledger import Ledger, LedgerEntry, Price
from workbench.synth.proxy import create_proxy_app
from workbench.synth.runner import STEPS, SynthError, SynthRunner
from workbench.synth.validate import categorize, parse_check_all

ENV = {"OPENAI_API_KEY": "k", "AWM_SYN_OVERRIDE_MODEL": "m", "EMBEDDING_OPENAI_API_KEY": "e"}


def settings(tmp_path: Path) -> Settings:
    return Settings(synth={"out_dir": tmp_path / "synth"})  # type: ignore[arg-type]


class FakeAwm:
    """Pretends to be `python -m awm.cli gen <step>`: writes the step's output files."""

    def __init__(self, fail_once: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once

    def __call__(self, argv: list[str], env: dict[str, str], log: Path) -> int:
        step = argv[argv.index("gen") + 1] if "gen" in argv else argv[argv.index("env") + 1]
        self.calls.append(step)
        if step == self.fail_once:
            self.fail_once = None
            return 1
        for flag in ("--output", "--output_path"):
            if flag in argv:
                Path(argv[argv.index(flag) + 1]).write_text("{}\n", encoding="utf-8")
        return 0


def test_dry_run_plan_is_pure(tmp_path: Path) -> None:
    run_dir = tmp_path / "synth" / "r1"
    r = SynthRunner(settings(tmp_path), run_dir, scenarios=3, environ={})
    plan = r.describe()
    assert plan["mode"] == "dry-run" and plan["origin"] == "local-synth"
    assert [s["name"] for s in plan["steps"]] == list(STEPS)
    assert set(plan["missing_env"]) == set(ENV)
    assert not run_dir.exists()  # dry-run writes nothing
    argv = {s["name"]: s["argv"] for s in plan["steps"]}
    assert argv["scenario"][-1] == "3" and argv["task"][argv["task"].index("--limit") + 1] == "3"
    for s in plan["steps"]:  # every file argument points into the run dir, never the submodule
        for a in s["argv"]:
            if a.startswith("/"):
                assert a.startswith(str(run_dir.resolve())), a


def test_output_must_be_under_data_synth(tmp_path: Path) -> None:
    with pytest.raises(SynthError, match="must live under"):
        SynthRunner(settings(tmp_path), tmp_path / "elsewhere", scenarios=1)
    with pytest.raises(SynthError):
        SynthRunner(settings(tmp_path), tmp_path / "synth" / "x", scenarios=0)


def test_execute_requires_llm_env(tmp_path: Path) -> None:
    r = SynthRunner(
        settings(tmp_path), tmp_path / "synth" / "r", scenarios=1, environ={}, command_runner=FakeAwm()
    )
    with pytest.raises(SynthError, match="needs"):
        r.execute(validate=False)


def test_checkpoint_resume_and_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "synth" / "r2"
    fake = FakeAwm(fail_once="db")
    r = SynthRunner(settings(tmp_path), run_dir, scenarios=2, environ=ENV, command_runner=fake)
    with pytest.raises(SynthError, match="step db failed"):
        r.execute(validate=False)
    state = json.loads((run_dir / "state.json").read_text())
    assert state["steps"]["task"]["status"] == "done" and state["steps"]["db"]["status"] == "failed"
    fake.calls.clear()
    result = r.execute(validate=False)
    assert fake.calls == ["db", "sample", "spec", "env", "verifier"]  # resumed after 'task'
    assert all(v["status"] == "done" for v in result["steps"].values())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["origin"] == "local-synth" and manifest["official_data"] is False
    seed = (run_dir / "seed_scenario.jsonl").read_text()
    assert seed == Path("third_party/agent-world-model/outputs/seed_scenario.jsonl").read_text()


def test_step_env_points_awm_at_proxy(tmp_path: Path) -> None:
    r = SynthRunner(settings(tmp_path), tmp_path / "synth" / "r", scenarios=1, environ=ENV)
    env = r.step_env("task", "http://127.0.0.1:9")
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9/step/task/v1"
    assert env["AWM_SYN_LLM_PROVIDER"] == "openai" and env["AWM_SYN_OVERRIDE_MODEL"] == "m"
    # the real key stays in the proxy only
    assert env["OPENAI_API_KEY"] == "workbench-proxy"  # pragma: allowlist secret


def test_ledger_costs(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.jsonl")
    led.record(LedgerEntry("task", "m", 1_000_000, 500_000, cached=False))
    led.record(LedgerEntry("task", "m", 1_000_000, 500_000, cached=True))
    led.record(LedgerEntry("db", "other", 10, 10, cached=False))
    s = led.summary({"m": Price(2.0, 8.0)})
    assert s["steps"]["task"]["cost"] == 6.0 and s["steps"]["task"]["cached_calls"] == 1
    assert s["steps"]["db"]["unpriced_models"] == ["other"] and s["total"]["complete"] is False


def test_proxy_caches_retries_and_records(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200, json={"model": "m", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
            ),
        ]
    )

    def upstream(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return next(responses)

    ledger = Ledger(tmp_path / "ledger.jsonl")
    app = create_proxy_app(
        upstream_base_url="http://up/v1",
        upstream_api_key="secret",  # pragma: allowlist secret
        cache_dir=tmp_path / "cache",
        ledger=ledger,
        backoff_s=0.0,
        transport=httpx.MockTransport(upstream),
    )
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        r1 = c.post("/step/task/v1/chat/completions", json=body)
        r2 = c.post("/step/task/v1/chat/completions", json=body)
    assert r1.status_code == 200 and r2.json() == r1.json()
    assert len(calls) == 2  # 503 + retry; the second client request is a cache hit
    entries = ledger.entries()
    assert [(e.step, e.cached, e.prompt_tokens) for e in entries] == [("task", False, 5), ("task", True, 0)]


def test_validation_parsing(tmp_path: Path) -> None:
    (tmp_path / "gen_envs.jsonl").write_text(
        json.dumps({"scenario": "a", "full_code": "operation_id='x' operation_id='y'"})
        + "\n"
        + json.dumps({"scenario": "b", "full_code": ""})
        + "\n",
        encoding="utf-8",
    )
    log = "... | SUCCESS | PASSED: a\n... | ERROR | FAILED: b\n    ModuleNotFoundError: No module named 'x'\n"
    rep = parse_check_all(log, tmp_path)
    d = rep.as_dict()
    assert d["environments_total"] == 2 and d["environments_started"] == 1 and d["tools_total"] == 2
    assert d["failure_categories"] == {"import_error": 1}
    assert "failure category" in rep.markdown()
    assert categorize("sqlalchemy.exc.ArgumentError") == "sqlalchemy_error"
    assert categorize("weird") == "other"


def test_validation_parsing_strips_ansi_colors(tmp_path: Path) -> None:
    # loguru colors its output on GitHub Actions; names must still match the catalog.
    (tmp_path / "gen_envs.jsonl").write_text(
        json.dumps({"scenario": "a", "full_code": "operation_id='x'"}) + "\n", encoding="utf-8"
    )
    log = "\x1b[1mPASSED: a\x1b[0m\n"
    d = parse_check_all(log, tmp_path).as_dict()
    assert d["environments_started"] == 1 and d["tools_total"] == 1
