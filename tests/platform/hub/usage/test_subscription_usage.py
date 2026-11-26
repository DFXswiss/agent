"""U-HUB-usage (who used how much, KOR-75, ADR-100): `usage` items with `subscription_id` add their tokens to
`hub.subscription_usage` per subscription, five-minute slot, person and session - only for a subscription that may
serve the session -, and `GET /v1/me/subscriptions/usage` answers per subscription the span, the measured window and
per person the tokens, sessions, share of the tokens and the estimated share of the quota (the owner sees everybody,
another person only their own row)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.subscriptions import usage
from agent_hub.subscriptions.outbox import slot_of
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.outbox.support import usage_body
from tests.platform.hub.usage.support import Meter, window

COUNTS = {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 1000, "cache_write_tokens": 30}
FIGURES = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _record(meter: Meter, owner: Actor, *, primary: bool) -> str:
    record = new_id()
    meter.execute(
        "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
        " created_at, is_primary) VALUES (%s, %s, 'claude_subscription', 1, 1, 'h', %s, %s, %s, %s, %s)",
        record,
        owner.id,
        secrets.token_bytes(32),
        secrets.token_bytes(64),
        meter.hub.clock(),
        meter.hub.clock(),
        primary,
    )
    return record


def _use(meter: Meter, record: str, owner: Actor, *, pool: bool = False, orgs: list[str] | None = None) -> None:
    meter.execute(
        "INSERT INTO hub.subscription_use (credential_id, owner_id, pool, orgs, set_at) VALUES (%s, %s, %s, %s, %s)",
        record,
        owner.id,
        pool,
        Jsonb(orgs or []),
        meter.hub.clock(),
    )


def _push(meter: Meter, session: str, subscription: str | None, **fields: Any) -> tuple[str, str | None]:
    body: dict[str, Any] = {**usage_body(session, requests=1, **COUNTS), **fields}
    if subscription is not None:
        body["subscription_id"] = subscription
    return meter.status("usage", body)


def _reading(meter: Meter, owner: Actor, subscription: str, *windows: Any) -> tuple[str, str | None]:
    body = {**meter.quota_body(owner.id, *windows), "subscription_id": subscription}
    return meter.status("quota", body, meter.low)


def _rows(meter: Meter) -> list[tuple[Any, ...]]:
    return meter.rows(
        "SELECT credential_id::text, owner_id::text, user_id::text, session_id::text, slot, requests, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens FROM hub.subscription_usage ORDER BY user_id, slot"
    )


def _usage(actor: Actor, query: str) -> Any:
    response = actor.browser.get(f"/v1/me/subscriptions/usage?{query}")
    assert response.status_code == 200, response.text
    return response.json()


def _prompts(meter: Meter, task: str, *authors: Actor) -> None:
    scene = meter.scene
    scene.draft.add("grant", scene.admin, subject=scene.reader.id, wg=scene.wg, access="write")
    assert scene.draft.post(scene.admin).status_code == 200
    for author in authors:
        scene.append(author, scene.prompt(author, task))


def test_the_device_state_announces_the_usage_api(meter: Meter) -> None:
    assert meter.state(meter.low)["subscriptions"]["api"] == 2


def test_the_slot_is_the_start_of_its_five_minutes() -> None:
    at = datetime(2026, 11, 26, 13, 37, 42, 123456, tzinfo=UTC)
    assert slot_of(at) == datetime(2026, 11, 26, 13, 35, tzinfo=UTC)


def test_usage_on_the_own_subscription_is_kept_per_person_slot_and_session(meter: Meter) -> None:
    writer, reader, stranger = meter.scene.writer, meter.scene.reader, meter.scene.stranger
    primary = _record(meter, writer, primary=True)
    task, session = meter.session("subscription")
    _prompts(meter, task, writer, reader)
    at = meter.time(-600)  # another slot than now
    assert _push(meter, session, primary, triggered_by=writer.id, at=at) == ("ok", None)
    assert _push(meter, session, primary, triggered_by=writer.id, at=at) == ("ok", None)  # the same slot: added
    assert _push(meter, session, primary, triggered_by=reader.id) == ("ok", None)
    # nobody named, and somebody who wrote no prompt of the task: the session's owner
    assert _push(meter, session, primary, triggered_by=None) == ("ok", None)
    assert _push(meter, session, primary, triggered_by=stranger.id) == ("ok", None)
    # a time after the receive time counts as the receive time
    assert _push(meter, session, primary, triggered_by=reader.id, at=meter.time(3600)) == ("ok", None)
    # no subscription named (an older runner, another access): nothing
    assert _push(meter, session, None, triggered_by=writer.id) == ("ok", None)
    now_slot = slot_of(meter.hub.clock())
    at_slot = slot_of(datetime.fromisoformat(at.replace("Z", "+00:00")))
    found = {(row[2], row[4]): row[5:] for row in _rows(meter)}
    assert {row[0] for row in _rows(meter)} == {primary}
    assert {row[1] for row in _rows(meter)} == {writer.id}
    assert {row[3] for row in _rows(meter)} == {session}
    assert found[(writer.id, at_slot)] == (2, 200, 40, 2000, 60)
    assert found[(writer.id, now_slot)] == (2, 200, 40, 2000, 60)
    assert found[(reader.id, now_slot)] == (2, 200, 40, 2000, 60)
    assert len(found) == 3
    # the rows go with their record
    meter.execute("DELETE FROM hub.credential WHERE id = %s", primary)
    assert _rows(meter) == []


def test_a_subscription_that_may_not_serve_the_session_records_nothing(meter: Meter) -> None:
    writer, reader = meter.scene.writer, meter.scene.reader
    _task, session = meter.session("subscription")
    theirs = _record(meter, reader, primary=True)  # another person's own subscription
    measured = _record(meter, writer, primary=False)  # measure only
    elsewhere = _record(meter, writer, primary=False)
    _use(meter, elsewhere, writer, orgs=[new_id()])
    for record in (theirs, measured, elsewhere, new_id()):
        assert _push(meter, session, record) == ("ok", None)
    assert _rows(meter) == []
    shared = _record(meter, reader, primary=False)
    _use(meter, shared, reader, orgs=[meter.desk.org])
    pooled = _record(meter, reader, primary=False)
    _use(meter, pooled, reader, pool=True)
    assert _push(meter, session, shared) == ("ok", None)
    assert _push(meter, session, pooled) == ("ok", None)
    assert sorted((row[0], row[1], row[2]) for row in _rows(meter)) == sorted(
        [(shared, reader.id, writer.id), (pooled, reader.id, writer.id)]
    )


def test_the_owner_sees_every_person_with_the_estimated_share(meter: Meter) -> None:
    writer, reader = meter.scene.writer, meter.scene.reader
    primary = _record(meter, writer, primary=True)
    resets = meter.time(3 * 3600)
    assert meter.quota(writer.id, window("five_hour", 40, resets), window("seven_day", 10)) == ("ok", None)
    task, session = meter.session("subscription")
    _prompts(meter, task, writer, reader)
    for _ in range(3):
        assert _push(meter, session, primary, triggered_by=writer.id) == ("ok", None)
    assert _push(meter, session, primary, triggered_by=reader.id) == ("ok", None)
    # before the running window began: not counted in it
    assert _push(meter, session, primary, triggered_by=reader.id, at=meter.time(-3 * 3600)) == ("ok", None)
    answer = _usage(writer, "period=five_hour")
    assert (answer["period"], "since" in answer) == ("five_hour", False)
    [entry] = answer["subscriptions"]
    assert (entry["subscription_id"], entry["owner_id"], entry["primary"], entry["own"]) == (
        primary,
        writer.id,
        True,
        True,
    )
    reset = datetime.fromisoformat(resets.replace("Z", "+00:00"))
    assert entry["since"] == format_time(reset - timedelta(hours=5))
    assert entry["until"] <= meter.time()
    assert (entry["measured"]["used_percent"], entry["measured"]["resets_at"]) == (40, resets)
    tokens = sum(COUNTS.values())
    assert entry["total"] == {
        "requests": 4,
        **{name: value * 4 for name, value in COUNTS.items()},
        "tokens": tokens * 4,
        "sessions": 1,
    }
    assert [(p["user_id"], p["tokens"], p["sessions"]) for p in entry["people"]] == [
        (writer.id, tokens * 3, 1),
        (reader.id, tokens, 1),
    ]
    assert [p["token_share"] for p in entry["people"]] == [0.75, 0.25]
    assert [p["estimated_percent"] for p in entry["people"]] == [30.0, 10.0]
    # the week has no reset in the reading: the last 7 days count, no share of the quota
    [week] = _usage(writer, "period=week")["subscriptions"]
    assert week["measured"] is None
    until = datetime.fromisoformat(week["until"].replace("Z", "+00:00"))
    assert week["since"] == format_time(until - timedelta(days=7))
    assert [p["estimated_percent"] for p in week["people"]] == [None, None]
    assert week["total"]["tokens"] == tokens * 5


def test_another_person_sees_only_their_own_row(meter: Meter) -> None:
    writer, reader = meter.scene.writer, meter.scene.reader
    shared = _record(meter, reader, primary=False)
    _use(meter, shared, reader, orgs=[meter.desk.org])
    task, session = meter.session("subscription")
    _prompts(meter, task, writer, reader)
    assert _push(meter, session, shared, triggered_by=writer.id) == ("ok", None)
    assert _push(meter, session, shared, triggered_by=reader.id) == ("ok", None)
    [theirs] = _usage(writer, "period=week")["subscriptions"]
    assert (theirs["own"], theirs["owner_id"], theirs["total"]) == (False, reader.id, None)
    assert [(p["user_id"], p["token_share"]) for p in theirs["people"]] == [(writer.id, 0.5)]
    # the owner sees both people
    [owned] = _usage(reader, "period=week")["subscriptions"]
    assert (owned["own"], {p["user_id"] for p in owned["people"]}) == (True, {writer.id, reader.id})
    # somebody who used none of it sees nothing of it
    assert _usage(meter.scene.admin, "period=week")["subscriptions"] == []


def test_another_persons_window_counts_from_its_start(meter: Meter) -> None:
    """The running window of another person's subscription began after the caller's last use: not listed."""
    writer, reader = meter.scene.writer, meter.scene.reader
    meter.subscribe(reader)
    shared = _record(meter, reader, primary=False)
    _use(meter, shared, reader, orgs=[meter.desk.org])
    reading = window("five_hour", 12, meter.time(3600))  # began 4 hours ago
    assert _reading(meter, reader, shared, reading) == ("ok", None)
    _task, session = meter.session("subscription")
    assert _push(meter, session, shared, at=meter.time(-int(4.5 * 3600))) == ("ok", None)
    assert _usage(writer, "period=five_hour")["subscriptions"] == []
    assert len(_usage(writer, "period=week")["subscriptions"]) == 1


def test_a_chosen_range(meter: Meter) -> None:
    writer = meter.scene.writer
    primary = _record(meter, writer, primary=True)
    _task, session = meter.session("subscription")
    assert _push(meter, session, primary, at=meter.time(-2 * 86400)) == ("ok", None)
    since, until = meter.time(-3 * 86400), meter.time(-86400)
    answer = _usage(writer, f"period=range&since={since}&until={until}")
    assert (answer["since"], answer["until"]) == (since, until)
    [entry] = answer["subscriptions"]
    assert (entry["measured"], entry["total"]["requests"], entry["people"][0]["estimated_percent"]) == (None, 1, None)
    # nothing in a span without use; an own subscription is listed anyway
    [empty] = _usage(writer, f"period=range&since={until}")["subscriptions"]
    assert (empty["people"], empty["total"]["tokens"]) == ([], 0)


@pytest.mark.parametrize(
    ("period", "since", "until", "pointer"),
    [
        (None, None, None, "/period"),
        ("month", None, None, "/period"),
        ("five_hour", "2026-11-01T00:00:00.000Z", None, "/since"),
        ("week", None, "2026-11-01T00:00:00.000Z", "/until"),
        ("range", None, None, "/since"),
        ("range", "yesterday", None, "/since"),
        ("range", "2026-01-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z", "/since"),
    ],
)
def test_malformed_periods_are_refused(period: str | None, since: str | None, until: str | None, pointer: str) -> None:
    """The query the contract refuses (the test client checks requests against it) is refused by the hub too."""
    with pytest.raises(HubError) as refused:
        usage.checked(period, since, until, datetime(2026, 11, 26, 12, tzinfo=UTC))
    assert refused.value.code == "request_invalid"
    assert refused.value.detail == {"pointers": [pointer]}


def test_the_window_follows_the_reading() -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    later = format_time(now + timedelta(hours=1))
    reading = {
        "fetched_at": format_time(now),
        "unreadable": False,
        "windows": [
            {"window": "seven_day", "used_percent": 5.0, "resets_at": None},
            window("five_hour", 20, later),
        ],
    }
    found = usage.window_span("five_hour", reading, now)
    assert found.since == now - timedelta(hours=4)
    assert found.measured == {"used_percent": 20, "resets_at": later, "fetched_at": format_time(now)}
    assert usage.window_span("week", reading, now).measured is None  # no reset known
    gone = {**reading, "windows": [window("five_hour", 20, format_time(now - timedelta(minutes=1)))]}
    assert usage.window_span("five_hour", gone, now) == usage.Span(now - timedelta(hours=5), now, None)
    assert usage.window_span("five_hour", {**reading, "unreadable": True}, now).measured is None
    assert usage.window_span("five_hour", None, now).measured is None


def test_checked_periods() -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    assert usage.checked("week", None, None, now) == ("week", None)
    found = usage.checked("range", format_time(now - timedelta(days=1)), None, now)
    assert found == ("range", usage.Span(now - timedelta(days=1), now, None))
    with pytest.raises(HubError):
        usage.checked(None, None, None, now)


def test_an_entry_of_nobody(meter: Meter) -> None:
    writer = meter.scene.writer
    record = _record(meter, writer, primary=False)
    now = meter.hub.clock()
    with meter.hub.pool.connection() as connection:
        entry = usage.subscription_usage(
            connection,
            record=UUID(record),
            owner=UUID(writer.id),
            primary=False,
            span=usage.Span(now - timedelta(hours=1), now, {"used_percent": 1.0, "resets_at": "", "fetched_at": ""}),
            caller=UUID(writer.id),
        )
    assert (entry["people"], entry["total"]) == ([], dict.fromkeys((*FIGURES, "tokens", "sessions"), 0))
