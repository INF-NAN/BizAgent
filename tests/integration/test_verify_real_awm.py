"""`workbench verify` against the real `awm verify` (ADR-025, owner decision D13a).

The test plants secrets in the environment `workbench verify` is started with, and a verifier
whose code reports what it can see from inside `awm verify`. The sql judge is a local fake
behind the workbench proxy. No LLM is called.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.unit.secrets_harness import TREE_EXTRA, leaked, unexpected
from tests.unit.synth_harness import free_port
from workbench.config import Settings
from workbench.synth.proxy import PLACEHOLDER_KEY
from workbench.synth.runner import ProxyThread, default_command_runner
from workbench.verify import VerifyError, run_verify, verify_argv

pytestmark = pytest.mark.integration

PLANTED = {
    "DEEPSEEK_API_KEY": "planted-deepseek-key",  # pragma: allowlist secret
    "OPENAI_API_KEY": "planted-upstream-key",  # pragma: allowlist secret
    "HF_TOKEN": "planted-hf-token",  # pragma: allowlist secret
}

# Reports only stable facts, so the judge's prompt (which embeds the result) is the same on a rerun.
SPY = """
def {func}(initial_db_path, final_db_path, final_answer=""):
    import os
    return {{
        "result": "complete",
        "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        "names": sorted(os.environ),
        "planted": sorted(k for k, v in os.environ.items() if v.startswith("planted-")),
    }}
"""


def agent_output(tmp_path: Path) -> Path:
    """What `awm agent` leaves behind, reduced to what `awm verify` reads (awm/core/verify.py:350-373)."""
    out = tmp_path / "awm-agent" / "s_task_0"
    out.mkdir(parents=True)
    trajectory = {
        "scenario": "s",
        "task_id": 0,
        "task": "Add the item to the cart",
        "trajectory": [],
        "messages": [{"role": "assistant", "content": "done"}],
    }
    (out / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
    for name in ("initial.db", "final.db"):
        with sqlite3.connect(out / name) as con:
            con.execute("CREATE TABLE cart_items (id INTEGER PRIMARY KEY)")
    return out


def verifier_file(tmp_path: Path, name: str, func: str) -> Path:
    path = tmp_path / name
    row = {"scenario": "s", "task_idx": 0, "verification": {"code": SPY.format(func=func)}}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def fake_judge(calls: list[dict[str, Any]]) -> Starlette:
    """An OpenAI-compatible judge that answers "complete" and records what reached it."""

    async def chat(request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        calls.append({"authorization": request.headers.get("authorization"), "model": body["model"]})
        verdict = {"reasoning": "fake", "confidence_score": [100, 0, 0, 0], "classification": "complete"}
        message = {"role": "assistant", "content": json.dumps(verdict)}
        return JSONResponse(
            {
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    return Starlette(routes=[Route("/chat/completions", chat, methods=["POST"])])


def seen_by_verifier(out: Path, mode: str) -> dict[str, Any]:
    result = json.loads((out / f"verify.{mode}.json").read_text())["verify_result"]
    return dict(result["raw_result"] if mode == "code" else result)


def test_control_awm_verify_with_the_full_environment_sees_every_key(tmp_path: Path) -> None:
    # how Phase 12 ran it: `awm verify` straight from a shell that holds the keys
    out = agent_output(tmp_path)
    verifier = verifier_file(tmp_path, "pure_code.jsonl", "verify_task_completion")
    argv = verify_argv(out, "code", verifier)
    assert default_command_runner(argv, {**os.environ, **PLANTED}, out / "control.log") == 0
    assert seen_by_verifier(out, "code")["planted"] == sorted(PLANTED)


def test_code_mode_runs_the_verifier_without_any_key(tmp_path: Path) -> None:
    out = agent_output(tmp_path)
    verifier = verifier_file(tmp_path, "pure_code.jsonl", "verify_task_completion")
    environ = {**os.environ, **PLANTED}
    summary = run_verify(Settings(), out, mode="code", verifier=verifier, environ=environ)
    assert summary["reward_type"] == "complete" and "judge_calls" not in summary
    seen = seen_by_verifier(out, "code")
    assert seen["planted"] == [] and seen["openai_api_key"] is None
    # deny-first: nothing outside the allowlist, apart from what AWM's process sets itself
    assert leaked(seen["names"]) == [] and unexpected(seen["names"], TREE_EXTRA) == []


def test_sql_mode_judges_through_the_proxy_and_the_key_stays_outside(tmp_path: Path) -> None:
    out = agent_output(tmp_path)
    verifier = verifier_file(tmp_path, "sql.jsonl", "verify_task")
    calls: list[dict[str, Any]] = []
    with ProxyThread(fake_judge(calls), "127.0.0.1", free_port()) as judge:
        environ = {**os.environ, **PLANTED, "OPENAI_BASE_URL": judge, "AWM_SYN_OVERRIDE_MODEL": "judge-model"}
        summary = run_verify(Settings(), out, mode="sql", verifier=verifier, environ=environ)
        assert summary["judge_classification"] == "complete"
        assert summary["judge_calls"] == {"upstream": 1, "cached": 0, "failed": 0}
        # the real key reached the judge through the proxy; the verifier code saw only the placeholder
        assert calls == [{"authorization": f"Bearer {PLANTED['OPENAI_API_KEY']}", "model": "judge-model"}]
        seen = seen_by_verifier(out, "sql")
        assert seen["planted"] == [] and seen["openai_api_key"] == PLACEHOLDER_KEY
        proxy_vars = ("AWM_SYN_LLM_PROVIDER", "OPENAI_BASE_URL", "OPENAI_API_KEY", "AWM_SYN_OVERRIDE_MODEL")
        assert unexpected(seen["names"], (*TREE_EXTRA, *proxy_vars)) == []

        # the same verification again: the judgment comes from the cache, no new call
        again = run_verify(Settings(), out, mode="sql", verifier=verifier, environ=environ)
    assert again["judge_classification"] == "complete" and len(calls) == 1
    assert again["judge_calls"] == {"upstream": 0, "cached": 1, "failed": 0}
    assert (out / "verify_ledger.jsonl").is_file()


def test_sql_mode_needs_a_judge_and_the_input_must_be_an_agent_output(tmp_path: Path) -> None:
    out = agent_output(tmp_path)
    verifier = verifier_file(tmp_path, "sql.jsonl", "verify_task")
    with pytest.raises(VerifyError, match="needs the judge"):
        run_verify(Settings(), out, mode="sql", verifier=verifier, environ={"PATH": os.environ["PATH"]})
    with pytest.raises(VerifyError, match=r"no trajectory\.json"):
        run_verify(Settings(), tmp_path, verifier=verifier, environ={})
    with pytest.raises(VerifyError, match="--mode"):
        run_verify(Settings(), out, mode="llm", verifier=verifier, environ={})
