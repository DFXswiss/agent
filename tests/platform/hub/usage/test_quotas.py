"""U-HUB-usage (quotas): `quota_poll_owners` of `GET /v1/device/state` (one online eligible device per subscription
owner, the smallest ID), the move of the duty when that device goes offline, `quota` items only from the polling device
(else `rejected` and audited) and `GET /v1/me/quotas` (own rows only, read log) - 03-api.md sections 4.1, 6.1;
05-runner.md section 10.6; KONZEPT section 6."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from agent_hub.credentials.eligibility import eligible_device_ids
from agent_hub.stream.presence import online_devices
from agent_hub.usage import polling
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import audit_rows
from tests.platform.hub.usage.support import Meter, window


def _expected_owners(meter: Meter, device: str, owners: list[str]) -> list[str]:
    """The rule written out with the eligibility and presence functions of the other areas."""
    now, found = meter.hub.clock(), []
    with meter.hub.pool.connection() as connection:
        for owner in owners:
            online = online_devices(connection, eligible_device_ids(connection, owner), now, 30)
            if online and min(str(item) for item in online) == device:
                found.append(owner)
    return sorted(found)


def test_the_smallest_online_eligible_device_polls(meter: Meter) -> None:
    scene = meter.scene
    # no stored subscription: nobody polls
    assert meter.poll_owners(meter.low) == []
    for owner in (scene.writer, scene.reader, scene.stranger):
        meter.subscribe(owner)
    # writer (write access) and reader (read access) are served by both devices; the stranger by none of them
    assert meter.poll_owners(meter.low) == sorted([scene.writer.id, scene.reader.id])
    assert meter.poll_owners(meter.high) == []
    subscribed = [scene.writer.id, scene.reader.id, scene.stranger.id]
    for device in (meter.low, meter.high):
        assert meter.poll_owners(device) == _expected_owners(meter, device.id, subscribed)
    # the admin may use both devices as well, but has no subscription
    assert _expected_owners(meter, meter.low.id, [scene.admin.id]) == [scene.admin.id]
    # an API-key credential is no subscription
    meter.execute("UPDATE hub.credential SET kind = 'anthropic_api_key' WHERE owner_id = %s", scene.reader.id)
    assert meter.poll_owners(meter.low) == [scene.writer.id]


def test_the_duty_moves_when_the_polling_device_goes_offline(meter: Meter) -> None:
    writer = meter.scene.writer
    meter.subscribe(writer)
    assert (meter.poll_owners(meter.low), meter.poll_owners(meter.high)) == ([writer.id], [])
    assert meter.quota(writer.id, window("five_hour", 10), device=meter.low) == ("ok", None)
    # the heartbeat of the low device is older than the online window (30 s): the high device takes over
    meter.desk.online(meter.low, seen=meter.hub.clock() - timedelta(seconds=30))
    assert (meter.poll_owners(meter.low), meter.poll_owners(meter.high)) == ([], [writer.id])
    assert meter.quota(writer.id, window("five_hour", 20), device=meter.low) == ("rejected", "forbidden")
    assert meter.quota(writer.id, window("five_hour", 30), device=meter.high) == ("ok", None)
    # the low device disconnects completely; then it comes back and polls again
    meter.desk.offline(meter.low)
    assert meter.poll_owners(meter.high) == [writer.id]
    meter.desk.online(meter.low)
    assert (meter.poll_owners(meter.low), meter.poll_owners(meter.high)) == ([writer.id], [])
    assert meter.quota(writer.id, window("five_hour", 40), device=meter.high) == ("rejected", "forbidden")
    # both offline: nobody polls
    meter.desk.offline(meter.low)
    meter.desk.offline(meter.high)
    assert (meter.poll_owners(meter.low), meter.poll_owners(meter.high)) == ([], [])
    assert meter.rows(
        'SELECT used_percent, device_id::text FROM hub.quota_snapshot WHERE "window" = %s', "five_hour"
    ) == [(30.0, meter.high.id)]


def test_unassigned_or_disabled_devices_do_not_poll(meter: Meter) -> None:
    writer = meter.scene.writer
    meter.subscribe(writer)
    meter.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", meter.hub.clock(), meter.low.id)
    with meter.hub.pool.connection() as connection:
        now = meter.hub.clock()
        assert polling.poll_owners(connection, meter.high.id, now, 30) == [UUID(writer.id)]
        assert polling.polls_for(connection, meter.high.id, writer.id, now, 30)
        assert not polling.polls_for(connection, meter.low.id, writer.id, now, 30)
        assert not polling.polls_for(connection, meter.high.id, new_id(), now, 30)
    meter.execute("UPDATE hub.principal SET disabled_at = NULL WHERE id = %s", meter.low.id)
    meter.execute("UPDATE hub.wg_device SET unassigned_seq = 1 WHERE device_id = %s", meter.low.id)
    with meter.hub.pool.connection() as connection:
        assert polling.poll_owners(connection, meter.high.id, meter.hub.clock(), 30) == [UUID(writer.id)]


def test_quota_snapshots_keep_the_newest_fetch(meter: Meter) -> None:
    writer = meter.scene.writer.id
    meter.subscribe(meter.scene.writer)
    resets = meter.time(3600)
    first = (window("five_hour", 12.5, resets), window("weekly:opus", 85))
    assert meter.quota(writer, *first, fetched=meter.time(-2)) == ("ok", None)
    assert meter.rows(
        'SELECT "window", used_percent, resets_at IS NULL, severity, device_id::text FROM hub.quota_snapshot'
        ' WHERE owner_id = %s ORDER BY "window"',
        writer,
    ) == [("five_hour", 12.5, False, None, meter.low.id), ("weekly:opus", 85.0, True, "warning", meter.low.id)]
    # a newer fetch replaces a window, an older one (a late retry, the previous polling device) does not
    assert meter.quota(writer, window("five_hour", 40, resets), fetched=meter.time()) == ("ok", None)
    older = (window("five_hour", 1, resets), window("seven_day", 3))
    assert meter.quota(writer, *older, fetched=meter.time(-60)) == ("ok", None)
    assert meter.rows(
        'SELECT "window", used_percent FROM hub.quota_snapshot WHERE owner_id = %s ORDER BY "window"', writer
    ) == [("five_hour", 40.0), ("seven_day", 3.0), ("weekly:opus", 85.0)]


def test_a_quota_of_the_wrong_device_or_owner_is_rejected_and_audited(meter: Meter) -> None:
    scene = meter.scene
    for owner in (scene.writer, scene.stranger):
        meter.subscribe(owner)
    assert meter.quota(scene.writer.id, window("five_hour", 1), device=meter.high) == ("rejected", "forbidden")
    assert meter.quota(scene.stranger.id, window("five_hour", 1)) == ("rejected", "forbidden")
    assert meter.quota(meter.low.id, window("five_hour", 1)) == ("rejected", "forbidden")
    assert meter.quota(scene.reader.id, window("five_hour", 1)) == ("rejected", "forbidden")  # no subscription
    assert meter.rows("SELECT count(*) FROM hub.quota_snapshot") == [(0,)]
    rejected = audit_rows(meter.desk, "outbox.rejected")
    assert [(row[0], row[1], row[3]["item_kind"], row[3]["code"]) for row in rejected] == [
        ("device", UUID(meter.high.id), "quota", "forbidden"),
        ("device", UUID(meter.low.id), "quota", "forbidden"),
        ("device", UUID(meter.low.id), "quota", "forbidden"),
        ("device", UUID(meter.low.id), "quota", "forbidden"),
    ]
    # a window listed twice breaks the contract
    twice = meter.quota_body(scene.writer.id, window("five_hour", 1), window("five_hour", 2))
    response = meter.push("quota", twice, meter.low, extensions={"contract": False})
    assert response.json()["results"][0]["status"] == "rejected"
    assert response.json()["results"][0]["error"] == "request_invalid"


def test_me_quotas_shows_only_own_rows_newest_first(meter: Meter) -> None:
    scene = meter.scene
    for owner in (scene.writer, scene.reader):
        meter.subscribe(owner)
    assert meter.quota(scene.writer.id, window("five_hour", 5, meter.time(600)), fetched=meter.time(-5)) == ("ok", None)
    assert meter.quota(scene.writer.id, window("seven_day", 50), window("weekly:opus", 90)) == ("ok", None)
    assert meter.quota(scene.reader.id, window("five_hour", 77)) == ("ok", None)
    response = scene.writer.browser.get("/v1/me/quotas")
    assert response.status_code == 200, response.text
    body = response.json()
    contracts_registry().validate("api/me-quotas.response.json", body)
    assert [(row["window"], row["used_percent"], row["device_id"]) for row in body["quotas"]] == [
        ("seven_day", 50, meter.low.id),
        ("weekly:opus", 90, meter.low.id),
        ("five_hour", 5, meter.low.id),
    ]
    assert body["quotas"][2]["resets_at"] is not None
    assert body["quotas"][0]["resets_at"] is None
    assert body["quotas"][1]["severity"] == "warning"
    assert scene.admin.browser.get("/v1/me/quotas").json() == {"quotas": []}
    meter.hub.flush_read_log()
    reads = meter.rows("SELECT actor_id::text, result FROM hub.audit WHERE action = 'read.me.quotas' ORDER BY seq")
    assert reads == [(scene.writer.id, "ok"), (scene.admin.id, "ok")]


def test_me_quotas_needs_a_signed_in_user(meter: Meter) -> None:
    assert meter.hub.anon.get("/v1/me/quotas").status_code == 401
    response = meter.ops.fleet.client(meter.low).get("/v1/me/quotas")
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert (
        meter.scene.writer.browser.get(
            "/v1/me/quotas", params={"owner": meter.scene.reader.id}, extensions={"contract": False}
        ).status_code
        == 400
    )


def test_an_unavailable_quota_drops_the_older_windows_of_the_owner(meter: Meter) -> None:
    """KOR-05 (B-18): the usage endpoint refused the owner's token; the web then shows "not available", not an old
    snapshot. Windows fetched later than the report stay (and a later fetch brings them back)."""
    writer = meter.scene.writer.id
    meter.subscribe(meter.scene.writer)
    assert meter.quota(writer, window("five_hour", 12), window("seven_day", 3), fetched=meter.time(-120)) == (
        "ok",
        None,
    )
    assert meter.quota(writer, window("weekly:opus", 5), fetched=meter.time(-10)) == ("ok", None)
    unavailable = {"owner_id": writer, "unavailable": True, "fetched_at": meter.time(-60)}
    assert meter.status("quota", unavailable, meter.low) == ("ok", None)
    assert meter.rows('SELECT "window" FROM hub.quota_snapshot WHERE owner_id = %s', writer) == [("weekly:opus",)]
    # only the polling device may report it
    assert meter.status("quota", {**unavailable, "fetched_at": meter.time()}, meter.high) == ("rejected", "forbidden")
    assert meter.rows("SELECT count(*) FROM hub.quota_snapshot WHERE owner_id = %s", writer) == [(1,)]
    assert meter.status("quota", {**unavailable, "fetched_at": meter.time()}, meter.low) == ("ok", None)
    assert meter.rows("SELECT count(*) FROM hub.quota_snapshot WHERE owner_id = %s", writer) == [(0,)]
    assert meter.quota(writer, window("five_hour", 20), fetched=meter.time(1)) == ("ok", None)
    assert meter.rows('SELECT "window", used_percent FROM hub.quota_snapshot WHERE owner_id = %s', writer) == [
        ("five_hour", 20.0)
    ]
    # windows and the marker together break the contract (and the hub's own body check)
    both = {**meter.quota_body(writer, window("five_hour", 1)), "unavailable": True}
    response = meter.push("quota", both, meter.low, extensions={"contract": False})
    assert response.json()["results"][0]["error"] == "request_invalid"
    # every other body the contract refuses is refused by the hub as well
    refused = [
        meter.quota_body(writer),
        {"owner_id": writer, "unavailable": True, "windows": [], "fetched_at": meter.time(2)},
        {**meter.quota_body(writer, window("five_hour", 1)), "unavailable": None},
        {"owner_id": writer, "unavailable": None, "fetched_at": meter.time(2)},
        {"owner_id": writer, "fetched_at": meter.time(2)},
    ]
    for body in refused:
        response = meter.push("quota", body, meter.low, extensions={"contract": False})
        assert response.json()["results"][0]["error"] == "request_invalid", body
    assert meter.rows("SELECT count(*) FROM hub.quota_snapshot WHERE owner_id = %s", writer) == [(1,)]
