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
