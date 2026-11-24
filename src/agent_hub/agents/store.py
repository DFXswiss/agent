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

Own identity (ADR-082): a pairing confirmed in the new form receives no identity. The row keeps the user's delegation
certificate `cert` over the agent's ID, keys and generation, the generation `gen` (one more than the highest the hub has
issued for the user, `next_gen`; the browser learns it from `GET /v1/agent-pairings/{id}` signed by its session) and
`user_ct` (the user's public keys and the certificate sealed to the agent) in the place of `bundle_ct`; `own_identity`
is set and `held_identity` cleared. The old form (`bundle_ct`) is refused for an account with a passkey
(`409 client_outdated`, ADR-082 section 9.10); an account of an agent alone keeps both forms. An own-identity agent that
is not claimed before its deadline is revoked (`unclaimed`) instead of deleted, so that its generation, which is in
the logs, stays counted. Every revocation records its reason (`revoked_reason`).

A pairing from another computer (`remote`, ADR-067, 11-agent-access.md section 5.4) has a 16-byte `nonce` drawn by
the hub (input of the agent code the user types), must be confirmed within `REMOTE_PAIRING_SECONDS` of its creation
and then has `REMOTE_CLAIM_SECONDS` to be claimed. Its confirmation must carry `handover_ct` (the handover sealed to
the agent; `handover_required`), a loopback confirmation must not (`handover_unexpected`). The hub relays
`handover_ct` to the agent (`handover`) and clears it at the claim, the revocation and the claim deadline
(`clear_expired_handovers`).
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg
import psycopg.errors

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
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
REMOTE_PAIRING_SECONDS = 300
"""A remote pairing must be confirmed within this time of its creation (ADR-067 section 4.3)."""
REMOTE_CLAIM_SECONDS = 600
"""After the confirmation of a remote pairing the agent has this long to claim: a conversational agent may answer its
user only at the end of a turn (ADR-067 section 3)."""
NONCE_BYTES = agent_format.NONCE_BYTES
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
_PAIRING_COLUMNS = "id, kem_pub, sig_pub, state, expires_at, remote, nonce"


@dataclass(frozen=True)
class Pairing:
    """The public part of a pairing."""

    id: UUID
    kem_pub: bytes
    sig_pub: bytes
    state: str
    expires_at: datetime
    remote: bool = False
    nonce: bytes | None = None


def _pairing(row: tuple[Any, ...]) -> Pairing:
    nonce = None if row[6] is None else bytes(row[6])
    return Pairing(
        id=row[0],
        kem_pub=bytes(row[1]),
        sig_pub=bytes(row[2]),
        state=row[3],
        expires_at=row[4],
        remote=row[5],
        nonce=nonce,
    )


def public_state(state: str, expires_at: datetime, now: datetime) -> str:
    """`open`, `confirmed`, `active` or `closed` (revoked, or unclaimed past its deadline)."""
    if state == "active":
        return "active"
    if state in ("open", "confirmed") and expires_at > now:
        return state
    return "closed"


def remote_json(pairing: Pairing) -> dict[str, Any]:
    """`{"remote": true, "nonce"}` of a remote pairing; nothing for a loopback pairing (answered as before KOR-42)."""
    if not pairing.remote or pairing.nonce is None:
        return {}
    return {"remote": True, "nonce": b64u_encode(pairing.nonce)}


def pairing_json(pairing: Pairing, now: datetime) -> dict[str, Any]:
    """The answer of `GET /v1/agent-pairings/{id}`."""
    return {
        "pairing_id": str(pairing.id),
        "pub": {"kem": b64u_encode(pairing.kem_pub), "sig": b64u_encode(pairing.sig_pub)},
        "state": public_state(pairing.state, pairing.expires_at, now),
        "expires_at": format_time(pairing.expires_at),
        **remote_json(pairing),
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
    connection: Connection,
    pairing_id: UUID,
    *,
    kem: str,
    sig: str,
    pop: str,
    source: str,
    now: datetime,
    remote: bool = False,
) -> Pairing:
    """Store an open pairing after checking the proof of possession (`signature_invalid`); the same ID with the same
    keys and mode returns the stored pairing, other keys or another mode (or keys of another pairing)
    `409 id_conflict`; a remote pairing gets a fresh nonce and the shorter deadline; past the limits of
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
        if stored.kem_pub != kem_pub or stored.sig_pub != sig_pub or stored.remote != remote:
            raise HubError("id_conflict")
        unchanged()
        return stored
    key = source_key(source)
    parsed = _address(source)
    _check_limits(connection, key, source_limits(source), now)
    expires_at = now + timedelta(seconds=REMOTE_PAIRING_SECONDS if remote else PAIRING_SECONDS)
    nonce = secrets.token_bytes(NONCE_BYTES) if remote else None
    try:
        with connection.transaction():
            connection.execute(
                "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, source, source_net, created_at, expires_at,"
                " remote, nonce) VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s, %s)",
                (
                    pairing_id,
                    kem_pub,
                    sig_pub,
                    key,
                    None if parsed is None else str(parsed),
                    now,
                    expires_at,
                    remote,
                    nonce,
                ),
            )
    except psycopg.errors.UniqueViolation:
        # the keys of an agent that is not revoked (`agent_sig_pub`)
        raise HubError("id_conflict") from None
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="anonymous",
            action="agent.pairing",
            result="ok",
            target_kind="agent",
            target_id=str(pairing_id),
        ),
    )
    return Pairing(
        id=pairing_id, kem_pub=kem_pub, sig_pub=sig_pub, state="open", expires_at=expires_at, remote=remote, nonce=nonce
    )


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
class OwnIdentity:
    """What a confirmation or conversion in the new form stores instead of the sealed identity (ADR-082 section 2):
    the generation, the user's certificate (64-byte Ed25519 signature) and the sealed user anchor."""

    gen: int
    cert: bytes
    user_ct: bytes


@dataclass(frozen=True)
class Confirmation:
    """The body of `POST /v1/me/agents`, decoded: exactly one of `bundle_ct` (the old form) and `own`."""

    pairing_id: UUID
    label_ct: bytes
    claim_hash: bytes
    handover_ct: bytes | None = None
    bundle_ct: bytes | None = None
    own: OwnIdentity | None = None


def _audit(connection: Connection, action: str, user: UUID, agent_id: UUID, now: datetime, *, remote: bool) -> None:
    detail: dict[str, Any] = {"agent": str(agent_id)}
    if remote:
        detail["remote"] = True
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
            detail=detail,
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
    """The result of `confirm`: whether this call changed anything, the agents it replaced (revoked), whether the
    pairing is a remote one, and the agent's public keys `(kem, sig)`."""

    changed: bool
    replaced: tuple[UUID, ...] = ()
    remote: bool = False
    pub: tuple[bytes, bytes] = (b"", b"")


def next_gen(connection: Connection, user: UUID) -> int:
    """The generation the next own-identity agent of the user gets: one more than the highest the hub has issued for
    the user (revoked rows included; rows are never deleted once they have a generation)."""
    row = connection.execute("SELECT max(gen) FROM hub.agent WHERE user_id = %s", (user,)).fetchone()
    return 1 if row is None or row[0] is None else int(row[0]) + 1


def has_passkey(connection: Connection, user: UUID) -> bool:
    """Whether the user has an active passkey: an account of a person. An account without one is an account of an
    agent alone (rule E-27, KOR-62), whose agent may replace its main orchestrator itself."""
    row = connection.execute(
        "SELECT 1 FROM hub.passkey WHERE user_id = %s AND removed_at IS NULL LIMIT 1", (user,)
    ).fetchone()
    return row is not None


def lock_user(connection: Connection, user: UUID) -> None:
    """Serialise the confirmations of one user (the rule of one main orchestrator holds under concurrency)."""
    connection.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (PAIRING_LOCK, str(user)))


def confirm(
    connection: Connection, user: UUID, key_id: str, body: Confirmation, now: datetime, *, by_agent: bool = False
) -> Confirmed:
    """Confirm an open pairing for `user` by the browser session of the pairing-tagged login (the passkey gesture),
    or - `by_agent` - by the active agent of an account without a passkey (KOR-62: nobody's approval to ask; refused
    with `403 forbidden` once the account has a passkey, concept idea 34). The user's previous confirmed or active
    agent is revoked (`replaced`) and named in `replaces`. A repeat with identical values is idempotent (`changed`
    False, nothing replaced). A remote pairing needs `handover_ct` (`400 handover_required`), a loopback pairing
    refuses it (`400 handover_unexpected`). The new form (`own`) is checked by `check_own`; the old form (`bundle_ct`)
    only from the agent of an account without a passkey (a browser gets `409 client_outdated`). The log batches of
    the new form are the caller's (`agents.identity`)."""
    if by_agent:
        lock_user(connection, user)
        if has_passkey(connection, user):
            raise HubError("forbidden")
    else:
        gesture_session(connection, key_id, body.pairing_id, now)
        if body.bundle_ct is not None:
            # a browser confirms after a passkey login: the old form would hand the identity of a person to the
            # agent (ADR-082 section 9.10); the old tab reloads
            raise HubError("client_outdated")
        lock_user(connection, user)
    row = connection.execute(
        "SELECT state, user_id, expires_at, bundle_ct, label_ct, claim_hash, remote, handover_ct, gen, cert, user_ct,"
        " kem_pub, sig_pub FROM hub.agent WHERE id = %s FOR UPDATE",
        (body.pairing_id,),
    ).fetchone()
    if row is None or row[2] <= now:
        raise HubError("pairing_unknown")
    state, owner, _expires, bundle_ct, label_ct, stored_hash, remote, handover_ct = row[:8]
    pub = (bytes(row[11]), bytes(row[12]))
    repeat = state == "confirmed" and owner == user
    if state != "open" and not repeat:
        raise HubError("pairing_unknown")
    if remote and body.handover_ct is None:
        raise HubError("handover_required")
    if not remote and body.handover_ct is not None:
        raise HubError("handover_unexpected")
    if repeat:
        stored = (
            _raw(bundle_ct),
            bytes(label_ct),
            bytes(stored_hash),
            _raw(handover_ct),
            None if row[8] is None else OwnIdentity(row[8], bytes(row[9]), bytes(row[10])),
        )
        if stored == (body.bundle_ct, body.label_ct, body.claim_hash, body.handover_ct, body.own):
            unchanged()
            return Confirmed(changed=False, remote=remote, pub=pub)
        raise HubError("id_conflict")
    if body.own is not None:
        check_own(connection, user, body.pairing_id, pub, body.own)
    previous = [
        agent_id
        for (agent_id,) in connection.execute(
            "SELECT id FROM hub.agent WHERE user_id = %s AND state IN ('confirmed', 'active') ORDER BY id FOR UPDATE",
            (user,),
        ).fetchall()
    ]
    for agent_id in previous:
        revoke(connection, user, agent_id, now, reason="replaced")
    own = body.own
    # a loopback agent claims at once; a remote agent gets the longer claim deadline from the confirmation on
    deadline = "%s" if remote else "LEAST(expires_at, %s)"
    connection.execute(
        "UPDATE hub.agent SET state = 'confirmed', user_id = %s, bundle_ct = %s, label_ct = %s, claim_hash = %s,"
        " handover_ct = %s, confirmed_at = %s, source = NULL, source_net = NULL, replaces = %s,"
        " own_identity = %s, held_identity = %s, gen = %s, cert = %s, user_ct = %s,"
        f" expires_at = {deadline} WHERE id = %s",
        (
            user,
            body.bundle_ct,
            body.label_ct,
            body.claim_hash,
            body.handover_ct,
            now,
            previous[-1] if previous else None,
            own is not None,
            own is None,
            None if own is None else own.gen,
            None if own is None else own.cert,
            None if own is None else own.user_ct,
            now + timedelta(seconds=REMOTE_CLAIM_SECONDS if remote else CLAIM_SECONDS),
            body.pairing_id,
        ),
    )
    _audit(connection, "agent.confirm", user, body.pairing_id, now, remote=remote)
    return Confirmed(changed=True, replaced=tuple(previous), remote=remote, pub=pub)


def _raw(value: object) -> bytes | None:
    return None if value is None else bytes(cast(bytes, value))


def check_own(connection: Connection, user: UUID, agent_id: UUID, pub: tuple[bytes, bytes], own: OwnIdentity) -> None:
    """The generation is the next one of the user (`409 agent_gen_mismatch` with it in `detail.expected`) and the
    certificate verifies under the user's registered identity for exactly this agent, keys and generation
    (`401 signature_invalid`). The caller holds the user's lock."""
    expected = next_gen(connection, user)
    if own.gen != expected:
        raise HubError("agent_gen_mismatch", detail={"expected": expected})
    if not cert_valid(connection, user, agent_id, pub, gen=own.gen, cert=own.cert):
        raise HubError("signature_invalid")


def cert_valid(
    connection: Connection, user: UUID, agent_id: UUID, pub: tuple[bytes, bytes], *, gen: int, cert: bytes
) -> bool:
    """Whether `cert` is the user's certificate (under the registered identity) of the agent with these keys and
    generation (ADR-082 sections 1 and 9.1)."""
    row = connection.execute("SELECT sig_pub FROM hub.user_identity WHERE user_id = %s", (user,)).fetchone()
    if row is None:
        return False
    try:
        agent_format.verify_cert(
            bytes(row[0]),
            b64u_encode(cert),
            agent_id=str(agent_id),
            pub=_public_json(pub),
            user_id=str(user),
            gen=gen,
        )
    except CryptoError:
        return False
    return True


def agent_author_key(connection: Connection, user: UUID, agent_id: UUID) -> bytes:
    """The Ed25519 key of the user's active own-identity agent whose certificate verifies under the user's registered
    identity (`403 forbidden` otherwise)."""
    row = connection.execute(
        "SELECT kem_pub, sig_pub, gen, cert FROM hub.agent WHERE id = %s AND user_id = %s AND state = 'active'"
        " AND own_identity",
        (agent_id, user),
    ).fetchone()
    if row is None:
        raise HubError("forbidden")
    pub = (bytes(row[0]), bytes(row[1]))
    if not cert_valid(connection, user, agent_id, pub, gen=int(row[2]), cert=bytes(row[3])):
        raise HubError("forbidden")
    return pub[1]


def agent_keys(connection: Connection, agent_id: UUID) -> tuple[bytes, bytes] | None:
    """`(kem_pub, sig_pub)` of an active own-identity agent whose certificate verifies under its user's registered
    identity, else None (the community accepts an agent only while it is its user's active agent, ADR-082 §9.12)."""
    row = connection.execute(
        "SELECT user_id FROM hub.agent WHERE id = %s AND state = 'active' AND own_identity", (agent_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        sig = agent_author_key(connection, row[0], agent_id)
    except HubError:
        return None
    kem = connection.execute("SELECT kem_pub FROM hub.agent WHERE id = %s", (agent_id,)).fetchone()
    return bytes(cast(tuple[bytes], kem)[0]), sig


def agent_authors(connection: Connection, agent_ids: list[UUID]) -> dict[str, dict[str, Any]]:
    """The public description of own-identity agents (also revoked ones, whose messages stay readable): the agent's
    keys, generation and certificate and its user with the user's signing key (readers check the certificate and
    show "Agent of <user>", ADR-082 section 1)."""
    rows = connection.execute(
        "SELECT a.id, a.user_id, a.kem_pub, a.sig_pub, a.gen, a.cert, i.sig_pub FROM hub.agent a"
        " JOIN hub.user_identity i ON i.user_id = a.user_id WHERE a.id = ANY(%s::uuid[]) AND a.own_identity",
        (agent_ids,),
    ).fetchall()
    return {
        str(agent_id): {
            "sig_pub": b64u_encode(bytes(sig_pub)),
            "moderator": False,
            "agent": {
                "user": str(user),
                "user_sig_pub": b64u_encode(bytes(user_sig)),
                "kem_pub": b64u_encode(bytes(kem_pub)),
                "gen": gen,
                "cert": b64u_encode(bytes(cert)),
            },
        }
        for agent_id, user, kem_pub, sig_pub, gen, cert, user_sig in rows
    }


def acting_user(connection: Connection, principal: UUID) -> UUID:
    """The user a principal acts for: the user of an agent (ADR-082), else the principal itself."""
    found = connection.execute("SELECT user_id FROM hub.agent WHERE id = %s", (principal,)).fetchone()
    return principal if found is None or found[0] is None else cast(UUID, found[0])


def _public_json(pub: tuple[bytes, bytes]) -> dict[str, str]:
    return {"kem": b64u_encode(pub[0]), "sig": b64u_encode(pub[1])}


def convert(connection: Connection, agent_id: UUID, user: UUID, own: OwnIdentity, now: datetime) -> Confirmed:
    """Convert the user's active agent that still holds the user's identity into an own-identity agent (ADR-082
    section 5): `check_own`, then `own_identity`, `gen`, `cert`, `user_ct` are set and the sealed identity is deleted
    (`held_identity` stays set); audit `agent.identity`. Only for an account with a passkey (`403 forbidden`: the
    registering agent of an account alone holds the identity by design, ADR-082 section 7). A repeat with the stored
    values is answered unchanged (`changed` False), other values are `409 id_conflict`; a row revoked meanwhile is
    `401 key_revoked`. The answer carries the agent's keys; the log batches are the caller's (`agents.identity`)."""
    lock_user(connection, user)
    row = connection.execute(
        "SELECT state, own_identity, gen, cert, user_ct, kem_pub, sig_pub FROM hub.agent WHERE id = %s FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None or row[0] != "active":
        raise HubError("key_revoked")
    pub = (bytes(row[5]), bytes(row[6]))
    if row[1]:
        if (row[2], bytes(row[3]), _raw(row[4])) == (own.gen, own.cert, own.user_ct):
            unchanged()
            return Confirmed(changed=False, pub=pub)
        raise HubError("id_conflict")
    if not has_passkey(connection, user):
        raise HubError("forbidden")
    check_own(connection, user, agent_id, pub, own)
    connection.execute(
        "UPDATE hub.agent SET own_identity = true, gen = %s, cert = %s, user_ct = %s, bundle_ct = NULL WHERE id = %s",
        (own.gen, own.cert, own.user_ct, agent_id),
    )
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action="agent.identity",
            result="ok",
            target_kind="agent",
            target_id=str(agent_id),
            detail={"agent": str(agent_id), "gen": own.gen},
        ),
    )
    return Confirmed(changed=True, pub=pub)


@dataclass(frozen=True)
class Installed:
    """An agent the hub activates at once, without a pairing (an account of an agent alone, KOR-62): its keys and
    the identity and label it sealed itself under a handover secret it drew itself (`claim_hash` from that secret)."""

    agent_id: UUID
    kem_pub: bytes
    sig_pub: bytes
    bundle_ct: bytes
    label_ct: bytes
    claim_hash: bytes


def install(connection: Connection, user: UUID, agent: Installed, now: datetime, *, replace: bool) -> tuple[UUID, ...]:
    """Store `agent` as the user's active main orchestrator. With `replace` the user's previous confirmed or active
    agents are revoked first (named in `replaces`); the agents revoked. A taken agent ID or key is `409 id_conflict`.
    The caller has verified the agent's proof of possession and holds the user's lock (`lock_user`)."""
    previous: list[UUID] = []
    if replace:
        previous = [
            agent_id
            for (agent_id,) in connection.execute(
                "SELECT id FROM hub.agent WHERE user_id = %s AND state IN ('confirmed', 'active') ORDER BY id"
                " FOR UPDATE",
                (user,),
            ).fetchall()
        ]
        for agent_id in previous:
            revoke(connection, user, agent_id, now, reason="replaced")
    try:
        with connection.transaction():
            connection.execute(
                "INSERT INTO hub.agent (id, user_id, kem_pub, sig_pub, state, bundle_ct, label_ct, claim_hash,"
                " created_at, expires_at, confirmed_at, claimed_at, last_used_at, replaces)"
                " VALUES (%s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    agent.agent_id,
                    user,
                    agent.kem_pub,
                    agent.sig_pub,
                    agent.bundle_ct,
                    agent.label_ct,
                    agent.claim_hash,
                    now,
                    now + timedelta(seconds=PAIRING_SECONDS),
                    now,
                    now,
                    now,
                    previous[-1] if previous else None,
                ),
            )
    except psycopg.errors.UniqueViolation:
        # the agent ID, or the keys of an agent that is not revoked (`agent_sig_pub`)
        raise HubError("id_conflict") from None
    return tuple(previous)


def _agent_json(connection: Connection, row: tuple[Any, ...]) -> dict[str, Any]:
    agent_id, user_id, bundle_ct, label_ct, created_at, claimed_at, user_ct, gen = row
    answer: dict[str, Any] = {"agent_id": str(agent_id), "user_id": str(user_id)}
    if user_ct is not None:
        # an own-identity agent gets its anchor, never an identity (ADR-082 section 2)
        answer.update(user_ct=b64u_encode(bytes(user_ct)), identity="own", gen=gen, migrate=False)
    else:
        migrate = has_passkey(connection, user_id)
        answer.update(bundle_ct=b64u_encode(bytes(bundle_ct)), identity="user", migrate=migrate)
        if migrate:
            # the generation the conversion signs (ADR-082 sections 5 and 9.1)
            answer["migrate_gen"] = next_gen(connection, user_id)
    answer.update(
        label_ct=b64u_encode(bytes(label_ct)), created_at=format_time(created_at), claimed_at=format_time(claimed_at)
    )
    return answer


def claim(connection: Connection, agent_id: UUID, claim_raw: bytes, now: datetime) -> dict[str, Any] | None:
    """Activate a confirmed agent that proves the claim; a repeat by an active agent with the right claim returns the
    same answer. A wrong claim is counted (the fifth deletes an unclaimed pairing, or revokes an own-identity one,
    `unclaimed`) and answered None: the caller commits the count and refuses with `401 claim_invalid`."""
    row = connection.execute(
        "SELECT state, user_id, expires_at, claim_hash, claim_misses, remote, own_identity FROM hub.agent"
        " WHERE id = %s FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None or row[0] not in ("confirmed", "active") or (row[0] == "confirmed" and row[2] <= now):
        raise HubError("pairing_unknown")
    state, user, _expires, stored_hash, misses, remote, own = row
    if not hmac.compare_digest(agent_format.claim_hash(claim_raw), bytes(stored_hash)):
        if misses + 1 >= MAX_CLAIM_MISSES and state == "confirmed" and own:
            # the agent is in its user's logs: revoked, so that its generation stays counted
            revoke(connection, user, agent_id, now, reason="unclaimed")
        elif misses + 1 >= MAX_CLAIM_MISSES and state == "confirmed":
            connection.execute("DELETE FROM hub.agent WHERE id = %s", (agent_id,))
        else:
            connection.execute("UPDATE hub.agent SET claim_misses = claim_misses + 1 WHERE id = %s", (agent_id,))
        return None
    if state == "confirmed":
        connection.execute(
            "UPDATE hub.agent SET state = 'active', claimed_at = %s, last_used_at = %s, handover_ct = NULL"
            " WHERE id = %s",
            (now, now, agent_id),
        )
        _audit(connection, "agent.claim", user, agent_id, now, remote=remote)
    else:
        unchanged()  # the repeat of an active agent's claim
    return me(connection, agent_id)


def handover(connection: Connection, agent_id: UUID, now: datetime) -> dict[str, Any]:
    """The answer of `GET /v1/agent/handover`: the sealed handover of a confirmed remote pairing before its claim
    deadline; `404 pairing_unknown` for anything else (a loopback pairing, claimed, revoked or expired)."""
    row = connection.execute(
        "SELECT user_id, handover_ct FROM hub.agent WHERE id = %s AND state = 'confirmed' AND remote"
        " AND handover_ct IS NOT NULL AND expires_at > %s",
        (agent_id, now),
    ).fetchone()
    if row is None:
        raise HubError("pairing_unknown")
    return {"agent_id": str(agent_id), "user_id": str(row[0]), "handover_ct": b64u_encode(bytes(row[1]))}


def me(connection: Connection, agent_id: UUID) -> dict[str, Any]:
    """The answer of `GET /v1/agent/me` (and of a successful claim): an own-identity agent gets `user_ct`,
    `identity: "own"` and its `gen`; an agent holding the user's identity `bundle_ct`, `identity: "user"` and
    `migrate` (it converts itself, ADR-082 section 5) - true when the user has a passkey, then with `migrate_gen`."""
    row = connection.execute(
        "SELECT id, user_id, bundle_ct, label_ct, created_at, claimed_at, user_ct, gen FROM hub.agent"
        " WHERE id = %s AND state = 'active'",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise HubError("key_revoked")
    return _agent_json(connection, row)


def _time(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


def list_agents(connection: Connection, user: UUID, now: datetime) -> list[dict[str, Any]]:
    """The user's agents, newest first; an unclaimed one past its deadline is `expired`; `remote` for one paired from
    another computer (the web client's banner, ADR-067 section 4.4); `own_identity`, `held_identity` (it held a copy
    of the user's identity: the honest revocation dialog, ADR-082 section 4), `pub`, `gen` and `cert` (the user's
    clients add the active agent to a log that does not name it yet, ADR-082 section 4), `revoked_reason` and
    `session_id` (the orchestrator session of stage b, null until then)."""
    rows = connection.execute(
        "SELECT id, state, label_ct, created_at, confirmed_at, claimed_at, last_used_at, revoked_at, expires_at,"
        " replaces, remote, own_identity, held_identity, gen, revoked_reason, kem_pub, sig_pub, cert, session_id"
        " FROM hub.agent WHERE user_id = %s"
        " ORDER BY created_at DESC, id DESC",
        (user,),
    ).fetchall()
    agents = []
    for row in rows:
        agent_id, state, label_ct, created, confirmed, claimed, used, revoked, expires, replaces, remote = row[:11]
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
                "remote": remote,
                "own_identity": row[11],
                "held_identity": row[12],
                "gen": row[13],
                "revoked_reason": row[14],
                "pub": _public_json((bytes(row[15]), bytes(row[16]))),
                "cert": None if row[17] is None else b64u_encode(bytes(row[17])),
                "session_id": None if row[18] is None else str(row[18]),
            }
        )
    return agents


REVOKED_REASONS = frozenset({"revoked", "replaced", "unclaimed", "session_ended"})


def revoke(connection: Connection, user: UUID, agent_id: UUID, now: datetime, *, reason: str = "revoked") -> bool:
    """Revoke the user's agent at once (`404 not_found` for an unknown or foreign one) for `reason`
    (`REVOKED_REASONS`); False when it was revoked already (no change). The sealed identity or anchor is deleted."""
    row = connection.execute(
        "SELECT state FROM hub.agent WHERE id = %s AND user_id = %s FOR UPDATE", (agent_id, user)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    if row[0] == "revoked":
        unchanged()
        return False
    if reason not in REVOKED_REASONS:
        raise ValueError("unknown revocation reason")
    connection.execute(
        "UPDATE hub.agent SET state = 'revoked', revoked_at = %s, revoked_reason = %s, bundle_ct = NULL,"
        " user_ct = NULL, handover_ct = NULL WHERE id = %s",
        (now, reason, agent_id),
    )
    _audit(connection, "agent.revoke", user, agent_id, now, remote=False)
    return True


def revoke_all(connection: Connection, user: UUID, now: datetime) -> list[UUID]:
    """Revoke every confirmed or active agent of the user at once; the IDs revoked."""
    rows = connection.execute(
        "SELECT id FROM hub.agent WHERE user_id = %s AND state IN ('confirmed', 'active') ORDER BY id FOR UPDATE",
        (user,),
    ).fetchall()
    if not rows:
        unchanged()
    for (agent_id,) in rows:
        revoke(connection, user, agent_id, now)
    return [agent_id for (agent_id,) in rows]


def clear_expired_handovers(connection: Connection, now: datetime) -> int:
    """Clear the sealed handover of remote pairings past their claim deadline; the number cleared."""
    cursor = connection.execute(
        "UPDATE hub.agent SET handover_ct = NULL WHERE handover_ct IS NOT NULL AND expires_at <= %s", (now,)
    )
    return cursor.rowcount


def purge_unclaimed(connection: Connection, now: datetime) -> int:
    """Revoke (`unclaimed`) own-identity rows past their claim deadline: the agent is in its user's logs and its
    generation stays counted; delete the other open and confirmed rows one hour after their claim deadline. The number
    of rows revoked or deleted."""
    expired = connection.execute(
        "SELECT id, user_id FROM hub.agent WHERE state = 'confirmed' AND own_identity AND expires_at <= %s"
        " ORDER BY id FOR UPDATE",
        (now,),
    ).fetchall()
    for agent_id, user in expired:
        revoke(connection, user, agent_id, now, reason="unclaimed")
    cursor = connection.execute(
        "DELETE FROM hub.agent WHERE state IN ('open', 'confirmed') AND NOT own_identity AND expires_at < %s",
        (now - timedelta(seconds=UNCLAIMED_RETENTION_SECONDS),),
    )
    return len(expired) + cursor.rowcount
