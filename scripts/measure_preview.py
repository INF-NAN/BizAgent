#!/usr/bin/env python3
"""Measure approval-preview timings (ADR-029): prepare, start, call, diff, reclaim and total.

    uv run python scripts/measure_preview.py                    # mini fixture, 10 previews
    uv run python scripts/measure_preview.py --dataset-dir data/awm1k --scenario e_commerce_33 \\
        --args '{"product_offer_id": 1, "quantity": 1}'          # an official scenario (make data)

Starts one session environment, then runs the same preview N times on it; each preview copies
the session DB, starts and reclaims its own shadow server. Prints per-stage minimum, median and
maximum in milliseconds, and the preview timeout they suggest (five times the slowest total,
rounded up to whole 5 s). Engineering numbers only (rule R3): they depend on the machine, which
is printed with them. No LLM is called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import socket
import statistics
import sys
import tempfile
import time
from pathlib import Path

from workbench.config import EnvSettings
from workbench.envs.manager import EnvManager

STAGES = ("prepare", "start", "call", "diff", "reclaim", "total")


def port_base() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1]) % 20000 + 30000


async def measure(args: argparse.Namespace) -> int:
    arguments = json.loads(args.args)
    print(f"$ {' '.join([Path(sys.argv[0]).as_posix(), *sys.argv[1:]])}")
    print(f"machine: {platform.platform()}, {os.cpu_count()} CPUs, Python {platform.python_version()}")
    print(f"dataset: {args.dataset_dir}  scenario: {args.scenario}")
    print(f"tool: {args.tool} {json.dumps(arguments)}")
    with tempfile.TemporaryDirectory(prefix="workbench-measure-") as tmp:
        base = port_base()
        settings = EnvSettings(
            dataset_dir=args.dataset_dir,
            runs_dir=Path(tmp),
            port_min=base,
            port_max=base + 20,
            start_timeout_s=args.timeout,
        )
        manager = EnvManager(settings)
        try:
            started = time.perf_counter()
            await manager.start(args.scenario, session_id="measure")
            print(f"session env start: {round((time.perf_counter() - started) * 1000)} ms")
            runs: list[dict[str, float]] = []
            for i in range(args.runs):
                r = await manager.preview("measure", args.tool, arguments, timeout_s=args.timeout)
                if r["status"] != "ok":
                    print(f"preview {i + 1}: {r['status']} ({r['error']})")
                    continue
                runs.append(r["timings_ms"])
        finally:
            await manager.stop_all()
    print(f"previews: {len(runs)} ok, {args.runs - len(runs)} failed")
    if not runs:
        return 1
    print(f"{'stage':8} {'min':>6} {'median':>7} {'max':>6}   (ms)")
    for stage in STAGES:
        values = [r[stage] for r in runs]
        print(f"{stage:8} {min(values):6.0f} {statistics.median(values):7.0f} {max(values):6.0f}")
    slowest = max(r["total"] for r in runs) / 1000
    print(f"suggested approval.preview_timeout_s: {5 * math.ceil(5 * slowest / 5)} (5 x the slowest total)")
    return 0 if len(runs) == args.runs else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure approval-preview timings.")
    ap.add_argument("--dataset-dir", type=Path, default=Path("tests/fixtures/awm_mini"))
    ap.add_argument("--scenario", default="mini_e_commerce")
    ap.add_argument("--tool", default="add_item_to_cart")
    ap.add_argument("--args", default='{"product_offer_id": 11, "quantity": 1}', help="JSON arguments")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=120.0, help="per preview (generous: we measure)")
    return asyncio.run(measure(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
