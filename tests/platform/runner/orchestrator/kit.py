"""Test kit of the orchestrator suites (KOR-57, 05-runner.md section 17).

Real: the store (migrated PostgreSQL), the session and VM rows, the crypto of the keys, the device's and the task
owner's signatures (the identities of the log vectors). Doubles: a guest that draws real agent keys and records what
it was handed, a hub that checks the device's signature over the keys like the hub does and serves the session view,
and an org book with the verified state of one private organization.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from agent_runner.crypto import agent as agent_format
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.state import Principal
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.orchestrator.env import OrchestratorEnv
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.store.pool import open_pool
from agent_runner.vm import records as vm_records

ORG = v.pid("orch/org")
WG = v.pid("orch/wg")
TASK = v.pid("orch/task")
OWNER = v.pid("alice")
OTHER = v.pid("bob")
DEVICE = v.pid("rdev")
HUB_URL = "https://hub.example.org"
HUB_KEY = b64u_encode(bytes(range(32)))
ROOT_KEY = b64u_encode(bytes(range(32, 64)))
WALL = 1_795_000_000.0
VM = "vm-0a0b0c0d"
OTHER_VM = "vm-0e0f0a0b"
Json = dict[str, Any]


def session_id(n: int) -> str:
    return v.pid(f"orch/session/{n}")


def principal(label: str, kind: str = "user") -> Principal:
    pub = v.identity(label).public()
    return Principal(id=v.pid(label), kind=kind, role="admin", kem=pub["kem"], sig=pub["sig"], joined_seq=0)


@dataclass
class OrgStateDouble:
    private: bool = True
    members: dict[str, Principal] = field(default_factory=lambda: {OWNER: principal("alice")})

    def member(self, who: str) -> Principal | None:
        return self.members.get(who)

    @property
    def principals(self) -> dict[str, Principal]:
        return self.members


@dataclass
class Book:
    org_device: bool = False
    state: OrgStateDouble = field(default_factory=OrgStateDouble)
    acting: bool = True

    def log(self, org: str) -> Any:
        return SimpleNamespace(state=self.state) if self.acting and org == ORG else None


@dataclass
class Guest:
    """The guest's orchestrator routes and the relay answers (05-runner.md section 17.3)."""

    calls: list[tuple[str, Any]] = field(default_factory=list)
    agent: Identity = field(default_factory=agent_format.new_agent)
    pairing_id: str = field(default_factory=uuid7)
    keys_answer: Callable[[Json], Json] | None = None
    fail: dict[str, Exception] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    """Calls that always fail (`GuestUnreachable`)."""
    answered: asyncio.Event = field(default_factory=asyncio.Event)

    def keys(self) -> Json:
        return {
            "pairing_id": self.pairing_id,
            "pub": self.agent.public(),
            "pop": agent_format.sign_pop(self.agent, self.pairing_id),
            "fp": agent_format.fingerprint(self.agent.kem_public(), self.agent.sig_public()),
        }

    def _maybe_fail(self, name: str) -> None:
        if name in self.failing:
            self.answered.set()
            raise GuestUnreachable(name)
        if name in self.fail:
            raise self.fail.pop(name)

    async def orchestrator_keys(self, sid: str, request: Json) -> Json:
        self.calls.append(("keys", (sid, request)))
        self._maybe_fail("keys")
        answer = self.keys()
        return self.keys_answer(answer) if self.keys_answer is not None else answer

    async def orchestrator_end(self, sid: str, pairing_id: str) -> None:
        self.calls.append(("end", (sid, pairing_id)))
        self._maybe_fail("end")

    async def orchestrator_handover(self, sid: str, request: Json) -> None:
        self.calls.append(("handover", (sid, request)))
        self._maybe_fail("handover")

    async def answer_relay(self, pending_id: str, answer: Json) -> None:
        self.calls.append(("relay", (pending_id, answer)))
        self._maybe_fail("relay")
        self.answered.set()

    async def __aenter__(self) -> Guest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def of(self, name: str) -> list[Any]:
        return [value for kind, value in self.calls if kind == name]


