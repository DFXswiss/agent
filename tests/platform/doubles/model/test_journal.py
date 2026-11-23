"""U-DBL-model: the per-route summary of the journal and the Claude Code upgrade guard (05-runner.md section 15)."""

from __future__ import annotations

from typing import Any

from agent_doubles.model.journal import VARIES, route_key, summarize_endpoints, unknown_names


def record(path: str, query: str = "", body: Any = None, **headers: str) -> dict[str, Any]:
    return {"method": "POST", "path": path, "query": query, "headers": headers, "body": body}


def test_summary_shapes() -> None:
    body = {
        "model": "m",
        "stream": True,
        "system": "plain",
        "messages": [{"role": "user", "content": "x"}, "junk", {"role": "assistant", "content": [{"type": "text"}, 1]}],
        "tools": [{"name": "Read", "input_schema": {}}, "junk"],
    }
    summary = summarize_endpoints(
        [
            record("/v1/messages", "beta=true", body, host="a", accept="*/*"),
            record("/v1/messages", "beta=true", "text body", host="b", accept="*/*"),
            record("/v1/messages/count_tokens", body={"messages": []}),
        ]
    )
    entry = summary["POST /v1/messages?beta=true"]
    assert entry["count"] == 2
    assert entry["headers"] == {"host": [VARIES], "accept": ["*/*"]}
    assert entry["body_keys"] == ["messages", "model", "stream", "system", "tools"]
    assert entry["fields"]["message_roles"] == ['["assistant", "user"]']
    assert entry["fields"]["message_block_types"] == ["text"]
    assert entry["fields"]["system_blocks_with_cache_control"] == ["0"]
    assert entry["fields"]["tool_keys"] == ["input_schema", "name"]
    assert summary["POST /v1/messages/count_tokens"]["fields"]["message_roles"] == ["[]"]
    assert route_key({"method": "GET", "path": "/x"}) == "GET /x"


def test_unknown_names_flags_new_routes_headers_and_fields() -> None:
    recorded = summarize_endpoints([record("/v1/messages", body={"model": "m"}, accept="*/*")])
    run = summarize_endpoints(
        [
            record("/v1/messages", body={"model": "m", "new_field": 1}, accept="*/*", **{"x-new": "1"}),
            record("/v1/files", body=None),
        ]
    )
    assert unknown_names(run, recorded) == [
        "header x-new on POST /v1/messages",
        "body key new_field on POST /v1/messages",
        "route POST /v1/files",
    ]
    assert unknown_names(recorded, recorded) == []
