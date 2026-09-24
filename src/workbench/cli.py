"""`workbench` command-line interface (typer)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from workbench.config import get_settings

if TYPE_CHECKING:
    from workbench.envs.service import RemoteEnvService


app = typer.Typer(
    help="BizAgent Workbench: engineering layer around AWM MCP environments.", no_args_is_help=True
)
env_app = typer.Typer(help="Manage isolated AWM environment sessions.", no_args_is_help=True)
gateway_app = typer.Typer(help="MCP gateway (routing, policy, audit).", no_args_is_help=True)
serve_app = typer.Typer(help="Model serving helpers (vLLM).", no_args_is_help=True)
agent_app = typer.Typer(help="LangGraph application agent.", no_args_is_help=True)
api_app = typer.Typer(help="HTTP API + UI.", no_args_is_help=True)
synth_app = typer.Typer(
    help="AWM synthesis pipeline orchestration (dry-run by default).", no_args_is_help=True
)
train_app = typer.Typer(
    help="Training launcher (subprocess into the separate train env).", no_args_is_help=True
)
results_app = typer.Typer(help="Paper-number registry tools.", no_args_is_help=True)

for sub, name in [
    (env_app, "env"),
    (gateway_app, "gateway"),
    (serve_app, "serve"),
    (agent_app, "agent"),
    (api_app, "api"),
    (synth_app, "synth"),
    (train_app, "train"),
    (results_app, "results"),
]:
    app.add_typer(sub, name=name)

console = Console()


def _not_implemented(phase: int) -> None:
    console.print(f"[yellow]not implemented yet (Phase {phase})[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def doctor() -> None:
    """Check python, submodules, dataset, ports, GPU, env vars and LLM backend."""
    from workbench.doctor import exit_code, run_checks

    results = run_checks(get_settings())
    table = Table(title="workbench doctor")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for r in results:
        table.add_row(r.name, f"[{colors[r.status]}]{r.status}[/]", r.detail)
    console.print(table)
    raise typer.Exit(code=exit_code(results))


def _env_client() -> RemoteEnvService:
    from workbench.envs.service import RemoteEnvService

    s = get_settings().env
    return RemoteEnvService(s.manager_url or f"http://{s.manager_host}:{s.manager_port}")


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@env_app.command("serve")
def env_serve() -> None:
    """Run the env-manager control plane (owns the AWM server subprocesses)."""
    import uvicorn

    from workbench.envs.http import create_env_app

    s = get_settings().env
    uvicorn.run(create_env_app(s), host=s.manager_host, port=s.manager_port)


@env_app.command("up")
def env_up(scenario: str, session_id: str | None = typer.Option(None, "--session-id")) -> None:
    """Start an isolated environment session (via `workbench env serve`)."""

    async def go() -> None:
        client = _env_client()
        try:
            info = await client.start(scenario, session_id)
        finally:
            await client.close()
        console.print_json(data=info.as_dict())

    _run(go())


@env_app.command("down")
def env_down(session_id: str) -> None:
    """Stop an environment session."""

    async def go() -> None:
        client = _env_client()
        try:
            await client.stop(session_id)
        finally:
            await client.close()
        console.print(f"stopped {session_id}")

    _run(go())


@env_app.command("ls")
def env_ls() -> None:
    """List environment sessions."""

    async def go() -> None:
        client = _env_client()
        try:
            envs = await client.list_envs()
        finally:
            await client.close()
        table = Table(title="environments")
        for col in ("session", "scenario", "state", "url", "tools", "error"):
            table.add_column(col)
        for e in envs:
            table.add_row(e.session_id, e.scenario, e.state, e.url, str(len(e.tools)), e.error or "")
        console.print(table)

    _run(go())


@env_app.command("logs")
def env_logs(session_id: str, lines: int = typer.Option(200, "--lines", "-n")) -> None:
    """Show the tail of a session's server log."""

    async def go() -> None:
        client = _env_client()
        try:
            console.print(await client.logs(session_id, lines), markup=False, highlight=False)
        finally:
            await client.close()

    _run(go())


