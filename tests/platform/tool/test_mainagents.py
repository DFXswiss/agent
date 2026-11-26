"""Main agents with names in the agent tool (KOR-52, ADR-095 section 7): the automatic name against every case of
`contracts/tasks/agent-name.json`, the main agent in task rows (`<name> #<number>`), `agents rename` (sealed and
appended, checked here first), the newest name that does not verify, `agents people|list|show`, `task list
--by-person` and the audit's agent actors with `--on-behalf-of`, against the hub double."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import agentnames, mainagents
from agent_platform.errors import PlatformError
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from .conftest import Tool
from .fakes.account_views import views
from .hubfake import HubFake, User, now_ms_text
from .seeding import reader
from .test_attention import World, record
from .test_inbox import audit_row
from .test_tasks import default_wg

CASES = Path(__file__).resolve().parents[3] / "contracts" / "tasks" / "agent-name.json"
TITLE = "Fix the login bug and add the tests now"
AUTOMATIC = "Fix the login bug and"


@pytest.mark.parametrize("case", json.loads(CASES.read_text(encoding="utf-8"))["cases"], ids=lambda case: case["name"])
def test_automatic_name_cases(case: dict[str, str]) -> None:
    assert agentnames.automatic(case["title"]) == agentnames.AgentName(case["agent_name"], "automatic")


def test_the_client_side_check_of_a_name() -> None:
    for good in ("Login doctor", "x" * 80, "😀" * 80, "a  b"):
        assert agentnames.checked_name(good) == good
    # white space at both ends is removed first, as the web client does
    for typed, kept in ((" a ", "a"), ("\u3000Doc\t\n", "Doc"), (" " + "x" * 80 + "\u2028", "x" * 80)):
        assert agentnames.checked_name(typed) == kept
    for bad in ("", "   ", "\u2029", "a\nb", "a\rb", "a\x0bb", "a\x0cb", "a\x85b", "a\u2028b", "a\u2029b", "x" * 81):
        with pytest.raises(PlatformError) as caught:
            agentnames.checked_name(bad)
        assert caught.value.code == "invalid_argument", bad


def test_shown_texts_and_handles() -> None:
    given = agentnames.AgentName("Login doctor", "given")
    assert agentnames.shown(given, 42) == "Login doctor #42"
    assert agentnames.shown(given, None) == "Login doctor"
    assert agentnames.shown(agentnames.AgentName(None, "unverifiable"), 42) == "#42"
    assert (agentnames.handle(True), agentnames.handle("1"), agentnames.handle(4, "1.2")) == (None, None, "#4.1.2")
    sub = {"number": 42, "agent_path": "1"}
    assert agentnames.actor_text("subagent", sub, None, "Wendy") == "Subagent #42.1 of #42 on behalf of Wendy"
    assert agentnames.actor_text("main_agent", sub, "Doc #42", None) == "Main agent Doc #42 (no single person)"


def names_of(hub: HubFake, wg: str) -> list[dict[str, Any]]:
    """The headers of the stored `task.agent_name` events of the workgroup."""
    return [item["event"]["header"] for item in hub.events[wg] if item["event"]["header"]["kind"] == "task.agent_name"]


def _forge(hub: HubFake, wg: str, kind: str, seq: int | None = None) -> None:
    """A copy of the newest stored event of `kind` whose ciphertext does not verify, appended (or at position `seq`)."""
    stored = hub.events[wg]
    original = next(item for item in reversed(stored) if item["event"]["header"]["kind"] == kind)
    forged: Any = json.loads(json.dumps(original))
    forged["event"]["ct"] = forged["event"]["ct"][:-4] + "AAAA"
    forged["event"]["header"]["id"] = uuid7()
    stored.append({**forged, "scope_seq": len(stored) + 1 if seq is None else seq})


def test_task_rows_show_the_main_agent_and_rename_changes_it(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    task = tool.run("task", "create", "--wg", wg, "--text", TITLE)[1]["task_id"]
    row = tool.run("task", "list", "--wg", wg)[1]["tasks"][0]
    assert (row["number"], row["agent"], row["name_source"]) == (1, f"{AUTOMATIC} #1", "automatic")
    code, renamed = tool.run("agents", "rename", "--task", task, "--name", "Login doctor")
    assert code == 0, renamed
    assert (renamed["name"], renamed["name_source"], renamed["task_id"]) == ("Login doctor", "given", task)
    assert isinstance(renamed["scope_seq"], int)
    stored = names_of(hub, wg)
    assert [(h["task"], h["session"], h["ref"], h["author"]) for h in stored] == [(task, "", "", account.id)]
    assert b"Login doctor" not in b"".join(hub.bodies)
    shown = tool.run("task", "show", "--task", task)[1]["task"]
    assert (shown["agent"], shown["name_source"], shown["number"]) == ("Login doctor #1", "given", 1)
    # back to the automatic name
    renamed = tool.run("agents", "rename", "--task", task, "--automatic")[1]
    assert (renamed["name"], renamed["name_source"]) == (AUTOMATIC, "automatic")
    # the client-side checks run before anything is sent
    sent = len(names_of(hub, wg))
    for argv in (("--name", ""), ("--name", "a\nb"), ("--name", "x" * 81), ("--name", "x", "--automatic"), ()):
        assert tool.run("agents", "rename", "--task", task, *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("agents", "rename", "--task", "x", "--automatic")[1]["error"] == "invalid_argument"
    assert len(names_of(hub, wg)) == sent
    # a newer name that does not verify: unverifiable, never the older given one
    tool.run("agents", "rename", "--task", task, "--name", "Login doctor")
    _forge(hub, wg, "task.agent_name")
    row = tool.run("task", "list", "--wg", wg)[1]["tasks"][0]
    assert (row["agent"], row["name_source"]) == ("#1", "unverifiable")
    # a forged older one (by position) changes nothing; nor does a forged title
    tool.run("agents", "rename", "--task", task, "--name", "Doc")
    _forge(hub, wg, "task.agent_name", seq=1)
    _forge(hub, wg, "task.meta")
    row = tool.run("task", "list", "--wg", wg)[1]["tasks"][0]
    assert (row["agent"], row["title"]) == ("Doc #1", TITLE)


def test_names_that_break_the_schema_or_are_locked(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    wg = default_wg(hub, account)
    task = tool.run("task", "create", "--wg", wg, "--text", TITLE)[1]["task_id"]
    other = tool.run("task", "create", "--wg", wg, "--text", "Other")[1]["task_id"]
    # a writer whose client does not check (the hub cannot): the body breaks the schema
    monkeypatch.setattr(mainagents, "checked_name", lambda text: text)
    for bad in ("a\nb", "x" * 81):
        assert tool.run("agents", "rename", "--task", task, "--name", bad)[1]["name_source"] == "unverifiable"
    monkeypatch.undo()
    # an older title event listed after a newer one does not win; events without a position are left out
    tool.run("agents", "rename", "--task", other, "--name", "Other agent")
    path = f"/v1/scopes/{wg}/events"
    stored = list(hub.events[wg])
    hub.hooks[("GET", path)] = lambda body, request: (
        200,
        {"events": [*reversed(stored), {"x": 1}], "next_after": None},
    )
    rows = {row["task_id"]: row for row in tool.run("task", "list", "--wg", wg)[1]["tasks"]}
    assert (rows[other]["agent"], rows[task]["title"]) == ("Other agent #2", TITLE)
    del hub.hooks[("GET", path)]
    # without the workgroup key every event is locked: no title, the name unverifiable or `Agent`
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg"]
    rows = {row["task_id"]: row for row in tool.run("task", "list", "--wg", wg)[1]["tasks"]}
    assert (rows[task]["title"], rows[task]["name_source"]) == (None, "unverifiable")
    assert rows[other]["name_source"] == "unverifiable"


def test_a_reader_cannot_rename_and_a_row_without_number(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    other, _bob, _org, wg = reader(tool, hub, tmp_path)
    task = tool.run("task", "create", "--wg", wg, "--text", TITLE)[1]["task_id"]
    assert other.run("agents", "rename", "--task", task, "--name", "Mine")[1]["error"] == "no_write_access"
    assert names_of(hub, wg) == []
    # a hub of an older release sends no number: the name alone
    del hub.tasks[task]["task"]["number"]
    row = other.run("task", "list", "--wg", wg)[1]["tasks"][0]
    assert (row["number"], row["agent"]) == (None, AUTOMATIC)


def test_people_and_their_main_agents(tool: Tool, hub: HubFake, account: User) -> None:
    world = World(tool, hub, account)
    first, second, third = world.task(TITLE), world.task("Deploy"), world.task("Write docs")
    tool.run("agents", "rename", "--task", second, "--name", "Deployer")
    hub.attention[first] = record(first, "needs_decision", "question", "2026-11-24T10:00:00.000Z")
    hub.attention[second] = record(second, "working", "working", "2026-11-24T09:00:00.000Z", subagents_active=2)
    someone = uuid7()
    foreign = {**hub.tasks[third]["task"], "task_id": uuid7(), "owner_id": someone, "number": 9}
    hub.tasks[foreign["task_id"]] = {"task": foreign, "sessions": [], "transitions": []}
    for found, at in ((first, "2026-12-01T00:00:00.000Z"), (second, "2026-12-02T00:00:00.000Z"), (third, "2027")):
        hub.tasks[found]["task"]["last_activity_at"] = at
    hub.tasks[third]["task"]["github_identity"] = uuid7()
    code, listed = tool.run("agents", "people", "--org", world.org)
    assert code == 0, listed
    assert [(p["owner_id"], p["you"], p["agents"]) for p in listed["people"]] == [
        (account.id, True, 3),
        (someone, False, 1),
    ]
    mine = listed["people"][0]
    assert (mine["needs_decision"], mine["working"], mine["blocked"], mine["subagents_active"]) == (1, 1, 0, 2)
    assert (listed["next_after"], listed["unverified"]) == (None, 0)
    paged = tool.run("agents", "people", "--org", world.org, "--limit", "1")[1]
    assert (len(paged["people"]), paged["next_after"]) == (1, account.id)
    rest = tool.run("agents", "people", "--org", world.org, "--after", account.id)[1]
    assert [p["owner_id"] for p in rest["people"]] == [someone]
    code, agents = tool.run("agents", "list", "--org", world.org)
    assert code == 0, agents
    assert [(a["task_id"], a["agent"], a["handle"]) for a in agents["agents"]] == [
        (third, "Write docs #3", "#3"),
        (second, "Deployer #2", "#2"),
        (first, f"{AUTOMATIC} #1", "#1"),
    ]
    by_task = {a["task_id"]: a for a in agents["agents"]}
    assert (by_task[third]["github_account"], by_task[second]["github_account"]) == ({"kind": "other"}, "automatic")
    assert (by_task[second]["category"], by_task[second]["subagents_active"]) == ("working", 2)
    assert (by_task[third]["category"], by_task[first]["reason"]) == (None, "question")
    assert agents["owner_id"] == account.id
    paged = tool.run("agents", "list", "--org", world.org, "--limit", "2")[1]
    assert len(paged["agents"]) == 2
    following = tool.run("agents", "list", "--org", world.org, "--before", paged["next_before"])[1]
    assert [a["task_id"] for a in following["agents"]] == [first]
    theirs = tool.run("agents", "list", "--org", world.org, "--owner", someone, "--limit", "500")[1]
    assert [a["handle"] for a in theirs["agents"]] == ["#9"]
    for argv in (
        ("agents", "people", "--org", world.org, "--after", "x"),
        ("agents", "people", "--org", world.org, "--limit", "0"),
        ("agents", "list", "--org", world.org, "--owner", "x"),
        ("agents", "list", "--owner", "me"),
        ("agents", "list", "--before", "x"),
        ("agents", "people", "--org", "x"),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("agents", "people", "--org", uuid7())[1]["error"] == "no_org"
    # without --org: the connected AI agents as before
    assert "agents" in tool.run("agents", "list")[1]


def test_rows_the_hub_breaks_are_left_out(tool: Tool, hub: HubFake, account: User) -> None:
    world = World(tool, hub, account)
    task = world.task(TITLE)
    good = {"task": hub.tasks[task]["task"], "attention": None}
    broken: list[Any] = [
        "x",
        {"task": "x", "attention": None},
        {"task": {**good["task"], "task_id": "x"}, "attention": None},
        {"task": {**good["task"], "org_id": uuid7()}, "attention": None},
        {"task": {**good["task"], "number": None}, "attention": None},
        {"task": good["task"], "attention": "x"},
        {"task": good["task"], "attention": {"task_id": uuid7()}},
    ]
    path = f"/v1/orgs/{world.org}/main-agents"
    hub.hooks[("GET", path)] = lambda body, request: (200, {"items": [good, *broken], "next_before": ""})
    listed = tool.run("agents", "list", "--org", world.org)[1]
    assert ([a["task_id"] for a in listed["agents"]], listed["unverified"], listed["next_before"]) == ([task], 6, None)
    hub.hooks[("GET", path)] = lambda body, request: (200, {"items": None})
    assert tool.run("agents", "list", "--org", world.org)[1]["reason"] == "response_invalid"
    person = {"owner_id": account.id, "agents": 1, "needs_decision": 0, "blocked": 0, "working": 1}
    person["subagents_active"] = 0
    rows: list[Any] = ["x", {**person, "owner_id": "x"}, {**person, "agents": -1}, {**person, "working": True}]
    path = f"/v1/orgs/{world.org}/people/agents"
    hub.hooks[("GET", path)] = lambda body, request: (200, {"people": [person, *rows], "next_after": "x"})
    listed = tool.run("agents", "people", "--org", world.org)[1]
    assert (len(listed["people"]), listed["unverified"], listed["next_after"]) == (1, 4, None)


def _session(task: str, sid: str, *, depth: int = 0, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    row = {
        "session_id": sid,
        "task_id": task,
        "depth": depth,
        "parent_session_id": parent,
        "state": "working",
        "created_at": now_ms_text(),
        "agent_path": None,
        "origin_operation_id": None,
        "github_identity": None,
        "triggered_by": None,
        **extra,
    }
    return {"session": row, "usage": None, "transitions": []}


def _params(world: World, task: str, operation: str, body: Any, *, ref: str | None = None) -> None:
    """A device-signed `operation.params` event (as `World.params_event`, with a free `ref`)."""
    log = world.hub.orgs[world.org].log
    header = new_header(
        id=uuid7(),
        org=world.org,
        scope=WG,
        scope_id=world.wg,
        epoch=log.state.epoch(WG, world.wg),
        author=world.device,
        kind="operation.params",
        ts=now_ms_text(),
        lh=log.head.seq,
        wg=world.wg,
        task=task,
        ref=ref or operation,
    )
    key = world.hub.org_key(world.org, world.account, WG, world.wg)
    world.hub.store_event(
        world.wg, seal_event(key, header, body_json(body), world.hub.platform_devices[world.device].sig)
    )


def test_show_prints_the_agent_tree(tool: Tool, hub: HubFake, account: User) -> None:
    world = World(tool, hub, account)
    task = world.task(TITLE)
    other_task = world.task("Other")
    main, first, second, grandchild, later_main, orphan = (uuid7() for _ in range(6))
    review, fix, unread, wrong_ref = (uuid7() for _ in range(4))
    request = {"type": "subagent.request", "params": {"purpose": "Review the pull request", "prompt": "Review it"}}
    _params(world, task, review, {"operation_id": review, **request})
    _params(world, task, fix, {"operation_id": fix, "type": "subagent.request", "params": {"purpose": 5}})
    _params(world, task, unread, {"operation_id": unread, "type": "human.question", "params": {"question": "?"}})
    _params(world, task, wrong_ref, {"operation_id": wrong_ref, **request}, ref=uuid7())
    _params(world, task, uuid7(), {"operation_id": uuid7(), "type": "subagent.request", "params": "x"})
    _params(world, other_task, review, {"operation_id": review, **request})
    identity = uuid7()
    hub.tasks[task]["sessions"] = [
        _session(task, later_main, created_at="2026-11-24T12:00:00.000Z", triggered_by=uuid7()),
        _session(task, main, created_at="2026-11-24T10:00:00.000Z", triggered_by=account.id),
        _session(task, second, depth=1, parent=main, agent_path="2", origin_operation_id=fix, github_identity=identity),
        _session(task, first, depth=1, parent=main, agent_path="1", origin_operation_id=review, created_at="1"),
        _session(task, grandchild, depth=2, parent=first, agent_path="1.1", origin_operation_id=wrong_ref),
        _session(task, orphan, depth=1, parent=uuid7(), created_at="9"),
        _session(other_task, uuid7()),
        {"session": "x"},
        "x",
    ]
    code, shown = tool.run("agents", "show", "--task", task)
    assert code == 0, shown
    agent = shown["agent"]
    assert (agent["agent"], agent["handle"], agent["name_source"], agent["owner_id"]) == (
        f"{AUTOMATIC} #1",
        "#1",
        "automatic",
        account.id,
    )
    assert agent["github_account"] == "automatic"
    rows = [(s["session_id"], s["level"], s["kind"], s["handle"], s["purpose"]) for s in shown["sessions"]]
    assert rows == [
        (main, 0, "main", "#1", None),
        (first, 1, "subagent", "#1.1", "Review the pull request"),
        (grandchild, 2, "subagent", "#1.1.1", None),
        (second, 1, "subagent", "#1.2", None),
        (later_main, 0, "main", "#1", None),
        (orphan, 0, "subagent", None, None),
    ]
    by_id = {s["session_id"]: s for s in shown["sessions"]}
    assert by_id[second]["github_account"] == {"kind": "other"}
    assert (by_id[main]["triggered_by"], by_id[orphan]["triggered_by_name"]) == (account.id, None)
    # a hub answer of another task, or without sessions
    hub.hooks[("GET", f"/v1/tasks/{task}")] = lambda body, request: (200, {"task": {"task_id": task, "wg_id": "x"}})
    assert tool.run("agents", "show", "--task", task)[1]["reason"] == "response_invalid"
    row = {**hub.tasks[task]["task"], "number": None, "owner_id": None}
    hub.hooks[("GET", f"/v1/tasks/{task}")] = lambda body, request: (200, {"task": row, "sessions": None})
    bare = tool.run("agents", "show", "--task", task)[1]
    assert (bare["sessions"], bare["agent"]["handle"], bare["agent"]["owner"]) == ([], None, None)


def test_task_list_by_person(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    first = tool.run("task", "create", "--wg", wg, "--text", "One")[1]["task_id"]
    second = tool.run("task", "create", "--wg", wg, "--text", "Two")[1]["task_id"]
    unknown = uuid7()
    hub.tasks[unknown] = {"task": {**hub.tasks[first]["task"], "task_id": unknown, "number": 3}}
    code, grouped = tool.run("task", "list", "--wg", wg, "--by-person")
    assert code == 0, grouped
    assert [(p["owner_id"], p["you"], [t["task_id"] for t in p["tasks"]]) for p in grouped["people"]] == [
        (account.id, True, [second, first]),  # newest first, as the list
        (None, False, [unknown]),
    ]
    assert grouped["people"][1]["name"] is None
    org = grouped["org_id"]
    for argv in (("--org", org, "--state", "working", "--by-person"), ("--org", org, "--by-person")):
        assert tool.run("task", "list", *argv)[1]["error"] == "invalid_argument", argv


def test_attention_entries_name_their_main_agent(tool: Tool, hub: HubFake, account: User) -> None:
    world = World(tool, hub, account)
    task = world.task(TITLE)
    hub.attention[task] = record(task, "working", "working", "2026-11-24T08:00:00.000Z")
    entry = tool.run("task", "list", "--org", world.org, "--state", "working")[1]["tasks"][0]
    assert (entry["agent"], entry["number"], entry["title"]) == (f"{AUTOMATIC} #1", 1, TITLE)


def test_audit_names_agent_actors_on_behalf_of_people(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    other, bob, org, wg = reader(tool, hub, tmp_path)
    task = tool.run("task", "create", "--wg", wg, "--text", TITLE)[1]["task_id"]
    secret = str(tool.run("wg", "create", "--org", org, "--name", "Secret")[1]["wg_id"])
    hidden = tool.run("task", "create", "--wg", secret, "--text", "Hidden")[1]["task_id"]
    main, sub, unknown, broken, nowhere, elsewhere = (uuid7() for _ in range(6))
    someone = uuid7()
    rows = views(hub).audit
    rows += [
        audit_row(org, main, actor_kind="main_agent", action="operation.succeeded", wg_id=wg, on_behalf_of=someone),
        audit_row(org, sub, actor_kind="subagent", action="operation.failed", wg_id=wg),
        audit_row(org, unknown, actor_kind="subagent", action="operation.failed", wg_id=wg),
        audit_row(org, broken, actor_kind="subagent", action="operation.failed", wg_id=wg),
        audit_row(org, nowhere, actor_kind="main_agent", action="session.requested"),
        audit_row(org, elsewhere, actor_kind="main_agent", action="operation.failed", wg_id=secret),
        audit_row(org, main, actor_kind="main_agent", action="x.y", wg_id=wg, on_behalf_of=5),
    ]
    actors = views(hub).agent_actors
    actors[main] = {"task_id": task, "number": 1, "agent_path": None, "parent_session_id": None}
    actors[sub] = {"task_id": task, "number": 1, "agent_path": "1", "parent_session_id": main}
    actors[broken] = {"task_id": task, "number": 1, "agent_path": "0", "parent_session_id": main}
    actors[nowhere] = {"task_id": uuid7(), "number": 7, "agent_path": None, "parent_session_id": None}
    actors[elsewhere] = {"task_id": hidden, "number": 2, "agent_path": None, "parent_session_id": None}
    code, shown = tool.run("audit", "--org", org)
    assert code == 0, shown
    texts = {row["actor_id"]: row.get("actor_text") for row in shown["rows"]}
    assert texts == {
        main: f"Main agent {AUTOMATIC} #1 on behalf of {someone}",
        sub: f"Subagent #1.1 of {AUTOMATIC} #1 (no single person)",
        unknown: None,
        broken: None,
        nowhere: "Main agent #7 (no single person)",
        elsewhere: "Main agent Hidden #2 (no single person)",
    }
    # Bob cannot read the secret workgroup: its agent is shown by its handle (his own rows stay visible to him)
    views(hub).audit.append(
        audit_row(org, elsewhere, actor_kind="main_agent", action="operation.failed", wg_id=secret, actor_id=bob.id)
    )
    actors[bob.id] = {"task_id": hidden, "number": 2, "agent_path": None, "parent_session_id": None}
    hub.hooks[("GET", f"/v1/scopes/{secret}/events")] = lambda body, request: (404, {"error": "not_found"})
    seen = {row["actor_id"]: row.get("actor_text") for row in other.run("audit", "--org", org)[1]["rows"]}
    assert seen[bob.id] == "Main agent #2 (no single person)"
    del hub.hooks[("GET", f"/v1/scopes/{secret}/events")]
    # the person's name where the verified profiles give one
    views(hub).audit.append(audit_row(org, main, actor_kind="main_agent", action="a.b", wg_id=wg, on_behalf_of=bob.id))
    named = tool.run("audit", "--org", org, "--on-behalf-of", bob.id)[1]["rows"]
    assert [row["action"] for row in named] == ["a.b"]
    assert named[0]["actor_text"].startswith(f"Main agent {AUTOMATIC} #1 on behalf of ")
    assert tool.run("audit", "--org", org, "--on-behalf-of", "x")[1]["error"] == "invalid_argument"
    assert tool.run("audit", "--on-behalf-of", bob.id)[1]["error"] == "invalid_argument"
    # a page without agents, and a hub that sends no actors
    assert [row["action"] for row in tool.run("audit", "--org", org, "--actor", account.id)[1]["rows"]] == []
    hub.hooks[("GET", f"/v1/orgs/{org}/audit")] = lambda body, request: (200, {"rows": rows[:1], "next_before": None})
    assert tool.run("audit", "--org", org)[1]["rows"][0].get("actor_text") is None
