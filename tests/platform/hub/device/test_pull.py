"""U-HUB-device-api (pull): `GET /v1/device/pull` - rights per scope, kinds, cursors, pages (03-api.md section 6.1)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.device import pull, routes
from agent_hub.errors import HubError
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.events.support import Scene
from tests.platform.hub.events.test_page_bytes import sized
from tests.platform.hub.orgs.support import World, new_id

NOT_CONTRACT = {"contract": False}


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]


def kinds(answer: dict[str, Any]) -> list[tuple[str, int, str]]:
    return [(item["scope_id"], item["scope_seq"], item["event"]["header"]["kind"]) for item in answer["events"]]


def fill(scene: Scene) -> str:
    """Org-scope profiles exist from the creation; add wg events, a terminal chunk and a foreign workgroup's event."""
    task, session = scene.start_session()
    response = scene.post(scene.writer, scene.prompt(scene.writer, task), scene.wg_event(scene.writer))
    assert response.status_code == 200, response.text
    scene.append(scene.device, scene.session_event(scene.device, session, "session.terminal"))
    scene.append(scene.device, scene.session_event(scene.device, session, "session.output"))
    default = scene.draft.event(
        scene.writer, "settings.workgroup", scope="wg", scope_id=scene.draft.default_wg, wg=scene.draft.default_wg
    )
    assert scene.post(scene.writer, default).status_code == 200
    return session


def test_pull_everything_the_device_reads(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    fill(scene)
    answer = fleet.client(scene.device).get("/v1/device/pull").json()
    org_kinds = [(scene.org, 1, "org.profile"), (scene.org, 2, "wg.profile"), (scene.org, 3, "member.profile")]
    wg_kinds = [(scene.wg, 1, "prompt"), (scene.wg, 2, "settings.workgroup"), (scene.wg, 4, "session.output")]
    expected = sorted([*org_kinds, *wg_kinds], key=lambda item: (item[0], item[1]))
    assert kinds(answer) == expected
    assert answer["more"] is False
    assert all("received_at" in item for item in answer["events"])


def test_cursors_kinds_and_pages(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    fill(scene)
    client = fleet.client(scene.device)
    after = client.get("/v1/device/pull", params={"cursor": f"{scene.wg}:1"}).json()
    assert kinds(after) == [(scene.wg, 2, "settings.workgroup"), (scene.wg, 4, "session.output")]
    prompts = client.get("/v1/device/pull", params={"cursor": [f"{scene.wg}:0", f"{scene.org}:0"], "kinds": "prompt"})
    assert kinds(prompts.json()) == [(scene.wg, 1, "prompt")]
    terminal = client.get("/v1/device/pull", params={"cursor": f"{scene.wg}:0", "kinds": "session.terminal"})
    assert terminal.json() == {"events": [], "more": False}
    ordered = client.get("/v1/device/pull", params={"cursor": [f"{scene.wg}:0", f"{scene.org}:0"], "limit": "4"})
    assert kinds(ordered.json()) == [
        (scene.wg, 1, "prompt"),
        (scene.wg, 2, "settings.workgroup"),
        (scene.wg, 4, "session.output"),
        (scene.org, 1, "org.profile"),
    ]
    assert ordered.json()["more"] is True
    exact = client.get("/v1/device/pull", params={"cursor": [f"{scene.wg}:0", f"{scene.org}:0"], "limit": "3"})
    assert len(exact.json()["events"]) == 3
    assert exact.json()["more"] is True
    done = client.get("/v1/device/pull", params={"cursor": [f"{scene.wg}:4", f"{scene.org}:3"], "limit": "1"})
    assert done.json() == {"events": [], "more": False}
    one = client.get("/v1/device/pull", params={"cursor": [f"{scene.org}:2", f"{scene.wg}:4"], "limit": "1"})
    assert kinds(one.json()) == [(scene.org, 3, "member.profile")]
    assert one.json()["more"] is False


def test_a_pull_is_bounded_in_bytes_over_its_scopes(
    world: World, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOT-29: at most `PAGE_BYTES` of stored events over every scope, always at least one event."""
    scene = org_device_scene(world)
    for _ in range(2):
        assert scene.post(scene.writer, sized(scene)).status_code == 200
    client = fleet.client(scene.device)
    cursors = [f"{scene.wg}:0", f"{scene.org}:0"]

    def pulled(params: dict[str, Any]) -> tuple[list[tuple[str, int]], bool]:
        answer = client.get("/v1/device/pull", params=params).json()
        return [(item["scope_id"], item["scope_seq"]) for item in answer["events"]], answer["more"]

    whole, more = pulled({"cursor": cursors})
    assert (whole[:3], more) == ([(scene.wg, 1), (scene.wg, 2), (scene.org, 1)], False)
    monkeypatch.setattr(pull, "PAGE_BYTES", 1)
    # the first scope leaves an event behind: the page ends there, the org scope is only probed
    assert pulled({"cursor": cursors}) == ([(scene.wg, 1)], True)
    # a scope read to its end leaves the rest of the bound to the next one, which serves at least one event
    assert pulled({"cursor": [f"{scene.wg}:1", f"{scene.org}:0"]}) == ([(scene.wg, 2), (scene.org, 1)], True)


def test_pull_a_kind_with_an_underscore(world: World, fleet: Fleet) -> None:
    """KOR-52: the `kinds` filter takes the kind of a main agent's name (`task.agent_name`)."""
    scene = org_device_scene(world)
    fill(scene)
    task, _session = scene.start_session()
    named = scene.wg_event(scene.writer, "task.agent_name", task=task)
    assert scene.post(scene.writer, named).status_code == 200
    client = fleet.client(scene.device)
    answer = client.get("/v1/device/pull", params={"cursor": f"{scene.wg}:0", "kinds": "task.agent_name"}).json()
    assert kinds(answer) == [(scene.wg, 5, "task.agent_name")]
    assert answer["events"][0]["event"] == named


def test_pull_rights(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    fill(scene)
    client = fleet.client(scene.device)
    other = world.public_org()
    for scope in (scene.draft.default_wg, other.id, other.default_wg, new_id()):
        assert error(client.get("/v1/device/pull", params={"cursor": f"{scope}:0"})) == (404, "not_found"), scope
    lonely = fleet.org_device(world.public_org(), {})
    assert fleet.client(lonely).get("/v1/device/pull").json() == {"events": [], "more": False}


def test_pull_query_values(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    for params, pointer in (
        ({"cursor": scene.wg}, "/cursor"),
        ({"cursor": f"{scene.wg}:-1"}, "/cursor"),
        ({"cursor": [f"{scene.wg}:0", f"{scene.wg}:1"]}, "/cursor"),
        ({"kinds": "Prompt"}, "/kinds"),
        ({"kinds": "no.such"}, "/kinds"),
        ({"limit": "1001"}, "/limit"),
    ):
        response = client.get("/v1/device/pull", params=params, extensions=NOT_CONTRACT)
        assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}, params
    many = [f"{new_id()}:0" for _ in range(10001)]
    response = client.get("/v1/device/pull", params={"cursor": many[:2]}, extensions=NOT_CONTRACT)
    assert error(response) == (404, "not_found")
    with pytest.raises(HubError, match="request_invalid"):
        routes._cursors(many)  # the contract's limit of 10000 cursors (a URL that long exceeds the test client)
