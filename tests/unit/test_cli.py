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
