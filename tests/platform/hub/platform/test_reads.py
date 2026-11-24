"""`GET /v1/platform/log` (browser, `user`) and `GET /v1/platform/enrollments` (root): pagination, head, read log,
and the verified pending enrollments (03-api.md section 4.3, 04-crypto.md section 5.9)."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_hub.crypto.encoding import b64u_encode
from agent_hub.events import paging
from agent_hub.log.platform import PlatformState
from agent_hub.platform import enrollments
from agent_hub.platform import log as platform_log
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.platform.conftest import Platform, new_user


def test_the_log_reads_ascending_with_head_and_cursor(plat: Platform, hub: HubHarness) -> None:
    for _ in range(4):
        plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
    browser = hub.browser()
    whole = browser.get("/v1/platform/log").json()
    assert [item["seq"] for item in whole["entries"]] == [0, 1, 2, 3, 4]
    assert [item["entry"] for item in whole["entries"]] == plat.entries
    assert whole["head"]["seq"] == 4
    assert whole["next_after"] is None
    page = browser.get("/v1/platform/log", params={"after": "0", "limit": "2"}).json()
    assert [item["seq"] for item in page["entries"]] == [1, 2]
    assert page["next_after"] == 2
    rest = browser.get("/v1/platform/log", params={"after": "2", "limit": "2"}).json()
    assert [item["seq"] for item in rest["entries"]] == [3, 4]
    assert rest["next_after"] is None
    beyond = browser.get("/v1/platform/log", params={"after": "9"}).json()
    assert beyond["entries"] == []
    assert beyond["next_after"] is None
    assert beyond["head"] == whole["head"]


def test_a_log_read_racing_an_append_serves_entries_and_head_of_one_state(
    plat: Platform, hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The head is read before the entries: an append committed in between must not show up beyond the served head
    # (the browser would refuse the page as a broken chain, `log_chain`).
    plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
    read_entries = platform_log.stored_entries

    def append_first(*args: Any, **kwargs: Any) -> list[tuple[int, dict[str, object]]]:
        monkeypatch.setattr(platform_log, "stored_entries", read_entries)
        plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
        return read_entries(*args, **kwargs)

    monkeypatch.setattr(platform_log, "stored_entries", append_first)
    raced = hub.browser().get("/v1/platform/log", params={"after": "0"}).json()
    assert ([item["seq"] for item in raced["entries"]], raced["head"]["seq"], raced["next_after"]) == ([1], 1, None)
    later = hub.browser().get("/v1/platform/log", params={"after": "1"}).json()
    assert ([item["seq"] for item in later["entries"]], later["head"]["seq"]) == ([2], 2)


def test_reading_the_log_needs_a_browser_session_and_is_logged(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    assert hub.anon.get("/v1/platform/log").status_code == 401
    assert hub.root.get("/v1/platform/log").status_code == 403
    browser = hub.browser()
    assert browser.get("/v1/platform/log").status_code == 200
    hub.flush_read_log()
    rows = plat.query("SELECT action, actor_id, result FROM hub.audit WHERE action = 'read.platform.log'")
    assert [(row[0], row[2]) for row in rows] == [("read.platform.log", "ok")]
    assert browser.key is not None
    assert rows[0][1] == browser.key.principal.principal_id


@pytest.mark.parametrize("params", [{"after": "-1"}, {"limit": "0"}, {"limit": "1001"}, {"after": "01"}])
def test_bad_query_parameters_are_refused(plat: Platform, hub: HubHarness, params: dict[str, str]) -> None:
    plat.init()
    response = hub.browser().get("/v1/platform/log", params=params, extensions={"contract": False})
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_an_empty_log_is_not_found(hub: HubHarness) -> None:
    response = hub.browser().get("/v1/platform/log")
    assert (response.status_code, response.json()["error"]) == (404, "not_found")


def test_enrollments_list_verified_pending_requests_with_the_log(plat: Platform, hub: HubHarness) -> None:
    assert hub.root.get("/v1/platform/enrollments").json() == {"enrollments": [], "log": []}
    first = plat.enroll()
    hub.clock.advance(1)
    second = plat.enroll()
    certified = plat.enroll()
    plat.certify(certified)
    answer = hub.root.get("/v1/platform/enrollments").json()
    assert answer["log"] == plat.entries
    listed = answer["enrollments"]
    assert [item["device_id"] for item in listed] == [first.device_id, second.device_id]
    assert listed[0] == {
        "invite_id": first.invite.invite_id,
        "device_id": first.device_id,
        "pub": first.pub,
        "enroll_sig": first.request["enroll_sig"],
        "inv_sig": first.request["inv_sig"],
        "agent_version": "0.1.0",
        "os": "macos",
        "arch": "arm64",
        "capacity": first.request["capacity"],
        "created_at": "2026-11-21T12:00:00.123Z",
    }
    hub.flush_read_log()
    # the same read twice in one window: one row and its repeat (ADR-063 section 3)
    assert plat.query("SELECT actor_kind FROM hub.audit WHERE action = 'read.platform.enrollments'") == [("operator",)]
    assert plat.query("SELECT actor_kind, detail->>'action' FROM hub.audit WHERE action = 'audit.repeat'") == [
        ("operator", "read.platform.enrollments")
    ]


def test_the_log_of_the_enrollments_pages_and_is_never_cut(
    plat: Platform, hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-086 section 2 (KOR-35): `after`/`limit` read the embedded log page by page with `next_after`; without them
    the whole log up to `CAP_LARGE` entries, and a longer log is refused with `paging_required` - an old operator CLI
    must never verify and extend a cut log."""
    plat.init()
    plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
    plat.append("role_grant", subject=str(new_user(hub)), role="inviter")
    entries = plat.entries
    assert len(entries) == 3
    first = hub.root.get("/v1/platform/enrollments", params={"limit": "2"}).json()
    assert (first["log"], first["next_after"], first["enrollments"]) == (entries[:2], 1, [])
    rest = hub.root.get("/v1/platform/enrollments", params={"after": "1"}).json()
    assert (rest["log"], rest["next_after"]) == (entries[2:], None)
    assert hub.root.get("/v1/platform/enrollments", params={"after": "2", "limit": "5"}).json()["log"] == []
    whole = hub.root.get("/v1/platform/enrollments").json()
    assert (whole["log"], set(whole)) == (entries, {"enrollments", "log"})
    monkeypatch.setattr(paging, "CAP_LARGE", 2)
    refused = hub.root.get("/v1/platform/enrollments")
    assert (refused.status_code, refused.json()) == (
        400,
        {"error": "request_invalid", "detail": {"paging_required": True}},
    )
    paged = hub.root.get("/v1/platform/enrollments", params={"limit": "2"}).json()
    assert (paged["log"], paged["next_after"]) == (entries[:2], 1)  # paged reads are not affected by the cap
    response = hub.root.get("/v1/platform/enrollments", params={"limit": "1001"}, extensions={"contract": False})
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_enrollments_need_the_root(hub: HubHarness) -> None:
    assert hub.browser().get("/v1/platform/enrollments").status_code == 403
    assert hub.anon.get("/v1/platform/enrollments").status_code == 401


