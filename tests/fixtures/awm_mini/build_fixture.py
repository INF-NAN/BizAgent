"""Build the hand-written mini AWM dataset used by offline tests and the mock demo.

HAND-WRITTEN FIXTURE — NOT official AgentWorldModel-1K data. The tool interface of the served
code was reconciled with the official `e_commerce_33` environment (see PROVENANCE below).

Writes AWM-format JSONL files (field layout per docs/RECON.md §1.6) next to this script:
gen_scenario / gen_tasks / gen_db / gen_sample / gen_envs / gen_verifier.pure_code.
Run: `uv run python tests/fixtures/awm_mini/build_fixture.py`
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIO = "mini_e_commerce"

def _table(name: str, ddl: str) -> dict[str, object]:
    return {"name": name, "ddl": ddl, "indexes": [], "examples": []}


TABLES = [
    _table(
        "products",
        "CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, "
        "is_prime_eligible INTEGER NOT NULL);",
    ),
    _table(
        "product_aggregates",
        "CREATE TABLE product_aggregates (product_id INTEGER PRIMARY KEY REFERENCES products(id), "
        "average_rating REAL NOT NULL);",
    ),
    _table(
        "product_offers",
        "CREATE TABLE product_offers (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL "
        "REFERENCES products(id), price REAL NOT NULL, currency TEXT NOT NULL, is_active INTEGER NOT NULL);",
    ),
    _table(
        "carts",
        "CREATE TABLE carts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL);",
    ),
    _table(
        "cart_items",
        "CREATE TABLE cart_items (id INTEGER PRIMARY KEY, cart_id INTEGER NOT NULL REFERENCES carts(id), "
        "product_offer_id INTEGER NOT NULL REFERENCES product_offers(id), quantity INTEGER NOT NULL);",
    ),
    _table(
        "payment_methods",
        "CREATE TABLE payment_methods (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
        "payment_type TEXT NOT NULL, card_brand TEXT, card_last4 TEXT, is_default INTEGER NOT NULL);",
    ),
]


def _inserts(table: str, rows: list[str]) -> dict[str, object]:
    return {"table_name": table, "insert_statements": [f"INSERT INTO {table} VALUES {r};" for r in rows]}


# Hand-written sample rows. Offer ids (11-14) deliberately differ from product ids (1-4) so a
# model that passes a product id where a product_offer_id is required is visible in the DB diff.
SAMPLE = {
    "tables": [
        _inserts(
            "products",
            [
                "(1, 'Wireless Noise Cancelling Headphones A', 'Over-ear, 30h battery', 1)",
                "(2, 'Wireless Noise Cancelling Headphones B', 'Over-ear, premium', 1)",
                "(3, 'Wired Headphones C', 'Budget wired headphones', 0)",
                "(4, 'USB-C Charger', '65W wall charger', 1)",
            ],
        ),
        _inserts("product_aggregates", ["(1, 4.7)", "(2, 4.8)", "(3, 4.1)", "(4, 4.4)"]),
        _inserts(
            "product_offers",
            [
                "(11, 1, 189.0, 'USD', 1)",
                "(12, 2, 249.0, 'USD', 1)",
                "(13, 3, 39.0, 'USD', 1)",
                "(14, 4, 25.0, 'USD', 1)",
            ],
        ),
        _inserts("carts", ["(1, 1, 'active')"]),
        _inserts("cart_items", ["(1, 1, 14, 1)"]),
        _inserts(
            "payment_methods",
            ["(1, 1, 'credit_card', 'Visa', '4242', 1)", "(2, 1, 'credit_card', 'MasterCard', '5555', 0)"],
        ),
    ]
}

TASKS = [
    "Search for 'wireless noise cancelling headphones' and add the top-rated one under $200 to my cart.",
    "Delete my MasterCard payment method.",
]

VERIFIER_TASK0 = '''
def verify_task_completion(initial_db_path, final_db_path, final_answer=""):
    import sqlite3
    conn = sqlite3.connect(final_db_path)
    rows = conn.execute(
        "SELECT ci.product_offer_id, ci.quantity FROM cart_items ci JOIN carts c ON c.id = ci.cart_id "
        "WHERE c.user_id = 1 AND c.status = 'active'"
    ).fetchall()
    conn.close()
    return {"result": "complete" if (11, 1) in rows else "others"}
'''

PROVENANCE = {
    "origin": "hand-written-fixture",
    "official_data": False,
    "scenarios": [SCENARIO],
    "interface_reconciled_with": {
        "dataset": "Snowflake/AgentWorldModel-1K",
        "revision": "dde80a0283fe781bdc51656bce57063dc5650213",
        "scenario": "e_commerce_33",
        "license": "CC-BY-4.0",
        "attribution": "AgentWorldModel-1K by Zhaoyang Wang, Canwen Xu, Boyi Liu, Yite Wang, Siwei Han, "
        "Zhewei Yao, Huaxiu Yao, Yuxiong He; https://huggingface.co/datasets/Snowflake/AgentWorldModel-1K",
        "taken_from_official": "names of 7 tools; their parameter names and required-ness; top-level "
        "response field names; table names products, product_offers, carts, cart_items, payment_methods",
        "changes": "code written from scratch and simplified: fewer parameters, tables and columns; "
        "product_aggregates reduced to average_rating; sample rows, tasks and verifier are invented",
        "evidence": "docs/verification/2026-09-24-fixture-reconciliation.md",
    },
}


def build() -> None:
    code = (HERE / "mini_e_commerce_server.py").read_text(encoding="utf-8")
    records = {
        "gen_scenario.jsonl": [{"name": SCENARIO, "description": "Hand-written mini e-commerce fixture."}],
        "gen_tasks.jsonl": [{"scenario": SCENARIO, "tasks": TASKS}],
        "gen_db.jsonl": [
            {"scenario": SCENARIO, "db_schema": {"tables": TABLES}, "db_path": f"databases/{SCENARIO}.db"}
        ],
        "gen_sample.jsonl": [
            {
                "scenario": SCENARIO,
                "tables_count": len(SAMPLE["tables"]),
                "inserts_count": sum(len(t["insert_statements"]) for t in SAMPLE["tables"]),
                "sample_data": SAMPLE,
            }
        ],
        "gen_envs.jsonl": [{"scenario": SCENARIO, "db_path": f"databases/{SCENARIO}.db", "full_code": code}],
        "gen_verifier.pure_code.jsonl": [
            {
                "scenario": SCENARIO,
                "task_idx": 0,
                "task": TASKS[0],
                "verification": {"code": VERIFIER_TASK0, "raw_response": "hand-written"},
            }
        ],
    }
    for name, rows in records.items():
        with (HERE / name).open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (HERE / "MANIFEST.json").write_text(json.dumps(PROVENANCE, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
