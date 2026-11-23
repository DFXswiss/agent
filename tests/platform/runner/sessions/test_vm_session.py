"""Integration on the runner device (marker `vm`): a real session in a real pool VM against the model double.

Collected only with `-m vm` (see `conftest.py`); run on the runner device from the repository root:

    AGENT_RUNNER_TEST_GOLDEN=<stopped golden> AGENT_RUNNER_VM_PREFIX=<prefix> \
        pytest -c tests/platform/pytest.ini --confcutdir=tests/platform -m vm tests/platform/runner/sessions -s

With real `limactl`, the real guest of the golden and the runner's own code (store, sync runtime and verified logs of
the RUN-03 rig, `VmManager` of VM-02, `SessionService` of RUN-04):

1. the warm pool is filled with one VM (clone, start with the control token, sandbox self-test) and the model double
   is started inside that VM on loopback (`--access openrouter`: it answers only requests that carry the platform key
   as bearer token, so the run proves the key injection of the guest gateway) - test setup, not measured;
2. the hub schedules a session (desired state `requested`); `SessionService` starts it: pool assignment, health,
   platform access, `POST /v1/sessions` with the initial prompt "What is 2+2?", credential, agent start;
3. the journal loop of the VM reads `prompt_delivering`, `working` (prompt accepted by the UserPromptSubmit hook),
   the answer (`session.output`) and the gateway's `usage` and turns them into transitions, events and usage items,
   which are pushed to the rig's hub (contract-checked there);
4. the measurements of every start step (`runner.session start_step ...`) and the total to `working` are written to
   `.wp/vm-session.json` below the current directory (git-ignored; `remote.sh --fetch .wp/vm-session.json`).

Every instance starts with `AGENT_RUNNER_VM_PREFIX` and is deleted at the end, also on failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.crypto.ids import uuid7
from agent_runner.model.selection import BUILTIN, parse_catalog
from agent_runner.orgs.book import book_for
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.service import SessionService
from agent_runner.sessions.stop import stop_session
from agent_runner.vm import lima
from agent_runner.vm.capacity import physical_memory_mib
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import TokenStore

from ..reconcile.rig import DEVICE, Rig, pid, standard_world
from .kit import ORG, WG, desired, verify_world

pytestmark = pytest.mark.vm

GOLDEN_ENV = "AGENT_RUNNER_TEST_GOLDEN"
PREFIX_ENV = "AGENT_RUNNER_VM_PREFIX"
KEY = "vm-test-platform-key-" + "0" * 16
MODEL = "double-model"
DOUBLE_DIR = "/tmp/run04-double"
DOUBLE_ARCHIVE = "/tmp/run04-doubles.tgz"
SRC = Path(__file__).resolve().parents[4] / "src"
CATALOG = {
    "version": 1,
    "access": {
        "platform": {"default": MODEL, "allowed": [MODEL], "providers_allowed": []},
        "api_key": {"default": None, "allowed": []},
        "subscription": {"default": None, "allowed": []},
    },
    "models": {MODEL: {"context_window": 200000, "max_output": 32000}},
}
TIMEOUT_S = 90.0
STEP_RE = re.compile(r"start_step session_id=\S+ step=(\w+) path=(\w+) ms=(\d+)")
TOTAL_RE = re.compile(r"started session_id=\S+ path=(\w+) total_ms=(\d+)")


def _no_caches(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if "__pycache__" in info.name else info


def _archive(target: Path) -> Path:
    with tarfile.open(target, "w:gz") as archive:
        archive.add(
            SRC / "agent_doubles", arcname="agent_doubles", filter=lambda i: None if "__pycache__" in i.name else i
        )
    return target


def start_double(machine: lima.Lima, name: str, archive: Path) -> int:
    """The model double inside the VM on loopback (scenario `hello`, OpenRouter bearer check); returns its port."""
    machine.copy_to(str(archive), name, DOUBLE_ARCHIVE)
    script = (
        f"rm -rf {DOUBLE_DIR} && mkdir -p {DOUBLE_DIR} && tar -xzf {DOUBLE_ARCHIVE} -C {DOUBLE_DIR} && cd {DOUBLE_DIR}"
        f" && (MODEL_DOUBLE_TOKEN={KEY} setsid nohup python3 -m agent_doubles.model --scenario hello"
        " --access openrouter --token-env MODEL_DOUBLE_TOKEN --port 0"
        f" --port-file {DOUBLE_DIR}/port --journal {DOUBLE_DIR}/journal.jsonl >{DOUBLE_DIR}/double.log 2>&1"
        " </dev/null &) && i=0 && while [ ! -s port ] && [ $i -lt 200 ]; do i=$((i+1)); sleep 0.05; done && cat port"
    )
    return int(machine.shell(name, ["sh", "-c", script]).stdout.strip())


async def wait_for(check: Any, timeout: float = TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.05)
    return False


def test_real_session_in_a_pool_vm_reaches_working(store_dsn: str) -> None:
    golden = os.environ.get(GOLDEN_ENV, "")
    prefix = os.environ.get(PREFIX_ENV, "")
    if not golden or not prefix:
        pytest.fail(f"{GOLDEN_ENV} and {PREFIX_ENV} are required for the vm integration run")
    lines: list[str] = []

    async def main() -> dict[str, Any]:
        async with Rig(store_dsn).env(standard_world()) as env:
            async with env.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest,"
                    " built_at, state) VALUES ('vmtest', %s, '2.1.330', '0.1.0', 'sha256:vmtest', now(), 'ready')",
                    (golden,),
                )
            machine = lima.Lima(lima.run_limactl, prefix=prefix)
            scratch = Path(tempfile.mkdtemp(prefix="run04-"))
            settings = VmSettings(cpus=4, memory_mib=8192, disk_gib=20, pool_size=1, memory_reserve_mib=32768,
                                  vm_idle_seconds=1800)  # fmt: skip
            vm_env = VmEnv(
                lima=machine,
                db=env.pool,
                settings=settings,
                tokens=TokenStore(scratch / "vm"),
                physical_mib=physical_memory_mib,
                log=lambda name, fields: lines.append(f"runner.{name} {fields}"),
            )
            manager = VmManager(vm_env)
            try:
                return await scenario(env, manager, machine, scratch, lines)
            finally:
                for instance in machine.list():
                    if instance.name.startswith(prefix + "arp-"):
                        machine.delete(instance.name)

    result = asyncio.run(main())
    report = Path(".wp") / "vm-session.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    # the session may already be idle again (Stop hook after the answer, GST-06)
    assert result["state"] in ("working", "idle")
    assert result["transitions"][:6] == [
        "vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering", "working"
    ]  # fmt: skip
    assert result["path"] == "pool"
    assert result["total_ms"] < 10_000


async def scenario(env: Any, manager: VmManager, machine: lima.Lima, scratch: Path, lines: list[str]) -> dict[str, Any]:
    await manager.reconcile()
    await verify_world(env)
    assert await manager.refill() == 1
    (pool_vm,) = [row for row in await _vms(env) if row["state"] == "pool"]
    port = start_double(machine, pool_vm["lima_name"], _archive(scratch / "doubles.tgz"))
    prompt = InitialPrompt(prompt_id=uuid7(), text="What is 2+2?")

    async def first_prompt(_context: Any, _session: LocalSession) -> InitialPrompt:
        return prompt

    session_env = SessionEnv(
        db=env.pool,
        outbox=env.sync,
        vms=manager,
        book=book_for(env.context),
        device_id=pid(DEVICE),
        device_kind="platform",
        catalog=parse_catalog(CATALOG),
        strategies=BUILTIN,
        storage_key=env.sync.storage_key,
        platform_key=lambda: KEY,
        upstream_openrouter=f"http://127.0.0.1:{port}",
        context=env.context,
        notify=env.sync.pusher.notify,
        first_prompt=first_prompt,
        log=lines.append,
    )
    service = SessionService(session_env, handlers=registry.journal_handlers())
    stopping = asyncio.Event()
    journals = asyncio.ensure_future(service.run_journals(stopping))
    item = desired(1, session_id=uuid7(), task_id=uuid7(), org_id=ORG, wg_id=WG)
    try:
        assert service.schedule_start(item)

        async def working() -> bool:
            async with env.pool.connection() as conn:
                found = await records.get(conn, item.session_id)
            return found is not None and found.state in ("working", "idle", "failed")

        reached = await wait_for(working)
        await service.idle()

        async def answered() -> bool:
            kinds = await _outbox_kinds(env)
            return "usage" in kinds and kinds.count("event") >= 1

        await wait_for(answered, 30.0)
        async with env.pool.connection() as conn:
            final = await records.get(conn, item.session_id)
        assert final is not None
        await env.sync.pusher.drain()
        kinds = [entry["item_kind"] for entry in env.hub.items()]
        transitions = [
            entry["body"]["state"] for entry in env.hub.items() if entry["item_kind"] == "session_transition"
        ]
        steps = {match.group(1): int(match.group(3)) for match in map(STEP_RE.search, lines) if match}
        total = next((m for m in map(TOTAL_RE.search, lines) if m), None)
        report = service.reports[0] if service.reports else None
        result = {
            "reached": reached,
            "state": final.state,
            "path": report.path if report else None,
            "steps_ms": steps,
            "total_ms": int(total.group(2)) if total else None,
            "transitions": transitions,
            "pushed": {kind: kinds.count(kind) for kind in sorted(set(kinds))},
            "claude_session_id_known": final.claude_session_id is not None,
            "log": [line for line in lines if "start_" in line or "journal" in line][-40:],
        }
        await stop_session(session_env, item.session_id)
        return result
    finally:
        stopping.set()
        await journals


async def _vms(env: Any) -> list[dict[str, Any]]:
    rows = await env.rows("SELECT id, lima_name, state FROM runner.vm ORDER BY created_at")
    return [{"id": row[0], "lima_name": row[1], "state": row[2]} for row in rows]


async def _outbox_kinds(env: Any) -> list[str]:
    return [str(row[0]) for row in await env.rows("SELECT item_kind FROM runner.outbox ORDER BY origin_seq")]
