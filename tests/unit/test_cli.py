import pytest
from typer.testing import CliRunner

from workbench.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [[], ["env"], ["gateway"], ["serve"], ["agent"], ["api"], ["synth"], ["train"], ["results"], ["doctor"]],
)
def test_help(args: list[str]) -> None:
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output


def test_gateway_export_risk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import csv

    out = tmp_path / "risk.csv"
    r = runner.invoke(
        app, ["gateway", "export-risk", "--dataset-dir", "tests/fixtures/awm_mini", "--out", str(out)]
    )
    assert r.exit_code == 0, r.output
    rows = {row["tool"]: row for row in csv.DictReader(out.open())}
    assert rows["delete_user_payment_method"]["risk"] == "destructive"
    assert rows["search_products"]["requires_approval"] == "False"
    assert (rows["add_item_to_cart"]["http_method"], rows["search_products"]["http_method"]) == (
        "POST",
        "GET",
    )
    assert "graded read: 0 by name alone, 0 with the HTTP-method floor" in " ".join(r.output.split())


def test_gateway_export_risk_applies_the_method_floor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import csv
    import json

    code = (
        "@app.delete('/lists/{list_id}', operation_id='purge_my_list')\ndef purge(list_id: int): ...\n"
        "@app.get('/lists', operation_id='list_lists')\ndef lists(): ...\n"
    )
    (tmp_path / "gen_envs.jsonl").write_text(json.dumps({"scenario": "s", "full_code": code}) + "\n")
    out = tmp_path / "risk.csv"
    r = runner.invoke(app, ["gateway", "export-risk", "--dataset-dir", str(tmp_path), "--out", str(out)])
    assert r.exit_code == 0, r.output
    rows = {row["tool"]: row for row in csv.DictReader(out.open())}
    purge = rows["purge_my_list"]
    assert (purge["http_method"], purge["heuristic_risk"], purge["risk"], purge["source"]) == (
        "DELETE",
        "read",
        "destructive",
        "http_method",
    )
    assert purge["requires_approval"] == "True" and rows["list_lists"]["risk"] == "read"
    assert "graded read: 1 by name alone, 0 with the HTTP-method floor" in " ".join(r.output.split())
