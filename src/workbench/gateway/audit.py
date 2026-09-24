"""Append-only JSONL audit log with PII redaction."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13-19 digits, optionally grouped by spaces/dashes (card numbers)
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# phone numbers: optional +country, 7-15 digits with separators
PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")


def _mask_card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return f"[CARD ****{digits[-4:]}]"


DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _mask_phone(m: re.Match[str]) -> str:
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15 or DATE.match(raw.strip()):
        return raw
    return "[PHONE]"


def redact_text(text: str) -> str:
    text = EMAIL.sub("[EMAIL]", text)
    text = CARD.sub(_mask_card, text)
    return PHONE.sub(_mask_phone, text)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def summarize(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = redact_text(text)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


@dataclass
class AuditRecord:
    trace_id: str
    session_id: str
    tool: str
    risk: str
    decision: str
    approver: str | None
    args_summary: str
    result_summary: str
    status: str
    duration_ms: float
    ts: float = 0.0


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        record.ts = record.ts or time.time()
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def read(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(ln) for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [r for r in rows if session_id is None or r["session_id"] == session_id]
