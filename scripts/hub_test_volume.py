"""The volume data set of the web suite's volume gate (KOR-35, ADR-063 section 4), seeded by the device double.

`scripts/hub_test_director.py` serves these routes (only loopback, only for tests):

- `POST /volume/seed {counts?}`: starts seeding in a background thread (once per hub; a repeat answers the running or
  finished seed); `GET /volume/seed` answers `{"state": "running" | "done" | "failed", "data"?, "error"?}`.
- `POST /volume/invite {}`: a new admin invitation of the volume org (`{"code"}`), joined by a browser user of a
  volume project with the suite's `joinWithCode` (its own passkey and identity).
- `POST /volume/joined {"user_id"}`: what the joined user needs to see at volume: the notifications (rows of the
  notifications area, content-free) and the platform role `inviter` (the platform invitations screen).
- `POST /volume/live {"seconds", "rate"}`: live load for `seconds` - session events of the live sessions (in other
  tasks of the big workgroup), about `rate` per second in total, stored and announced like the outbox does.

The data set is built the way the clients build it - nothing bypasses the encryption of user-authored content:

- a founder (a user principal with a registered identity, held only by this process) creates a non-private org through
  the orgs area (`create_org`), the operator grants it platform access (platform log), and a platform device of this
  process (certified, offline, never a scheduling candidate) is assigned to the big workgroup with its wrapped key;
- members and org devices are real principals with their own keys: one `invite` batch of the founder, then one
  `accept` batch per member (`member.profile` included) and per device, through `append_batch` with every log rule;
  open invitations and the platform invitations stay open (at the list caps);
- every event (`task.meta`, `prompt`, whiteboard, the session kinds, operation parameters and results, terminal) is
  sealed with the scope key and signed by its author and stored through `append_events` with every hub check, in
  big transactions; rows without content (tasks, sessions, operations, notifications) are inserted directly, like
  `scripts/hub_test_events.py` does for subagent sessions.

Sizes (`COUNTS`, overridable per request) and why: `docs/platform/decisions/KOR-35.md`.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_hub.crypto import event as event_format
from agent_hub.crypto import invite as invite_format
from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, body_json, format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.keys import Identity
from agent_hub.events.notify import announce
from agent_hub.events.store import StoredEvent, append_events
from agent_hub.log.entries import entry_hash, sign_log_entry
from agent_hub.orgs.append import Appender, AppendResult, append_batch
from agent_hub.orgs.genesis import create_org

Json = dict[str, Any]
Pool = ConnectionPool[psycopg.Connection[Any]]
Connection = psycopg.Connection[Any]

COUNTS: dict[str, int] = {
    "chat_per_kind": 10_000,
    "small_per_kind": 1_000,
    "terminal_chunks": 10_000,
    "tasks": 5_000,
    "notifications": 2_000,
    "members": 500,
    "org_devices": 600,
    "org_invites": 1_000,
    "platform_invites": 1_000,
    "whiteboard_entries": 300,
    "whiteboard_versions": 200,
    "subagent_depth": 6,
    "subagent_siblings": 30,
    "live_sessions": 20,
}
"""The data set of ADR-063 section 4 (operations: one card per chat round, so `chat_per_kind` of them)."""
BATCH = 2_000
"""Events per `append_events` transaction."""
EXPIRES = timedelta(days=20)
BIG = 64 * 1024
"""The contract's largest text of a message, tool input or result, whiteboard entry (64 KiB)."""
REPOSITORIES = 2_000
"""Repositories of the largest operation result (about 160 KB of JSON: close to the 256 KiB ciphertext bound)."""


def now() -> datetime:
    return datetime.now(UTC)


class DirectorLike(Protocol):
    """What the seeder uses of `hub_test_director.Director`."""

    hub: str
    pool: Pool
    identities: dict[str, Identity]

    def append(self, *bodies: Json) -> int: ...

    def create_device(self, online: bool, pool_ready: int, free: int = ..., exclusive: bool = ...) -> str: ...

    def certified_seq(self, device_id: str) -> int: ...

    def hub_key(self) -> Json: ...


