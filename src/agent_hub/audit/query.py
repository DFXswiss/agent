"""The audit view of an org and the chain verification (03-api.md section 4.11, 06-web.md section 9.2).

**List.** `GET /v1/orgs/{org}/audit` returns the audit rows of the org (`org_id` = the org), newest first (`seq DESC`),
at most `limit` (default 50, max 200) with the opaque cursor `next_before` (b64u of the canonical `{"seq": n}`). An
admin sees every row of the org; a member only the rows of workgroups the member reads (`wg_access`) and the member's
own actions (`actor_id` = the member); a non-member gets `404 not_found`. Filters (all optional, combined with AND):
`action` (the action itself or a dotted group, e.g. `operation` matches `operation.accepted`), `actor`, `task`,
`session`, `wg`. Rows are returned as the hash covers them (`agent_hub.audit.chain.row_json`), so a client can
recompute a row's hash with its `prev_hash` taken from the verification.

**Account.** `GET /v1/me/audit` (KOR-24) returns the caller's account audit in the same form and paging: the rows the
user wrote outside any org (`actor_id` = the user and no `org_id`: sign-in, passkeys, recovery, agents) and every row
written by a request of one of the user's agents (`agent_id`), in an org or not.

**Verify.** `GET /v1/orgs/{org}/audit/verify` (admin) recomputes the whole chain (`verify_chain`: every hash and link
from `seq` 1 to the head) and checks the newest checkpoint of the hub key (`audit_checkpoint`, written hourly by
`agent_hub.audit.jobs`): its signature verifies under the hub key and its hash equals the stored hash at its `seq`.
`ok` is true when both hold (or no checkpoint exists yet). `head` is the stored head read before the
verification, which covers exactly the rows up to it (rows appended meanwhile are not part of the answer); the chain
is global, so the head is the same for every org. An empty audit (only possible before the first org exists) has the
head `{seq: 0, hash: "0" * 64}`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, row_json, verify_chain
from agent_hub.audit.checkpoint import AuditCheckpoint, checkpoint_valid, newest_checkpoint
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.orgs.rights import org_role

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
EMPTY_HASH = "0" * 64
FILTERS = ("action", "actor", "task", "session", "wg")
_FILTER_COLUMNS = {"actor": "actor_id", "task": "task_id", "session": "session_id", "wg": "wg_id"}
_ACTION_RE = re.compile(r"[a-z_]+(?:\.[a-z_]+)*")
_COLUMNS = (
    "seq, at, actor_id, actor_kind, action, result, org_id, wg_id, task_id, session_id, device_id, target_kind,"
    " target_id, detail, event_id, agent_id"
)
MAX_ACTION = 96


def encode_before(seq: int) -> str:
    """The opaque cursor of a row."""
    return b64u_encode(canonical({"seq": seq}))


def decode_before(text: str) -> int:
    """The `seq` of a cursor; anything else is `400 request_invalid`."""
    try:
        seq = json.loads(b64u_decode(text))["seq"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or encode_before(seq) != text:
            raise ValueError("not a cursor")
    except (CryptoError, ValueError, TypeError, KeyError):
        raise HubError("request_invalid", detail={"pointers": ["/before"]}) from None
    return seq


def _uuid(text: str, name: str) -> UUID:
    try:
        value = UUID(text)
    except ValueError:
        value = None
    if value is None or str(value) != text:
        raise HubError("request_invalid", detail={"pointers": [f"/{name}"]})
    return value


def parse_filters(params: Mapping[str, str | None]) -> dict[str, Any]:
    """The given filters, checked (`400 request_invalid` with the pointer of a bad one)."""
    found: dict[str, Any] = {}
    for name in FILTERS:
        text = params.get(name)
        if text is None:
            continue
        if name == "action":
            if len(text) > MAX_ACTION or _ACTION_RE.fullmatch(text) is None:
                raise HubError("request_invalid", detail={"pointers": ["/action"]})
            found[name] = text
        else:
            found[name] = _uuid(text, name)
    return found


def _row(record: tuple[Any, ...]) -> dict[str, Any]:
    row = AuditRow(
        at=record[1],
        actor_id=record[2],
        actor_kind=record[3],
        action=record[4],
        result=record[5],
        org_id=record[6],
        wg_id=record[7],
        task_id=record[8],
        session_id=record[9],
        device_id=record[10],
        target_kind=record[11],
        target_id=record[12],
        detail=record[13],
        event_id=record[14],
        agent_id=record[15],
    )
    return row_json(int(record[0]), row)


def list_audit(
    connection: Connection,
    org: UUID,
    user: UUID,
    *,
    filters: Mapping[str, Any],
    before: str | None,
    limit: int,
) -> dict[str, Any]:
    """A page of the org's audit rows the user may read (module docstring)."""
    role = org_role(connection, org, user)
    if role not in ("admin", "member"):
        raise HubError("not_found")
    clauses, params = ["org_id = %s"], list[Any]([org])
    if role == "member":
        clauses.append(
            "(actor_id = %s OR wg_id IN (SELECT a.wg_id FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id"
            " WHERE w.org_id = %s AND a.user_id = %s AND a.revoked_seq IS NULL))"
        )
        params.extend([user, org, user])
    if before is not None:
        clauses.append("seq < %s")
        params.append(decode_before(before))
    for name, value in filters.items():
        if name == "action":
            clauses.append("(action = %s OR left(action, %s) = %s)")
            params.extend([value, len(value) + 1, value + "."])
        else:
            clauses.append(f"{_FILTER_COLUMNS[name]} = %s")
            params.append(value)
    found = connection.execute(
        f"SELECT {_COLUMNS} FROM hub.audit WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT %s",
        (*params, limit + 1),
    ).fetchall()
    rows = [_row(record) for record in found[:limit]]
    return {"rows": rows, "next_before": encode_before(rows[-1]["seq"]) if len(found) > limit else None}


