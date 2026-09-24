"""Mid-step interruption and the budget stop, end to end against a local fake upstream (D17).

Real processes throughout: the driver runs SynthRunner and the workbench proxy, every step is a
subprocess that talks to the proxy, and the fake upstream listens on a real port. No paid API.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from tests.unit.synth_harness import (
    MODEL,
    expected_reply,
    fake_upstream,
    free_port,
    request_body,
    start_driver,
)
from workbench.synth.ledger import Ledger, LedgerEntry, Price
from workbench.synth.proxy import create_proxy_app
from workbench.synth.runner import STEPS, ProxyThread

REQUESTS = 3


@pytest.fixture
def upstream() -> Iterator[str]:
    with ProxyThread(fake_upstream(delay_s=0.3), "127.0.0.1", free_port()) as base:
        yield base


def ledger_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_for(check: Any, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def replies(step: str, n: int = REQUESTS) -> list[str]:
    """What an uninterrupted run writes for ``step``: one deterministic reply per request."""
    return [expected_reply(request_body(MODEL, step, i)) for i in range(n)]


# `db` writes its output at the end; `verifier` appends as it goes, like AWM's gen verifier
@pytest.mark.parametrize(("step", "output"), [("db", "gen_db.jsonl"), ("verifier", "gen_verifier.jsonl")])
def test_interrupt_mid_step_then_resume(tmp_path: Path, upstream: str, step: str, output: str) -> None:
    run_dir = tmp_path / "synth" / "r"
    driver = start_driver(run_dir, upstream, requests=REQUESTS, server_step=step)
    drivers = [driver]
    server_pidfile = run_dir.parent / f"{step}.server.pid"
    partial = 1 if step == "verifier" else 0  # rows on disk when the signal arrives
    try:

        def calls() -> int:
            return sum(r["step"] == step for r in ledger_rows(run_dir))

        # interrupt after the step's first response, while its second request is in flight
        wait_for(lambda: calls() == 1 and server_pidfile.exists() and len(lines(run_dir / output)) == partial)
        driver.send_signal(signal.SIGTERM)  # the runner's own PID: nothing to guess about groups
        assert driver.wait(timeout=60) == 130
        state = json.loads((run_dir / "state.json").read_text())["steps"]
        assert all(state[s]["status"] == "done" for s in STEPS[: STEPS.index(step)])
        assert state[step]["status"] == "interrupted"
        assert 1 <= calls() < REQUESTS  # the interruption really landed inside the step
        assert lines(run_dir / output) == replies(step, partial)
        # the "test server" the step started in its own session was stopped as well
        assert not alive(int(server_pidfile.read_text()))

        drivers.append(start_driver(run_dir, upstream, requests=REQUESTS, server_step=step))
        assert drivers[-1].wait(timeout=120) == 0
    finally:
        for proc in drivers:
            if proc.poll() is None:
                proc.kill()
        if server_pidfile.exists() and alive(pid := int(server_pidfile.read_text())):
            os.kill(pid, signal.SIGKILL)
    state = json.loads((run_dir / "state.json").read_text())["steps"]
    assert all(state[s]["status"] == "done" for s in STEPS)
    rows = [r for r in ledger_rows(run_dir) if r["step"] == step]
    assert rows[-REQUESTS]["cached"] is True  # the response received before the interruption: cache hit
    assert sum(not r["cached"] for r in rows) == REQUESTS  # every request reached the upstream once
    # results are what an uninterrupted run produces; a partial output was set aside, not appended to
    for s, name in {"db": "gen_db.jsonl", "verifier": "gen_verifier.jsonl"}.items():
        assert lines(run_dir / name) == replies(s)
    if partial:
        assert state[step]["set_aside"] == f"attempts/{step}.1"
        assert lines(run_dir / "attempts" / f"{step}.1" / output) == replies(step, partial)
    else:
        assert "set_aside" not in state[step] and not (run_dir / "attempts").exists()


def test_budget_stop_fails_the_step_and_a_higher_budget_resumes(tmp_path: Path) -> None:
    run_dir = tmp_path / "synth" / "r"
    # every upstream call costs CNY 1 (1M prompt tokens at CNY 1 per 1M in the harness pricing)
    costly = fake_upstream(prompt_tokens=1_000_000, completion_tokens=0)
    with ProxyThread(costly, "127.0.0.1", free_port()) as up:
        first = start_driver(run_dir, up, requests=2, budget=2.5)
        assert first.wait(timeout=60) == 3  # scenario: CNY 2; task: 1st call -> CNY 3, 2nd refused
        state = json.loads((run_dir / "state.json").read_text())["steps"]
        assert state["scenario"]["status"] == "done"
        # like AWM, the step turned the refusal into an empty reply and exited 0: only the ledger tells
        task = state["task"]
        assert (task["status"], task["returncode"], task["reason"]) == ("failed", 0, "budget")
        assert lines(run_dir / "gen_tasks.jsonl") == [*replies("task", 1), ""]
        rows = ledger_rows(run_dir)
        assert [(r["step"], r["cached"], r["refused"]) for r in rows] == [
            ("scenario", False, False),
            ("scenario", False, False),
            ("task", False, False),
            ("task", False, True),
        ]

        again = start_driver(run_dir, up, requests=2, budget=2.5)
        assert again.wait(timeout=60) == 3  # still over budget: refused before the step starts
        assert len(ledger_rows(run_dir)) == 4

        raised = start_driver(run_dir, up, requests=2, budget=100)
        assert raised.wait(timeout=120) == 0
    state = json.loads((run_dir / "state.json").read_text())["steps"]
    assert all(state[s]["status"] == "done" for s in STEPS)
    task = [r for r in ledger_rows(run_dir) if r["step"] == "task" and not r["refused"]]
    assert [r["cached"] for r in task] == [False, True, False]  # 1st request replayed from the cache
    assert lines(run_dir / "gen_tasks.jsonl") == replies("task", 2)
    assert state["task"]["set_aside"] == "attempts/task.1"  # the output with the empty reply, kept
    assert lines(run_dir / "attempts" / "task.1" / "gen_tasks.jsonl") == [*replies("task", 1), ""]
    summary = json.loads((run_dir / "ledger_summary.json").read_text())
    assert summary["steps"]["task"]["refused_calls"] == 1
    assert summary["total"]["cost"] == 2 * len(STEPS)  # each request paid for exactly once


def proxy(tmp_path: Path, calls: list[bytes], *, budget: float | None, prices: dict[str, Price]) -> Any:
    def upstream(req: httpx.Request) -> httpx.Response:
        calls.append(req.content)
        body = json.loads(req.content)
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 1_000_000}})

    ledger = Ledger(tmp_path / "ledger.jsonl")
    app = create_proxy_app(
        upstream_base_url="http://up",
        upstream_api_key=None,
        cache_dir=tmp_path / "cache",
        ledger=ledger,
        transport=httpx.MockTransport(upstream),
        prices=prices,
        budget=budget,
    )
    return app, ledger


def test_proxy_refuses_over_budget_but_serves_the_cache(tmp_path: Path) -> None:
    calls: list[bytes] = []
    app, ledger = proxy(tmp_path, calls, budget=1.0, prices={"m": Price(1.0, 1.0)})
    first = {"model": "m", "messages": [{"role": "user", "content": "one"}]}
    with TestClient(app) as c:
        assert c.post("/step/task/v1/chat/completions", json=first).status_code == 200  # CNY 1 spent
        refused = c.post("/step/task/v1/chat/completions", json={**first, "messages": []})
        cached = c.post("/step/task/v1/chat/completions", json=first)
    assert refused.status_code == 402 and refused.json()["error"]["type"] == "budget_exceeded"
    assert cached.status_code == 200 and len(calls) == 1  # the refused request never went upstream
    assert [(e.cached, e.refused) for e in ledger.entries()] == [(False, False), (False, True), (True, False)]
    assert ledger.spent({"m": Price(1.0, 1.0)}) == (1.0, [])


def test_proxy_fails_closed_without_a_price_and_can_be_disabled(tmp_path: Path) -> None:
    calls: list[bytes] = []
    app, _ = proxy(tmp_path / "a", calls, budget=5.0, prices={})
    with TestClient(app) as c:
        r = c.post("/step/task/v1/chat/completions", json={"model": "unpriced", "messages": []})
    assert r.status_code == 402 and "no price" in r.json()["error"]["message"] and not calls
    app, _ = proxy(tmp_path / "b", calls, budget=None, prices={})
    with TestClient(app) as c:
        r = c.post("/step/task/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 200
    assert len(calls) == 1


def test_ledger_spent_ignores_cached_and_refused_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    ledger.record(LedgerEntry("task", "m", 1_000_000, 500_000, cached=False))
    ledger.record(LedgerEntry("task", "m", 1_000_000, 500_000, cached=True))
    ledger.record(LedgerEntry("task", "m", 0, 0, cached=False, refused=True))
    ledger.record(LedgerEntry("db", "other", 10, 10, cached=False))
    assert ledger.spent({"m": Price(2.0, 8.0)}) == (6.0, ["other"])
    s = ledger.summary({"m": Price(2.0, 8.0)})["steps"]["task"]
    assert (s["calls"], s["cached_calls"], s["refused_calls"]) == (2, 1, 1)
