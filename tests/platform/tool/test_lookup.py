"""Looking things up (KOR-63, owner rule E-26): the short start, `search`, `guide --topic`, `--limit`/`--fields`,
and the completeness of the one source: every command of the tree carries its purpose, an example from the guide
and words that find it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_platform import errors, lookup
from agent_platform.cli import build_parser
from agent_platform.commands import Parser, trimmed
from agent_platform.errors import PlatformError

from .conftest import Tool
from .hubfake import HubFake, User
from .test_tasks import default_wg

REPO_ROOT = Path(__file__).resolve().parents[3]
GUIDE = REPO_ROOT / "docs" / "platform" / "agents.md"
START_LIMIT = 3500


def commands() -> list[tuple[str, Parser]]:
    return list(lookup.walk(build_parser()))


def found(query: str) -> list[str]:
    return [hit["command"] for hit in lookup.search(build_parser(), query)["results"]]


# --- the one source ---


def test_every_command_has_a_purpose_an_example_of_the_guide_and_words_that_find_it() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    listed = commands()
    assert len(listed) > 80
    for command, parser in listed:
        doc = parser.doc
        assert doc is not None, command
        assert doc.summary.strip(), command
        assert f"agent-platform {command}" in doc.example, command
        assert doc.example in guide, command
        first = " ".join(doc.words.split()[:2])
        assert command in found(first), (command, first, found(first))
        assert found(command)[0] == command, (command, found(command))
        assert doc.example in parser.format_help(), command


def test_every_command_named_by_an_error_is_found_first() -> None:
    names = {command for command, _parser in commands()}
    spans = [span for text in [*errors.NEXT.values(), *errors.NEXT_FOR_REASON.values()] for span in _spans(text)]
    named = [span.removeprefix("agent-platform ") for span in spans if span.startswith("agent-platform ")]
    named = [span for span in named if not span.startswith("<")]
    assert "device assign" in " ".join(named)
    for span in named:
        words = span.split()
        command = " ".join(words[:2]) if " ".join(words[:2]) in names else words[0]
        assert command in names, span
        assert found(command)[0] == command, span


def _spans(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


# --- search ---


@pytest.mark.parametrize(
    ("query", "command"),
    [
        ("Aufgabe anlegen", "task create"),
        ("create a task", "task create"),
        ("einladen", "org invite"),
        ("invite a colleague", "org invite"),
        ("Kosten", "task charges"),
        ("costs", "billing"),
        ("Gerät zuweisen", "device assign"),
        ("task create", "task create"),
        ("org invite", "org invite"),
    ],
)
def test_search_finds_in_german_and_english(query: str, command: str) -> None:
    assert command in found(query)


def test_search_answers_the_best_first_at_most_five() -> None:
    result = lookup.search(build_parser(), "task")
    assert len(result["results"]) == lookup.MAX_RESULTS
    assert all(hit["command"].startswith("task ") for hit in result["results"][:3])
    hit = lookup.search(build_parser(), "task create")["results"][0]
    assert hit["command"] == "task create"
    assert hit["usage"].startswith("agent-platform task create --wg WG --text TEXT")
    assert "--platform-root-key" not in hit["usage"]
    assert "[-h]" not in hit["usage"]
    assert hit["example"].startswith("agent-platform task create")
    assert hit["next"]
    assert "next" not in lookup.search(build_parser(), "search")["results"][0]


def test_search_without_a_hit_says_where_to_look(tool: Tool) -> None:
    code, result = tool.run("search", "xyzzy", "qwrt")
    assert code == 0
    assert result["results"] == []
    assert "guide --topic" in result["hint"]
    assert "task" in result["commands"]
    assert "org" in result["commands"]
    assert "every-command" in result["topics"]
    assert tool.run("search", "Aufgabe", "anlegen")[1]["results"][0]["command"] == "task create"
    assert tool.run("search", "the", "a")[1]["results"] == []


def test_search_explains_a_named_error_code() -> None:
    result = lookup.search(build_parser(), "task create failed with no_device")
    assert result["results"][0]["command"] == "task create"
    assert result["errors"] == [{"error": "no_device", "next": errors.NEXT["no_device"]}]
    assert "device assign" in result["errors"][0]["next"]
    assert lookup.search(build_parser(), "not_paired zzzq")["errors"][0]["error"] == "not_paired"
    assert "errors" not in lookup.search(build_parser(), "task create")
    assert found("repair") == []


def test_a_mistyped_command_gets_the_usage_and_close_names(tool: Tool) -> None:
    result = tool.run("tsk", "list")[1]
    assert result["error"] == "invalid_argument"
    assert result["did_you_mean"] == ["task", "test"]
    assert result["usage"].startswith("agent-platform [-h] [--version]")
    assert tool.run("task", "craete")[1]["did_you_mean"] == ["create"]
    wrong = tool.run("task", "create", "--txt", "x")[1]
    assert "did_you_mean" not in wrong
    assert wrong["usage"].startswith("agent-platform task create")
    assert "did_you_mean" not in tool.run("task", "zzzzzz")[1]


def test_similar_words() -> None:
    assert lookup.similar("einladen", "einladung") == 0.7
    assert lookup.similar("task", "tasks") == 0.7
    assert lookup.similar("task", "task") == 1.0
    assert lookup.similar("wg", "wgs") == 0.0
    assert lookup.similar("device", "devote") == 0.0
    assert lookup.tokens("Gerät für die Arbeitsgruppe") == ["gerat", "arbeitsgruppe"]


# --- guide ---


def test_guide_gives_the_short_start_and_the_topics(tool: Tool) -> None:
    code, result = tool.run("guide")
    assert code == 0
    assert len(result["guide"]) <= START_LIMIT
    assert "agent-platform search" in result["guide"]
    assert result["topics"][0] == "intro"
    assert "every-command" in result["topics"]


def test_guide_topic_gives_sections(tool: Tool) -> None:
    whole = GUIDE.read_text(encoding="utf-8")
    assert tool.run("guide", "--topic", "all")[1]["guide"] == whole
    result = tool.run("guide", "--topic", "every command")[1]
    assert result["topic"] == "every-command"
    assert result["guide"].startswith("## 4. Every command")
    assert result["guide"] in whole
    connect = tool.run("guide", "--topic", "connect")[1]
    assert connect["topic"].count(",") >= 1
    assert "pair --hub" in connect["guide"]
    assert "claude connect" in connect["guide"]
    assert tool.run("guide", "--topic", "Install")[1]["guide"].startswith("## 0. Install")
    assert tool.run("guide", "--topic", "alone")[1]["guide"].startswith("## 1a. You work for yourself alone")
    missing = tool.run("guide", "--topic", "nothing here")[1]
    assert missing["guide"] is None
    assert missing["topics"]
    assert missing["hint"]
    assert tool.run("guide", "--topic", "the")[1]["guide"] is None


def test_help_says_what_a_command_is_for(tool: Tool) -> None:
    shown = tool.run("task", "create", "--help")[1]["help"]
    assert "a new task with its first prompt" in shown
    assert "example: agent-platform task create" in shown
    assert "next: agent-platform task" in shown


# --- --limit and --fields ---


def test_trimmed_cuts_lists_and_keeps_keys() -> None:
    result = {"rows": [{"id": 1, "state": "a", "x": 0}, {"id": 2, "state": "b"}, 3], "n": 1}
    assert trimmed(result, None, None, "first") == result
    assert trimmed(result, 2, "id, state", "first") == {
        "rows": [{"id": 1, "state": "a"}, {"id": 2, "state": "b"}],
        "n": 1,
        "limited": {"rows": 3},
    }
    assert trimmed(result, 1, None, "last") == {"rows": [3], "n": 1, "limited": {"rows": 3}}
    assert trimmed(result, 5, "id", "first") == {"rows": [{"id": 1}, {"id": 2}, 3], "n": 1}
    for limit, fields in ((0, None), (None, ","), (None, "")):
        with pytest.raises(PlatformError) as caught:
            trimmed(result, limit, fields, "first")
        assert caught.value.code == "invalid_argument"


def test_trimmed_moves_next_after_and_cuts_text_from_the_end() -> None:
    follow = {"events": [{"seq": 4}, {"seq": 7}, {"seq": 9}], "next_after": 9}
    assert trimmed(follow, 2, None, "first") == {
        "events": [{"seq": 4}, {"seq": 7}],
        "next_after": 7,
        "limited": {"events": 3},
    }
    assert trimmed(follow, 2, None, "last")["next_after"] == 9
    paged = {"rows": [{"seq": 3}, {"seq": 2}], "next_before": "cursor"}
    assert trimmed(paged, 1, None, "first") == {"rows": [{"seq": 3}], "next_before": None, "limited": {"rows": 2}}
    assert trimmed(paged, 2, None, "first") == paged
    assert trimmed({"events": [{"x": 1}, {"x": 2}], "next_after": 3}, 1, None, "first")["next_after"] == 3
    assert trimmed({"events": [{"seq": 1}, {"seq": 2}]}, 1, None, "first") == {
        "events": [{"seq": 1}],
        "limited": {"events": 2},
    }
    terminal = {"text": "a\nb\nc\n", "state": "working"}
    assert trimmed(terminal, 2, None, "last") == {"text": "b\nc\n", "state": "working", "limited": {"text": 3}}
    assert trimmed(terminal, 3, None, "last") == terminal
    assert trimmed(terminal, 4, None, "last") == terminal
    assert trimmed(terminal, 1, None, "first") == terminal


def test_limit_and_fields_on_the_command_line(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    for text in ("First", "Second", "Third"):
        assert tool.run("task", "create", "--wg", wg, "--text", text)[0] == 0
    listed = tool.run("task", "list", "--wg", wg)[1]
    assert len(listed["tasks"]) == 3
    assert "limited" not in listed
    code, cut = tool.run("task", "list", "--wg", wg, "--limit", "2", "--fields", "task_id,title")
    assert code == 0, cut
    assert cut["tasks"] == [{key: row[key] for key in ("task_id", "title")} for row in listed["tasks"][:2]]
    assert cut["limited"] == {"tasks": 3}
    assert cut["wg_id"] == wg
    task = listed["tasks"][0]["task_id"]
    assert tool.run("task", "prompt", "--task", task, "--text", "More")[0] == 0
    shown = tool.run("task", "show", "--task", task, "--limit", "1", "--fields", "body")[1]
    assert [set(event) for event in shown["events"]] == [{"body"}]
    assert shown["events"][0]["body"]["text"] == "More"
    # `task show` reads only the newest `--limit` events (HOT-29): the older ones are behind `next_before`
    assert "limited" not in shown
    older = tool.run("task", "show", "--task", task, "--limit", "1", "--before", str(shown["next_before"]))[1]
    first = [listed["tasks"][0]["title"]]
    assert ([event["body"]["text"] for event in older["events"]], older["next_before"]) == (first, None)
    assert tool.run("task", "list", "--wg", wg, "--limit", "0")[1]["error"] == "invalid_argument"
    assert "--limit" not in tool.run("task", "create", "--help")[1]["help"]
