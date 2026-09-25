#!/usr/bin/env python3
"""Docker smoke test (Phase 14, owner decision D15; runs in .github/workflows/docker-smoke.yml).

Builds the compose stack, starts it WITHOUT the gpu profile, drives the mock demo over the HTTP
API (query -> write -> preview + approval -> done), prints image sizes and the cold-start time,
and stops the stack. The approval request must carry the preview diff (a shadow environment in
the env-manager container, ADR-029) and the approved write must match it. Standard library only,
so it runs on a bare CI runner.

The stack needs no edits for this: the image already contains tests/fixtures, compose mounts
./data and reads an optional ./.env. The script copies the hand-written mini dataset to
./data/awm1k and writes a .env that selects the scripted mock LLM; it refuses to run if either
already exists (a real dataset or a user's .env must never be overwritten) and removes both at
the end. Full mode therefore needs a clean checkout without ./data (as on a CI runner).

Engineering facts only (R3): sizes come from `docker image inspect`, times from time.monotonic().

    python3 scripts/docker_smoke.py                          # full run (needs Docker + Compose v2)
    python3 scripts/docker_smoke.py --api-only http://HOST:PORT   # only the API flow
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MINI = ROOT / "tests" / "fixtures" / "awm_mini"
DATASET = ROOT / "data" / "awm1k"
ENV_FILE = ROOT / ".env"
API = "http://127.0.0.1:8080"
SCENARIO = "mini_e_commerce"
REQUEST = "Add the best wireless noise cancelling headphones under $200 to my cart"
ENV_LINES = (
    "WORKBENCH_LLM__MOCK_FIXTURE=tests/fixtures/trajectories/demo_query_write_approve.jsonl",
    "WORKBENCH_LLM__MOCK_RESET_PER_SESSION=true",
)


class SmokeError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def run(*cmd: str, capture: bool = False) -> str:
    log(f"$ {' '.join(cmd)}")
    done = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture, check=False)
    if done.returncode != 0:
        if capture:
            log(done.stdout + done.stderr)
        raise SmokeError(f"command failed ({done.returncode}): {' '.join(cmd)}")
    return done.stdout if capture else ""


def http(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 120) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def sse_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def wait_healthy(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body = http("GET", url, timeout=5)
            if status == 200 and json.loads(body).get("ok") is True:
                return
            last = f"HTTP {status}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise SmokeError(f"{url} not healthy after {timeout_s}s ({last})")


def api_flow(base: str) -> list[str]:
    """Query -> write -> approval -> done over the HTTP API; returns summary lines."""
    out: list[str] = []
    t0 = time.monotonic()
    status, body = http("POST", f"{base}/sessions", {"scenario": SCENARIO})
    if status != 200:
        raise SmokeError(f"POST /sessions -> {status}: {body[:300]}")
    session = json.loads(body)
    sid = session["session_id"]
    took = time.monotonic() - t0
    out.append(f"session start (POST /sessions, {SCENARIO}): {took:.1f} s, {len(session['tools'])} tools")

    status, body = http("POST", f"{base}/sessions/{sid}/messages", {"content": REQUEST})
    events = sse_events(body)
    if status != 200 or not events:
        raise SmokeError(f"POST /messages -> {status}: {body[:300]}")
    reads = [e for e in events if e.get("type") == "tool_call"]
    last = events[-1]
    if last.get("type") != "approval_required" or not reads:
        kinds = [e.get("type") for e in events]
        raise SmokeError(f"expected a read tool call, then approval_required; got {kinds}")
    calls = ", ".join(f"{e['tool']} -> {e['status']}" for e in reads)
    out.append(f"query: {calls}")
    out.append(f"write paused for approval: {last.get('tool')} (risk {last.get('risk')})")
    # the approval card carries the preview diff: the rows the call will change (ADR-029)
    preview = last.get("preview") or {}
    added = (((preview.get("changes") or {}).get("tables") or {}).get("cart_items") or {}).get("added") or []
    if preview.get("status") != "ok" or [a.get("row", {}).get("product_offer_id") for a in added] != [11]:
        raise SmokeError(f"expected a preview adding one cart_items row for offer 11, got {preview}")
    total_ms = (preview.get("timings_ms") or {}).get("total")
    out.append(f"preview (shadow env in the env-manager): {preview.get('summary')}, {total_ms} ms")

    status, body = http("GET", f"{base}/approvals")
    pending = json.loads(body) if status == 200 else []
    if not any(p.get("approval_id") == sid for p in pending):
        raise SmokeError(f"GET /approvals -> {status}: {body[:300]}")

    status, body = http("POST", f"{base}/approvals/{sid}", {"approved": True, "approver": "ci-docker-smoke"})
    events = sse_events(body)
    if status != 200 or not events or events[-1].get("type") != "done":
        raise SmokeError(f"POST /approvals -> {status}: {[e.get('type') for e in events]} {body[-300:]}")
    writes = [e for e in events if e.get("type") == "tool_call"]
    calls = ", ".join(f"{e['tool']} -> {e['status']}" for e in writes)
    out.append(f"approved by ci-docker-smoke: {calls}")
    checks = [(e.get("preview_check") or {}).get("result") for e in writes]
    if checks != ["match"]:
        raise SmokeError(f"expected the approved write to match its preview, got {checks}")
    out.append("preview check of the approved write: match")
    out.append(f"done: {events[-1].get('final_answer')!r}")

    status, body = http("GET", f"{base}/sessions/{sid}/diff")
    diff = json.loads(body) if status == 200 else {}
    added = (diff.get("tables") or {}).get("cart_items", {}).get("added")
    if not added:
        raise SmokeError(f"GET /diff -> {status}: expected a new cart_items row, got {body[:300]}")
    out.append(f"db diff: cart_items added {added}")

    status, _ = http("DELETE", f"{base}/sessions/{sid}")
    if status != 200:
        raise SmokeError(f"DELETE /sessions -> {status}")
    return out


def prepare() -> None:
    if DATASET.parent.exists() or ENV_FILE.exists():
        raise SmokeError(f"{DATASET.parent} or {ENV_FILE} already exists; full mode needs a clean checkout")
    DATASET.mkdir(parents=True)
    for f in sorted(MINI.glob("gen_*.jsonl")):
        shutil.copy2(f, DATASET / f.name)
    ENV_FILE.write_text("\n".join(ENV_LINES) + "\n", encoding="utf-8")


def cleanup() -> None:
    ENV_FILE.unlink(missing_ok=True)
    shutil.rmtree(DATASET.parent, ignore_errors=True)  # created by prepare(); container files may stay


def full() -> None:
    summary: list[str] = []
    prepare()
    started = False
    try:
        server = run("docker", "version", "--format", "{{.Server.Version}}", capture=True).strip()
        summary.append(f"docker {server}")
        summary.append("compose " + run("docker", "compose", "version", "--short", capture=True).strip())
        t = time.monotonic()
        run("docker", "compose", "build")
        summary.append(f"build (docker compose build): {time.monotonic() - t:.1f} s")
        for image in run("docker", "compose", "config", "--images", capture=True).split():
            inspect = run("docker", "image", "inspect", "--format", "{{.Id}} {{.Size}}", image, capture=True)
            image_id, size = inspect.split()
            summary.append(f"image {image}: {int(size) / 1e6:.1f} MB ({image_id[:19]})")
        t = time.monotonic()
        started = True
        run("docker", "compose", "up", "-d")
        wait_healthy(f"{API}/healthz", timeout_s=300)
        summary.append(f"cold start (docker compose up -d -> GET /healthz ok): {time.monotonic() - t:.1f} s")
        summary += api_flow(API)
        summary.append("result: PASS")
    except BaseException:
        if started:
            subprocess.run(["docker", "compose", "logs", "--tail", "80"], cwd=ROOT, check=False)
        summary.append("result: FAIL")
        raise
    finally:
        if started:
            subprocess.run(["docker", "compose", "down"], cwd=ROOT, check=False)
        cleanup()
        log("\n== docker smoke summary ==")
        for line in summary:
            log(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-only", metavar="BASE_URL", help="only run the API flow against a running API")
    args = parser.parse_args()
    try:
        if args.api_only:
            for line in api_flow(args.api_only.rstrip("/")):
                log(line)
            log("result: PASS")
        else:
            full()
    except SmokeError as exc:
        log(f"docker smoke failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
