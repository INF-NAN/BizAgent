from pathlib import Path

import pytest

from workbench.llm.backends.mock_replay import FixtureFormatError, MockExhaustedError, MockReplayBackend

FIX = Path("tests/fixtures/trajectories/e_commerce_33_basic.jsonl")


async def test_replays_in_order_with_tool_calls() -> None:
    b = MockReplayBackend(FIX)
    assert "手写夹具" in b.header["_fixture"]
    first = await b.chat([{"role": "user", "content": "hi"}])
    assert first.tool_calls[0].name == "search_products"
    assert first.tool_calls[0].arguments["sort_by"] == "rating"
    assert first.usage.total_tokens == 138
    second = await b.chat([])
    assert second.tool_calls == []
    assert "Headphones B" in second.content
    with pytest.raises(MockExhaustedError):
        await b.chat([])


def test_header_required(tmp_path: Path) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text('{"content": "x"}\n', encoding="utf-8")
    with pytest.raises(FixtureFormatError):
        MockReplayBackend(p)


def test_all_fixtures_declare_hand_written() -> None:
    import json

    for f in Path("tests/fixtures/trajectories").glob("*.jsonl"):
        header = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        assert "手写夹具，非模型输出" in header["_fixture"], f