@dataclass
class Hub:
    """`request(method, target, body)` of the signed hub client: the registration (device signature checked) and
    the session view."""

    device_pub: bytes
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    views: dict[str, Json] = field(default_factory=dict)
    fail: dict[str, Exception] = field(default_factory=dict)
    echo: str | None = None

    async def request(self, method: str, target: str, body: object | None = None) -> dict[str, object]:
        self.requests.append((method, target, body))
        if target in self.fail:
            raise self.fail.pop(target)
        if method == "POST" and target == "/v1/device/orchestrator-pairings":
            found = cast(Json, body)
            kem, sig = b64u_decode(found["pub"]["kem"], 32), b64u_decode(found["pub"]["sig"], 32)
            agent_format.verify_pop(found["pairing_id"], kem, sig, found["pop"])
            text = agent_format.orchestrator_key_text(found["fp"], found["pairing_id"], found["session_id"])
            agent_format.verify_text(self.device_pub, found["device_sig"], text)
            return {"pairing_id": self.echo or found["pairing_id"], "expires_at": "2026-11-25T12:10:00.000Z"}
        return dict(self.views[target])

    def registered(self) -> list[Json]:
        return [cast(Json, body) for method, _target, body in self.requests if method == "POST"]


@dataclass
class Kit:
    env: OrchestratorEnv
    guest: Guest
    hub: Hub
    book: Book
    lines: list[str]

    async def add_session(
        self, n: int = 1, *, state: str = "idle", depth: int = 0, vm_id: str | None = VM, owner: str = OWNER
    ) -> str:
        sid = session_id(n)
        async with self.env.sessions.transaction() as conn:
            await records.insert(
                conn,
                session_id=sid,
                task_id=TASK,
                org_id=ORG,
                wg_id=WG,
                parent_id=None,
                depth=depth,
                owner_id=owner,
                created_at=datetime.fromtimestamp(WALL, UTC),
            )
            await records.update(conn, sid, state=state, vm_id=vm_id)
        return sid

    async def add_vm(self, vm_id: str = VM, state: str = "running") -> None:
        await vm_records.insert(
            self.env.db,
            vm_id=vm_id,
            lima_name=f"agent-{vm_id}",
            golden_version="g1",
            state=state,
            token_sha256="0" * 64,
            cpus=2,
            memory_mib=2048,
            created_at=datetime.fromtimestamp(WALL, UTC),
            org_id=ORG,
            wg_id=WG,
        )

    async def vm(self, vm_id: str = VM) -> Any:
        return await vm_records.get(self.env.db, vm_id)

    async def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        async with self.env.db.connection() as conn:
            cur = await conn.execute(sql, params or None)
            return list(await cur.fetchall())

    def signed_view(
        self, sid: str, pairing_id: str, *, signer: str = "alice", ct: str | None = None, **over: Any
    ) -> Json:
        """The hub's session view with a handover signed by `signer`."""
        ct = ct if ct is not None else b64u_encode(bytes(range(64)))
        text = agent_format.orchestrator_handover_text(ct, pairing_id, sid)
        sig = agent_format.sign_text(v.identity(signer).sig, text)
        view: Json = {
            "session_id": sid,
            "user_id": OWNER,
            "requested_at": "2026-11-25T12:00:00.000Z",
            "requested": False,
            "pairing": {
                "pairing_id": pairing_id,
                "state": "confirmed",
                "expires_at": "2026-11-25T12:10:00.000Z",
                "handover": {"ct": ct, "sig": sig},
            },
        }
        view.update(over)
        return view


class _Vms:
    def __init__(self, guest: Guest) -> None:
        self.guest = guest
        self.clients: list[str] = []

    async def client(self, row: Any) -> Guest:
        self.clients.append(row.id)
        return self.guest


@asynccontextmanager
async def kit(dsn: str, **settings: Any) -> AsyncIterator[Kit]:
    pool = await open_pool(dsn, max_size=4)
    lines: list[str] = []
    guest = Guest()
    device = v.identity("rdev")
    hub = Hub(device_pub=device.sig_public())
    book = Book()
    sessions = SessionEnv(
        db=pool,
        outbox=cast(Any, None),
        vms=cast(Any, _Vms(guest)),
        book=book,
        device_id=DEVICE,
        device_kind="platform",
        catalog=cast(Any, None),
        strategies={},
        storage_key=b"",
        platform_key=lambda **_kw: None,
        upstream_openrouter="",
        log=lines.append,
        wall=lambda: WALL,
    )
    env = OrchestratorEnv(
        sessions=sessions,
        hub=hub,
        signing_key=device.sig,
        hub_url=HUB_URL,
        hub_key=HUB_KEY,
        platform_root_key=ROOT_KEY,
        **settings,
    )
    try:
        yield Kit(env=env, guest=guest, hub=hub, book=book, lines=lines)
    finally:
        await pool.close()


def run(dsn: str, body: Callable[[Kit], Awaitable[None]], **settings: Any) -> None:
    async def main() -> None:
        async with kit(dsn, **settings) as k:
            await body(k)

    asyncio.run(main())
