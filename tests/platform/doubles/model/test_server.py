"""U-DBL-model: the model double over real HTTP - routes, recorded wire forms, errors, access kinds, control routes."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.model.access import Access
from agent_doubles.model.journal import summarize_endpoints, unknown_names
from agent_doubles.model.scenario import parse_scenario
from agent_doubles.model.server import ModelDouble

FIXTURES = Path(__file__).resolve().with_name("fixtures")
MESSAGES = "/v1/messages?beta=true"
TOKEN = "double-test-key"  # not a secret: the double only compares it
TEXT: dict[str, Any] = {"reply": {"content": [{"type": "text", "text": "hello world"}]}}
OK: dict[str, Any] = {"reply": {"content": [{"type": "text", "text": "SPIKE-OK"}]}}

Start = Callable[..., tuple[ModelDouble, DoubleClient]]
User = Callable[..., dict[str, Any]]
Sse = Callable[[bytes], list[tuple[str, dict[str, Any]]]]


def raw_request(double: ModelDouble, data: bytes) -> bytes:
    host, port = double.url.removeprefix("http://").rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


# --- recorded forms (Spike C) -------------------------------------------------------------------


def test_wire_forms_are_byte_identical_to_the_spike(start_model: Start, fixture_json: Callable[[str], Any]) -> None:
    """Every answer form equals, byte for byte, what the Spike C double sent to Claude Code 2.1.330."""
    recorded = fixture_json("wire-forms.json")
    double, client = start_model(scenario=recorded["scenario"])
    for request, expected in zip(recorded["requests"], recorded["responses"], strict=True):
        response = client.request("POST", MESSAGES, request["body"])
        assert response.status == expected["status"], request["step"]
        assert response.headers["content-type"] == expected["content_type"]
        assert response.headers.get("retry-after") == expected["retry_after"]
        assert response.body == (FIXTURES / expected["file"]).read_bytes(), request["step"]
    assert double.state()["ok"]


ERROR_CASES: dict[str, dict[str, Any]] = {
    "auth_401": {"token": "other-key", "steps": [{"id": "never", **OK}]},
    "stream_error_then_ok": {
        "steps": [
            {
                "id": "stream_err",
                "error": {"status": 529, "type": "overloaded_error", "message": "mid", "in_stream": True},
            },
            {"id": "ok", **OK},
        ]
    },
    "http_500_then_ok": {
        "steps": [{"id": "e500", "error": {"status": 500, "type": "api_error", "message": "boom"}}, {"id": "ok", **OK}]
    },
    "rate_limit_429_then_ok": {
        "steps": [
            {"id": "e429", "error": {"status": 429, "type": "rate_limit_error", "headers": {"retry-after": "1"}}},
            {"id": "ok", **OK},
        ]
    },
    "overloaded_persistent": {
        "steps": [{"id": f"e529_{i}", "error": {"status": 529, "type": "overloaded_error"}} for i in range(3)]
        + [{"id": "unused", **OK}]
    },
    "invalid_request_400": {
        "steps": [{"id": f"e400_{i}", "error": {"status": 400, "type": "invalid_request_error"}} for i in range(6)]
        + [{"id": "unused", **OK}]
    },
    "prompt_too_long": {
        "steps": [
            {
                "id": "too_long",
                "error": {
                    "status": 400,
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 1200000 tokens > 1000000 maximum",
                },
            },
            {"id": "unused", **OK},
        ]
    },
    "mcp_tool_error": {
        "steps": [
            {"id": "call_fail", "reply": {"content": [{"type": "tool_use", "name": "mcp__spike__fail", "input": {}}]}},
            {"id": "after", "expect": {"tool_result_is_error": True, "tool_result_contains": "refused"}, **OK},
        ],
        "result": {"tool_use_id": "toolu_double_call_fail_0", "content": "refused: scripted", "is_error": True},
    },
    "unknown_tool": {
        "steps": [
            {
                "id": "call_bash",
                "reply": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]},
            },
            {"id": "after", "expect": {"tool_result_is_error": True}, **OK},
        ],
        "result": {"tool_use_id": "toolu_double_call_bash_0", "content": "No such tool", "is_error": True},
    },
    "max_tokens": {
        "steps": [
            {"id": "cut", "reply": {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"}},
            {"id": "maybe", **OK},
        ]
    },
}


def test_every_recorded_error_form(start_model: Start, fixture_json: Callable[[str], Any], make_user: User) -> None:
    """Each case of Spike C's double-errors.json: the same requests get the recorded statuses and consume the steps."""
    recorded = fixture_json("double-errors.json")
    assert sorted(case["name"] for case in recorded) == sorted(ERROR_CASES)
    for case in recorded:
        spec = ERROR_CASES[case["name"]]
        double, client = start_model(*spec["steps"], access=Access("api_key", TOKEN))
        headers = {"x-api-key": spec.get("token", TOKEN)}
        statuses = []
        for index in range(case["requests"]):
            body = make_user("SPIKE-ERR: go", stream=True)
            if index and "result" in spec:
                body["messages"].append({"role": "user", "content": [{"type": "tool_result", **spec["result"]}]})
            statuses.append(client.request("POST", MESSAGES, body, headers).status)
        state = double.state()
        assert statuses == case["statuses"], case["name"]
        assert state["consumed"] == case["consumed"], case["name"]
        assert state["requests"] == case["requests"]


