"""Test kit of the RUN-04 suites: a fake guest, fake VMs and a `SessionEnv` over the RUN-03 rig.

- `FakeGuest` answers the control calls the session area makes (health, sessions, credential, control, journal
  long poll, ack) and records them; `emit(...)` appends journal records like the guest's control process does.
  Failures are injected per operation (`fail`).
- `FakeVms` stands in for `VmManager`: `ensure_vm` takes the first preferred live VM of the workgroup, else reuses
  its first `runner.vm` row, else inserts one (golden `g1`, Claude Code 2.1.330), books it for the session, records
  the call (`calls`) or raises the configured `RunnerError`.
- `Kit` (from `kit(env)`) runs the RUN-03 reconcile steps 1-3 once against the rig's hub, so the verified logs and
  the workgroup and org keys of org `a` are in the store, and builds a `SessionEnv` with the rig's real sync runtime
  (outbox, sealer, pusher against the contract-checking fake hub).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from agent_runner.crypto.encoding import parse_json
from agent_runner.crypto.event import open_event
from agent_runner.encoding import b64u_decode
from agent_runner.errors import RunnerError
from agent_runner.guestlink.client import Health, JournalPage, JournalRecord, SessionState
from agent_runner.guestlink.errors import GuestError
from agent_runner.model.selection import BUILTIN, load_catalog
from agent_runner.orgs.book import book_for
from agent_runner.reconcile import logs, state, wraps
from agent_runner.reconcile.engine import Engine
from agent_runner.registry import ReconcileStep
from agent_runner.sessions.desired import Desired
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.records import LocalSession
from agent_runner.sync.outbox import open_item
from agent_runner.vm import records as vm_records
from agent_runner.vm.records import VmRow

from ..reconcile.rig import DEVICE, Env, pid, scope_key

KEY = "test-platform-key-SECRETMARKER-0123456789abcdef"
GOLDEN = "g1"
CLAUDE = "2.1.330"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
ORG = pid("a/org")
WG = pid("a/wg/default")
WG_OTHER = pid("a/wg/one")
OWNER = pid("alice")
TASK = "01930004-0000-7000-8000-000000000001"


def session_id(n: int = 1) -> str:
    return f"01930005-0000-7000-8000-{n:012d}"


def desired(n: int = 1, **overrides: Any) -> Desired:
    fields: dict[str, Any] = {
        "session_id": session_id(n),
        "task_id": TASK,
        "org_id": ORG,
        "wg_id": WG,
        "state": "requested",
        "parent_session_id": None,
        "depth": 0,
        "owner_id": OWNER,
        "model_access_pref": "auto",
        "model_id_pref": None,
    }
    fields.update(overrides)
    return Desired(**fields)


def wire(item: Desired) -> dict[str, Any]:
    """The desired session as `GET /v1/device/state` lists it (`resume_from` only when set)."""
    return {key: value for key, value in item.__dict__.items() if key != "resume_from" or value is not None}


@dataclass
class FakeGuest:
    org_id: str | None = ORG
    wg_id: str | None = WG
    state: str = "assigned"
    calls: list[tuple[str, Any]] = field(default_factory=list)
    fail: dict[str, BaseException] = field(default_factory=dict)
    create_answer: dict[str, Any] = field(default_factory=lambda: {"state": "sandbox_ready"})
    records: list[JournalRecord] = field(default_factory=list)
    acked: int = 0
    head_offset: int = 0
    poll_wait: float = 0.01
    on_credential: Callable[[str], None] | None = None
    closed: int = 0

    async def __aenter__(self) -> FakeGuest:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed += 1

    def _call(self, name: str, value: Any = None) -> None:
        self.calls.append((name, value))
        failure = self.fail.get(name)
        if failure is not None:
            raise failure

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def health(self, *, timeout: float | None = None) -> Health:
        self._call("health")
        return Health(
            state=self.state,
            guest_version="0.1.0",
            claude_version=CLAUDE,
            landlock_abi=4,
            boot_id="b",
            org_id=self.org_id,
            wg_id=self.wg_id,
            sessions=(SessionState("x", "working"),),
        )

    async def create_session(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._call("create_session", dict(request))
        return dict(self.create_answer)

    async def put_credential(self, sid: str, credential: Mapping[str, Any]) -> None:
        self._call("put_credential", (sid, dict(credential)))
        if self.on_credential is not None:
            self.on_credential(sid)

    async def control(self, sid: str, action: str) -> None:
        self._call("control", (sid, action))

    def emit(self, sid: str | None, kind: str, body: Mapping[str, Any], ts: str = "2026-11-21T12:00:01.000Z") -> int:
        seq = (self.records[-1].seq if self.records else 0) + 1
        self.records.append(JournalRecord(seq=seq, ts=ts, session_id=sid, kind=kind, body=dict(body)))
        return seq

    async def journal(self, after: int, *, limit: int = 200, wait: int = 25) -> JournalPage:
        self._call("journal", after)
        found = [record for record in self.records if record.seq > after][:limit]
        if not found:
            await asyncio.sleep(self.poll_wait)
        head = (self.records[-1].seq if self.records else 0) - self.head_offset
        return JournalPage(records=tuple(found), head=head)

    async def ack(self, through: int) -> None:
        self._call("ack", through)
        self.acked = through


@dataclass
class FakeVms:
    db: Any
    guest: FakeGuest
    error: RunnerError | Exception | None = None
    client_error: RunnerError | None = None
    created: int = 0
    touched: list[str] = field(default_factory=list)
    on_ensure: Callable[[], None] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def ensure_vm(
        self,
        org_id: str,
        wg_id: str,
        *,
        session_id: str | None = None,
        prefer: Sequence[str] = (),
        new_allowed: bool = True,
    ) -> VmRow:
        self.calls.append({"session_id": session_id, "prefer": list(prefer), "new_allowed": new_allowed})
        if self.on_ensure is not None:
            self.on_ensure()
        if self.error is not None:
            raise self.error
        row = await self._choose(org_id, wg_id, prefer)
        if session_id is not None:
            await vm_records.assign_session(self.db, session_id, row.id)
        return row

    async def _choose(self, org_id: str, wg_id: str, prefer: Sequence[str]) -> VmRow:
        rows = await vm_records.of_workgroup(self.db, wg_id)
        preferred = [row for vm_id in prefer for row in rows if row.id == vm_id]
        if preferred or rows:
            return await vm_records.update(self.db, (preferred or rows)[0].id, state="running")
        self.created += 1
        vm_id = f"vm-{self.created:08x}"
        return await vm_records.insert(
            self.db,
            vm_id=vm_id,
            lima_name=f"wp-test-arp-{self.created:08x}",
            golden_version=GOLDEN,
            state="running",
            token_sha256="0" * 64,
            cpus=4,
            memory_mib=8192,
            created_at=NOW,
            org_id=org_id,
            wg_id=wg_id,
        )

    async def client(self, row: VmRow) -> Any:
        if self.client_error is not None:
            raise self.client_error
        return self.guest

    async def touch(self, vm_id: str) -> None:
        self.touched.append(vm_id)

    async def wait_reconciled(self) -> None:
        return None


async def add_golden(env: Env, version: str = GOLDEN, claude: str = CLAUDE) -> None:
    async with env.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
            " state) VALUES (%s, %s, %s, '0.1.0', 'sha256:x', %s, 'ready') ON CONFLICT DO NOTHING",
            (version, f"wp-test-golden-{version}", claude, NOW),
        )


async def verify_world(env: Env) -> None:
    """Reconcile steps 1-3: verified logs, wraps (keys) of the rig's world."""
    engine = Engine(
        env.context,
        [
            ReconcileStep(10, "state", state.fetch_state),
            ReconcileStep(20, "logs", logs.sync_logs),
            ReconcileStep(30, "wraps", wraps.step),
        ],
    )
    run = await engine.run_once()
    assert run is not None
    assert run.halted is None, env.log()


