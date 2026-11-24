"""U-HUB-log against the shared vectors (`contracts/vectors/log.json`, CRY-02): the hub accepts the valid membership
log and refuses every broken variant with the vector's code.

The valid chain is appended batch by batch through the append service (the rotations together with the entry that
requires them, every new epoch with its wraps). Each broken variant is appended to its valid prefix, which is seeded
into a fresh database as the hub stores it (entries, org row, projections); the hub then verifies the stored prefix
itself before it checks the appended entries in append mode (`now` = the vector's time or the hub clock).

Differences that follow from the hub being the appender, not a reader:

- the first appended entry with a wrong `seq` is `seq_conflict` (a concurrent append), not `log_chain`;
- a `device_assign` with `platform_seq` in an org without platform access is answered `platform_access_denied` before
  the log rules run (ADR-026); the vector org is seeded as an org with platform access except for that case;
- genesis cases go through `POST /v1/orgs` semantics (`create_org`), where the founder key is the caller's registered
  identity; `log_empty`, `L3_founder_not_pinned` (a reader's pinned key) and the reader rules L9/L10 have no hub
  counterpart (the contract requires at least one entry; L10 at the hub is `header_invalid`, tested with events).

The `admit_*` variants (ADR-060) are appended to their prefix of the vector log `admit` against the platform log with
the identity anchors; every vector principal is registered (erin under the ID derived from its platform invitation)
and every vector device announced `log.admit`, so the hub-only checks of `agent_hub.orgs.admit` (registered identity,
`peer_outdated`) pass and the hub answers each variant with the vector's code: the log rules run first. The valid
admissions are appended up to the last one the hub accepts: the vector's third `admit` is anchored by a certification
that was ended later, which readers accept and the hub refuses (`admit_decertified_anchor_at_hub`).
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
from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.encoding import canonical, parse_time
from agent_hub.errors import HubError
from agent_hub.log import vectors
from agent_hub.log.entries import entry_hash
from agent_hub.log.platform import verify_platform_log
from agent_hub.log.verify import verify_log
from agent_hub.orgs import projection
from agent_hub.orgs.append import Appender, append_batch
from agent_hub.orgs.genesis import create_org
from agent_hub.orgs.projections import rebuild_orgs
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, hub_environ, start_hub
from tests.platform.hub.orgs.support import insert_device, insert_identity, insert_platform_entries

VECTORS = json.loads((Path(__file__).parents[4] / "contracts" / "vectors" / "log.json").read_text(encoding="utf-8"))
USERS = ("alice", "bob", "carol", "dave", "erin", "hub")
DEVICES = ("device", "dev2", "pdev", "qdev")
LABELS = {vectors.pid(label): label for label in (*USERS, *DEVICES)} | {vectors.uid("erin"): "erin"}
HUB_CODES = {"L1_seq_gap": "seq_conflict", "L1_duplicate_entry": "seq_conflict"}
NOT_AT_THE_HUB = {"log_empty", "L3_founder_not_pinned"}
NO_PLATFORM_ACCESS = {
    "assign_without_platform_access": "platform_access_denied",
    "assign_org_without_platform_access": "platform_access_denied",
}
CASES = [case for case in VECTORS["invalid"] if case["op"] in ("verify_log", "append_log")]
EXCLUDED = frozenset({vectors.pid("qdev")})
VALID_BATCHES = [
    (1, 1),
    (2, 2),
    (3, 3),
    (4, 4),
    (5, 5),
    (6, 6),
    (7, 7),
    (8, 8),
    (9, 9),
    (10, 10),
    (11, 11),
    (12, 12),
    (13, 13),
    (14, 14),
    (15, 16),
    (17, 21),
    (22, 22),
    (23, 23),
    (24, 24),
    (25, 26),
]
Json = dict[str, Any]
Pool = ConnectionPool[Any]


@pytest.fixture
def vector_hub(migrated_db: HubDatabase, hub_pool: Pool, clock: FixedClock) -> Iterator[HubHarness]:
    """A hub configured with the vectors' platform root key and the vectors' principals registered."""
    config = load_config(hub_environ(migrated_db, AGENT_HUB_PLATFORM_ROOT_KEY=vectors.sig_pk("root")))
    for label in USERS:
        insert_identity(hub_pool, vectors.pid(label), vectors.identity(label), clock())
    insert_identity(hub_pool, vectors.uid("erin"), vectors.identity("erin"), clock())
    for label in DEVICES:
        insert_device(hub_pool, vectors.pid(label), vectors.identity(label), clock(), "platform")
    with hub_pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.device SET features = '{log.admit,log.org_assign}'")
    yield from start_hub(config, hub_pool, clock)


def _platform(hub: HubHarness, ref: Json) -> None:
    insert_platform_entries(hub.pool, VECTORS["logs"][ref["name"]][: ref["count"]], hub.clock())


def _seed(hub: HubHarness, count: int, *, private: bool, log: str | list[Json] = "main") -> None:
    """The first `count` entries of a vector log (a log of `log.json` by name, default the main log, or the entries
    of another vector file) as the hub stores them."""
    entries = (VECTORS["logs"][log] if isinstance(log, str) else log)[:count]
    state = verify_platform_log(VECTORS["logs"]["platform"], vectors.sig_pk("root")).state
    verified = verify_log(entries, vectors.sig_pk("alice"), platform=state)
    now = hub.clock()
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.org (id, private, platform_access, default_wg, created_by, created_at, log_head_seq,"
            " log_head_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                vectors.ORG,
                private,
                private,
                vectors.W0,
                vectors.pid("alice"),
                now,
                verified.head.seq,
                verified.head.hash,
            ),
        )
        for seq, entry in enumerate(entries):
            body = entry["body"]
            connection.execute(
                "INSERT INTO hub.membership_entry (org_id, seq, entry_text, entry_hash, type, by_id, received_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (vectors.ORG, seq, canonical(entry).decode("ascii"), entry_hash(entry), body["type"], body["by"], now),
            )
        projection.project_log(connection, uuid.UUID(vectors.ORG), verified, dict.fromkeys(range(count), now))


def _appender(by: str) -> Appender:
    return Appender(by, "device" if LABELS.get(by) in DEVICES else "user")


def _append(hub: HubHarness, entries: list[Json], wraps: list[Json], now: datetime) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        head = connection.execute("SELECT log_head_seq FROM hub.org").fetchone()
        append_batch(
            connection,
            org_id=uuid.UUID(vectors.ORG),
            appender=_appender(entries[0]["body"]["by"]),
            expected_seq=int(head[0]) + 1 if head else 0,
            entries=entries,
            wraps=wraps,
            events=[],
            now=now,
            root_key=hub.config.platform_root_key,
        )


def _create(hub: HubHarness, genesis: Json) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        create_org(
            connection,
            appender=_appender(genesis["body"]["by"]),
            genesis=genesis,
            wraps=[],
            events=[],
            platform_invite_id=None,
            now=hub.clock(),
            root_key=hub.config.platform_root_key,
        )


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_broken_vector_is_refused_with_its_code(vector_hub: HubHarness, case: Json) -> None:
    if case["name"] in NOT_AT_THE_HUB:
        assert case["op"] == "verify_log"
        return
    args = case["args"]
    _platform(vector_hub, args["platform"])
    count = args["log"]["count"]
    appended = args.get("entries", args.get("append"))
    expected = NO_PLATFORM_ACCESS.get(case["name"], HUB_CODES.get(case["name"], case["error"]))
    now = parse_time(args["now"]) if "now" in args else vector_hub.clock()
    if count == 0:
        with pytest.raises(HubError) as refused:
            _create(vector_hub, appended[0])
    else:
        _seed(vector_hub, count, private=case["name"] not in NO_PLATFORM_ACCESS, log=args["log"]["name"])
        for entry in appended[:-1]:
            _append(vector_hub, [entry], [], now)
        with pytest.raises(HubError) as refused:
            _append(vector_hub, [appended[-1]], [], now)
    assert refused.value.code == expected


def _wraps_for(log_states: Any, batch: tuple[int, int], entries: list[Json] | None = None) -> list[Json]:
    """The wraps of every epoch the batch creates, for every entitled recipient, by the batch's author."""
    first, last = batch
    entries = VECTORS["logs"]["main"] if entries is None else entries
    author = LABELS[entries[first]["body"]["by"]]
    state = log_states[last]
    created = set()
    for seq in range(first, last + 1):
        body = entries[seq]["body"]
        if body["type"] == "rotate":
            created.add((body["scope"], body["scope_id"]))
        if body["type"] == "wg_create":
            created.add(("wg", body["wg"]))
    wraps = []
    for scope, scope_id in sorted(created):
        for recipient in state.readers(scope, scope_id, EXCLUDED):
            wraps.append(vectors.make_wrap(author, scope, scope_id, state.epoch(scope, scope_id), LABELS[recipient]))
    return wraps


