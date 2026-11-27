"""Captured tasks with the agent tool on the real hub (E-41, ADR-102, KOR-75): `task capture` starts no agent, `topic
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


def test_the_dashboard_over_organizations(real: RealHub) -> None:
    """ADR-103 section 4: `task overview` lists the tasks of every readable workgroup of every organization,
    filtered, searched and sorted, with the counters; `task list --org` is the same for one organization."""
    ada, org, wg = _setup(real)
    other_org = str(run_ok(ada.tool, "org", "create", "--name", "Beta", "--display-name", "Ada")["org_id"])
    other_wg = str(run_ok(ada.tool, "wg", "create", "--org", other_org, "--name", "Ops")["wg_id"])
    urgent = run_ok(ada.tool, "task", "capture", "--wg", wg, "--title", "Fix payments", "--urgency", "urgent")
    mine = run_ok(
        ada.tool, "task", "capture", "--wg", other_wg, "--title", "Write docs", "--assignee", "me", "--repo", "acme/d"
    )
    later = run_ok(ada.tool, "task", "capture", "--wg", wg, "--title", "Plan the release")
    run_ok(ada.tool, "task", "edit", "--task", str(later["task_id"]), "--status", "completed")

    def ids(*argv: str) -> list[str]:
        return [str(row["task_id"]) for row in run_ok(ada.tool, "task", "overview", *argv)["tasks"]]

    everything = run_ok(ada.tool, "task", "overview")
    assert {str(row["task_id"]) for row in everything["tasks"]} >= {urgent["task_id"], mine["task_id"]}
    assert set(everything["organizations"]) >= {org, other_org}
    assert everything["counts"]["by_status"]["completed"] >= 1
    assert everything["counts"]["by_urgency"]["urgent"] >= 1
    # captured, never started: "no agent" of the start page's three groups; the completed one is not open
    assert everything["counts"]["by_overview"] == {"working": 0, "waiting": 0, "no_agent": 2}
    assert set(ids("--overview", "no-agent")) == {urgent["task_id"], mine["task_id"]}
    assert ids("--overview", "working") == []
    assert ids("--wg", wg, "--overview", "no-agent", "--urgency", "urgent") == [urgent["task_id"]]
    assert ids("--order", "urgency")[0] == urgent["task_id"]
    assert ids("--order", "created")[0] == later["task_id"]
    assert ids("--urgency", "urgent") == [urgent["task_id"]]
    assert ids("--assignee", "me") == [mine["task_id"]]
    assert ids("--repo", "ACME/d") == [mine["task_id"]]
    assert ids("--search", "PAYMENTS") == [urgent["task_id"]]
    assert ids("--org", other_org) == [mine["task_id"]]
    assert ids("--wg", wg, "--status", "completed") == [later["task_id"]]
    assert ids("--state", "open", "--org", org) == [urgent["task_id"]]
    assert ids("--mine", "--org", org, "--order", "urgency") == [urgent["task_id"], later["task_id"]]
    row = next(row for row in everything["tasks"] if row["task_id"] == mine["task_id"])
    assert (row["org_id"], row["workgroup"], row["assignee_id"], row["status"]) == (other_org, "Ops", ada.id, "todo")
    one = run_ok(ada.tool, "task", "list", "--org", org, "--status", "todo")
    assert [row["task_id"] for row in one["tasks"]] == [urgent["task_id"]]
    assert one["counts"]["by_status"] == {"todo": 1, "inProgress": 0, "completed": 1}
    assert run_ok(ada.tool, "task", "list", "--org", org, "--state", "open")["counts"]["total"] == 2
    listed = run_ok(ada.tool, "task", "list", "--wg", wg, "--order", "urgency", "--search", "e")
    assert [row["task_id"] for row in listed["tasks"]] == [urgent["task_id"], later["task_id"]]
    no_agent = run_ok(ada.tool, "task", "list", "--wg", wg, "--overview", "no-agent")
    assert [row["task_id"] for row in no_agent["tasks"]] == [urgent["task_id"]]
    assert ada.tool.run("task", "overview", "--org", org, "--wg", wg)[0] != 0
    assert ada.tool.run("task", "overview", "--overview", "idle")[0] != 0
    for argv in (
        ("task", "list", "--org", org, "--mine"),
        ("task", "list", "--org", org, "--state", "needs-decision", "--status", "todo"),
        ("task", "list", "--org", org, "--by-person"),
    ):
        assert ada.tool.run(*argv)[1]["error"] == "invalid_argument", argv


def test_a_chat_program_captures_a_task(real: RealHub) -> None:
    ada, _org, wg = _setup(real)
    captured = call(3, "run", {"args": ["task", "capture", "--wg", wg, "--title", "Thema xy anschauen"]})
    replies, _stderr, code = stdio(ada.tool.env, [INIT, call(2, "search", {"query": "Ticket erfassen"}), captured])
    assert code == 0
    assert replies[1]["result"]["structuredContent"]["results"][0]["command"] == "task capture"
    result = replies[2]["result"]["structuredContent"]
    assert (result["ok"], result["status"], result["title"]) == (True, "todo", "Thema xy anschauen")
    assert _ids(run_ok(ada.tool, "task", "list", "--wg", wg, "--status", "todo")) == [result["task_id"]]
