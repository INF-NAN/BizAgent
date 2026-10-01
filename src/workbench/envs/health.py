"""MCP health check equivalent to `awm env check`.

AWM's check (third_party/agent-world-model/awm/tools.py:141-199) connects over
streamable_http, runs list_tools under a timeout and treats "at least one tool" as running.
We do the same with the MCP Python SDK client (mcp 1.26.0:
mcp/client/streamable_http.py `streamablehttp_client`, mcp/client/session.py `ClientSession`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    tools: list[str] = field(default_factory=list)
    error: str | None = None


async def check_mcp(url: str, timeout_s: float) -> HealthResult:
    try:
        with anyio.fail_after(timeout_s):
            async with (
                streamablehttp_client(url, timeout=timeout_s) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                listed = await session.list_tools()
    except TimeoutError:
        return HealthResult(ok=False, error=f"timeout after {timeout_s}s")
    except BaseException as exc:  # connection errors surface as ExceptionGroup from anyio task groups
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        return HealthResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    names = [t.name for t in listed.tools]
    if not names:
        return HealthResult(ok=False, error="no tools available from MCP server")
    return HealthResult(ok=True, tools=names)