def test_recorded_request_shape_is_accepted(start_model: Start, fixture_json: Callable[[str], Any]) -> None:
    """A request shaped as Spike C recorded it from Claude Code 2.1.330 passes, and its summary matches the record."""
    recorded = fixture_json("double-endpoints.json")
    shape = recorded["POST /v1/messages?beta=true"]
    headers = {
        name: values[0]
        for name, values in shape["headers"].items()
        if name not in {"connection", "host", "content-length", "accept-encoding"}
    }
    headers["x-api-key"] = TOKEN
    headers["x-claude-code-session-id"] = "00000000-0000-4000-8000-000000000001"
    headers["x-stainless-retry-count"] = "0"
    body = {key: json.loads(values[0]) for key, values in shape["fields"].items() if key in shape["body_keys"]}
    body.update(
        system=[
            {"type": "text", "text": "billing"},
            {"type": "text", "text": "identity", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "main", "cache_control": {"type": "ephemeral"}},
        ],
        messages=[
            {"role": "system", "content": [{"type": "text", "text": "Primary working directory: /w"}]},
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
        ],
        tools=[{"name": "Read", "description": "d", "input_schema": {}, "defer_loading": True}],
        metadata={"user_id": "{}"},
    )
    assert sorted(body) == shape["body_keys"]
    double, client = start_model(
        {"expect": {"headers": {"anthropic-version": "2023-06-01", "x-app": "cli"}, "stream": True}, **TEXT},
        access=Access("api_key", TOKEN),
    )
    assert client.request("POST", MESSAGES, body, headers).status == 200
    summary = summarize_endpoints(double.exchanges)
    entry = summary["POST /v1/messages?beta=true"]
    assert entry["body_keys"] == shape["body_keys"]
    assert set(shape["headers"]) - set(entry["headers"]) <= {"connection", "host", "accept-encoding"}
    assert entry["headers"]["x-api-key"] == shape["headers"]["x-api-key"]
    assert entry["fields"]["system_blocks_with_cache_control"] == ["2"]
    assert entry["fields"]["tool_keys"] == shape["fields"]["tool_keys"]
    assert unknown_names(summary, recorded) == []
    assert double.state()["ok"]


# --- conversation, journal, refusals ------------------------------------------------------------


def journal_text(path: Path, lines: int, timeout: float = 5.0) -> str:
    """The journal file once it holds `lines` lines: the double appends an entry after it has answered."""
    deadline = time.monotonic() + timeout
    while True:
        text = path.read_text() if path.exists() else ""
        if len(text.splitlines()) >= lines or time.monotonic() > deadline:
            return text
        time.sleep(0.01)


