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
    from workbench.config import Settings
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


@app.command()
def verify(
    input_dir: Path = typer.Option(..., "--input", help="`awm agent` output dir (has trajectory.json)."),
    mode: str = typer.Option(
        "code", "--mode", help="code: the dataset's pure-code verifier, no LLM; sql: verifier + LLM judge."
    ),
    verifier: Path | None = typer.Option(
        None,
        "--verifier",
        help="Verifier file (default: gen_verifier.pure_code.jsonl / gen_verifier.jsonl in env.dataset_dir).",
    ),
    init_db: Path | None = typer.Option(None, "--init-db", help="Initial database (<input>/initial.db)."),
    final_db: Path | None = typer.Option(None, "--final-db", help="Final database (<input>/final.db)."),
    judge_model: str | None = typer.Option(
        None, "--judge-model", help="sql mode: judge model (default AWM_SYN_OVERRIDE_MODEL)."
    ),
) -> None:
    """Run `awm verify` once; no key reaches the verifier code (sql judge via the local proxy, ADR-025)."""
    from workbench.verify import VerifyError, run_verify

    try:
        summary = run_verify(
            get_settings(),
            input_dir,
            mode=mode,
            verifier=verifier,
            init_db=init_db,
            final_db=final_db,
            judge_model=judge_model,
        )
    except VerifyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print_json(data=summary)


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
    """Classify every tool of every scenario offline (name + route HTTP method) into a CSV for review.

    ``heuristic_risk`` is the name-only result before the HTTP-method floor (ADR-015), so the CSV
    also shows what the floor changed.
    """
    import csv

    from workbench.envs.catalog import build_catalog, iter_route_methods
    from workbench.gateway.policy import METHOD_FLOOR, PolicyConfig, classify

    settings = get_settings()
    policy = PolicyConfig.load(settings.gateway.policy_file)
    directory = dataset_dir or settings.env.dataset_dir
    methods = dict(iter_route_methods(directory))
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = heuristic_read_writes = final_read_writes = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "scenario",
                "tool",
                "http_method",
                "risk",
                "source",
                "reason",
                "requires_approval",
                "heuristic_risk",
            ]
        )
        for scenario in build_catalog(directory):
            for tool in scenario.tool_names:
                method = methods.get(scenario.name, {}).get(tool)
                c = classify(tool, "", policy, scenario.name, http_method=method)
                heuristic = classify(tool, "", policy, scenario.name)
                if method in METHOD_FLOOR:
                    heuristic_read_writes += heuristic.level == "read"
                    final_read_writes += c.level == "read"
                writer.writerow(
                    [
                        scenario.name,
                        tool,
                        method or "",
                        c.level,
                        c.source,
                        c.reason,
                        c.level in policy.require_approval,
                        heuristic.level,
                    ]
                )
                rows += 1
    console.print(
        f"wrote {rows} rows to {out} (offline: names and route methods; live sessions add descriptions)"
    )
    console.print(
        f"POST/PUT/PATCH/DELETE tools graded read: {heuristic_read_writes} by name alone, "
        f"{final_read_writes} with the HTTP-method floor"
    )


@serve_app.command("vllm-cmd")
def serve_vllm_cmd(
    profile: Path = typer.Option(Path("configs/serving/arctic-awm-4b.yaml"), "--profile"),
    args_only: bool = typer.Option(False, "--args-only", help="one argument per line (for scripts)"),
) -> None:
    """Print the vLLM command for a serving profile (running it needs a GPU: UNVERIFIED-LOCAL)."""
    import shlex

    from workbench.llm.serving import ServingProfile, vllm_command

    cmd = vllm_command(ServingProfile.load(profile))
    if args_only:
        typer.echo("\n".join(cmd))
    else:
        typer.echo(shlex.join(cmd))


