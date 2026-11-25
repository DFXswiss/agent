"""U-HUB-events (watched kinds, KOR-44): the one list of watched kinds (13-work-vm.md section 13.6, E-19) drives the
read filter, the device pull, the frame type of the announcement, the device hints and the bus payload."""

from __future__ import annotations

import uuid
from typing import Any

from agent_hub.device import pull
from agent_hub.events import notify, watched
from agent_hub.log.events import KIND_RULES
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.events.support import Scene


def test_the_list_names_terminal_output_and_browser_actions() -> None:
    assert [(item.kind, item.frame, item.param, item.replay) for item in watched.WATCHED] == [
        ("session.terminal", "terminal", "terminal", True),
        ("browser.action", "browser_action", "browser", False),
    ]
    assert {"session.terminal", "browser.action"} == watched.KINDS
    assert {"terminal", "browser_action"} == watched.FRAMES
    assert watched.PARAMS == ("terminal", "browser")
    assert watched.frame_of("browser.action") == "browser_action"
    assert watched.frame_of("session.terminal") == "terminal"
    assert watched.frame_of("browser.state") == "event"
    assert {rule.kind for rule in KIND_RULES} >= watched.KINDS


def test_device_pulls_never_return_a_watched_kind() -> None:
    assert not pull.WG_KINDS & watched.KINDS
    assert "browser.state" in pull.WG_KINDS
    assert pull.allowed_kinds("wg", ["browser.action", "browser.state"]) == ["browser.state"]


def _seqs(response: Any) -> list[int]:
    assert response.status_code == 200, response.text
    return [item["scope_seq"] for item in response.json()["events"]]


def test_a_watched_kind_is_read_only_when_listed(scene: Scene) -> None:
    _task, session = scene.start_session()
    action, terminal, state = scene.append(
        scene.device,
        scene.session_event(scene.device, session, "browser.action"),
        scene.session_event(scene.device, session, "session.terminal"),
        scene.session_event(scene.device, session, "browser.state"),
    )
    after = str(action.scope_seq - 1)
    assert _seqs(scene.read(scene.reader, after=after)) == [state.scope_seq]
    assert _seqs(scene.read(scene.reader, after=after, kinds="browser.action")) == [action.scope_seq]
    assert _seqs(scene.read(scene.reader, after=after, kinds="session.terminal,browser.action")) == [
        action.scope_seq,
        terminal.scope_seq,
    ]


def test_the_announcement_follows_the_list(
    scene: Scene, published: MemoryPublisher, emitted: list[tuple[str, dict[str, Any]]]
) -> None:
    _task, session = scene.start_session()
    action, state = scene.append(
        scene.device,
        scene.session_event(scene.device, session, "browser.action"),
        scene.session_event(scene.device, session, "browser.state"),
    )
    notify.announce([action, state])  # what the outbox and the events route do after their commit
    items = published.items()
    org = Audience("org", uuid.UUID(scene.org))
    assert [(item.audience, item.frame, item.data["scope_seq"]) for item in items if item.audience == org] == [
        (org, "browser_action", action.scope_seq),
        (org, "event", state.scope_seq),
    ]
    hinted = {item.data["scope_seq"] for item in items if item.audience.kind == "device"}
    assert hinted == {state.scope_seq}  # no device hint for a watched kind
    assert [ids["kind"] for _name, ids in emitted] == ["browser.action", "browser.state"]
    parsed = notify.parse(notify.payload(action))
    assert (parsed.kind, parsed.session_id) == ("browser.action", uuid.UUID(session))
