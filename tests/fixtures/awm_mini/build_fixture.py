"""Build the hand-written mini AWM dataset used by offline tests and the mock demo.

HAND-WRITTEN FIXTURE — NOT official AgentWorldModel-1K data.

Writes AWM-format JSONL files (field layout per docs/RECON.md §1.6) next to this script:
gen_scenario / gen_tasks / gen_db / gen_sample / gen_envs / gen_verifier.pure_code.
Run: `uv run python tests/fixtures/awm_mini/build_fixture.py`
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIO = "mini_e_commerce"

TABLES = [
    {
        "name": "products",
        "ddl": "CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
        "price REAL NOT NULL, rating REAL NOT NULL);",
        "indexes": ["CREATE INDEX idx_products_title ON products(title);"],
        "examples": [],
    },
    {
        "name": "cart_items",
        "ddl": "CREATE TABLE cart_items (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
        "product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL);",
        "indexes": [],
        "examples": [],
    },
    {
        "name": "payment_methods",
        "ddl": "CREATE TABLE payment_methods (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
        "brand TEXT NOT NULL, last4 TEXT NOT NULL);",
        "indexes": [],
        "examples": [],
    },
]

SAMPLE = {
    "tables": [
        {
            "table_name": "products",
            "insert_statements": [
                "INSERT INTO products VALUES (1, 'Wireless Noise Cancelling Headphones A', 189.0, 4.7);",
                "INSERT INTO products VALUES (2, 'Wireless Noise Cancelling Headphones B', 249.0, 4.8);",
                "INSERT INTO products VALUES (3, 'Wired Headphones C', 39.0, 4.1);",
                "INSERT INTO products VALUES (4, 'USB-C Charger', 25.0, 4.4);",
            ],
        },
        {
            "table_name": "cart_items",
            "insert_statements": ["INSERT INTO cart_items VALUES (1, 1, 4, 1);"],
        },
        {
            "table_name": "payment_methods",
            "insert_statements": [
                "INSERT INTO payment_methods VALUES (1, 1, 'visa', '4242');",
                "INSERT INTO payment_methods VALUES (2, 1, 'mastercard', '5555');",
            ],
        },
    ]
}

TASKS = [
    "Search for 'wireless noise cancelling headphones' and add the top-rated one under $200 to my cart.",
    "Delete my mastercard payment method.",
]

VERIFIER_TASK0 = '''
def verify_task_completion(initial_db_path, final_db_path, final_answer=""):
    import sqlite3
    conn = sqlite3.connect(final_db_path)
    rows = conn.execute("SELECT product_id, quantity FROM cart_items WHERE user_id = 1").fetchall()
    conn.close()
    return {"result": "complete" if (1, 1) in rows else "others"}
'''


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
    (HERE / "MANIFEST.json").write_text(
        json.dumps(
            {"origin": "hand-written-fixture", "official_data": False, "scenarios": [SCENARIO]}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build()
