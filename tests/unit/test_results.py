from pathlib import Path

import pytest
import yaml

from workbench.results.check_numbers import load_whitelist, scan
from workbench.results.registry import DISCLAIMER, RegistryError, load_registry, render_results_md

REG = Path("results/registry.yaml")


def test_registry_contract() -> None:
    reg = load_registry(REG)
    assert len(reg.entries) == 9 and not any(e.verified for e in reg.entries)
    assert "39.03" in reg.printed_values


@pytest.mark.parametrize(
    ("mutate", "msg"),
    [
        (lambda r: r.update(disclaimer="x"), "disclaimer"),
        (lambda r: r["entries"][0]["source"].pop("table"), "source lacks"),
        (lambda r: r["entries"][0].update(verified="no"), "verified must be"),
        (lambda r: r["entries"][0].update(verified=True), "verified entries need"),
        (lambda r: r["entries"][0].update(printed="1.00"), "printed"),
        (lambda r: r["entries"][1].update(id=r["entries"][0]["id"]), "duplicate"),
    ],
)
def test_registry_rejects_bad_entries(tmp_path: Path, mutate, msg: str) -> None:  # type: ignore[no-untyped-def]
    raw = yaml.safe_load(REG.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(RegistryError, match=msg):
        load_registry(p)


def test_results_md_is_generated_from_registry() -> None:
    rendered = render_results_md(load_registry(REG))
    assert Path("docs/RESULTS.md").read_text(encoding="utf-8") == rendered, "run `make results`"
    assert DISCLAIMER in rendered and rendered.count("**待核对**") == 9


def _scan(tmp_path: Path, text: str) -> list[str]:
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "x.md"
    doc.write_text(text, encoding="utf-8")
    wl = load_whitelist(Path("configs/number_whitelist.yaml"))
    return [f.reason for f in scan([doc], load_registry(REG), wl, tmp_path)]


def test_guard_flags_unregistered_metrics(tmp_path: Path) -> None:
    reasons = _scan(tmp_path, "our agent reaches 87.50 overall and 92% tool accuracy, pass@1: 0.7\n")
    assert len(reasons) == 3


def test_guard_flags_app_layer_claims(tmp_path: Path) -> None:
    assert any("rule R3" in r for r in _scan(tmp_path, "网关带来了成功率提升\n"))


def test_guard_accepts_registry_values_with_disclaimer(tmp_path: Path) -> None:
    assert _scan(tmp_path, f"14B AWM: 39.03 ({DISCLAIMER})\n") == []


def test_guard_requires_disclaimer_next_to_paper_numbers(tmp_path: Path) -> None:
    assert _scan(tmp_path, "14B AWM: 39.03\n") == ["paper numbers cited without the R3 disclaimer"]


def test_guard_ignores_versions_and_whitelist(tmp_path: Path) -> None:
    assert _scan(tmp_path, "python 3.12, `pydantic>=2.11`, starlette <0.47, mcp 1.26.0, 2026-09-24\n") == []
