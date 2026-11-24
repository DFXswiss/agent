"""U-HUB-events against the shared vectors (`contracts/vectors/log-events.json`, CRY-02): every hub-valid event is
stored, every broken event is refused with the vector's code, and the replay pair is `event_conflict`.

The vector org (the main log of 27 entries, the platform log prefix of the case) is seeded into a fresh database as the
hub stores it; the case's `now` is the receipt time, its `rotated_at` the receipt time of the scope's current epoch
(`scope_epoch.created_at`) and its `session_device` the device of the header's session (`hub.session`, a row of the
tasks area). Differences that follow from the hub being the receiver, not a reader:

- `log_fork` (an `lh` beyond the head) is `header_invalid` at the hub (decision HUB-05, 7);
- reader cases run with the hub's own checks as well (E5 at receipt, E6, E7); a reader-valid event whose epoch is not
  current, or whose session is not placed on its device, is refused by the hub - the table `READER_VALID_AT_HUB` holds
  the hub's answer for every reader-valid case.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from agent_hub.config import load_config
from agent_hub.crypto.encoding import canonical, parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.events.store import StoredEvent, append_events
from agent_hub.log import vectors
from agent_hub.log.entries import entry_hash, sign_platform_entry
from agent_hub.log.events import check_event_hub
from agent_hub.log.platform import verify_platform_log
from agent_hub.log.verify import verify_log
from agent_hub.orgs import projection
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, hub_environ, start_hub
from tests.platform.hub.orgs.support import insert_device, insert_identity, insert_platform_entries

ROOT = Path(__file__).parents[4] / "contracts" / "vectors"
EVENTS = json.loads((ROOT / "log-events.json").read_text(encoding="utf-8"))
PLATFORM = json.loads((ROOT / "log.json").read_text(encoding="utf-8"))["logs"]["platform"]
USERS = ("alice", "bob", "carol", "dave", "erin", "hub")
DEVICES = ("device", "dev2", "pdev", "qdev")
HUB_CODES = {"log_fork": "header_invalid"}
CASE_CODES = {"E1_org_mismatch": "not_found"}
"""An event of an org the hub does not know is `404 not_found` (the reader compares with the log it holds)."""
Json = dict[str, Any]
Pool = ConnectionPool[Any]


@pytest.fixture
def vector_hub(migrated_db: HubDatabase, hub_pool: Pool, clock: FixedClock) -> Iterator[HubHarness]:
    """A hub with the vectors' platform root key, principals, platform log and org (the main log)."""
    config = load_config(hub_environ(migrated_db, AGENT_HUB_PLATFORM_ROOT_KEY=vectors.sig_pk("root")))
    for label in USERS:
        insert_identity(hub_pool, vectors.pid(label), vectors.identity(label), clock())
    for label in DEVICES:
        insert_device(hub_pool, vectors.pid(label), vectors.identity(label), clock(), "platform")
    yield from start_hub(config, hub_pool, clock)


def _decertify(platform: list[Json], devices: list[str]) -> list[Json]:
    """The platform log extended by a `device_decertify` of every device (the hub derives `excluded` from it)."""
    entries = list(platform)
    root = vectors.identity("root").sig
    for device in devices:
        body = {
            "seq": len(entries),
            "prev": entry_hash(entries[-1]),
            "type": "device_decertify",
            "by": "platform-root",
            "ts": "2026-11-21T12:30:00.000Z",
            "v": 1,
            "device": device,
        }
        entries.append(sign_platform_entry(body, root))
    return entries


def _seed(hub: HubHarness, platform_count: int, excluded: list[str] | None = None, log: Json | None = None) -> None:
    """The platform prefix and the case's membership log (`log` `{name, count}`, default the main log)."""
    platform = _decertify(PLATFORM[:platform_count], excluded or [])
    insert_platform_entries(hub.pool, platform, hub.clock())
    entries = EVENTS["logs"]["main"] if log is None else EVENTS["logs"][log["name"]][: log["count"]]
    state = verify_platform_log(platform, vectors.sig_pk("root")).state
    verified = verify_log(entries, EVENTS["founder"], platform=state)
    now = hub.clock()
    genesis = entries[0]["body"]
    org = genesis["org"]
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.org (id, private, platform_access, default_wg, created_by, created_at, log_head_seq,"
            " log_head_hash) VALUES (%s, true, true, %s, %s, %s, %s, %s)",
            (org, genesis["default_wg"], genesis["subject"], now, verified.head.seq, verified.head.hash),
        )
        for seq, entry in enumerate(entries):
            body = entry["body"]
            connection.execute(
                "INSERT INTO hub.membership_entry (org_id, seq, entry_text, entry_hash, type, by_id, received_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (org, seq, canonical(entry).decode("ascii"), entry_hash(entry), body["type"], body["by"], now),
            )
        projection.project_log(connection, uuid.UUID(org), verified, dict.fromkeys(range(len(entries)), now))


