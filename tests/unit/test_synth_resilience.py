"""Mid-step interruption and resume, end to end against a local fake upstream (D17, ADR-022).

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

import pytest

from tests.unit.synth_harness import (
    MODEL,
    expected_reply,
    fake_upstream,
    free_port,
    request_body,
    start_driver,
)
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
