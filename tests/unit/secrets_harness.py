"""Planted secrets and leak checks for the subprocess isolation tests (ADR-019)."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest

from workbench.subprocess_env import BASE_VARS

MARK = "planted-not-a-real-secret"  # pragma: allowlist secret
PLANTED = {
    "DEEPSEEK_API_KEY": MARK,
    "OPENAI_API_KEY": MARK,
    "ANY_VENDOR_API_KEY": MARK,
    "GH_TOKEN": MARK,
    "AWS_SECRET_ACCESS_KEY": MARK,
    "HF_TOKEN": MARK,
    "WORKBENCH_APPROVAL_SECRET": MARK,
}

SECRET_LIKE = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|ACCESS_KEY|ACCESS_KEY_ID|CREDENTIALS?)$", re.I)

# Variables the children of AWM's launcher may carry besides BASE_VARS, all set inside the
# launcher: PORT and DATABASE_PATH (awm/core/server.py:157-158), the two KMP_* flags from
# scikit-learn 1.9.1, which awm.tools pulls in via mcp_agent (sklearn/__init__.py:56,60), and the
# working directory `sh` exports.
TREE_EXTRA = (
    "PORT",
    "DATABASE_PATH",
    "KMP_DUPLICATE_LIB_OK",
    "KMP_INIT_AT_FORK",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "_",
)

# Prints the variable NAMES a child sees (never values) and whether a planted value reached it.
PROBE = (
    "import json, os; print(json.dumps({'names': sorted(os.environ), "
    f"'planted': any({MARK!r} in v for v in os.environ.values())}}))"
)


def plant(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)


def leaked(names: Iterable[str]) -> list[str]:
    """``DEEPSEEK_API_KEY``, any ``*_API_KEY`` and other secret-looking names among ``names``."""
    return sorted(
        n for n in names if n == "DEEPSEEK_API_KEY" or n.endswith("_API_KEY") or SECRET_LIKE.search(n)
    )


def unexpected(names: Iterable[str], extra: Iterable[str] = ()) -> list[str]:
    """Names outside the allowlist (deny-first: anything not listed is a finding)."""
    allowed = {*BASE_VARS, *extra}
    return sorted(n for n in names if n not in allowed)
