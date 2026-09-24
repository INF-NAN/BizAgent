"""SQLite snapshot / restore / diff for per-session environment databases.

Uses the sqlite3 online-backup API, so it is safe while the AWM server holds the database
open. ``diff`` compares table-level row counts and primary-key sets (rows keyed by the
table's primary key, or by ``rowid`` when a table has none).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _backup(src: Path, dst: Path) -> None:
    with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)


def snapshot(db_path: Path, dest: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    _backup(db_path, dest)
    return dest


def restore(snapshot_path: Path, db_path: Path) -> None:
    """Overwrite the live database content with the snapshot (in place, same file)."""
    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)
    _backup(snapshot_path, db_path)


@dataclass
class TableDiff:
    rows_before: int
    rows_after: int
    added: list[Any] = field(default_factory=list)
    removed: list[Any] = field(default_factory=list)
    changed: list[Any] = field(default_factory=list)

    @property
    def is_changed(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.rows_before != self.rows_after)


@dataclass
class DbDiff:
    tables: dict[str, TableDiff]
    tables_added: list[str] = field(default_factory=list)
    tables_removed: list[str] = field(default_factory=list)

    @property
    def is_changed(self) -> bool:
        return bool(
            self.tables_added or self.tables_removed or any(t.is_changed for t in self.tables.values())
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": self.is_changed,
            "tables_added": self.tables_added,
            "tables_removed": self.tables_removed,
            "tables": {
                name: {
                    "rows_before": t.rows_before,
                    "rows_after": t.rows_after,
                    "added": t.added,
                    "removed": t.removed,
                    "changed": t.changed,
                }
                for name, t in self.tables.items()
                if t.is_changed
            },
        }


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk = sorted((int(r[5]), str(r[1])) for r in info if int(r[5]) > 0)
    return [name for _, name in pk]


def _rows_by_key(conn: sqlite3.Connection, table: str) -> dict[Any, tuple[Any, ...]]:
    pk = _pk_columns(conn, table)
    key_sql = ", ".join(f'"{c}"' for c in pk) if pk else "rowid"
    cur = conn.execute(f'SELECT {key_sql}, * FROM "{table}"')
    width = len(pk) if pk else 1
    out: dict[Any, tuple[Any, ...]] = {}
    for row in cur.fetchall():
        key = row[0] if width == 1 else tuple(row[:width])
        out[key] = tuple(row[width:])
    return out


def diff(before: Path, after: Path, max_keys: int = 50) -> DbDiff:
    with closing(sqlite3.connect(before)) as a, closing(sqlite3.connect(after)) as b:
        ta, tb = set(_tables(a)), set(_tables(b))
        result = DbDiff(tables={}, tables_added=sorted(tb - ta), tables_removed=sorted(ta - tb))
        for table in sorted(ta & tb):
            ra, rb = _rows_by_key(a, table), _rows_by_key(b, table)
            added = [k for k in rb if k not in ra]
            removed = [k for k in ra if k not in rb]
            changed = [k for k in ra if k in rb and ra[k] != rb[k]]
            result.tables[table] = TableDiff(
                rows_before=len(ra),
                rows_after=len(rb),
                added=sorted(added, key=repr)[:max_keys],
                removed=sorted(removed, key=repr)[:max_keys],
                changed=sorted(changed, key=repr)[:max_keys],
            )
        return result
