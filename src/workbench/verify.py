"""`workbench verify`: run `awm verify` on an `awm agent` output without handing it a key (ADR-025).

`awm verify` executes the dataset's verifier code in its own process, with `os` in the namespace
(awm/core/verify.py:104-126, 151-174), and in sql mode that same process reads the judge's key
from its environment (awm/core/verify.py:417-419, awm/tools.py:386-437). This wrapper keeps every
key out of that process:

- ``code`` mode never calls an LLM (only sql mode runs the judge, awm/core/verify.py:416-433): the
  process gets only the allowlist of ADR-019.
- ``sql`` mode sends the judge's request through the local proxy (workbench.synth.proxy): the
  process gets the allowlist, the proxy's address, a placeholder key and the judge model, while
  the judge's real address and key stay in this process. The proxy writes a ledger next to the
  output and caches the answer, so running the same verification again returns the recorded
  judgment without a new call.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from workbench.config import Settings
from workbench.subprocess_env import generated_code_env
from workbench.synth.ledger import Ledger
from workbench.synth.proxy import PLACEHOLDER_KEY, create_proxy_app
from workbench.synth.runner import CommandRunner, ProxyThread, default_command_runner

MODES = ("code", "sql")
# the verifier files the official dataset ships for each mode (awm/core/verify.py:376-379)
DEFAULT_VERIFIERS = {"code": "gen_verifier.pure_code.jsonl", "sql": "gen_verifier.jsonl"}


class VerifyError(RuntimeError):
    """Invalid request, or `awm verify` did not finish."""


def verify_argv(
    input_dir: Path, mode: str, verifier: Path, init_db: Path | None = None, final_db: Path | None = None
) -> list[str]:
    """The `awm verify` command (flags: awm/core/verify.py:37-49)."""
    flag = "--verifier_code_path" if mode == "code" else "--verifier_path"
    argv = [sys.executable, "-m", "awm.cli", "verify", "--input", str(input_dir), "--mode", mode]
    argv += [flag, str(verifier)]
    if init_db is not None:
        argv += ["--init_db_path", str(init_db)]
    if final_db is not None:
        argv += ["--final_db_path", str(final_db)]
    return argv


def verify_env(environ: dict[str, str], proxy_base: str | None = None, model: str = "") -> dict[str, str]:
    """The allowlist of ADR-019; behind the proxy (sql mode) also its address, a placeholder key
    and the judge model, which is all `resolve_llm_config` reads (awm/tools.py:386-437)."""
    env = generated_code_env(environ)
    if proxy_base is not None:
        env.update(
            {
                "AWM_SYN_LLM_PROVIDER": "openai",
                "OPENAI_BASE_URL": f"{proxy_base}/step/verify/v1",
                "OPENAI_API_KEY": PLACEHOLDER_KEY,
                "AWM_SYN_OVERRIDE_MODEL": model,
            }
        )
    return env


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def run_verify(
    settings: Settings,
    input_dir: Path,
    *,
    mode: str = "code",
    verifier: Path | None = None,
    init_db: Path | None = None,
    final_db: Path | None = None,
    judge_model: str | None = None,
    environ: dict[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Run `awm verify` once and summarise its `verify.<mode>.json`."""
    if mode not in MODES:
        raise VerifyError(f"--mode must be one of {', '.join(MODES)}")
    env_in = dict(os.environ if environ is None else environ)
    input_dir = input_dir.resolve()
    if not (input_dir / "trajectory.json").is_file():
        raise VerifyError(f"{input_dir} has no trajectory.json (expected an `awm agent` output directory)")
    verifier = (verifier or settings.env.dataset_dir / DEFAULT_VERIFIERS[mode]).resolve()
    if not verifier.is_file():
        raise VerifyError(f"verifier file not found: {verifier}")
    argv = verify_argv(
        input_dir,
        mode,
        verifier,
        init_db.resolve() if init_db else None,
        final_db.resolve() if final_db else None,
    )
    run = command_runner or default_command_runner
    log = input_dir / f"verify.{mode}.log"
    summary: dict[str, Any] = {"mode": mode, "verifier": str(verifier), "log": str(log)}
    if mode == "code":
        rc = run(argv, verify_env(env_in), log)
    else:
        s = settings.synth
        upstream = env_in.get(s.upstream_base_url_env)
        model = judge_model or env_in.get("AWM_SYN_OVERRIDE_MODEL", "")
        if not upstream or not model:
            raise VerifyError(
                f"--mode sql needs the judge's endpoint in {s.upstream_base_url_env} and its model "
                "(--judge-model or AWM_SYN_OVERRIDE_MODEL)"
            )
        ledger = Ledger(input_dir / "verify_ledger.jsonl")
        seen = len(ledger.entries())
        app = create_proxy_app(
            upstream_base_url=upstream,
            upstream_api_key=env_in.get(s.upstream_api_key_env),
            cache_dir=input_dir / "verify_llm_cache",
            ledger=ledger,
        )
        with ProxyThread(app, s.proxy_host, _free_port(s.proxy_host)) as base:
            rc = run(argv, verify_env(env_in, base, model), log)
        calls = ledger.entries()[seen:]
        summary["judge_calls"] = {
            "upstream": sum(not (e.cached or e.failed or e.refused) for e in calls),
            "cached": sum(e.cached for e in calls),
            "failed": sum(e.failed for e in calls),
        }
    out = input_dir / f"verify.{mode}.json"
    if rc != 0 or not out.is_file():
        raise VerifyError(f"awm verify did not finish (rc={rc}); see {log}")
    result = json.loads(out.read_text(encoding="utf-8"))
    summary.update({"output": str(out), "reward_type": result.get("reward_type")})
    if mode == "sql":
        summary["judge_classification"] = (result.get("llm_judge") or {}).get("classification")
    return summary
