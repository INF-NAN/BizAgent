"""Turn successful teacher episodes into chat-format SFT samples.

Every LLM call of an episode is a (messages, response) pair. The act calls of one episode grow a
single conversation (each call's messages start with the previous call's messages and reply), so
a call whose conversation is a prefix of a later call is dropped and only the longest one is
kept; plan and verify calls are separate conversations and stay as they are. A sample is the
conversation plus the tool list sent with it, in the OpenAI request format the serving engine
renders with the model's chat template, so training sees the same text the model is served.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from workbench.lab.episodes import load_results
from workbench.lab.recorder import load_recording
from workbench.llm.backends.openai_compat import _tools_payload


def assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
    if response.get("tool_calls"):
        msg["tool_calls"] = [
            {
                "id": c.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})},
            }
            for i, c in enumerate(response["tool_calls"])
        ]
    return msg


def _norm(msg: dict[str, Any]) -> tuple[Any, ...]:
    calls = tuple(
        (c["function"]["name"], json.dumps(_args(c["function"].get("arguments")), sort_keys=True))
        for c in msg.get("tool_calls") or []
    )
    return (msg.get("role"), (msg.get("content") or "").strip(), calls)


def _args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw or {}


def conversations(recording: dict[str, Any]) -> list[dict[str, Any]]:
    """The episode's calls as full conversations, without those contained in a later one."""
    convs = []
    for call in recording["calls"]:
        messages = [dict(m) for m in call["messages"]] + [assistant_message(call["response"])]
        convs.append({"messages": messages, "with_tools": call["with_tools"]})
    keys = [[_norm(m) for m in c["messages"]] for c in convs]
    kept = []
    for i, conv in enumerate(convs):
        covered = any(
            len(keys[j]) > len(keys[i]) and keys[j][: len(keys[i])] == keys[i]
            for j in range(i + 1, len(convs))
        )
        if not covered:
            kept.append(conv)
    return kept


def build(
    run_dirs: list[Path],
    out: Path,
    *,
    include_failed: bool = False,
    max_per_task: int | None = None,
) -> dict[str, Any]:
    """Write ``out`` (one JSON sample per line) from the episodes of ``run_dirs``.

    Only verified successes are used unless ``include_failed`` (the unfiltered ablation keeps
    every episode that ran to the end). ``max_per_task`` caps the episodes kept per task across
    all run dirs, in the order given, so tasks the model already solves often do not dominate.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()
    scenarios: set[str] = set()
    per_task: Counter[str] = Counter()
    with out.open("w", encoding="utf-8") as f:
        for run_dir in run_dirs:
            for r in load_results(run_dir / "results.jsonl"):
                stats["episodes"] += 1
                if r.get("status") != "ok" or not (r.get("success") or include_failed):
                    continue
                task_key = f"{r['scenario']}#{r['task_id']}"
                if max_per_task is not None and per_task[task_key] >= max_per_task:
                    stats["capped"] += 1
                    continue
                per_task[task_key] += 1
                path = run_dir / "calls" / f"{r['run_id']}.json.gz"
                if not path.is_file():
                    stats["missing_recording"] += 1
                    continue
                recording = load_recording(path)
                tools = _tools_payload(recording["tools"]) if recording["tools"] else []
                stats["used_episodes"] += 1
                stats["used_successful_episodes"] += 1 if r.get("success") else 0
                stats[f"used_from:{run_dir.name}"] += 1
                scenarios.add(r["scenario"])
                for conv in conversations(recording):
                    sample = {
                        "messages": conv["messages"],
                        "tools": tools if conv["with_tools"] else [],
                        "meta": {"run_id": r["run_id"], "scenario": r["scenario"], "tag": r["tag"]},
                    }
                    f.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    stats["samples"] += 1
                    stats["samples_with_tools" if conv["with_tools"] else "samples_without_tools"] += 1
                    stats["assistant_turns"] += sum(1 for m in conv["messages"] if m["role"] == "assistant")
    summary = {**stats, "tasks": len(per_task), "scenarios": len(scenarios), "out": str(out)}
    out.with_suffix(".stats.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary
