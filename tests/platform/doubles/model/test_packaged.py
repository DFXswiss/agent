"""U-DBL-model: the packaged scenarios - every adversary scenario ATK-01..ATK-32 runs through the double as Claude Code
would drive it, the mapping table covers every ID and every Spike B entry, and the double stays standard-library-only.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import agent_doubles
from agent_doubles.common.client import DoubleClient
from agent_doubles.model.scenario import packaged_names, packaged_scenario
from agent_doubles.model.server import ModelDouble

SRC = Path(agent_doubles.__file__).resolve().parent
SCENARIOS = SRC / "model" / "scenarios"
SESSION = "11111111-2222-4333-8444-555555555555"
WORK = f"/srv/sessions/{SESSION}/work"
FOREIGN_ID = "99999999-2222-4333-8444-555555555555"
FOREIGN = f"/srv/sessions/{FOREIGN_ID}/work"
TOOL_NAMES = (
    "Bash",
    "Agent",
    "Task",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
    "Skill",
    "Write",
    "Read",
    "Edit",
    "Grep",
    "Glob",
    "mcp__evil__shell",
)
ATK_IDS = [f"atk-{i:02d}" for i in range(1, 33)]


def converse(client: DoubleClient, first_text: str, results: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    """Drive the double like Claude Code: answer every tool use with a tool result until the turn ends."""
    tools = [{"name": n} for n in ("Read", "Write", "Edit", "Glob", "Grep", "mcp__platform__get_task")]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": f"<env>Primary working directory: {WORK}</env>"}]},
        {"role": "user", "content": [{"type": "text", "text": first_text}]},
    ]
    background = {"model": "m", "max_tokens": 32, "messages": [{"role": "user", "content": "title?"}]}
    assert client.request("POST", "/v1/messages", background).status == 200
    calls: list[dict[str, Any]] = []
    for _ in range(20):
        body = {"model": "m", "max_tokens": 100, "stream": False, "tools": tools, "messages": messages}
        response = client.request("POST", "/v1/messages", body)
        assert response.status == 200, response.body
        message = response.json()
        messages.append({"role": "assistant", "content": message["content"]})
        uses = [b for b in message["content"] if b["type"] == "tool_use"]
        if message["stop_reason"] != "tool_use":
            return calls
        calls.extend(uses)
        content = [
            {"type": "tool_result", "tool_use_id": u["id"], "content": results(u), "is_error": i % 2 == 0}
            for i, u in enumerate(uses)
        ]
        messages.append({"role": "user", "content": content})
    pytest.fail("the scenario did not end")


@pytest.mark.parametrize("name", ATK_IDS)
def test_adversary_scenarios_run_as_scripted(
    name: str, serve: Callable[[ModelDouble], ModelDouble], client_for: Callable[[ModelDouble], DoubleClient]
) -> None:
    scenario = packaged_scenario(name)
    assert scenario.description.startswith(f"ATK-{name[4:]} (")
    double = serve(ModelDouble(scenario, variables={"foreign_work": FOREIGN, "foreign_session": FOREIGN_ID}))
    calls = converse(client_for(double), "Work on the task.", lambda use: f"result of {use['name']}")
    state = double.state()
    assert state["ok"], state
    assert calls
    text = json.dumps(calls)
    assert "${" not in text
    assert "/srv/sessions/a1" not in text
    assert "/srv/sessions/b2" not in text
    if "foreign_work" in scenario.variables:
        assert FOREIGN in text
    if "foreign_session" in scenario.variables:
        assert f"/run/agent-guest/harness/{FOREIGN_ID}.md" in text
    assert sorted(c["name"] for c in calls) == sorted(readme_tool_calls()[f"ATK-{name[4:]}"])


def readme_tool_calls() -> dict[str, list[str]]:
    """The "Tool calls" column of the README group table: tool names, each repeated by its multiplier."""
    readme = (SCENARIOS / "atk" / "README.md").read_text(encoding="utf-8")
    names = "|".join(re.escape(n) for n in TOOL_NAMES)
    out: dict[str, list[str]] = {}
    for attack, column in re.findall(r"^\| (ATK-\d\d) \|.*\| ([^|]+) \|$", readme, re.MULTILINE):
        out[attack] = [
            name
            for name, times in re.findall(rf"\b({names})\b(?: \u00d7(\d))?", column)
            for _ in range(int(times or 1))
        ]
    return out


def test_baseline_and_examples(
    serve: Callable[[ModelDouble], ModelDouble], client_for: Callable[[ModelDouble], DoubleClient]
) -> None:
    double = serve(ModelDouble(packaged_scenario("atk-baseline")))
    calls = converse(client_for(double), "Baseline.", lambda use: "legit work file")
    assert [c["name"] for c in calls] == ["Write", "Read", "mcp__platform__get_task"]
    assert calls[0]["input"]["file_path"] == f"{WORK}/notes.md"
    assert double.state()["ok"]
    hello = serve(ModelDouble(packaged_scenario("hello")))
    body = {
        "model": "m",
        "stream": True,
        "tools": [{"name": "Read"}],
        "messages": [{"role": "user", "content": "2+2?"}],
    }
    client = client_for(hello)
    assert client.request("POST", "/v1/messages", {"messages": [{"role": "user", "content": "title"}]}).status == 200
    assert client.request("POST", "/v1/messages", body).status == 200
    assert hello.state()["ok"]


def test_openrouter_example(
    serve: Callable[[ModelDouble], ModelDouble], client_for: Callable[[ModelDouble], DoubleClient]
) -> None:
    double = serve(ModelDouble(packaged_scenario("openrouter-usage")))
    client = client_for(double)
    first = {
        "model": "m",
        "tools": [{"name": "Read"}],
        "messages": [{"role": "user", "content": f"Primary working directory: {WORK}"}],
    }
    response = client.request("POST", "/v1/messages", first, {"anthropic-version": "2023-06-01"})
    message = response.json()
    assert message["provider"] == "OtherProvider"
    assert message["usage"]["cost"] == 0.000340854
    assert response.headers["x-generation-id"].startswith("gen-double-")
    tool_id = message["content"][1]["id"]
    follow = {
        **first,
        "messages": [
            *first["messages"],
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "hello"}]},
        ],
    }
    limited = client.request("POST", "/v1/messages", follow)
    assert limited.status == 429
    assert limited.headers["retry-after"] == "1"
    final = client.request("POST", "/v1/messages", follow).json()
    assert final["provider"] == "DoubleProvider"
    assert double.state()["ok"]


def test_mapping_table_covers_every_attack_and_spike_entry() -> None:
    readme = (SCENARIOS / "atk" / "README.md").read_text(encoding="utf-8")
    group_rows = re.findall(r"^\| (ATK-\d\d) \|", readme, re.MULTILINE)
    assert group_rows == [f"ATK-{i:02d}" for i in range(1, 41)]
    spike_rows = re.findall(r"^\| (\d+) \| .+ \| (.+) \|$", readme, re.MULTILINE)
    assert [int(n) for n, _ in spike_rows] == list(range(1, 28))
    for _, target in spike_rows:
        ids = re.findall(r"ATK-\d\d", target)
        assert ids or "atk-baseline" in target
        assert all(f"atk-{i[4:]}" in packaged_names() or i == "ATK-33" for i in ids)


def test_scenario_files_are_valid_json_and_named_after_their_file() -> None:
    for path in sorted(SCENARIOS.glob("*.json")):
        assert json.loads(path.read_text(encoding="utf-8"))["name"] == path.stem


def test_double_modules_import_only_the_standard_library() -> None:
    """07 §1: the doubles are stdlib-only, so they run anywhere Claude Code runs (also inside a VM)."""
    allowed = set(sys.stdlib_module_names) | {"agent_doubles"}
    files = [SRC / "__init__.py", *sorted((SRC / "model").rglob("*.py")), *sorted((SRC / "common").rglob("*.py"))]
    assert len(files) > 10
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path}: relative import"
                roots = [str(node.module).split(".")[0]]
            else:
                continue
            assert set(roots) <= allowed, f"{path}: imports {roots}"
