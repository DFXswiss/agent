"""The main orchestrator on platform hardware (ADR-082 sections 6 and 9.6-9.9, 11-agent-access.md section 11).

A running main session of the user becomes the user's main orchestrator. The flow at the hub:

1. **Request** (`request`, `POST /v1/sessions/{id}/orchestrator`): only the browser session of the task owner (the
   route refuses an agent or a device with `403 forbidden`; another reader of the session gets `403 forbidden`, a
   session the user cannot read `404 not_found`) for a session that `allowed` admits, else
   `409 orchestrator_unavailable` with `detail.reason`. The hub records the time on the session
   (`session.orchestrator_at`, audit `session.orchestrator`) and hints the session's device (`session.orchestrator`).
2. **Keys** (`register`, `POST /v1/device/orchestrator-pairings`): the runner of the session registers the keys the
   session drew in its VM as an ordinary open pairing (`hub.agent`) bound to the session, its device and - through
   the session - its task owner, with `device_sig`, the device's signature over the fingerprint
   (`orchestrator_key_text`), which the hub checks under the device's registered key. A new registration replaces an
   open pairing of the same session; a confirmed or active one is `409 id_conflict`.
3. **Approval**: the browser shows the pairing (`view`, `GET /v1/sessions/{id}/orchestrator`), checks `device_sig`
   and the keys against `fp`, runs the login ceremony tagged with the pairing and confirms with `POST /v1/me/agents`
   exactly like every pairing (`store.confirm` calls `check_confirm`: only the task owner, only while the session is
   still allowed; the replacement of the previous main orchestrator of either form is the same).
4. **Handover** (`store_handover`, `POST /v1/agent-pairings/{id}/orchestrator-handover`): the browser stores the
   handover sealed to the session's key with the owner's signature over it (`orchestrator_handover_text`), checked
   here under the owner's registered identity; the device is hinted and fetches it (`device_view`,
   `GET /v1/device/sessions/{id}/orchestrator`); the runner checks the signature under the owner's key from its
   verified log before it passes the handover into the VM. The tool in the VM claims as a remote agent does.
5. **End** (`end_session`, called by every session transition into `stopped`, `failed` or `suspended` - a suspended
   VM holds no keys -, in its transaction): every confirmed or active orchestrator of the session is revoked
   (`session_ended`), an open pairing of it is deleted. `revoke_unfit` does the same when the session stops fitting
   while it runs: another user joins the private organization (`orgs.append`) or the device is decertified
   (`platform.log`).

`allowed` (ADR-082 section 9.6): a main session (`not_main`) of a task in the owner's private organization - private,
created by the owner and with no other active user member (`not_private`), of a task that is no analysis
(`analysis`, ADR-097 section 2: the orchestrator would hold the user's full agent access) - on an active platform device
(`not_platform`), in a state in which its VM runs, `working` or `idle` (`not_running`). Registering keys also needs
a request the session has not answered yet (`not_requested`); the same keys registered again after their pairing
expired unconfirmed open it anew, a pairing confirmed but not claimed by its deadline no longer holds the session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg
import psycopg.errors

from agent_hub.agents import store
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.orgs import rights
from agent_hub.stream.publish import hint as publish_hint

Connection = psycopg.Connection[Any]

RUNNING = frozenset({"working", "idle"})
"""Session states in which the session's VM runs and can draw the keys."""
UNAVAILABLE = "orchestrator_unavailable"
REASONS = ("not_main", "analysis", "not_private", "not_platform", "not_running", "not_requested")
HINT = "session.orchestrator"
_FACTS = (
    "SELECT s.id, s.owner_id, s.org_id, s.wg_id, s.task_id, s.device_id, s.depth, s.state, s.orchestrator_at,"
    " o.private AND o.created_by = s.owner_id AND NOT EXISTS (SELECT 1 FROM hub.org_member m"
    " WHERE m.org_id = s.org_id AND m.kind = 'user' AND m.removed_seq IS NULL AND m.principal_id <> s.owner_id),"
    " COALESCE(d.kind = 'platform' AND d.state = 'active', false),"
    " EXISTS (SELECT 1 FROM hub.task t WHERE t.id = s.task_id AND t.analysis IS NOT NULL)"
    " FROM hub.session s JOIN hub.org o ON o.id = s.org_id LEFT JOIN hub.device d ON d.id = s.device_id"
    " WHERE s.id = %s"
)
_PAIRING = (
    "SELECT id, kem_pub, sig_pub, state, expires_at, device_sig, handover_ct, handover_sig, created_at FROM hub.agent"
    " WHERE session_id = %s ORDER BY created_at DESC, id DESC LIMIT 1"
)


