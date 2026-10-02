"""Replay recorded agent requests against an OpenAI-compatible server and time them.

Each simulated client replays one recorded episode call by call, the way the agent sends them
(every call's prompt extends the previous one), so prefix caching sees the reuse a real agent
produces. Generation length is fixed (``max_tokens`` with ``ignore_eos``) and ``tool_choice`` is
``none``: the tools are still rendered into the prompt, but no output parsing is involved, so
runs differ only in the server's configuration. Prefix-cache counters are read from the vLLM
``/metrics`` endpoint before and after each setting.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from workbench.lab.recorder import load_recording
from workbench.llm.backends.openai_compat import _tools_payload

METRIC_RE = re.compile(r"^(vllm:prefix_cache_(?:queries|hits)_total)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$")


def load_episodes(run_dir: Path, n: int, seed: int = 0) -> list[list[dict[str, Any]]]:
    paths = sorted((run_dir / "calls").glob("*.json.gz"))
    random.Random(seed).shuffle(paths)
    episodes = []
    for p in paths:
        rec = load_recording(p)
        tools = _tools_payload(rec["tools"]) if rec["tools"] else []
        calls = [{"messages": c["messages"], "tools": tools if c["with_tools"] else []} for c in rec["calls"]]
        if calls:
            episodes.append(calls)
        if len(episodes) >= n:
            break
    return episodes


def parse_prefix_metrics(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = METRIC_RE.match(line.strip())
        if m:
            out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(2))
    return out


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


async def _one(
    client: httpx.AsyncClient, model: str, call: dict[str, Any], max_tokens: int, thinking: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": call["messages"],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "ignore_eos": True,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    if call["tools"]:
        payload["tools"] = call["tools"]
        payload["tool_choice"] = "none"
    started = time.perf_counter()
    first: float | None = None
    usage: dict[str, Any] = {}
    async with client.stream("POST", "/chat/completions", json=payload) as resp:
        if resp.status_code >= 400:
            body = (await resp.aread()).decode("utf-8", errors="replace")
            return {"error": f"HTTP {resp.status_code}: {body[:300]}"}
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                usage = chunk["usage"]
            if first is None and any(
                (c.get("delta") or {}).get("content") for c in chunk.get("choices") or []
            ):
                first = time.perf_counter()
    end = time.perf_counter()
    return {
        "ttft_ms": ((first or end) - started) * 1000,
        "latency_ms": (end - started) * 1000,
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
    }


async def run_setting(
    base_url: str,
    model: str,
    episodes: list[list[dict[str, Any]]],
    concurrency: int,
    *,
    max_tokens: int = 256,
    thinking: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    root = base_url.rstrip("/")
    metrics_url = root[: -len("/v1")] + "/metrics" if root.endswith("/v1") else root + "/metrics"
    timeout = httpx.Timeout(600.0, connect=10.0)
    async with httpx.AsyncClient(base_url=root, timeout=timeout, transport=transport) as client:
        before = parse_prefix_metrics((await client.get(metrics_url)).text)
        queue: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue()
        for ep in episodes:
            queue.put_nowait(ep)
        rows: list[dict[str, Any]] = []

        async def worker() -> None:
            while not queue.empty():
                ep = queue.get_nowait()
                for call in ep:
                    rows.append(await _one(client, model, call, max_tokens, thinking))

        started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        wall = time.perf_counter() - started
        after = parse_prefix_metrics((await client.get(metrics_url)).text)
    ok = [r for r in rows if "error" not in r]
    queries = after.get("vllm:prefix_cache_queries_total", 0.0) - before.get(
        "vllm:prefix_cache_queries_total", 0.0
    )
    hits = after.get("vllm:prefix_cache_hits_total", 0.0) - before.get("vllm:prefix_cache_hits_total", 0.0)
    ttft = [r["ttft_ms"] for r in ok]
    lat = [r["latency_ms"] for r in ok]
    out_tokens = sum(r["completion_tokens"] for r in ok)
    return {
        "concurrency": concurrency,
        "episodes": len(episodes),
        "requests": len(rows),
        "errors": len(rows) - len(ok),
        "errors_sample": [r["error"] for r in rows if "error" in r][:3],
        "wall_s": round(wall, 2),
        "prompt_tokens_mean": round(statistics.mean([r["prompt_tokens"] for r in ok]), 1) if ok else 0,
        "ttft_ms_p50": round(percentile(ttft, 0.5), 1),
        "ttft_ms_p95": round(percentile(ttft, 0.95), 1),
        "latency_ms_p50": round(percentile(lat, 0.5), 1),
        "latency_ms_p95": round(percentile(lat, 0.95), 1),
        "output_tok_s": round(out_tokens / wall, 1) if wall else 0.0,
        "requests_per_s": round(len(ok) / wall, 3) if wall else 0.0,
        "prefix_cache_hit_rate": round(hits / queries, 4) if queries else None,
    }


async def bench(
    base_url: str,
    model: str,
    run_dir: Path,
    label: str,
    concurrencies: list[int],
    out: Path,
    *,
    max_tokens: int = 256,
    seed: int = 0,
) -> dict[str, Any]:
    """One result per concurrency level; clients = level, episodes = max(8, 2 x level)."""
    results = []
    for c in concurrencies:
        episodes = load_episodes(run_dir, max(8, 2 * c), seed=seed + c)
        results.append(await run_setting(base_url, model, episodes, c, max_tokens=max_tokens))
    doc = {
        "label": label,
        "model": model,
        "source": str(run_dir),
        "max_tokens": max_tokens,
        "results": results,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc
