"""Predict, without the verifier, which finished episodes failed (runs in the GPU venv: sklearn).

Every feature is something the workbench sees at run time: step and tool counts, tool errors and
empty results, how the run ended, the rows the episode changed (the gateway measures them) and
the wording of the final answer. The label is the code verifier's verdict, which is itself
imperfect, so the scores are about agreeing with that verifier. Folds are grouped by scenario:
no scenario is in both the fitting and the scoring part of a fold.

usage: python scripts/lab/risk_model.py --lab-dir data/lab --tags base-test base-val ... --out risk.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HEDGE = re.compile(
    r"\b(unable|cannot|can't|could not|couldn't|sorry|not found|no matching|failed|error|unfortunately)\b",
    re.I,
)
REASONS = (
    "max_steps",
    "repeated_call",
    "no_state_change",
    "token_budget",
    "wall_clock",
    "plan_invalid",
    "llm_error",
)


def features(r: dict) -> dict[str, float]:
    diff = r.get("db_diff") or {}
    rows = {k: 0 for k in ("added", "removed", "changed")}
    for t in diff.values():
        for k in rows:
            v = t.get(k, 0)
            rows[k] += v if isinstance(v, int) else len(v or [])
    answer = r.get("final_answer") or ""
    reason = (r.get("termination") or {}).get("reason")
    f = {
        "steps": r.get("steps", 0),
        "llm_calls": r.get("llm_calls", 0),
        "tool_calls": r.get("tool_calls", 0),
        "tool_errors": r.get("tool_errors", 0),
        "tool_empty": r.get("tool_empty", 0),
        "error_share": r.get("tool_errors", 0) / max(1, r.get("tool_calls", 0)),
        "writes": r.get("writes", 0),
        "no_write": float(r.get("writes", 0) == 0),
        "denied": r.get("denied", 0),
        "approvals": len(r.get("approvals") or []),
        "log_prompt_tokens": math.log1p(r.get("prompt_tokens", 0)),
        "log_completion_tokens": math.log1p(r.get("completion_tokens", 0)),
        "tables_changed": len(diff),
        "rows_added": rows["added"],
        "rows_removed": rows["removed"],
        "rows_changed": rows["changed"],
        "no_db_change": float(not diff),
        "answer_len_log": math.log1p(len(answer)),
        "answer_hedges": len(HEDGE.findall(answer)),
        "terminated": float(reason is not None),
    }
    for name in REASONS:
        f[f"term_{name}"] = float(reason == name)
    return f


def recall_at_budget(y: np.ndarray, score: np.ndarray, budget: float) -> float:
    k = max(1, round(budget * len(y)))
    top = np.argsort(-score, kind="stable")[:k]
    return float(y[top].sum() / max(1, y.sum()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab-dir", type=Path, required=True)
    ap.add_argument("--tags", nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()

    rows = []
    for tag in args.tags:
        path = args.lab_dir / "runs" / tag / "results.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("status") == "ok" and "success" in r:
                    rows.append(r)
    if len(rows) < 50:
        args.out.write_text(json.dumps({"skipped": f"only {len(rows)} verified episodes"}), encoding="utf-8")
        return
    feats = [features(r) for r in rows]
    names = list(feats[0])
    X = np.array([[f[n] for n in names] for f in feats], dtype=float)
    y = np.array([0 if r["success"] else 1 for r in rows])
    groups = np.array([r["scenario"] for r in rows])
    folds = min(args.folds, len(set(groups)))
    models = {
        "logistic_regression": lambda: make_pipeline(
            StandardScaler(), LogisticRegression(max_iter=2000, C=1.0)
        ),
        "gradient_boosting": lambda: HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.05, random_state=0
        ),
    }
    out: dict = {
        "episodes": len(rows),
        "failure_rate": float(y.mean()),
        "scenarios": len(set(groups)),
        "folds": folds,
        "tags": args.tags,
        "features": names,
        "models": {},
    }
    for name, make in models.items():
        score = np.zeros(len(y))
        for train, test in GroupKFold(n_splits=folds).split(X, y, groups):
            m = make().fit(X[train], y[train])
            score[test] = m.predict_proba(X[test])[:, 1]
        out["models"][name] = {
            "auroc": float(roc_auc_score(y, score)),
            "auprc": float(average_precision_score(y, score)),
            "recall_at_10pct": recall_at_budget(y, score, 0.10),
            "recall_at_20pct": recall_at_budget(y, score, 0.20),
            "recall_at_30pct": recall_at_budget(y, score, 0.30),
        }
    # single-signal rules for comparison (no fitting)
    rules = {
        "tool_errors>0": X[:, names.index("tool_errors")] > 0,
        "terminated": X[:, names.index("terminated")] > 0,
        "no_write": X[:, names.index("no_write")] > 0,
    }
    out["rules"] = {
        k: {
            "flagged_share": float(v.mean()),
            "recall": float(y[v].sum() / max(1, y.sum())),
            "precision": float(y[v].mean()) if v.any() else 0.0,
        }
        for k, v in rules.items()
    }
    lr = models["logistic_regression"]().fit(X, y)
    coef = lr[-1].coef_[0]
    out["logistic_coefficients"] = dict(
        sorted(zip(names, map(float, coef), strict=True), key=lambda kv: -abs(kv[1]))[:10]
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
