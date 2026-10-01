"""EnvManager lifecycle with fake processes (no AWM, no network)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.unit.secrets_harness import PROBE, leaked, plant, unexpected
from workbench.config import EnvSettings
from workbench.envs.health import HealthResult
from workbench.envs.manager import (
    EnvManager,
    EnvNotFoundError,
    EnvQueueTimeoutError,
    EnvStartError,
)
from workbench.envs.procs import live_group_members


def _free_base() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def fake_db(dataset_dir: Path, scenario: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    db = dest / f"{scenario}.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO t VALUES (1)")
    return db


def cmd_for(script: str) -> Any:
    def build(**_: Any) -> list[str]:
        return [sys.executable, "-c", script]

    return build


async def ok_health(url: str, timeout: float) -> HealthResult:
    return HealthResult(ok=True, tools=["t1"])


async def never_healthy(url: str, timeout: float) -> HealthResult:
    await asyncio.sleep(0.01)
    return HealthResult(ok=False, error="connection refused")


def make(tmp_path: Path, script: str, health: Any = ok_health, **overrides: Any) -> EnvManager:
    base = _free_base()
    settings = EnvSettings(
        runs_dir=tmp_path / "runs",
        dataset_dir=tmp_path,
        port_min=base,
        port_max=base + 20,
        start_timeout_s=overrides.pop("start_timeout_s", 3.0),
        health_poll_s=0.05,
        stop_grace_s=1.0,
        **overrides,
    )
    return EnvManager(settings, health_fn=health, db_builder=fake_db, command_builder=cmd_for(script))


SLEEPER = "import time; time.sleep(60)"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_start_stop_isolated_run_dir(tmp_path: Path) -> None:
    m = make(tmp_path, SLEEPER)
    h = await m.start("scn", session_id="s1")
    assert h.state == "healthy" and h.tools == ["t1"]
    assert (tmp_path / "runs/s1/initial.db").exists() and (tmp_path / "runs/s1/work.db").exists()
    assert h.port in m.ports.leased
    pid = h.pid
    assert pid is not None and alive(pid)
    await m.stop("s1")
    assert h.state == "stopped" and h.port not in m.ports.leased
    await asyncio.sleep(0.1)
    assert not alive(pid)


async def test_health_timeout_kills_process(tmp_path: Path) -> None:
    m = make(tmp_path, SLEEPER, health=never_healthy, start_timeout_s=0.5)
    with pytest.raises(EnvStartError, match="timed out"):
        await m.start("scn", session_id="s2")
    h = m.get("s2")
    assert h.state == "stopped" and h.pid is not None
    await asyncio.sleep(0.1)
    assert not alive(h.pid)
    assert not m.ports.leased
    # capacity is returned: a new start works
    m2 = make(tmp_path, SLEEPER)
    await m2.start("scn")
    await m2.stop_all()


async def test_crash_during_start(tmp_path: Path) -> None:
    m = make(tmp_path, "import sys; print('boom'); sys.exit(3)", health=never_healthy)
    with pytest.raises(EnvStartError, match="exited with code 3"):
        await m.start("scn", session_id="s3")


async def test_crash_after_healthy_marks_unhealthy(tmp_path: Path) -> None:
    m = make(tmp_path, "import time; time.sleep(0.3)")
    h = await m.start("scn", session_id="s4")
    await asyncio.sleep(0.8)
    assert m.refresh() == ["s4"]
    assert h.state == "unhealthy" and "exited" in (h.error or "")
    assert (await m.check("s4")).ok is False
    await m.stop("s4")


async def test_queue_timeout_when_at_capacity(tmp_path: Path) -> None:
    m = make(tmp_path, SLEEPER, max_envs=1, queue_timeout_s=0.3)
    await m.start("scn", session_id="a")
    with pytest.raises(EnvQueueTimeoutError):
        await m.start("scn", session_id="b")
    await m.stop("a")
    h = await m.start("scn", session_id="c")
    assert h.state == "healthy"
    await m.stop_all()


async def test_queued_start_proceeds_when_slot_frees(tmp_path: Path) -> None:
    m = make(tmp_path, SLEEPER, max_envs=1, queue_timeout_s=5)
    await m.start("scn", session_id="a")
    waiter = asyncio.create_task(m.start("scn", session_id="b"))
    await asyncio.sleep(0.2)
    assert m.waiting == 1 and not waiter.done()
    await m.stop("a")
    h = await waiter
    assert h.session_id == "b" and h.state == "healthy"
    await m.stop_all()


async def test_idle_reaper(tmp_path: Path) -> None:
    now = [1000.0]
    m = make(tmp_path, SLEEPER, idle_timeout_s=10)
    m._clock = lambda: now[0]
    await m.start("scn", session_id="idle")
    await m.start("scn", session_id="busy")
    now[0] += 11
    m.touch("busy")
    assert await m.reap_idle() == ["idle"]
    assert m.get("idle").state == "stopped" and m.get("busy").state == "healthy"
    await m.stop_all()


async def test_kill_all_now_kills_process_groups(tmp_path: Path) -> None:
    # The fake server spawns a grandchild, like AWM's `sh -c "python ... | tee"`.
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    )
    m = make(tmp_path, script)
    h = await m.start("scn", session_id="k")
    assert h.pid is not None
    pgid = os.getpgid(h.pid)
    m.kill_all_now()
    await asyncio.sleep(0.2)
    assert live_group_members(pgid) == []


async def test_snapshot_restore_diff_and_logs(tmp_path: Path) -> None:
    m = make(tmp_path, "print('hello from server', flush=True); import time; time.sleep(60)")
    h = await m.start("scn", session_id="d")
    m.snapshot("d", "before")
    with sqlite3.connect(h.work_db) as c:
        c.execute("INSERT INTO t VALUES (2)")
    assert m.diff("d").tables["t"].added == [2]
    assert not m.diff("d", against="before").tables["t"].removed
    m.restore("d", "initial")
    assert not m.diff("d").is_changed
    await asyncio.sleep(0.2)
    assert "hello from server" in m.logs("d")
    # path traversal is neutralised: only [A-Za-z0-9_-] survive, the file stays in snapshots/
    assert m.snapshot("d", "../../etc") == h.run_dir / "snapshots" / "etc.db"
    with pytest.raises(ValueError):
        m.snapshot("d", "../..")
    await m.stop_all()


def test_unknown_session(tmp_path: Path) -> None:
    m = make(tmp_path, SLEEPER)
    with pytest.raises(EnvNotFoundError):
        m.get("nope")


def test_signal_handler_installed(tmp_path: Path) -> None:
    from workbench.envs.manager import install_cleanup

    m = make(tmp_path, SLEEPER)
    before = signal.getsignal(signal.SIGTERM)
    install_cleanup(m)
    try:
        assert signal.getsignal(signal.SIGTERM) is not before
    finally:
        signal.signal(signal.SIGTERM, before)


async def test_env_server_process_gets_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(monkeypatch)
    out = tmp_path / "view.json"
    script = (  # the "server": records what it sees (atomically), then stays up
        "import contextlib, io, os, time\n"
        "buf = io.StringIO()\n"
        f"with contextlib.redirect_stdout(buf): exec({PROBE!r})\n"
        f"with open({str(out)!r} + '.tmp', 'w') as f: f.write(buf.getvalue())\n"
        f"os.replace({str(out)!r} + '.tmp', {str(out)!r})\n"
        "time.sleep(60)\n"
    )
    m = make(tmp_path, script)
    await m.start("scn", session_id="s-env")
    for _ in range(200):
        if out.exists():
            break
        await asyncio.sleep(0.05)
    await m.stop_all()
    view = json.loads(out.read_text())
    assert leaked(view["names"]) == [] and not view["planted"]
    assert unexpected(view["names"]) == []
