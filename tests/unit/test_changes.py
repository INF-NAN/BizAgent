"""workbench.envs.changes: row-level changes and the structural preview comparison (ADR-029)."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from workbench.envs.changes import changes, compare, summary, volatile_reason

SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    total REAL NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_on TEXT DEFAULT (datetime('now')),
    shipped_date TEXT
);
CREATE TABLE tokens (code TEXT PRIMARY KEY, order_id INTEGER NOT NULL);
CREATE TABLE order_tags (order_id INTEGER, tag TEXT, PRIMARY KEY (order_id, tag));
INSERT INTO orders (id, status, total, created_at, updated_on) VALUES
    (1, 'open', 10.0, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
"""


def db(path: Path, *statements: str) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        for s in statements:
            conn.execute(s)
        conn.commit()
    return path


def run(base: Path, dest: Path, *statements: str) -> Path:
    """Apply ``statements`` to a copy of ``base``, like one execution of a tool call."""
    shutil.copy2(base, dest)
    with closing(sqlite3.connect(dest)) as conn:
        for s in statements:
            conn.execute(s)
        conn.commit()
    return dest


def test_rows_columns_and_shape_of_an_insert_and_an_update(tmp_path: Path) -> None:
    before = db(tmp_path / "before.db")
    after = run(
        before,
        tmp_path / "after.db",
        "INSERT INTO orders (status, total, created_at) VALUES ('open', 25.5, '2026-09-25 10:00:00')",
        "UPDATE orders SET status = 'paid', updated_on = '2026-09-25 10:00:01' WHERE id = 1",
    )
    c = changes(before, after)
    orders = c["tables"]["orders"]
    assert c["changed"] and list(c["tables"]) == ["orders"]
    assert orders["counts"] == {"added": 1, "removed": 0, "changed": 1}
    assert orders["added"][0]["key"] == 2 and orders["added"][0]["row"]["total"] == 25.5
    assert orders["changed"][0] == {
        "key": 1,
        "columns": ["status", "updated_on"],
        "before": {"status": "open", "updated_on": "2026-01-01 00:00:00"},
        "after": {"status": "paid", "updated_on": "2026-09-25 10:00:01"},
    }
    assert orders["shape"] == {"added": [2], "removed": [], "changed": [[1, ["status", "updated_on"]]]}
    assert orders["volatile"] == {
        "created_at": "time type",
        "updated_on": "time default",
        "shipped_date": "time name",
    }
    assert orders["key_generated"] is False  # INTEGER PRIMARY KEY: SQLite assigns it
    assert summary(c) == "orders +1 ~1"


def test_only_timestamps_differ_between_two_runs_is_not_a_mismatch(tmp_path: Path) -> None:
    """The same call executed twice from the same state: the rows differ only in time columns."""
    before = db(tmp_path / "before.db")
    call = "INSERT INTO orders (status, total, created_at) VALUES ('open', 25.5, '{ts}')"
    touch = "UPDATE orders SET updated_on = '{ts}' WHERE id = 1"
    preview = run(
        before,
        tmp_path / "preview.db",
        call.format(ts="2026-09-25 10:00:00"),
        touch.format(ts="2026-09-25 10:00:00"),
    )
    actual = run(
        before,
        tmp_path / "actual.db",
        call.format(ts="2026-09-25 10:00:07"),
        touch.format(ts="2026-09-25 10:00:07"),
    )
    p, a = changes(before, preview), changes(before, actual)
    assert p != a  # the recorded rows differ (the timestamps) ...
    check = compare(p, a)
    assert check["result"] == "match" and check["differences"] == []  # ... the structure does not
    assert check["ignored_columns"] == {"orders": ["created_at", "shipped_date", "updated_on"]}


def test_structural_differences_are_a_mismatch(tmp_path: Path) -> None:
    before = db(tmp_path / "before.db")
    preview = run(before, tmp_path / "p.db", "UPDATE orders SET status = 'paid' WHERE id = 1")
    actual = run(
        before,
        tmp_path / "a.db",
        "UPDATE orders SET status = 'paid', total = 0 WHERE id = 1",
        "INSERT INTO order_tags VALUES (1, 'rush')",
    )
    check = compare(changes(before, preview), changes(before, actual))
    assert check["result"] == "preview_mismatch"
    assert "order_tags: changed only in the real call" in check["differences"]
    assert 'orders.changed: preview {1: ["status"]}, actual {1: ["status","total"]}' in check["differences"]
    other = run(before, tmp_path / "o.db", "INSERT INTO orders (id, status, total) VALUES (5, 'x', 1)")
    added = run(before, tmp_path / "n.db", "INSERT INTO orders (id, status, total) VALUES (6, 'x', 1)")
    assert compare(changes(before, other), changes(before, added))["differences"] == [
        "orders.added: preview [5], actual [6]"
    ]


def test_generated_text_keys_are_compared_by_count(tmp_path: Path) -> None:
    before = db(tmp_path / "before.db")
    preview = run(before, tmp_path / "p.db", "INSERT INTO tokens VALUES ('tok_1f3a', 1)")
    actual = run(before, tmp_path / "a.db", "INSERT INTO tokens VALUES ('tok_9c2e', 1)")
    p, a = changes(before, preview), changes(before, actual)
    assert p["tables"]["tokens"]["key_generated"] is True
    check = compare(p, a)
    assert check["result"] == "match" and check["count_only"] == ["tokens"]
    two_rows = ("INSERT INTO tokens VALUES ('a', 1)", "INSERT INTO tokens VALUES ('b', 1)")
    two = run(before, tmp_path / "two.db", *two_rows)
    assert compare(p, changes(before, two))["result"] == "preview_mismatch"


def test_composite_keys_removed_rows_and_display_limit(tmp_path: Path) -> None:
    before = db(
        tmp_path / "before.db",
        *[f"INSERT INTO order_tags VALUES (1, 't{i}')" for i in range(5)],
    )
    after = run(before, tmp_path / "after.db", "DELETE FROM order_tags WHERE order_id = 1")
    c = changes(before, after, max_rows=2)
    tags = c["tables"]["order_tags"]
    assert tags["counts"]["removed"] == 5 and len(tags["removed"]) == 2  # rows shown: capped
    assert len(tags["shape"]["removed"]) == 5  # compared: all of them
    assert tags["removed"][0] == {"key": [1, "t0"], "row": {"order_id": 1, "tag": "t0"}}
    assert compare(c, c)["result"] == "match"
    unchanged = {"tables_added": [], "tables_removed": [], "tables": {}, "changed": False}
    assert changes(before, before) == unchanged


def test_volatile_reason_rules() -> None:
    assert volatile_reason("start", "TIMESTAMP", None) == "time type"
    assert volatile_reason("seen", "TEXT", "CURRENT_DATE") == "time default"
    assert volatile_reason("expires_at", "INTEGER", None) == "time name"
    for name in ("format", "status", "attempts", "update_count"):
        assert volatile_reason(name, "TEXT", "'x'") is None, name
