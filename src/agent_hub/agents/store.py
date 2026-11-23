"""Pairings and agents at the hub (11-agent-access.md sections 2, 4 and 5, ADR-056).

Life cycle of a `hub.agent` row: `open` (created by the agent, `POST /v1/agent-pairings`) → `confirmed` (the user's
browser stored the sealed identity, the sealed label and the claim hash, `POST /v1/me/agents`) → `active` (the agent
proved the claim, `POST /v1/agent/claim`) → `revoked` (`DELETE /v1/me/agents/{id}`). The claim deadline
`expires_at` is ten minutes after the creation and at most `CLAIM_SECONDS` after the confirmation; an unclaimed row is
deleted one hour after it (`agents/jobs.py`).

The hub stores public keys, states, times and the two ciphertexts it cannot open; it checks the agent's proof of
possession and the claim against its hash. Confirmation needs the browser session that the pairing-tagged login
ceremony created (the passkey gesture), at most `GESTURE_SECONDS` before.

One main orchestrator per user (concept idea 34, ADR-056 section 4): a user has at most one `confirmed` or `active`
agent. The passkey confirmation of a new pairing revokes the previous one in the same transaction and records it in
`replaces`; the unique index `agent_one_per_user` makes two at once impossible.
"""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import psycopg.errors

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError

Connection = psycopg.Connection[Any]

PAIRING_SECONDS = 600
GESTURE_SECONDS = 300
CLAIM_SECONDS = 60
"""After the confirmation the claim deadline is at most this far away: the agent listening on the loopback claims at
once (review M1, decisions KOR-24)."""
UNCLAIMED_RETENTION_SECONDS = 3600
MAX_OPEN_PAIRINGS = 10_000
MAX_OPEN_PER_SOURCE = 3
# Unexpired open pairings per network of the source (prefix length, limit) by IP version, on top of
# `MAX_OPEN_PER_SOURCE` per IPv4 address or IPv6 /64 (review M6, decisions KOR-24).
NETWORK_LIMITS: dict[int, tuple[tuple[int, int], ...]] = {4: ((24, 10),), 6: ((56, 10), (48, 30))}
PAIRING_LOCK = 742180240
MAX_CLAIM_MISSES = 5
MIN_SEALED_BYTES = 48
"""The shortest sealed identity or label `hub.agent` stores: HPKE `enc` (32) and tag (16)."""
_PAIRING_COLUMNS = "id, kem_pub, sig_pub, state, expires_at, created_at"


@dataclass(frozen=True)
class Pairing:
    """The public part of a pairing."""

    id: UUID
    kem_pub: bytes
    sig_pub: bytes
    state: str
    expires_at: datetime


def _pairing(row: tuple[Any, ...]) -> Pairing:
    return Pairing(id=row[0], kem_pub=bytes(row[1]), sig_pub=bytes(row[2]), state=row[3], expires_at=row[4])


def public_state(state: str, expires_at: datetime, now: datetime) -> str:
    """`open`, `confirmed`, `active` or `closed` (revoked, or unclaimed past its deadline)."""
    if state == "active":
        return "active"
    if state in ("open", "confirmed") and expires_at > now:
        return state
    return "closed"


def pairing_json(pairing: Pairing, now: datetime) -> dict[str, Any]:
    """The answer of `POST /v1/agent-pairings` and `GET /v1/agent-pairings/{id}`."""
    return {
        "pairing_id": str(pairing.id),
        "pub": {"kem": b64u_encode(pairing.kem_pub), "sig": b64u_encode(pairing.sig_pub)},
        "state": public_state(pairing.state, pairing.expires_at, now),
        "expires_at": format_time(pairing.expires_at),
    }


def source_key(address: str) -> str:
    """The source of the per-source limit: the IPv4 address, the /64 network of an IPv6 address, else the text."""
    parsed = _address(address)
    if parsed is None:
        return address[:64]
    if isinstance(parsed, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((parsed, 64), strict=False))
    return str(parsed)


def _address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def source_limits(address: str) -> list[tuple[str, int]]:
    """The networks of the source with their limits of unexpired open pairings (`NETWORK_LIMITS`); empty for a
    source that is no IP address (its text is limited by `MAX_OPEN_PER_SOURCE` alone)."""
    parsed = _address(address)
    if parsed is None:
        return []
    return [
        (str(ipaddress.ip_network((parsed, prefix), strict=False)), limit)
        for prefix, limit in NETWORK_LIMITS[parsed.version]
    ]


