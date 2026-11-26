"""`optimize` and `optimize adopt` (ADR-097 sections 1, 5, 6 and 7): the analysis task with its fixed, sealed title
and prompt and the plaintext `analysis` field, the period's defaults and bounds, the language vectors of
`contracts/tasks/analysis.json`, and the newest output written onto a whiteboard as the user."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_platform import optimize, relay
from agent_platform.errors import PlatformError
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.membership import extra
from .fakes.sessions import add_session, device_event
from .hubfake import HubFake, User
from .seeding import assign_device, new_task, reader
from .teams import team, wg_create

VECTORS = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "tasks" / "analysis.json").read_text())
NOW = datetime(2026, 11, 26, 10, 0, 0, 123456, tzinfo=UTC)
NOW_TEXT = "2026-11-26T10:00:00.123Z"
CLOCK = optimize.now


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(optimize, "now", lambda: NOW)


def posted(hub: HubFake) -> dict[str, Any]:
    """The body of the last `POST /v1/tasks`."""
    request = [r for r in hub.requests if (r.method, r.path) == ("POST", "/v1/tasks")][-1]
    return dict(json.loads(request.raw))


def wg_names(tool: Tool, org: str) -> dict[str, Any]:
    shown = tool.run("org", "show", "--org", org)[1]
    return {item["wg_id"]: item for item in shown["workgroups"]}


def show(tool: Tool, task: str) -> dict[str, Any]:
    code, shown = tool.run("task", "show", "--task", task)
    assert code == 0, shown
    return shown


# --- the fixed texts ---


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: f"{case['language']}-{case['scope']}")
def test_language_vectors(case: dict[str, Any]) -> None:
    found = optimize.brief(case["language"], case["scope"], case["name"], case["since"], case["until"])
    assert found == (case["title"], case["prompt"])


def test_the_templates_are_the_contract() -> None:
    for language in optimize.LANGUAGES:
        title, prompt = optimize.TEMPLATES[language]
        assert {"title": title, "prompt": prompt} == VECTORS["templates"][language]
        assert optimize.SCOPES[language] == VECTORS["scopes"][language]
    assert set(optimize.LANGUAGES) == set(VECTORS["templates"])


# --- the period ---


def test_period_defaults_and_bounds() -> None:
    week = optimize.iso(NOW - timedelta(days=7))
    assert optimize.period(None, None, None) == (week, NOW_TEXT)
    assert optimize.period(1, None, None) == ("2026-11-25T10:00:00.123Z", NOW_TEXT)
    assert optimize.period(92, None, "2026-11-01T00:00:00Z") == ("2026-08-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z")
    # a time zone is converted, sub-milliseconds are cut
    # an end in the future is the hub's to judge (its clock, not this computer's)
    assert optimize.period(1, None, "2026-11-26T11:00:00Z")[1] == "2026-11-26T11:00:00.000Z"
    assert optimize.period(None, "2026-11-20T12:00:00.987654+02:00", "2026-11-26T10:01:00.123Z") == (
        "2026-11-20T10:00:00.987Z",
        "2026-11-26T10:01:00.123Z",
    )
    for days, since, until in (
        (0, None, None),
        (93, None, None),
        (7, "2026-11-20T00:00:00Z", None),
        (None, "2026-11-26T10:00:00.123Z", None),  # since == until
        (None, "2026-11-27T00:00:00Z", "2026-11-26T00:00:00Z"),
        (None, "2026-08-01T00:00:00Z", "2026-11-01T00:00:00.001Z"),  # over 92 days
    ):
        with pytest.raises(PlatformError) as caught:
            optimize.period(days, since, until)
        assert caught.value.fields["reason"] == "period_invalid", (days, since, until)
    for text in ("yesterday", "2026-11-26T10:00:00"):
        with pytest.raises(PlatformError) as caught:
            optimize.period(None, text, None)
        assert caught.value.fields["reason"] == "time_invalid", text


def test_the_default_end_is_the_current_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(optimize, "now", CLOCK)
    since, until = optimize.period(None, None, None)
    end = datetime.fromisoformat(until)
    assert abs(end - datetime.now(UTC)) < timedelta(minutes=1)
    assert end - datetime.fromisoformat(since) == timedelta(days=7)


# --- optimize ---


def test_scope_org_in_the_default_workgroup(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    workgroups = wg_names(tool, org)
    default = next(wg for wg, item in workgroups.items() if item["default"])
    code, created = tool.run("optimize", "--org", org)
    assert code == 0, created
    since = optimize.iso(NOW - timedelta(days=7))
    analysis = {"scope": "org", "since": since, "until": NOW_TEXT}
    assert (created["org_id"], created["wg_id"], created["title"]) == (org, default, "Optimize processes")
    assert created["analysis"] == analysis
    assert created["session_id"]
    name = workgroups[default]["name"]
    assert created["notice"] == optimize.NOTICE_WG.format(name=name)
    assert name in created["notice"]
    body = posted(hub)
    assert (body["wg_id"], body["task_id"], body["analysis"]) == (default, created["task_id"], analysis)
    prompt = f"Optimize processes: analyse the organization from {since} to {NOW_TEXT} and propose improvements."
    # the title and the prompt are sealed: the hub sees ciphertext only
    assert b"analyse the organization" not in b"".join(hub.bodies)
    shown = show(tool, created["task_id"])
    assert shown["title"] == "Optimize processes"
    assert [event["body"]["text"] for event in shown["events"]] == [prompt]
    assert hub.tasks[created["task_id"]]["task"]["analysis"] == analysis


def test_scope_org_in_another_workgroup_in_german(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    backend = wg_create(tool, org, "Backend")
    code, created = tool.run("optimize", "--org", org, "--in", backend, "--days", "30", "--language", "de")
    assert code == 0, created
    since = optimize.iso(NOW - timedelta(days=30))
    assert (created["wg_id"], created["title"]) == (backend, "Prozesse optimieren")
    assert created["analysis"] == {"scope": "org", "since": since, "until": NOW_TEXT}
    assert created["notice"] == optimize.NOTICE_WG.format(name="Backend")
    assert posted(hub)["analysis"]["scope"] == "org"
    shown = show(tool, created["task_id"])
    assert shown["title"] == "Prozesse optimieren"
    assert shown["events"][0]["body"]["text"] == (
        f"Prozesse optimieren: Analysiere die Organisation von {since} bis {NOW_TEXT} und schlage Verbesserungen vor."
    )


def test_scope_wg_in_the_workgroup_itself(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    research = wg_create(tool, org, "Research")
    since, until = "2026-11-01T00:00:00Z", "2026-11-02T00:00:00.500+00:00"
    code, created = tool.run("optimize", "--wg", research, "--since", since, "--until", until)
    assert code == 0, created
    analysis = {"scope": "wg", "since": "2026-11-01T00:00:00.000Z", "until": "2026-11-02T00:00:00.500Z"}
    assert (created["org_id"], created["wg_id"], created["analysis"]) == (org, research, analysis)
    assert "notice" not in created
    assert posted(hub)["analysis"] == analysis
    assert show(tool, created["task_id"])["events"][0]["body"]["text"] == (
        "Optimize processes: analyse workgroup Research from 2026-11-01T00:00:00.000Z to 2026-11-02T00:00:00.500Z"
        " and propose improvements."
    )


def test_the_scope_wg_names_the_workgroup_by_its_verified_name(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, _device = assign_device(tool, hub, account)
    code, created = tool.run("optimize", "--wg", wg, "--days", "1")
    assert code == 0, created
    text = show(tool, created["task_id"])["events"][0]["body"]["text"]
    assert text.startswith("Optimize processes: analyse workgroup Default from 2026-11-25T10:00:00.123Z")


def test_invalid_arguments(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    other = team(tool, "Other")
    foreign = wg_create(tool, other, "Elsewhere")
    research = wg_create(tool, org, "Research")
    refusals: list[tuple[tuple[str, ...], str]] = [
        ((), "org_or_wg"),
        (("--org", org, "--wg", research), "org_or_wg"),
        (("--wg", research, "--in", research), "org_or_wg"),
        (("--org", org, "--in", foreign), "wg_not_in_org"),
        (("--org", org, "--days", "93"), "period_invalid"),
        (("--org", org, "--days", "2", "--since", "2026-11-20T00:00:00Z"), "period_invalid"),
        (("--wg", research, "--since", "now"), "time_invalid"),
    ]
    for argv, reason in refusals:
        code, document = tool.run("optimize", *argv)
        assert (code, document["error"], document.get("reason")) == (1, "invalid_argument", reason), argv
    for argv in (("--org", "x"), ("--wg", "x"), ("--org", org, "--in", "x"), ("--org", org, "--language", "fr")):
        assert tool.run("optimize", *argv)[1]["error"] == "invalid_argument", argv
    period = tool.run("optimize", "--org", org, "--days", "0")[1]
    assert "1-92" in period["next"]
    with pytest.raises(PlatformError):
        optimize.optimize(None, org=org, wg=None, language="fr")  # type: ignore[arg-type]
    assert not any(r.path == "/v1/tasks" for r in hub.requests)


def test_the_hub_refusals_pass_through(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    outdated = [uuid7()]
    hub.hooks[("POST", "/v1/tasks")] = lambda body, request: (
        409,
        {"error": "peer_outdated", "detail": {"devices": outdated}},
    )
    code, document = tool.run("optimize", "--org", org)
    assert (code, document["error"], document["hub_error"], document["devices"]) == (
        1,
        "hub_refused",
        "peer_outdated",
        outdated,
    )
    hub.hooks[("POST", "/v1/tasks")] = lambda body, request: (400, {"error": "request_invalid"})
    assert tool.run("optimize", "--org", org)[1]["hub_error"] == "request_invalid"


def test_an_agent_in_a_session_may_start_and_adopt() -> None:
    assert not relay.refused_in_session(["optimize", "--org", uuid7()])
    assert not relay.refused_in_session(["optimize", "adopt", "--task", uuid7()])


# --- optimize adopt ---


def analysis_task(tool: Tool, hub: HubFake, account: User) -> tuple[str, str, str, Any]:
    """`(org, wg, task, write)`: an analysis task of the default workgroup with a session that writes events."""
    org, wg, device = assign_device(tool, hub, account)
    task = tool.run("optimize", "--wg", wg)[1]["task_id"]
    session = add_session(hub, task, device=device)

    def write(kind: str, body: object) -> None:
        device_event(hub, account, device, kind, body, wg=wg, task=task, session=session)

    return org, wg, task, write


def output(text: str, turn: int) -> dict[str, Any]:
    return {"message_id": f"m{turn}", "role": "assistant", "text": text, "turn": turn}


def test_adopt_on_the_workgroup_board(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, task, write = analysis_task(tool, hub, account)
    write("session.output", output("A first draft", 1))
    write("session.tool", {"tool_use_id": "t1", "name": "get_usage", "input": "{}"})
    write("session.output", output("## Efficiency\nThe report", 2))
    code, adopted = tool.run("optimize", "adopt", "--task", task)
    assert code == 0, adopted
    assert (adopted["level"], adopted["org_id"], adopted["wg_id"], adopted["task_id"]) == ("workgroup", org, wg, None)
    assert adopted["analysis_task_id"] == task
    assert all(adopted[key] for key in ("entry_id", "event_id", "output_event_id"))
    assert "notice" not in adopted
    assert extra(hub).entries[adopted["entry_id"]]["notify"] is False
    listed = tool.run("whiteboard", "list", "--org", org, "--wg", wg)[1]["items"]
    assert [(item["entry_id"], item["text"], item["author"]) for item in listed] == [
        (adopted["entry_id"], "## Efficiency\nThe report", account.id)
    ]
    # the report's text is sealed on the board as well
    assert b"The report" not in b"".join(hub.bodies)


def test_adopt_on_the_organization_board(tool: Tool, hub: HubFake, account: User) -> None:
    org, _wg, task, write = analysis_task(tool, hub, account)
    write("session.output", output("The report", 1))
    code, adopted = tool.run("optimize", "adopt", "--task", task, "--level", "org", "--notify")
    assert code == 0, adopted
    assert (adopted["level"], adopted["org_id"], adopted["wg_id"]) == ("org", org, None)
    assert adopted["notice"] == optimize.NOTICE_ORG
    assert extra(hub).entries[adopted["entry_id"]]["notify"] is True
    listed = tool.run("whiteboard", "list", "--org", org)[1]["items"]
    assert [item["text"] for item in listed] == ["The report"]


def test_adopt_without_output_and_on_an_ordinary_task(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, task, write = analysis_task(tool, hub, account)
    code, document = tool.run("optimize", "adopt", "--task", task)
    assert (code, document["error"], document["reason"]) == (1, "invalid_argument", "no_output")
    assert "task wait" in document["next"]
    write("session.output", output("   ", 1))
    assert tool.run("optimize", "adopt", "--task", task)[1]["reason"] == "no_output"
    ordinary = new_task(tool, wg, "Fix the login bug")
    code, document = tool.run("optimize", "adopt", "--task", ordinary)
    assert (code, document["error"], document["reason"]) == (1, "invalid_argument", "not_analysis")
    assert "agent-platform optimize" in document["next"]
    assert tool.run("optimize", "adopt", "--task", "x")[1]["error"] == "invalid_argument"
    assert tool.run("optimize", "adopt", "--task", task, "--level", "task")[1]["error"] == "invalid_argument"
    with pytest.raises(PlatformError):
        optimize.adopt(None, task, "task")  # type: ignore[arg-type]
    assert extra(hub).entries == {}


def test_adopt_needs_the_write_right_of_the_board(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    bob_tool, _bob, _org, wg = reader(tool, hub, tmp_path)
    task = tool.run("optimize", "--wg", wg)[1]["task_id"]
    # refused before the chat is read
    assert bob_tool.run("optimize", "adopt", "--task", task)[1]["error"] == "no_write_access"
    assert bob_tool.run("optimize", "adopt", "--task", task, "--level", "org")[1]["error"] == "not_admin"
    assert extra(hub).entries == {}
