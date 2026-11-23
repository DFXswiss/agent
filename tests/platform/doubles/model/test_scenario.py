"""U-DBL-model: scenario format, loading, matching, captures and templates of the model double."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_doubles.model.scenario import (
    Matcher,
    ScenarioError,
    capture_values,
    expand,
    load_scenario,
    messages_text,
    packaged_names,
    packaged_scenario,
    parse_scenario,
    tool_results,
)

TEXT: dict[str, Any] = {"reply": {"content": [{"type": "text", "text": "hello world"}]}}
REPLY = TEXT["reply"]
OR: dict[str, Any] = {"openrouter": {}}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "scenario must be an object"),
        ({"name": "x", "steps": [], "x": 1}, "unknown keys"),
        ({"name": "x", "steps": []}, "steps must be a non-empty list"),
        ({"name": "x", "steps": [TEXT], "side": {}}, "side must be a list"),
        ({"name": 1, "steps": [TEXT]}, "name must be a string"),
        ({"name": "x", "steps": [{"id": "a", **TEXT}, {"id": "a", **TEXT}]}, "ids must be unique"),
        ({"name": "x", "steps": [{}]}, "exactly one of reply and error"),
        ({"name": "x", "steps": [{**TEXT, "delay_ms": -1}]}, "delay_ms"),
        ({"name": "x", "steps": [{**TEXT, "repeat": 0}]}, "repeat"),
        ({"name": "x", "steps": [{**TEXT, "repeat": True}]}, "repeat"),
        ({"name": "x", "steps": [TEXT], "side": [{**TEXT, "repeat": 2}]}, "cannot repeat"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"bogus": 1}}]}, "unknown keys"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"tools_include": "Read"}}]}, "list of strings"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"stream": "yes"}}]}, "must be a boolean"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"has_tools": 1}}]}, "must be a boolean"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"min_messages": -1}}]}, "at least 0"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"headers": {"a": 1}}}]}, "map names to strings"),
        ({"name": "x", "steps": [{**TEXT, "expect": {"headers_absent": "a"}}]}, "list of strings"),
        ({"name": "x", "steps": [{**TEXT, "capture": []}]}, "capture must be an object"),
        ({"name": "x", "steps": [{**TEXT, "capture": {"a": {"regex": "x", "source": "nope"}}}]}, "source"),
        ({"name": "x", "steps": [{**TEXT, "capture": {"a": {"regex": "("}}}]}, "regex"),
        ({"name": "x", "steps": [{**TEXT, "capture": {"a": {"regex": "x"}}}]}, "needs a group"),
        ({"name": "x", "steps": [{**TEXT, "capture": {"a": "x"}}]}, "must be an object"),
        ({"name": "x", "steps": [{"reply": {"content": []}}]}, "non-empty list"),
        ({"name": "x", "steps": [{"reply": {"content": [{"type": "image"}]}}]}, "block of type"),
        ({"name": "x", "steps": [{"reply": {"content": ["text"]}}]}, "block of type"),
        ({"name": "x", "steps": [{"reply": {"content": [{"type": "text", "text": 1}]}}]}, "text must be"),
        ({"name": "x", "steps": [{"reply": {"content": [{"type": "text", "text": "a", "x": 1}]}}]}, "unknown keys"),
        (
            {"name": "x", "steps": [{"reply": {"content": [{"type": "tool_use", "name": "R", "input": []}]}}]},
            "input must be",
        ),
        ({"name": "x", "steps": [{"reply": {"content": [{"type": "tool_use", "name": "R", "id": 3}]}}]}, "id must be"),
        ({"name": "x", "steps": [{"reply": {"content": [{"type": "thinking"}]}}]}, "thinking must be"),
        (
            {"name": "x", "steps": [{"reply": {"content": [{"type": "thinking", "thinking": "t", "signature": 1}]}}]},
            "signature must be",
        ),
        ({"name": "x", "steps": [{"reply": {**REPLY, "stop_reason": "x"}}]}, "stop_reason"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "usage": {"a": -1}}}]}, "usage"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "usage": {"a": True}}}]}, "usage"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "usage": []}}]}, "usage"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "chunk_size": 0}}]}, "chunk_size"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "model": 1}}]}, "model must be"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "cost": 1}}]}, "need the scenario's openrouter"),
        ({"name": "x", "steps": [{"reply": {**REPLY, "provider": "p"}}]}, "need the scenario's openrouter"),
        ({"name": "x", **OR, "steps": [{"reply": {**REPLY, "cost": -1}}]}, "non-negative number"),
        ({"name": "x", **OR, "steps": [{"reply": {**REPLY, "cost": True}}]}, "non-negative number"),
        ({"name": "x", **OR, "steps": [{"reply": {**REPLY, "cost": "1"}}]}, "non-negative number"),
        ({"name": "x", **OR, "steps": [{"reply": {**REPLY, "provider": 2}}]}, "provider must be"),
        ({"name": "x", "openrouter": {"x": 1}, "steps": [TEXT]}, "unknown keys"),
        ({"name": "x", "openrouter": {"provider": 1}, "steps": [TEXT]}, "provider must be"),
        ({"name": "x", "steps": [{"error": {"status": 200, "type": "x"}}]}, "HTTP error status"),
        ({"name": "x", "steps": [{"error": {"status": True, "type": "x"}}]}, "HTTP error status"),
        ({"name": "x", "steps": [{"error": {"status": 500, "type": "x", "headers": []}}]}, "headers"),
        ({"name": "x", "steps": [{"error": {"status": 500, "type": "x", "in_stream": 1}}]}, "in_stream"),
        ({"name": "x", "steps": [{"error": {"status": 500, "type": 5}}]}, "type must be"),
        ({"name": "x", "variables": {"1a": "x"}, "steps": [TEXT]}, "invalid variable name"),
        ({"name": "x", "variables": {"a": 1}, "steps": [TEXT]}, "map names to strings"),
    ],
)
def test_parse_rejects(raw: Any, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        parse_scenario(raw)


def test_parse_full_scenario() -> None:
    scenario = parse_scenario(
        {
            "name": "full",
            "description": "d",
            "variables": {"v": "1"},
            "openrouter": {"provider": "P"},
            "steps": [
                {
                    "id": "a",
                    "repeat": 3,
                    "delay_ms": 5,
                    "expect": {
                        "headers": {"Anthropic-Version": "2023"},
                        "headers_absent": ["X-Api-Key"],
                        "has_tools": True,
                        "has_tool_result": False,
                        "min_messages": 1,
                    },
                    "reply": {
                        **REPLY,
                        "cost": 1,
                        "provider": "Q",
                        "chunk_size": 3,
                        "model": "mm",
                        "stop_reason": "end_turn",
                    },
                },
                {"error": {"status": 529, "type": "overloaded_error"}},
            ],
            "side": [TEXT],
        }
    )
    step = scenario.steps[0]
    assert (step.repeat, step.delay_ms) == (3, 5)
    assert step.expect.headers == (("anthropic-version", "2023"),)
    assert step.expect.headers_absent == ("x-api-key",)
    assert step.reply is not None
    assert (step.reply.cost, step.reply.provider, step.reply.chunk_size, step.reply.model) == (1.0, "Q", 3, "mm")
    error = scenario.steps[1].error
    assert error is not None
    assert (scenario.steps[1].id, error.message, error.in_stream) == ("step1", "overloaded_error", False)
    assert scenario.openrouter is not None
    assert scenario.openrouter.provider == "P"
    assert scenario.variables == {"v": "1"}
    assert scenario.side[0].id == "step0"
    assert parse_scenario({"name": "x", "openrouter": {}, "steps": [TEXT]}).openrouter is not None
    assert [c.name for c in parse_scenario({"name": "x", "steps": [TEXT]}).captures] == ["cwd"]


def test_load_scenario_files(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"name": "g", "steps": [{"reply": {"content": [{"type": "text", "text": "x"}]}}]}')
    assert load_scenario(good).name == "g"
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(ScenarioError, match=r"bad\.json"):
        load_scenario(bad)
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(ScenarioError):
        load_scenario(binary)


def test_packaged_scenarios() -> None:
    names = packaged_names()
    assert {"e2e-tools", "hello", "openrouter-usage", "atk-baseline"} <= set(names)
    assert [n for n in names if n.startswith("atk-") and n[4:].isdigit()] == [f"atk-{i:02d}" for i in range(1, 41)]
    for name in names:
        assert packaged_scenario(name).name == name
    for name in ("missing", "../hello", "Hello", ""):
        with pytest.raises(ScenarioError, match="no packaged scenario"):
            packaged_scenario(name)


def test_matcher_and_reading() -> None:
    body: dict[str, Any] = {
        "model": "m",
        "stream": True,
        "system": [{"type": "text", "text": "sys prompt"}, "junk"],
        "tools": [{"name": "Read"}, {"name": "Bash"}, "junk"],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "file body"}, 1]},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "plain", "is_error": True},
                    {"type": "tool_result", "tool_use_id": "t3"},
                    "junk",
                ],
            },
        ],
    }
    headers = {"anthropic-version": "2023-06-01", "x-api-key": "secret"}
    good = Matcher(
        tool_result_contains="file body",
        tools_include=("Read",),
        system_contains="sys",
        model="m",
        stream=True,
        min_messages=3,
        has_tools=True,
        has_tool_result=True,
        headers=(("anthropic-version", "2023"),),
        headers_absent=("authorization",),
    )
    assert good.mismatches(body, headers) == []
    misses = Matcher(
        user_text_contains="nope",
        tool_result_contains="zz",
        tool_result_is_error=False,
        tools_include=("Write",),
        tools_exclude=("Bash",),
        system_contains="x",
        model="o",
        stream=False,
        min_messages=9,
        has_tools=False,
        has_tool_result=False,
        headers=(("anthropic-beta", "oauth"), ("x-api-key", "other")),
        headers_absent=("x-api-key",),
    ).mismatches(body, headers)
    assert len(misses) == 14
    assert "header x-api-key lacks the expected value" in misses
    assert "header anthropic-beta lacks 'oauth'" in misses
    assert all("secret" not in m and "other" not in m for m in misses)
    assert [r.text for r in tool_results(body["messages"][2])] == ["file body", "plain", ""]
    empty: dict[str, Any] = {"messages": []}
    assert Matcher(tool_result_is_error=True).mismatches(empty, {})
    assert Matcher(has_tools=True, has_tool_result=True).mismatches(empty, {}) == [
        "tool_result missing",
        "no tools offered",
    ]
    assert Matcher(system_contains="abc").mismatches({"system": "abc"}, {}) == []
    assert Matcher(user_text_contains="x").mismatches({"messages": ["bad", {"role": "user", "content": None}]}, {})
    assert messages_text({"messages": ["junk", {"role": "user", "content": "a"}]}) == "a"


def test_capture_and_expand() -> None:
    scenario = parse_scenario(
        {
            "name": "x",
            "steps": [TEXT],
            "capture": {
                "s": {"regex": "S=(\\w+)", "source": "system"},
                "a": {"regex": "A=(\\w+)"},
                "missing": {"regex": "Q=(\\w+)"},
            },
        }
    )
    body = {"system": "S=one", "messages": [{"role": "user", "content": "A=two"}]}
    assert capture_values(scenario.captures, body) == {"s": "one", "a": "two"}
    assert expand({"k": ["${a}", 1]}, {"a": "x"}) == {"k": ["x", 1]}
    with pytest.raises(ScenarioError, match="unknown value"):
        expand("${b}", {})
