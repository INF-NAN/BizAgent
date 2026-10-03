"""`workbench lab ...`: batch experiments on the official dataset (docs/EXPERIMENTS.md)."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import typer
from rich.console import Console

from workbench.config import get_settings

lab_app = typer.Typer(help="Batch experiments on the official AWM scenarios.", no_args_is_help=True)
console = Console()
LAB_DIR = Path("data/lab")


@lab_app.command("split")
def lab_split(
    dataset_dir: Path | None = typer.Option(None, "--dataset-dir", help="Default: env.dataset_dir."),
    lab_dir: Path = typer.Option(LAB_DIR, "--lab-dir", envvar="WORKBENCH_LAB_DIR"),
    seed: int = typer.Option(20260601, "--seed"),
    test_scenarios: int = typer.Option(200, "--test-scenarios"),
    val_scenarios: int = typer.Option(100, "--val-scenarios"),
    test_tasks: int = typer.Option(600, "--test-tasks"),
    val_tasks: int = typer.Option(200, "--val-tasks"),
    train_tasks: int = typer.Option(1500, "--train-tasks"),
    train_extra_tasks: int = typer.Option(
        1500, "--train-extra-tasks", help="Reserve train-scenario tasks for more teacher data if needed."
    ),
    inject_tasks: int = typer.Option(
        200, "--inject-tasks", help="Tasks from test scenarios that have a destructive tool (0: none)."
    ),
) -> None:
    """Split scenarios into train / val / test and sample tasks from each (deterministic)."""
    from workbench.lab.splits import (
        destructive_scenarios,
        load_tasks,
        load_verifier_index,
        make_split,
        write_split,
    )

    settings = get_settings()
    ds = dataset_dir or settings.env.dataset_dir
    tasks = load_tasks(ds)
    verifiers = load_verifier_index(ds)
    split = make_split(
        tasks,
        verifiers,
        seed=seed,
        scenario_counts={"test": test_scenarios, "val": val_scenarios},
        task_counts={
            "test": test_tasks,
            "val": val_tasks,
            "train": train_tasks,
            "train_extra": train_extra_tasks,
            "inject": inject_tasks,
        },
        inject_scenarios=destructive_scenarios(ds, settings.gateway.policy_file) if inject_tasks else None,
    )
    path = write_split(lab_dir, split, verifiers, seed)
    for name, ts in split.items():
        console.print(f"{name}: {len(ts)} tasks from {len({t.scenario for t in ts})} scenarios")
    console.print(f"wrote {path}")


@lab_app.command("eval")
def lab_eval(
    split: str = typer.Option(..., "--split", help="train | val | test"),
    tag: str = typer.Option(..., "--tag", help="Run name; results go to <lab-dir>/runs/<tag>/."),
    lab_dir: Path = typer.Option(LAB_DIR, "--lab-dir", envvar="WORKBENCH_LAB_DIR"),
    backend: str = typer.Option(
        "vllm", "--backend", help="vllm | openai_compat | mock_replay | null (never answers: verifier floor)"
    ),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    api_key_env: str | None = typer.Option(None, "--api-key-env"),
    temperature: float = typer.Option(0.0, "--temperature"),
    enable_thinking: bool = typer.Option(False, "--enable-thinking/--no-thinking", help="vllm backend only"),
    max_tokens: int = typer.Option(4096, "--max-tokens"),
    samples: int = typer.Option(1, "--samples", help="Episodes per task (for pass@k)."),
    concurrency: int = typer.Option(16, "--concurrency"),
    approver: str = typer.Option("auto", "--approver", help="auto | preview-guard"),
    inject: bool = typer.Option(False, "--inject", help="Append an injected instruction to a read result."),
    policy_file: Path = typer.Option(Path("configs/lab/eval_policy.yaml"), "--policy-file"),
    port_min: int = typer.Option(20000, "--port-min", help="Give concurrent runs separate port ranges."),
    offset: int = typer.Option(0, "--offset", help="Skip the first N tasks of the split."),
    limit: int | None = typer.Option(None, "--limit", help="Only the first N tasks (after --offset)."),
    wall_clock_s: float = typer.Option(900.0, "--wall-clock", help="Per-episode agent wall clock."),
    keep_envs: bool = typer.Option(False, "--keep-envs", help="Keep each session's databases and trace."),
    min_ok: float = typer.Option(
        0.0, "--min-ok", help="Exit with code 3 if fewer than this share of episodes ran without an error."
    ),
) -> None:
    """Run the agent on a split, verify every episode, append to results.jsonl (resumable)."""
    from workbench.lab.episodes import APPROVERS, EvalConfig, NullBackend, lab_settings, run_eval
    from workbench.lab.report import run_summary
    from workbench.lab.splits import read_split, read_verifiers

    if approver not in APPROVERS:
        raise typer.BadParameter(f"--approver must be one of {', '.join(APPROVERS)}")
    base = get_settings()
    llm = base.llm.model_copy(
        update={
            "backend": "mock_replay" if backend == "null" else backend,
            "temperature": temperature,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "read_timeout_s": 300.0,
            "total_timeout_s": 600.0,
            **({"base_url": base_url} if base_url else {}),
            **({"model": model} if model else {}),
            **({"api_key_env": api_key_env} if api_key_env else {}),
        }
    )
    approval = base.approval.model_copy(update={"policy_file": policy_file})
    run_root = lab_dir / "runs" / tag
    settings = lab_settings(
        base.model_copy(update={"llm": llm, "approval": approval}),
        run_root,
        concurrency=concurrency,
        port_min=port_min,
        wall_clock_s=wall_clock_s,
    )
    tasks = read_split(lab_dir, split)[offset:]
    if limit is not None:
        tasks = tasks[:limit]
    cfg = EvalConfig(
        tag=tag,
        out_dir=lab_dir,
        samples=samples,
        concurrency=concurrency,
        approver=approver,
        inject=inject,
        model_label="null" if backend == "null" else (model or llm.model),
        keep_envs=keep_envs,
    )
    meta = {
        "tag": tag,
        "split": split,
        "tasks": len(tasks),
        "samples": samples,
        "backend": backend,
        "model": llm.model,
        "temperature": temperature,
        "enable_thinking": enable_thinking,
        "approver": approver,
        "inject": inject,
        "policy_file": str(policy_file),
    }
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    extra = {"backend": NullBackend()} if backend == "null" else {}

    async def main() -> Path:
        # SIGTERM / SIGINT cancel the run, so every open environment is closed on the way out
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        return await run_eval(settings, tasks, cfg, read_verifiers(lab_dir), **extra)

    try:
        path = asyncio.run(main())
    except asyncio.CancelledError:
        console.print(f"[yellow]{tag}: stopped; finished episodes are kept and the run resumes from them[/]")
        raise typer.Exit(code=130) from None
    from workbench.lab.episodes import load_results

    s = run_summary(load_results(path))
    console.print(f"{tag}: {s['success']}/{s['n']} verified complete; status {s['status']}")
    ok_share = s["status"].get("ok", 0) / max(1, s["episodes"])
    if ok_share < min_ok:
        console.print(f"[red]only {ok_share:.0%} of episodes ran without an error (--min-ok {min_ok})[/]")
        raise typer.Exit(code=3)


@lab_app.command("sft-data")
def lab_sft_data(
    runs: list[str] = typer.Option(..., "--run", help="Run tag to read (repeatable, in priority order)."),
    out: Path = typer.Option(..., "--out"),
    lab_dir: Path = typer.Option(LAB_DIR, "--lab-dir", envvar="WORKBENCH_LAB_DIR"),
    include_failed: bool = typer.Option(False, "--include-failed", help="Unfiltered ablation."),
    max_per_task: int | None = typer.Option(None, "--max-per-task"),
    max_episodes: int | None = typer.Option(
        None, "--max-episodes", help="Seeded random subset of this many episodes."
    ),
    match_stats: Path | None = typer.Option(
        None, "--match", help="Use as many episodes as the stats file of another variant reports."
    ),
    exclude_trivial: list[str] = typer.Option(
        [], "--exclude-trivial", help="Null-agent run tag; tasks it passed are left out (repeatable)."
    ),
) -> None:
    """Build chat-format SFT samples from recorded episodes."""
    from workbench.lab.episodes import trivial_tasks
    from workbench.lab.sft_data import build

    if match_stats is not None:
        max_episodes = int(json.loads(match_stats.read_text(encoding="utf-8")).get("used_episodes", 0))
    summary = build(
        [lab_dir / "runs" / r for r in runs],
        out,
        include_failed=include_failed,
        max_per_task=max_per_task,
        max_episodes=max_episodes,
        exclude_tasks=trivial_tasks([lab_dir / "runs" / t for t in exclude_trivial]),
    )
    console.print_json(data=summary)


@lab_app.command("select")
def lab_select(
    variant: str = typer.Option(..., "--variant"),
    lab_dir: Path = typer.Option(LAB_DIR, "--lab-dir", envvar="WORKBENCH_LAB_DIR"),
    trivial_run: str = typer.Option(
        "null-val", "--trivial-run", help="Null-agent run on val; tasks it passed do not count."
    ),
) -> None:
    """Pick the checkpoint with the best val success rate on non-trivial tasks (ties: the earlier one)."""
    from workbench.lab.episodes import load_results, trivial_tasks
    from workbench.lab.metrics import rate

    trivial = trivial_tasks([lab_dir / "runs" / trivial_run])
    prefix = f"sft-{variant}-val-"
    scores = []
    for d in sorted((lab_dir / "runs").glob(f"{prefix}*")):
        rows = [r for r in load_results(d / "results.jsonl") if r.get("sample", 0) == 0]
        kept = [r for r in rows if f"{r['scenario']}#{r['task_id']}" not in trivial]
        s = rate(kept)
        step = int(d.name.rsplit("-", 1)[-1])
        scores.append((-s["rate"], step, d.name[len(prefix) :], s, rate(rows)))
    if not scores:
        console.print(f"[red]no {prefix}* runs[/]")
        raise typer.Exit(code=1)
    scores.sort(key=lambda x: (x[0], x[1]))
    _, _, ck, s, all_tasks = scores[0]
    out = lab_dir / "sft" / variant / "selected.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "checkpoint": ck,
        "val_rate": s["rate"],
        "val_success": s["success"],
        "val_n": s["n"],
        "val_trivial_excluded": len(trivial),
        "val_rate_all_tasks": all_tasks["rate"],
    }
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    typer.echo(ck)


@lab_app.command("bench")
def lab_bench(
    source: str = typer.Option("base-test", "--source", help="Run tag whose recorded calls are replayed."),
    label: str = typer.Option(..., "--label"),
    base_url: str = typer.Option("http://127.0.0.1:8000/v1", "--base-url"),
    model: str = typer.Option(..., "--model"),
    concurrency: str = typer.Option("1,8,32", "--concurrency"),
    max_tokens: int = typer.Option(256, "--max-tokens"),
    lab_dir: Path = typer.Option(LAB_DIR, "--lab-dir", envvar="WORKBENCH_LAB_DIR"),
) -> None:
    """Replay recorded agent requests at several concurrency levels; write <lab-dir>/bench/<label>.json."""
    from workbench.lab.serving_bench import bench

    levels = [int(x) for x in concurrency.split(",") if x.strip()]
    doc = asyncio.run(
        bench(
            base_url,
            model,
            lab_dir / "runs" / source,
            label,
            levels,
            lab_dir / "bench" / f"{label}.json",
            max_tokens=max_tokens,
        )
    )
    for r in doc["results"]:
        console.print(
            f"{label} c={r['concurrency']}: ttft p50 {r['ttft_ms_p50']} ms, {r['output_tok_s']} tok/s, "
            f"prefix hit {r['prefix_cache_hit_rate']}, errors {r['errors']}"
        )


@lab_app.command("report")
def lab_report(lab_dir: Path = typer.Option(LAB_DIR, "--lab-dir", envvar="WORKBENCH_LAB_DIR")) -> None:
    """Write <lab-dir>/summary.json and <lab-dir>/REPORT.md from everything that has run."""
    from workbench.lab.report import write_report

    console.print(f"wrote {write_report(lab_dir)}")
