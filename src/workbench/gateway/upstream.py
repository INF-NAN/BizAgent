"""MCP client for the per-session AWM servers (MCP SDK 1.26.0, see docs/RECON.md §10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class UpstreamTimeoutError(TimeoutError):
    """The upstream MCP call exceeded its deadline."""


class UpstreamTransportError(ConnectionError):
    """The upstream MCP server could not be reached or the session broke."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class Upstream(Protocol):
    async def list_tools(self, url: str, timeout_s: float) -> list[ToolSpec]: ...
    async def call_tool(
        self, url: str, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> tuple[bool, str]: ...


class McpUpstream:
    async def list_tools(self, url: str, timeout_s: float) -> list[ToolSpec]:
        try:
            with anyio.fail_after(timeout_s):
                async with (
                    streamablehttp_client(url, timeout=timeout_s) as (r, w, _),
                    ClientSession(r, w) as s,
                ):
                    await s.initialize()
                    listed = await s.list_tools()
        except TimeoutError as exc:
            raise UpstreamTimeoutError(f"list_tools timed out after {timeout_s}s") from exc
        except Exception as exc:  # ExceptionGroup from anyio task groups on connection errors
            raise UpstreamTransportError(f"{type(exc).__name__}: {exc}") from exc
        return [ToolSpec(t.name, t.description or "", dict(t.inputSchema or {})) for t in listed.tools]

    async def call_tool(
        self, url: str, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> tuple[bool, str]:
        try:
            with anyio.fail_after(timeout_s):
                async with (
                    streamablehttp_client(url, timeout=timeout_s) as (r, w, _),
                    ClientSession(r, w) as s,
                ):
                    await s.initialize()
                    result = await s.call_tool(name, arguments)
        except TimeoutError as exc:
            raise UpstreamTimeoutError(f"{name} timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise UpstreamTransportError(f"{type(exc).__name__}: {exc}") from exc
        text = "\n".join(getattr(c, "text", str(c)) for c in result.content)
        return bool(result.isError), text