@dataclass
class Actor:
    id: str
    kind: str
    identity: Identity

    @property
    def appender(self) -> Appender:
        return Appender(self.id, self.kind)


@dataclass
class Org:
    """The volume org as its founder knows it: the entries, the scope keys and the workgroup."""

    id: str
    wg: str
    founder: Actor
    entries: list[Json] = field(default_factory=list)
    keys: dict[tuple[str, str, int], bytes] = field(default_factory=dict)

    def body(self, entry_type: str, by: Actor, **fields: object) -> Json:
        return {
            "org": self.id,
            "seq": len(self.entries),
            "prev": entry_hash(self.entries[-1]) if self.entries else "",
            "type": entry_type,
            "by": by.id,
            "ts": format_time(now()),
            "v": 1,
            **fields,
        }

    def add(self, entry_type: str, by: Actor, invite: Any = None, **fields: object) -> Json:
        entry = sign_log_entry(self.body(entry_type, by, **fields), by.identity.sig, invite)
        self.entries.append(entry)
        return entry

    def wrap(self, wrapper: Actor, scope: str, scope_id: str, recipient: str, kem_public: bytes) -> Json:
        sealed = wrap_format.wrap_key(
            kem_public, scope=scope, scope_id=scope_id, epoch=1, recipient=recipient, key=self.keys[scope, scope_id, 1]
        )
        signature = wrap_format.sign_wrap(
            wrapper.identity.sig, scope=scope, scope_id=scope_id, epoch=1, recipient=recipient, wrap=sealed
        )
        return {
            "scope": scope,
            "scope_id": scope_id,
            "epoch": 1,
            "recipient_id": recipient,
            "wrap": sealed,
            "sig": signature,
        }

    def seal(self, author: Actor, kind: str, body: object, *, scope: str = "wg", **header: str) -> Json:
        scope_id = self.wg if scope == "wg" else self.id
        fields = event_format.new_header(
            id=str(uuid7()),
            org=self.id,
            scope=scope,
            scope_id=scope_id,
            epoch=1,
            author=author.id,
            kind=kind,
            ts=format_time(now()),
            lh=len(self.entries) - 1,
            **({"wg": self.wg} if scope == "wg" else {}),
            **header,
        )
        return event_format.seal_event(self.keys[scope, scope_id, 1], fields, body_json(body), author.identity.sig)


def insert_user(connection: Connection, actor: Actor, at: datetime) -> None:
    """A user principal with its registered identity (public keys only)."""
    connection.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (actor.id, at))
    connection.execute(
        "INSERT INTO hub.user_identity (user_id, kem_pub, sig_pub, recovery_id, created_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (actor.id, actor.identity.kem_public(), actor.identity.sig_public(), secrets.token_urlsafe(16), at),
    )


def insert_org_device(connection: Connection, actor: Actor, org: str, at: datetime) -> None:
    """An org device principal with its own keys (enrolled, waiting for its `accept`)."""
    connection.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s)", (actor.id, at))
    connection.execute(
        "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, agent_version, os, arch,"
        " capacity, updated_at) VALUES (%s, 'org', %s, %s, %s, 'active', %s, '0.1.0', 'linux', 'x86_64', %s, %s)",
        (actor.id, actor.identity.kem_public(), actor.identity.sig_public(), org, at, Jsonb({}), at),
    )