def test_stream_tool_use_and_journal(start_model: Start, make_user: User, parse_sse: Sse, tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    double, client = start_model(
        {
            "id": "s1",
            "expect": {"stream": True},
            "reply": {
                "content": [
                    {"type": "text", "text": "in ${cwd}"},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "${cwd}/a"}},
                ]
            },
        },
        {
            "id": "s2",
            "expect": {"tool_result_contains": "done"},
            "reply": {
                "content": [{"type": "text", "text": "ok"}],
                "model": "other",
                "usage": {"output_tokens": 3, "x": 1},
            },
        },
        journal=journal,
    )
    body = make_user("x", stream=True)
    body["messages"].append({"role": "system", "content": [{"type": "text", "text": "Primary working directory: /w"}]})
    response = client.request("POST", MESSAGES, body, {"authorization": "Bearer secret-value"})
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["request-id"] == "req_double_0001"
    events = parse_sse(response.body)
    start = events[0][1]["message"]
    assert start["id"] == "msg_double_0001"
    assert start["usage"]["output_tokens"] == 1
    assert "".join(e[1]["delta"].get("text", "") for e in events if e[0] == "content_block_delta") == "in /w"
    assert events[-2][1]["delta"]["stop_reason"] == "tool_use"
    follow = make_user("x")
    follow["messages"].append(
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_double_s1_1", "content": "done"}]}
    )
    message = client.request("POST", MESSAGES, follow).json()
    assert message["model"] == "other"
    assert message["usage"]["x"] == 1
    assert message["stop_reason"] == "end_turn"
    assert "x-generation-id" not in response.headers
    assert double.state()["ok"]
    text = journal_text(journal, 2)
    lines = [json.loads(x) for x in text.splitlines()]
    assert lines[0]["headers"]["authorization"] == "Bearer <redacted 12 chars>"
    assert "secret-value" not in text
    assert [x["step"] for x in lines] == ["s1", "s2"]
    assert all(x["double"] == "model" for x in lines)


def test_refusals_side_steps_and_routes(start_model: Start, make_user: User) -> None:
    _, client = start_model(
        scenario={
            "name": "t",
            "steps": [{"id": "main", "expect": {"user_text_contains": "main"}, **TEXT}],
            "side": [
                {
                    "id": "bg",
                    "expect": {"user_text_contains": "title"},
                    "capture": {"t": {"regex": "title (\\w+)"}},
                    "reply": {"content": [{"type": "text", "text": "T ${t}"}]},
                }
            ],
        }
    )
    hello = client.request("HEAD", "/api/hello")
    assert (hello.status, hello.body) == (200, b"")
    assert client.request("GET", "/api/hello").json() == {"ok": True}
    assert client.request("POST", "/v1/messages/count_tokens", make_user("abcd")).json()["input_tokens"] > 0
    assert client.request("POST", MESSAGES, make_user("title x")).json()["content"][0]["text"] == "T x"
    response = client.request("POST", MESSAGES, make_user("wrong"))
    assert response.status == 400
    assert "step main" in response.json()["error"]["message"]
    assert client.request("POST", MESSAGES, b"not json").status == 400
    assert client.request("POST", "/v1/messages/count_tokens", b"[]").status == 400
    assert client.request("POST", MESSAGES, make_user("main")).status == 200
    assert client.request("POST", MESSAGES, make_user("main")).status == 400
    for method, path in (
        ("GET", "/v1/models"),
        ("PUT", "/v1/x"),
        ("DELETE", "/v1/x"),
        ("PATCH", "/v1/x"),
        ("GET", MESSAGES),
    ):
        unknown = client.request(method, path)
        assert unknown.status == 404
        assert unknown.json()["error"]["type"] == "not_found_error"
    follow = make_user("x")
    follow["messages"][0]["content"] = [{"type": "tool_result", "tool_use_id": "nope", "content": "x"}]
    client.request("POST", MESSAGES, follow)
    state = client.state()
    assert state["done"]
    assert not state["ok"]
    assert len(state["failures"]) == 11
    assert any(f.startswith("unhandled route GET /v1/models") for f in state["failures"])
    assert "tool_result for unknown id nope" in state["failures"]
    assert state["requests"] == 15
    journal = client.journal()
    assert [e["path"] for e in journal[:2]] == ["/api/hello", "/api/hello"]
    models = next(e for e in journal if e["path"] == "/v1/models")
    assert (models["status"], models["note"]) == (404, "unhandled route GET /v1/models")


def test_openrouter_key_document_is_a_background_read(start_model: Start) -> None:
    """`GET /v1/key` (the runner's spend poll of the platform key, every 900 s) answers the OpenRouter key document
    with zero spend; it is journaled but not counted, so an idle double stays idle. Without a bearer token it is
    refused like OpenRouter does, and that is a failure."""
    _, client = start_model({"id": "only", **TEXT})
    key = client.request("GET", "/v1/key", headers={"Authorization": "Bearer platform-key"})
    assert key.status == 200
    assert key.json() == {
        "data": {
            "label": "double-platform-key",
            "usage": 0,
            "usage_daily": 0,
            "usage_weekly": 0,
            "usage_monthly": 0,
            "limit": None,
            "limit_remaining": None,
            "limit_reset": None,
            "is_free_tier": False,
        }
    }
    state = client.state()
    assert (state["requests"], state["failures"]) == (0, [])
    assert [(e["path"], e["status"]) for e in client.journal()] == [("/v1/key", 200)]
    assert "platform-key" not in json.dumps(client.journal())
    refused = client.request("GET", "/v1/key")
    assert refused.status == 401
    assert refused.json()["error"]["type"] == "authentication_error"
    assert client.state()["failures"] == ["key document requested without a bearer token"]
    assert client.request("POST", "/v1/key").status == 404


def test_key_document_checks_the_configured_platform_key(serve: Any, client_for: Any) -> None:
    """With the platform key configured (the run's key file of the runners), `GET /v1/key` answers only that exact
    bearer token; a different token is refused like OpenRouter does (401) and recorded as a failure. The key is never
    journaled."""
    double = serve(ModelDouble(parse_scenario({"name": "t", "steps": [{"id": "only", **TEXT}]}), key_token="run-key"))
    client = client_for(double)
    assert client.request("GET", "/v1/key", headers={"Authorization": "Bearer run-key"}).status == 200
    assert client.state()["failures"] == []
    wrong = client.request("GET", "/v1/key", headers={"Authorization": "Bearer other-key"})
    assert (wrong.status, wrong.json()["error"]["type"]) == (401, "authentication_error")
    assert client.state()["failures"] == ["key document requested with a wrong bearer token"]
    assert client.request("GET", "/v1/key", headers={"Authorization": "Bearer run-key-longer"}).status == 401
    assert "run-key" not in json.dumps(client.journal())


def test_errors_and_template_failure(start_model: Start, make_user: User, parse_sse: Sse) -> None:
    double, client = start_model(
        {"id": "e1", "error": {"status": 529, "type": "overloaded_error", "message": "busy in ${cwd}"}},
        {"id": "e2", "error": {"status": 529, "type": "overloaded_error", "in_stream": True}},
        {"id": "e3", "error": {"status": 500, "type": "api_error", "in_stream": True}},
        {"id": "bad", "reply": {"content": [{"type": "text", "text": "${missing}"}]}},
    )
    body = make_user("Primary working directory: /w")
    assert client.request("POST", MESSAGES, body).json()["error"]["message"] == "busy in /w"
    events = parse_sse(client.request("POST", MESSAGES, make_user("x", stream=True)).body)
    assert [e[0] for e in events] == ["message_start", "error"]
    assert client.request("POST", MESSAGES, make_user("x")).status == 500
    for _ in range(2):
        response = client.request("POST", MESSAGES, make_user("x"))
        assert response.status == 500
        assert response.json()["error"]["type"] == "api_error"
        assert "unknown value" in response.json()["error"]["message"]
    state = double.state()
    assert state["remaining"] == ["bad"]
    assert state["failures"] == ["step bad: template names unknown value ${missing}"] * 2
    assert client.request("POST", MESSAGES, make_user("x")).status == 500
    fixed = {"name": "fixed", "steps": [{"reply": {"content": [{"type": "text", "text": "${missing}"}]}}]}
    assert client.scenario({"scenario": fixed, "variables": {"missing": "found"}}).status == 200
    assert client.request("POST", MESSAGES, make_user("x")).json()["content"][0]["text"] == "found"
    assert double.state()["ok"]


def test_delay_and_thinking(start_model: Start, make_user: User, parse_sse: Sse) -> None:
    double, client = start_model(
        {"id": "slow", "delay_ms": 5, "reply": {"content": [{"type": "thinking", "thinking": "t"}]}}
    )
    streamed = parse_sse(client.request("POST", MESSAGES, make_user("x", stream=True)).body)
    assert any(e[1].get("delta", {}).get("type") == "signature_delta" for e in streamed)
    assert double.state()["ok"]


def test_stop_ends_a_delayed_answer_in_flight(start_model: Start, make_user: User) -> None:
    double, client = start_model(
        {"id": "slow", "delay_ms": 600_000, "reply": {"content": [{"type": "text", "text": "late"}]}}
    )
    outcome: list[object] = []

    def ask() -> None:
        try:
            outcome.append(client.request("POST", MESSAGES, make_user("x")).status)
        except OSError as error:
            outcome.append(error)

    asking = threading.Thread(target=ask)
    asking.start()
    deadline = time.monotonic() + 10
    while double.state()["remaining"] and time.monotonic() < deadline:  # the step is chosen, its delay runs
        time.sleep(0.01)
    started = time.monotonic()
    double.stop()
    asking.join(30)
    assert not asking.is_alive()
    assert time.monotonic() - started < 30  # not the 600 s of the delay
    assert len(outcome) == 1
    assert not isinstance(outcome[0], int)  # the connection closed without an answer


def test_tool_ids_stay_unique(start_model: Start, make_user: User) -> None:
    """A repeated step and a side step answering again issue fresh tool ids, as the real API would."""
    tool = {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}
    double, client = start_model(
        scenario={
            "name": "ids",
            "steps": [{"id": "again", "repeat": 2, "reply": tool}],
            "side": [{"id": "bg", "expect": {"has_tools": False}, "reply": tool}],
        }
    )
    with_tools = make_user("x", tools=[{"name": "Read"}])
    ids = [client.request("POST", MESSAGES, with_tools).json()["content"][0]["id"] for _ in range(2)]
    ids += [client.request("POST", MESSAGES, make_user("bg")).json()["content"][0]["id"] for _ in range(2)]
    assert ids == ["toolu_double_again_0", "toolu_double_again_0002_0", "toolu_double_bg_0", "toolu_double_bg_0004_0"]
    assert double.state()["ok"]


def test_repeat_and_remaining(start_model: Start, make_user: User) -> None:
    double, client = start_model(
        {"id": "busy", "repeat": 2, "error": {"status": 529, "type": "overloaded_error"}},
        {"id": "ok", "repeat": 2, **TEXT},
    )
    assert double.state()["remaining"] == ["busy", "busy", "ok", "ok"]
    assert client.request("POST", MESSAGES, make_user("x")).status == 529
    assert double.state()["remaining"] == ["busy", "ok", "ok"]
    assert client.request("POST", MESSAGES, make_user("x")).status == 529
    assert client.request("POST", MESSAGES, make_user("x")).status == 200
    assert double.state()["remaining"] == ["ok"]
    assert client.request("POST", MESSAGES, make_user("x")).status == 200
    state = double.state()
    assert state["consumed"] == ["busy", "busy", "ok", "ok"]
    assert state["ok"]