@dataclass(frozen=True)
class SessionFacts:
    """What decides whether a session may become the main orchestrator."""

    id: UUID
    owner: UUID
    org: UUID
    wg: UUID
    task: UUID
    device: UUID | None
    depth: int
    state: str
    requested_at: datetime | None
    private: bool
    platform: bool
    analysis: bool = False
    """The session belongs to an analysis task (ADR-097 section 2): it never becomes the main orchestrator."""


def facts(connection: Connection, session_id: UUID, *, lock: bool = False) -> SessionFacts:
    """The facts of the session (`404 not_found` for an unknown one); `lock` holds the session `FOR SHARE`."""
    row = connection.execute(_FACTS + (" FOR SHARE OF s" if lock else ""), (session_id,)).fetchone()
    if row is None:
        raise HubError("not_found")
    return SessionFacts(*row)


def reason(found: SessionFacts) -> str | None:
    """Why the session cannot become the main orchestrator (module docstring), None when it can."""
    if found.depth != 0:
        return "not_main"
    if found.analysis:
        return "analysis"
    if not found.private:
        return "not_private"
    if not found.platform:
        return "not_platform"
    if found.state not in RUNNING:
        return "not_running"
    return None


def _refuse(why: str) -> HubError:
    return HubError(UNAVAILABLE, detail={"reason": why})


def allowed(found: SessionFacts) -> None:
    """`409 orchestrator_unavailable` with `detail.reason` unless the session may become the main orchestrator."""
    why = reason(found)
    if why is not None:
        raise _refuse(why)


def _owned(connection: Connection, user: UUID, session_id: UUID, *, person: bool) -> SessionFacts:
    """The facts of a session the user reads (`404 not_found` otherwise) and owns, asked by the user's browser session
    (`403 forbidden` otherwise: the task owner's agent is no person)."""
    found = facts(connection, session_id)
    rights.require(connection, user, rights.TASK_READ, wg_id=found.wg)
    if found.owner != user or not person:
        raise HubError("forbidden")
    return found