def chunks[T](items: Sequence[T], size: int = BATCH) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Seeder:
    """Builds the volume data set once per hub and serves the follow-up routes (module docstring)."""

    def __init__(self, director: DirectorLike) -> None:
        self.director = director
        self.pool = director.pool
        self.lock = threading.Lock()
        self.state: Json = {"state": "idle"}
        self.org: Org | None = None
        self.device: Actor | None = None
        self.live: list[tuple[str, str]] = []
        self.root_key = b""

    # --- routes ---

    def start(self, counts: Mapping[str, int], root_key: bytes) -> Json:
        with self.lock:
            if self.state["state"] == "idle":
                self.state = {"state": "running"}
                self.root_key = root_key
                wanted = {**COUNTS, **{key: int(value) for key, value in counts.items() if key in COUNTS}}
                threading.Thread(target=self._run, args=(wanted,), daemon=True).start()
            return dict(self.state)

    def status(self) -> Json:
        with self.lock:
            return dict(self.state)

    def _run(self, counts: dict[str, int]) -> None:
        started = time.monotonic()
        try:
            data = self.seed(counts)
            data["seconds"] = round(time.monotonic() - started, 1)
            with self.lock:
                self.state = {"state": "done", "data": data}
        except Exception as error:  # the spec reads the message
            with self.lock:
                self.state = {
                    "state": "failed",
                    "error": f"{type(error).__name__}: {error}",
                    "trace": traceback.format_exc(limit=-4),
                }

    # --- helpers ---

    def store(self, author: Actor, events: Sequence[Json], *, live: bool = False) -> list[StoredEvent]:
        """`append_events` in transactions of `BATCH` events (announced only for live load)."""
        stored: list[StoredEvent] = []
        for part in chunks(events):
            with self.pool.connection() as connection, connection.transaction():
                stored.extend(append_events(connection, part, author.id, now=now(), root_key=self.root_key))
        if live:
            announce(stored)
        return stored

    def batch(
        self,
        by: Actor,
        first: int,
        *,
        wraps: Sequence[Json] = (),
        events: Sequence[Json] = (),
        extra: Callable[[Connection, AppendResult], None] | None = None,
    ) -> None:
        assert self.org is not None
        with self.pool.connection() as connection, connection.transaction():
            append_batch(
                connection,
                org_id=UUID(self.org.id),
                appender=by.appender,
                expected_seq=first,
                entries=self.org.entries[first:],
                wraps=list(wraps),
                events=list(events),
                now=now(),
                root_key=self.root_key,
                extra=extra,
            )

    def invite_rows(self, invites: Sequence[tuple[str, str, int, bytes | None]], founder: Actor) -> Callable[..., None]:
        """The `extra` step that stores invitation rows `(invite_id, kind, seq, package)` with their entries."""
        assert self.org is not None
        org = self.org.id

        def extra(connection: Connection, _result: AppendResult) -> None:
            at = now()
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO hub.invite (id, scope, kind, org_id, log_seq, created_by, created_at, expires_at,"
                    " package_ct) VALUES (%s, 'org', %s, %s, %s, %s, %s, %s, %s)",
                    [(item[0], item[1], org, item[2], founder.id, at, at + EXPIRES, item[3]) for item in invites],
                )

        return extra

    # --- the data set ---

    def seed(self, counts: dict[str, int]) -> Json:
        founder = Actor(str(uuid7()), "user", Identity.generate())
        org = Org(str(uuid7()), str(uuid7()), founder)
        self.org = org
        org.keys = {("org", org.id, 1): secrets.token_bytes(32), ("wg", org.wg, 1): secrets.token_bytes(32)}
        timings: dict[str, float] = {}
        mark = time.monotonic()

        def lap(name: str) -> None:
            nonlocal mark
            timings[name] = round(time.monotonic() - mark, 1)
            mark = time.monotonic()

        self.create_org(founder, org)
        lap("org")
        members = self.members(founder, org, counts)
        lap("members")
        tasks = self.tasks(founder, org, counts["tasks"], 2 + counts["live_sessions"])
        lap("tasks")
        big = self.chat_task(founder, org, tasks[0], counts["chat_per_kind"], big_items=True)
        lap("chat")
        small = self.chat_task(founder, org, tasks[1], counts["small_per_kind"], big_items=False)
        lap("small chat")
        self.terminal(big["session"], counts["terminal_chunks"])
        lap("terminal")
        tree = self.subagents(org, tasks[0], big["session"], counts["subagent_depth"], counts["subagent_siblings"])
        lap("subagents")
        boards = self.whiteboards(founder, org, tasks[0], counts["whiteboard_entries"], counts["whiteboard_versions"])
        lap("whiteboards")
        self.live = [
            (tasks[2 + index], self.session(org, tasks[2 + index])) for index in range(counts["live_sessions"])
        ]
        self.platform_invites(counts["platform_invites"])
        lap("platform invites")
        return {
            "org": org.id,
            "wg": org.wg,
            "founder": founder.id,
            "device": self.device.id if self.device else None,
            "big_task": tasks[0],
            "big_session": big["session"],
            "big_child": big["child"],
            "big_operation": big["operation"],
            "small_task": tasks[1],
            "small_session": small["session"],
            "deep_session": tree,
            "whiteboard_entry": boards["history"],
            "members": len(members),
            "counts": counts,
            "timings": timings,
        }

    def create_org(self, founder: Actor, org: Org) -> None:
        at = now()
        with self.pool.connection() as connection, connection.transaction():
            insert_user(connection, founder, at)
        genesis = org.add(
            "genesis", founder, subject=founder.id, pub=founder.identity.public(), default_wg=org.wg, private=False
        )
        kem = founder.identity.kem_public()
        wraps = [org.wrap(founder, "org", org.id, founder.id, kem), org.wrap(founder, "wg", org.wg, founder.id, kem)]
        events = [
            org.seal(founder, "org.profile", {"name": "Volume"}, scope="org"),
            org.seal(founder, "wg.profile", {"name": "Volume work"}, scope="org", wg=org.wg),
            org.seal(founder, "member.profile", {"display_name": "Volume founder"}, scope="org", ref=founder.id),
        ]
        with self.pool.connection() as connection, connection.transaction():
            create_org(
                connection,
                appender=founder.appender,
                genesis=genesis,
                wraps=wraps,
                events=events,
                platform_invite_id=None,
                now=now(),
                root_key=self.root_key,
            )
        # platform access (operator) and the platform device of the sessions (offline: never a candidate of others)
        self.director.append({"type": "org_platform_access", "org": org.id, "granted": True})
        device_id = self.director.create_device(False, 0, 0, exclusive=False)
        self.device = Actor(device_id, "device", self.director.identities[device_id])
        first = len(org.entries)
        org.add(
            "device_assign", founder, device=device_id, wg=org.wg, platform_seq=self.director.certified_seq(device_id)
        )
        wrap = org.wrap(founder, "wg", org.wg, device_id, self.device.identity.kem_public())
        self.batch(founder, first, wraps=[wrap])

    def members(self, founder: Actor, org: Org, counts: Mapping[str, int]) -> list[Actor]:
        """Members and org devices (invited and accepted) and open invitations at the cap."""
        at = now()
        users = [Actor(str(uuid7()), "user", Identity.generate()) for _ in range(counts["members"])]
        devices = [Actor(str(uuid7()), "device", Identity.generate()) for _ in range(counts["org_devices"])]
        with self.pool.connection() as connection, connection.transaction():
            for user in users:
                insert_user(connection, user, at)
            for device in devices:
                insert_org_device(connection, device, org.id, at)
        expires = format_time(at + EXPIRES)
        first = len(org.entries)
        rows: list[tuple[str, str, int, bytes | None]] = []
        found: dict[str, invite_format.InviteSecrets] = {}
        for actor in [*users, *devices]:
            code = invite_format.invite_secrets(invite_format.generate_code())
            found[actor.id] = code
            fields: Json = {"invite_id": code.invite_id, "pk_inv": code.pk_inv, "kind": actor.kind}
            if actor.kind == "user":
                fields.update(role="member", wgs={})
            else:
                fields["wgs"] = {org.wg: "device"}
            org.add("invite", founder, **fields, expires=expires)
            rows.append((code.invite_id, actor.kind, len(org.entries) - 1, None))
        for _ in range(counts["org_invites"]):
            code = invite_format.invite_secrets(invite_format.generate_code())
            org.add(
                "invite",
                founder,
                invite_id=code.invite_id,
                pk_inv=code.pk_inv,
                kind="user",
                role="member",
                wgs={},
                expires=expires,
            )
            rows.append((code.invite_id, "user", len(org.entries) - 1, None))
        self.batch(founder, first, extra=self.invite_rows(rows, founder))
        for index, actor in enumerate([*users, *devices]):
            code = found[actor.id]
            first = len(org.entries)
            org.add(
                "accept",
                actor,
                code.signing,
                invite_id=code.invite_id,
                subject=actor.id,
                kind=actor.kind,
                pub=actor.identity.public(),
            )
            events = []
            if actor.kind == "user":
                name = {"display_name": f"Member {index:04d} with a rather long display name"}
                events.append(org.seal(actor, "member.profile", name, scope="org", ref=actor.id))
            self.batch(actor, first, events=events)
        return users

    def tasks(self, founder: Actor, org: Org, count: int, working: int) -> list[str]:
        """`count` tasks of the workgroup (rows without content, titles as sealed `task.meta` events), newest first."""
        start = now() - timedelta(days=30)
        ids = [str(uuid7(int((start + timedelta(minutes=index)).timestamp() * 1000))) for index in range(count)]
        ids.reverse()
        with self.pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
            rows = []
            for index, task in enumerate(ids):
                at = start + timedelta(minutes=count - index)
                state = "working" if index < working else "idle"
                rows.append((task, org.id, org.wg, founder.id, state, at, at, at))
            cursor.executemany(
                "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
            cursor.executemany(
                "INSERT INTO hub.task_transition (task_id, seq, state, reason, at, by_id)"
                " VALUES (%s, 1, %s, NULL, %s, %s)",
                [(row[0], row[4], row[5], founder.id) for row in rows],
            )
        titles = [
            org.seal(
                founder, "task.meta", {"title": f"Volume task {index:05d} - refactor module {index % 97}"}, task=task
            )
            for index, task in enumerate(ids)
        ]
        self.store(founder, titles)
        return ids

    def session(self, org: Org, task: str, parent: str | None = None, depth: int = 0) -> str:
        """A working session of the task on the volume device (a row without content)."""
        assert self.device is not None
        session = str(uuid7())
        at = now()
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth,"
                " state, model_access, created_at, ready_at, first_working_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'working', 'platform', %s, %s, %s)",
                (session, task, org.id, org.wg, org.founder.id, self.device.id, parent, depth, at, at, at),
            )
            connection.execute(
                "INSERT INTO hub.session_transition (session_id, seq, state, reason, at, received_at, reported_by)"
                " VALUES (%s, 1, 'working', NULL, %s, %s, %s)",
                (session, at, at, self.device.id),
            )
        return session

    def chat_task(self, founder: Actor, org: Org, task: str, per_kind: int, *, big_items: bool) -> Json:
        """`per_kind` items of every chat kind in one task, interleaved (so every window shows every kind)."""
        assert self.device is not None
        device = self.device
        session = self.session(org, task)
        child = self.session(org, task, session, 1)
        prompts: list[Json] = []
        device_events: list[Json] = []
        operations: list[tuple[str, int]] = []
        patch = "\n".join(f"+line {line} of a checked-out file" for line in range(20))
        for index in range(per_kind):
            prompts.append(
                org.seal(founder, "prompt", {"text": f"Prompt {index}: continue", "attachments": []}, task=task)
            )
            kinds: list[tuple[str, object, str, str]] = [
                (
                    "session.output",
                    {
                        "message_id": f"m{index}",
                        "role": "assistant",
                        "turn": index + 1,
                        "text": f"## Step {index}\n\nSome **Markdown** of answer {index}.",
                    },
                    session,
                    "",
                ),
                (
                    "session.tool",
                    {
                        "tool_use_id": f"t{index}",
                        "name": "Bash",
                        "input_summary": f"cat {index}",
                        "input": f"cat file{index}",
                        "result_summary": "ok",
                        "is_error": False,
                        "duration_ms": 10,
                    },
                    session,
                    "",
                ),
                (
                    "session.file",
                    {
                        "path": f"repo/dir{index % 37}/file{index}.txt",
                        "op": "write",
                        "sha256": "0" * 64,
                        "size": 3,
                        "patch": patch,
                    },
                    session,
                    "",
                ),
                (
                    "session.hook",
                    {"event": "PreToolUse", "tool": "Bash", "decision": "deny", "reason": f"network {index}"},
                    session,
                    "",
                ),
                ("session.status", {"state": "failed", "reason": "oom"}, session, ""),
                (
                    "session.output",
                    {
                        "message_id": f"c{index}",
                        "role": "assistant",
                        "turn": index + 1,
                        "text": f"Subagent answer {index}",
                    },
                    child,
                    "",
                ),
            ]
            if index % 10 == 0:  # a second file right after the first: a file group
                kinds.insert(
                    3,
                    (
                        "session.file",
                        {"path": f"repo/group{index}.txt", "op": "edit", "sha256": "1" * 64, "size": 4, "patch": patch},
                        session,
                        "",
                    ),
                )
            operation = str(uuid7())
            operations.append((operation, index))
            kinds.append(
                (
                    "operation.params",
                    {"operation_id": operation, "type": "github.list_repositories", "params": {}},
                    session,
                    operation,
                )
            )
            kinds.append(
                (
                    "operation.result",
                    {"operation_id": operation, "status": "succeeded", "result": {"repositories": []}},
                    session,
                    operation,
                )
            )
            for kind, body, on, ref in kinds:
                header = {"task": task, "session": on, **({"ref": ref} if ref else {})}
                device_events.append(org.seal(device, kind, body, **header))
        largest = ""
        if big_items:
            line = "x" * BIG
            markdown = ("A long paragraph of **Markdown** text. " * 1700)[:BIG]
            device_events.extend(
                org.seal(device, kind, body, task=task, session=session)
                for kind, body in (
                    ("session.output", {"message_id": "big-line", "role": "assistant", "turn": 1, "text": line}),
                    ("session.output", {"message_id": "big-md", "role": "assistant", "turn": 1, "text": markdown}),
                    (
                        "session.tool",
                        {
                            "tool_use_id": "big",
                            "name": "Bash",
                            "input_summary": "big",
                            "input": line,
                            "result_summary": line,
                            "is_error": False,
                            "duration_ms": 1,
                        },
                    ),
                    (
                        "session.file",
                        {
                            "path": "repo/big.txt",
                            "op": "write",
                            "sha256": "2" * 64,
                            "size": BIG,
                            "patch": "+" + line[1:],
                        },
                    ),
                )
            )
            largest = str(uuid7())
            operations.append((largest, per_kind))
            repositories = [
                {"repo": f"volume-org/repository-{n:05d}", "default_branch": "main", "private": False, "allowed": True}
                for n in range(REPOSITORIES)
            ]
            for kind, body in (
                ("operation.params", {"operation_id": largest, "type": "github.list_repositories", "params": {}}),
                (
                    "operation.result",
                    {"operation_id": largest, "status": "succeeded", "result": {"repositories": repositories}},
                ),
            ):
                device_events.append(org.seal(device, kind, body, task=task, session=session, ref=largest))
        self.store(founder, prompts)
        self.store(device, device_events)
        self.operation_rows(org, task, session, operations)
        # a later main session (a restart of the task): its countdown item comes last in the timeline
        self.session(org, task)
        return {"session": session, "child": child, "operation": largest or operations[-1][0]}

    def operation_rows(self, org: Org, task: str, session: str, operations: Sequence[tuple[str, int]]) -> None:
        """The operation rows (content-free) of the stored `operation.params`/`operation.result` events."""
        assert self.device is not None
        params: dict[str, str] = {}
        results: dict[str, str] = {}
        with self.pool.connection() as connection:
            for row in connection.execute(
                "SELECT id, kind, ref FROM hub.event WHERE task_id = %s AND kind IN ('operation.params',"
                " 'operation.result')",
                (task,),
            ):
                (params if row[1] == "operation.params" else results)[str(row[2])] = str(row[0])
        at = now()
        rows = [
            (
                op,
                org.id,
                org.wg,
                task,
                session,
                self.device.id,
                self.device.id,
                "github.list_repositories",
                params.get(op),
                results.get(op),
                at - timedelta(seconds=len(operations) - index),
            )
            for op, index in operations
        ]
        with self.pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
                " params_event_id, result_event_id, created_at, decided_at, started_at, finished_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'succeeded', %s, %s, %s, %s, %s, %s)",
                [(*row, row[-1], row[-1], row[-1]) for row in rows],
            )
            cursor.executemany(
                "INSERT INTO hub.operation_transition (operation_id, seq, state, error_code, at, by_id)"
                " VALUES (%s, %s, %s, NULL, %s, %s)",
                [
                    (row[0], seq, state, row[-1], self.device.id)
                    for row in rows
                    for seq, state in enumerate(("received", "accepted", "executing", "succeeded"), start=1)
                ],
            )

    def terminal(self, session: str, count: int) -> None:
        """Terminal history: `count` chunks of about 2 KB each, some with very long lines."""
        assert self.org is not None
        assert self.device is not None
        org, device = self.org, self.device
        task = self.task_of(session)
        events = []
        for index in range(count):
            text = (f"line {index}: " + "output " * 40 + "\r\n") * 6 if index % 50 else ("y" * 16_000) + "\r\n"
            body = {"offset_ms_start": index * 100, "frames": [[index * 100, "o", text]], "cols": 160, "rows": 48}
            events.append(org.seal(device, "session.terminal", body, task=task, session=session))
        self.store(device, events)

    def task_of(self, session: str) -> str:
        with self.pool.connection() as connection:
            row = connection.execute("SELECT task_id FROM hub.session WHERE id = %s", (session,)).fetchone()
        assert row is not None
        return str(row[0])

    def subagents(self, org: Org, task: str, main: str, depth: int, siblings: int) -> str:
        """A chain of subagents deeper than the hub's limit (2) and more siblings than it allows (10)."""
        parent = main
        for level in range(1, depth + 1):
            parent = self.session(org, task, parent, level)
        for _ in range(siblings):
            self.session(org, task, main, 1)
        return parent

    def whiteboards(self, founder: Actor, org: Org, task: str, entries: int, versions: int) -> Json:
        """`entries` entries on every level and `versions` versions of one workgroup entry (64 KiB each)."""
        events: list[Json] = []
        org_events: list[Json] = []
        for level in ("org", "workgroup", "task"):
            for index in range(entries):
                ref = str(uuid7())
                text = f"# Note {index} on the {level} level\n\n" + "Some *Markdown* text of the note. " * 20
                body = {"level": level, "text": text}
                if level == "org":
                    org_events.append(org.seal(founder, "whiteboard.entry", body, scope="org", ref=ref))
                else:
                    extra = {"task": task} if level == "task" else {}
                    events.append(org.seal(founder, "whiteboard.entry", body, ref=ref, **extra))
        history = str(uuid7())
        base = org.seal(founder, "whiteboard.entry", {"level": "workgroup", "text": self.version_text(0)}, ref=history)
        self.store(founder, org_events)
        self.store(founder, [*events, base])
        previous = str(base["header"]["id"])
        edits = []
        for version in range(1, versions):
            edit = org.seal(
                founder, "whiteboard.edit", {"text": self.version_text(version), "base_event_id": previous}, ref=history
            )
            previous = str(edit["header"]["id"])
            edits.append(edit)
        self.store(founder, edits)
        return {"history": history}

    @staticmethod
    def version_text(version: int) -> str:
        """About 64 KiB of lines, a few of them changed in every version."""
        lines = [f"line {line:05d} of a long whiteboard entry" for line in range(1800)]
        for line in range(version % 7, len(lines), 97):
            lines[line] = f"line {line:05d} changed in version {version}"
        return "\n".join(lines)[:BIG]

    def platform_invites(self, count: int) -> None:
        """Open platform invitations at the list cap (one platform-log append)."""
        expires = format_time(now() + EXPIRES)
        bodies = []
        for _ in range(count):
            code = invite_format.invite_secrets(invite_format.generate_code())
            bodies.append(
                {"type": "user_invite", "invite_id": code.invite_id, "pk_inv": code.pk_inv, "expires": expires}
            )
        for part in chunks(bodies, 500):
            self.director.append(*part)

    # --- follow-up routes ---

    def admin_invite(self) -> Json:
        """A new admin invitation of the volume org with a real package (the joiner opens it)."""
        org = self.org
        if org is None:
            raise RuntimeError("the volume data set is not seeded")
        founder = org.founder
        with self.lock:
            self.reload_entries(org)
            code = invite_format.generate_code()
            found = invite_format.invite_secrets(code)
            first = len(org.entries)
            org.add(
                "invite",
                founder,
                invite_id=found.invite_id,
                pk_inv=found.pk_inv,
                kind="user",
                role="admin",
                wgs={},
                expires=format_time(now() + EXPIRES),
            )
            package = {
                "v": invite_format.PACKAGE_VERSION,
                "org": org.id,
                "org_name": "Volume",
                "wg_names": {org.wg: "Volume work"},
                "founder": b64u_encode(founder.identity.sig_public()),
                "head": {"seq": first, "hash": entry_hash(org.entries[-1])},
                "keys": [
                    {"scope": scope, "scope_id": scope_id, "epoch": epoch, "key": b64u_encode(key)}
                    for (scope, scope_id, epoch), key in sorted(org.keys.items())
                ],
                "hub_key": self.director.hub_key(),
            }
            sealed = b64u_decode(invite_format.seal_package(found, package))
            self.batch(founder, first, extra=self.invite_rows([(found.invite_id, "user", first, sealed)], founder))
        return {"code": invite_format.format_code(code)}

    def reload_entries(self, org: Org) -> None:
        """The stored log of the org (joined browser users append their own `accept` entries)."""
        with self.pool.connection() as connection:
            org.entries = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT entry_text FROM hub.membership_entry WHERE org_id = %s ORDER BY seq", (org.id,)
                )
            ]

    def joined(self, user_id: str) -> Json:
        """Notifications and the platform role `inviter` of a joined user (its `accept` extended the org log)."""
        org = self.org
        if org is None:
            raise RuntimeError("the volume data set is not seeded")
        with self.lock:
            self.reload_entries(org)
        counts = self.status().get("data", {}).get("counts", COUNTS)
        start = now() - timedelta(days=10)
        kinds = ("whiteboard.entry", "task.idle", "task.failed")
        with self.pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
            tasks = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM hub.task WHERE wg_id = %s ORDER BY created_at DESC LIMIT 200", (org.wg,)
                )
            ]
            refs = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM hub.whiteboard_entry WHERE org_id = %s AND level = 'workgroup' LIMIT 200", (org.id,)
                )
            ]
            rows = []
            for index in range(int(counts["notifications"])):
                at = start + timedelta(minutes=index)
                kind = kinds[index % len(kinds)]
                board = kind == "whiteboard.entry"
                rows.append(
                    (
                        str(uuid7(int(at.timestamp() * 1000))),
                        user_id,
                        kind,
                        org.id,
                        org.wg,
                        None if board else tasks[index % len(tasks)],
                        refs[index % len(refs)] if board else None,
                        at,
                    )
                )
            cursor.executemany(
                "INSERT INTO hub.notification (id, recipient_id, kind, org_id, wg_id, task_id, ref, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
        self.director.append({"type": "role_grant", "subject": user_id, "role": "inviter"})
        return {}

    def live_load(self, seconds: float, rate: int) -> Json:
        """Session events of the live sessions for `seconds`, about `rate` per second (background thread)."""
        if self.org is None or self.device is None or not self.live:
            raise RuntimeError("the volume data set is not seeded")
        threading.Thread(target=self._live, args=(seconds, rate), daemon=True).start()
        return {"sessions": len(self.live)}

    def _live(self, seconds: float, rate: int) -> None:
        assert self.org is not None
        assert self.device is not None
        org, device = self.org, self.device
        tick = 0.25
        per_tick = max(1, round(rate * tick))
        deadline = time.monotonic() + seconds
        index = 0
        while time.monotonic() < deadline:
            began = time.monotonic()
            events = []
            for _ in range(per_tick):
                task, session = self.live[index % len(self.live)]
                body = {"message_id": f"live{index}", "role": "assistant", "turn": 1, "text": f"Live output {index}"}
                events.append(org.seal(device, "session.output", body, task=task, session=session))
                index += 1
            try:
                self.store(device, events, live=True)
            except Exception as error:  # printed; the spec measures what arrived
                print(f"volume: live load failed: {error}", flush=True)
                return
            time.sleep(max(0.0, tick - (time.monotonic() - began)))