def test_header_expectations(start_model: Start, make_user: User) -> None:
    double, client = start_model(
        {"expect": {"headers": {"anthropic-beta": "oauth-2025-04-20"}, "headers_absent": ["x-api-key"]}, **TEXT}
    )
    response = client.request("POST", MESSAGES, make_user("x"), {"anthropic-beta": "a", "x-api-key": "k"})
    assert response.status == 400
    assert double.failures == ["step step0: header anthropic-beta lacks 'oauth-2025-04-20'; header x-api-key present"]
    assert client.request("POST", MESSAGES, make_user("x"), {"anthropic-beta": "a,oauth-2025-04-20"}).status == 200


# --- access kinds -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("api_key", {"x-api-key": TOKEN}, {"authorization": f"Bearer {TOKEN}"}),
        ("openrouter", {"authorization": f"Bearer {TOKEN}"}, {"x-api-key": TOKEN}),
        (
            "subscription",
            {"authorization": f"Bearer {TOKEN}", "anthropic-beta": "claude-code-20250219,oauth-2025-04-20"},
            {"authorization": f"Bearer {TOKEN}", "anthropic-beta": "claude-code-20250219"},
        ),
    ],
)
def test_access_kinds(
    start_model: Start, make_user: User, kind: str, good: dict[str, str], bad: dict[str, str]
) -> None:
    double, client = start_model({"repeat": 2, **TEXT}, access=Access(kind, TOKEN))
    refused = client.request("POST", MESSAGES, make_user("x"), bad)
    assert refused.status == 401
    body = refused.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert body["request_id"] == refused.headers["request-id"]
    assert client.request("POST", "/v1/messages/count_tokens", make_user("x"), bad).status == 401
    assert client.request("POST", "/v1/messages/count_tokens", make_user("x"), good).status == 200
    assert client.request("HEAD", "/api/hello").status == 200
    assert client.request("POST", MESSAGES, make_user("x"), good).status == 200
    assert client.request("POST", MESSAGES, make_user("x"), good).status == 200
    state = double.state()
    assert state["done"]
    assert not state["ok"]
    assert len(state["failures"]) == 2
    assert all(TOKEN not in f for f in state["failures"])
    assert TOKEN not in json.dumps(client.journal())


# --- OpenRouter extras ----------------------------------------------------------------------------


def test_openrouter_extras(start_model: Start, make_user: User, parse_sse: Sse) -> None:
    double, client = start_model(
        scenario={
            "name": "or",
            "openrouter": {"provider": "Default"},
            "steps": [
                {"reply": {**TEXT["reply"], "cost": 0.25, "provider": "Fixed"}},
                {"reply": {**TEXT["reply"], "usage": {"input_tokens": 1000, "output_tokens": 10}}},
            ],
        }
    )
    response = client.request("POST", MESSAGES, make_user("x", stream=True))
    assert response.headers["x-generation-id"] == "gen-double-0001"
    events = parse_sse(response.body)
    assert events[0][1]["message"]["provider"] == "Fixed"
    assert "cost" not in events[0][1]["message"]["usage"]
    assert events[-2][1]["usage"]["cost"] == 0.25
    message = client.request("POST", MESSAGES, make_user("x")).json()
    assert message["provider"] == "Default"
    assert message["usage"]["cost"] == round(1000 * 1e-7 + 10 * 4e-7, 12)
    assert message["usage"]["cost_details"]["upstream_inference_cost"] == message["usage"]["cost"]
    assert double.state()["ok"]


# --- control routes -----------------------------------------------------------------------------


