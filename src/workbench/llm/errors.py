"""LLM error taxonomy. Only network errors and HTTP 5xx are retried (Phase 4 spec)."""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base LLM error."""


class LLMRetryableError(LLMError):
    """Network failure (connect/read/protocol) or HTTP 5xx — safe to retry."""


class LLMRequestError(LLMError):
    """HTTP 4xx or malformed response — retrying would not help."""


class LLMTimeoutError(LLMError):
    """The overall wall-clock budget for one chat() call was exhausted."""
