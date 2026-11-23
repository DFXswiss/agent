"""Content-free pings to sessions of this device (05-runner.md section 9, 03-api.md section 7.1, ADR-020).

`send_ping(session_id, kind, level, ref, ping_id, context=...)` (with the daemon context) is the one way a ping
reaches a guest: the hub's durable `whiteboard.entry` pings (reconcile step, `reconcile.py`), the `subagent.finished`
ping of RUN-11 (`subagent_finished.py`) and the `operation.finished` ping of RUN-07 (`op_finished.py`). A ping carries
only its kind, the whiteboard level and the ID of what is new; never content, never a prompt, never tmux.

The ping is first stored in `runner.ping` (idempotent by `ping_id`; the same ID with other fields is refused) and then
sent with `POST /v1/sessions/{id}/pings` when the session can take it: it is not `stopping`, `stopped` or `failed`,
the guest knows it (`sandbox_ready` or later) and its VM runs. Once the guest kept it, `sent_at` is set; a ping that
could not be sent yet stays unsent and `Pinger.flush` (every reconcile run) tries again. The guest is idempotent by
`ping_id` as well, so a repeated send changes nothing there.

`stable_ping_id(kind, ref)` derives the UUIDv7-shaped ping ID of a runner ping from its kind and the (UUIDv7) ID of the
finished child or operation: the timestamp bits of `ref`, the rest from SHA-256, so a restarted daemon sends the same
ID again (the guest contract requires UUIDv7).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.prompts.resume import vm_running
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv

ATTRIBUTE = "pinger"
KINDS = ("whiteboard.entry", "subagent.finished", "operation.finished")
LEVELS = frozenset({"org", "workgroup", "task"})
SENDABLE = frozenset(
    {"sandbox_ready", "agent_starting", "prompt_delivering", "working", "idle", "suspended", "resuming"}
)
"""Session states in which the guest knows the session and it may still take a turn."""
KEEP_AFTER_END_S = 3600
"""Rows of an ended session are kept this long (a `ping.delivered` journaled just before the end still finds them)."""
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class PingInvalid(ValueError):
    """A ping that cannot be sent (shape of its fields)."""


def stable_ping_id(kind: str, ref: str) -> str:
    """A UUIDv7-shaped ID that is the same for the same `(kind, ref)` (see the module docstring)."""
    if not _UUID_RE.fullmatch(ref):
        raise PingInvalid(kind)
    digest = bytearray(hashlib.sha256(f"{kind}:{ref}".encode()).digest()[:16])
    digest[:6] = uuid.UUID(ref).bytes[:6]
    digest[6] = 0x70 | (digest[6] & 0x0F)
    digest[8] = 0x80 | (digest[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(digest)))


def check(session_id: str, kind: str, level: str | None, ref: str, ping_id: str) -> None:
    """`PingInvalid` unless the fields form a ping of `contracts/guest/objects.json#/$defs/ping`."""
    if (
        kind not in KINDS
        or (level is None) == (kind == "whiteboard.entry")
        or (level is not None and level not in LEVELS)
    ):
        raise PingInvalid(kind)
    for value in (session_id, ref, ping_id):
        if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
            raise PingInvalid(kind)


@dataclass(frozen=True)
class StoredPing:
    id: str
    session_id: str
    kind: str
    level: str | None
    ref: str
    sent: bool

    def body(self) -> dict[str, Any]:
        """The request of `POST /v1/sessions/{id}/pings`."""
        found: dict[str, Any] = {"ping_id": self.id, "kind": self.kind, "ref": self.ref}
        if self.level is not None:
            found["level"] = self.level
        return found


_COLUMNS = "p.id::text, p.session_id::text, p.kind, p.level, p.ref::text, p.sent_at IS NOT NULL"


class Pinger:
    def __init__(self, env: SessionEnv) -> None:
        self.env = env

    def line(self, text: str) -> None:
        self.env.log(f"runner.pings {text}")

    async def store(self, session_id: str, kind: str, level: str | None, ref: str, ping_id: str) -> StoredPing | None:
        """Keep the ping (idempotent); None when its ID is taken by another ping."""
        check(session_id, kind, level, ref, ping_id)
        async with self.env.transaction() as conn:
            await conn.execute(
                "INSERT INTO runner.ping (id, session_id, kind, level, ref) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING",
                (ping_id, session_id, kind, level, ref),
            )
            cursor = await conn.execute(f"SELECT {_COLUMNS} FROM runner.ping p WHERE p.id = %s", (ping_id,))
            row = await cursor.fetchone()
        assert row is not None
        ping = StoredPing(*row)
        if (ping.session_id, ping.kind, ping.level, ping.ref) != (session_id, kind, level, ref):
            self.line(f"id_conflict ping_id={ping_id}")
            return None
        return ping

    async def send(self, session_id: str, kind: str, level: str | None, ref: str, ping_id: str) -> bool:
        """Store and send one ping; True once the guest kept it."""
        ping = await self.store(session_id, kind, level, ref, ping_id)
        return ping is not None and await self.deliver(ping)

    async def deliver(self, ping: StoredPing) -> bool:
        if ping.sent:
            return True
        async with self.env.transaction() as conn:
            session = await records.get(conn, ping.session_id)
        if session is None or session.state not in SENDABLE:
            return False
        row = await vm_running(self.env, session)
        if row is None:
            return False
        try:
            async with await self.env.client(row) as guest:
                await guest.ping(ping.session_id, ping.body())
        except GuestRefused as exc:
            self.line(f"refused ping_id={ping.id} session_id={ping.session_id} error={exc.error}")
            return False
        except (GuestError, RunnerError) as exc:
            self.line(f"send_failed ping_id={ping.id} session_id={ping.session_id} code={exc.code}")
            return False
        async with self.env.transaction() as conn:
            await conn.execute("UPDATE runner.ping SET sent_at = now() WHERE id = %s", (ping.id,))
        self.line(f"sent ping_id={ping.id} session_id={ping.session_id} kind={ping.kind}")
        return True

    async def flush(self) -> int:
        """Send every unsent ping of a session that can take it; delete the rows of long-ended sessions. Returns the
        number of pings sent."""
        async with self.env.transaction() as conn:
            await conn.execute(
                "DELETE FROM runner.ping p WHERE NOT EXISTS (SELECT 1 FROM runner.session s WHERE s.id = p.session_id"
                " AND (s.state <> ALL(%s) OR s.ended_at > now() - make_interval(secs => %s)))",
                (sorted(records.FINAL), KEEP_AFTER_END_S),
            )
            rows = await (
                await conn.execute(
                    f"SELECT {_COLUMNS} FROM runner.ping p"
                    " JOIN runner.session s ON s.id = p.session_id"
                    " WHERE p.sent_at IS NULL AND s.state = ANY(%s) ORDER BY p.created_at, p.id",
                    (sorted(SENDABLE),),
                )
            ).fetchall()
        sent = 0
        for row in rows:
            sent += await self.deliver(StoredPing(*row))
        return sent


def existing(context: Any) -> Pinger | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, Pinger) else None


def install(context: Any, pinger: Pinger) -> Pinger:
    setattr(context, ATTRIBUTE, pinger)
    return pinger


def pinger_for(context: Any) -> Pinger | None:
    """The pinger of this daemon over the session area's collaborators; None while the device is not enrolled."""
    found = existing(context)
    if found is not None:
        return found
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points

    service = service_for(context)
    if service is None:
        return None
    return install(context, Pinger(service.env))


async def send_ping(session_id: str, kind: str, level: str | None, ref: str, ping_id: str, *, context: Any) -> bool:
    """Send one content-free ping to a session of this device (see the module docstring); True once the guest kept
    it, False when it waits for the next reconcile run (or the device is not enrolled)."""
    pinger = pinger_for(context)
    if pinger is None:
        return False
    return await pinger.send(session_id, kind, level, ref, ping_id)
