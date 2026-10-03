"""Lab package on CPU: statistics, split, SFT data, report and one verified episode end to end."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.unit.agent_harness import MiniUpstream
from workbench.config import GatewaySettings, LLMSettings, Settings
from workbench.envs.service import EnvInfo
from workbench.gateway.approval_policy import ApprovalPolicy
from workbench.gateway.core import Gateway
from workbench.gateway.policy import ApprovalService, PolicyConfig
from workbench.lab import metrics, sft_data, splits
from workbench.lab.episodes import (
    EvalConfig,
    InjectingGateway,
    lab_settings,
    load_results,
    preview_guard,
    run_eval,
)
from workbench.lab.recorder import load_recording
from workbench.lab.report import collect, render
from workbench.llm.client import build_backend

FIX = Path("tests/fixtures")
MINI = FIX / "awm_mini"


def test_pass_at_k_and_tests() -> None:
    assert metrics.pass_at_k(4, 0, 1) == 0.0
    assert metrics.pass_at_k(4, 4, 2) == 1.0
    assert metrics.pass_at_k(4, 1, 1) == pytest.approx(0.25)
    assert metrics.pass_at_k(4, 1, 4) == 1.0
    assert metrics.pass_at_k(4, 2, 2) == pytest.approx(1 - 1 / 6)
    assert metrics.mcnemar_exact(0, 0) == 1.0
    assert metrics.mcnemar_exact(0, 10) == pytest.approx(2 / 2**10)
    lo, hi = metrics.wilson(5, 10)
    assert lo < 0.5 < hi
    mean, lo, hi = metrics.paired_bootstrap([0, 0, 1, 1], [1, 1, 1, 1])
    assert mean == 0.5 and lo <= mean <= hi


def test_failure_category_order() -> None:
    assert metrics.failure_category({"status": "env_error"}) == "error:env_error"
    assert metrics.failure_category({"status": "ok", "success": True}) == "success"
    term = {"status": "ok", "termination": {"reason": "max_steps"}}
    assert metrics.failure_category(term) == "guard:max_steps"
    assert metrics.failure_category({"status": "ok", "tool_calls": 0}) == "no_tool_call"
    assert metrics.failure_category({"status": "ok", "tool_calls": 2, "writes": 0}) == "no_write"
    assert metrics.failure_category({"status": "ok", "tool_calls": 2, "writes": 1}) == "wrong_outcome"


def test_split_is_scenario_disjoint_and_deterministic() -> None:
    tasks = {f"s{i}": [f"task {i}.{j}" for j in range(5)] for i in range(20)}
    verifiers = {(s, j): {"scenario": s, "task_idx": j} for s in tasks for j in range(4)}  # j=4 has none
    kw: dict[str, Any] = {"scenario_counts": {"test": 5, "val": 3}, "task_counts": {"test": 10, "train": 30}}
    a = splits.make_split(tasks, verifiers, seed=1, **kw)
    b = splits.make_split(tasks, verifiers, seed=1, **kw)
    assert a == b
    scen = {k: {t.scenario for t in v} for k, v in a.items()}
    assert (
        not (scen["test"] & scen["train"])
        and not (scen["val"] & scen["train"])
        and not (scen["test"] & scen["val"])
    )
    assert len(a["test"]) == 10 and len(a["val"]) == 12 and len(a["train"]) == 30
    assert all(t.task_id != 4 for v in a.values() for t in v)


def test_split_files_round_trip(tmp_path: Path) -> None:
    tasks = splits.load_tasks(MINI)
    verifiers = splits.load_verifier_index(MINI)
    split = splits.make_split(tasks, verifiers, seed=0, scenario_counts={}, task_counts={})
    splits.write_split(tmp_path, split, verifiers, 0)
    assert splits.read_split(tmp_path, "train") == split["train"]
    assert set(splits.read_verifiers(tmp_path)) == {(t.scenario, t.task_id) for t in split["train"]}


def test_vllm_backend_sends_enable_thinking() -> None:
    off = build_backend(LLMSettings(backend="vllm", enable_thinking=False))
    on = build_backend(LLMSettings(backend="vllm"))
    compat = build_backend(LLMSettings(backend="openai_compat"))
    assert off.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}  # type: ignore[attr-defined]
    assert on.extra_body == {"chat_template_kwargs": {"enable_thinking": True}}  # type: ignore[attr-defined]
    assert compat.extra_body == {}  # type: ignore[attr-defined]


def test_preview_guard() -> None:
    removed = {"status": "ok", "changes": {"tables": {"t": {"counts": {"removed": 1}}}}}
    added = {"status": "ok", "changes": {"tables": {"t": {"counts": {"added": 1, "removed": 0}}}}}
    assert preview_guard({"preview": removed})[0] is False
    assert preview_guard({"preview": {"status": "failed"}})[0] is False
    assert preview_guard({"preview": added})[0] is True


def _call(messages: list[dict[str, Any]], content: str = "", tool: str | None = None) -> dict[str, Any]:
    calls = [{"id": "c", "name": tool, "arguments": {"x": 1}}] if tool else []
    return {
        "messages": messages,
        "with_tools": tool is not None or content == "final",
        "response": {"content": content, "tool_calls": calls, "finish_reason": "stop"},
    }


def test_sft_conversations_keep_only_longest_act_chain() -> None:
    sys_user = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    first = _call(sys_user, tool="t")
    a1 = sft_data.assistant_message(first["response"])
    second_msgs = [*sys_user, a1, {"role": "tool", "tool_call_id": "c", "content": "r"}]
    second = _call(second_msgs, content="final")
    plan = _call([{"role": "user", "content": "plan please"}], content="{}")
    convs = sft_data.conversations({"tools": [], "calls": [plan, first, second]})
    assert len(convs) == 2
    assert convs[1]["messages"][-1] == {"role": "assistant", "content": "final"}
    assert convs[1]["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'


# --- one episode end to end: env service, gateway, agent, recording, verifier ------------------


class FakeEnvService:
    """Session databases on disk like the env-manager's; no server processes."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    async def start(self, scenario: str, session_id: str | None = None) -> EnvInfo:
        assert session_id
        d = self.runs_dir / session_id
        d.mkdir(parents=True, exist_ok=True)
        for name in ("initial.db", "work.db"):
            with sqlite3.connect(d / name) as c:
                c.execute("CREATE TABLE carts (id INTEGER PRIMARY KEY, user_id INT, status TEXT)")
                c.execute(
                    "CREATE TABLE cart_items "
                    "(id INTEGER PRIMARY KEY, cart_id INT, product_offer_id INT, quantity INT)"
                )
                c.execute("INSERT INTO carts VALUES (1, 1, 'active')")
        return EnvInfo(session_id, scenario, f"http://fake/{session_id}", "running", 0)

    async def stop(self, session_id: str) -> None:
        return None

    async def touch(self, session_id: str) -> None:
        return None

    async def close(self) -> None:
        return None

    async def preview(
        self, session_id: str, tool: str, arguments: dict[str, Any], timeout_s: float, max_rows: int = 20
    ) -> dict[str, Any]:
        return {"status": "ok", "changes": {"tables": {}}, "digest": "d"}


