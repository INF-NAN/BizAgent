"""Load / validate results/registry.yaml and render docs/RESULTS.md from it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SOURCE_KEYS = ("arxiv_id", "version", "table", "row", "column")
VERIFICATION_KEYS = ("verified_by", "verified_at", "evidence")
UNITS = ("score", "pass rate (%)", "success rate (%)", "unknown")
DISCLAIMER = "论文报告值，由官方模型在官方评测 harness 上测得，不是本仓库应用层的测量结果。"


class RegistryError(ValueError):
    """The registry violates the R3 contract."""


@dataclass(frozen=True)
class Entry:
    id: str
    benchmark: str
    metric: str
    model: str
    value: float
    printed: str
    source: dict[str, Any]
    verified: bool
    unit: str = "unknown"
    evidence: str | None = None


@dataclass(frozen=True)
class Registry:
    raw: dict[str, Any]
    entries: list[Entry]

    @property
    def printed_values(self) -> set[str]:
        return {e.printed for e in self.entries}


def load_registry(path: Path, repo_root: Path | None = None) -> Registry:
    """Load and validate the registry; evidence paths resolve against `repo_root`
    (default: the parent of the registry's `results/` directory)."""
    root = repo_root if repo_root is not None else path.resolve().parent.parent
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw.get("disclaimer") != DISCLAIMER:
        raise RegistryError("registry disclaimer missing or altered")
    entries: list[Entry] = []
    seen: set[str] = set()
    for row in raw.get("entries", []):
        rid = row.get("id", "<missing id>")
        if rid in seen:
            raise RegistryError(f"duplicate id {rid}")
        seen.add(rid)
        src = row.get("source") or {}
        missing = [k for k in SOURCE_KEYS if k not in src]
        if missing:
            raise RegistryError(f"{rid}: source lacks {missing}")
        if not isinstance(row.get("verified"), bool):
            raise RegistryError(f"{rid}: verified must be true/false")
        if row["verified"] and any(src.get(k) in (None, "") for k in SOURCE_KEYS):
            raise RegistryError(f"{rid}: verified entries need version/table/row/column")
        if row["verified"]:
            lacking = [k for k in VERIFICATION_KEYS if not row.get(k)]
            if lacking:
                raise RegistryError(f"{rid}: verified entries need {lacking}")
            if not (root / str(row["evidence"])).is_file():
                raise RegistryError(f"{rid}: evidence file {row['evidence']} does not exist")
        unit = str(row.get("unit", "unknown"))
        if unit not in UNITS:
            raise RegistryError(f"{rid}: unit {unit!r} not in {UNITS}")
        printed = str(row["printed"])
        if float(printed) != float(row["value"]):
            raise RegistryError(f"{rid}: printed {printed} != value {row['value']}")
        if row.get("model") not in (raw.get("models") or {}):
            raise RegistryError(f"{rid}: unknown model key {row.get('model')}")
        entries.append(
            Entry(
                rid,
                row["benchmark"],
                row["metric"],
                row["model"],
                float(row["value"]),
                printed,
                src,
                row["verified"],
                unit,
                row.get("evidence"),
            )
        )
    return Registry(raw, entries)


def render_results_md(reg: Registry) -> str:
    paper = reg.raw["paper"]
    models = reg.raw["models"]
    version = paper.get("version_checked")
    checked = f"{version}（{paper.get('version_date', '?')}）" if version else "未核对（PDF 未能访问）"
    lines = [
        "# RESULTS — 论文报告值（非本仓库测量）",
        "",
        "> 本文件由 `make results`（`workbench results render`）从 `results/registry.yaml` 自动生成，"
        "请勿手改。",
        f"> **{DISCLAIMER}**",
        "> 本仓库的应用层（智能体、网关、记忆、服务）从未做过任何基准评测。",
        "",
        f"- 论文：arXiv {paper['arxiv_id']} — {paper['title']}",
        f"- 核对的论文版本：{checked}",
    ]
    if paper.get("venue_claimed"):
        lines.append(f"- 发表状态：{paper['venue_claimed']}")
    if paper.get("evidence"):
        lines.append(f"- 核对记录：`{paper['evidence']}`")
    notes = reg.raw.get("table_notes") or []
    if notes:
        lines += ["", "## 表注", "", *[f"- {n}" for n in notes]]
    lines += [
        "",
        "## 登记的数值",
        "",
        "| 基准 | 指标 | 单位 | 模型 | 数值 | 来源（版本 / 表 / 行 / 列） | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in reg.entries:
        s = e.source
        where = " / ".join(
            str(s.get(k) if s.get(k) is not None else "?") for k in ("version", "table", "row", "column")
        )
        status = "已核对" if e.verified else "**待核对**"
        label = models[e.model]["label"]
        src = f"arXiv {s['arxiv_id']}: {where}"
        lines.append(f"| {e.benchmark} | {e.metric} | {e.unit} | {label} | {e.printed} | {src} | {status} |")
    lines += [
        "",
        "## 模型身份（推定）",
        "",
        "| 行 | 推定身份与证据 | 身份已确认 |",
        "|---|---|---|",
    ]
    for key, m in models.items():
        ident = " ".join(str(m.get("presumed_identity", "")).split())
        lines.append(f"| {m['label']} (`{key}`) | {ident} | {'是' if m.get('verified') else '否（推定）'} |")
    pending = reg.raw.get("pending") or []
    if pending:
        lines += ["", "## 尚未录入的格子", ""]
        for p in pending:
            cells = ", ".join(models[c]["label"] for c in p.get("cells", [])) or "—"
            lines.append(f"- {p['benchmark']} {p['metric']}：{cells}；{p.get('also', '')}")
    if any(not e.verified for e in reg.entries):
        lines += ["", "`待核对`：数值尚未对照论文表格逐格核实。"]
    lines.append("")
    return "\n".join(lines)
