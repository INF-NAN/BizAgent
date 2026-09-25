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

Training (`workbench train preflight` and `launch`, ADR-026) gets ``train_env()``: the base
variables, the network settings and what the ML stack reads, never a credential-like name.
"""

from __future__ import annotations

import os
import re
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


# Training (ADR-026). The train env runs third-party ML code, and the smoke run feeds model output
# to AgentFly's calculator tool, whose sympify uses eval (sympy/core/sympify.py:138-139). Named
# variables it may need: caches under the home directory (uv, Hugging Face, torch, Triton), the
# CUDA driver libraries (LD_LIBRARY_PATH, set by NVIDIA container images), a C compiler for
# Triton, thread counts and determinism switches read by veRL, and AgentFly's own settings
# (agentfly/__init__.py:16-47, agents/agent_base.py:155-171).
TRAIN_VARS: tuple[str, ...] = (
    "HOME",
    "USER",
    "LOGNAME",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CC",
    "CXX",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "TOKENIZERS_PARALLELISM",
    "PYTHONHASHSEED",
    "PYTHONFAULTHANDLER",
    "CUBLAS_WORKSPACE_CONFIG",
    "FLASH_ATTENTION_DETERMINISTIC",
    "XDG_CACHE_HOME",
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "UV_PYTHON_INSTALL_DIR",
    "AGENT_DATA_DIR",
    "AGENT_CONFIG_DIR",
    "TOOL_ERROR_AS_OBSERVATION",
    "REWARD_DECOMPOSITION",
    "REWARD_DECOMPOSITION_GAMMA",
)
# Families of settings read by CUDA, NCCL/Gloo, PyTorch, Triton, vLLM, Ray, veRL and Hugging Face.
TRAIN_PREFIXES: tuple[str, ...] = (
    "CUDA_",
    "NVIDIA_",
    "NCCL_",
    "GLOO_",
    "TORCH_",
    "PYTORCH_",
    "TORCHINDUCTOR_",
    "TRITON_",
    "VLLM_",
    "RAY_",
    "VERL_",
    "HF_",
    "HUGGINGFACE_",
    "TRANSFORMERS_",
    "AGENTFLY_",
)
# Never passed under a prefix: HF_TOKEN, VLLM_API_KEY and the like. Tracker keys (WANDB_API_KEY,
# SWANLAB_API_KEY, VOLC_SECRET_ACCESS_KEY, which veRL can read) are not in any family anyway.
CREDENTIAL_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)


def pick(environ: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    """The entries of ``environ`` named in ``names`` (missing ones are skipped)."""
    return {name: environ[name] for name in names if name in environ}


def generated_code_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a child that runs generated code and calls no LLM: ``BASE_VARS`` only."""
    env = pick(os.environ if environ is None else environ, BASE_VARS)
    env.setdefault("PYTHONPYCACHEPREFIX", str(PYCACHE_DIR.resolve()))
    return env


def train_env(environ: Mapping[str, str] | None = None, passthrough: Iterable[str] = ()) -> dict[str, str]:
    """Environment for the train env's processes (ADR-026).

    ``BASE_VARS``, ``NETWORK_VARS`` (model downloads), ``TRAIN_VARS`` and every name under
    ``TRAIN_PREFIXES`` that does not look like a credential. ``passthrough`` (the owner's
    ``train.env_passthrough``) adds names on purpose, credential-like ones included.
    """
    source = os.environ if environ is None else environ
    env = generated_code_env(source)
    env.update(pick(source, NETWORK_VARS))
    env.update(pick(source, TRAIN_VARS))
    for name, value in source.items():
        if name.startswith(TRAIN_PREFIXES) and not CREDENTIAL_NAME.search(name):
            env[name] = value
    env.update(pick(source, passthrough))
    return env
