"""Fake Lima VMs with real guest control processes (VM-02 tests of pool, lifecycle and tasks).

`LimaWithGuests` extends the recorded-output `FakeLimactl` of `conftest.py`: `clone` writes the instance's
`lima.yaml` (with the empty control-token parameter of the golden template), `start` takes the token from the
`--set '.param.AGENT_GUEST_CONTROL_TOKEN="..."'` argument, writes it into `lima.yaml` like Lima does and boots a real
`agent_guest.control.ControlService` (GST-01) on `<instance dir>/sock/agent-guest.sock` with that token - the
`agent-guest-harden.service` step. `stop` halts the guest (its journal and assignment survive, as on the VM's disk),
`delete` removes it. The pool self-test result is answered from `selftest`.

Instance directories live in a short directory under /tmp (Unix socket paths are limited to 104 bytes on macOS).
"""

from __future__ import annotations

import asyncio
import grp
import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from agent_guest.config import load_config
from agent_guest.control import ControlService
from agent_runner.store.pool import open_pool
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.lima import Lima
from agent_runner.vm.pool import SELFTEST_RESULT, VmEnv, VmSettings
from agent_runner.vm.records import TokenStore, VmRow

from .conftest import SELFTEST_OK, FakeLimactl

T = TypeVar("T")
TOKEN_SET_RE = re.compile(r'^\.param\.AGENT_GUEST_CONTROL_TOKEN="([^"]*)"$')
LIMA_YAML = """vmType: vz
cpus: 4
memory: 8GiB
param:
  AGENT_GUEST_CONTROL_TOKEN: ""
provision:
- mode: system
"""


@dataclass
class GuestFarm:
    """One real control process per running instance."""

    contracts: Path
    services: dict[str, ControlService] = field(default_factory=dict)
    homes: dict[str, Path] = field(default_factory=dict)
    # The guest sets the process umask (0o177) while it binds its sockets; in a real VM every guest is its own
    # process, here the guests of parallel clones start in threads of one process. Every file operation of the fake
    # (instance directories, `lima.yaml`, the guest's home and socket directory, deletion) and the whole boot hold
    # this lock, so no directory is created inside another guest's umask window (it would lack the search bit and
    # the guest could neither bind nor be reached: refill and the pool start path failed at once under load).
    lock: threading.RLock = field(default_factory=threading.RLock)

    def home(self, directory: Path) -> Path:
        return directory / "guest"

    def boot(self, name: str, directory: Path, token: str | None) -> ControlService:
        with self.lock:
            return self._boot(name, directory, token)

    def _boot(self, name: str, directory: Path, token: str | None) -> ControlService:
        home = self.home(directory)
        home.mkdir(parents=True, exist_ok=True)
        (directory / "sock").mkdir(parents=True, exist_ok=True)
        token_file = home / "control-token"
        if token:
            token_file.write_text(token, encoding="ascii")
        (home / "boot_id").write_text(str(uuid.uuid4()) + "\n", encoding="ascii")
        (home / "build.json").write_text(json.dumps({"landlock_abi": 4}), encoding="utf-8")
        (home / "install.json").write_text(json.dumps({"version": "2.1.330"}), encoding="utf-8")
        group = grp.getgrgid(os.getgid()).gr_name
        config = load_config(
            {
                "AGENT_GUEST_CONTROL_SOCKET": str(directory / "sock" / "agent-guest.sock"),
                "AGENT_GUEST_API_SOCKET": str(home / "api.sock"),
                "AGENT_GUEST_CONTROL_GROUP": group,
                "AGENT_GUEST_API_GROUP": group,
                "AGENT_GUEST_CONTROL_TOKEN_FILE": str(token_file),
                "AGENT_GUEST_JOURNAL": str(home / "state" / "journal.db"),
                "AGENT_GUEST_API_PORT": "0",
                "AGENT_GUEST_BUILD_FILE": str(home / "build.json"),
                "AGENT_GUEST_CLAUDE_INSTALL_FILE": str(home / "install.json"),
                "AGENT_GUEST_BOOT_ID_FILE": str(home / "boot_id"),
                "AGENT_GUEST_CONTRACTS": str(self.contracts),
            }
        )
        service = ControlService(config)
        service.start()
        self.services[name] = service
        self.homes[name] = home
        return service

    def halt(self, name: str) -> None:
        service = self.services.pop(name, None)
        if service is not None:
            service.stop()

    def close(self) -> None:
        for name in list(self.services):
            self.halt(name)


