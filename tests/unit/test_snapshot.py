import sqlite3
from pathlib import Path

from workbench.envs import snapshot


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE tags (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
        c.execute("CREATE TABLE logs (msg TEXT)")  # no primary key -> rowid
        c.executemany("INSERT INTO items VALUES (?, ?)", [(1, "a"), (2, "b"), (3, "c")])
        c.execute("INSERT INTO tags VALUES (1, 1)")


def test_snapshot_restore_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "work.db"
    _make_db(db)
    snap = snapshot.snapshot(db, tmp_path / "snaps" / "s1.db")
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM items")
    snapshot.restore(snap, db)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM items").fetchone()[0] == 3


def test_diff_rows_and_primary_keys(tmp_path: Path) -> None:
    before, after = tmp_path / "a.db", tmp_path / "b.db"
    _make_db(before)
    snapshot.snapshot(before, after)
    with sqlite3.connect(after) as c:
        c.execute("DELETE FROM items WHERE id = 1")
        c.execute("UPDATE items SET name = 'B' WHERE id = 2")
        c.execute("INSERT INTO items VALUES (9, 'z')")
        c.execute("INSERT INTO tags VALUES (2, 3)")
        c.execute("INSERT INTO logs VALUES ('x')")
        c.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
    d = snapshot.diff(before, after)
    assert d.is_changed
    items = d.tables["items"]
    assert (items.rows_before, items.rows_after) == (3, 3)
    assert items.added == [9] and items.removed == [1] and items.changed == [2]
    assert d.tables["tags"].added == [(2, 3)]
    assert d.tables["logs"].added == [1]
    assert d.tables_added == ["extra"]
    as_dict = d.as_dict()
    assert set(as_dict["tables"]) == {"items", "tags", "logs"}


def test_diff_unchanged(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    _make_db(a)
    b = snapshot.snapshot(a, tmp_path / "b.db")
    assert not snapshot.diff(a, b).is_changed