def _check_limits(connection: Connection, key: str, networks: list[tuple[str, int]], now: datetime) -> None:
    counts = connection.execute(
        "SELECT count(*), count(*) FILTER (WHERE source = %s) FROM hub.agent WHERE state = 'open' AND expires_at > %s",
        (key, now),
    ).fetchone()
    refused = counts is not None and (int(counts[0]) >= MAX_OPEN_PAIRINGS or int(counts[1]) >= MAX_OPEN_PER_SOURCE)
    for network, limit in networks:
        found = connection.execute(
            "SELECT count(*) FROM hub.agent WHERE state = 'open' AND expires_at > %s AND source_net::inet <<= %s::inet",
            (now, network),
        ).fetchone()
        refused = refused or (found is not None and int(found[0]) >= limit)
    if refused:
        raise HubError("rate_limited", headers={"Retry-After": "60"})


def create_pairing(
    connection: Connection, pairing_id: UUID, *, kem: str, sig: str, pop: str, source: str, now: datetime
) -> Pairing:
    """Store an open pairing after checking the proof of possession (`signature_invalid`); the same ID with the same
    keys returns the stored pairing, other keys (or keys of another pairing) `409 id_conflict`; past the limits of
    unexpired open pairings - `MAX_OPEN_PER_SOURCE` per source, `NETWORK_LIMITS` per network, `MAX_OPEN_PAIRINGS` in
    all - `429 rate_limited`. Creations are serialised by the transaction lock `PAIRING_LOCK` (the counts hold until
    the insert commits)."""
    try:
        kem_pub, sig_pub = b64u_decode(kem, 32), b64u_decode(sig, 32)
        agent_format.verify_pop(str(pairing_id), kem_pub, sig_pub, pop)
    except CryptoError:
        raise HubError("signature_invalid") from None
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (PAIRING_LOCK,))
    found = connection.execute(f"SELECT {_PAIRING_COLUMNS} FROM hub.agent WHERE id = %s", (pairing_id,)).fetchone()
    if found is not None:
        stored = _pairing(found)
        if stored.kem_pub != kem_pub or stored.sig_pub != sig_pub:
            raise HubError("id_conflict")
        return stored
    key = source_key(source)
    parsed = _address(source)
    _check_limits(connection, key, source_limits(source), now)
    expires_at = now + timedelta(seconds=PAIRING_SECONDS)
    try:
        with connection.transaction():
            connection.execute(
                "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, source, source_net, created_at, expires_at)"
                " VALUES (%s, %s, %s, 'open', %s, %s, %s, %s)",
                (pairing_id, kem_pub, sig_pub, key, None if parsed is None else str(parsed), now, expires_at),
            )
    except psycopg.errors.UniqueViolation:
        # the keys of an agent that is not revoked (`agent_sig_pub`)
        raise HubError("id_conflict") from None
    return Pairing(id=pairing_id, kem_pub=kem_pub, sig_pub=sig_pub, state="open", expires_at=expires_at)


def get_pairing(connection: Connection, pairing_id: UUID) -> Pairing:
    """The pairing row (`404 pairing_unknown` when there is none)."""
    row = connection.execute(f"SELECT {_PAIRING_COLUMNS} FROM hub.agent WHERE id = %s", (pairing_id,)).fetchone()
    if row is None:
        raise HubError("pairing_unknown")
    return _pairing(row)


def visible_pairing(connection: Connection, pairing_id: UUID, now: datetime) -> Pairing:
    """The pairing while it is open or confirmed and before its deadline, else `404 pairing_unknown` (a claimed,
    revoked or expired pairing is not shown to anybody)."""
    pairing = get_pairing(connection, pairing_id)
    if public_state(pairing.state, pairing.expires_at, now) not in ("open", "confirmed"):
        raise HubError("pairing_unknown")
    return pairing


def check_open(connection: Connection, pairing_id: UUID, now: datetime) -> None:
    """`404 pairing_unknown` unless the pairing is open and before its deadline (login ceremony with a pairing)."""
    pairing = get_pairing(connection, pairing_id)
    if public_state(pairing.state, pairing.expires_at, now) != "open":
        raise HubError("pairing_unknown")


@dataclass(frozen=True)
class Confirmation:
    """The body of `POST /v1/me/agents`, decoded."""

    pairing_id: UUID
    bundle_ct: bytes
    label_ct: bytes
    claim_hash: bytes


def _audit(connection: Connection, action: str, user: UUID, agent_id: UUID, now: datetime) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=action,
            result="ok",
            target_kind="agent",
            target_id=str(agent_id),
            detail={"agent": str(agent_id)},
        ),
    )


def gesture_session(connection: Connection, key_id: str, pairing_id: UUID, now: datetime) -> None:
    """`403 pairing_gesture_required` unless the browser session `key_id` was created by a login ceremony tagged with
    this pairing at most `GESTURE_SECONDS` ago."""
    row = connection.execute(
        "SELECT pairing_id, created_at FROM hub.browser_session WHERE key_id = %s", (key_id,)
    ).fetchone()
    if row is None or row[0] != pairing_id or row[1] < now - timedelta(seconds=GESTURE_SECONDS):
        raise HubError("pairing_gesture_required")


@dataclass(frozen=True)
class Confirmed:
    """The result of `confirm`: whether this call changed anything, and the agents it replaced (revoked)."""

    changed: bool
    replaced: tuple[UUID, ...] = ()


def _lock_user(connection: Connection, user: UUID) -> None:
    """Serialise the confirmations of one user (the rule of one main orchestrator holds under concurrency)."""
    connection.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (PAIRING_LOCK, str(user)))


def confirm(connection: Connection, user: UUID, key_id: str, body: Confirmation, now: datetime) -> Confirmed:
    """Confirm an open pairing for `user` by the browser session of the pairing-tagged login (the passkey gesture;
    an agent never confirms, scope.EXCEPTIONS). The user's previous confirmed or active agent is revoked and named
    in `replaces`. A repeat with identical values is idempotent (`changed` False, nothing replaced)."""
    gesture_session(connection, key_id, body.pairing_id, now)
    _lock_user(connection, user)
    row = connection.execute(
        "SELECT state, user_id, expires_at, bundle_ct, label_ct, claim_hash FROM hub.agent WHERE id = %s FOR UPDATE",
        (body.pairing_id,),
    ).fetchone()
    if row is None or row[2] <= now:
        raise HubError("pairing_unknown")
    state, owner, _expires, bundle_ct, label_ct, stored_hash = row
    if state == "confirmed" and owner == user:
        same = (bytes(bundle_ct), bytes(label_ct), bytes(stored_hash)) == (
            body.bundle_ct,
            body.label_ct,
            body.claim_hash,
        )
        if same:
            return Confirmed(changed=False)
        raise HubError("id_conflict")
    if state != "open":
        raise HubError("pairing_unknown")
    previous = [
        agent_id
        for (agent_id,) in connection.execute(
            "SELECT id FROM hub.agent WHERE user_id = %s AND state IN ('confirmed', 'active') ORDER BY id FOR UPDATE",
            (user,),
        ).fetchall()
    ]
    for agent_id in previous:
        revoke(connection, user, agent_id, now)
    connection.execute(
        "UPDATE hub.agent SET state = 'confirmed', user_id = %s, bundle_ct = %s, label_ct = %s, claim_hash = %s,"
        " confirmed_at = %s, source = NULL, source_net = NULL, replaces = %s,"
        " expires_at = LEAST(expires_at, %s) WHERE id = %s",
        (
            user,
            body.bundle_ct,
            body.label_ct,
            body.claim_hash,
            now,
            previous[-1] if previous else None,
            now + timedelta(seconds=CLAIM_SECONDS),
            body.pairing_id,
        ),
    )
    _audit(connection, "agent.confirm", user, body.pairing_id, now)
    return Confirmed(changed=True, replaced=tuple(previous))


def _agent_json(row: tuple[Any, ...]) -> dict[str, Any]:
    agent_id, user_id, bundle_ct, label_ct, created_at, claimed_at = row
    return {
        "agent_id": str(agent_id),
        "user_id": str(user_id),
        "bundle_ct": b64u_encode(bytes(bundle_ct)),
        "label_ct": b64u_encode(bytes(label_ct)),
        "created_at": format_time(created_at),
        "claimed_at": format_time(claimed_at),
    }


