"""Checks against the official AgentWorldModel-1K dataset (data/awm1k, fetched by `make data`).

Skipped automatically when the dataset is absent (CI never downloads it). Nothing here
modifies the official files: the env manager copies each scenario's database into a
per-session run directory (rule R2). Counts asserted below are dataset facts recorded in
docs/verification/2026-09-24-dataset.md, not model metrics.
"""

from __future__ import annotations

import collections
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.conftest import OFFICIAL_DATASET, OFFICIAL_FILES
from tests.integration.conftest import MINI, free_base
from workbench.config import EnvSettings
from workbench.envs.catalog import build_catalog
from workbench.envs.manager import EnvManager
from workbench.gateway.errors import normalize

pytestmark = [pytest.mark.integration, pytest.mark.official_data]

SCENARIO = "e_commerce_33"
FIXTURE_TOOLS = {
    "search_products",
    "get_product_by_id",
    "list_cart_items",
    "add_item_to_cart",
    "remove_cart_item",
    "list_user_payment_methods",
    "delete_user_payment_method",
}


def _rows(name: str) -> list[dict[str, Any]]:
    with (OFFICIAL_DATASET / name).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _manager(dataset_dir: Path, runs: Path) -> EnvManager:
    base = free_base()
    return EnvManager(
        EnvSettings(
            dataset_dir=dataset_dir,
            runs_dir=runs,
            port_min=base,
            port_max=base + 30,
            start_timeout_s=90,
        )
    )


@pytest.fixture
async def official(tmp_path: Path) -> AsyncIterator[EnvManager]:
    m = _manager(OFFICIAL_DATASET, tmp_path / "official-runs")
    yield m
    await m.stop_all()


async def _schemas(url: str) -> dict[str, dict[str, Any]]:
    async with streamablehttp_client(url) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        return {t.name: t.inputSchema for t in (await s.list_tools()).tools}


def test_dataset_files_and_entry_counts() -> None:
    for name in OFFICIAL_FILES:
        assert (OFFICIAL_DATASET / name).is_file(), name
    for name in ("gen_scenario.jsonl", "gen_tasks.jsonl", "gen_db.jsonl", "gen_sample.jsonl"):
        assert len(_rows(name)) == 1000, name
    for name in ("gen_spec.jsonl", "gen_envs.jsonl"):
        assert len(_rows(name)) == 1000, name
    tasks = _rows("gen_tasks.jsonl")
    assert sum(len(r["tasks"]) for r in tasks) == 10_000
    for name in ("gen_verifier.jsonl", "gen_verifier.pure_code.jsonl"):
        keys = collections.Counter((r["scenario"], r["task_idx"]) for r in _rows(name))
        assert len(keys) == 10_000, name  # every task has a verifier; duplicates exist (see docs)
    manifest = json.loads((OFFICIAL_DATASET / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["origin"] == "official" and manifest["license"] == "CC-BY-4.0"


def test_catalog_reads_official_scenarios() -> None:
    cat = {s.name: s for s in build_catalog(OFFICIAL_DATASET)}
    assert len(cat) == 1000
    s = cat[SCENARIO]
    assert (s.tools, s.tasks, s.tables) == (39, 10, 19)
    assert set(s.tool_names) >= FIXTURE_TOOLS


async def test_official_env_starts_and_fixture_matches_its_interface(
    official: EnvManager, tmp_path: Path
) -> None:
    h = await official.start(SCENARIO, session_id="off1")
    assert h.state == "healthy" and len(h.tools) == 39
    off = await _schemas(h.url)
    assert set(off) == set(h.tools)
    # the official data directory is never written to (artifacts go to the session run dir)
    assert not list(OFFICIAL_DATASET.glob("temp_server_*.py"))

    mini = _manager(MINI, tmp_path / "mini-runs")
    try:
        hm = await mini.start("mini_e_commerce", session_id="mini1")
        fx = await _schemas(hm.url)
    finally:
        await mini.stop_all()
    assert set(fx) == FIXTURE_TOOLS and set(off) >= FIXTURE_TOOLS
    for name in FIXTURE_TOOLS:
        fx_props = set((fx[name].get("properties") or {}).keys())
        off_props = set((off[name].get("properties") or {}).keys())
        assert fx_props <= off_props, (name, fx_props - off_props)
        assert sorted(fx[name].get("required", [])) == sorted(off[name].get("required", [])), name


async def test_official_empty_search_normalizes_to_empty(official: EnvManager) -> None:
    h = await official.start(SCENARIO, session_id="off2")
    async with streamablehttp_client(h.url) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("search_products", {"query": "no-such-product-zzz"})
        bad = await s.call_tool("get_product_by_id", {"product_id": "abc"})
    text = res.content[0].text
    assert not res.isError and json.loads(text) == {"products": [], "total": 0}
    assert normalize("search_products", res.isError, text).status == "empty"
    norm = normalize("get_product_by_id", bad.isError, bad.content[0].text)
    assert norm.status == "error" and norm.error is not None
    assert norm.error.details["expected_type"] == "'integer'"
    assert not official.diff("off2").is_changed


async def test_successful_write_with_empty_lists_is_not_empty(official: EnvManager) -> None:
    # official social_media_4: removing every hidden subreddit is a real write whose result has
    # only an empty list; it must be "ok", not "empty" (ADR-014)
    from workbench.gateway.policy import PolicyConfig, classify

    h = await official.start("social_media_4", session_id="off3")
    async with streamablehttp_client(h.url) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("patch_hidden_subreddits", {"remove_subreddit_ids": list(range(1, 2001))})
    text = res.content[0].text
    assert not res.isError and json.loads(text)["hide_subreddit_ids"] == []
    risk = classify("patch_hidden_subreddits", "", PolicyConfig.load(Path("configs/tool_policy.yaml"))).level
    assert risk != "read"
    assert normalize("patch_hidden_subreddits", False, text, read_only=risk == "read").status == "ok"
    assert official.diff("off3").tables["user_content_preferences"].changed == [1]
