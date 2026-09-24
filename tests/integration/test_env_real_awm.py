"""Starts ONE real AWM scenario server via AWM's own launcher and calls list_tools."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from workbench.envs.manager import EnvManager
from workbench.envs.ports import is_bindable
from workbench.envs.procs import live_group_members

pytestmark = pytest.mark.integration


async def test_real_awm_env_list_tools_and_clean_stop(manager: EnvManager) -> None:
    h = await manager.start("mini_e_commerce", session_id="int1")
    assert h.state == "healthy"
    assert "add_item_to_cart" in h.tools and len(h.tools) == 7

    async with streamablehttp_client(h.url) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        listed = await s.list_tools()
        assert {t.name for t in listed.tools} == set(h.tools)
        res = await s.call_tool("search_products", {"query": "Headphones", "sort_by": "price_asc"})
        assert not res.isError
        body = json.loads(res.content[0].text)
        assert body["total"] == 3 and body["products"][0]["product"]["title"] == "Wired Headphones C"

    # AWM artifacts land in the session dir, never next to the dataset (RECON §1.2)
    assert (h.run_dir / "temp_server.py").exists()
    assert not list(manager.settings.dataset_dir.glob("temp_server_*.py"))

    assert h.pid is not None
    pgid = os.getpgid(h.pid)
    await manager.stop("int1")
    await asyncio.sleep(0.3)
    # No orphaned `sh | tee | server` left alive. (Killed members may linger as zombies when
    # PID 1 does not reap them, as in this sandbox; docker compose uses `init: true`.)
    assert live_group_members(pgid) == []
    assert is_bindable("127.0.0.1", h.port)


async def test_write_shows_up_in_db_diff(manager: EnvManager) -> None:
    h = await manager.start("mini_e_commerce", session_id="int2")
    async with streamablehttp_client(h.url) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("add_item_to_cart", {"product_offer_id": 11, "quantity": 1})
        assert not res.isError
    d = manager.diff("int2")
    assert d.tables["cart_items"].added == [2]
    manager.restore("int2", "initial")
    assert not manager.diff("int2").is_changed
