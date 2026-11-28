"""U-HUB-usage (subscriptions, KOR-69, ADR-090 section 4): `quota` items with `subscription_id` keep the newest reading
of a further subscription (only from the polling device, never for the primary or a gone record), `GET
/v1/me/subscriptions` shows each record's reading (the primary one's from the owner's windows) and the device state
announces `subscriptions.api`."""

from __future__ import annotations

import secrets
from uuid import UUID

from agent_hub.subscriptions import store
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.usage.support import Meter, window


def _further(meter: Meter, owner: Actor) -> str:
    record = new_id()
    meter.execute(
        "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
        " created_at, is_primary) VALUES (%s, %s, 'claude_subscription', 1, 1, 'h', %s, %s, %s, %s, false)",
        record,
        owner.id,
        secrets.token_bytes(32),
        secrets.token_bytes(64),
        meter.hub.clock(),
        meter.hub.clock(),
    )
    return record


def _reading(meter: Meter, owner: Actor, subscription: str, *windows: object, fetched: str | None = None) -> object:
    body = meter.quota_body(owner.id, *windows, fetched=fetched)  # type: ignore[arg-type]
    return meter.status("quota", {**body, "subscription_id": subscription}, meter.low)


def _listed(owner: Actor) -> dict[str, object]:
    response = owner.browser.get("/v1/me/subscriptions")
    assert response.status_code == 200, response.text
    return {item["id"]: item["reading"] for item in response.json()["subscriptions"]}


def test_the_device_state_announces_the_subscriptions_api(meter: Meter) -> None:
    assert meter.state(meter.low)["subscriptions"] == {"api": 2, "shared": []}


def test_an_owner_with_only_further_subscriptions_is_polled(meter: Meter) -> None:
    writer = meter.scene.writer
    _further(meter, writer)
    assert meter.poll_owners(meter.low) == [writer.id]


def test_a_reading_of_a_further_subscription_is_kept_per_record(meter: Meter) -> None:
    writer = meter.scene.writer
    meter.subscribe(writer)
    first, second = _further(meter, writer), _further(meter, writer)
    early, late = meter.time(-10), meter.time(-5)
    assert _reading(meter, writer, first, window("seven_day", 12.5), fetched=early) == ("ok", None)
    unreadable = {"owner_id": writer.id, "subscription_id": second, "unavailable": True, "fetched_at": late}
    assert meter.status("quota", unreadable, meter.low) == ("ok", None)
    readings = _listed(writer)
    assert readings[first] == {
        "fetched_at": early,
        "device_id": meter.low.id,
        "unreadable": False,
        "windows": [window("seven_day", 12.5)],
    }
    assert readings[second] == {
        "fetched_at": late,
        "device_id": meter.low.id,
        "unreadable": True,
        "windows": [],
    }
    # the owner's own windows and limit are untouched
    assert meter.rows("SELECT count(*) FROM hub.quota_snapshot WHERE owner_id = %s", writer.id) == [(0,)]
    primary = [key for key, value in readings.items() if key not in (first, second)]
    assert [readings[key] for key in primary] == [None]
    # an older report than the stored one changes nothing; a newer one replaces it
    assert _reading(meter, writer, first, window("seven_day", 99), fetched=meter.time(-20)) == ("ok", None)
    assert _listed(writer)[first]["windows"] == [window("seven_day", 12.5)]  # type: ignore[index]
    assert _reading(meter, writer, first, window("five_hour", 3), fetched=meter.time(-1)) == ("ok", None)
    assert _listed(writer)[first]["windows"] == [window("five_hour", 3)]  # type: ignore[index]
    # the reading goes with its record
    meter.execute("DELETE FROM hub.credential WHERE id = %s", first)
    assert meter.rows("SELECT count(*) FROM hub.subscription_quota WHERE credential_id = %s", first) == [(0,)]


def test_readings_of_the_primary_or_a_gone_record_are_dropped_and_other_devices_refused(meter: Meter) -> None:
    writer, reader = meter.scene.writer, meter.scene.reader
    meter.subscribe(writer)
    meter.subscribe(reader)
    primary = str(meter.rows("SELECT id FROM hub.credential WHERE owner_id = %s", writer.id)[0][0])
    assert _reading(meter, writer, primary, window("seven_day", 1)) == ("ok", None)
    assert _reading(meter, writer, new_id(), window("seven_day", 1)) == ("ok", None)
    theirs = _further(meter, reader)
    assert _reading(meter, writer, theirs, window("seven_day", 1)) == ("ok", None)  # not the writer's record
    assert meter.rows("SELECT count(*) FROM hub.subscription_quota") == [(0,)]
    further = _further(meter, writer)
    body = {**meter.quota_body(writer.id, window("seven_day", 1)), "subscription_id": further}
    assert meter.status("quota", body, meter.high) == ("rejected", "forbidden")


def test_the_primary_reading_comes_from_the_owners_windows(meter: Meter) -> None:
    writer = meter.scene.writer
    meter.subscribe(writer)
    primary = str(meter.rows("SELECT id FROM hub.credential WHERE owner_id = %s", writer.id)[0][0])
    assert _listed(writer) == {primary: None}
    windows = (window("seven_day", 40), window("five_hour", 90))
    first, second = meter.time(-3), meter.time(-2)
    assert meter.quota(writer.id, *windows, fetched=first) == ("ok", None)
    assert _listed(writer)[primary] == {
        "fetched_at": first,
        "device_id": meter.low.id,
        "unreadable": False,
        "windows": [window("five_hour", 90), window("seven_day", 40)],
    }
    gone = {"owner_id": writer.id, "unavailable": True, "fetched_at": second}
    assert meter.status("quota", gone, meter.low) == ("ok", None)
    assert _listed(writer)[primary] == {
        "fetched_at": second,
        "device_id": meter.low.id,
        "unreadable": True,
        "windows": [],
    }


def test_the_primary_reading_names_the_newest_device_only(meter: Meter) -> None:
    writer = meter.scene.writer
    meter.subscribe(writer)
    rows = [
        ("five_hour", 10.0, meter.low.id, -9),
        ("seven_day", 20.0, meter.high.id, -4),
    ]
    for name, used, device, seconds in rows:
        meter.execute(
            'INSERT INTO hub.quota_snapshot (owner_id, "window", used_percent, resets_at, fetched_at, device_id)'
            " VALUES (%s, %s, %s, NULL, %s, %s)",
            writer.id,
            name,
            used,
            meter.time(seconds),
            device,
        )
    with meter.hub.pool.connection() as connection:
        found = store.rows_for(connection, UUID(writer.id))
    assert found[0]["reading"]["device_id"] == meter.high.id
    assert [item["window"] for item in found[0]["reading"]["windows"]] == ["seven_day"]
