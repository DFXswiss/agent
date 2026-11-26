"""The mailboxes a session may read (ADR-099 sections 2 and 3).

`released(context, session)` asks the hub for the mailbox records released to the session's workgroup (`GET
/v1/device/mailboxes?wg_id=`, the hub's plaintext mirror) and keeps a record only when

- it verifies (header names its owner and the kind `mailbox`, the author's signature, `credentials.cache`) and opens
  under the owner's `CK` this device holds;
- its **sealed** release `use.wgs` names the session's workgroup (the mirror only finds candidates: a hub that names
  more allows nothing);
- its owner has explicit `read` or `write` access to that workgroup on the verified membership log now (an owner who
  left the workgroup or the organization ends the release at once);
- its version is not older than the newest this device verified of it (`runner.mailbox_seen`, kept per record): a
  hub that serves an older version that still released the workgroup is refused (no rollback).

A record that fails any of these is left out silently (it is not this session's mailbox); a hub that cannot be reached
fails the operation `hub_unreachable` (retryable). `choose(found, wanted)` picks the mailbox by its ID or its name
(`not_found` naming the released ones, E-05; two released mailboxes of one name are `not_found` naming their IDs,
so the hub's order never decides). The opened body lives only in the returned `Mailbox` for the operation and is
never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runner.credentials import cache
from agent_runner.credentials.cache import CredentialUnavailable
from agent_runner.mail.reader import Server
from agent_runner.operations.extensions import OperationFailed
from agent_runner.orgs.book import book_for
from agent_runner.orgs.db import session as session_of
from agent_runner.sessions.records import LocalSession
from agent_runner.sync.http import HubRefusal, HubTransportError

KIND = "mailbox"
LIST_TARGET = "/v1/device/mailboxes"
REMEDY = "Its owner releases a mailbox in the settings under Mailboxes or with agent-platform mailbox release."


@dataclass(frozen=True)
class Mailbox:
    id: str
    owner_id: str
    name: str | None
    email: str
    server: Server = field(repr=False)


def _mailbox(record_id: str, owner: str, body: dict[str, Any], wg_id: str) -> Mailbox | None:
    use = body.get("use")
    wgs = use.get("wgs") if isinstance(use, dict) else None
    if not isinstance(wgs, list) or wg_id not in wgs:
        return None
    try:
        server = Server(
            host=str(body["imap_host"]),
            port=int(body["imap_port"]),
            user=str(body["imap_user"]),
            password=str(body["imap_password"]),
        )
        email = str(body["email"])
    except (KeyError, TypeError, ValueError):
        return None
    name = body.get("name")
    return Mailbox(record_id, owner, name if isinstance(name, str) else None, email, server)


async def released(context: Any, session: LocalSession) -> list[Mailbox]:
    """The mailboxes this session may read (module docstring), newest change first."""
    try:
        answer = await cache._sync(context).client.request("GET", f"{LIST_TARGET}?wg_id={session.wg_id}")
    except (HubRefusal, HubTransportError):
        raise OperationFailed(
            "hub_unreachable", retryable=True, message="The platform could not list the mailboxes; try again."
        ) from None
    log = book_for(context).log(session.org_id)
    items = answer.get("mailboxes") if isinstance(answer, dict) else None
    found: list[Mailbox] = []
    for item in items if isinstance(items, list) else []:
        record_id, owner = (item.get("id"), item.get("owner_id")) if isinstance(item, dict) else (None, None)
        if not isinstance(record_id, str) or not isinstance(owner, str) or log is None:
            continue
        if log.state.explicit_access(owner, session.wg_id) is None:
            continue
        try:
            body = await cache._open(context, owner, KIND, item.get("record"))
        except CredentialUnavailable:
            continue
        header = item["record"]["header"]
        current = header.get("id") == record_id and await _newest(context, record_id, owner, int(header["version"]))
        mailbox = _mailbox(record_id, owner, body, session.wg_id) if current else None
        body.clear()
        if mailbox is not None:
            found.append(mailbox)
    return found


SEEN = (
    "INSERT INTO runner.mailbox_seen (id, owner_id, version) VALUES (%s, %s, %s)"
    " ON CONFLICT (id) DO UPDATE SET version = GREATEST(runner.mailbox_seen.version, EXCLUDED.version)"
    " RETURNING version"
)


async def _newest(context: Any, record_id: str, owner: str, version: int) -> bool:
    """Record `version` as seen; False when this device verified a newer version of the record before."""
    async with session_of(context.pool) as conn, conn.transaction():
        cursor = await conn.execute(SEEN, (record_id, owner, version))
        row = await cursor.fetchone()
    return row is not None and int(row[0]) == version


def choose(found: list[Mailbox], wanted: str) -> Mailbox:
    """The released mailbox with the ID or name `wanted` (module docstring)."""
    matching = [mailbox for mailbox in found if wanted in (mailbox.id, mailbox.name)]
    if len(matching) == 1:
        return matching[0]
    if matching:
        ids = ", ".join(sorted(mailbox.id for mailbox in matching))
        raise OperationFailed(
            "not_found", message=f"Several mailboxes named {wanted} are released here; name one by its ID: {ids}."
        )
    names = sorted({mailbox.name or mailbox.id for mailbox in found})
    here = f" Released here: {', '.join(names)}." if names else " No mailbox is released here."
    raise OperationFailed(
        "not_found", message=f"No mailbox named {wanted} is released to this workgroup.{here} {REMEDY}"[:1024]
    )
