"""`pr list|show|link|unlink|fix-conflicts` (KOR-48, ADR-084 sections 1-3 and 6, 11-agent-access.md section 8.4)
against the hub double: the sealed pull-request index opened and checked on this computer, the fresh state read
through the device request `github.pull_status`, and the fixed platform instruction sent as an ordinary prompt; the
instruction is the same text as the web client's `web/src/core/pulls/instruction.ts`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from agent_platform import devicereq, pulls, pullstate
from agent_platform.errors import PlatformError
from agent_runner.crypto import context as crypto
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from .conftest import Tool
from .fakes.pulls import device_link, index
from .fakes.relay import opened, relay
from .hubfake import HubFake, User
from .seeding import assign_device, new_task, reader

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTRUCTION_TS = REPO_ROOT / "web" / "src" / "core" / "pulls" / "instruction.ts"
BRIEF_TEXT = """Platform instruction "fix merge conflicts" (v1) for pull request Octo/Repo#5:
1. Call github.merge_base with {"repo": "Octo/Repo", "number": 5}.
2. If the result says up_to_date, say so and stop.
3. Resolve every listed conflict in the workspace directory of the repository, keeping the intent of both sides; \
remove every conflict marker; record a decision for every conflict without markers.
4. If the repository has tests, run them with test.run on the resolved workspace and fix what the merge broke \
before you commit.
5. Call github.commit with merge: true on the pull request's branch, then github.push of that commit to the same \
branch.
6. Never merge, close or force-push the pull request.
7. If a resolution needs a decision of a person, ask with human.question and wait for the answer."""
JS_STRING = re.compile(r"'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\"|`((?:\\.|[^`\\])*)`")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)
    monkeypatch.setattr(pulls, "RECHECK_S", 0.0)


def state(repo: str, number: int, **changes: Any) -> dict[str, Any]:
    """A found `pull_state` with a conflict (changed by `changes`)."""
    return {
        "repo": repo,
        "number": number,
        "found": True,
        "title": "Add the login",
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": False,
        "mergeable_state": "dirty",
        "head_ref": "feature/login",
        "head_sha": "a" * 40,
        "head_repo": repo,
        "base_ref": "main",
        "url": f"https://github.com/{repo}/pull/{number}",
        "updated_at": "2026-11-24T10:00:00Z",
    } | changes


def answers(**changes: dict[str, Any]) -> Any:
    """A device answer for whatever the request names: a conflict, or `changes[<lower repo>_<number>]`."""

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        found = []
        for item in body["pulls"]:
            key = f"{item['repo'].lower().replace('/', '_').replace('-', '_')}_{item['number']}"
            found.append(state(item["repo"], item["number"]) | changes.get(key, {}))
        return {"step": "done", "pulls": found}

    return answer


def script(hub: HubFake, *bodies: object) -> None:
    relay(hub).script["github.pull_status"] = [[(body, True)] for body in bodies]


def setup(tool: Tool, hub: HubFake, user: User) -> tuple[str, str, str, str]:
    """`(org, wg, device, task)`: a device of the default workgroup and a task in it."""
    org, wg, device = assign_device(tool, hub, user)
    return org, wg, device, new_task(tool, wg)


def body_of(hub: HubFake, request: dict[str, Any]) -> dict[str, Any]:
    """The request body as the device opens it."""
    found = opened(hub, request["request_id"])
    assert isinstance(found, dict)
    return found


def requests_of(hub: HubFake) -> list[dict[str, Any]]:
    return [stored for stored in relay(hub).requests.values() if stored["kind"] == "github.pull_status"]


# --- the instruction ---


def test_instruction_is_the_fixed_text_v1() -> None:
    assert pulls.instruction("Octo/Repo", 5) == BRIEF_TEXT
    assert not pulls.instruction("a/b", 1).endswith("\n")


def test_instruction_equals_the_web_clients_text() -> None:
    assert INSTRUCTION_TS.exists(), f"the web client's instruction is missing: {INSTRUCTION_TS}"
    source = INSTRUCTION_TS.read_text(encoding="utf-8")
    version = re.search(r"export const INSTRUCTION_VERSION = (\d+);", source)
    assert version is not None
    assert version.group(1) == str(pulls.INSTRUCTION_VERSION)
    array = source.split("return [", 1)[1].split("].join('\\n')", 1)[0]
    lines = []
    for match in JS_STRING.finditer(array):
        single, double, template = match.groups()
        text = (single if single is not None else double if double is not None else template).replace("\\'", "'")
        lines.append(
            text.replace("${INSTRUCTION_VERSION}", str(pulls.INSTRUCTION_VERSION))
            .replace("${repo}", "{repo}")
            .replace("${number}", "{number}")
        )
    assert "\n".join(lines) == pulls.INSTRUCTION


# --- addresses and states ---


@pytest.mark.parametrize(
    ("text", "pull"),
    [
        ("https://github.com/Octo/Repo/pull/5", ("Octo/Repo", 5)),
        ("  https://www.github.com/octo/re.po/pull/12/files?x=1#y ", ("octo/re.po", 12)),
        ("HTTPS://GITHUB.COM/a-b/c_d/pull/7#discussion", ("a-b/c_d", 7)),
        ("Octo/Repo#2147483647", ("Octo/Repo", 2147483647)),
    ],
)
def test_addresses(text: str, pull: tuple[str, int]) -> None:
    assert pulls.parse_ref(text) == pull


@pytest.mark.parametrize(
    "text",
    [
        "Octo/Repo#2147483648",
        "Octo/Repo#0",
        "octo/..#1",
        "octo/.#1",
        "http://github.com/a/b/pull/1",
        "https://github.com/a/b/issues/1",
        "o_x/r#1",
        "a/b",
        "",
    ],
)
def test_addresses_refused(text: str) -> None:
    with pytest.raises(PlatformError) as refused:
        pulls.parse_ref(text)
    assert (refused.value.code, refused.value.fields["reason"]) == ("invalid_argument", "pull_request")


def test_pull_states() -> None:
    found = state("Octo/Repo", 5)
    assert pullstate.valid_response({"step": "done", "pulls": [found, {**found, "number": 6, "mergeable": None}]})
    missing = {"repo": "Octo/Repo", "number": 7, "found": False, "code": "repo_not_allowed"}
    assert pullstate.valid_response({"step": "done", "pulls": [missing]})
    assert pullstate.valid_pull({**found, "head_repo": None, "url": "http://127.0.0.1:9/a/b/pull/1"})
    for wrong in (
        "x",
        {**found, "repo": "no repo"},
        {**found, "number": 0},
        {**found, "number": True},
        {**found, "found": "yes"},
        {**found, "extra": 1},
        {**found, "title": "t" * 1025},
        {**found, "state": "draft"},
        {**found, "draft": 1},
        {**found, "merged": None},
        {**found, "mergeable": 0},
        {**found, "mergeable_state": "Dirty"},
        {**found, "head_ref": ""},
        {**found, "head_sha": "a" * 64},
        {**found, "head_repo": "x"},
        {**found, "base_ref": "a b"},
        {**found, "url": "ftp://x"},
        {**found, "updated_at": 1},
        {**found, "updated_at": "yesterday"},
        {**missing, "code": "gone"},
        {**missing, "title": "x"},
    ):
        assert not pullstate.valid_pull(wrong), wrong
    for body in (
        {"step": "done", "pulls": []},
        {"step": "done", "pulls": [found] * 51},
        {"step": "done", "pulls": "x"},
        {"step": "done", "pulls": [found], "x": 1},
        {"step": "done", "pulls": ["x"]},
    ):
        assert not pullstate.valid_response(body)
    assert devicereq.response_body("github.pull_status", {"step": "failed", "code": "github_not_connected"})
    assert devicereq.response_body("github.pull_status", {"step": "failed", "code": "forbidden"})
    cases: list[tuple[dict[str, Any], str, bool]] = [
        ({}, "conflict", True),
        ({"mergeable": True}, "clean", False),
        ({"mergeable": None}, "checking", False),
        ({"state": "closed"}, "closed", False),
        ({"state": "closed", "merged": True}, "merged", False),
        ({"head_repo": "Fork/Repo"}, "conflict", False),
        ({"head_repo": None}, "conflict", False),
        ({"head_repo": "octo/repo"}, "conflict", True),
    ]
    for changes, merge, fix in cases:
        assert (pullstate.merge_state(found | changes), pullstate.fixable(found | changes)) == (merge, fix), changes
    assert (pullstate.merge_state(missing), pullstate.fixable(missing)) == (None, False)


# --- list and show ---


def test_list_and_show_with_the_fresh_state(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device, task = setup(tool, hub, account)
    first = device_link(hub, account, task, "Octo/Repo", 5)
    device_link(hub, account, task, "Octo/Other", 6)
    script(hub, answers(octo_other_6={"mergeable": True}))
    code, listed = tool.run("pr", "list", "--wg", wg)
    assert code == 0, listed
    assert listed["unverified"] == 0
    assert [(i["repo"], i["number"], i["merge_state"], i["origin"]) for i in listed["links"]] == [
        ("Octo/Other", 6, "clean", "device"),
        ("Octo/Repo", 5, "conflict", "device"),
    ]
    item = listed["links"][1]
    assert (item["org_id"], item["wg_id"], item["task_id"], item["title"]) == (org, wg, task, "Fix the login bug")
    assert (item["session_id"], item["linked_by"], item["details"], item["status_error"]) == (
        first["session_id"],
        None,
        "available",
        None,
    )
    assert item["pull"]["head_ref"] == "feature/login"
    (request,) = requests_of(hub)
    assert request["device_id"] == device
    assert body_of(hub, request) == {
        "org_id": org,
        "wg_id": wg,
        "pulls": [{"repo": "Octo/Other", "number": 6}, {"repo": "Octo/Repo", "number": 5}],
    }
    # the hub never saw a repository or a number
    assert b"Octo" not in b"".join(hub.bodies)
    # without the state, by task, by org; --limit cuts the list
    bare = tool.run("pr", "list", "--task", task, "--no-status")[1]
    assert [i["number"] for i in bare["links"]] == [6, 5]
    assert "pull" not in bare["links"][0]
    assert len(requests_of(hub)) == 1
    script(hub, answers())
    by_org = tool.run("pr", "list", "--org", org, "--limit", "1")[1]
    assert (len(by_org["links"]), by_org["limited"]) == (1, {"links": 2})
    assert body_of(hub, requests_of(hub)[-1])["wg_id"] == wg
    assert tool.run("pr", "list", "--org", uuid7())[1]["error"] == "no_org"
    # show: the link with its task and session, the state, whether "Fix conflicts" applies
    script(hub, answers())
    code, shown = tool.run("pr", "show", "https://github.com/octo/repo/pull/5")
    assert code == 0, shown
    assert (shown["repo"], shown["number"], shown["merge_state"], shown["fixable"], shown["status_error"]) == (
        "octo/repo",
        5,
        "conflict",
        True,
        None,
    )
    assert [(link["task_id"], link["repo"], link["title"]) for link in shown["links"]] == [
        (task, "Octo/Repo", "Fix the login bug")
    ]
    assert body_of(hub, requests_of(hub)[-1]) == {
        "org_id": org,
        "wg_id": wg,
        "task_id": task,
        "pulls": [{"repo": "Octo/Repo", "number": 5}],
    }
    assert "pull" not in tool.run("pr", "show", "Octo/Repo#5", "--no-status")[1]
    # an unknown pull request: no links; its state only with a workgroup
    unknown = tool.run("pr", "show", "Octo/Repo#9")[1]
    assert (unknown["links"], unknown["pull"], unknown["status_error"]) == ([], None, "no_workgroup")
    script(hub, answers(octo_repo_9={"mergeable": None}), answers(octo_repo_9={"mergeable": None}))
    checking = tool.run("pr", "show", "Octo/Repo#9", "--wg", wg)[1]
    assert (checking["merge_state"], checking["fixable"]) == ("checking", False)
    assert "task_id" not in body_of(hub, requests_of(hub)[-1])
    assert len(requests_of(hub)) == 5  # asked once more after RECHECK_S


def test_rows_that_do_not_verify_are_left_out(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, _device, task = setup(tool, hub, account)
    other = new_task(tool, wg, "Other")
    device_link(hub, account, task, "Octo/Repo", 5)
    device_link(hub, account, task, "Octo/Old", 4, sealed=False)
    moved = device_link(hub, account, other, "Octo/Moved", 3)
    moved["task_id"] = task  # a hub that moves a link to another task
    rows = index(hub).rows
    rows.append({**rows[0], "epoch": 99})  # an epoch whose key the user does not hold
    for broken in ({"epoch": True}, {"tag": "short"}, {"task_id": "x"}, {"linked_by": uuid7()}, {"sealed": 5}):
        rows.append({**rows[0], **broken})
    rows.append({**rows[0], "session_id": None, "linked_by": None})
    listed = tool.run("pr", "list", "--wg", wg, "--no-status")[1]
    assert listed["unverified"] == 7
    assert [(i["details"], i["repo"], i["number"], i["epoch"]) for i in listed["links"]] == [
        ("locked", None, None, 99),
        ("unavailable", None, None, 1),
        ("available", "Octo/Repo", 5, 1),
    ]
    # a hub that answers rows of another task for this one
    foreign = device_link(hub, account, other, "Octo/Foreign", 8)
    path = f"/v1/workgroups/{wg}/pr-links"
    only = {k: v for k, v in foreign.items() if k != "wg"}
    hub.hooks[("GET", path)] = lambda body, request: (200, {"items": [only], "next_cursor": None})
    assert tool.run("pr", "list", "--task", task, "--no-status")[1] == {"ok": True, "links": [], "unverified": 1}
    # nothing to read when no link names a pull request
    old = {k: v for k, v in rows[1].items() if k != "wg"}
    hub.hooks[("GET", path)] = lambda body, request: (200, {"items": [old], "next_cursor": None})
    before = len(requests_of(hub))
    assert tool.run("pr", "list", "--wg", wg)[1]["links"][0]["status_error"] is None
    assert len(requests_of(hub)) == before
    del hub.hooks[("GET", path)]
    # an older runner's link: found by its tag, the details from the address
    script(hub, answers())
    shown = tool.run("pr", "show", "octo/old#4")[1]
    assert [(link["details"], link["repo"]) for link in shown["links"]] == [("unavailable", "octo/old")]
    assert shown["merge_state"] == "conflict"


def test_paging_and_batches(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, _device, task = setup(tool, hub, account)
    for number in range(1, 202):
        device_link(hub, account, task, "Octo/Repo", number)
    script(hub, *[answers()] * 5)
    listed = tool.run("pr", "list", "--wg", wg)[1]
    assert [i["number"] for i in listed["links"]] == list(range(201, 0, -1))
    assert {i["merge_state"] for i in listed["links"]} == {"conflict"}
    assert [len(body_of(hub, r)["pulls"]) for r in requests_of(hub)] == [50, 50, 50, 50, 1]
    cursors = [r.query.get("cursor") for r in hub.requests if r.path == f"/v1/workgroups/{wg}/pr-links"]
    assert cursors == [None, "200"]
    # a hub that answers the same cursor again ends the paging
    hub.hooks[("GET", f"/v1/workgroups/{wg}/pr-links")] = lambda body, request: (
        200,
        {"items": [], "next_cursor": request.query.get("cursor", "x")},
    )
    assert tool.run("pr", "list", "--wg", wg, "--no-status")[1]["links"] == []


def test_status_errors(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, _device, task = setup(tool, hub, account)
    device_link(hub, account, task, "Octo/Repo", 5)

    def status_error(*argv: str) -> object:
        return tool.run("pr", "list", "--wg", wg, *argv)[1]["links"][0]["status_error"]

    script(hub, {"step": "failed", "code": "github_not_connected"})
    assert status_error() == "github_not_connected"
    assert status_error("--wait", "0") == "timeout"
    script(hub, {"step": "done", "pulls": [state("Octo/Other", 1)]})
    assert status_error() == "response_invalid"
    hub.devices[account.id] = []
    assert status_error() == "no_device"
    # anything else fails the command
    del hub.devices[account.id]
    hub.hooks[("GET", "/v1/me/devices")] = lambda body, request: (403, {"error": "forbidden"})
    assert tool.run("pr", "list", "--wg", wg)[1]["hub_error"] == "forbidden"
    for argv in (("--wait", "601"), ("--wait", "-1")):
        assert tool.run("pr", "list", "--wg", wg, *argv)[1]["error"] == "invalid_argument"


def test_the_device_of_the_task_first(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device, task = setup(tool, hub, account)
    device_link(hub, account, task, "Octo/Repo", 5)
    session = uuid7()
    hub.tasks[task]["task"]["main_session_id"] = session
    hub.tasks[task]["sessions"] = [{"session": {"session_id": session, "state": "working", "device_id": device}}]
    script(hub, answers())
    assert tool.run("pr", "show", "Octo/Repo#5")[1]["merge_state"] == "conflict"
    hub.tasks[task]["sessions"] = [{"session": {"session_id": session, "state": "working", "device_id": None}}]
    script(hub, answers())
    assert tool.run("pr", "list", "--task", task)[1]["links"][0]["merge_state"] == "conflict"


def test_device_order(monkeypatch: pytest.MonkeyPatch) -> None:
    wg, keys = uuid7(), (b"k" * 32, b"s" * 32)
    a, b, c, d = sorted(uuid7() for _ in range(4))
    rows = [
        {"device_id": a, "online": False, "wg_ids": [wg]},
        {"device_id": b, "online": True, "wg_ids": [wg]},
        {"device_id": c, "online": True, "wg_ids": [uuid7()]},
        {"device_id": d, "online": True, "wg_ids": "x"},
        {"device_id": 5, "online": True, "wg_ids": [wg]},
    ]

    class Stub:
        def call(self, method: str, path: str) -> dict[str, Any]:
            return {"devices": rows}

    known = {a: keys, b: keys}
    monkeypatch.setattr(pulls, "device_keys", lambda account, device: known.get(device))
    stub: Any = Stub()
    assert pulls.status_device(stub, wg, None) == (b, keys)
    assert pulls.status_device(stub, wg, a) == (a, keys)
    del known[b]
    assert pulls.status_device(stub, wg, c) == (a, keys)
    known.clear()
    with pytest.raises(PlatformError, match="no_device"):
        pulls.status_device(stub, wg, None)


# --- link and unlink ---


def test_link_and_unlink(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, _device, task = setup(tool, hub, account)
    other = new_task(tool, wg, "Other")
    code, linked = tool.run("pr", "link", "https://github.com/Octo/Repo/pull/5", "--task", task)
    assert code == 0, linked
    link = linked["link"]
    assert linked["created"] is True
    assert (link["task_id"], link["linked_by"], link["origin"], link["repo"], link["number"]) == (
        task,
        account.id,
        "person",
        "Octo/Repo",
        5,
    )
    assert (link["org_id"], link["wg_id"], link["title"]) == (org, wg, "Fix the login bug")
    assert link["details"] == "available"
    assert b"Octo" not in b"".join(hub.bodies)
    # linked already: reported with its task, never moved
    again = tool.run("pr", "link", "octo/repo#5", "--task", other)[1]
    assert (again["created"], again["link"]["task_id"]) == (False, task)
    # the hub found it linked meanwhile (the lookup missed it): its answer names the task that keeps it
    epoch = hub.orgs[org].log.state.epoch(WG, wg)
    tag = crypto.pr_link_tag(crypto.pr_link_key(hub.org_key(org, account, WG, wg)), "octo/repo", 5)
    single = f"/v1/workgroups/{wg}/pr-links/{epoch}/{tag}"
    hub.hooks[("GET", single)] = lambda body, request: (404, {"error": "not_found"})
    raced = tool.run("pr", "link", "octo/repo#5", "--task", other)[1]
    assert (raced["created"], raced["link"]["task_id"]) == (False, task)
    # a row that does not verify is no link
    hub.hooks[("GET", single)] = lambda body, request: (200, {"task_id": task, "sealed": None})
    assert tool.run("pr", "show", "octo/repo#5", "--no-status")[1]["links"] == []
    # a lookup the hub refuses otherwise fails the command
    hub.hooks[("GET", single)] = lambda body, request: (403, {"error": "forbidden"})
    assert tool.run("pr", "show", "octo/repo#5", "--no-status")[1]["hub_error"] == "forbidden"
    del hub.hooks[("GET", single)]
    # answers that do not name the pull request just linked
    path = f"/v1/workgroups/{wg}/pr-links"
    stored = {k: v for k, v in index(hub).rows[0].items() if k != "wg"}
    hub.hooks[("POST", path)] = lambda body, request: (
        200,
        {"created": False, "link": {**stored, "tag": (body or {})["tag"]}},
    )
    assert tool.run("pr", "link", "octo/repo#6", "--task", other)[1]["reason"] == "response_invalid"
    hub.hooks[("POST", path)] = lambda body, request: (200, {"created": True, "link": "x"})
    assert tool.run("pr", "link", "octo/repo#6", "--task", other)[1]["reason"] == "response_invalid"
    del hub.hooks[("POST", path)]
    # a device link stays, a person's link goes
    device_link(hub, account, other, "Octo/Repo", 7)
    tool.run("pr", "link", "Octo/Repo#7", "--task", other)  # reported, nothing new
    code, unlinked = tool.run("pr", "unlink", "Octo/Repo#5")
    assert code == 0, unlinked
    assert ([i["task_id"] for i in unlinked["unlinked"]], unlinked["kept"]) == ([task], [])
    kept = tool.run("pr", "unlink", "Octo/Repo#7", "--wg", wg)[1]
    assert (kept["unlinked"], [i["origin"] for i in kept["kept"]]) == ([], ["device"])
    assert tool.run("pr", "unlink", "Octo/Repo#5")[1]["error"] == "pr_not_linked"
    assert tool.run("pr", "show", "Octo/Repo#5", "--no-status")[1]["links"] == []


def test_link_refusals(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _org, _wg, _device, task = setup(tool, hub, account)
    assert tool.run("pr", "link", "nonsense", "--task", task)[1]["reason"] == "pull_request"
    assert tool.run("pr", "link", "a/b#1", "--task", "x")[1]["error"] == "invalid_argument"
    # without the key of the workgroup's current epoch nothing is sealed
    with monkeypatch.context() as patch:
        patch.setattr(pulls, "seal_epoch", lambda state, scope, wg: 99)
        assert tool.run("pr", "link", "a/b#1", "--task", task)[1]["reason"] == "epoch_stale"
    # a reader may look, not link
    other, _bob, _org2, read_wg = reader(tool, hub, tmp_path)
    shared = new_task(tool, read_wg, "Shared")
    refused = other.run("pr", "link", "a/b#1", "--task", shared)[1]
    assert (refused["error"], refused["hub_error"]) == ("hub_refused", "forbidden")
    assert other.run("pr", "show", "a/b#1", "--wg", read_wg, "--no-status")[1]["links"] == []


# --- fix conflicts ---


def test_fix_conflicts_sends_the_instruction(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, _device, task = setup(tool, hub, account)
    device_link(hub, account, task, "Octo/Repo", 5)
    script(hub, answers())
    code, sent = tool.run("pr", "fix-conflicts", "https://github.com/octo/repo/pull/5")
    assert code == 0, sent
    text = pulls.instruction("Octo/Repo", 5)
    assert (sent["task_id"], sent["repo"], sent["instruction"], sent["instruction_version"]) == (
        task,
        "Octo/Repo",
        text,
        1,
    )
    assert (sent["merge_state"], sent["pull"]["mergeable"]) == ("conflict", False)
    assert isinstance(sent["scope_seq"], int)
    shown = tool.run("task", "show", "--task", task)[1]
    assert shown["events"][-1]["body"]["text"] == text
    assert shown["events"][-1]["author"] == account.id
    assert text.encode() not in b"".join(hub.bodies)
    # GitHub's spelling from the state wins
    script(hub, answers(octo_repo_5={"repo": "OCTO/repo"}))
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--task", task)[1]["repo"] == "OCTO/repo"


def test_fix_conflicts_needs_a_conflict(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, _device, task = setup(tool, hub, account)
    device_link(hub, account, task, "Octo/Repo", 5)
    for changes, reason in (
        ({"mergeable": True}, "clean"),
        ({"state": "closed"}, "closed"),
        ({"merged": True, "state": "closed"}, "merged"),
        ({"head_repo": "Fork/Repo"}, "fork"),
    ):
        script(hub, answers(octo_repo_5=changes))
        refused = tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]
        assert (refused["error"], refused["reason"]) == ("pr_no_conflict", reason), changes
    script(hub, answers(octo_repo_5={"mergeable": None}), answers(octo_repo_5={"mergeable": None}))
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]["reason"] == "checking"
    script(hub, answers(octo_repo_5={"mergeable": None}), answers())
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]["merge_state"] == "conflict"
    missing = {"repo": "Octo/Repo", "number": 5, "found": False, "code": "repo_not_accessible"}
    script(hub, {"step": "done", "pulls": [missing]})
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]["reason"] == "repo_not_accessible"
    # a failed read fails the command, with the step for a missing connection
    script(hub, {"step": "failed", "code": "github_not_connected"})
    failed = tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]
    assert (failed["error"], failed["failure"], failed["reason"]) == (
        "device_request_failed",
        "github_not_connected",
        "github_not_connected",
    )
    assert "github connect --from-gh" in failed["next"]
    script(hub, {"step": "failed", "code": "forbidden"})
    assert "reason" not in tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]
    # --anyway sends it without a read
    before = len(requests_of(hub))
    anyway = tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--anyway", "--wg", wg)[1]
    assert (anyway["repo"], anyway["pull"], anyway["merge_state"]) == ("Octo/Repo", None, None)
    assert len(requests_of(hub)) == before


def test_fix_conflicts_chooses_the_task(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, _device, task = setup(tool, hub, account)
    other = new_task(tool, wg, "Other")
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5")[1]["error"] == "pr_not_linked"
    sent = tool.run("pr", "fix-conflicts", "octo/repo#5", "--task", other, "--anyway")[1]
    assert (sent["task_id"], sent["repo"]) == (other, "octo/repo")
    device_link(hub, account, task, "Octo/Repo", 5)
    elsewhere = tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--task", other, "--anyway")[1]
    assert (elsewhere["reason"], elsewhere["tasks"]) == ("linked_elsewhere", [task])
    # linked in two workgroups to two tasks: the task must be named
    team = str(tool.run("org", "create", "--name", "Acme")[1]["org_id"])
    second_wg = str(tool.run("wg", "create", "--org", team, "--name", "Research")[1]["wg_id"])
    second = new_task(tool, second_wg, "Second")
    device_link(hub, account, second, "Octo/Repo", 5)
    ambiguous = tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--anyway")[1]
    assert (ambiguous["reason"], sorted(ambiguous["tasks"])) == ("ambiguous", sorted([task, second]))
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--task", second, "--anyway")[1]["task_id"] == second
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--wg", wg, "--anyway")[1]["task_id"] == task
    assert tool.run("pr", "fix-conflicts", "Octo/Repo#5", "--task", "x")[1]["error"] == "invalid_argument"
