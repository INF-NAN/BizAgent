import pytest
from typer.testing import CliRunner

from workbench.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["env"],
        ["gateway"],
        ["gateway", "policy", "test"],
        ["serve"],
        ["agent"],
        ["api"],
        ["synth"],
        ["train"],
        ["results"],
        ["doctor"],
    ],
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


def test_synth_run_dry_run_with_scenario_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    from pathlib import Path

    src = tmp_path / "s.jsonl"
    src.write_text(json.dumps({"name": "local_it_service_desk", "description": "d"}) + "\n")
    out = Path("data/synth/_dry_run_cli_test")
    result = runner.invoke(
        app, ["synth", "run", "--scenarios", "1", "--out", str(out), "--scenario-file", str(src)]
    )
    assert result.exit_code == 0, result.output
    assert '"name": "task"' in result.output and '"name": "scenario"' not in result.output
    assert "gen scenario skipped" in result.output and not out.exists()  # dry-run creates nothing


MINI = ["--dataset-dir", "tests/fixtures/awm_mini", "--scenario", "mini_e_commerce"]


def flat(output: str) -> str:
    return " ".join(output.split())


def test_gateway_policy_test_explains_each_rule_and_the_decision() -> None:
    args = '{"product_offer_id": 11, "quantity": 1}'
    r = runner.invoke(app, ["gateway", "policy", "test", *MINI, "--tool", "add_item_to_cart", "--args", args])
    assert r.exit_code == 0, r.output
    out = flat(r.output)
    assert "policy: configs/approval_policy.yaml (3 rules)" in out
    assert "risk: write (heuristic: verb 'add' => write)" in out
    assert "quantity=1 fails gt 5" in out  # every rule is listed with why it did not match
    assert "decision: require_human (default) - no rule matched: write calls need a human by default" in out
    assert "means: a person approves it on the approval card, after a preview (ADR-029)" in out

    pm_args = ["--tool", "delete_user_payment_method", "--args", '{"payment_method_id": 2}']
    pm = runner.invoke(app, ["gateway", "policy", "test", *MINI, *pm_args])
    assert pm.exit_code == 0 and "decision: deny (rule no-payment-method-deletion)" in flat(pm.output)
    assert "means: refused; nobody can approve it" in flat(pm.output)


def test_gateway_policy_test_auto_approve_and_the_destructive_guard(tmp_path) -> None:  # type: ignore[no-untyped-def]
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\nrules:\n  - {id: everything, decision: auto_approve}\n", encoding="utf-8")
    common = ["gateway", "policy", "test", "--policy", str(policy)]
    # --risk: no catalog needed; a prefixed tool name gives the scenario
    r = runner.invoke(app, [*common, "--tool", "shop__add_item_to_cart", "--risk", "write"])
    assert r.exit_code == 0, r.output
    out = flat(r.output)
    assert "risk: write (given with --risk)" in out and "decision: auto_approve (rule everything)" in out
    assert "approver policy:everything, no preview" in out
    # the same rule never approves a destructive call
    d = runner.invoke(app, [*common, *MINI, "--tool", "remove_cart_item", "--args", '{"cart_item_id": 1}'])
    assert d.exit_code == 0, d.output
    out = flat(d.output)
    assert "skipped" in out and "decision: require_human (default)" in out
    assert "guard: rule everything skipped: destructive calls are never auto-approved (ADR-030)" in out


def test_gateway_policy_test_errors(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nrules:\n  - {id: a, decision: deny, tool_name: x}\n", encoding="utf-8")
    r = runner.invoke(
        app, ["gateway", "policy", "test", "--policy", str(bad), "--tool", "s__t", "--risk", "read"]
    )
    assert r.exit_code == 2 and "rules[0] (id a).tool_name: unknown field" in flat(r.output)
    unknown = runner.invoke(app, ["gateway", "policy", "test", *MINI, "--tool", "launch_rocket"])
    assert unknown.exit_code == 2 and "pass --risk to test anyway" in flat(unknown.output)
    bad_args = runner.invoke(
        app, ["gateway", "policy", "test", "--tool", "s__t", "--risk", "read", "--args", "[1]"]
    )
    assert bad_args.exit_code == 2 and "--args must be a JSON object" in flat(bad_args.output)
    no_scenario = runner.invoke(app, ["gateway", "policy", "test", "--tool", "t", "--risk", "read"])
    assert no_scenario.exit_code == 2 and "give --scenario" in flat(no_scenario.output)
