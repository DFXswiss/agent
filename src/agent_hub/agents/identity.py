"""The agent's own identity at the hub (ADR-082 sections 1, 2, 5 and 9; 11-agent-access.md section 2).

- `actor(principal)`: the cryptographic actor of a request. A request of an own-identity agent is authorised as its
  user (routes, scopes and rights stay the user's, ADR-056 section 4), but what it signs - log entries `by`, events
  `author`, wraps `wrapped_by`, credential records `author`, device requests `header.user` - carries the agent's ID
  and is checked with the agent's key. Every other user request (a browser session, an agent that still signs with
  its user's identity) acts as the user, a device as the device.
- `apply_batches`: the log batches of a confirmation in the new form (`POST /v1/me/agents`) or of a conversion
  (`POST /v1/agent/identity`), in the caller's transaction: one batch per organization in which the user is an active
  member - no more, no fewer (`409 agent_batches_incomplete`, `detail.orgs`: the orgs missing or not expected) - each
  through the ordinary append path (`agent_hub.orgs.append.append_batch`: `check_batch`, W3 with `wrapped_by` = the
  user, storage, audit), each adding exactly this agent with exactly this generation and certificate (`agent_add` by
  the user; an agent of the user with a lower generation is replaced by it, ADR-082 section 9.1), then the
  `cred_wraps` (the user's `CK` wrapped to the agent; the current epoch is required while the user has one and is in
  an organization: else `agent_batches_incomplete` with the epoch in `detail.expected`). The verified logs after the
  batches are handed to the wrap check, so nothing is cached before the commit (E-19).
- `audience_of(connection, recipient)`: the live-channel audience of a wrap recipient (an agent receives on its
  user's channel).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.agents import store
from agent_hub.auth.keys import Principal
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.errors import HubError
from agent_hub.orgs import logs
from agent_hub.orgs.append import AGENT_APPENDER, Appender, AppendResult, append_batch
from agent_hub.stream.publish import Audience
from agent_hub.wraps.rules import cred_epoch, store_wraps

Connection = psycopg.Connection[Any]
MAX_DETAIL_ORGS = 50
CRED = "cred"


def actor(principal: Principal) -> Appender:
    """The cryptographic actor of a request of a user (browser session or agent) or a device (module docstring)."""
    principal_id = str(principal.principal_id)
    if principal.kind == "device":
        return Appender(principal_id, "device")
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    if isinstance(agent, UUID) and principal.attributes.get("own_identity") is True:
        return Appender(str(agent), AGENT_APPENDER, principal_id)
    return Appender(principal_id, "user")


@dataclass(frozen=True)
class Batch:
    """One append batch of a confirmation or conversion."""

    org: UUID
    expected_seq: int
    entries: Sequence[object]
    wraps: Sequence[object]


def _orgs(values: Iterable[UUID]) -> list[str]:
    return [str(value) for value in sorted(values)[:MAX_DETAIL_ORGS]]


def _added(result: AppendResult, first: int, agent_id: UUID, own: store.OwnIdentity, pub: dict[str, str]) -> bool:
    """The batch added exactly this agent with this generation, certificate and keys, and it is active after it."""
    adds = [
        body
        for entry in result.log.entries[first:]
        if (body := cast(dict[str, Any], entry["body"]))["type"] == "agent_add" and body["subject"] == str(agent_id)
    ]
    expected = (own.gen, b64u_encode(own.cert), pub)
    return (
        len(adds) == 1
        and (adds[0]["gen"], adds[0]["cert"], adds[0]["pub"]) == expected
        and result.log.state.member(str(agent_id)) is not None
    )


def _cred_shape(user: UUID, agent_id: UUID, wraps: Sequence[object]) -> None:
    """Every credential-key wrap is one of the user's `CK` to this agent (`400 request_invalid` at the wrap)."""
    for index, wrap in enumerate(wraps):
        data = wrap if isinstance(wrap, dict) else {}
        if (data.get("scope"), data.get("scope_id"), data.get("recipient_id")) != (CRED, str(user), str(agent_id)):
            raise HubError("request_invalid", detail={"pointers": [f"/cred_wraps/{index}"]})


def _require_current_cred(connection: Connection, user: UUID, agent_id: UUID) -> None:
    """The agent holds a wrap of the user's current `CK` epoch, if the user has one (`409 agent_batches_incomplete`
    with that epoch in `detail.expected`)."""
    epoch = cred_epoch(connection, str(user))
    if epoch is None:
        return
    found = connection.execute(
        "SELECT 1 FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND epoch = %s AND recipient_id = %s",
        (user, epoch, agent_id),
    ).fetchone()
    if found is None:
        raise HubError("agent_batches_incomplete", detail={"expected": epoch})


def apply_batches(
    connection: Connection,
    *,
    user: UUID,
    agent_id: UUID,
    own: store.OwnIdentity,
    pub: tuple[bytes, bytes],
    batches: Sequence[Batch],
    cred_wraps: Sequence[object],
    now: datetime,
    root_key: bytes,
) -> list[AppendResult]:
    """Append every batch and store the credential-key wraps (module docstring); the caller commits and publishes."""
    given = [batch.org for batch in batches]
    if len(set(given)) != len(given):
        raise HubError("request_invalid", detail={"pointers": ["/batches"]})
    active = {org.id for org in logs.member_orgs(connection, user)}
    missing, unexpected = active - set(given), set(given) - active
    if missing or unexpected:
        raise HubError("agent_batches_incomplete", detail={"orgs": _orgs(missing | unexpected)})
    appender = Appender(str(user), "user")
    public = {"kem": b64u_encode(pub[0]), "sig": b64u_encode(pub[1])}
    results = []
    for batch in sorted(batches, key=lambda item: item.org):  # org rows are locked in a fixed order
        result = append_batch(
            connection,
            org_id=batch.org,
            appender=appender,
            expected_seq=batch.expected_seq,
            entries=batch.entries,
            wraps=batch.wraps,
            events=[],
            now=now,
            root_key=root_key,
        )
        if not _added(result, batch.expected_seq, agent_id, own, public):
            raise HubError("agent_batches_incomplete", detail={"orgs": [str(batch.org)]})
        results.append(result)
    _cred_shape(user, agent_id, cred_wraps)
    if cred_wraps:
        known = {result.org_id: result.log for result in results}
        store_wraps(connection, cred_wraps, wrapper=str(user), now=now, root_key=root_key, known=known)
    if results:
        # the agent is a credential reader once it is in one of its user's logs (`cred_readers`)
        _require_current_cred(connection, user, agent_id)
    return results


def audience_of(connection: Connection, recipient: str, kind: str | None) -> Audience:
    """The live-channel audience of a wrap recipient whose `hub.principal` kind is `kind` (None: no hub principal):
    a device its own, an agent its user's channel, a user its own."""
    if kind == "device":
        return Audience("device", UUID(recipient))
    if kind is None:
        row = connection.execute("SELECT user_id FROM hub.agent WHERE id = %s", (recipient,)).fetchone()
        if row is not None:
            return Audience("user", cast(UUID, row[0]))
    return Audience("user", UUID(recipient))
