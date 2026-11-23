"""Host side shared by the probes that drive real VMs on the runner device (PRB-01).

* `runner_vms(golden, prefix, pool_size)` - an own PostgreSQL cluster in a temporary directory with the runner
  schema, the given golden registered as `ready`, and a `VmManager` over real `limactl` (the code of VM-02). The
  Lima wrapper and the guest client are subclassed only to take timestamps (`Marks`); every call does what the
  runner does.
* `setup_double(machine, name, src)` - copies `agent_doubles` (stdlib only) into the VM and starts the model double
  as the Lima default user on 127.0.0.1 inside the VM (the gateway accepts plain HTTP only to loopback); returns its
  port. Test setup, excluded from the measured time.
* `session_request(...)`, `credential(...)` - the bodies of `POST /v1/sessions` and `PUT .../credential`.
* `Watcher` - follows the guest journal (long poll) and marks `agent_starting`, the first hook, `prompt_accepted`
  and the first answered model request of one session at the host's receive time; keeps the guest's own times.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_cli.pg import create_database, ensure_cluster, stop_cluster
from agent_runner.guestlink.client import GuestClient, Health
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.vm import lima
from agent_runner.vm.capacity import physical_memory_mib
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import TokenStore

ORG = "01930001-0000-7000-8000-000000000001"
GOLDEN_VERSION = "probe"
DOUBLE_DIR = "/tmp/prb"
DOUBLE_ARCHIVE = "/tmp/prb-doubles.tgz"
DOUBLE_MODEL = "double-model"
MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
LIMITS = {"memory_max": 4 * 1024**3, "cpu_quota": 200, "tasks_max": 256, "runtime_max_sec": 8 * 3600}


def uuid7() -> str:
    """A UUIDv7 (millisecond time + random), the ID format of the contracts."""
    millis = time.time_ns() // 1_000_000
    raw = bytearray(millis.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))


def emit(kind: str, **fields: Any) -> None:
    print(json.dumps({"kind": kind, **fields}, sort_keys=True, default=str), flush=True)


class Marks:
    """Host monotonic timestamps relative to `begin()`; the first value of a mark wins, nothing before `begin`."""

    def __init__(self) -> None:
        self._t0: float | None = None
        self.values: dict[str, float] = {}

    def begin(self) -> None:
        self._t0 = time.monotonic()
        self.values = {"begin": 0.0}

    def stop(self) -> None:
        self._t0 = None

    def set(self, name: str) -> None:
        if self._t0 is not None and name not in self.values:
            self.values[name] = round(time.monotonic() - self._t0, 4)


class TimedLima(lima.Lima):
    def __init__(self, marks: Marks, prefix: str) -> None:
        super().__init__(lima.run_limactl, prefix=prefix)
        self.marks = marks

    def clone(self, source: str, name: str, *, sets: Sequence[str] = ()) -> None:
        super().clone(source, name, sets=sets)
        self.marks.set("cloned")

    def start(self, name: str, *, params: Mapping[str, str] | None = None, timeout: float | None = None) -> None:
        super().start(name, params=params, timeout=timeout)
        self.marks.set("started")


class TimedClient(GuestClient):
    marks: Marks

    async def health(self, *, timeout: float | None = None) -> Health:
        found = await super().health(timeout=timeout)
        if found.state == "unassigned":
            self.marks.set("healthy")
        return found


@dataclass
class Runner:
    manager: VmManager
    machine: TimedLima
    marks: Marks
    env: VmEnv


@contextlib.asynccontextmanager
async def runner_vms(golden: str, prefix: str, pool_size: int) -> AsyncIterator[Runner]:
    with tempfile.TemporaryDirectory(prefix="prb01-") as scratch:
        base = Path(scratch)
        admin = ensure_cluster(base / "pg")
        try:
            dsn = create_database(admin, "prb01")
            if migrate(dsn).status != "applied":
                raise SystemExit("runner schema not applied")
            db = await open_pool(dsn)
            try:
                async with db.connection() as conn:
                    await conn.execute(
                        "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest,"
                        " built_at, state) VALUES (%s, %s, '2.1.330', '0.1.0', 'sha256:probe', now(), 'ready')",
                        (GOLDEN_VERSION, golden),
                    )
                marks = Marks()
                machine = TimedLima(marks, prefix)

                def factory(path: str, token: str) -> GuestClient:
                    client = TimedClient(path, token)
                    client.marks = marks
                    return client

                settings = VmSettings(
                    cpus=4, memory_mib=8192, disk_gib=20, pool_size=pool_size, memory_reserve_mib=32768,
                    vm_idle_seconds=1800,
                )  # fmt: skip
                env = VmEnv(
                    lima=machine, db=db, settings=settings, tokens=TokenStore(base / "vm"),
                    physical_mib=physical_memory_mib, client_factory=factory,
                    log=lambda name, fields: emit("log", name=name, **fields),
                )  # fmt: skip
                manager = VmManager(env)
                await manager.reconcile()
                try:
                    yield Runner(manager=manager, machine=machine, marks=marks, env=env)
                finally:
                    for instance in machine.list():
                        if instance.name.startswith(prefix + "arp-"):
                            machine.delete(instance.name)
            finally:
                await db.close()
        finally:
            stop_cluster(base / "pg")


def doubles_archive(src: Path, target: Path) -> Path:
    """`agent_doubles` of the checkout as a tar.gz (copied into each VM)."""
    with tarfile.open(target, "w:gz") as archive:
        archive.add(src / "agent_doubles", arcname="agent_doubles", filter=_no_caches)
    return target


def _no_caches(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if "__pycache__" in info.name else info


def setup_double(machine: lima.Lima, name: str, archive: Path, scenario: str = "hello") -> int:
    machine.copy_to(str(archive), name, DOUBLE_ARCHIVE)
    script = (
        f"rm -rf {DOUBLE_DIR} && mkdir -p {DOUBLE_DIR} && tar -xzf {DOUBLE_ARCHIVE} -C {DOUBLE_DIR} && cd {DOUBLE_DIR}"
        f" && (setsid nohup python3 -m agent_doubles.model --scenario {scenario} --port 0"
        f" --port-file {DOUBLE_DIR}/port --journal {DOUBLE_DIR}/journal.jsonl >{DOUBLE_DIR}/double.log 2>&1"
        " </dev/null &) && i=0 && while [ ! -s port ] && [ $i -lt 200 ]; do i=$((i+1)); sleep 0.05; done && cat port"
    )
    done = machine.shell(name, ["sh", "-c", script])
    return int(done.stdout.strip())


def session_request(
    session_id: str, prompt: str, *, access: str, model_id: str | None, env: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "task_id": uuid7(),
        "parent_session_id": None,
        "depth": 0,
        "harness": {"context_markdown": "# Probe session\n\nEarly measurement of the session start.\n"},
        "model": {"access": access, "model_id": model_id, "env": dict(env)},
        "initial_prompt": {"prompt_id": uuid7(), "text": prompt},
        "limits": dict(LIMITS),
    }


def double_session(session_id: str, prompt: str) -> dict[str, Any]:
    env = dict.fromkeys(MODEL_ENV_KEYS, DOUBLE_MODEL)
    return session_request(session_id, prompt, access="platform", model_id=DOUBLE_MODEL, env=env)


def credential(
    upstream: str,
    auth_value: str,
    *,
    access: str,
    models: Sequence[tuple[str, int]],
    auth_header: str = "authorization",
) -> dict[str, Any]:
    return {
        "upstream_base_url": upstream,
        "auth_header": auth_header,
        "auth_value": auth_value,
        "extra_headers": [],
        "provider_allowlist": [],
        "access": access,
        "models_allowed": [{"model_id": model, "context_window": window} for model, window in models],
    }


@dataclass
class Watcher:
    """Follows the journal for one session (see the module docstring)."""

    client: GuestClient
    session_id: str
    marks: Marks
    after: int
    need_answer: bool = True
    done: asyncio.Event = field(default_factory=asyncio.Event)
    failure: str | None = None
    hook_event: str | None = None
    guest_at: dict[str, str] = field(default_factory=dict)
    usage: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)

    async def run(self) -> None:
        """Until cancelled (the caller waits for `done`, then may keep it running to collect later hooks)."""
        while True:
            page = await self.client.journal(self.after, wait=5)
            for record in page.records:
                self.after = record.seq
                if record.session_id == self.session_id:
                    self._handle(record.kind, dict(record.body), record.ts)

    def _handle(self, kind: str, body: dict[str, Any], ts: str) -> None:
        if kind == "session.state":
            state = str(body.get("state"))
            # The record time `ts` for every mark (the first hook has no `at`), so guest durations compare alike.
            self.guest_at.setdefault(state, ts)
            if state in {"agent_starting", "prompt_accepted"}:
                self.marks.set(state)
            if state in {"failed", "prompt_refused", "stopped"}:
                self.failure = f"{state}:{body.get('reason')}"
                self.done.set()
        elif kind == "session.hook":
            self.hooks.append(str(body.get("event")))
            if self.hook_event is None:
                self.hook_event = str(body.get("event"))
                self.guest_at["first_hook"] = ts
                self.marks.set("first_hook")
        elif kind == "usage":
            self.usage.append(body)
            if body.get("status") == 200:
                self.marks.set("answered")
        reached = "prompt_accepted" in self.marks.values
        if reached and ("answered" in self.marks.values or not self.need_answer):
            self.done.set()