def forged(plat: Platform, label: str) -> dict[str, Any] | None:
    """A stored enrollment request that must not be listed."""
    enrolled = plat.enroll(store=False)
    request: dict[str, Any] = dict(enrolled.request)
    if label == "not_an_object":
        plat.store_enrollment(enrolled)
        with plat.hub.pool.connection() as connection:
            connection.execute("UPDATE hub.device_enrollment SET request = '{\"x\":[1]}'::jsonb")
        return None
    if label == "extra_field":
        request["extra"] = 1
    elif label == "org_kind":
        request["kind"] = "org"
    elif label == "other_invite":
        request["invite_id"] = b64u_encode(bytes(16))
    elif label == "other_device":
        request["device_id"] = str(uuid7())
    elif label == "bad_inv_sig":
        request["inv_sig"] = request["enroll_sig"]
    elif label == "user_invite":
        invite_id = plat.invite_user()
        plat.store_enrollment(enrolled, request={**request, "invite_id": invite_id})
        with plat.hub.pool.connection() as connection:
            connection.execute("UPDATE hub.device_enrollment SET invite_id = %s", (invite_id,))
        return None
    elif label == "unknown_invite":
        with plat.hub.pool.connection() as connection:
            connection.execute(
                "INSERT INTO hub.device_enrollment (invite_id, device_id, request, created_at) VALUES (%s, %s, %s, %s)",
                (b64u_encode(bytes([3] * 16)), enrolled.device_id, Jsonb(request), plat.hub.clock()),
            )
        return None
    elif label == "invite_certified":
        other = plat.enroll(enrolled.invite, store=False)
        plat.certify(other)
    elif label == "device_certified":
        plat.store_enrollment(enrolled)
        twin = plat.enroll(device_id=enrolled.device_id, store=False)
        plat.certify(twin)
        return None
    plat.store_enrollment(enrolled, request=request)
    return request


@pytest.mark.parametrize(
    "label",
    [
        "not_an_object",
        "extra_field",
        "org_kind",
        "other_invite",
        "other_device",
        "bad_inv_sig",
        "user_invite",
        "unknown_invite",
        "invite_certified",
        "device_certified",
    ],
)
def test_enrollments_that_do_not_verify_are_left_out(plat: Platform, hub: HubHarness, label: str) -> None:
    plat.init()
    forged(plat, label)
    assert hub.root.get("/v1/platform/enrollments").json()["enrollments"] == []


def test_a_non_object_request_is_left_out(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    with hub.pool.connection() as connection:
        assert enrollments.pending(connection, None) == []
        empty = PlatformState(root="", seq=0, head="")
        assert enrollments._verified([1], empty, "a", "b") is None
