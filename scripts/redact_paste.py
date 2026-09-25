#!/usr/bin/env python3
"""Redact a Phase 15 paste-back file before it leaves the GPU machine (TASK_v2 N1).

    uv run python scripts/redact_paste.py data/p15/paste-15a.txt   # writes data/p15/paste-15a.redacted.txt

Replaces keys, tokens, credentials in URLs, emails and card and phone numbers with markers, and
prints how many of each it replaced; the input file stays as it is. Emails, cards and phones start
from the gateway's audit rules (workbench.gateway.audit, ADR-016). Logs are full of numbers that
only look like phones, so a phone here must be written the way the dataset writes one (see
_phone), and the rule never crosses a line break. Patterns cannot know every secret: read the
output before pasting it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from workbench.gateway.audit import CARD, EMAIL, PHONE, _mask_card, _mask_phone

TOKEN = re.compile(r"\b(?:hf_|sk-|ghp_|gho_|ghs_|github_pat_|xox[abprs]-)[A-Za-z0-9_-]{16,}")
URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")
BEARER = re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]{8,}")
# NAME=value where NAME looks like a credential (HF_TOKEN=..., OPENAI_API_KEY=...)
ASSIGNED = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)=([^\s'\";,]+)"
)
# Logs are full of numbers that only look like phones: versions and IPv4 addresses (570.124.06,
# 192.168.1.10), decimals (0.015688), column-aligned table cells and bare counts or sizes. A phone
# here is what the dataset writes (555-111-2222, +1-312-555-0100, +12125550123) or a bare CN mobile.
VERSION_LIKE = re.compile(r"^\d{1,4}(?:\.\d{1,4}){2,3}$")
DOTTED_PHONE = re.compile(r"^\d{3}\.\d{3}\.\d{4}$")
DECIMAL = re.compile(r"^\d+\.\d+$")
CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _phone(m: re.Match[str]) -> str:
    raw = m.group(0)
    if DECIMAL.match(raw) or "  " in raw or "\t" in raw:
        return raw
    if VERSION_LIKE.match(raw) and not DOTTED_PHONE.match(raw):
        return raw
    if raw.isdigit() and not CN_MOBILE.match(raw):
        return raw
    return _mask_phone(m)  # 7-15 digits and not a bare date (the audit rule)


def _assigned(m: re.Match[str]) -> str:
    # API_KEY_ENV=DEEPSEEK_API_KEY names a variable; it is not a value worth hiding
    return m.group(0) if ENV_NAME.match(m.group(2)) else f"{m.group(1)}=[REDACTED]"


def _email(m: re.Match[str]) -> str:
    # git@github.com:owner/repo.git is an SSH remote, not a person's address
    return m.group(0) if m.group(0).startswith("git@") else "[EMAIL]"


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def sub(name: str, pattern: re.Pattern[str], repl: str, value: str) -> str:
        out, n = pattern.subn(repl, value)
        counts[name] = n
        return out

    text = sub("token", TOKEN, "[TOKEN]", text)
    text = sub("url_credentials", URL_CREDENTIALS, "[CREDENTIALS]", text)
    text = sub("bearer", BEARER, "[TOKEN]", text)
    before = text.count("=[REDACTED]")
    text = ASSIGNED.sub(_assigned, text)
    counts["assignment"] = text.count("=[REDACTED]") - before
    before = text.count("[EMAIL]")
    text = EMAIL.sub(_email, text)
    counts["email"] = text.count("[EMAIL]") - before
    before = text.count("[CARD ")
    text = CARD.sub(_mask_card, text)
    counts["card"] = text.count("[CARD ") - before
    before = text.count("[PHONE]")
    text = "\n".join(PHONE.sub(_phone, line) for line in text.split("\n"))  # never across lines
    counts["phone"] = text.count("[PHONE]") - before
    return text, counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src = Path(argv[1])
    out = src.with_name(f"{src.stem}.redacted{src.suffix}")
    text, counts = redact(src.read_text(encoding="utf-8", errors="replace"))
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}; replaced: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