def _tables(hub: HubHarness) -> dict[str, list[tuple[Any, ...]]]:
    queries = {
        "org": "SELECT id, private, platform_access, default_wg, log_head_seq, log_head_hash, rotation_pending FROM"
        " hub.org",
        "org_member": "SELECT * FROM hub.org_member ORDER BY 1, 2",
        "wg": "SELECT * FROM hub.wg ORDER BY 1",
        "wg_access": "SELECT * FROM hub.wg_access ORDER BY 1, 2",
        "wg_device": "SELECT * FROM hub.wg_device ORDER BY 1, 2",
        "scope_epoch": "SELECT * FROM hub.scope_epoch ORDER BY 1, 2, 3",
    }
    with hub.pool.connection() as connection:
        return {name: [tuple(map(str, row)) for row in connection.execute(query)] for name, query in queries.items()}


def test_valid_vector_log_is_accepted_and_rebuilds_identically(vector_hub: HubHarness) -> None:
    _platform(vector_hub, {"name": "platform", "count": 11})
    _seed(vector_hub, 1, private=True)
    entries = VECTORS["logs"]["main"]
    state = verify_platform_log(VECTORS["logs"]["platform"], vectors.sig_pk("root")).state
    states = verify_log(entries, vectors.sig_pk("alice"), platform=state).states
    for first, last in VALID_BATCHES:
        _append(vector_hub, entries[first : last + 1], _wraps_for(states, (first, last)), vector_hub.clock())
    heads = VECTORS["valid"]["heads"]
    incremental = _tables(vector_hub)
    assert incremental["org"][0][4:6] == (str(heads[-1]["seq"]), heads[-1]["hash"])
    expected = VECTORS["valid"]["projections"][-1]
    members = {row[1]: (row[3], row[5]) for row in incremental["org_member"]}
    assert members == {item["principal_id"]: (item["role"], str(item["removed_seq"])) for item in expected["members"]}
    access = {(row[0], row[1]): row[2] for row in incremental["wg_access"] if row[4] == "None"}
    assert access == {
        (wg["wg_id"], user): level for wg in expected["workgroups"] for user, level in wg["access"].items()
    }
    devices = {(row[0], row[1]) for row in incremental["wg_device"] if row[3] == "None"}
    assert devices == {(wg["wg_id"], device) for wg in expected["workgroups"] for device in wg["devices"]}
    with vector_hub.pool.connection() as connection, connection.transaction():
        assert rebuild_orgs(connection, vector_hub.config) == 1
    assert _tables(vector_hub) == incremental


