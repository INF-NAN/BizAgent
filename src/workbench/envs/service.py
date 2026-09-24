"""EnvService: one interface over an in-process EnvManager or a remote env-manager service.

The API and gateway only depend on this protocol, so switching between "app manages its
own subprocesses" and "separate env-manager container" (docker compose) is configuration
(``env.manager_url``), not code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import httpx

from workbench.envs.manager import EnvHandle, EnvManager, EnvNotFoundError


@dataclass(frozen=True)
class EnvInfo:
    session_id: str
    scenario: str
    url: str
    state: str
    port: int
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    tool_methods: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_handle(cls, h: EnvHandle) -> EnvInfo:
        return cls(
            h.session_id, h.scenario, h.url, h.state, h.port, list(h.tools), h.error, dict(h.tool_methods)
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EnvService(Protocol):
    async def start(self, scenario: str, session_id: str | None = None) -> EnvInfo: ...
    async def stop(self, session_id: str) -> None: ...
    async def info(self, session_id: str) -> EnvInfo: ...
    async def list_envs(self) -> list[EnvInfo]: ...
    async def logs(self, session_id: str, lines: int = 200) -> str: ...
    async def diff(self, session_id: str, against: str = "initial") -> dict[str, Any]: ...
    async def snapshot(self, session_id: str, name: str) -> None: ...
    async def restore(self, session_id: str, name: str) -> None: ...
    async def touch(self, session_id: str) -> None: ...
    async def close(self) -> None: ...


class LocalEnvService:
    def __init__(self, manager: EnvManager) -> None:
        self.manager = manager

    async def start(self, scenario: str, session_id: str | None = None) -> EnvInfo:
        return EnvInfo.from_handle(await self.manager.start(scenario, session_id))

    async def stop(self, session_id: str) -> None:
        await self.manager.stop(session_id)

    async def info(self, session_id: str) -> EnvInfo:
        self.manager.refresh()
        return EnvInfo.from_handle(self.manager.get(session_id))

    async def list_envs(self) -> list[EnvInfo]:
        return [EnvInfo.from_handle(h) for h in self.manager.list_envs()]

    async def logs(self, session_id: str, lines: int = 200) -> str:
        return self.manager.logs(session_id, lines)

    async def diff(self, session_id: str, against: str = "initial") -> dict[str, Any]:
        return self.manager.diff(session_id, against).as_dict()

    async def snapshot(self, session_id: str, name: str) -> None:
        self.manager.snapshot(session_id, name)

    async def restore(self, session_id: str, name: str) -> None:
        self.manager.restore(session_id, name)

    async def touch(self, session_id: str) -> None:
        self.manager.touch(session_id)

    async def close(self) -> None:
        await self.manager.stop_all()


class RemoteEnvService:
    """HTTP client for `workbench env serve` (see workbench.envs.http)."""

    def __init__(
        self, base_url: str, timeout_s: float = 120.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._client.request(method, path, **kw)
        if resp.status_code == 404:
            raise EnvNotFoundError(path)
        resp.raise_for_status()
        return resp.json()

    async def start(self, scenario: str, session_id: str | None = None) -> EnvInfo:
        return EnvInfo(
            **await self._call("POST", "/envs", json={"scenario": scenario, "session_id": session_id})
        )

    async def stop(self, session_id: str) -> None:
        await self._call("DELETE", f"/envs/{session_id}")

    async def info(self, session_id: str) -> EnvInfo:
        return EnvInfo(**await self._call("GET", f"/envs/{session_id}"))

    async def list_envs(self) -> list[EnvInfo]:
        return [EnvInfo(**e) for e in await self._call("GET", "/envs")]

    async def logs(self, session_id: str, lines: int = 200) -> str:
        return str((await self._call("GET", f"/envs/{session_id}/logs", params={"lines": lines}))["logs"])

    async def diff(self, session_id: str, against: str = "initial") -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "GET", f"/envs/{session_id}/diff", params={"against": against}
        )
        return result

    async def snapshot(self, session_id: str, name: str) -> None:
        await self._call("POST", f"/envs/{session_id}/snapshots", json={"name": name})

    async def restore(self, session_id: str, name: str) -> None:
        await self._call("POST", f"/envs/{session_id}/restore", json={"name": name})

    async def touch(self, session_id: str) -> None:
        await self._call("POST", f"/envs/{session_id}/touch")

    async def close(self) -> None:
        await self._client.aclose()