@dataclass
class LimaWithGuests(FakeLimactl):
    farm: GuestFarm | None = None
    selftest: tuple[int, str, str] = (0, SELFTEST_OK + "\n", "")
    # Instances whose guest does not come up at start (unhealthy VM).
    dead: set[str] = field(default_factory=set)
    starts: list[str] = field(default_factory=list)

    def directory(self, name: str) -> Path:
        return Path(self.instance_dir(name))

    def _clone(self, args: list[str]) -> tuple[int, str, str]:
        assert self.farm is not None
        with self.farm.lock:
            return self._clone_locked(args)

    def _clone_locked(self, args: list[str]) -> tuple[int, str, str]:
        result = super()._clone(args)
        if result[0] == 0:
            directory = self.directory(args[-1])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "lima.yaml").write_text(LIMA_YAML, encoding="utf-8")
        return result

    def _start_stop(self, args: list[str]) -> tuple[int, str, str]:
        assert self.farm is not None
        with self.farm.lock:
            return self._start_stop_locked(args)

    def _start_stop_locked(self, args: list[str]) -> tuple[int, str, str]:
        name = args[-1]
        if name not in self.instances:
            return super()._start_stop(args)
        assert self.farm is not None
        item = self.instances[name]
        directory = self.directory(name)
        if args[0] == "stop":
            self.farm.halt(name)
            item["status"] = "Stopped"
            return 0, "", ""
        token = None
        for value in args:
            match = TOKEN_SET_RE.fullmatch(value)
            if match:
                token = match.group(1)
        if token is not None:
            path = directory / "lima.yaml"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'AGENT_GUEST_CONTROL_TOKEN: ""', f'AGENT_GUEST_CONTROL_TOKEN: "{token}"'
                ),
                encoding="utf-8",
            )
        self.starts.append(name)
        item["status"] = "Running"
        if name not in self.dead:
            self.farm.boot(name, directory, token)
        return 0, "", ""

    def _delete(self, args: list[str]) -> tuple[int, str, str]:
        assert self.farm is not None
        with self.farm.lock:
            self.farm.halt(args[-1])
            result = super()._delete(args)
            shutil.rmtree(self.directory(args[-1]), ignore_errors=True)
            return result

    def _shell(self, args: list[str]) -> tuple[int, str, str]:
        if SELFTEST_RESULT in args:
            return self.selftest
        return super()._shell(args)


PREFIX = "wp-test-"
GOLDEN = "g1"
ORG = "01930001-0000-7000-8000-000000000001"
WG_A = "01930002-0000-7000-8000-00000000000a"
WG_B = "01930002-0000-7000-8000-00000000000b"
WG_C = "01930002-0000-7000-8000-00000000000c"
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
SETTINGS = VmSettings(cpus=4, memory_mib=8192, disk_gib=20, pool_size=2, memory_reserve_mib=32768, vm_idle_seconds=1800)


@dataclass
class Harness:
    """Builds `VmEnv`/`VmManager` over the fake Lima, a real store pool and the fake clock (see `run`)."""

    fake: LimaWithGuests
    dsn: str
    tokens_dir: Path
    clock: Any
    settings: VmSettings = SETTINGS
    # 32 GiB reserve + 4 slots of 8 GiB.
    physical_mib: int = 32768 + 4 * 8192
    now: datetime = NOW
    events: list[dict[str, Any]] = field(default_factory=list)
    logs: list[tuple[str, dict[str, str | int]]] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The token store creates its missing parents with the process umask; created while a guest of this process
        # binds its sockets (umask 0o177), a parent would lack the search bit and every token write would fail.
        self.tokens_dir.parent.mkdir(parents=True, exist_ok=True)

    def env(self, db: Any) -> VmEnv:
        async def sink(body: dict[str, Any]) -> None:
            self.events.append(body)

        def log(name: str, fields: dict[str, str | int]) -> None:
            self.logs.append((name, fields))

        return VmEnv(
            lima=Lima(self.fake, prefix=PREFIX),
            db=db,
            settings=self.settings,
            tokens=TokenStore(self.tokens_dir),
            physical_mib=lambda: self.physical_mib,
            sink=sink,
            log=log,
            now=lambda: self.now,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def run(self, body: Callable[[VmManager], Awaitable[T]], *, reconcile: bool = True) -> T:
        """Run `body(manager)` in a fresh event loop with an open store pool (reconciled first by default)."""

        async def main() -> T:
            db = await open_pool(self.dsn)
            try:

                async def on_unreachable(row: VmRow) -> None:
                    self.unreachable.append(row.id)

                manager = VmManager(self.env(db), on_unreachable=on_unreachable)
                if reconcile:
                    await manager.reconcile()
                return await body(manager)
            finally:
                await db.close()

        return asyncio.run(main())

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            cursor = conn.execute(query, params)
            return list(cursor.fetchall()) if cursor.description else []

    def add_golden(self, version: str = GOLDEN, *, state: str = "ready", minute: int = 0) -> str:
        name = f"{PREFIX}golden-{version}"
        self.fake.add(name)
        self.sql(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
            " state) VALUES (%s, %s, '2.1.330', '0.1.0', 'sha256:x', %s, %s)",
            (version, name, NOW.replace(minute=minute), state),
        )
        return name

    def vms(self) -> list[dict[str, Any]]:
        return self.sql(
            "SELECT id, lima_name, state, wg_id::text AS wg_id, golden_version FROM runner.vm ORDER BY created_at, id"
        )

    def live(self) -> list[dict[str, Any]]:
        return [row for row in self.vms() if row["state"] != "deleted"]

    def session(self, vm_id: str, wg_id: str, state: str, *, at: datetime = NOW) -> str:
        session_id = str(uuid.uuid4())
        self.sql(
            "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, state, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id, str(uuid.uuid4()), ORG, wg_id, vm_id, state, at),
        )
        return session_id