def _wrap_for(author: str, scope: str, scope_id: str, epoch: int, recipient: str) -> Json:
    """A wrap of a vector scope key for `recipient` (a label), addressed to its user ID (`vectors.uid`)."""
    recipient_id = vectors.uid(recipient)
    sealed = wrap_format.wrap_key(
        vectors.identity(recipient).kem_public(),
        scope=scope,
        scope_id=scope_id,
        epoch=epoch,
        recipient=recipient_id,
        key=vectors.scope_key(scope, scope_id, epoch),
    )
    signature = wrap_format.sign_wrap(
        vectors.identity(author).sig, scope=scope, scope_id=scope_id, epoch=epoch, recipient=recipient_id, wrap=sealed
    )
    return {
        "scope": scope,
        "scope_id": scope_id,
        "epoch": epoch,
        "recipient_id": recipient_id,
        "wrap": sealed,
        "sig": signature,
    }


def test_valid_admissions_of_the_vectors_are_accepted(vector_hub: HubHarness) -> None:
    valid = VECTORS["admit_valid"]
    _platform(vector_hub, valid["platform"])
    _seed(vector_hub, vectors.ADMIT_COUNT - 4, private=True, log="admit")
    entries = VECTORS["logs"]["admit"]
    state = verify_platform_log(VECTORS["logs"]["platform"], vectors.sig_pk("root")).state
    states = verify_log(entries, vectors.sig_pk("alice"), platform=state).states
    for seq, subject in ((27, "dave"), (28, "erin")):
        before = states[seq - 1]
        wraps = [
            _wrap_for("alice", "org", vectors.ORG, before.epoch("org", vectors.ORG), subject),
            _wrap_for("alice", "wg", vectors.W0, before.epoch("wg", vectors.W0), subject),
        ]
        _append(vector_hub, [entries[seq]], wraps, vector_hub.clock())
    tables = _tables(vector_hub)
    assert tables["org"][0][4:6] == (str(valid["heads"][1]["seq"]), valid["heads"][1]["hash"])
    expected = valid["projections"][1]["members"]
    members = {row[1]: (row[3], row[5]) for row in tables["org_member"]}
    assert members == {item["principal_id"]: (item["role"], str(item["removed_seq"])) for item in expected}
    with vector_hub.pool.connection() as connection:
        notified = connection.execute(
            "SELECT recipient_id::text, ref FROM hub.notification WHERE kind = 'org.admitted' ORDER BY recipient_id"
        ).fetchall()
    assert sorted(notified) == sorted(
        [(vectors.pid("dave"), vectors.pid("alice")), (vectors.uid("erin"), vectors.pid("alice"))]
    )
    bob = [_wrap_for("erin", "org", vectors.ORG, states[28].epoch("org", vectors.ORG), "bob")]
    bob.append(_wrap_for("erin", "wg", vectors.W0, states[28].epoch("wg", vectors.W0), "bob"))
    with pytest.raises(HubError) as refused:
        _append(vector_hub, [entries[29]], bob, vector_hub.clock())
    assert refused.value.code == "log_admit"