def test_control_routes(start_model: Start, make_user: User, tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    double, client = start_model({"id": "first", **TEXT}, journal=journal)
    assert client.request("POST", MESSAGES, make_user("x")).status == 200
    assert client.request("GET", "/_double/scenario").json() == {
        "name": "t",
        "description": "",
        "steps": ["first"],
        "side": [],
        "access": None,
    }
    loaded = client.scenario(
        {"packaged": "hello", "access": {"kind": "api_key", "token": TOKEN}, "variables": {"cwd": "/w"}}
    )
    assert loaded.status == 200
    assert loaded.json()["name"] == "hello"
    assert loaded.json()["access"] == "api_key"
    assert client.state()["requests"] == 0
    assert len(client.journal()) == 1
    response = client.request(
        "POST", MESSAGES, make_user("What is 2+2?", stream=True, tools=[{"name": "Read"}]), {"x-api-key": TOKEN}
    )
    assert response.status == 200
    assert client.state()["ok"]
    switched = client.scenario(
        {"scenario": {"name": "inline", "variables": {"v": "a"}, "steps": [TEXT]}, "access": None}
    )
    assert switched.json()["access"] is None
    assert client.scenario({"scenario": {"name": "keeps", "steps": [TEXT]}}).json()["access"] is None
    reset = client.reset()
    assert reset.status == 200
    assert reset.json()["requests"] == 0
    assert client.journal() == []
    assert len(journal.read_text().splitlines()) == 2
    for payload, message in (
        ({}, "exactly one of scenario and packaged"),
        ({"scenario": {}, "packaged": "hello"}, "exactly one"),
        ({"packaged": 1}, "packaged must be a scenario name"),
        ({"packaged": "nope"}, "no packaged scenario"),
        ({"scenario": {"name": "x"}}, "steps must be"),
        ({"packaged": "hello", "variables": []}, "variables must map"),
        ({"packaged": "hello", "variables": {"a": 1}}, "variables must map"),
        ({"packaged": "hello", "access": {"kind": "x", "token": "t"}}, "access kind"),
        ({"packaged": "hello", "extra": 1}, "payload must be an object"),
    ):
        refused = client.scenario(payload)
        assert refused.status == 400
        assert message in refused.json()["error"]
    assert client.request("POST", "/_double/scenario", b"[1]").status == 400
    assert client.request("GET", "/_double/nope").status == 404
    not_allowed = client.request("POST", "/_double/state")
    assert not_allowed.status == 405
    assert not_allowed.headers["allow"] == "GET"
    assert client.request("GET", "/_double/reset").status == 405
    assert double.describe()["name"] == "keeps"


def test_body_limits(start_model: Start) -> None:
    double, client = start_model(TEXT)
    bad = raw_request(double, b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: x\r\n\r\n")
    assert bad.startswith(b"HTTP/1.1 400")
    assert b"invalid content-length" in bad
    huge = raw_request(double, b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999999\r\n\r\n")
    assert huge.startswith(b"HTTP/1.1 413")
    assert b"request_too_large" in huge
    chunked = raw_request(
        double, b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    assert chunked.startswith(b"HTTP/1.1 411")
    control = raw_request(double, b"POST /_double/scenario HTTP/1.1\r\nHost: x\r\nContent-Length: x\r\n\r\n")
    assert control.startswith(b"HTTP/1.1 400")
    state = client.state()
    assert len(state["failures"]) == 3
    assert state["requests"] == 3


def test_context_manager() -> None:
    with ModelDouble(parse_scenario({"name": "t", "steps": [TEXT]})) as double:
        assert DoubleClient(double.url).request("GET", "/api/hello").status == 200


def test_serve_forever_in_calling_thread() -> None:
    double = ModelDouble(parse_scenario({"name": "t", "steps": [TEXT]}))
    thread = threading.Thread(target=double.serve_forever)
    thread.start()
    assert DoubleClient(double.url).request("GET", "/api/hello").status == 200
    double.stop()
    thread.join(5)
    assert not thread.is_alive()
