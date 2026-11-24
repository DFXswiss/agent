"""U-HUB-models (quotas): the latest observed limit of an own subscription (`quota.limited`), the operator
subscriptions of a platform device (`operator_quota`) and `GET /v1/orgs/{org}/quotas` (ADR-081 section 6, KOR-50)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_hub.crypto.encoding import b64u_encode, format_time, parse_time
from tests.platform.hub.events.support import Json
from tests.platform.hub.models.support import Keys
from tests.platform.hub.orgs.support import PlatformLog, insert_platform_entries, new_id
from tests.platform.hub.tasks.support import audit_rows
from tests.platform.hub.usage.support import window


def _quotas(keys: Keys, actor: Any = None) -> Json:
    response = (actor or keys.bill.scene.writer).browser.get(f"/v1/orgs/{keys.org}/quotas")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _subscription(name: str, state: str, **fields: Any) -> Json:
    found: Json = {
        "name": name,
        "state": state,
        "readable": True,
        "windows": [],
        "limited_at": None,
        "until": None,
        "fetched_at": None,
    }
    found.update(fields)
    return found


def _grant_operator_subscriptions(keys: Keys) -> None:
    log = PlatformLog()
    log.add("root", pub=b64u_encode(log.root.public_key().public_bytes_raw()))
    log.add("org_operator_subscriptions", org=keys.org, granted=True)
    insert_platform_entries(keys.hub.pool, log.entries, keys.hub.clock())


def test_the_operator_snapshot_comes_from_platform_devices_only(keys: Keys) -> None:
    body = {"at": keys.bill.meter.time(-1), "subscriptions": [_subscription("max-01", "usable")]}
    assert keys.status("operator_quota", body, keys.device) == ("rejected", "forbidden")
    keys.bill.platform(keys.issuer)
    assert keys.status("operator_quota", body) == ("ok", None)
    older = {"at": keys.bill.meter.time(-60), "subscriptions": []}
    assert keys.status("operator_quota", older) == ("ok", None)  # an older snapshot changes nothing
    stored = keys.rows("SELECT snapshot FROM hub.operator_quota WHERE device_id = %s", keys.issuer.id)
    assert [entry["name"] for entry in stored[0][0]] == ["max-01"]
    twice = {
        "at": keys.bill.meter.time(),
        "subscriptions": [_subscription("a", "usable"), _subscription("a", "ignored")],
    }
    assert keys.status("operator_quota", twice) == ("rejected", "request_invalid")


def test_the_latest_limit_is_kept_and_released(keys: Keys) -> None:
    meter = keys.bill.meter
    writer = keys.bill.scene.writer
    meter.subscribe(writer)
    limit_at = parse_time(format_time(keys.hub.clock() - timedelta(minutes=5)))
    until = parse_time(format_time(keys.hub.clock() + timedelta(hours=2)))
    body = meter.quota_body(writer.id, window("five_hour", 100, format_time(until)))
    body["limited"] = {"at": format_time(limit_at), "until": format_time(until), "readable": True}
    assert meter.status("quota", body, meter.low) == ("ok", None)
    older = meter.quota_body(writer.id, window("five_hour", 100, format_time(until)))
    older["limited"] = {"at": format_time(limit_at - timedelta(minutes=1)), "until": None, "readable": False}
    assert meter.status("quota", older, meter.low) == ("ok", None)
    assert keys.rows("SELECT at, until, readable FROM hub.quota_limit") == [(limit_at, until, True)]
    # a later report without a limit releases it at its fetch time; the time of the limit stays
    fetched = parse_time(format_time(keys.hub.clock()))
    assert meter.quota(writer.id, window("five_hour", 3), fetched=format_time(fetched)) == ("ok", None)
    assert keys.rows("SELECT at, until FROM hub.quota_limit") == [(limit_at, fetched)]
    # an unreadable subscription reports its limit as well
    unreadable = {"owner_id": writer.id, "unavailable": True, "fetched_at": meter.time()}
    unreadable["limited"] = {"at": meter.time(), "until": None, "readable": False}
    assert meter.status("quota", unreadable, meter.low) == ("ok", None)
    assert keys.rows("SELECT until, readable FROM hub.quota_limit") == [(None, False)]


def test_the_quotas_of_an_org_without_anything(keys: Keys) -> None:
    assert _quotas(keys) == {
        "own": {"windows": [], "unreadable": None, "limited": None},
        "operator": {"granted": False, "subscriptions": []},
        "platform": {"available": False},
        "summary": {"usable": 0, "exhausted": 0, "next_release": None},
    }
    assert keys.bill.scene.stranger.browser.get(f"/v1/orgs/{keys.org}/quotas").status_code == 404
    assert keys.bill.scene.writer.browser.get(f"/v1/orgs/{new_id()}/quotas").status_code == 404
    assert audit_rows(keys.bill.meter.desk, "read.org.quotas")


def test_the_quotas_of_an_org_with_own_and_operator_subscriptions(keys: Keys) -> None:
    now = keys.hub.clock()
    soon, later = now + timedelta(hours=1), now + timedelta(hours=3)
    writer = keys.bill.scene.writer
    keys.execute(
        'INSERT INTO hub.quota_snapshot (owner_id, "window", used_percent, resets_at, severity, fetched_at, device_id)'
        " VALUES (%s, 'five_hour', 100, %s, 'critical', %s, %s), (%s, 'weekly:opus', 100, %s, NULL, %s, %s),"
        " (%s, 'seven_day', 100, %s, NULL, %s, %s)",
        writer.id,
        later,
        now,
        keys.device.id,
        writer.id,
        later + timedelta(hours=1),
        now,
        keys.device.id,
        writer.id,
        now - timedelta(hours=1),  # reset already
        now,
        keys.device.id,
    )
    keys.bill.platform(keys.issuer)
    keys.bill.meter.desk.online(keys.issuer, seen=now - timedelta(minutes=5))
    snapshot = [
        _subscription(
            "max-02",
            "usable",
            windows=[
                window("five_hour", 20, format_time(soon)),
                window("seven_day", 55, None),
                window("weekly:opus", 99, None),
            ],
            fetched_at=format_time(now),
        ),
        _subscription("max-01", "exhausted", readable=False, limited_at=format_time(now), until=format_time(soon)),
        _subscription("max-03", "exhausted", readable=False, limited_at=format_time(now)),
        _subscription("max-04", "blocked"),
    ]
    assert keys.status("operator_quota", {"at": keys.bill.meter.time(-1), "subscriptions": snapshot}) == ("ok", None)
    found = _quotas(keys)
    assert [entry["window"] for entry in found["own"]["windows"]] == ["five_hour", "seven_day", "weekly:opus"]
    assert found["operator"] == {"granted": False, "subscriptions": []}  # not granted by the platform log
    assert found["platform"] == {"available": False}  # the platform device is offline
    assert found["summary"] == {"usable": 0, "exhausted": 1, "next_release": format_time(later)}
    _grant_operator_subscriptions(keys)
    keys.online(keys.issuer)
    found = _quotas(keys)
    assert found["platform"] == {"available": True}
    assert found["operator"]["granted"] is True
    assert [
        (entry["label"], entry["state"], entry["used_percent"]) for entry in found["operator"]["subscriptions"]
    ] == [
        ("max-01", "exhausted", None),
        ("max-02", "usable", 55),
        ("max-03", "exhausted", None),
        ("max-04", "blocked", None),
    ]
    assert found["operator"]["subscriptions"][1]["resets_at"] is None  # the seven-day window is the fullest
    assert found["summary"] == {"usable": 1, "exhausted": 3, "next_release": format_time(soon)}
    # a platform device that left the org is not counted
    keys.execute(
        "UPDATE hub.org_member SET removed_seq = joined_seq + 100 WHERE org_id = %s AND principal_id = %s",
        keys.org,
        keys.issuer.id,
    )
    found = _quotas(keys)
    assert (found["operator"]["subscriptions"], found["platform"]) == ([], {"available": False})


def test_an_own_limit_counts_until_it_is_released(keys: Keys) -> None:
    now = keys.hub.clock()
    writer = keys.bill.scene.writer
    keys.execute(
        "INSERT INTO hub.quota_limit (owner_id, at, until, readable, device_id, fetched_at)"
        " VALUES (%s, %s, NULL, false, %s, %s)",
        writer.id,
        now,
        keys.device.id,
        now,
    )
    found = _quotas(keys)
    assert found["own"]["limited"] == {"at": format_time(now), "until": None, "readable": False}
    assert found["summary"] == {"usable": 0, "exhausted": 1, "next_release": None}
    keys.execute("UPDATE hub.quota_limit SET until = %s", now - timedelta(seconds=1))
    assert _quotas(keys)["summary"] == {"usable": 1, "exhausted": 0, "next_release": None}
    keys.execute("DELETE FROM hub.quota_limit")
    keys.execute(
        "INSERT INTO hub.quota_unreadable (owner_id, since, device_id) VALUES (%s, %s, %s)",
        writer.id,
        now,
        keys.device.id,
    )
    found = _quotas(keys)
    assert found["own"]["unreadable"] == {"since": format_time(now)}
    assert found["summary"]["usable"] == 1


def test_a_held_key_makes_dfx_access_available(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    keys.bill.meter.desk.online(keys.issuer, seen=keys.hub.clock() - timedelta(minutes=5))
    keys.execute(
        "UPDATE hub.org_member SET removed_seq = joined_seq + 100 WHERE org_id = %s AND principal_id = %s",
        keys.org,
        keys.issuer.id,
    )
    assert _quotas(keys)["platform"] == {"available": True}
