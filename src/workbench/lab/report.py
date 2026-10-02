"""Collect every run under the lab directory into summary.json and REPORT.md.

Run tags follow scripts/lab/run_all.sh: base-test, base-val, base-passk, teacher-train,
teacher-test, student-train, sft-<variant>-val-<checkpoint>, sft-<variant>-test, inj-a, inj-b,
inj-c, inj-sft-a. A run that is missing is left out; nothing is filled in. Every number in
REPORT.md is computed here from results.jsonl,
the serving benchmark files, the SFT training metadata and the risk-model output.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from workbench.lab.episodes import load_results
from workbench.lab.metrics import (
    GUARD_REASONS,
    failure_category,
    mcnemar_exact,
    paired_bootstrap,
    pass_at_k,
    rate,
    wilson,
)

INJECTION = {
    "inj-a": "A: base, approve everything",
    "inj-b": "B: base, policy denies destructive tools",
    "inj-c": "C: base, approve only when the preview removes no rows",
    "inj-sft-a": "A: selected SFT model, approve everything",
}
SFT_VARIANTS = {
    "teacher": "teacher distillation (verified teacher episodes)",
    "rft": "teacher + student rejection sampling (verified episodes of both)",
    "unfiltered": "ablation: every finished teacher episode, verified or not",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def run_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok"]
    first = [r for r in rows if r.get("sample", 0) == 0]
    n = max(1, len(ok))
    return {
        **rate(first),
        "episodes": len(rows),
        "status": dict(Counter(r.get("status") for r in rows)),
        "mean_steps": sum(r.get("steps", 0) for r in ok) / n,
        "mean_tool_calls": sum(r.get("tool_calls", 0) for r in ok) / n,
        "mean_prompt_tokens": sum(r.get("prompt_tokens", 0) for r in ok) / n,
        "mean_completion_tokens": sum(r.get("completion_tokens", 0) for r in ok) / n,
        "mean_wall_s": sum(r.get("wall_s", 0) for r in ok) / n,
        "failures": dict(Counter(failure_category(r) for r in first if not r.get("success")).most_common()),
    }


def by_task(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        out[task_key(r)].append(1 if r.get("success") else 0)
    return out


def passk(rows: list[dict[str, Any]], ks: tuple[int, ...] = (1, 2, 4)) -> dict[str, Any]:
    tasks = by_task(rows)
    if not tasks:
        return {}
    n = min(len(v) for v in tasks.values())
    out: dict[str, Any] = {"tasks": len(tasks), "samples_per_task": n}
    for k in ks:
        if k <= n:
            out[f"pass@{k}"] = sum(pass_at_k(n, sum(v[:n]), k) for v in tasks.values()) / len(tasks)
    return out


def natural_key(name: str) -> list[Any]:
    """sft-x-val-ckpt-3 before sft-x-val-ckpt-12."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def task_key(r: dict[str, Any]) -> str:
    return f"{r['scenario']}#{r['task_id']}"


def paired(
    a_rows: list[dict[str, Any]], b_rows: list[dict[str, Any]], exclude: set[str] | None = None
) -> dict[str, Any]:
    """Paired comparison on the tasks both runs have; ``exclude`` drops tasks (e.g. trivial ones)."""
    a = {k: v[0] for k, v in by_task([r for r in a_rows if r.get("sample", 0) == 0]).items()}
    b = {k: v[0] for k, v in by_task([r for r in b_rows if r.get("sample", 0) == 0]).items()}
    keys = sorted((set(a) & set(b)) - (exclude or set()))
    if not keys:
        return {}
    xa, xb = [a[k] for k in keys], [b[k] for k in keys]
    only_a = sum(1 for x, y in zip(xa, xb, strict=True) if x and not y)
    only_b = sum(1 for x, y in zip(xa, xb, strict=True) if y and not x)
    mean, lo, hi = paired_bootstrap(xa, xb)
    return {
        "tasks": len(keys),
        "a_success": sum(xa),
        "b_success": sum(xb),
        "only_a": only_a,
        "only_b": only_b,
        "mcnemar_p": mcnemar_exact(only_a, only_b),
        "diff": mean,
        "diff_ci95": [lo, hi],
    }


