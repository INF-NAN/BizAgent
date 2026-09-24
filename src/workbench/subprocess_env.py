"""Environment of subprocesses that run generated code: an allowlist, deny by default (ADR-019).

The AWM env servers execute each scenario's LLM-generated ``full_code``
(awm/core/server.py:163); `awm env check_all` starts those servers as well and they inherit its
environment (awm/core/env.py:161-172 passes no ``env=``); `awm env reset_db` runs generated SQL.
None of them calls an LLM, so they get ``generated_code_env()``: the variables in ``BASE_VARS``
and nothing else. ``DEEPSEEK_API_KEY``, cloud and git tokens and every other variable of this
process are dropped, whether or not their names look secret.

The `awm gen` steps call the LLM and `gen env` / `gen verifier` also run the code they generate
(awm/core/env.py:161-172, awm/core/verifier.py:104), so a gen step gets the base variables plus
the LLM settings only (``LLM_VARS``, ``NETWORK_VARS``; see ``SynthRunner.step_env``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

PYCACHE_DIR = Path(".cache/pycache")

# Needed to run a Python child: PATH (AWM pipes the server through `sh` and `tee`,
# awm/core/server.py:163), locale, time zone, temp dir and interpreter I/O settings.
# PYTHONPYCACHEPREFIX keeps __pycache__ out of the submodules (CLAUDE.md).
BASE_VARS: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "PYTHONUNBUFFERED",
    "PYTHONPYCACHEPREFIX",
)

# Read by AWM's LLM clients (awm/gpt.py:38-55, awm/tools.py:407-432, awm/core/scenario.py:63-84).
LLM_VARS: tuple[str, ...] = (
    "AWM_SYN_LLM_PROVIDER",
    "AWM_SYN_OVERRIDE_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_ENDPOINT_URL",
    "AZURE_OPENAI_API_KEY",
    "EMBEDDING_OPENAI_API_KEY",
    "EMBEDDING_OPENAI_BASE_URL",
)

# Outbound proxy and CA bundle, for a step that calls an LLM API itself.
NETWORK_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def pick(environ: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    """The entries of ``environ`` named in ``names`` (missing ones are skipped)."""
    return {name: environ[name] for name in names if name in environ}


def generated_code_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a child that runs generated code and calls no LLM: ``BASE_VARS`` only."""
    env = pick(os.environ if environ is None else environ, BASE_VARS)
    env.setdefault("PYTHONPYCACHEPREFIX", str(PYCACHE_DIR.resolve()))
    return env