@dataclass
class Kit:
    env: Env
    guest: FakeGuest
    vms: FakeVms
    session_env: SessionEnv
    lines: list[str]
    first_prompts: list[str] = field(default_factory=list)

    async def outbox(self) -> list[tuple[str, dict[str, Any]]]:
        """`(item_kind, body)` of every local outbox item, opened with the storage key."""
        rows = await self.env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
        return [(kind, open_item(self.env.sync.storage_key, seq, kind, bytes(blob))) for seq, kind, blob in rows]

    async def transitions(self, sid: str | None = None) -> list[tuple[str, str | None]]:
        found = [(b["state"], b["reason"]) for k, b in await self.outbox() if k == "session_transition"]
        if sid is None:
            return found
        return [
            (b["state"], b["reason"])
            for k, b in await self.outbox()
            if k == "session_transition" and b["session_id"] == sid
        ]

    async def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [body for k, body in await self.outbox() if k == kind]

    async def stored(self, sid: str) -> LocalSession:
        from agent_runner.sessions import records  # noqa: PLC0415

        async with self.env.pool.connection() as conn:
            found = await records.get(conn, sid)
        assert found is not None
        return found

    async def pushed_events(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Push the outbox to the hub, then `(header, body)` of every event the hub stored, decrypted."""
        await self.env.sync.pusher.drain()
        found = []
        for item in self.env.hub.items():
            if item["item_kind"] != "event":
                continue
            event = item["body"]["event"]
            header = event["header"]
            key = scope_key(header["scope"], header["scope_id"], header["epoch"])
            device_sig = b64u_decode(_device_sig_public())
            opened = parse_json(open_event(key, event, device_sig))
            assert isinstance(opened, dict)
            found.append((header, opened))
        return found


def _device_sig_public() -> str:
    from ..reconcile.rig import device_keys  # noqa: PLC0415

    return device_keys().sig_public


async def kit(
    env: Env,
    *,
    device_kind: str = "platform",
    key: str | None = KEY,
    guest: FakeGuest | None = None,
    prompt: InitialPrompt | None = None,
    desired_state: Mapping[str, Any] | None = None,
    refresh: Callable[[], Any] | None = None,
) -> Kit:
    await verify_world(env)
    await add_golden(env)
    guest = guest or FakeGuest()
    vms = FakeVms(db=env.pool, guest=guest)
    lines: list[str] = []
    asked: list[str] = []

    async def first_prompt(_context: Any, session: LocalSession) -> InitialPrompt | None:
        asked.append(session.id)
        return prompt

    async def refresh_profiles() -> None:
        if refresh is not None:
            await refresh()

    session_env = SessionEnv(
        db=env.pool,
        outbox=env.sync,
        vms=vms,
        book=book_for(env.context),
        device_id=pid(DEVICE),
        device_kind=device_kind,
        catalog=load_catalog(None),
        strategies=BUILTIN,
        storage_key=env.sync.storage_key,
        platform_key=lambda: key,
        upstream_openrouter="https://openrouter.ai/api",
        context=env.context,
        notify=env.sync.pusher.notify,
        first_prompt=first_prompt,
        refresh_profiles=refresh_profiles,
        log=lines.append,
        clock=env.clock,
        wall=NOW.timestamp,
        desired=desired_state,
    )
    return Kit(env=env, guest=guest, vms=vms, session_env=session_env, lines=lines, first_prompts=asked)


def guest_unreachable() -> GuestError:
    from agent_runner.guestlink.errors import GuestUnreachable  # noqa: PLC0415

    return GuestUnreachable("x")


def new_id() -> str:
    return str(uuid.uuid4())