class DbUpstream(MiniUpstream):
    """MiniUpstream whose cart writes land in the session's work.db."""

    def __init__(self, runs_dir: Path) -> None:
        super().__init__()
        self.runs_dir = runs_dir

    async def call_tool(
        self, url: str, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> tuple[bool, str]:
        if name == "add_item_to_cart":
            with sqlite3.connect(self.runs_dir / url.rsplit("/", 1)[-1] / "work.db") as c:
                c.execute(
                    "INSERT INTO cart_items (cart_id, product_offer_id, quantity) VALUES (1, ?, ?)",
                    (arguments["product_offer_id"], arguments["quantity"]),
                )
        return await super().call_tool(url, name, arguments, timeout_s)


def code_verify(settings: Settings, run_dir: Path, **kw: Any) -> dict[str, Any]:
    entry = json.loads(Path(kw["verifier"]).read_text(encoding="utf-8").splitlines()[0])
    traj = json.loads((run_dir / "trajectory.json").read_text(encoding="utf-8"))
    scope: dict[str, Any] = {}
    exec(entry["verification"]["code"], scope)  # the fixture's hand-written verifier
    out = scope["verify_task_completion"](
        str(kw["init_db"]), str(kw["final_db"]), traj["messages"][-1]["content"]
    )
    return {"reward_type": out["result"]}


async def _run(
    tmp_path: Path, *, inject: bool = False, backend: Any = None, verify_fn: Any = None
) -> list[dict[str, Any]]:
    lab = tmp_path / "lab"
    tasks = splits.load_tasks(MINI)
    verifiers = splits.load_verifier_index(MINI)
    split = splits.make_split(tasks, verifiers, seed=0, scenario_counts={}, task_counts={})
    splits.write_split(lab, split, verifiers, 0)
    base = Settings(
        env={"dataset_dir": MINI},  # type: ignore[arg-type]
        llm={
            "backend": "mock_replay",
            "mock_fixture": FIX / "trajectories" / "demo_query_write_approve.jsonl",
        },  # type: ignore[arg-type]
    )
    settings = lab_settings(base, lab / "runs" / "t", concurrency=1, port_min=30000)
    runs_dir = settings.env.runs_dir
    gateway = Gateway(
        GatewaySettings(audit_path=lab / "audit.jsonl"),
        policy=PolicyConfig.load(Path("configs/tool_policy.yaml")),
        approvals=ApprovalService(secret=b"k"),
        upstream=DbUpstream(runs_dir),
        approval_policy=ApprovalPolicy.load(Path("configs/lab/eval_policy.yaml")),
    )
    task = next(t for t in splits.read_split(lab, "train") if t.task_id == 0)
    cfg = EvalConfig(tag="t", out_dir=lab, concurrency=1, inject=inject)
    path = await run_eval(
        settings,
        [task],
        cfg,
        splits.read_verifiers(lab),
        env_service=FakeEnvService(runs_dir),
        gateway=gateway,
        verify_fn=verify_fn or code_verify,
        **({"backend": backend} if backend is not None else {}),
    )
    return load_results(path)


async def test_episode_end_to_end(tmp_path: Path) -> None:
    rows = await _run(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "ok" and r["success"] is True and r["reward_type"] == "complete"
    assert r["writes"] == 1 and r["tool_errors"] == 0 and r["db_diff"]["cart_items"]["added"]
    assert [e["tool"] for e in r["executed"]] == [
        "mini_e_commerce__search_products",
        "mini_e_commerce__add_item_to_cart",
    ]
    rec = load_recording(tmp_path / "lab" / "runs" / "t" / "calls" / f"{r['run_id']}.json.gz")
    assert len(rec["calls"]) == r["llm_calls"] > 0 and rec["tools"]
    assert not (tmp_path / "lab" / "runs" / "t" / "envs" / r["session_id"]).exists()  # cleaned after verify
    # resumable: a second run skips the finished episode
    assert len(await _run(tmp_path)) == 1

    out = tmp_path / "sft.jsonl"
    stats = sft_data.build([tmp_path / "lab" / "runs" / "t"], out)
    assert stats["used_episodes"] == 1 and stats["samples"] >= 1
    sample = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert sample["messages"][-1]["role"] == "assistant"

    doc = collect(tmp_path / "lab")
    assert doc["runs"]["t"]["success"] == 1
    assert "BizAgent Lab" in render(doc)


async def test_injection_is_appended_once(tmp_path: Path) -> None:
    rows = await _run(tmp_path, inject=True)
    r = rows[0]
    assert r["injection"]["tool"] == "mini_e_commerce__delete_user_payment_method"
    assert r["attack_executed"] is False  # the scripted model ignores it
    rec = load_recording(tmp_path / "lab" / "runs" / "t" / "calls" / f"{r['run_id']}.json.gz")
    tool_texts = [m["content"] for c in rec["calls"] for m in c["messages"] if m.get("role") == "tool"]
    assert any(r["injection"]["text"] in t for t in tool_texts)


def test_injecting_gateway_delegates() -> None:
    class G:
        marker = 7

    assert InjectingGateway(G()).marker == 7  # type: ignore[arg-type]


async def test_serving_bench_replays_episodes() -> None:
    import httpx

    from workbench.lab import serving_bench

    seen: list[dict[str, Any]] = []
    counters = {"q": 0.0, "h": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            q, h = counters["q"], counters["h"]
            text = (
                f'vllm:prefix_cache_queries_total{{model_name="m"}} {q}\n'
                f'vllm:prefix_cache_hits_total{{model_name="m"}} {h}\n'
            )
            return httpx.Response(200, text=text)
        body = json.loads(request.content)
        seen.append(body)
        counters["q"] += 100
        counters["h"] += 60
        chunks = [
            {"choices": [{"delta": {"content": "x"}}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 4}},
        ]
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    tool = {"type": "function", "function": {"name": "t", "parameters": {}}}
    episodes = [[{"messages": [{"role": "user", "content": "u"}], "tools": [tool]}] * 3] * 4
    out = await serving_bench.run_setting(
        "http://srv/v1", "m", episodes, 2, max_tokens=4, transport=httpx.MockTransport(handler)
    )
    assert out["requests"] == 12 and out["errors"] == 0
    assert out["prefix_cache_hit_rate"] == pytest.approx(0.6)
    assert out["prompt_tokens_mean"] == 50
    assert all(b["ignore_eos"] and b["tool_choice"] == "none" and b["max_tokens"] == 4 for b in seen)
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_null_agent_touches_nothing(tmp_path: Path) -> None:
    from workbench.lab.episodes import NullBackend

    r = (await _run(tmp_path, backend=NullBackend()))[0]
    assert r["status"] == "ok" and r["tool_calls"] == 0 and r["db_diff"] == {}
    assert r["success"] is False  # the fixture's verifier needs a cart item


def _fake_run(root: Path, tag: str, rows: list[dict[str, Any]]) -> Path:
    from workbench.lab.recorder import save_recording

    d = root / "runs" / tag
    d.mkdir(parents=True)
    with (d / "results.jsonl").open("w") as f:
        for r in rows:
            r = {"tag": tag, "status": "ok", "sample": 0, "split": "test", **r}
            f.write(json.dumps(r) + "\n")
            rec = {"tools": [], "calls": [_call([{"role": "user", "content": r["run_id"]}], content="a")]}
            save_recording(d / "calls" / f"{r['run_id']}.json.gz", rec)
    return d


def test_sft_build_filters_caps_and_subsamples(tmp_path: Path) -> None:
    rows = [
        {"run_id": f"s{i}__0__s0", "scenario": f"s{i}", "task_id": 0, "success": i % 2 == 0}
        for i in range(10)
    ]
    d = _fake_run(tmp_path, "teacher", rows)
    ok = sft_data.build([d], tmp_path / "a.jsonl")
    assert ok["used_episodes"] == 5 and ok["used_successful_episodes"] == 5
    every = sft_data.build([d], tmp_path / "b.jsonl", include_failed=True, max_episodes=5)
    assert every["used_episodes"] == 5 and every["subsampled_out"] == 5
    again = sft_data.build([d], tmp_path / "c.jsonl", include_failed=True, max_episodes=5)
    assert (tmp_path / "b.jsonl").read_text() == (tmp_path / "c.jsonl").read_text()  # seeded
    capped = sft_data.build([d, d], tmp_path / "d.jsonl", max_per_task=1)
    assert capped["used_episodes"] == 5 and capped["capped"] == 5
    assert again["tasks"] == 5


def test_report_excludes_tasks_the_verifier_passes_without_action(tmp_path: Path) -> None:
    def rows(succ: list[int]) -> list[dict[str, Any]]:
        return [
            {"run_id": f"s{i}__0__s0", "scenario": f"s{i}", "task_id": 0, "success": i in succ}
            for i in range(6)
        ]

    _fake_run(tmp_path, "null-test", rows([0]))
    _fake_run(tmp_path, "base-test", rows([0, 1]))
    _fake_run(tmp_path, "sft-teacher-test", rows([0, 1, 2, 3]))
    (tmp_path / "sft" / "teacher").mkdir(parents=True)
    doc = collect(tmp_path)
    assert doc["verifier_floor"]["pass_without_action"] == 1
    assert doc["runs"]["base-test"]["nontrivial"]["n"] == 5
    assert doc["runs"]["base-test"]["nontrivial"]["success"] == 1
    p = doc["sft"]["teacher"]["test_paired"]
    assert p["tasks"] == 6 and p["only_b"] == 2
    assert p["excluding_trivial"]["tasks"] == 5 and p["excluding_trivial"]["b_success"] == 3
    text = render(doc)
    assert "verifier 下限" in text and "去掉不做任何操作也能通过" in text


def test_trace_hub_forget() -> None:
    from workbench.obs.tracing import TraceHub

    hub = TraceHub(None)
    hub.emit("s", "x")
    assert hub.events("s")
    hub.forget("s")
    assert hub.events("s") == []


def test_inject_split_only_uses_test_scenarios_with_destructive_tools() -> None:
    tasks = {f"s{i}": [f"t{i}.{j}" for j in range(3)] for i in range(12)}
    verifiers = {(s, j): {} for s in tasks for j in range(3)}
    kw: dict[str, Any] = {"scenario_counts": {"test": 4, "val": 2}, "task_counts": {"inject": 5}}
    base = splits.make_split(tasks, verifiers, seed=3, **kw)
    test_scen = {t.scenario for t in base["test"]}
    risky = {sorted(test_scen)[0], sorted(test_scen)[1], "s_not_in_test"}
    out = splits.make_split(tasks, verifiers, seed=3, inject_scenarios=risky, **kw)
    assert {t.scenario for t in out["inject"]} <= risky & test_scen
    assert len(out["inject"]) == 5 and all(t.split == "inject" for t in out["inject"])
    assert out["train"] == base["train"] and out["test"] == base["test"]  # inject changes nothing else


def test_destructive_scenarios_on_the_mini_fixture() -> None:
    found = splits.destructive_scenarios(MINI, Path("configs/tool_policy.yaml"))
    assert found == {"mini_e_commerce"}  # delete_user_payment_method


def test_train_extra_follows_train_without_overlap() -> None:
    tasks = {f"s{i}": [f"t{i}.{j}" for j in range(4)] for i in range(10)}
    verifiers = {(s, j): {} for s in tasks for j in range(4)}
    kw: dict[str, Any] = {"scenario_counts": {"test": 2, "val": 2}}
    out = splits.make_split(tasks, verifiers, seed=1, task_counts={"train": 10, "train_extra": 8}, **kw)
    plain = splits.make_split(tasks, verifiers, seed=1, task_counts={"train": 10}, **kw)
    assert out["train"] == plain["train"]  # the reserve does not change the train split
    assert len(out["train_extra"]) == 8 and all(t.split == "train_extra" for t in out["train_extra"])
    assert not {t.key for t in out["train"]} & {t.key for t in out["train_extra"]}
    test_scen = {t.scenario for t in out["test"]}
    assert not {t.scenario for t in out["train_extra"]} & test_scen


def test_sft_build_leaves_out_trivial_tasks(tmp_path: Path) -> None:
    from workbench.lab.episodes import trivial_tasks

    rows = [{"run_id": f"s{i}__0__s0", "scenario": f"s{i}", "task_id": 0, "success": True} for i in range(4)]
    d = _fake_run(tmp_path, "teacher", rows)
    null = _fake_run(tmp_path, "null-train", [{**rows[0]}, {**rows[1], "success": False}])
    trivial = trivial_tasks([null, tmp_path / "runs" / "missing"])
    assert trivial == {"s0#0"}
    stats = sft_data.build([d], tmp_path / "a.jsonl", exclude_tasks=trivial)
    assert stats["used_episodes"] == 3 and stats["excluded_trivial"] == 1


def test_select_ignores_trivial_val_tasks(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from workbench.cli import app

    def rows(succ: list[int]) -> list[dict[str, Any]]:
        return [
            {
                "run_id": f"s{i}__0__s0",
                "scenario": f"s{i}",
                "task_id": 0,
                "success": i in succ,
                "split": "val",
            }
            for i in range(5)
        ]

    _fake_run(tmp_path, "null-val", rows([0, 1]))
    _fake_run(tmp_path, "sft-v-val-ckpt-2", rows([0, 1, 2]))  # 3/5 overall, 1/3 non-trivial
    _fake_run(tmp_path, "sft-v-val-ckpt-4", rows([2, 3]))  # 2/5 overall, 2/3 non-trivial
    res = CliRunner().invoke(app, ["lab", "select", "--variant", "v", "--lab-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    sel = json.loads((tmp_path / "sft" / "v" / "selected.json").read_text())
    assert sel["checkpoint"] == "ckpt-4" and sel["val_n"] == 3 and sel["val_trivial_excluded"] == 2


def test_report_process_metrics(tmp_path: Path) -> None:
    base = [
        {
            "run_id": f"s{i}__0__s0",
            "scenario": f"s{i}",
            "task_id": 0,
            "success": False,
            "steps": 4,
            "tool_errors": 1,
        }
        for i in range(4)
    ]
    base[0]["termination"] = {"reason": "plan_invalid"}
    _fake_run(tmp_path, "base-test", base)
    doc = collect(tmp_path)
    m = doc["process"]["base-test"]
    assert m["episodes"] == 4 and m["share_plan_invalid"] == 0.25 and m["tool_errors_per_episode"] == 1
    assert "过程指标" in render(doc)


def test_llm_unavailable_is_told_apart_from_model_errors() -> None:
    from workbench.lab.episodes import llm_unavailable

    for detail in (
        "PoolTimeout: ",
        "ConnectError: refused",
        "ReadTimeout: x",
        "HTTP 503: busy",
        "HTTP 429: slow",
    ):
        assert llm_unavailable({"reason": "llm_error", "detail": detail}), detail
    assert not llm_unavailable({"reason": "llm_error", "detail": "HTTP 400: maximum context length"})
    assert not llm_unavailable({"reason": "max_steps", "detail": "PoolTimeout"})
    assert not llm_unavailable(None)


async def test_infrastructure_failures_are_retried_within_the_run(tmp_path: Path, monkeypatch: Any) -> None:
    from workbench.lab import episodes
    from workbench.verify import VerifyError

    calls = {"n": 0}

    def flaky(settings: Settings, run_dir: Path, **kw: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise VerifyError("awm verify did not finish (rc=1)")
        return code_verify(settings, run_dir, **kw)

    async def no_wait(_: float) -> None:
        return None

    from workbench.llm.backends.mock_replay import MockReplayBackend

    class Rewinding(MockReplayBackend):
        """The scripted episode again for every retry."""

        async def chat(self, *a: Any, **kw: Any) -> Any:
            if self.remaining == 0:
                self.reset()
            return await super().chat(*a, **kw)

    monkeypatch.setattr(episodes.asyncio, "sleep", no_wait)
    backend = Rewinding(FIX / "trajectories" / "demo_query_write_approve.jsonl")
    rows = await _run(tmp_path, verify_fn=flaky, backend=backend)
    assert calls["n"] == 2
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["success"] is True


def test_db_diff_with_blob_keys_is_written(tmp_path: Path) -> None:
    from workbench.lab.episodes import _dumps, json_safe

    diff = {
        "items_fts_idx": {
            "rows_before": 8,
            "rows_after": 9,
            "added": [(8, b"")],
            "removed": [],
            "changed": [],
        }
    }
    record = {"run_id": "x", "db_diff": diff, "blob": b"\x01\xff"}
    assert json_safe(diff)["items_fts_idx"]["added"] == [[8, "0x"]]
    row = json.loads(_dumps(record))
    assert row["blob"] == "0x01ff" and row["db_diff"]["items_fts_idx"]["added"] == [[8, "0x"]]