@agent_app.command("run")
def agent_run(
    request: str,
    scenario: str = typer.Option(..., "--scenario"),
    dataset_dir: Path | None = typer.Option(None, "--dataset-dir"),
    approve: str = typer.Option("prompt", "--approve", help="prompt | auto | deny"),
    approver: str = typer.Option("cli-user", "--approver"),
) -> None:
    """Run the agent once on a fresh isolated session (mock LLM unless configured otherwise)."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from workbench.runtime import Runtime

    base = get_settings()
    settings = (
        base
        if dataset_dir is None
        else base.model_copy(update={"env": base.env.model_copy(update={"dataset_dir": dataset_dir})})
    )

    async def go() -> int:
        settings.agent.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(settings.agent.checkpoint_db)) as saver:
            rt = Runtime(settings, checkpointer=saver)
            try:
                s = await rt.create_session(scenario)
                console.print(f"session {s.session_id}: {len(s.tools)} tools from {s.env.url}")
                assert rt.runner is not None
                stream = rt.runner.run(s.thread_id, s.session_id, request)
                while True:
                    last: dict[str, Any] = {}
                    async for event in stream:
                        _print_event(event)
                        last = event
                    if last.get("type") != "approval_required":
                        break
                    ok = {"auto": True, "deny": False}.get(approve)
                    if ok is None:
                        ok = typer.confirm(f"approve {last['tool']} {last['arguments']}?")
                    decision = {"approved": ok, "approver": approver, "reason": "cli"}
                    stream = rt.runner.resume(s.thread_id, s.session_id, decision)
                console.print_json(data=await rt.envs.diff(s.session_id))
                return 0
            finally:
                await rt.aclose()

    raise typer.Exit(code=_run(go()))


def _print_event(e: dict[str, Any]) -> None:
    kind = e.get("type")
    if kind == "tool_call":
        console.print(f"[cyan]tool[/] {e['tool']} -> {e['status']} ({e['decision']})")
    elif kind in ("approval_required", "approval_granted", "approval_rejected", "terminated", "memory_saved"):
        console.print(f"[yellow]{kind}[/] {e.get('tool') or e.get('key') or e.get('reason')}")
    elif kind == "done":
        console.print(f"[green]answer[/] {e['final_answer']}")


def demo_settings() -> Settings:
    """Offline demo: hand-written mini scenario + scripted mock LLM (query -> write -> approve)."""
    from workbench.config import Settings

    root = Path("data/demo")
    return Settings(
        env={"dataset_dir": Path("tests/fixtures/awm_mini"), "runs_dir": root / "runs"},  # type: ignore[arg-type]
        gateway={"audit_path": root / "audit.jsonl"},  # type: ignore[arg-type]
        llm={  # type: ignore[arg-type]
            "backend": "mock_replay",
            "mock_fixture": Path("tests/fixtures/trajectories/demo_query_write_approve.jsonl"),
            "mock_reset_per_session": True,
        },
        agent={"checkpoint_db": root / "checkpoints.sqlite", "memory_db": root / "memory.sqlite"},  # type: ignore[arg-type]
    )


@api_app.command("serve")
def api_serve(
    demo: bool = typer.Option(False, "--demo", help="offline mock demo on the mini scenario"),
) -> None:
    """Run the HTTP API (+ gateway MCP at /gateway/mcp, UI at /ui/)."""
    import uvicorn

    from workbench.api.app import create_app

    settings = demo_settings() if demo else get_settings()
    if demo:
        console.print(
            f"[bold]demo mode[/]: open http://{settings.api.host}:{settings.api.port}/ui/ "
            "and pick scenario mini_e_commerce"
        )
    uvicorn.run(create_app(settings), host=settings.api.host, port=settings.api.port)


@synth_app.command("run")
def synth_run(
    scenarios: int = typer.Option(..., "--scenarios", help="number of scenarios to synthesize"),
    out: Path = typer.Option(..., "--out", help="run directory under data/synth/"),
    num_tasks: int = typer.Option(10, "--num-tasks"),
    verifier_mode: str = typer.Option("sql", "--verifier-mode"),
    execute: bool = typer.Option(False, "--execute", help="actually call the LLM API (default: dry-run)"),
    scenario_file: Path | None = typer.Option(
        None,
        "--scenario-file",
        help="hand-written gen_scenario.jsonl (local_ names): start at `gen task`, skip `gen scenario`",
    ),
) -> None:
    """Plan (default) or execute AWM's gen steps with checkpoints, LLM cache, ledger and validation."""
    import os

    from workbench.synth.ledger import Ledger, load_prices
    from workbench.synth.proxy import create_proxy_app
    from workbench.synth.runner import ProxyThread, SynthError, SynthInterrupted, SynthRunner

    settings = get_settings()
    try:
        runner = SynthRunner(
            settings,
            out,
            scenarios=scenarios,
            num_tasks=num_tasks,
            verifier_mode=verifier_mode,
            scenario_file=scenario_file,
        )
        if not execute:
            console.print_json(data=runner.describe())
            return
        s = settings.synth
        upstream = os.environ.get(s.upstream_base_url_env) or "https://api.openai.com/v1"
        currency, prices = load_prices(s.pricing_file)
        app = create_proxy_app(
            upstream_base_url=upstream,
            upstream_api_key=os.environ.get(s.upstream_api_key_env),
            cache_dir=out / "llm_cache",
            ledger=Ledger(out / "ledger.jsonl"),
            prices=prices,
            budget=s.budget,  # ADR-023
            currency=currency,
        )
        with ProxyThread(app, s.proxy_host, s.proxy_port) as base:
            result = runner.execute(proxy_base=base)
        console.print_json(data=result)
        for name, step in result["steps"].items():
            if step.get("status") == "done_with_failures":  # ADR-024
                console.print(
                    f"[yellow]step {name}: {step['failed_requests']} LLM request(s) ended in an upstream "
                    f"error ({step['failed_by_status']}); its output misses those parts[/yellow]"
                )
    except SynthInterrupted as exc:  # ADR-022: the step's processes are already stopped
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=130) from exc
    except SynthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@synth_app.command("validate")
