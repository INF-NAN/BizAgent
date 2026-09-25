"""Row-level database changes for approval previews, and their structural comparison (ADR-029).

``snapshot.diff`` lists the primary keys that changed. A preview also needs the rows themselves
(what the approver sees: "the rows this call will change") and, for every changed row, the
columns that changed (what is compared with the real call afterwards).

The comparison is structural only: changed tables; added, removed and changed keys; the names of
the changed columns. Volatile columns are kept in the rows but never compared, and every
comparison says which columns it ignored. The basis for the rules below is in docs/RECON.md
("Phase 17"):

- time columns: a declared type containing DATE or TIME (DATETIME, DATE, TIME, TIMESTAMP), a
  time default (CURRENT_TIMESTAMP, datetime('now'), ...) or a time-like name (``*_at``,
  ``*_time``, ``*_date``, ``timestamp``); AWM's generated servers set these with
  ``datetime.utcnow()`` at call time, so two runs never agree on them;
- generated keys: a single-column primary key that is not an INTEGER rowid alias may be
  produced by the server (``uuid4``, ``secrets``, ``random``), so rows added to such a table are
  compared by count, not by key. INTEGER keys are assigned by SQLite from the same starting
  state and are compared as values.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from workbench.envs import snapshot

TIME_TYPE = re.compile(r"DATE|TIME", re.IGNORECASE)
TIME_DEFAULT = re.compile(
    r"CURRENT_(?:TIMESTAMP|DATE|TIME)\b|\b(?:datetime|date|time|strftime|julianday|unixepoch)\s*\(",
    re.IGNORECASE,
)
TIME_NAME = re.compile(r"(?:^|_)(?:at|time|date|timestamp)$", re.IGNORECASE)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def volatile_reason(name: str, decl_type: str, default: str | None) -> str | None:
    if TIME_TYPE.search(decl_type or ""):
        return "time type"
    if default is not None and TIME_DEFAULT.search(default):
        return "time default"
    if TIME_NAME.search(name):
        return "time name"
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def _key(value: Any) -> Any:
    return [_json_value(v) for v in value] if isinstance(value, tuple) else _json_value(value)


class _Table:
    """Schema facts for one table and its rows, keyed as ``snapshot.diff`` keys them."""

    def __init__(self, conn: sqlite3.Connection, name: str) -> None:
        # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
        info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        types = {str(r[1]): str(r[2] or "") for r in info}
        self.volatile = {
            str(r[1]): reason
            for r in info
            if (reason := volatile_reason(str(r[1]), str(r[2] or ""), r[4])) is not None
        }
        self.pk = snapshot.pk_columns(conn, name)
        # INTEGER PRIMARY KEY (exactly that type name) is SQLite's rowid alias: assigned, not generated
        self.key_generated = len(self.pk) == 1 and types[self.pk[0]].strip().upper() != "INTEGER"
        self.columns, rows = snapshot.rows_by_key(conn, name)
        self.rows = {k: dict(zip(self.columns, v, strict=True)) for k, v in rows.items()}


def _row(values: dict[str, Any]) -> dict[str, Any]:
    return {k: _json_value(v) for k, v in values.items()}


def changes(before: Path, after: Path, max_rows: int = 20) -> dict[str, Any]:
    """What changed from ``before`` to ``after``: rows (at most ``max_rows`` per kind and table, for
    display) and the full shape (every key and changed column, for the comparison)."""
    with closing(sqlite3.connect(before)) as a, closing(sqlite3.connect(after)) as b:
        ta, tb = set(snapshot.list_tables(a)), set(snapshot.list_tables(b))
        out: dict[str, Any] = {
            "tables_added": sorted(tb - ta),
            "tables_removed": sorted(ta - tb),
            "tables": {},
        }
        for name in sorted(ta & tb):
            old, new = _Table(a, name), _Table(b, name)
            added = sorted((k for k in new.rows if k not in old.rows), key=repr)
            removed = sorted((k for k in old.rows if k not in new.rows), key=repr)
            changed: list[tuple[Any, list[str]]] = []
            for k in sorted((k for k in old.rows if k in new.rows), key=repr):
                cols = [c for c in new.columns if old.rows[k].get(c) != new.rows[k].get(c)]
                if cols:
                    changed.append((k, cols))
            if not (added or removed or changed):
                continue
            out["tables"][name] = {
                "pk": new.pk,
                "key_generated": new.key_generated,
                "volatile": new.volatile,
                "rows_before": len(old.rows),
                "rows_after": len(new.rows),
                "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
                "added": [{"key": _key(k), "row": _row(new.rows[k])} for k in added[:max_rows]],
                "removed": [{"key": _key(k), "row": _row(old.rows[k])} for k in removed[:max_rows]],
                "changed": [
                    {
                        "key": _key(k),
                        "columns": cols,
                        "before": {c: _json_value(old.rows[k].get(c)) for c in cols},
                        "after": {c: _json_value(new.rows[k].get(c)) for c in cols},
                    }
                    for k, cols in changed[:max_rows]
                ],
                "shape": {
                    "added": [_key(k) for k in added],
                    "removed": [_key(k) for k in removed],
                    "changed": [[_key(k), cols] for k, cols in changed],
                },
            }
        out["changed"] = bool(out["tables"] or out["tables_added"] or out["tables_removed"])
        return out


def _project(table: dict[str, Any] | None) -> dict[str, Any]:
    """The part of a table's changes that is compared: keys and non-volatile changed columns."""
    if not table:
        return {}
    volatile = set(table.get("volatile") or {})
    shape = table["shape"]
    out: dict[str, Any] = {}
    if shape["added"]:
        out["added"] = (
            {"count": len(shape["added"])}
            if table.get("key_generated")
            else sorted(canonical(k) for k in shape["added"])
        )
    if shape["removed"]:
        out["removed"] = sorted(canonical(k) for k in shape["removed"])
    changed = {canonical(k): sorted(c for c in cols if c not in volatile) for k, cols in shape["changed"]}
    changed = {k: cols for k, cols in changed.items() if cols}  # only volatile columns changed: not compared
    if changed:
        out["changed"] = changed
    return out


