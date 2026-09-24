"""Local OpenAI-compatible proxy used while AWM synthesis runs.

AWM's GPTClient talks to ``OPENAI_BASE_URL`` (third_party/agent-world-model/awm/gpt.py:53-55,
71-75); scenario generation also calls embeddings via ``EMBEDDING_OPENAI_BASE_URL``
(awm/core/scenario.py:83-86). Pointing both at ``http://127.0.0.1:<port>/step/<step>/v1``
lets us, without touching AWM code: (1) cache identical requests on disk, (2) retry network
errors / 5xx with backoff, (3) attribute token usage to the pipeline step in the ledger.
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

from workbench.synth.ledger import Ledger, LedgerEntry


def cache_key(endpoint: str, body: bytes) -> str:
    return hashlib.sha256(endpoint.encode() + b"\n" + body).hexdigest()


def create_proxy_app(
    *,
    upstream_base_url: str,
    upstream_api_key: str | None,
    cache_dir: Path,
    ledger: Ledger,
    max_retries: int = 3,
    backoff_s: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    client = httpx.AsyncClient(
        base_url=upstream_base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {upstream_api_key}"} if upstream_api_key else {},
        timeout=httpx.Timeout(connect=10, read=1800, write=60, pool=10),
        transport=transport,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    async def forward(request: Request) -> JSONResponse:
        step = request.path_params["step"]
        endpoint = request.path_params["endpoint"]
        body = await request.body()
        key = cache_key(endpoint, body)
        cached_file = cache_dir / f"{key}.json"
        payload: dict[str, Any] = json.loads(body or b"{}")
        if cached_file.exists():
            data = json.loads(cached_file.read_text(encoding="utf-8"))
            ledger.record(
                LedgerEntry(step, str(payload.get("model", "")), 0, 0, cached=True, endpoint=endpoint)
            )
            return JSONResponse(data)
        last: str = ""
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"/{endpoint}", content=body, headers={"content-type": "application/json"}
                )
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 500:
                    data = resp.json()
                    if resp.status_code < 400:
                        usage = data.get("usage") or {}
                        ledger.record(
                            LedgerEntry(
                                step,
                                str(data.get("model") or payload.get("model", "")),
                                int(usage.get("prompt_tokens", 0)),
                                int(usage.get("completion_tokens", 0)),
                                cached=False,
                                endpoint=endpoint,
                            )
                        )
                        cached_file.write_text(json.dumps(data), encoding="utf-8")
                    return JSONResponse(data, status_code=resp.status_code)
                last = f"HTTP {resp.status_code}"
            if attempt < max_retries:
                await asyncio.sleep(backoff_s * (2**attempt))
        return JSONResponse({"error": {"message": f"upstream failed after retries: {last}"}}, status_code=502)

    return Starlette(routes=[Route("/step/{step}/v1/{endpoint:path}", forward, methods=["POST"])])