def list_account_audit(connection: Connection, user: UUID, *, before: str | None, limit: int) -> dict[str, Any]:
    """A page of the user's account audit (`GET /v1/me/audit`, KOR-24): the rows the user wrote outside any org
    (`actor_id` = the user, no org) and every row written by a request of one of the user's agents, newest first."""
    clauses = ["((actor_id = %s AND org_id IS NULL) OR agent_id IN (SELECT id FROM hub.agent WHERE user_id = %s))"]
    params: list[Any] = [user, user]
    if before is not None:
        clauses.append("seq < %s")
        params.append(decode_before(before))
    found = connection.execute(
        f"SELECT {_COLUMNS} FROM hub.audit WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT %s",
        (*params, limit + 1),
    ).fetchall()
    rows = [_row(record) for record in found[:limit]]
    return {"rows": rows, "next_before": encode_before(rows[-1]["seq"]) if len(found) > limit else None}


def checkpoint_json(checkpoint: AuditCheckpoint) -> dict[str, Any]:
    """A checkpoint as the verification returns it."""
    return {
        "hub_key_id": checkpoint.hub_key_id,
        "seq": checkpoint.seq,
        "hash": checkpoint.hash,
        "published_at": format_time(checkpoint.published_at),
        "signature": b64u_encode(checkpoint.signature),
    }


def verify(connection: Connection, hub_key_id: str, hub_public_key: bytes) -> dict[str, Any]:
    """The verification of the chain and the newest checkpoint (module docstring)."""
    head = connection.execute("SELECT seq, hash FROM hub.audit ORDER BY seq DESC LIMIT 1").fetchone()
    check = verify_chain(connection, 1, 0 if head is None else int(head[0]))
    checkpoint = newest_checkpoint(connection, hub_key_id)
    ok = check.ok and (checkpoint is None or checkpoint_valid(connection, checkpoint, hub_public_key))
    return {
        "ok": ok,
        "head": {"seq": 0, "hash": EMPTY_HASH} if head is None else {"seq": int(head[0]), "hash": str(head[1])},
        "checkpoint": None if checkpoint is None else checkpoint_json(checkpoint),
    }
