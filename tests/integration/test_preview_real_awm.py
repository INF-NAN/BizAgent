"""Approval previews on a real AWM server (ADR-029, owner decision D29).

A shadow environment is started by the env-manager from a copy of the session's current DB, the
same call runs there, its changes are diffed, and the shadow is reclaimed. The approved call then
runs on the session's own server and is compared with the preview. Mock LLM not needed: the
gateway is driven directly, on CPU, with the hand-written mini dataset.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import MINI, free_base
from workbench.config import ApprovalSettings, EnvSettings, GatewaySettings
from workbench.envs.manager import EnvHandle, EnvManager, SubprocessLauncher
from workbench.envs.ports import is_bindable
from workbench.envs.procs import live_group_members
from workbench.envs.service import LocalEnvService
from workbench.envs.snapshot import fingerprint
from workbench.gateway.core import Gateway
from workbench.gateway.policy import PREVIEW_UNAVAILABLE, ApprovalError, ApprovalService

pytestmark = pytest.mark.integration

ADD = "mini_e_commerce__add_item_to_cart"
REMOVE = "mini_e_commerce__remove_cart_item"
DELETE_PM = "mini_e_commerce__delete_user_payment_method"


class RecordingLauncher(SubprocessLauncher):
    """The real launcher, remembering every process it started (session and shadow servers)."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[bytes]] = []

    def launch(self, cmd: list[str], log_path: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
        proc = super().launch(cmd, log_path, env)
        self.procs.append(proc)
        return proc


@dataclass
class Rig:
    manager: EnvManager
    launcher: RecordingLauncher
    gateway: Gateway
    env: EnvHandle

    @property
    def shadows(self) -> list[subprocess.Popen[bytes]]:
        return [p for p in self.launcher.procs if p.pid != self.env.pid]


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    base = free_base()
    launcher = RecordingLauncher()
    settings = EnvSettings(
        dataset_dir=MINI, runs_dir=tmp_path / "runs", port_min=base, port_max=base + 30, start_timeout_s=60
    )
    manager = EnvManager(settings, launcher=launcher)
    gateway = Gateway(
        GatewaySettings(audit_path=tmp_path / "audit.jsonl"),
        approvals=ApprovalService(secret=b"k"),
        approval=ApprovalSettings(preview_timeout_s=60),
        previews=LocalEnvService(manager),
    )
    env = await manager.start("mini_e_commerce", session_id="pv1")
    await gateway.register_session("pv1", env.scenario, env.url)
    try:
        yield Rig(manager, launcher, gateway, env)
    finally:
        await manager.stop_all()


async def approve_and_run(rig: Rig, tool: str, args: dict[str, Any], record: dict[str, Any]) -> Any:
    token = rig.gateway.issue_approval("pv1", tool, args, "alice", preview=record)
    return await rig.gateway.call_tool("pv1", tool, args, approval_token=token)


async def test_add_to_cart_preview_matches_the_real_call(rig: Rig) -> None:
    args = {"product_offer_id": 11, "quantity": 1}
    before = fingerprint(rig.env.work_db)
    record = await rig.gateway.preview("pv1", ADD, args)
    assert record["status"] == "ok" and record["summary"] == "cart_items +1", record
    [added] = record["changes"]["tables"]["cart_items"]["added"]
    assert added == {"key": 2, "row": {"id": 2, "cart_id": 1, "product_offer_id": 11, "quantity": 1}}
    assert set(record["timings_ms"]) == {"prepare", "start", "call", "diff", "reclaim", "total"}
    assert fingerprint(rig.env.work_db) == before  # the shadow never touched the session's DB

    out = await approve_and_run(rig, ADD, args, record)
    assert out.status == "ok" and out.preview == {"binding": record["digest"], "id": record["id"]}
    assert out.preview_check is not None and out.preview_check["result"] == "match", out.preview_check
    assert out.preview_check["actual"] == "cart_items +1"
    assert rig.manager.diff("pv1").tables["cart_items"].added == [2]  # the real call made the change
    decisions = [(r["decision"], r["status"]) for r in rig.gateway.audit.read("pv1")]
    assert decisions == [("preview", "ok"), ("allowed", "ok")]


async def test_destructive_preview_shows_the_rows_it_would_remove(rig: Rig) -> None:
    record = await rig.gateway.preview("pv1", REMOVE, {"cart_item_id": 1})
    assert record["risk"] == "destructive" and record["required"] and record["status"] == "ok", record
    [removed] = record["changes"]["tables"]["cart_items"]["removed"]
    assert removed == {"key": 1, "row": {"id": 1, "cart_id": 1, "product_offer_id": 14, "quantity": 1}}
    assert rig.manager.diff("pv1").tables["cart_items"].removed == []  # nothing removed yet

    out = await approve_and_run(rig, REMOVE, {"cart_item_id": 1}, record)
    assert out.status == "ok" and out.preview_check is not None and out.preview_check["result"] == "match"
    assert rig.manager.diff("pv1").tables["cart_items"].removed == [1]


async def test_preview_starts_from_the_sessions_current_db(rig: Rig) -> None:
    args = {"product_offer_id": 11, "quantity": 1}
    first = await rig.gateway.preview("pv1", ADD, args)
    await approve_and_run(rig, ADD, args, first)  # the session DB now has cart item 2
    second = await rig.gateway.preview("pv1", ADD, {"product_offer_id": 13, "quantity": 2})
    [added] = second["changes"]["tables"]["cart_items"]["added"]
    assert added["key"] == 3 and second["changes"]["tables"]["cart_items"]["rows_before"] == 2


async def test_preview_failures_are_reported_and_approval_follows_the_policy(rig: Rig) -> None:
    rig.gateway.approval_settings = ApprovalSettings(preview_timeout_s=0.3)
    timed_out = await rig.gateway.preview("pv1", DELETE_PM, {"payment_method_id": 2})
    assert timed_out["status"] == "failed" and timed_out["stage"] == "start"
    assert timed_out["error"] == "preview timed out after 0.3s (stage: start)"
    assert timed_out["required"] and not timed_out["approvable"]
    with pytest.raises(ApprovalError, match="needs a successful preview"):
        rig.gateway.issue_approval("pv1", DELETE_PM, {"payment_method_id": 2}, "alice", preview=timed_out)

    # a shadow server that exits at once (write: approvable without a preview, bound to preview_unavailable)
    rig.gateway.approval_settings = ApprovalSettings(preview_timeout_s=60)
    real_command = rig.manager._command
    rig.manager._command = lambda **kw: [sys.executable, "-c", "raise SystemExit(3)"]
    try:
        crashed = await rig.gateway.preview("pv1", ADD, {"product_offer_id": 11, "quantity": 1})
    finally:
        rig.manager._command = real_command
    assert crashed["status"] == "failed" and crashed["stage"] == "start"
    assert crashed["error"].startswith("EnvStartError: shadow server exited with code 3")
    assert crashed["approvable"] and not crashed["required"]
    out = await approve_and_run(rig, ADD, {"product_offer_id": 11, "quantity": 1}, crashed)
    assert out.status == "ok" and out.preview == {"binding": PREVIEW_UNAVAILABLE, "id": None}
    assert out.preview_check is not None and out.preview_check["result"] == "preview_unavailable"
    assert out.preview_check["actual"] == "cart_items +1"  # measured even without a preview
    audit = rig.gateway.audit.read("pv1")
    assert [(r["decision"], r["status"]) for r in audit] == [
        ("preview", "failed"),
        ("preview", "failed"),
        ("allowed", "ok"),
    ]
    assert audit[-1]["preview_check"]["result"] == "preview_unavailable"


async def test_reclaim_leaves_no_processes_ports_or_files(rig: Rig) -> None:
    await rig.gateway.preview("pv1", ADD, {"product_offer_id": 11, "quantity": 1})
    rig.gateway.approval_settings = ApprovalSettings(preview_timeout_s=0.3)
    await rig.gateway.preview("pv1", REMOVE, {"cart_item_id": 1})  # killed while starting
    assert len(rig.shadows) == 2
    for proc in rig.shadows:
        assert proc.poll() is not None
        # the shadow's whole process group (launcher, sh, server, tee) is gone
        assert live_group_members(proc.pid) == [], proc.args
        port = int(proc.args[proc.args.index("--port") + 1])  # type: ignore[index, union-attr, arg-type]
        assert is_bindable("127.0.0.1", port)
    assert rig.manager.ports.leased == {rig.env.port}  # only the session's own port is leased
    run_dir = rig.env.run_dir
    assert not (run_dir / "previews").exists() and not (run_dir / "checkpoints").exists()
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "awm_server",
        "build",
        "initial.db",
        "meta.json",
        "server.log",
        "temp_server.py",
        "work.db",
    ]
    assert not list(MINI.glob("temp_server_*.py"))  # nothing next to the dataset either