def _time(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


def _newest(connection: Connection, session_id: UUID) -> tuple[Any, ...] | None:
    """The newest pairing of the session in any state (None for none)."""
    row = connection.execute(_PAIRING, (session_id,)).fetchone()
    return None if row is None else tuple(row)


def _live(row: tuple[Any, ...] | None, now: datetime) -> tuple[Any, ...] | None:
    """The pairing while it is open, confirmed (both before their deadline) or active, else None."""
    if row is None or store.public_state(row[3], row[4], now) == "closed":
        return None
    return row


def _requested(found: SessionFacts, newest: tuple[Any, ...] | None) -> bool:
    """A request the runner still has to answer with keys: requested after the newest pairing of the session was
    registered, and the session still allowed."""
    return (
        found.requested_at is not None and (newest is None or newest[8] < found.requested_at) and reason(found) is None
    )


def view(connection: Connection, user: UUID, session_id: UUID, now: datetime, *, person: bool) -> dict[str, Any]:
    """The answer of `GET /v1/sessions/{id}/orchestrator` for the task owner: the session's device, whether it may
    become the main orchestrator (`available`, `reason`), the time of the request and the current pairing with the
    device's signature over its fingerprint."""
    found = _owned(connection, user, session_id, person=person)
    return _view(connection, found, now)


def _view(connection: Connection, found: SessionFacts, now: datetime) -> dict[str, Any]:
    why = reason(found)
    row = _live(_newest(connection, found.id), now)
    pairing = None
    if row is not None:
        kem, sig = bytes(row[1]), bytes(row[2])
        pairing = {
            "pairing_id": str(row[0]),
            "pub": {"kem": b64u_encode(kem), "sig": b64u_encode(sig)},
            "fp": agent_format.fingerprint(kem, sig),
            "device_sig": b64u_encode(bytes(row[5])),
            "state": store.public_state(row[3], row[4], now),
            "expires_at": format_time(row[4]),
            "handover": row[6] is not None,
        }
    return {
        "session_id": str(found.id),
        "device_id": None if found.device is None else str(found.device),
        "available": why is None,
        "reason": why,
        "requested_at": _time(found.requested_at),
        "pairing": pairing,
    }


def _audit(connection: Connection, action: str, now: datetime, *, actor: tuple[str, UUID], **target: Any) -> None:
    kind, actor_id = actor
    append_audit(
        connection,
        AuditRow(at=now, actor_kind=kind, actor_id=actor_id, action=action, result="ok", **target),
    )


def request(
    connection: Connection, user: UUID, session_id: UUID, now: datetime, *, person: bool
) -> tuple[dict[str, Any], UUID | None]:
    """Record the task owner's request (module docstring, step 1); the answer of `view` and the device to hint. While
    the session has a live pairing (open, confirmed or active) the request is answered unchanged and nobody is hinted:
    the browser continues with that pairing."""
    found = _owned(connection, user, session_id, person=person)
    allowed(found)
    if _live(_newest(connection, session_id), now) is not None:
        unchanged()
        return _view(connection, found, now), None
    connection.execute("UPDATE hub.session SET orchestrator_at = %s WHERE id = %s", (now, session_id))
    _audit(
        connection,
        "session.orchestrator",
        now,
        actor=("user", user),
        target_kind="session",
        target_id=str(session_id),
        org_id=found.org,
        wg_id=found.wg,
        task_id=found.task,
        session_id=session_id,
        device_id=found.device,
    )
    return _view(connection, facts(connection, session_id), now), found.device


@dataclass(frozen=True)
class Registration:
    """The body of `POST /v1/device/orchestrator-pairings`, decoded."""

    session_id: UUID
    pairing_id: UUID
    kem: bytes
    sig: bytes
    pop: str
    fp: str
    device_sig: str


def _device_key(connection: Connection, device: UUID) -> bytes:
    """The registered signing key of the requesting device (it exists: the request was signed with it)."""
    row = connection.execute("SELECT sig_pub FROM hub.device WHERE id = %s", (device,)).fetchone()
    return bytes(cast(tuple[bytes], row)[0])


def _on_device(connection: Connection, device: UUID, session_id: UUID, *, lock: bool = False) -> SessionFacts:
    """The facts of a session that runs on the device (`404 not_found` unknown, `403 forbidden` another device)."""
    found = facts(connection, session_id, lock=lock)
    if found.device != device:
        raise HubError("forbidden")
    return found


def register(connection: Connection, device: UUID, body: Registration, now: datetime) -> dict[str, Any]:
    """Store the session's keys as an open pairing (module docstring, step 2); `{pairing_id, expires_at}`."""
    found = _on_device(connection, device, body.session_id, lock=True)
    allowed(found)
    try:
        agent_format.verify_pop(str(body.pairing_id), body.kem, body.sig, body.pop)
        if agent_format.fingerprint(body.kem, body.sig) != body.fp:
            raise HubError("request_invalid", detail={"pointers": ["/fp"]})
        signed = agent_format.orchestrator_key_text(body.fp, str(body.pairing_id), str(body.session_id))
        agent_format.verify_text(_device_key(connection, device), body.device_sig, signed)
    except CryptoError:
        raise HubError("signature_invalid") from None
    device_sig = b64u_decode(body.device_sig)
    store.lock_user(connection, found.owner)
    stored = connection.execute(
        "SELECT kem_pub, sig_pub, session_id, device_sig, expires_at, state FROM hub.agent WHERE id = %s FOR UPDATE",
        (body.pairing_id,),
    ).fetchone()
    if stored is not None:
        same = (bytes(stored[0]), bytes(stored[1]), stored[2], bytes(stored[3] or b"")) == (
            body.kem,
            body.sig,
            body.session_id,
            device_sig,
        )
        if not same:
            raise HubError("id_conflict")
        if store.public_state(stored[5], stored[4], now) != "closed":
            unchanged()
            return {"pairing_id": str(body.pairing_id), "expires_at": format_time(stored[4])}
        if stored[5] != "open":
            # confirmed and past its claim deadline, or revoked: these keys are used up, the VM draws new ones
            raise HubError("id_conflict")
    # keys only for a request the session has not answered yet (ADR-082 section 9.9: a new orchestrator needs a new
    # gesture of the person)
    if not _requested(found, _newest(connection, body.session_id)):
        raise _refuse("not_requested")
    expires_at = now + timedelta(seconds=store.PAIRING_SECONDS)
    if stored is not None:
        # the same keys again after their pairing expired unconfirmed: the pairing opens anew
        connection.execute(
            "UPDATE hub.agent SET created_at = %s, expires_at = %s WHERE id = %s", (now, expires_at, body.pairing_id)
        )
    else:
        _insert(connection, device, body, device_sig, now=now, expires_at=expires_at)
    _audit(
        connection,
        "agent.pairing",
        now,
        actor=("device", device),
        target_kind="agent",
        target_id=str(body.pairing_id),
        org_id=found.org,
        wg_id=found.wg,
        task_id=found.task,
        session_id=body.session_id,
        device_id=device,
    )
    return {"pairing_id": str(body.pairing_id), "expires_at": format_time(expires_at)}


def _insert(
    connection: Connection, device: UUID, body: Registration, device_sig: bytes, *, now: datetime, expires_at: datetime
) -> None:
    """A new open pairing of the session (module docstring, step 2), unless the session is taken."""
    taken = connection.execute(
        "SELECT state FROM hub.agent WHERE session_id = %s AND (state = 'active' OR (state = 'confirmed'"
        " AND expires_at > %s)) LIMIT 1",
        (body.session_id, now),
    ).fetchone()
    if taken is not None:
        raise HubError("id_conflict")
    # a new registration replaces an open pairing of the session (the VM drew new keys)
    connection.execute("DELETE FROM hub.agent WHERE session_id = %s AND state = 'open'", (body.session_id,))
    try:
        with connection.transaction():
            connection.execute(
                "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, created_at, expires_at, session_id, device_id,"
                " device_sig) VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s)",
                (body.pairing_id, body.kem, body.sig, now, expires_at, body.session_id, device, device_sig),
            )
    except psycopg.errors.UniqueViolation:
        # the keys of an agent that is not revoked (`agent_sig_pub`)
        raise HubError("id_conflict") from None


def device_view(connection: Connection, device: UUID, session_id: UUID, now: datetime) -> dict[str, Any]:
    """The answer of `GET /v1/device/sessions/{id}/orchestrator` for the session's device: the task owner, whether a
    request waits for keys (`requested`: requested after the newest pairing of the session, the session allowed) and
    the current pairing with the stored handover `{ct, sig}` (null until the browser stored it)."""
    found = _on_device(connection, device, session_id)
    newest = _newest(connection, session_id)
    row = _live(newest, now)
    pairing = None
    if row is not None:
        handover = None
        if row[6] is not None:
            handover = {"ct": b64u_encode(bytes(row[6])), "sig": b64u_encode(bytes(row[7]))}
        pairing = {
            "pairing_id": str(row[0]),
            "state": store.public_state(row[3], row[4], now),
            "expires_at": format_time(row[4]),
            "handover": handover,
        }
    return {
        "session_id": str(session_id),
        "user_id": str(found.owner),
        "requested_at": _time(found.requested_at),
        "requested": _requested(found, newest),
        "pairing": pairing,
    }


def check_confirm(connection: Connection, user: UUID, session_id: UUID) -> None:
    """The confirmation of a session pairing: only by the task owner (`403 forbidden`), only while the session may
    become the main orchestrator (`409 orchestrator_unavailable`)."""
    found = facts(connection, session_id)
    if found.owner != user:
        raise HubError("forbidden")
    allowed(found)


def _user_key(connection: Connection, user: UUID) -> bytes:
    row = connection.execute("SELECT sig_pub FROM hub.user_identity WHERE user_id = %s", (user,)).fetchone()
    if row is None:
        raise HubError("signature_invalid")
    return bytes(row[0])


def store_handover(
    connection: Connection, user: UUID, pairing_id: UUID, *, ct: bytes, sig: str, now: datetime, person: bool
) -> tuple[dict[str, Any], UUID | None]:
    """Store the handover of a confirmed session pairing of the user (module docstring, step 4): `404
    pairing_unknown` for anything but a confirmed session pairing of the user before its claim deadline,
    `409 orchestrator_unavailable` once the session may no longer be the orchestrator, `401 signature_invalid` unless
    `sig` is the user's signature over `orchestrator_handover_text`; a repeat with the same values is unchanged, other
    values are `409 id_conflict`. The answer and the device to hint (None for a repeat)."""
    row = connection.execute(
        "SELECT session_id, handover_ct, handover_sig FROM hub.agent WHERE id = %s AND user_id = %s"
        " AND state = 'confirmed' AND session_id IS NOT NULL AND expires_at > %s FOR UPDATE",
        (pairing_id, user, now),
    ).fetchone()
    if row is None:
        raise HubError("pairing_unknown")
    if not person:
        raise HubError("forbidden")
    session_id: UUID = row[0]
    found = facts(connection, session_id)
    allowed(found)
    ct_text = b64u_encode(ct)
    try:
        signed = agent_format.orchestrator_handover_text(ct_text, str(pairing_id), str(session_id))
        agent_format.verify_text(_user_key(connection, user), sig, signed)
    except CryptoError:
        raise HubError("signature_invalid") from None
    answer = {"pairing_id": str(pairing_id), "session_id": str(session_id)}
    sig_raw = b64u_decode(sig)
    if row[1] is not None:
        if (bytes(row[1]), bytes(row[2])) != (ct, sig_raw):
            raise HubError("id_conflict")
        unchanged()
        return answer, None
    connection.execute(
        "UPDATE hub.agent SET handover_ct = %s, handover_sig = %s WHERE id = %s", (ct, sig_raw, pairing_id)
    )
    _audit(
        connection,
        "agent.handover",
        now,
        actor=("user", user),
        target_kind="agent",
        target_id=str(pairing_id),
        org_id=found.org,
        wg_id=found.wg,
        task_id=found.task,
        session_id=session_id,
        device_id=found.device,
        detail={"agent": str(pairing_id)},
    )
    return answer, found.device


def pending(connection: Connection, device: UUID, now: datetime) -> list[dict[str, Any]]:
    """The orchestrator work of the device's sessions for `GET /v1/device/state` (`orchestrators`, the reconcile of a
    missed hint): every running main session whose request waits for keys (`step` `keys`), whose confirmed pairing has
    a stored handover (`step` `handover`) or whose newest pairing the hub revoked (`step` `revoked`: the guest ends the
    orchestrator and deletes its keys; repeated while the session runs, the runner treats it as done once done),
    oldest session first."""
    sessions = connection.execute(
        "SELECT id FROM hub.session WHERE device_id = %s AND orchestrator_at IS NOT NULL AND depth = 0"
        " AND state IN ('working', 'idle') ORDER BY created_at, id",
        (device,),
    ).fetchall()
    work: list[dict[str, Any]] = []
    for (session_id,) in sessions:
        found = facts(connection, session_id)
        newest = _newest(connection, session_id)
        live = _live(newest, now)
        if _requested(found, newest):
            work.append({"session_id": str(session_id), "step": "keys", "pairing_id": None})
        elif live is not None and live[3] == "confirmed" and live[6] is not None:
            work.append({"session_id": str(session_id), "step": "handover", "pairing_id": str(live[0])})
        elif newest is not None and newest[3] == "revoked":
            # revoked while the session runs (replaced, by the user, on suspend, unfit): the guest ends it
            work.append({"session_id": str(session_id), "step": "revoked", "pairing_id": str(newest[0])})
    return work


def end_session(connection: Connection, session_id: UUID, now: datetime) -> list[tuple[UUID, UUID]]:
    """At the end of a session (in the transition's transaction): revoke its confirmed or active orchestrator
    (`session_ended`) and delete an open pairing of it; the `(user, agent)` pairs revoked now."""
    rows = connection.execute(
        "SELECT id, state, user_id FROM hub.agent WHERE session_id = %s AND state <> 'revoked' ORDER BY id FOR UPDATE",
        (session_id,),
    ).fetchall()
    revoked = []
    for agent_id, state, user in rows:
        if state == "open":
            connection.execute("DELETE FROM hub.agent WHERE id = %s", (agent_id,))
        else:
            store.revoke(connection, user, agent_id, now, reason="session_ended")
            revoked.append((user, agent_id))
    return revoked


UNFIT = frozenset({"not_private", "not_platform"})
"""Reasons that end a session orchestrator while its session runs on: another user joined the private organization,
or the device stopped being an active platform device (ADR-082 section 9.6)."""


def revoke_unfit(
    connection: Connection, now: datetime, *, org: UUID | None = None, device: UUID | None = None
) -> list[tuple[UUID, UUID]]:
    """Revoke (`session_ended`) every confirmed or active session orchestrator of the organization `org` or on the
    device `device` whose session no longer fits (`UNFIT`), in the caller's transaction (a log append, a
    decertification); the `(user, agent)` pairs revoked now."""
    rows = connection.execute(
        "SELECT a.id, a.user_id, a.session_id FROM hub.agent a JOIN hub.session s ON s.id = a.session_id"
        " WHERE a.state IN ('confirmed', 'active') AND (%s::uuid IS NULL OR s.org_id = %s)"
        " AND (%s::uuid IS NULL OR s.device_id = %s) ORDER BY a.id FOR UPDATE OF a",
        (org, org, device, device),
    ).fetchall()
    revoked = []
    for agent_id, user, session_id in rows:
        if reason(facts(connection, session_id)) in UNFIT:
            store.revoke(connection, user, agent_id, now, reason="session_ended")
            revoked.append((user, agent_id))
    return revoked


def session_hints(connection: Connection, agent_ids: list[UUID]) -> list[tuple[UUID, UUID]]:
    """`(device, session)` of the session orchestrators among `agent_ids` (read in the revoking transaction): their
    devices get the hint `session.orchestrator` after the commit (`publish_hints`)."""
    if not agent_ids:
        return []
    rows = connection.execute(
        "SELECT device_id, session_id FROM hub.agent WHERE id = ANY(%s::uuid[]) AND session_id IS NOT NULL ORDER BY id",
        (agent_ids,),
    ).fetchall()
    return [(UUID(str(device)), UUID(str(session))) for device, session in rows]


def publish_hints(hints: list[tuple[UUID, UUID]]) -> None:
    """The hint `session.orchestrator {session_id}` to each device (after the commit)."""
    for device, session in hints:
        publish_hint(device, HINT, {"session_id": str(session)})
