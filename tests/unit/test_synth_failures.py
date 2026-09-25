"""Upstream errors during synthesis: the proxy records them, the runner judges the step (D19, ADR-024).

Zero cost. The proxy talks to httpx mock transports, or to the local fake upstream through a
transport that scripts 402, 429, 5xx and network errors; the steps are real subprocesses that,
like AWM, turn a request that keeps failing into an empty reply and still exit 0.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from tests.unit.synth_harness import (
    MODEL,
    ScriptedTransport,
    expected_reply,
    fake_step_runner,
    fake_upstream,
    free_port,
    request_body,
)
from workbench.config import Settings
from workbench.synth.ledger import Ledger, LedgerEntry, StepRequests, step_requests
from workbench.synth.proxy import create_proxy_app
from workbench.synth.runner import (
    STEPS,
    ProxyThread,
    SynthRunner,
    SynthUpstreamErrors,
    judge_step,
    validate_run,
)

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
REQUESTS = 2


# ------------------------------------------------------------------ the proxy


@pytest.mark.parametrize(
    ("outcome", "client_status", "upstream_status", "attempts"),
    [
        ("402", 402, 402, 1),  # e.g. DeepSeek's insufficient balance: passed on, not retried
        ("429", 429, 429, 1),  # rate limit: passed on (AWM and the openai SDK retry it)
        ("503", 502, 503, 3),  # server error: retried by the proxy, then 502
        ("network", 502, None, 3),  # connection error: retried by the proxy, then 502
    ],
)
def test_proxy_records_an_upstream_error_as_failed(
    tmp_path: Path, outcome: str, client_status: int, upstream_status: int | None, attempts: int
) -> None:
    calls: list[bytes] = []

    def upstream(req: httpx.Request) -> httpx.Response:
        calls.append(req.content)
        if outcome == "network":
            raise httpx.ConnectError("connection refused", request=req)
        return httpx.Response(int(outcome), json={"error": {"message": f"upstream {outcome}"}})

    ledger = Ledger(tmp_path / "ledger.jsonl")
    app = create_proxy_app(
        upstream_base_url="http://up/v1",
        upstream_api_key=None,
        cache_dir=tmp_path / "cache",
        ledger=ledger,
        max_retries=2,
        backoff_s=0.0,
        transport=httpx.MockTransport(upstream),
    )
    with TestClient(app) as c:
        r = c.post("/step/task/v1/chat/completions", json=BODY)
    assert r.status_code == client_status and "error" in r.json()
    assert len(calls) == attempts
    [entry] = ledger.entries()
    assert (entry.failed, entry.upstream_status, entry.cached, entry.refused) == (
        True,
        upstream_status,
        False,
        False,
    )
    assert entry.key and entry.error and not list((tmp_path / "cache").iterdir())  # nothing cached
    assert ledger.summary({})["steps"]["task"]["failed_calls"] == 1
    assert step_requests([entry]).failed_by_status == {"network" if upstream_status is None else outcome: 1}


def test_proxy_passes_on_error_bodies_that_are_not_json(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    app = create_proxy_app(
        upstream_base_url="http://up/v1",
        upstream_api_key=None,
        cache_dir=tmp_path / "cache",
        ledger=ledger,
        transport=httpx.MockTransport(lambda req: httpx.Response(429, text="<html>slow down</html>")),
    )
    with TestClient(app) as c:
        r = c.post("/step/task/v1/chat/completions", json=BODY)
    assert r.status_code == 429 and "slow down" in r.json()["error"]["message"]
    assert [(e.failed, e.upstream_status) for e in ledger.entries()] == [(True, 429)]


# ------------------------------------------------------------------ judging a step


def fail(key: str, status: int | None) -> LedgerEntry:
    return LedgerEntry("task", "m", 0, 0, cached=False, failed=True, upstream_status=status, key=key)


def test_step_requests_counts_requests_and_the_last_attempt_decides() -> None:
    answered = LedgerEntry("task", "m", 1, 1, cached=False, key="a")
    # a request that failed and was answered on a retry (AWM retries with the same body) is not lost
    assert step_requests([fail("a", 503), answered]) == StepRequests()
    # two attempts of one request count once; its last attempt gives the status
    got = step_requests([fail("b", 503), fail("b", None), fail("c", 402)])
    assert (got.failed, got.failed_by_status) == (2, {"402": 1, "network": 1})
    refused = LedgerEntry("task", "m", 0, 0, cached=False, refused=True, key="d")
    assert step_requests([refused]) == StepRequests(refused=1)
    # ledgers written before Phase 15 have no keys: every failed entry stands alone
    assert step_requests([fail("", 429), fail("", 429)]).failed == 2


@pytest.mark.parametrize(
    ("outputs_ok", "requests", "limit", "expected"),
    [
        (True, StepRequests(), 0, ("done", None)),
        (True, StepRequests(failed=1), 0, ("failed", "upstream_errors")),
        (True, StepRequests(failed=1), 1, ("done_with_failures", None)),
        (True, StepRequests(failed=2), 1, ("failed", "upstream_errors")),
        (False, StepRequests(failed=1), 1, ("failed", None)),
        (False, StepRequests(), 0, ("failed", None)),
        (True, StepRequests(refused=1), 5, ("failed", "budget")),  # the budget stop always fails
    ],
)
def test_judge_step(
    outputs_ok: bool, requests: StepRequests, limit: int, expected: tuple[str, str | None]
) -> None:
    assert judge_step(outputs_ok, requests, limit) == expected


def test_max_failed_requests_is_not_negative() -> None:
    assert Settings().synth.max_failed_requests == 0
    with pytest.raises(ValidationError):
        Settings(synth={"max_failed_requests": -1})  # type: ignore[arg-type]


# ------------------------------------------------------------------ whole runs


@contextmanager
def pipeline(root: Path, script: dict[bytes, list[str]]) -> Iterator[tuple[str, ScriptedTransport]]:
    """The fake upstream and the workbench proxy (1 retry, as scripted by ``script``)."""
    transport = ScriptedTransport(script)
    with ProxyThread(fake_upstream(), "127.0.0.1", free_port()) as upstream:
        app = create_proxy_app(
            upstream_base_url=upstream,
            upstream_api_key=None,
            cache_dir=root / "r" / "llm_cache",
            ledger=Ledger(root / "r" / "ledger.jsonl"),
            max_retries=1,
            backoff_s=0.0,
            transport=transport,
        )
        with ProxyThread(app, "127.0.0.1", free_port()) as proxy:
            yield proxy, transport


def run(root: Path, proxy: str, limit: int, attempts: int = 1) -> dict[str, Any]:
    settings = Settings(synth={"out_dir": root, "budget": None, "max_failed_requests": limit})  # type: ignore[arg-type]
    environ = {"PATH": os.environ["PATH"], "OPENAI_API_KEY": "k", "AWM_SYN_OVERRIDE_MODEL": MODEL}
    environ["EMBEDDING_OPENAI_API_KEY"] = "e"  # pragma: allowlist secret
    runner = SynthRunner(
        settings,
        root / "r",
        scenarios=1,
        command_runner=fake_step_runner(root, REQUESTS, attempts=attempts),
        environ=environ,
    )
    return runner.execute(proxy_base=proxy, validate=False)


def steps(root: Path) -> dict[str, Any]:
    return dict(json.loads((root / "r" / "state.json").read_text())["steps"])


def rows(root: Path, step: str) -> list[dict[str, Any]]:
    path = root / "r" / "ledger.jsonl"
    return [r for line in path.read_text().splitlines() if (r := json.loads(line))["step"] == step]


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def replies(step: str) -> list[str]:
    return [expected_reply(request_body(MODEL, step, i)) for i in range(REQUESTS)]


def test_an_upstream_error_fails_the_step_and_the_same_command_resumes(tmp_path: Path) -> None:
    message = r"1 request\(s\).*402: 1.*max_failed_requests=0"
    script = {request_body(MODEL, "task", 1): ["402"]}
    with pipeline(tmp_path, script) as (proxy, _), pytest.raises(SynthUpstreamErrors, match=message):
        run(tmp_path, proxy, limit=0)
    state = steps(tmp_path)
    assert state["scenario"]["status"] == "done"
    task = state["task"]
    assert (task["status"], task["reason"], task["returncode"]) == ("failed", "upstream_errors", 0)
    assert (task["failed_requests"], task["failed_by_status"]) == (1, {"402": 1})
    # like AWM, the step wrote an empty reply for the lost request and exited 0: only the ledger tells
    assert lines(tmp_path / "r" / "gen_tasks.jsonl") == [replies("task")[0], ""]

    with pipeline(tmp_path, {}) as (proxy, _):  # the upstream answers again: the same command resumes
        result = run(tmp_path, proxy, limit=0)
    assert all(s["status"] == "done" for s in result["steps"].values())
    assert lines(tmp_path / "r" / "gen_tasks.jsonl") == replies("task")
    # attempt 1: answered, failed (402); attempt 2: the answered request comes from the cache
    got = [(r["cached"], r["failed"]) for r in rows(tmp_path, "task")]
    assert got == [(False, False), (False, True), (True, False), (False, False)]


def test_failures_within_the_limit_mark_the_step_done_with_failures(tmp_path: Path) -> None:
    with pipeline(tmp_path, {request_body(MODEL, "task", 1): ["429"]}) as (proxy, _):
        result = run(tmp_path, proxy, limit=1)
    task = result["steps"]["task"]
    assert (task["status"], task["failed_requests"], task["failed_by_status"]) == (
        "done_with_failures",
        1,
        {"429": 1},
    )
    assert "reason" not in task
    assert all(result["steps"][s]["status"] == "done" for s in STEPS if s != "task")  # the run went on
    assert result["ledger"]["steps"]["task"]["failed_calls"] == 1

    before = len(rows(tmp_path, "task"))
    with pipeline(tmp_path, {}) as (proxy, _):  # same limit: the step counts as finished
        run(tmp_path, proxy, limit=1)
    assert len(rows(tmp_path, "task")) == before and steps(tmp_path)["task"]["status"] == "done_with_failures"

    with pipeline(tmp_path, {}) as (proxy, _):  # a stricter limit redoes it, and every later step
        run(tmp_path, proxy, limit=0)
    state = steps(tmp_path)
    assert all(state[s]["status"] == "done" for s in STEPS)
    assert state["task"]["set_aside"] == "attempts/task.1"
    assert all(state[s]["set_aside"] == f"attempts/{s}.1" for s in STEPS[STEPS.index("task") + 1 :])
    assert "set_aside" not in state["scenario"]
    assert lines(tmp_path / "r" / "gen_tasks.jsonl") == replies("task")


def test_5xx_and_network_errors_count_after_the_proxy_retries(tmp_path: Path) -> None:
    script = {
        request_body(MODEL, "task", 0): ["503", "503"],
        request_body(MODEL, "task", 1): ["network", "network"],
    }
    with pipeline(tmp_path, script) as (proxy, transport):
        with pytest.raises(SynthUpstreamErrors, match="503: 1, network: 1"):
            run(tmp_path, proxy, limit=1)
        # the proxy retried each request once: two attempts each reached the transport
        assert [transport.seen[body] for body in script] == [2, 2]
    assert [(r["failed"], r["upstream_status"]) for r in rows(tmp_path, "task")] == [
        (True, 503),
        (True, None),
    ]
    task = steps(tmp_path)["task"]
    assert (task["failed_requests"], task["failed_by_status"]) == (2, {"503": 1, "network": 1})


def test_a_request_answered_on_a_retry_is_not_lost(tmp_path: Path) -> None:
    # the step's first attempt at request 0 fails after the proxy's retry; its second attempt works
    with pipeline(tmp_path, {request_body(MODEL, "task", 0): ["503", "503"]}) as (proxy, _):
        result = run(tmp_path, proxy, limit=0, attempts=2)
    assert result["steps"]["task"]["status"] == "done" and "failed_requests" not in result["steps"]["task"]
    got = [(r["failed"], r["cached"]) for r in rows(tmp_path, "task")]
    assert got == [(True, False), (False, False), (False, False)]
    assert lines(tmp_path / "r" / "gen_tasks.jsonl") == replies("task")


# ------------------------------------------------------------------ validation output


def test_validation_lists_steps_with_failed_requests(tmp_path: Path) -> None:
    run_dir = tmp_path / "synth" / "r"
    run_dir.mkdir(parents=True)
    env = {"scenario": "s", "full_code": "operation_id='x'"}
    (run_dir / "gen_envs.jsonl").write_text(json.dumps(env) + "\n", encoding="utf-8")
    failures = {
        "status": "done_with_failures",
        "failed_requests": 2,
        "failed_by_status": {"429": 1, "network": 1},
    }
    state = {
        "steps": {"task": {"status": "done", "returncode": 0}, "verifier": {**failures, "returncode": 0}}
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def fake_awm_env(argv: list[str], env: dict[str, str], log: Path) -> int:
        log.parent.mkdir(parents=True, exist_ok=True)
        if "check_all" in argv:
            log.write_text("PASSED: s\n", encoding="utf-8")
        return 0

    report = validate_run(run_dir, fake_awm_env, {})
    assert report.as_dict()["gen_steps_with_failed_requests"] == {"verifier": failures}
    saved = json.loads((run_dir / "validation.json").read_text())
    assert saved["gen_steps_with_failed_requests"]["verifier"]["failed_requests"] == 2
    assert (
        "| verifier | done_with_failures | 2 | 429: 1, network: 1 |"
        in (run_dir / "validation.md").read_text()
    )
