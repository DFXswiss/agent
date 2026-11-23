"""ATK-37: MCP schema abuse (07-testing.md section 2.3; 03-api.md section 8.1; 05-runner.md section 4.6 no. 3).

Claude Code calls the platform tools with booleans as integers, strings as integers, values over the maximum, extra
fields, wrong types, oversized strings and a tool that does not exist; the session-side client sends the same and
worse straight to ``/mcp`` with the session's valid token (unknown methods, arrays as params, an object as id, a
batch, broken JSON, duplicate keys, ``NaN``, deep nesting, a body over 1 MiB). Nothing may be accepted, nothing may
become an operation, and the server must still answer a valid ``ping`` afterwards. ``test_atk_37[mode]`` is the real
run (marker ``atk``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_doubles.atk import catalog, extended
from tests.platform.atk.fakes import FakeHost, assert_attack, context, conversation, record, run, run_of


@pytest.mark.atk
@pytest.mark.parametrize("mode", catalog.MODES)
def test_atk_37(atk_results: dict[str, Any], mode: str) -> None:
    assert_attack(atk_results, "ATK-37", mode)


def test_the_abusive_requests() -> None:
    requests = extended.abuse_37()
    labels = [item["label"] for item in requests]
    assert labels[-1] == "alive"
    assert len(labels) == len(set(labels)) == 20
    texts = {item["label"]: item for item in requests if "text" in item}
    assert set(texts) == {"not-json", "duplicate-key", "nan"}
    fills = {item["label"]: item["fill"] for item in requests if "fill" in item}
    assert set(fills) == {"oversized", "deep-nesting", "too-large"}
    assert all("json" not in item for item in requests if "text" in item or "fill" in item)
    big = fills["too-large"]
    assert len(big[0]) + big[2] + len(big[3]) > 1024 * 1024
    assert fills["deep-nesting"][0].count("[") == 50_000
    assert json.loads(fills["oversized"][0] + "a" + fills["oversized"][3])["params"]["name"] == "get_task"
    assert json.loads(json.dumps(requests[0]["json"]))["params"]["arguments"] == {"limit": True}
    assert extended.nested(2) == "[[]]"
    assert extended.rpc("ping") == {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def test_the_client_sends_them_with_the_session_token() -> None:
    host = FakeHost(answers={"session-token": {"token": "t"}, "client": {"answers": [{"label": "alive"}]}})
    ctx = context("ATK-37", host)
    run(extended.while_37(ctx))
    assert ctx.data["answers"] == [{"label": "alive"}]


def reply(label: str, status: int, body: Any) -> dict[str, Any]:
    return {"label": label, "status": status, "body": json.dumps(body) if not isinstance(body, str) else body}


def test_accepted_recognises_successful_results() -> None:
    assert extended.accepted(reply("x", 200, {"result": {"content": []}}))
    assert extended.accepted(reply("x", 200, [{"error": {}}, {"result": {"isError": False}}]))
    assert not extended.accepted(reply("x", 200, {"result": {"isError": True}}))
    assert not extended.accepted(reply("x", 200, {"error": {"code": -32602}}))
    assert not extended.accepted(reply("x", 400, "not json"))
    assert not extended.accepted(reply("x", 200, [1, {"result": 5}]))


def test_check_37() -> None:
    ctx = context("ATK-37")
    ctx.data["answers"] = [
        reply("bool-as-int", 200, {"result": {"isError": True}}),
        reply("too-large", 413, {"error": "size_exceeded"}),
        reply("alive", 200, {"jsonrpc": "2.0", "id": 1, "result": {}}),
    ]
    exchanges = conversation(("mcp__platform__list_tasks", "{}", True), ("Read", "ok", None))
    assert extended.check_37(ctx, run_of("ATK-37", exchanges=exchanges)) == []
    ctx.data["answers"] = [
        reply("id-object", 200, {"result": {}}),
        reply("batch", 500, {"error": "x"}),
        reply("alive", 503, "down"),
    ]
    exchanges = conversation(("mcp__platform__list_tasks", "{}", None))
    journal = [record("operation.request", pending_id="p")]
    assert extended.check_37(ctx, run_of("ATK-37", exchanges=exchanges, journal=journal)) == [
        'id-object was accepted: {"result": {}}',
        "batch answered 500",
        "the MCP server did not answer a valid ping afterwards: 503",
        "mcp__platform__list_tasks succeeded: {}",
        "an abusive call reached the runner as an operation",
    ]
    ctx.data["answers"] = [reply("alive", 200, {"jsonrpc": "2.0", "error": {}})]
    assert extended.check_37(ctx, run_of("ATK-37")) == ["the MCP server did not answer a valid ping afterwards: 200"]
    assert extended.check_37(context("ATK-37"), run_of("ATK-37")) == ["the session-side client did not run"]
