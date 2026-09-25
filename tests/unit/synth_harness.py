"""Zero-cost synthesis harness (owner decision D17): no paid API is involved.

- ``fake_upstream``: an OpenAI-compatible ``/chat/completions`` served on a real local port; its
  reply is a digest of the request body, so a rerun that sends the same requests gets the same
  results.
- ``FAKE_STEP``: stands in for ``python -m awm.cli gen <step>``. It is a real subprocess that sends
  its requests through the workbench proxy one after another and can leave a "test server"
  running in its own session, as AWM's `gen env` does (awm/core/env.py:161-172). Like AWM, it
  retries a request that got an HTTP error (``attempts`` in all), then writes an empty reply and
  still exits 0, and `verifier` appends each reply to its output as it arrives while the other
  steps write once at the end. ``fake_step_runner`` runs it for every `awm gen` command.
- ``ScriptedTransport``: the proxy's upstream transport, answering chosen requests with an HTTP
  error or a network error for their first attempts (ADR-024 tests).
- ``main``: the driver, run as its own process (``python -m tests.unit.synth_harness ...``) so a
  test can signal it by its exact PID. It runs SynthRunner and the proxy the way
  `workbench synth run --execute` does.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MODEL = "fake-model"
PRICING = "currency: CNY\nmodels:\n  fake-model:\n    input_per_1m: 1.0\n    output_per_1m: 1.0\n"

FAKE_STEP = r"""
import json, os, subprocess, sys, urllib.error, urllib.request
output, step, requests, server_pidfile = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
attempts = int(sys.argv[5]) if len(sys.argv) > 5 else 1
# like AWM: gen verifier appends as it goes (awm/core/verifier.py:172-176), the others write once
append = step == "verifier"
if server_pidfile != "-" and not os.path.exists(server_pidfile):
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    with open(server_pidfile, "w") as fh:
        fh.write(str(server.pid))