def claim(connection: Connection, agent_id: UUID, claim_raw: bytes, now: datetime) -> dict[str, Any] | None:
    """Activate a confirmed agent that proves the claim; a repeat by an active agent with the right claim returns the
    same answer. A wrong claim is counted (the fifth deletes an unclaimed pairing) and answered None: the caller
    commits the count and refuses with `401 claim_invalid`."""
    row = connection.execute(
        "SELECT state, user_id, expires_at, claim_hash, claim_misses FROM hub.agent WHERE id = %s FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None or row[0] not in ("confirmed", "active") or (row[0] == "confirmed" and row[2] <= now):
        raise HubError("pairing_unknown")
    state, user, _expires, stored_hash, misses = row
    if not hmac.compare_digest(agent_format.claim_hash(claim_raw), bytes(stored_hash)):
        if misses + 1 >= MAX_CLAIM_MISSES and state == "confirmed":
            connection.execute("DELETE FROM hub.agent WHERE id = %s", (agent_id,))
        else:
            connection.execute("UPDATE hub.agent SET claim_misses = claim_misses + 1 WHERE id = %s", (agent_id,))
        return None
    if state == "confirmed":
        connection.execute(
            "UPDATE hub.agent SET state = 'active', claimed_at = %s, last_used_at = %s WHERE id = %s",
            (now, now, agent_id),
        )
        _audit(connection, "agent.claim", user, agent_id, now)
    return me(connection, agent_id)


def me(connection: Connection, agent_id: UUID) -> dict[str, Any]:
    """The answer of `GET /v1/agent/me` (and of a successful claim)."""
    row = connection.execute(
        "SELECT id, user_id, bundle_ct, label_ct, created_at, claimed_at FROM hub.agent"
        " WHERE id = %s AND state = 'active'",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise HubError("key_revoked")
    return _agent_json(row)


def _time(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


def list_agents(connection: Connection, user: UUID, now: datetime) -> list[dict[str, Any]]:
    """The user's agents, newest first; an unclaimed one past its deadline is `expired`."""
    rows = connection.execute(
        "SELECT id, state, label_ct, created_at, confirmed_at, claimed_at, last_used_at, revoked_at, expires_at,"
        " replaces FROM hub.agent WHERE user_id = %s ORDER BY created_at DESC, id DESC",
        (user,),
    ).fetchall()
    agents = []
    for agent_id, state, label_ct, created, confirmed, claimed, used, revoked, expires, replaces in rows:
        shown = "expired" if state == "confirmed" and expires <= now else state
        agents.append(
            {
                "agent_id": str(agent_id),
                "state": shown,
                "label_ct": b64u_encode(bytes(label_ct)),
                "created_at": format_time(created),
                "confirmed_at": format_time(confirmed),
                "claimed_at": _time(claimed),
                "last_used_at": _time(used),
                "revoked_at": _time(revoked),
                "replaces": None if replaces is None else str(replaces),
            }
        )
    return agents


def revoke(connection: Connection, user: UUID, agent_id: UUID, now: datetime) -> bool:
    """Revoke the user's agent at once (`404 not_found` for an unknown or foreign one); False when it was revoked
    already (no change)."""
    row = connection.execute(
        "SELECT state FROM hub.agent WHERE id = %s AND user_id = %s FOR UPDATE", (agent_id, user)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    if row[0] == "revoked":
        return False
    connection.execute(
        "UPDATE hub.agent SET state = 'revoked', revoked_at = %s, bundle_ct = NULL WHERE id = %s", (now, agent_id)
    )
    _audit(connection, "agent.revoke", user, agent_id, now)
    return True


def revoke_all(connection: Connection, user: UUID, now: datetime) -> list[UUID]:
    """Revoke every confirmed or active agent of the user at once; the IDs revoked."""
    rows = connection.execute(
        "SELECT id FROM hub.agent WHERE user_id = %s AND state IN ('confirmed', 'active') ORDER BY id FOR UPDATE",
        (user,),
    ).fetchall()
    for (agent_id,) in rows:
        revoke(connection, user, agent_id, now)
    return [agent_id for (agent_id,) in rows]


def purge_unclaimed(connection: Connection, now: datetime) -> int:
    """Delete open and confirmed rows one hour after their claim deadline; the number deleted."""
    cursor = connection.execute(
        "DELETE FROM hub.agent WHERE state IN ('open', 'confirmed') AND expires_at < %s",
        (now - timedelta(seconds=UNCLAIMED_RETENTION_SECONDS),),
    )
    return cursor.rowcount
