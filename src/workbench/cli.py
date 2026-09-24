"""`workbench` command-line interface (typer)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from workbench.config import get_settings

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


@env_app.command("ls")
def env_ls() -> None:
    """List running environment sessions."""
    _not_implemented(2)


@gateway_app.command("serve")
def gateway_serve() -> None:
    """Run the MCP gateway server."""
    _not_implemented(3)


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
