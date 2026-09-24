from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tests.unit.test_env_manager import SLEEPER, make
from workbench.cli import app as cli_app
from workbench.envs.http import create_env_app
from workbench.envs.manager import EnvNotFoundError
from workbench.envs.service import LocalEnvService, RemoteEnvService


async def test_remote_client_against_http_control_plane(tmp_path: Path) -> None:
    mgr = make(tmp_path, SLEEPER, max_envs=1, queue_timeout_s=0.2)
    app = create_env_app(mgr.settings, manager=mgr)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://env")
    remote = RemoteEnvService("http://env", client=client)
    info = await remote.start("scn", "r1")
    assert info.state == "healthy" and info.tools == ["t1"]
    assert [e.session_id for e in await remote.list_envs()] == ["r1"]
    # capacity exhausted -> 503 from the control plane
    resp = await client.post("/envs", json={"scenario": "scn", "session_id": "r2"})
    assert resp.status_code == 503
    await remote.snapshot("r1", "s")
    await remote.restore("r1", "s")
    assert (await remote.diff("r1"))["changed"] is False
    await remote.touch("r1")
    assert isinstance(await remote.logs("r1"), str)
    await remote.stop("r1")
    assert (await remote.info("r1")).state == "stopped"
    with pytest.raises(EnvNotFoundError):
        await remote.info("missing")
    await remote.close()
    await mgr.stop_all()


async def test_local_service_wraps_manager(tmp_path: Path) -> None:
    svc = LocalEnvService(make(tmp_path, SLEEPER))
    info = await svc.start("scn", "l1")
    assert (await svc.info("l1")).url == info.url
    await svc.close()
    assert (await svc.info("l1")).state == "stopped"


def test_cli_env_search_on_mini_dataset() -> None:
    r = CliRunner().invoke(cli_app, ["env", "search", "cart", "--dataset-dir", "tests/fixtures/awm_mini"])
    assert r.exit_code == 0, r.output
    assert "mini_e_commerce" in r.output
    miss = CliRunner().invoke(cli_app, ["env", "search", "zzz", "--dataset-dir", "tests/fixtures/awm_mini"])
    assert miss.exit_code == 1
