import json
from pathlib import Path
from typing import Any

from workbench.envs.catalog import (
    build_catalog,
    iter_route_methods,
    load_route_methods,
    load_tasks,
    normalize_scenario_name,
    route_methods,
    search,
)

MINI = Path("tests/fixtures/awm_mini")


def test_catalog_counts_from_dataset_files() -> None:
    cat = build_catalog(MINI)
    assert [s.name for s in cat] == ["mini_e_commerce"]
    s = cat[0]
    assert s.tools == 7 and s.tasks == 2 and s.tables == 6
    assert "add_item_to_cart" in s.tool_names


def test_search_by_name_and_tool() -> None:
    cat = build_catalog(MINI)
    assert search(cat, "commerce") and search(cat, "payment") and not search(cat, "airline")


def test_normalize_matches_awm_rule() -> None:
    assert normalize_scenario_name("E-Commerce 33!") == "e_commerce_33"


def test_load_tasks_and_missing_dir(tmp_path: Path) -> None:
    assert len(load_tasks(MINI, "mini_e_commerce")) == 2
    assert build_catalog(tmp_path) == []


ROUTES = """
from fastapi import FastAPI
app = FastAPI()

@app.get("/items", operation_id="list_items")
def list_items(): ...

@app.delete(
    "/lists/{list_id}",
    operation_id="purge_my_list",
)
async def purge(list_id: int): ...

@app.post("/items/{item_id:int}")
async def add_item(item_id: int): ...

@router.patch("/x", name="custom")
def patch_x(): ...

@app.get("/dup", operation_id="dup")
def dup_get(): ...

@app.delete("/dup", operation_id="dup")
def dup_delete(): ...

@some_decorator
def not_a_route(): ...
"""


def test_route_methods_from_code() -> None:
    assert route_methods(ROUTES) == {
        "list_items": "GET",
        "purge_my_list": "DELETE",
        "add_item_items__item_id__post": "POST",  # FastAPI's default operationId
        "custom_x_patch": "PATCH",
        "dup": "DELETE",  # the riskiest method wins
    }


def test_default_operation_id_matches_fastapi() -> None:
    code = (
        "from fastapi import FastAPI\napp = FastAPI()\n"
        '@app.post("/items/{item_id:int}")\nasync def add_item(item_id: int): ...\n'
        '@app.get("/x/{a}-{b}", name="custom")\ndef f(a: str, b: str): ...\n'
    )
    ns: dict[str, Any] = {}
    exec(code, ns)  # our own snippet, to compare with FastAPI's real OpenAPI output
    spec = ns["app"].openapi()
    real = {op["operationId"]: m.upper() for path in spec["paths"].values() for m, op in path.items()}
    assert route_methods(code) == real


def test_route_methods_unparseable_code_is_empty() -> None:
    assert route_methods("def broken(:\n") == {}


def test_load_route_methods_mini_and_missing(tmp_path: Path) -> None:
    m = load_route_methods(MINI, "mini_e_commerce")
    assert (m["search_products"], m["add_item_to_cart"], m["remove_cart_item"]) == ("GET", "POST", "DELETE")
    assert len(m) == 7
    assert load_route_methods(MINI, "no_such_scenario") == {}
    assert load_route_methods(tmp_path, "anything") == {}


def test_load_route_methods_last_record_wins_like_awm(tmp_path: Path) -> None:
    records = [
        {
            "scenario": "Dup One",
            "db_path": "x",
            "full_code": "@app.get('/a', operation_id='t')\ndef a(): ...",
        },
        {"db_path": "x", "scenario": "other", "full_code": "@app.put('/b', operation_id='u')\ndef b(): ..."},
        {
            "scenario": "dup_one",
            "db_path": "x",
            "full_code": "@app.delete('/a', operation_id='t')\ndef a(): ...",
        },
    ]
    (tmp_path / "gen_envs.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    assert load_route_methods(tmp_path, "dup_one") == {"t": "DELETE"}
    assert load_route_methods(tmp_path, "other") == {"u": "PUT"}  # key order without the fast path
    assert dict(iter_route_methods(tmp_path)) == {"dup_one": {"t": "DELETE"}, "other": {"u": "PUT"}}
