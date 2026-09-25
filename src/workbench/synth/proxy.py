"""Local OpenAI-compatible proxy used while AWM synthesis runs.

AWM's GPTClient talks to ``OPENAI_BASE_URL`` (third_party/agent-world-model/awm/gpt.py:53-55,
71-75); scenario generation also calls embeddings via ``EMBEDDING_OPENAI_BASE_URL``
(awm/core/scenario.py:83-86). Pointing both at ``http://127.0.0.1:<port>/step/<step>/v1``
lets us, without touching AWM code: (1) cache identical requests on disk, (2) retry network
errors / 5xx with backoff, (3) attribute token usage to the pipeline step in the ledger, and
(4) stop at a budget (ADR-023): once the run's ledger cost reaches it, requests that would go
upstream are refused with HTTP 402 (cache hits are still served), which makes the current step
fail; the runner then marks it failed and a rerun with a higher budget resumes, and (5) record
every request that ends in an upstream error as ``failed`` (ADR-024): AWM turns such errors into
empty replies and exits 0 (awm/gpt.py:195-206), so the runner judges the step from the ledger.
Every entry carries the request's cache key so the runner can tell AWM's retries of one request
apart from different requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from workbench.synth.ledger import Ledger, LedgerEntry, Price

# What a process behind the proxy gets instead of the upstream key (ADR-019, ADR-025).
PLACEHOLDER_KEY = "workbench-proxy"  # pragma: allowlist secret


def cache_key(endpoint: str, body: bytes) -> str:
    return hashlib.sha256(endpoint.encode() + b"\n" + body).hexdigest()


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    """The upstream's error body, or a wrapper when it is not a JSON object (e.g. an HTML page)."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    return {"error": {"message": f"upstream returned HTTP {resp.status_code}: {resp.text[:300]}"}}


def create_proxy_app(
    *,
    upstream_base_url: str,
    upstream_api_key: str | None,
    cache_dir: Path,
    ledger: Ledger,
    max_retries: int = 3,
    backoff_s: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
    prices: dict[str, Price] | None = None,
    budget: float | None = None,
    currency: str = "CNY",
) -> Starlette:
    client = httpx.AsyncClient(
        base_url=upstream_base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {upstream_api_key}"} if upstream_api_key else {},
        timeout=httpx.Timeout(connect=10, read=1800, write=60, pool=10),
        transport=transport,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    price_table = prices or {}

    def over_budget(model: str) -> str | None:
        """Why a request must not go upstream, or None. Fails closed on models without a price."""
        if budget is None:
            return None
        if model not in price_table:
            return f"model {model!r} has no price in the pricing file, so the budget cannot be enforced"
        spent, unpriced = ledger.spent(price_table)
        if unpriced:
            return f"the ledger has models without a price ({', '.join(unpriced)})"
        if spent >= budget:
            return f"budget reached: spent {spent:.4f} {currency} of {budget} {currency}"
        return None

    async def forward(request: Request) -> JSONResponse:
        step = request.path_params["step"]
        endpoint = request.path_params["endpoint"]
        body = await request.body()
        key = cache_key(endpoint, body)
        cached_file = cache_dir / f"{key}.json"
        payload: dict[str, Any] = json.loads(body or b"{}")
        model = str(payload.get("model", ""))
        if cached_file.exists():
            data = json.loads(cached_file.read_text(encoding="utf-8"))
            ledger.record(LedgerEntry(step, model, 0, 0, cached=True, endpoint=endpoint, key=key))
            return JSONResponse(data)

        def failure(status: int | None, error: str) -> None:
            ledger.record(
                LedgerEntry(
                    step,
                    model,
                    0,
                    0,
                    cached=False,
                    endpoint=endpoint,
                    failed=True,
                    upstream_status=status,
                    error=error[:300],
                    key=key,
                )
            )

        if reason := over_budget(model):
            ledger.record(
                LedgerEntry(step, model, 0, 0, cached=False, endpoint=endpoint, refused=True, key=key)
            )
            message = (
                f"workbench synthesis stopped: {reason}. Raise synth.budget (e.g. WORKBENCH_SYNTH__BUDGET)"
                " and run the same command again to resume."
            )
            return JSONResponse({"error": {"message": message, "type": "budget_exceeded"}}, status_code=402)
        last_status: int | None = None
        last = ""
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"/{endpoint}", content=body, headers={"content-type": "application/json"}
                )
            except httpx.TransportError as exc:
                last_status, last = None, f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except ValueError:
                        failure(resp.status_code, f"non-JSON response: {resp.text[:200]}")
                        message = f"upstream returned HTTP {resp.status_code} without a JSON body"
                        return JSONResponse({"error": {"message": message}}, status_code=502)
                    usage = data.get("usage") or {}
                    ledger.record(
                        LedgerEntry(
                            step,
                            str(data.get("model") or model),
                            int(usage.get("prompt_tokens", 0)),
                            int(usage.get("completion_tokens", 0)),
                            cached=False,
                            endpoint=endpoint,
                            key=key,
                        )
                    )
                    cached_file.write_text(json.dumps(data), encoding="utf-8")
                    return JSONResponse(data, status_code=resp.status_code)
                if resp.status_code < 500:  # 4xx (402, 429, ...) is passed on, not retried here
                    data = _error_body(resp)
                    failure(resp.status_code, json.dumps(data.get("error", data))[:300])
                    return JSONResponse(data, status_code=resp.status_code)
                last_status, last = resp.status_code, f"HTTP {resp.status_code}"
            if attempt < max_retries:
                await asyncio.sleep(backoff_s * (2**attempt))
        failure(last_status, f"after {max_retries + 1} attempt(s): {last}")
        return JSONResponse({"error": {"message": f"upstream failed after retries: {last}"}}, status_code=502)

    return Starlette(routes=[Route("/step/{step}/v1/{endpoint:path}", forward, methods=["POST"])])
