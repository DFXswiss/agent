"""Integration on the runner device (marker `vm`): a follow-up prompt after an idle suspend is answered by the resumed
Claude Code session (`--resume`), with real Lima, the real guest of the golden and the model double inside the VM.

Collected only with `-m vm` (see `conftest.py`); run on the runner device from the repository root:

    AGENT_RUNNER_TEST_GOLDEN=<stopped golden> AGENT_RUNNER_VM_PREFIX=<prefix> \
        pytest -c tests/platform/pytest.ini --confcutdir=tests/platform -m vm tests/platform/runner/prompts -s

1. a pool VM with the model double inside (scenario: the first question, then a follow-up that must arrive with the
   earlier turn - at least three messages - which only a resumed conversation sends);
2. two signed prompt events of the rig's org are pulled and verified by the real inbox; the first is routed to a new
   session and goes with its start (`first_prompt`), the session answers and becomes `idle`;
3. the `Deliverer` (idle time 3 s) suspends it; the second prompt is routed: the session is resumed with
   `resume_claude_session_id` (`--resume`), the prompt is typed into the resumed TUI, accepted and answered;
4. the results go to `.wp/vm-resume.json` below the current directory (git-ignored).

Every instance starts with `AGENT_RUNNER_VM_PREFIX` and is deleted at the end, also on failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.client import GuestClient
from agent_runner.model.selection import BUILTIN, parse_catalog
from agent_runner.orgs.book import book_for
from agent_runner.prompts.deliver import Deliverer, install
from agent_runner.prompts.first import first_prompt
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.extensions import request_amenders, state_observers
from agent_runner.sessions.service import SessionService
from agent_runner.sessions.stop import stop_session
from agent_runner.sync.outbox import open_item
from agent_runner.vm import lima
from agent_runner.vm.capacity import physical_memory_mib
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import TokenStore

from ..reconcile.rig import DEVICE, Rig, pid, standard_world
from ..sessions.kit import ORG, TASK, WG, desired, verify_world, wire
from ..sessions.test_vm_session import CATALOG, DOUBLE_ARCHIVE, DOUBLE_DIR, KEY, SRC
from .kit import add_prompt, pull

pytestmark = pytest.mark.vm

GOLDEN_ENV = "AGENT_RUNNER_TEST_GOLDEN"
PREFIX_ENV = "AGENT_RUNNER_VM_PREFIX"
IDLE_S = 3.0
TIMEOUT_S = 120.0
SCENARIO = {
    "name": "resume",
    "description": "A question, then a follow-up after a suspend that must carry the earlier turn.",
    "steps": [
        {
            "id": "answer",
            "expect": {"user_text_contains": "2+2", "has_tools": True, "stream": True},
            "reply": {"content": [{"type": "text", "text": "2+2 is 4."}]},
        },
        {
            "id": "follow",
            "expect": {"user_text_contains": "3+3", "has_tools": True, "stream": True, "min_messages": 3},
            "reply": {"content": [{"type": "text", "text": "3+3 is 6, after 2+2 was 4."}]},
        },
    ],
    "side": [{"id": "title", "expect": {"has_tools": False}, "reply": {"content": [{"type": "text", "text": "Math"}]}}],
}


def _no_caches(info: Any) -> Any:
    return None if "__pycache__" in info.name else info


def start_double(machine: lima.Lima, name: str, scratch: Path) -> int:
    import tarfile  # noqa: PLC0415

    archive = scratch / "doubles.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        packed.add(
            SRC / "agent_doubles", arcname="agent_doubles", filter=lambda i: None if "__pycache__" in i.name else i
        )
    scenario = scratch / "resume.json"
    scenario.write_text(json.dumps(SCENARIO), encoding="utf-8")
    machine.copy_to(str(archive), name, DOUBLE_ARCHIVE)
    machine.copy_to(str(scenario), name, "/tmp/run05-resume.json")
    script = (
        f"rm -rf {DOUBLE_DIR} && mkdir -p {DOUBLE_DIR} && tar -xzf {DOUBLE_ARCHIVE} -C {DOUBLE_DIR} && cd {DOUBLE_DIR}"
        f" && (MODEL_DOUBLE_TOKEN={KEY} setsid nohup python3 -m agent_doubles.model --scenario /tmp/run05-resume.json"
        " --access openrouter --token-env MODEL_DOUBLE_TOKEN --port 0"
        f" --port-file {DOUBLE_DIR}/port --journal {DOUBLE_DIR}/journal.jsonl >{DOUBLE_DIR}/double.log 2>&1"
        " </dev/null &) && i=0 && while [ ! -s port ] && [ $i -lt 200 ]; do i=$((i+1)); sleep 0.05; done && cat port"
    )
    return int(machine.shell(name, ["sh", "-c", script]).stdout.strip())


async def wait_state(env: Any, session_id: str, states: set[str], timeout: float = TIMEOUT_S) -> str | None:
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        async with env.pool.connection() as conn:
            found = await records.get(conn, session_id)
        state = None if found is None else found.state
        if state in states:
            return state
        await asyncio.sleep(0.05)
    return state


def test_a_follow_up_after_an_idle_suspend_is_answered_with_resume(store_dsn: str) -> None:
    golden = os.environ.get(GOLDEN_ENV, "")
    prefix = os.environ.get(PREFIX_ENV, "")
    if not golden or not prefix:
        pytest.fail(f"{GOLDEN_ENV} and {PREFIX_ENV} are required for the vm integration run")

    async def main() -> dict[str, Any]:
        async with Rig(store_dsn).env(standard_world()) as env:
            async with env.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest,"
                    " built_at, state) VALUES ('vmtest', %s, '2.1.330', '0.1.0', 'sha256:vmtest', now(), 'ready')",
                    (golden,),
                )
            machine = lima.Lima(lima.run_limactl, prefix=prefix)
            scratch = Path(tempfile.mkdtemp(prefix="run05-"))
            lines: list[str] = []
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
    report = Path(".wp") / "vm-resume.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    assert result["first_idle"] == "idle"
    assert result["suspended"] == "suspended"
    assert result["resume_requests"] == [result["claude_session_id_before"]]
    assert result["second_idle"] == "idle"
    assert result["claude_session_id_after"] == result["claude_session_id_before"]
    assert "3+3 is 6, after 2+2 was 4." in result["answers"]
    assert result["deliveries"] == {"first": "accepted", "second": "accepted"}
    states = result["transitions"]
    assert states.index("suspended") < states.index("resuming") < len(states) - 1 - states[::-1].index("working")


async def scenario(env: Any, manager: VmManager, machine: lima.Lima, scratch: Path, lines: list[str]) -> dict[str, Any]:
    await manager.reconcile()
    await verify_world(env)
    assert await manager.refill() == 1
    rows = await env.rows("SELECT lima_name FROM runner.vm WHERE state = 'pool'")
    port = start_double(machine, str(rows[0][0]), scratch)

    async def refresh() -> None:
        await pull(env)

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
        platform_key=lambda *_: KEY,
        upstream_openrouter=f"http://127.0.0.1:{port}",
        context=env.context,
        notify=env.sync.pusher.notify,
        first_prompt=first_prompt,
        amenders=request_amenders(),
        refresh_profiles=refresh,
        log=lines.append,
    )
    service = SessionService(session_env, handlers=registry.journal_handlers(), observers=state_observers())
    deliverer = install(env.context, Deliverer(session_env, idle_seconds=IDLE_S, stop=service.schedule_stop))
    resume_requests: list[str | None] = []
    original = GuestClient.create_session

    async def recording(self: GuestClient, request: Any) -> dict[str, Any]:
        resume_requests.append(request.get("resume_claude_session_id"))
        return await original(self, request)

    GuestClient.create_session = recording  # type: ignore[method-assign]
    stopping = asyncio.Event()
    journals = asyncio.ensure_future(service.run_journals(stopping))
    delivery = asyncio.ensure_future(deliverer.loop(stopping, asyncio.sleep))
    sid = uuid7()
    first_id = add_prompt(env, 1, "first", text="What is 2+2?")
    second_id = add_prompt(env, 2, "second", text="And what is 3+3?")
    await pull(env)
    item = desired(1, session_id=sid, task_id=TASK, org_id=ORG, wg_id=WG)
    session_env.desired = {
        "sessions": [wire(item)],
        "pending_prompts": [{"task_id": TASK, "session_id": sid, "scope_id": WG, "scope_seq": 1, "event_id": first_id}],
        "delivered_prompt_event_ids": {},
    }
    try:
        assert service.schedule_start(item)
        first_idle = await wait_state(env, sid, {"idle", "failed"})
        await service.idle()
        async with env.pool.connection() as conn:
            before = await records.get(conn, sid)
        assert before is not None
        resume_requests.clear()
        suspended = await wait_state(env, sid, {"suspended", "failed"}, timeout=IDLE_S + 30)
        started = time.monotonic()
        await deliverer.apply_desired(
            {
                "pending_prompts": [
                    {"task_id": TASK, "session_id": sid, "scope_id": WG, "scope_seq": 2, "event_id": second_id}
                ],
                "delivered_prompt_event_ids": {sid: [first_id]},
            }
        )
        working = await wait_state(env, sid, {"working", "idle", "failed"})
        resumed_ms = round((time.monotonic() - started) * 1000)
        second_idle = await wait_state(env, sid, {"idle", "failed"})
        await asyncio.sleep(1.0)  # the transcript reader journals the answer after the turn
        async with env.pool.connection() as conn:
            after = await records.get(conn, sid)
        assert after is not None
        result = {
            "first_idle": first_idle,
            "suspended": suspended,
            "working_after_resume": working,
            "resume_to_working_ms": resumed_ms,
            "second_idle": second_idle,
            "resume_requests": resume_requests,
            "claude_session_id_before": before.claude_session_id,
            "claude_session_id_after": after.claude_session_id,
            **await _evidence(env, sid, {"first": first_id, "second": second_id}),
            "log": [line for line in lines if "prompts" in line or "resum" in line or "suspend" in line][-40:],
        }
        await stop_session(session_env, sid)
        return result
    finally:
        GuestClient.create_session = original  # type: ignore[method-assign]
        stopping.set()
        await journals
        await delivery


async def _evidence(env: Any, sid: str, prompts: dict[str, str]) -> dict[str, Any]:
    """Pushed transitions of the session, the answers it journaled and the delivery state of each prompt."""
    await env.sync.pusher.drain()
    transitions = [
        entry["body"]["state"]
        for entry in env.hub.items()
        if entry["item_kind"] == "session_transition" and entry["body"]["session_id"] == sid
    ]
    deliveries = {}
    for label, event_id in prompts.items():
        found = await env.rows("SELECT state FROM runner.prompt_delivery WHERE event_id = %s", event_id)
        deliveries[label] = str(found[0][0])
    return {"transitions": transitions, "answers": await _answers(env), "deliveries": deliveries}


async def _answers(env: Any) -> list[str]:
    rows = await env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
    found = []
    for seq, kind, blob in rows:
        if kind != "event":
            continue
        item = open_item(env.sync.storage_key, seq, kind, bytes(blob))
        if item["header"]["kind"] == "session.output":
            found.append(str(item["body"].get("text", "")))
    return found