def _fmt(projected: Any) -> str:
    """A projected part for a difference message: keys as JSON, counts and columns spelled out."""
    if projected is None:
        return "none"
    if isinstance(projected, dict) and set(projected) == {"count"}:
        return f"{projected['count']} row(s)"
    if isinstance(projected, dict):
        return "{" + ", ".join(f"{k}: {canonical(cols)}" for k, cols in projected.items()) + "}"
    return "[" + ", ".join(projected) + "]"


def compare(preview: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Structural comparison of a preview's changes with the real call's changes.

    ``result`` is ``match`` or ``preview_mismatch``; ``differences`` explains a mismatch;
    ``ignored_columns`` lists, per table, the volatile columns that were recorded but not compared;
    ``count_only`` lists the tables whose added rows were compared by count (generated keys).
    """
    differences: list[str] = []
    for kind in ("tables_added", "tables_removed"):
        in_preview, in_actual = sorted(preview.get(kind) or []), sorted(actual.get(kind) or [])
        if in_preview != in_actual:
            differences.append(f"{kind}: preview {in_preview}, actual {in_actual}")
    ptables, atables = preview.get("tables") or {}, actual.get("tables") or {}
    ignored: dict[str, list[str]] = {}
    count_only: list[str] = []
    for name in sorted(set(ptables) | set(atables)):
        tables = [t for t in (ptables.get(name), atables.get(name)) if t]
        volatile = sorted({c for t in tables for c in (t.get("volatile") or {})})
        if volatile:
            ignored[name] = volatile
        if any(t.get("key_generated") for t in tables):
            count_only.append(name)
        p, a = _project(ptables.get(name)), _project(atables.get(name))
        if p == a:
            continue
        if not p or not a:
            where = "the real call" if not p else "the preview"
            differences.append(f"{name}: changed only in {where}")
            continue
        for part in ("added", "removed", "changed"):
            if p.get(part) != a.get(part):
                differences.append(f"{name}.{part}: preview {_fmt(p.get(part))}, actual {_fmt(a.get(part))}")
    return {
        "result": "preview_mismatch" if differences else "match",
        "differences": differences,
        "ignored_columns": ignored,
        "count_only": count_only,
    }


def summary(result: dict[str, Any]) -> str:
    """One line for audit logs and events, e.g. ``cart_items +1; carts ~1``."""
    parts = []
    for name, t in (result.get("tables") or {}).items():
        c = t["counts"]
        marks = [f"+{c['added']}" if c["added"] else "", f"-{c['removed']}" if c["removed"] else ""]
        marks.append(f"~{c['changed']}" if c["changed"] else "")
        parts.append(f"{name} {' '.join(m for m in marks if m)}")
    parts += [f"new table {t}" for t in result.get("tables_added") or []]
    parts += [f"dropped table {t}" for t in result.get("tables_removed") or []]
    return "; ".join(parts) or "no changes"
