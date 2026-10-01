"""Parse `<tool_call>{json}</tool_call>` blocks from plain text content.

Same wire format as AWM's agent (third_party/agent-world-model/awm/core/agent.py:130-167) and
vLLM's hermes parser (vLLM v0.19.0 vllm/tool_parsers/hermes_tool_parser.py:61-65). Used when
the server returns tool calls inside `content` (vLLM without --enable-auto-tool-choice:
vllm/entrypoints/openai/chat_completion/serving.py:1399-1403).
"""

from __future__ import annotations

import json
import re
from typing import Any

from workbench.llm.types import ToolCall

TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
THINK = re.compile(r"<think>.*?</think>", re.S)


def strip_think(text: str) -> str:
    return THINK.sub("", text).strip()


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw_arguments": raw}
        return value if isinstance(value, dict) else {"_raw_arguments": raw}
    return {}


def parse_tool_calls(content: str, id_prefix: str = "call") -> tuple[str, list[ToolCall]]:
    """Return (content without think/tool_call blocks, parsed calls)."""
    calls: list[ToolCall] = []
    for i, block in enumerate(TOOL_CALL.findall(content)):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            obj = obj[0]
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        calls.append(ToolCall(f"{id_prefix}_{i}", str(obj["name"]), parse_arguments(obj.get("arguments"))))
    text = strip_think(TOOL_CALL.sub("", content))
    return text, calls
