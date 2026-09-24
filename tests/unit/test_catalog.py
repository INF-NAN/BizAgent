from pathlib import Path

from workbench.envs.catalog import build_catalog, load_tasks, normalize_scenario_name, search

MINI = Path("tests/fixtures/awm_mini")


def test_catalog_counts_from_dataset_files() -> None:
    cat = build_catalog(MINI)
    assert [s.name for s in cat] == ["mini_e_commerce"]
    s = cat[0]
    assert s.tools == 7 and s.tasks == 2 and s.tables == 3
    assert "add_item_to_cart" in s.tool_names


def test_search_by_name_and_tool() -> None:
    cat = build_catalog(MINI)
    assert search(cat, "commerce") and search(cat, "payment") and not search(cat, "airline")


def test_normalize_matches_awm_rule() -> None:
    assert normalize_scenario_name("E-Commerce 33!") == "e_commerce_33"


def test_load_tasks_and_missing_dir(tmp_path: Path) -> None:
    assert len(load_tasks(MINI, "mini_e_commerce")) == 2
    assert build_catalog(tmp_path) == []