@env_app.command("diff")
def env_diff(session_id: str, against: str = typer.Option("initial", "--against")) -> None:
    """Show table-level row-count and primary-key diff against the initial DB or a snapshot."""

    async def go() -> None:
        client = _env_client()
        try:
            console.print_json(data=await client.diff(session_id, against))
        finally:
            await client.close()

    _run(go())


@env_app.command("snapshot")
def env_snapshot(session_id: str, name: str) -> None:
    """Snapshot a session DB."""
    client = _env_client()
    _run(client.snapshot(session_id, name))
    console.print(f"snapshot {name} saved")


@env_app.command("restore")
def env_restore(session_id: str, name: str) -> None:
    """Restore a session DB from `initial` or a named snapshot."""
    client = _env_client()
    _run(client.restore(session_id, name))
    console.print(f"restored {name}")


@env_app.command("search")
def env_search(keyword: str, dataset_dir: Path | None = typer.Option(None, "--dataset-dir")) -> None:
    """Search the scenario catalog (name or tool name) built from the dataset directory."""
    from workbench.envs.catalog import build_catalog, search

    directory = dataset_dir or get_settings().env.dataset_dir
    rows = search(build_catalog(directory), keyword)
    table = Table(title=f"scenarios matching {keyword!r} in {directory}")
    for col in ("scenario", "tools", "tasks", "tables"):
        table.add_column(col)
    for r in rows:
        table.add_row(r.name, str(r.tools), str(r.tasks), str(r.tables))
    console.print(table)
    if not rows:
        raise typer.Exit(code=1)


@gateway_app.command("serve")
def gateway_serve() -> None:
    """Run the MCP gateway standalone (MCP at /mcp, admin endpoints under /admin)."""
    import uvicorn

    from workbench.gateway.core import Gateway
    from workbench.gateway.server import create_gateway_app

    s = get_settings().gateway
    uvicorn.run(create_gateway_app(Gateway(s)), host=s.host, port=s.port)


@gateway_app.command("export-risk")
def gateway_export_risk(
    dataset_dir: Path | None = typer.Option(None, "--dataset-dir"),
    out: Path = typer.Option(Path("data/risk_table.csv"), "--out"),
) -> None:
    """Classify every tool of every scenario (offline, by tool name) into a CSV for human review."""
    import csv

    from workbench.envs.catalog import build_catalog
    from workbench.gateway.policy import PolicyConfig, classify

    settings = get_settings()
    policy = PolicyConfig.load(settings.gateway.policy_file)
    directory = dataset_dir or settings.env.dataset_dir
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scenario", "tool", "risk", "source", "reason", "requires_approval"])
        for scenario in build_catalog(directory):
            for tool in scenario.tool_names:
                c = classify(tool, "", policy, scenario.name)
                writer.writerow(
                    [scenario.name, tool, c.level, c.source, c.reason, c.level in policy.require_approval]
                )
                rows += 1
    console.print(f"wrote {rows} rows to {out} (offline: names only; live sessions add descriptions)")


@serve_app.command("vllm-cmd")
def serve_vllm_cmd() -> None:
    """Print the vLLM command for the configured model."""
    _not_implemented(4)


@agent_app.command("run")
def agent_run() -> None:
    """Run the agent once."""
    _not_implemented(5)


@api_app.command("serve")
def api_serve() -> None:
    """Run the HTTP API."""
    _not_implemented(6)


@synth_app.command("run")
def synth_run() -> None:
    """Plan or execute an AWM synthesis run."""
    _not_implemented(7)


@train_app.command("preflight")
def train_preflight() -> None:
    """Check the training environment."""
    _not_implemented(7)


@results_app.command("check")
def results_check() -> None:
    """Validate the registry and scan docs for unregistered numbers."""
    _not_implemented(8)


if __name__ == "__main__":
    app()
