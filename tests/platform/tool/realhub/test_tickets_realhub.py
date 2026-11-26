"""Captured tasks with the agent tool on the real hub (E-41, ADR-100, KOR-75): `task capture` starts no agent, `topic
create`/`topic list`, the filters of `task list` (status, urgency, person and topic at the hub, GitHub organisation and
repository on the opened `task.meta`), `task edit` of sealed and plaintext fields, `completed` and reopening, `task
start`, and the same through the tool's MCP server (`search` and `run`)."""

from __future__ import annotations

from typing import Any

from ..test_mcp import INIT, call, stdio
from .support import RealHub, pair, person, run_ok


def _setup(real: RealHub) -> tuple[Any, str, str]:
    ada = person(real, "Ada")
    pair(real, ada)
    org = str(run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")["org_id"])
    wg = str(run_ok(ada.tool, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    return ada, org, wg


def _ids(listed: dict[str, Any]) -> list[str]:
    return [str(row["task_id"]) for row in listed["tasks"]]


def test_capture_filter_edit_and_start(real: RealHub) -> None:
    ada, org, wg = _setup(real)
    first = run_ok(
        ada.tool,
        "task",
        "capture",
        "--wg",
        wg,
        "--title",
        "Look at the payment retries",
        "--description",
        "-",
        "--assignee",
        "me",
        "--new-topic",
        "Payments",
        "--repo",
        "Example/pay-api",
        "--urgency",
        "high",
        stdin="They fail twice a day.",
    )
    assert (first["status"], first["urgency"], first["assignee_id"], first["topic"]) == (
        "todo",
        "high",
        ada.id,
        "Payments",
    )
    assert (first["github_org"], first["github_repo"]) == ("Example", "Example/pay-api")
    task = str(first["task_id"])
    topics = run_ok(ada.tool, "topic", "list", "--org", org)["topics"]
    assert topics == [{"topic_id": first["topic_id"], "name": "Payments"}]
    second = str(run_ok(ada.tool, "task", "capture", "--wg", wg, "--title", "Write the release notes")["task_id"])

    # no agent: no session, a todo task
    shown = run_ok(ada.tool, "task", "show", "--task", task)["task"]
    assert (shown["state"], shown["status"], shown["description"]) == ("idle", "todo", "They fail twice a day.")
    assert (shown["topic"], shown["github_repo"]) == ("Payments", "Example/pay-api")

    def listed(*filters: str) -> list[str]:
        return _ids(run_ok(ada.tool, "task", "list", "--wg", wg, *filters))

    assert set(listed("--status", "todo")) == {task, second}
    assert listed("--urgency", "high") == [task]
    assert listed("--assignee", "me") == [task]
    assert listed("--assignee", "none") == [second]
    assert listed("--topic", str(first["topic_id"])) == [task]
    assert listed("--topic", "none") == [second]
    assert listed("--github-org", "example") == [task]
    assert listed("--repo", "example/PAY-API") == [task]
    assert listed("--repo", "example/other") == []

    # edit: sealed fields keep the rest, plaintext fields go to the hub
    edited = run_ok(ada.tool, "task", "edit", "--task", task, "--title", "Payment retries", "--urgency", "urgent")
    assert (edited["title"], edited["description"], edited["urgency"]) == (
        "Payment retries",
        "They fail twice a day.",
        "urgent",
    )
    cleared = run_ok(ada.tool, "task", "edit", "--task", task, "--repo", "", "--assignee", "none", "--topic", "none")
    assert (cleared["github_repo"], cleared["github_org"], cleared["assignee_id"], cleared["topic_id"]) == (
        None,
        "Example",
        None,
        None,
    )
    retitled = run_ok(ada.tool, "task", "edit", "--task", second, "--title", "Write the notes")
    assert (retitled["title"], retitled["status"]) == ("Write the notes", "todo")
    moved = run_ok(ada.tool, "task", "edit", "--task", second, "--assignee", "me", "--new-topic", "Docs")
    assert (moved["assignee_id"], moved["topic"]) == (ada.id, "Docs")
    renamed = run_ok(ada.tool, "topic", "create", "--org", org, "--name", "Documentation", "--topic", moved["topic_id"])
    assert renamed["topic_id"] == moved["topic_id"]
    assert run_ok(ada.tool, "task", "show", "--task", second)["task"]["topic"] == "Documentation"
    third = run_ok(
        ada.tool, "task", "capture", "--wg", wg, "--title", "Payments, part two", "--topic", str(first["topic_id"])
    )
    assert third["topic"] == "Payments"
    done = run_ok(ada.tool, "task", "edit", "--task", second, "--status", "completed")
    assert (done["status"], done["state"]) == ("completed", "closed")
    assert listed("--state", "closed", "--github-org", "Example") == []
    assert listed("--state", "open", "--github-org", "Example") == [task]
    back = run_ok(ada.tool, "task", "edit", "--task", second, "--status", "todo")
    assert (back["status"], back["state"]) == ("todo", "idle")

    # start: the first prompt, the agent's session, inProgress
    real.fleet.platform_device()
    real.fleet.platform.post({"type": "org_platform_access", "org": org, "granted": True})
    run_ok(ada.tool, "device", "assign", "--wg", wg)
    started = run_ok(ada.tool, "task", "start", "--task", task)
    assert started["status"] == "inProgress"
    assert started["session_id"]
    chat = run_ok(ada.tool, "task", "show", "--task", task)
    assert chat["events"][0]["body"]["text"] == "Payment retries\n\nThey fail twice a day."
    assert chat["task"]["status"] == "inProgress"


def test_refusals(real: RealHub) -> None:
    ada, org, wg = _setup(real)
    for argv in (
        ("task", "capture", "--wg", wg, "--title", "two\nlines"),
        ("task", "capture", "--wg", wg, "--title", "x", "--repo", "no-slash"),
        ("task", "capture", "--wg", wg, "--title", "x", "--github-org", "a", "--repo", "b/c"),
        ("task", "capture", "--wg", wg, "--title", "x", "--assignee", "01930000-0000-7000-8000-000000000000"),
        ("task", "list", "--org", org, "--state", "needs-decision", "--status", "todo"),
        ("topic", "create", "--org", org, "--name", ""),
    ):
        code, document = ada.tool.run(*argv)
        assert (code, document["ok"], document["error"]) != (0, True, None), argv
        assert document["error"] == "invalid_argument", (argv, document)
    task = str(run_ok(ada.tool, "task", "capture", "--wg", wg, "--title", "Only a title")["task_id"])
    code, document = ada.tool.run("task", "edit", "--task", task)
    assert document["error"] == "invalid_argument"


def test_a_chat_program_captures_a_task(real: RealHub) -> None:
    ada, _org, wg = _setup(real)
    captured = call(3, "run", {"args": ["task", "capture", "--wg", wg, "--title", "Thema xy anschauen"]})
    replies, _stderr, code = stdio(ada.tool.env, [INIT, call(2, "search", {"query": "Ticket erfassen"}), captured])
    assert code == 0
    assert replies[1]["result"]["structuredContent"]["results"][0]["command"] == "task capture"
    result = replies[2]["result"]["structuredContent"]
    assert (result["ok"], result["status"], result["title"]) == (True, "todo", "Thema xy anschauen")
    assert _ids(run_ok(ada.tool, "task", "list", "--wg", wg, "--status", "todo")) == [result["task_id"]]
