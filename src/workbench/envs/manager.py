"""Env Manager: turns "manually run `awm env start`" into a supervised service.

Per session it (1) builds a private copy of the scenario DB under ``data/runs/<session_id>/``,
(2) leases a port, (3) launches AWM's server in its **own process group**
(``start_new_session=True``) so that stopping it also kills the ``sh | tee`` pipeline AWM
spawns internally (awm/core/server.py:163) — killing only the launcher leaves the real
server orphaned (observed in Phase 2, see docs/RECON.md §9), (4) polls an `awm env check`
equivalent health check with a deadline, and (5) supervises the process afterwards
(crash -> ``unhealthy``, idle -> reaped, main-process exit -> everything killed).
Concurrency is bounded by a semaphore; callers beyond the limit queue up to a timeout.

``preview`` runs one tool call on a shadow copy of a session's current DB, on a leased port
and in its own process group, and reports the changes; ``checkpoint``/``changes_since`` measure
what a real call changed (approval previews, ADR-029).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from workbench.config import EnvSettings
from workbench.envs import awm_adapter
from workbench.envs import changes as chg
from workbench.envs import snapshot as snap
from workbench.envs.catalog import load_route_methods
from workbench.envs.health import HealthResult, check_mcp
from workbench.envs.ports import PortPool
from workbench.subprocess_env import generated_code_env

EnvState = Literal["starting", "healthy", "unhealthy", "stopped"]
HealthFn = Callable[[str, float], Awaitable[HealthResult]]
DbBuilder = Callable[[Path, str, Path], Path]
CommandBuilder = Callable[..., list[str]]
MethodsLoader = Callable[[Path, str], dict[str, str]]
# (url, tool, arguments, timeout_s) -> (is_error, text), like gateway.upstream.McpUpstream.call_tool
ToolCaller = Callable[[str, str, dict[str, Any], float], Awaitable[tuple[bool, str]]]


class EnvError(RuntimeError):
    """Base class for env manager errors."""


class EnvStartError(EnvError):
    """The environment process crashed or never became healthy."""


class EnvQueueTimeoutError(EnvError):
    """No capacity freed up within the queue timeout."""


class EnvNotFoundError(EnvError, KeyError):
    """Unknown session id."""


class Launcher(Protocol):
    def launch(self, cmd: list[str], log_path: Path, env: dict[str, str]) -> subprocess.Popen[bytes]: ...


class SubprocessLauncher:
    def launch(self, cmd: list[str], log_path: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            # Own process group: stop() signals the whole group (launcher + sh + server + tee).
            return subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )


@dataclass
class EnvHandle:
    session_id: str
    scenario: str
    port: int
    url: str
    run_dir: Path
    work_db: Path
    initial_db: Path
    log_path: Path
    state: EnvState
    started_at: float
    last_used: float
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    error: str | None = None
    tools: list[str] = field(default_factory=list)
    # tool -> HTTP method of its route, from the scenario's offline code (risk floor, ADR-015)
    tool_methods: dict[str, str] = field(default_factory=dict)

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scenario": self.scenario,
            "state": self.state,
            "url": self.url,
            "port": self.port,
            "pid": self.pid,
            "run_dir": str(self.run_dir),
            "tools": len(self.tools),
            "idle_s": round(time.monotonic() - self.last_used, 1),
            "error": self.error,
        }


def _kill_group(proc: subprocess.Popen[bytes], grace_s: float) -> None:
    if proc.poll() is not None:
        # The launcher is gone, but its process group (sh | tee | server) may still be alive.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace_s)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace_s)


@dataclass
class _Shadow:
    """What a preview has to reclaim: its directory, and the port and process once started."""

    dir: Path
    port: int | None = None
    process: subprocess.Popen[bytes] | None = None


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 1)


def _default_tool_caller() -> ToolCaller:
    from workbench.gateway.upstream import McpUpstream  # the same MCP client the gateway uses

    return McpUpstream().call_tool


def tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()[-64_000:].decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])


class EnvManager:
    def __init__(
        self,
        settings: EnvSettings,
        *,
        launcher: Launcher | None = None,
        health_fn: HealthFn | None = None,
        db_builder: DbBuilder | None = None,
        command_builder: CommandBuilder | None = None,
        methods_loader: MethodsLoader | None = None,
        tool_caller: ToolCaller | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.ports = PortPool(settings.host, settings.port_min, settings.port_max)
        self._launcher = launcher or SubprocessLauncher()
        self._health = health_fn or check_mcp
        self._db_builder = db_builder or awm_adapter.build_session_db
        self._command = command_builder or awm_adapter.server_command
        self._methods = methods_loader or load_route_methods
        self._call_tool = tool_caller or _default_tool_caller()
        self._clock = clock
        self._handles: dict[str, EnvHandle] = {}
        self._sem: asyncio.Semaphore | None = None
        self._preview_sem: asyncio.Semaphore | None = None
        self._lock = threading.Lock()
        self.waiting = 0

    # ------------------------------------------------------------------ capacity
    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.settings.max_envs)
        return self._sem

    @property
    def active(self) -> list[EnvHandle]:
        with self._lock:
            return [h for h in self._handles.values() if h.state in ("starting", "healthy", "unhealthy")]

    # ------------------------------------------------------------------ lifecycle
    async def start(self, scenario: str, session_id: str | None = None) -> EnvHandle:
        sid = session_id or uuid.uuid4().hex[:12]
        sem = self._semaphore()
        self.waiting += 1
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.settings.queue_timeout_s)
        except TimeoutError as exc:
            raise EnvQueueTimeoutError(
                f"no env capacity within {self.settings.queue_timeout_s}s (max_envs={self.settings.max_envs})"
            ) from exc
        finally:
            self.waiting -= 1
        try:
            return await self._start_locked(scenario, sid)
        except BaseException:
            sem.release()
            raise

    async def _start_locked(self, scenario: str, sid: str) -> EnvHandle:
        s = self.settings
        run_dir = s.runs_dir / sid
        run_dir.mkdir(parents=True, exist_ok=True)
        built = await asyncio.to_thread(self._db_builder, s.dataset_dir, scenario, run_dir / "build")
        tool_methods = await asyncio.to_thread(self._methods, s.dataset_dir, scenario)
        initial_db = awm_adapter.copy_db(built, run_dir / "initial.db")
        work_db = awm_adapter.copy_db(built, run_dir / "work.db")
        port = self.ports.allocate()
        url = f"http://{s.public_host or s.host}:{port}/mcp"
        log_path = run_dir / "server.log"
        cmd = self._command(
            dataset_dir=s.dataset_dir,
            scenario=scenario,
            db_path=work_db,
            host=s.host,
            port=port,
            temp_server_path=run_dir / "temp_server.py",
            output_dir=run_dir / "awm_server",
        )
        # The server runs the scenario's generated code: allowlisted variables only (ADR-019).
        env = generated_code_env()
        now = self._clock()
        handle = EnvHandle(
            session_id=sid,
            scenario=scenario,
            port=port,
            url=url,
            run_dir=run_dir,
            work_db=work_db,
            initial_db=initial_db,
            log_path=log_path,
            state="starting",
            started_at=now,
            last_used=now,
            tool_methods=tool_methods,
        )
        try:
            handle.process = self._launcher.launch(cmd, log_path, env)
        except BaseException:
            self.ports.release(port)
            raise
        with self._lock:
            self._handles[sid] = handle
        self._write_meta(handle, cmd)
        try:
            await self._wait_healthy(handle)
        except BaseException:
            # The slot is released by start()'s except branch, not here.
            await self._terminate(handle, reason="failed to start", release_slot=False)
            raise
        return handle

    async def _wait_healthy(self, handle: EnvHandle) -> None:
        s = self.settings
        deadline = time.monotonic() + s.start_timeout_s
        last_error = "not checked"
        while time.monotonic() < deadline:
            proc = handle.process
            if proc is not None and proc.poll() is not None:
                handle.state = "unhealthy"
                handle.error = f"process exited with code {proc.returncode}"
                raise EnvStartError(f"{handle.error}\n{tail_file(handle.log_path, 30)}")
            budget = min(s.health_timeout_s, max(0.1, deadline - time.monotonic()))
            result = await self._health(handle.url, budget)
            if result.ok:
                handle.state = "healthy"
                handle.tools = list(result.tools)
                handle.error = None
                return
            last_error = result.error or "unhealthy"
            await asyncio.sleep(s.health_poll_s)
        handle.state = "unhealthy"
        handle.error = f"health check timed out after {s.start_timeout_s}s: {last_error}"
        raise EnvStartError(f"{handle.error}\n{tail_file(handle.log_path, 30)}")

    async def _terminate(self, handle: EnvHandle, reason: str, release_slot: bool = True) -> None:
        if handle.state == "stopped":
            return
        if handle.process is not None:
            await asyncio.to_thread(_kill_group, handle.process, self.settings.stop_grace_s)
        self.ports.release(handle.port)
        handle.state = "stopped"
        handle.error = handle.error or reason
        if release_slot and self._sem is not None:
            self._sem.release()

    async def stop(self, session_id: str) -> EnvHandle:
        handle = self.get(session_id)
        await self._terminate(handle, reason="stopped")
        return handle

    async def stop_all(self) -> None:
        for handle in list(self.active):
            await self._terminate(handle, reason="stop_all")

    def kill_all_now(self) -> None:
        """Synchronous, signal-safe cleanup used by SIGINT/SIGTERM handlers and atexit."""
        for handle in list(self.active):
            if handle.process is not None:
                _kill_group(handle.process, 1.0)
            self.ports.release(handle.port)
            handle.state = "stopped"

    # ------------------------------------------------------------------ supervision
    def get(self, session_id: str) -> EnvHandle:
        with self._lock:
            if session_id not in self._handles:
                raise EnvNotFoundError(session_id)
            return self._handles[session_id]

    def list_envs(self) -> list[EnvHandle]:
        self.refresh()
        with self._lock:
            return list(self._handles.values())

    def touch(self, session_id: str) -> None:
        self.get(session_id).last_used = self._clock()

    def refresh(self) -> list[str]:
        """Mark crashed processes as unhealthy; returns affected session ids."""
        crashed: list[str] = []
        for h in self.active:
            if h.state == "healthy" and h.process is not None and h.process.poll() is not None:
                h.state = "unhealthy"
                h.error = f"process exited with code {h.process.returncode}"
                crashed.append(h.session_id)
        return crashed

    async def reap_idle(self) -> list[str]:
        now = self._clock()
        idle = [h for h in self.active if now - h.last_used > self.settings.idle_timeout_s]
        for h in idle:
            await self._terminate(h, reason=f"idle for more than {self.settings.idle_timeout_s}s")
        return [h.session_id for h in idle]

    async def check(self, session_id: str) -> HealthResult:
        handle = self.get(session_id)
        if handle.state == "stopped":
            return HealthResult(ok=False, error="stopped")
        if handle.process is not None and handle.process.poll() is not None:
            self.refresh()
            return HealthResult(ok=False, error=handle.error)
        result = await self._health(handle.url, self.settings.health_timeout_s)
        handle.state = "healthy" if result.ok else "unhealthy"
        handle.error = None if result.ok else result.error
        return result

    async def run_supervisor(self) -> None:
        while True:
            self.refresh()
            await self.reap_idle()
            await asyncio.sleep(self.settings.reap_interval_s)

    # ------------------------------------------------------------------ data
    def logs(self, session_id: str, lines: int = 200) -> str:
        return tail_file(self.get(session_id).log_path, lines)

    def snapshot(self, session_id: str, name: str) -> Path:
        h = self.get(session_id)
        return snap.snapshot(h.work_db, h.run_dir / "snapshots" / f"{_safe(name)}.db")

    def restore(self, session_id: str, name: str) -> None:
        h = self.get(session_id)
        src = h.initial_db if name == "initial" else h.run_dir / "snapshots" / f"{_safe(name)}.db"
        snap.restore(src, h.work_db)

    def diff(self, session_id: str, against: str = "initial") -> snap.DbDiff:
        h = self.get(session_id)
        base = h.initial_db if against == "initial" else h.run_dir / "snapshots" / f"{_safe(against)}.db"
        return snap.diff(base, h.work_db)

    # ------------------------------------------------------------------ approval previews
    async def preview(
        self,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        timeout_s: float,
        max_rows: int = 20,
    ) -> dict[str, Any]:
        """Run ``tool`` once on a shadow copy of the session's current DB and report what changed.

        The copy is taken with SQLite's online-backup API, so the session's own server keeps
        running and its DB is only read. The shadow server runs like a session env (own process
        group, leased port, allowlisted environment) and writes only under
        ``<run_dir>/previews/<id>/``, which is removed with the process group afterwards, on
        success, error or timeout alike. The result is JSON-safe; ``status`` is ``ok`` or
        ``failed`` (then ``error`` and the ``stage`` it failed in are set).
        """
        handle = self.get(session_id)
        pid = uuid.uuid4().hex[:12]
        shadow = _Shadow(handle.run_dir / "previews" / pid)
        timings: dict[str, float] = {}
        out: dict[str, Any] = {
            "id": pid,
            "session_id": session_id,
            "tool": tool,
            "status": "failed",
            "error": None,
            "stage": None,
            "call": None,
            "changes": None,
            "digest": None,
            "timings_ms": timings,
        }
        stage = "queue"
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s), self._preview_semaphore():
                stage, t = "prepare", time.perf_counter()
                before, db = await asyncio.to_thread(self._prepare_shadow, handle.work_db, shadow.dir)
                timings["prepare"] = _ms(t)
                stage, t = "start", time.perf_counter()
                url = await self._start_shadow(handle.scenario, shadow, db)
                timings["start"] = _ms(t)
                stage, t = "call", time.perf_counter()
                is_error, text = await self._call_tool(url, tool, arguments, timeout_s)
                timings["call"] = _ms(t)
                out["call"] = {"is_error": is_error, "text": text[:2000]}
                stage, t = "diff", time.perf_counter()
                result = await asyncio.to_thread(chg.changes, before, db, max_rows)
                timings["diff"] = _ms(t)
                out.update(status="ok", changes=result, digest=chg.digest(result))
        except TimeoutError:
            out["error"] = f"preview timed out after {timeout_s:g}s (stage: {stage})"
        except Exception as exc:  # reported on the approval card; nothing escapes a preview
            out["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        finally:
            t = time.perf_counter()
            await asyncio.shield(asyncio.to_thread(self._reclaim_shadow, shadow))
            timings["reclaim"] = _ms(t)
            timings["total"] = _ms(started)
        if out["status"] != "ok":
            out["stage"] = stage
        return out

    def _preview_semaphore(self) -> asyncio.Semaphore:
        if self._preview_sem is None:
            self._preview_sem = asyncio.Semaphore(self.settings.max_previews)
        return self._preview_sem

    @staticmethod
    def _prepare_shadow(work_db: Path, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True)
        before = snap.snapshot(work_db, directory / "before.db")  # online backup: read only
        db = directory / "shadow.db"
        shutil.copy2(before, db)
        return before, db

    async def _start_shadow(self, scenario: str, shadow: _Shadow, db: Path) -> str:
        s = self.settings
        shadow.port = self.ports.allocate()
        cmd = self._command(
            dataset_dir=s.dataset_dir,
            scenario=scenario,
            db_path=db,
            host=s.host,
            port=shadow.port,
            temp_server_path=shadow.dir / "temp_server.py",
            output_dir=shadow.dir / "awm_server",
        )
        log = shadow.dir / "launcher.log"
        # generated code runs here as well: allowlisted variables only (ADR-019)
        shadow.process = self._launcher.launch(cmd, log, generated_code_env())
        host = "127.0.0.1" if s.host in ("0.0.0.0", "") else s.host  # this process calls it
        url = f"http://{host}:{shadow.port}/mcp"
        while True:  # bounded by the preview's timeout
            if shadow.process.poll() is not None:
                code = shadow.process.returncode
                raise EnvStartError(f"shadow server exited with code {code}\n{tail_file(log, 20)}")
            result = await self._health(url, s.health_timeout_s)
            if result.ok:
                return url
            await asyncio.sleep(s.health_poll_s)

    def _reclaim_shadow(self, shadow: _Shadow) -> None:
        if shadow.process is not None:
            _kill_group(shadow.process, self.settings.stop_grace_s)
        if shadow.port is not None:
            self.ports.release(shadow.port)
        shutil.rmtree(shadow.dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            shadow.dir.parent.rmdir()  # "previews/", once no other preview is running

    def checkpoint(self, session_id: str) -> str:
        """Snapshot the session's DB before a real call; ``changes_since`` consumes it."""
        h = self.get(session_id)
        cid = uuid.uuid4().hex[:12]
        snap.snapshot(h.work_db, h.run_dir / "checkpoints" / f"{cid}.db")
        return cid

    def changes_since(self, session_id: str, checkpoint: str, max_rows: int = 20) -> dict[str, Any]:
        """Row-level changes from the checkpoint to now; the checkpoint file is deleted."""
        h = self.get(session_id)
        path = h.run_dir / "checkpoints" / f"{_safe(checkpoint)}.db"
        if not path.exists():  # sqlite3.connect would create an empty database
            raise FileNotFoundError(path)
        try:
            return chg.changes(path, h.work_db, max_rows)
        finally:
            path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                path.parent.rmdir()

    def _write_meta(self, handle: EnvHandle, cmd: list[str]) -> None:
        meta = {"session_id": handle.session_id, "scenario": handle.scenario, "port": handle.port, "cmd": cmd}
        (handle.run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _safe(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in "-_")
    if not cleaned:
        raise ValueError(f"invalid snapshot name {name!r}")
    return cleaned


def install_cleanup(manager: EnvManager) -> None:
    """Kill every child process group when the main process exits or gets SIGINT/SIGTERM."""
    atexit.register(manager.kill_all_now)
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: Any, _prev: Any = previous) -> None:
            manager.kill_all_now()
            if callable(_prev):
                _prev(signum, frame)
            else:
                raise SystemExit(128 + signum)

        signal.signal(sig, handler)