replies = []
for i in range(requests):
    # same expression as request_body() in tests/unit/synth_harness.py
    content = {"role": "user", "content": f"{step} request {i}"}
    body = json.dumps({"model": os.environ["AWM_SYN_OVERRIDE_MODEL"], "messages": [content]}).encode()
    reply = ""
    for attempt in range(attempts):
        req = urllib.request.Request(
            os.environ["OPENAI_BASE_URL"] + "/chat/completions",
            data=body,
            headers={"authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                reply = json.load(resp)["choices"][0]["message"]["content"]
            break
        except urllib.error.HTTPError as exc:
            # like AWM's GPTClient: retry the same body, then an error becomes an empty reply and
            # the step goes on (awm/gpt.py:168-206, 103-131)
            print(f"HTTP {exc.code}: {exc.read().decode()}", file=sys.stderr)
    replies.append(reply)
    if append:
        with open(output, "a") as fh:
            fh.write(reply + "\n")
if not append:
    with open(output, "w") as fh:
        fh.write("\n".join(replies) + "\n")
"""


def request_body(model: str, step: str, i: int) -> bytes:
    """The exact bytes a fake step sends for its i-th request (same expression as in FAKE_STEP)."""
    content = {"role": "user", "content": f"{step} request {i}"}
    return json.dumps({"model": model, "messages": [content]}).encode()


def expected_reply(body: bytes) -> str:
    return "reply-" + hashlib.sha256(body).hexdigest()[:12]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def fake_upstream(
    delay_s: float = 0.0,
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    hold: dict[bytes, float] | None = None,
) -> Starlette:
    """Deterministic OpenAI-compatible chat endpoint; every call reports the given usage.

    ``hold`` maps request bodies to a longer delay, applied the first time each one arrives: that
    request stays in flight long enough for a test to interrupt the step while it waits.
    """
    held = dict(hold or {})

    async def chat(request: Request) -> JSONResponse:
        body = await request.body()
        await asyncio.sleep(held.pop(body, delay_s))
        return JSONResponse(
            {
                "model": json.loads(body)["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": expected_reply(body)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            }
        )

    return Starlette(routes=[Route("/chat/completions", chat, methods=["POST"])])


class ScriptedTransport(httpx.AsyncBaseTransport):
    """The proxy's upstream transport: scripted errors for chosen requests, the rest forwarded.

    ``script`` maps a request body to the outcomes of its first attempts, in order: an HTTP status
    such as "402", "429" or "503", or "network" (the connection fails). ``seen`` counts every
    attempt per body.
    """

    def __init__(self, script: dict[bytes, list[str]]) -> None:
        self.script = {body: list(outcomes) for body, outcomes in script.items()}
        self.seen: Counter[bytes] = Counter()
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.seen[body] += 1
        outcomes = self.script.get(body)
        if outcomes:
            outcome = outcomes.pop(0)
            if outcome == "network":
                raise httpx.ConnectError("scripted network error", request=request)
            return httpx.Response(int(outcome), json={"error": {"message": f"scripted HTTP {outcome}"}})
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def fake_step_runner(
    root: Path, requests: int, server_step: str = "-", attempts: int = 1
) -> Callable[[list[str], dict[str, str], Path], int]:
    """A SynthRunner command runner that runs FAKE_STEP for every `awm gen <step>` command."""
    from workbench.synth.runner import default_command_runner

    def run_step(cmd: list[str], env: dict[str, str], log: Path) -> int:
        step = cmd[cmd.index("gen") + 1]
        flag = "--output" if "--output" in cmd else "--output_path"
        pidfile = str(root / f"{step}.server.pid") if step == server_step else "-"
        out = cmd[cmd.index(flag) + 1]
        fake = [sys.executable, "-c", FAKE_STEP, out, step, str(requests), pidfile, str(attempts)]
        return default_command_runner(fake, env, log)

    return run_step


def start_driver(
    run_dir: Path, upstream: str, *, requests: int, server_step: str = "-", budget: float | None = None
) -> subprocess.Popen[bytes]:
    """Start the driver process; its PID is exactly the process a test signals."""
    args = [str(run_dir), upstream, str(requests), server_step, str(budget)]
    return subprocess.Popen(
        [sys.executable, "-m", "tests.unit.synth_harness", *args], cwd=Path(__file__).resolve().parents[2]
    )


def main(argv: list[str]) -> int:
    from workbench.config import Settings
    from workbench.synth.ledger import Ledger, load_prices
    from workbench.synth.proxy import create_proxy_app
    from workbench.synth.runner import (
        ProxyThread,
        SynthBudgetExceeded,
        SynthError,
        SynthInterrupted,
        SynthRunner,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("upstream")
    parser.add_argument("requests", type=int)
    parser.add_argument("server_step")
    parser.add_argument("budget")
    a = parser.parse_args(argv)
    budget = None if a.budget == "None" else float(a.budget)
    pricing = a.run_dir.parent / "pricing.yaml"
    pricing.parent.mkdir(parents=True, exist_ok=True)
    pricing.write_text(PRICING, encoding="utf-8")

    run_step = fake_step_runner(a.run_dir.parent, a.requests, a.server_step)
    settings = Settings(synth={"out_dir": a.run_dir.parent, "budget": budget, "pricing_file": pricing})  # type: ignore[arg-type]
    environ = {"PATH": os.environ["PATH"], "OPENAI_API_KEY": "k", "AWM_SYN_OVERRIDE_MODEL": MODEL}
    environ["EMBEDDING_OPENAI_API_KEY"] = "e"  # pragma: allowlist secret
    runner = SynthRunner(settings, a.run_dir, scenarios=1, command_runner=run_step, environ=environ)
    currency, prices = load_prices(pricing)
    app = create_proxy_app(
        upstream_base_url=a.upstream,
        upstream_api_key=None,
        cache_dir=a.run_dir / "llm_cache",
        ledger=Ledger(a.run_dir / "ledger.jsonl"),
        max_retries=0,
        prices=prices,
        budget=budget,
        currency=currency,
    )
    with ProxyThread(app, "127.0.0.1", free_port()) as base:
        try:
            runner.execute(proxy_base=base, validate=False)
        except SynthInterrupted as exc:
            print(exc)
            return 130
        except SynthBudgetExceeded as exc:
            print(exc)
            return 3
        except SynthError as exc:
            print(exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
