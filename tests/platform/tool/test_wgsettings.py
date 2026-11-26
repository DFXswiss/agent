"""`repos list|allow|disallow` and `wg settings` against the hub double: the `settings.workgroup` event sealed for the
workgroup, the newest verified one wins, the contract's limits, and the write right."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_platform import wgsettings
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from .conftest import Tool
from .hubfake import HubFake, User, now_ms_text
from .seeding import default_wg, private_org, reader


def own_event(hub: HubFake, user: User, wg: str, body: object) -> None:
    """A `settings.workgroup` event of the user written by another client."""
    org = private_org(hub, user)
    log = hub.orgs[org].log
    header = new_header(
        id=uuid7(),
        org=org,
        scope=WG,
        scope_id=wg,
        epoch=log.state.epoch(WG, wg),
        author=user.id,
        kind=wgsettings.KIND,
        ts=now_ms_text(),
        lh=log.head.seq,
        wg=wg,
    )
    hub.store_event(wg, seal_event(hub.org_key(org, user, WG, wg), header, body_json(body), user.identity.sig))


def test_repository_list(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    listed = tool.run("repos", "list", "--wg", wg)[1]
    assert (listed["repositories"], listed["all_repositories_allowed"], listed["seq"]) == ([], True, 0)
    code, allowed = tool.run("repos", "allow", "--wg", wg, "--repo", "Acme/App", "--repo", " acme/app ")
    assert code == 0, allowed
    assert (allowed["repositories"], allowed["changed"], allowed["all_repositories_allowed"]) == (
        ["acme/app"],
        True,
        False,
    )
    assert b"acme/app" not in b"".join(hub.bodies)
    assert hub.events[wg][-1]["event"]["header"]["kind"] == "settings.workgroup"
    assert tool.run("repos", "allow", "--wg", wg, "--repo", "acme/app")[1]["changed"] is False
    tool.run("repos", "allow", "--wg", wg, "--repo", "acme/lib")
    assert tool.run("repos", "list", "--wg", wg)[1]["repositories"] == ["acme/app", "acme/lib"]
    removed = tool.run("repos", "disallow", "--wg", wg, "--repo", "acme/app", "--repo", "acme/none")[1]
    assert (removed["repositories"], removed["changed"]) == (["acme/lib"], True)
    assert tool.run("repos", "disallow", "--wg", wg, "--repo", "acme/lib")[1]["all_repositories_allowed"] is True
    # the other settings stay as they were
    shown = tool.run("wg", "settings", "--wg", wg)[1]
    assert shown["settings"]["protected_branches"] == list(wgsettings.DEFAULT_PROTECTED)
    for argv in (("--repo", "no slash"), ("--repo", "a" * 40 + "/b"), ()):
        assert tool.run("repos", "allow", "--wg", wg, *argv)[1]["error"] == "invalid_argument", argv
    many = [arg for index in range(wgsettings.MAX_REPOSITORIES + 1) for arg in ("--repo", f"acme/r{index}")]
    assert tool.run("repos", "allow", "--wg", wg, *many)[1]["reason"] == "too_many"
    assert tool.run("repos", "list", "--wg", "x")[1]["error"] == "invalid_argument"
    assert tool.run("repos", "list", "--wg", uuid7())[1]["error"] == "no_org"


def test_workgroup_settings(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    shown = tool.run("wg", "settings", "--wg", wg)[1]
    assert (shown["status"], shown["settings"], shown["writable"]) == ("none", wgsettings.DEFAULTS, True)
    code, saved = tool.run(
        "wg",
        "settings",
        "--wg",
        wg,
        "--protected-branch",
        "main",
        "--protected-branch",
        "release/*",
        "--protected-branch",
        "main",
        "--pr-draft-default",
        "false",
    )
    assert code == 0, saved
    expected = {"repositories": [], "protected_branches": ["main", "release/*"], "pr_draft_default": False}
    assert (saved["status"], saved["settings"], saved["author"]) == ("set", expected, account.id)
    shown = tool.run("wg", "settings", "--wg", wg)[1]
    assert (shown["status"], shown["settings"], shown["seq"]) == ("set", expected, saved["seq"])
    assert tool.run("wg", "settings", "--wg", wg, "--pr-draft-default", "true")[1]["settings"]["pr_draft_default"]
    only = tool.run("wg", "settings", "--wg", wg, "--protected-branch", "main")[1]["settings"]
    assert (only["protected_branches"], only["pr_draft_default"]) == (["main"], True)
    # events of another scope that a hub hands over are left out
    org_events = hub.events[private_org(hub, account)]
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (
        200,
        {"events": org_events, "next_after": None},
    )
    foreign = tool.run("wg", "settings", "--wg", wg)[1]
    assert (foreign["status"], foreign["unverified"]) == ("none", len(org_events))
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    assert tool.run("wg", "settings", "--wg", wg, "--protected-branch", "bad branch")[1]["reason"] == "pattern_invalid"
    many = [arg for index in range(wgsettings.MAX_PATTERNS + 1) for arg in ("--protected-branch", f"b{index}")]
    assert tool.run("wg", "settings", "--wg", wg, *many)[1]["reason"] == "too_many"
    # a newer event whose body breaks the contract: invalid, the defaults apply
    own_event(hub, account, wg, {"repositories": ["Not Lower/Case"], "protected_branches": [], "pr_draft_default": 1})
    shown = tool.run("wg", "settings", "--wg", wg)[1]
    assert (shown["status"], shown["settings"]) == ("invalid", wgsettings.DEFAULTS)
    own_event(hub, account, wg, {**wgsettings.DEFAULTS, "pr_draft_default": "yes"})
    own_event(hub, account, wg, {**wgsettings.DEFAULTS, "repositories": ["x"]})
    own_event(hub, account, wg, ["not", "an", "object"])
    assert tool.run("wg", "settings", "--wg", wg)[1]["status"] == "invalid"
    # without the workgroup key every event is locked; a save cannot be sealed
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg"]
    assert tool.run("wg", "settings", "--wg", wg)[1]["status"] == "none"
    assert tool.run("repos", "allow", "--wg", wg, "--repo", "acme/app")[1]["reason"] == "epoch_stale"


def test_settings_need_write_access(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    other, _bob, _org, wg = reader(tool, hub, tmp_path)
    assert tool.run("repos", "allow", "--wg", wg, "--repo", "acme/app")[1]["changed"] is True
    shown = other.run("repos", "list", "--wg", wg)[1]
    assert shown["repositories"] == ["acme/app"]
    assert other.run("wg", "settings", "--wg", wg)[1]["writable"] is False
    assert other.run("repos", "allow", "--wg", wg, "--repo", "acme/lib")[1]["error"] == "no_write_access"


def test_answers_must_name_the_event(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    answers: list[Any] = [{"results": []}, {"results": [{"id": "x"}]}, {"results": "x"}]
    for answer in answers:

        def fixed(body: Any, request: Any, answer: Any = answer) -> tuple[int, Any]:
            return 200, answer

        hub.hooks[("POST", "/v1/events")] = fixed
        assert tool.run("repos", "allow", "--wg", wg, "--repo", "acme/app")[1]["reason"] == "response_invalid"

    def wrong_seq(body: Any, request: Any) -> tuple[int, Any]:
        event = body["events"][0]["event"]
        return 200, {"results": [{"id": event["header"]["id"], "scope_seq": True}]}

    hub.hooks[("POST", "/v1/events")] = wrong_seq
    assert tool.run("repos", "allow", "--wg", wg, "--repo", "acme/app")[1]["reason"] == "response_invalid"
    assert wgsettings.check_body({**wgsettings.DEFAULTS, "repositories": "x"}) is None


def test_workgroup_rename(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    other, _bob, org, wg = reader(tool, hub, tmp_path)
    code, renamed = tool.run("wg", "rename", "--wg", wg, "--name", "  Research and Development ")
    assert code == 0, renamed
    assert renamed["name"] == "Research and Development"
    listed = {row["wg_id"]: row["name"] for row in tool.run("wg", "list", "--org", org)[1]["workgroups"]}
    assert listed[wg] == "Research and Development"
    assert other.run("wg", "rename", "--wg", wg, "--name", "Mine")[1]["error"] == "not_admin"
    assert tool.run("wg", "rename", "--wg", wg, "--name", " ")[1]["error"] == "invalid_argument"
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "org" or wrap["recipient_id"] != account.id]
    assert tool.run("wg", "rename", "--wg", wg, "--name", "X")[1]["reason"] == "epoch_stale"


def test_merge_criteria_and_separate_duties(tool: Tool, hub: HubFake, account: User) -> None:
    """KOR-51 (ADR-094 sections 5 and 6): optional, off by default, set one by one."""
    wg = default_wg(hub, account)
    code, saved = tool.run(
        "wg",
        "settings",
        "--wg",
        wg,
        "--merge-verdicts",
        "1",
        "--merge-checks-green",
        "true",
        "--separate-duties",
        "true",
    )
    assert code == 0, saved
    assert saved["settings"]["merge_criteria"] == {"passed_verdicts": 1, "checks_green": True}
    assert saved["settings"]["separate_duties"] is True
    more = tool.run(
        "wg",
        "settings",
        "--wg",
        wg,
        "--merge-verdict-profile",
        "qa",
        "--merge-approvals",
        "2",
        "--separate-duties",
        "false",
    )[1]["settings"]
    assert more["merge_criteria"] == {
        "passed_verdicts": 1,
        "checks_green": True,
        "verdict_profile": "qa",
        "github_approvals": 2,
    }
    assert more["separate_duties"] is False
    shown = tool.run("wg", "settings", "--wg", wg)[1]
    assert (shown["status"], shown["settings"]["merge_criteria"]["verdict_profile"]) == ("set", "qa")
    assert tool.run("wg", "settings", "--wg", wg, "--merge-verdicts", "-1")[1]["reason"] == "count_invalid"
    assert tool.run("wg", "settings", "--wg", wg, "--merge-verdict-profile", "Bad")[1]["reason"] == "profile_invalid"
    bads: list[dict[str, Any]] = [
        {"merge_criteria": []},
        {"merge_criteria": {"other": 1}},
        {"merge_criteria": {"passed_verdicts": True}},
        {"merge_criteria": {"verdict_profile": "Bad"}},
        {"merge_criteria": {"checks_green": 1}},
        {"separate_duties": "yes"},
    ]
    for bad in bads:
        assert wgsettings.check_body({**wgsettings.DEFAULTS, **bad}) is None, bad
    assert wgsettings.merge_settings({"repositories": []}, {}) == {"repositories": []}
