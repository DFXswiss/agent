"""U-DBL-model: response shapes of the model double (messages, events, usage, OpenRouter extras, errors)."""

from __future__ import annotations

from typing import Any

from agent_doubles.model import wire
from agent_doubles.model.scenario import ErrorReply, Reply


def test_blocks_events_and_estimates() -> None:
    blocks = wire.resolve_blocks(
        [
            {"type": "text", "text": ""},
            {"type": "tool_use", "name": "R"},
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "name": "W", "id": "fixed", "input": {"a": 1}},
            {"type": "thinking", "thinking": "x", "signature": "sig"},
        ],
        "toolu_x",
    )
    assert blocks[1] == {"type": "tool_use", "id": "toolu_x_1", "name": "R", "input": {}}
    assert blocks[2]["signature"] == "double-signature-2"
    assert blocks[3]["id"] == "fixed"
    assert blocks[4]["signature"] == "sig"
    events = wire.stream_events(
        "msg", "m", blocks[:3], "tool_use", {"input_tokens": 1, "output_tokens": 2}, chunk_size=2
    )
    names = [e[0] for e in events]
    assert names[:2] == ["message_start", "ping"]
    assert names[-2:] == ["message_delta", "message_stop"]
    deltas = [e[1]["delta"]["type"] for e in events if e[0] == "content_block_delta"]
    assert deltas == ["text_delta", "input_json_delta", "thinking_delta", "thinking_delta", "signature_delta"]
    assert events[0][1]["message"]["usage"]["output_tokens"] == 1
    assert "provider" not in events[0][1]["message"]
    assert wire.estimate_tokens("abcde") == 2
    assert wire.estimate_tokens({"a": 1}) == 2
    assert wire.request_tokens({}) == wire.estimate_tokens({"system": None, "messages": None, "tools": None})


def test_stop_reason_and_usage() -> None:
    text = [{"type": "text", "text": "x"}]
    tool = [{"type": "tool_use", "id": "t", "name": "R", "input": {}}]
    assert wire.stop_reason(Reply(content=()), text) == "end_turn"
    assert wire.stop_reason(Reply(content=()), tool) == "tool_use"
    assert wire.stop_reason(Reply(content=(), stop_reason="max_tokens"), tool) == "max_tokens"
    usage = wire.usage_for(Reply(content=(), usage={"output_tokens": 3, "extra": 1}), {"messages": []}, text)
    assert usage == {
        "input_tokens": wire.request_tokens({"messages": []}),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 3,
        "extra": 1,
    }


def test_openrouter_extras() -> None:
    usage: dict[str, Any] = {
        "input_tokens": 1000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 100,
        "output_tokens": 10,
    }
    priced = wire.openrouter_usage(usage, None)
    assert priced["cost"] == round(1000 * 1e-7 + 100 * 1e-8 + 10 * 4e-7, 12)
    assert priced["is_byok"] is False
    assert priced["cost_details"]["upstream_inference_completions_cost"] == round(10 * 4e-7, 12)
    fixed = wire.openrouter_usage(usage, 0.5)
    assert fixed["cost"] == 0.5
    assert fixed["cost_details"] == {
        "upstream_inference_cost": 0.5,
        "upstream_inference_prompt_cost": 0.5,
        "upstream_inference_completions_cost": 0.0,
    }
    events = wire.stream_events("m", "x", [], "end_turn", priced, chunk_size=1, start_usage=usage, provider="P")
    start = events[0][1]["message"]
    assert start["provider"] == "P"
    assert "cost" not in start["usage"]
    assert events[-2][1]["usage"]["cost"] == priced["cost"]
    assert wire.message("i", "m", [], "end_turn", usage, provider="P")["provider"] == "P"


def test_errors_and_encoding() -> None:
    assert wire.error_for(ErrorReply(529, "overloaded_error", "busy"), "req") == {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "busy"},
        "request_id": "req",
    }
    assert wire.encode_event("ping", {"type": "ping"}) == b'event: ping\ndata: {"type":"ping"}\n\n'