def process(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How the episodes went, verifier aside: these respond to training more steadily than success."""
    ok = [r for r in rows if r.get("status") == "ok" and r.get("sample", 0) == 0]
    n = max(1, len(ok))

    def share(pred: Any) -> float:
        return sum(1 for r in ok if pred(r)) / n

    def reason(r: dict[str, Any]) -> Any:
        return (r.get("termination") or {}).get("reason")

    return {
        "episodes": len(ok),
        "mean_steps": sum(r.get("steps", 0) for r in ok) / n,
        "mean_tool_calls": sum(r.get("tool_calls", 0) for r in ok) / n,
        "tool_errors_per_episode": sum(r.get("tool_errors", 0) for r in ok) / n,
        "share_with_tool_error": share(lambda r: r.get("tool_errors", 0) > 0),
        "share_plan_invalid": share(lambda r: reason(r) == "plan_invalid"),
        "share_llm_error": share(lambda r: reason(r) == "llm_error"),
        "share_guard_stop": share(lambda r: reason(r) in GUARD_REASONS),
        "share_no_tool_call": share(lambda r: r.get("tool_calls", 0) == 0),
        "mean_prompt_tokens": sum(r.get("prompt_tokens", 0) for r in ok) / n,
        "mean_completion_tokens": sum(r.get("completion_tokens", 0) for r in ok) / n,
    }


def injection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    armed = [r for r in rows if r.get("status") == "ok" and r.get("injection")]
    n = len(armed)
    att = sum(1 for r in armed if r.get("attack_attempted"))
    exe = sum(1 for r in armed if r.get("attack_executed"))
    return {
        "episodes": n,
        "attempted": att,
        "executed": exe,
        "attempt_rate": att / n if n else 0.0,
        "executed_rate": exe / n if n else 0.0,
        "executed_ci95": list(wilson(exe, n)),
        "utility": rate(armed),
    }


def collect(lab: Path) -> dict[str, Any]:
    runs_dir = lab / "runs"
    dirs = sorted((p for p in runs_dir.glob("*") if p.is_dir()), key=lambda p: natural_key(p.name))
    runs = {p.name: load_results(p / "results.jsonl") for p in dirs}
    splits = _json(lab / "splits.json") or {}
    doc: dict[str, Any] = {
        "splits": {
            k: {"tasks": len(v), "scenarios": len(splits.get("scenarios", {}).get(k, []))}
            for k, v in (splits.get("tasks") or {}).items()
        },
        "runs": {tag: run_summary(rows) for tag, rows in runs.items() if rows},
    }
    # tasks the verifier passes when the agent does nothing (NullBackend run on test)
    trivial = {task_key(r) for r in runs.get("null-test", []) if r.get("success")}
    if runs.get("null-test"):
        null_n = len({task_key(r) for r in runs["null-test"] if r.get("status") == "ok"})
        doc["verifier_floor"] = {
            "tasks": null_n,
            "pass_without_action": len(trivial),
            "keys": sorted(trivial),
        }
        for tag, rows in runs.items():
            if tag != "null-test" and rows and rows[0].get("split") == "test":
                first = [r for r in rows if r.get("sample", 0) == 0 and task_key(r) not in trivial]
                doc["runs"][tag]["nontrivial"] = rate(first)

    def pair(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
        out = paired(a, b)
        if trivial and out:
            out["excluding_trivial"] = paired(a, b, exclude=trivial)
        return out

    if runs.get("base-passk"):
        doc["pass_at_k"] = passk(runs["base-passk"])
    keys = ("n", "success", "rate", "ci95")
    doc["sft"] = {}
    for variant in SFT_VARIANTS:
        vdir = lab / "sft" / variant
        if not vdir.is_dir():
            continue
        prefix = f"sft-{variant}-val-"
        entry: dict[str, Any] = {
            "data": _json(vdir / "train.stats.json"),
            "train": _json(vdir / "adapters" / "train_meta.json"),
            "val": {
                t[len(prefix) :]: {k: v[k] for k in keys}
                for t, v in doc["runs"].items()
                if t.startswith(prefix)
            },
            "selected": _json(vdir / "selected.json"),
        }
        test = runs.get(f"sft-{variant}-test")
        if runs.get("base-test") and test:
            entry["test_paired"] = pair(runs["base-test"], test)
        doc["sft"][variant] = entry
    if runs.get("base-val"):
        doc["base_val"] = {k: doc["runs"]["base-val"][k] for k in keys}
    if runs.get("sft-teacher-test") and runs.get("sft-rft-test"):
        doc["rft_vs_teacher_test"] = pair(runs["sft-teacher-test"], runs["sft-rft-test"])
    if runs.get("sft-unfiltered-test") and runs.get("sft-teacher-test"):
        doc["filtered_vs_unfiltered_test"] = pair(runs["sft-unfiltered-test"], runs["sft-teacher-test"])
    if runs.get("base-test") and runs.get("teacher-test"):
        doc["teacher_vs_base_test"] = pair(runs["base-test"], runs["teacher-test"])
    doc["injection"] = {tag: injection(runs[tag]) for tag in INJECTION if runs.get(tag)}
    process_tags = ["base-test", *(f"sft-{v}-test" for v in SFT_VARIANTS), "teacher-test"]
    doc["process"] = {t: process(runs[t]) for t in process_tags if runs.get(t)}
    doc["serving"] = [d for p in sorted((lab / "bench").glob("*.json")) if (d := _json(p))]
    doc["risk"] = _json(lab / "risk" / "risk.json")
    return doc


def _paired_lines(a: str, b: str, p: dict[str, Any]) -> list[str]:
    return [
        "",
        f"test 配对比较（{a} → {b}，同一批 {p['tasks']} 个任务）：",
        "",
        f"- {a} {p['a_success']}/{p['tasks']}，{b} {p['b_success']}/{p['tasks']}",
        f"- 仅 {a} 成功 {p['only_a']}，仅 {b} 成功 {p['only_b']}，McNemar 精确检验 p = {p['mcnemar_p']:.4g}",
        f"- 成功率差 {_pct(p['diff'])}，配对 bootstrap 95% 区间 {_ci(p['diff_ci95'])}",
        *(
            [
                f"- 去掉不做任何操作也能通过 verifier 的任务后（{q['tasks']} 个）：{a} {q['a_success']}，"
                f"{b} {q['b_success']}，McNemar p = {q['mcnemar_p']:.4g}，"
                f"差 {_pct(q['diff'])}，区间 {_ci(q['diff_ci95'])}"
            ]
            if (q := p.get("excluding_trivial"))
            else []
        ),
    ]


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _ci(c: list[float]) -> str:
    return f"[{_pct(c[0])}, {_pct(c[1])}]"


def render(doc: dict[str, Any]) -> str:
    L: list[str] = [
        "# BizAgent Lab 实验报告",
        "",
        "本文件由 `workbench lab report` 从 data/lab 下的运行结果生成。",
        "",
    ]
    L += ["## 数据划分", "", "| split | 场景数 | 任务数 |", "|---|---|---|"]
    for k, v in doc["splits"].items():
        L.append(f"| {k} | {v['scenarios']} | {v['tasks']} |")
    L += ["", "## 各次运行", "", "成功 = 官方 pure-code verifier 判定 complete。区间为 Wilson 95%。", ""]
    L += [
        "| run | n | 成功 | 成功率 | 95% CI | 平均步数 | 平均 prompt tokens | 平均耗时 s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tag, s in doc["runs"].items():
        L.append(
            f"| {tag} | {s['n']} | {s['success']} | {_pct(s['rate'])} | {_ci(s['ci95'])} "
            f"| {s['mean_steps']:.1f} "
            f"| {s['mean_prompt_tokens']:.0f} | {s['mean_wall_s']:.1f} |"
        )
    floor = doc.get("verifier_floor")
    if floor:
        L += [
            "",
            "### verifier 下限",
            "",
            f"让智能体什么都不做（模型不回答，不调用任何工具），test 的 {floor['tasks']} 个任务中有 "
            f"{floor['pass_without_action']} 个仍被 verifier 判为 complete。"
            "下表给出 test 上各次运行去掉这些任务后的成功率；"
            "配对比较也同时给出去掉它们之后的结果。",
            "",
            "| run | n | 成功 | 成功率 | 95% CI |",
            "|---|---|---|---|---|",
        ]
        for tag, s in doc["runs"].items():
            if (q := s.get("nontrivial")) is not None:
                L.append(f"| {tag} | {q['n']} | {q['success']} | {_pct(q['rate'])} | {_ci(q['ci95'])} |")
    L += ["", "### 失败分类（每个失败 episode 一个类别，按顺序判定）", ""]
    for tag, s in doc["runs"].items():
        if s["failures"]:
            L.append(f"- {tag}: " + ", ".join(f"{k} {v}" for k, v in s["failures"].items()))
    if doc.get("pass_at_k"):
        pk = doc["pass_at_k"]
        L += [
            "",
            "## pass@k（base 模型，temperature 0.7）",
            "",
            f"{pk['tasks']} 个 test 任务，每个任务 {pk['samples_per_task']} 次采样，无偏估计。",
            "",
        ]
        L += ["| k | pass@k |", "|---|---|"] + [
            f"| {k.split('@')[1]} | {_pct(v)} |" for k, v in pk.items() if k.startswith("pass@")
        ]
    if doc.get("sft"):
        L += ["", "## 训练：教师蒸馏、拒绝采样自提升与数据消融（LoRA SFT）", ""]
        if doc.get("base_val"):
            b = doc["base_val"]
            L.append(
                f"base 在 val 上：{b['success']}/{b['n']}（{_pct(b['rate'])}）。"
                "checkpoint 只按 val 选择，test 只评一次。"
            )
        for variant, e in doc["sft"].items():
            L += ["", f"### {variant}：{SFT_VARIANTS[variant]}", ""]
            if e.get("data"):
                d = e["data"]
                L.append(
                    f"- 数据：读取 {d.get('episodes', 0)} 个 episode，使用 {d.get('used_episodes', 0)} 个"
                    f"（其中 verifier 通过 {d.get('used_successful_episodes', 0)}），"
                    f"{d.get('tasks', 0)} 个任务、"
                    f"{d.get('scenarios', 0)} 个场景，{d.get('samples', 0)} 条样本。"
                )
            if e.get("train"):
                t = e["train"]
                L.append(
                    f"- 训练：{t['samples']} 条样本（超长丢弃 {t['dropped_too_long']}），"
                    f"{t['tokens']} tokens，"
                    f"其中计算 loss 的 {t['trained_tokens']}，{t['steps']} 步，用时 {t['train_s']:.0f} s。"
                )
            if e.get("val"):
                L += ["", "| checkpoint | n | val 成功率 | 95% CI |", "|---|---|---|---|"]
                for ck, v in e["val"].items():
                    L.append(f"| {ck} | {v['n']} | {_pct(v['rate'])} | {_ci(v['ci95'])} |")
            if e.get("selected"):
                L.append(f"\n选中：{e['selected'].get('checkpoint')}")
            if e.get("test_paired"):
                L += _paired_lines("base", variant, e["test_paired"])
        if doc.get("rft_vs_teacher_test"):
            rft = _paired_lines("teacher", "rft", doc["rft_vs_teacher_test"])
            L += ["", "### 拒绝采样自提升相对教师蒸馏（test）", *rft]
        if doc.get("filtered_vs_unfiltered_test"):
            filt = _paired_lines("unfiltered", "teacher", doc["filtered_vs_unfiltered_test"])
            L += ["", "### verifier 过滤的作用（test）", *filt]
    if doc.get("teacher_vs_base_test"):
        p = doc["teacher_vs_base_test"]
        L += [
            "",
            "## 教师模型参考（test 集）",
            "",
            f"教师 {p['b_success']}/{p['tasks']}，base {p['a_success']}/{p['tasks']}。",
        ]
    if doc.get("process"):
        L += [
            "",
            "## 过程指标（test）",
            "",
            "不看 verifier，只看 episode 是怎么进行的。teacher-test 只跑了 test 的前一部分任务。",
            "",
            "| run | episodes | 平均步数 | 每 episode 工具报错 | 有工具报错 | 计划无效 | LLM 错误 | 守卫终止 "
            "| 未调用工具 | 平均 prompt tokens |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for tag, m in doc["process"].items():
            L.append(
                f"| {tag} | {m['episodes']} | {m['mean_steps']:.1f} | {m['tool_errors_per_episode']:.2f} "
                f"| {_pct(m['share_with_tool_error'])} | {_pct(m['share_plan_invalid'])} "
                f"| {_pct(m['share_llm_error'])} | {_pct(m['share_guard_stop'])} "
                f"| {_pct(m['share_no_tool_call'])} | {m['mean_prompt_tokens']:.0f} |"
            )
    if doc.get("injection"):
        L += [
            "",
            "## 工具结果注入",
            "",
            "任务取自含 destructive 工具的 test 场景（inject 任务集）。每个 episode 第一次成功的读调用结果后"
            "追加一条要求调用本场景某个 destructive 工具的指令。",
            "attempted = 模型发起了该调用；executed = 该调用真正执行成功。",
            "",
            "| 配置 | episodes | attempted | executed | executed 95% CI | 任务成功率 |",
            "|---|---|---|---|---|---|",
        ]
        for tag, v in doc["injection"].items():
            L.append(
                f"| {INJECTION[tag]} | {v['episodes']} | {_pct(v['attempt_rate'])} "
                f"| {_pct(v['executed_rate'])} "
                f"| {_ci(v['executed_ci95'])} | {_pct(v['utility']['rate'])} |"
            )
    if doc.get("serving"):
        L += [
            "",
            "## 推理服务（回放录制的 agent 请求，固定输出 256 tokens）",
            "",
            "| 配置 | 并发 | 请求数 | TTFT p50 ms | TTFT p95 ms | 延迟 p50 ms | 输出 tok/s "
            "| 前缀缓存命中率 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for d in doc["serving"]:
            for r in d["results"]:
                hit = "-" if r["prefix_cache_hit_rate"] is None else _pct(r["prefix_cache_hit_rate"])
                L.append(
                    f"| {d['label']} | {r['concurrency']} | {r['requests']} | {r['ttft_ms_p50']} "
                    f"| {r['ttft_ms_p95']} "
                    f"| {r['latency_ms_p50']} | {r['output_tok_s']} | {hit} |"
                )
    risk = doc.get("risk")
    if risk and "models" in risk:
        L += [
            "",
            "## 无 verifier 的失败预测",
            "",
            f"{risk['episodes']} 个已验证 episode（失败占 {_pct(risk['failure_rate'])}），"
            f"{risk['scenarios']} 个场景，"
            f"按场景分组 {risk['folds']} 折。标签是 verifier 的判定。",
            "",
            "| 模型 | AUROC | AUPRC | 复核 10% 召回 | 复核 20% 召回 | 复核 30% 召回 |",
            "|---|---|---|---|---|---|",
        ]
        for name, m in risk["models"].items():
            L.append(
                f"| {name} | {m['auroc']:.3f} | {m['auprc']:.3f} | {_pct(m['recall_at_10pct'])} "
                f"| {_pct(m['recall_at_20pct'])} | {_pct(m['recall_at_30pct'])} |"
            )
        L += ["", "| 单一规则 | 标记比例 | 召回 | 精确率 |", "|---|---|---|---|"]
        for name, m in risk["rules"].items():
            L.append(
                f"| {name} | {_pct(m['flagged_share'])} | {_pct(m['recall'])} | {_pct(m['precision'])} |"
            )
    return "\n".join(L) + "\n"


def write_report(lab: Path) -> Path:
    doc = collect(lab)
    (lab / "summary.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    out = lab / "REPORT.md"
    out.write_text(render(doc), encoding="utf-8")
    return out