def _prepare(hub: HubHarness, args: Json) -> datetime:
    """Seed the case's world; the receipt time."""
    # the agent cases of ADR-082 name the agents log and no platform prefix (the whole platform log)
    platform = args.get("platform", {"count": len(PLATFORM)})
    _seed(hub, platform["count"], args.get("excluded"), args.get("log"))
    header = args["event"]["header"]
    with hub.pool.connection() as connection, connection.transaction():
        if args.get("rotated_at"):
            connection.execute(
                "UPDATE hub.scope_epoch SET created_at = %s WHERE scope = %s AND scope_id = %s AND epoch ="
                " (SELECT max(epoch) FROM hub.scope_epoch WHERE scope = %s AND scope_id = %s)",
                (
                    parse_time(args["rotated_at"]),
                    header["scope"],
                    header["scope_id"],
                    header["scope"],
                    header["scope_id"],
                ),
            )
        if args.get("session_device"):
            now = hub.clock()
            owner = vectors.pid("alice")
            connection.execute(
                "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
                " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
                (header["task"], header["org"], header["scope_id"], owner, now, now, now),
            )
            connection.execute(
                "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, depth, state, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, 0, 'working', %s)",
                (
                    header["session"],
                    header["task"],
                    header["org"],
                    header["scope_id"],
                    owner,
                    args["session_device"],
                    now,
                ),
            )
    return parse_time(args["now"]) if args.get("now") else hub.clock()


def _append(hub: HubHarness, event: Json, now: datetime) -> list[StoredEvent]:
    with hub.pool.connection() as connection, connection.transaction():
        author = event["header"]["author"] if isinstance(event.get("header"), dict) else vectors.pid("alice")
        return append_events(connection, [event], author, now=now, root_key=hub.config.platform_root_key)


HUB_VALID = EVENTS["hub_valid"]
INVALID = [case for case in EVENTS["invalid"] if case["op"] != "check_replay"]


@pytest.mark.parametrize("case", HUB_VALID, ids=[case["name"] for case in HUB_VALID])
def test_hub_valid_vector_is_stored(vector_hub: HubHarness, case: Json) -> None:
    now = _prepare(vector_hub, case["args"])
    [stored] = _append(vector_hub, case["args"]["event"], now)
    assert (stored.scope_seq, stored.replayed) == (1, False)
    assert _append(vector_hub, case["args"]["event"], now)[0].replayed


@pytest.mark.parametrize("case", INVALID, ids=[case["name"] for case in INVALID])
def test_broken_vector_is_refused_with_its_code(vector_hub: HubHarness, case: Json) -> None:
    now = _prepare(vector_hub, case["args"])
    with pytest.raises(HubError) as refused:
        _append(vector_hub, case["args"]["event"], now)
    assert refused.value.code == CASE_CODES.get(case["name"], HUB_CODES.get(case["error"], case["error"]))


READER_VALID = EVENTS["valid"]


def _reference(args: Json, now: datetime) -> str | None:
    """The answer of the reference hub check (`agent_hub.log.events.check_event_hub`) for the seeded world."""
    platform = verify_platform_log(PLATFORM[: args["platform"]["count"]], vectors.sig_pk("root")).state
    log = verify_log(EVENTS["logs"]["main"], EVENTS["founder"], platform=platform)
    try:
        check_event_hub(
            args["event"],
            log,
            now=now,
            rotated_at=None,
            session_device=args.get("session_device"),
            excluded=platform.excluded(log.state),
        )
    except CryptoError as error:
        return error.code
    return None


@pytest.mark.parametrize("case", READER_VALID, ids=[case["name"] for case in READER_VALID])
def test_reader_valid_vector_at_the_hub(vector_hub: HubHarness, case: Json) -> None:
    """A reader-valid event gets the answer of the reference hub check (stored, or refused because the epoch is not
    current or the author lost the right since `lh`)."""
    args = case["args"]
    now = _prepare(vector_hub, args)
    expected = _reference(args, now)
    if expected is None:
        assert _append(vector_hub, args["event"], now)[0].scope_seq == 1
        return
    assert expected in ("epoch_stale", "author_not_writer")
    with pytest.raises(HubError) as refused:
        _append(vector_hub, args["event"], now)
    assert refused.value.code == expected


def test_reader_valid_vectors_include_stored_events() -> None:
    answers = [_reference(case["args"], FixedClock()()) for case in READER_VALID]
    assert answers.count(None) >= len(READER_VALID) // 2


def test_replay_vector_is_an_event_conflict(vector_hub: HubHarness) -> None:
    [case] = [case for case in EVENTS["invalid"] if case["op"] == "check_replay"]
    stored, incoming = case["args"]["stored"], case["args"]["incoming"]
    _seed(vector_hub, 11)
    now = vector_hub.clock()
    assert _append(vector_hub, stored, now)[0].scope_seq == 1
    with pytest.raises(HubError) as refused:
        _append(vector_hub, incoming, now)
    assert refused.value.code == "event_conflict"
    assert EVENTS["replay"]["incoming"]["header"]["id"] == stored["header"]["id"]