def synth_validate(run_dir: Path) -> None:
    """Run `awm env reset_db` + `awm env check_all` on a synthesized run and write the report."""
    from workbench.subprocess_env import generated_code_env
    from workbench.synth.runner import default_command_runner, validate_run

    # Both run generated code and call no LLM: allowlisted variables only (ADR-019).
    report = validate_run(run_dir, default_command_runner, generated_code_env())
    console.print_json(data=report.as_dict())


@train_app.command("preflight")
def train_preflight(profile: Path | None = typer.Option(None, "--profile")) -> None:
    """Check GPU, CUDA/torch/vLLM/veRL, config keys and data for the smoke profile."""
    from workbench.doctor import exit_code
    from workbench.train.preflight import run_preflight
    from workbench.train.profile import TrainProfile

    s = get_settings()
    results = run_preflight(
        TrainProfile.load(profile or s.train.smoke_config), s.upstream.agentfly_dir, s.train.project_dir
    )
    table = Table(title="train preflight (smoke)")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    for r in results:
        table.add_row(r.name, r.status, r.detail)
    console.print(table)
    raise typer.Exit(code=exit_code(results))


@train_app.command("launch")
def train_launch(
    profile: Path | None = typer.Option(None, "--profile"),
    execute: bool = typer.Option(False, "--execute", help="run it (needs a GPU); default prints the plan"),
) -> None:
    """Launch the SMOKE profile in the separate train env (subprocess). Output is marked NO_RESULTS."""
    from workbench.train.launch import LaunchError, launch
    from workbench.train.preflight import run_preflight
    from workbench.train.profile import TrainProfile

    s = get_settings()
    prof = TrainProfile.load(profile or s.train.smoke_config)
    checks = run_preflight(prof, s.upstream.agentfly_dir, s.train.project_dir)
    try:
        console.print_json(data=launch(prof, s.train.out_dir, s.train.project_dir, checks, execute=execute))
    except LaunchError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@results_app.command("check")
def results_check() -> None:
    """Validate results/registry.yaml and scan README + docs for unregistered numbers."""
    from workbench.results.check_numbers import default_targets, load_whitelist, scan
    from workbench.results.registry import RegistryError, load_registry

    root = Path.cwd()
    try:
        reg = load_registry(root / "results" / "registry.yaml")
    except RegistryError as exc:
        console.print(f"[red]registry invalid: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    files = default_targets(root)
    findings = scan(files, reg, load_whitelist(root / "configs" / "number_whitelist.yaml"), root)
    for f in findings:
        console.print(str(f), style="red", markup=False, highlight=False)
    unverified = sum(1 for e in reg.entries if not e.verified)
    console.print(
        f"registry: {len(reg.entries)} entries ({unverified} unverified); scanned {len(files)} files; "
        f"{len(findings)} finding(s)"
    )
    raise typer.Exit(code=1 if findings else 0)


@results_app.command("render")
def results_render(out: Path = typer.Option(Path("docs/RESULTS.md"), "--out")) -> None:
    """Regenerate docs/RESULTS.md from results/registry.yaml."""
    from workbench.results.registry import load_registry, render_results_md

    out.write_text(render_results_md(load_registry(Path("results/registry.yaml"))), encoding="utf-8")
    console.print(f"wrote {out}")


if __name__ == "__main__":
    app()
