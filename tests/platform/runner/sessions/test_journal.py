"""U-RUN-sessions: guest journal -> transitions, device events, audit items and usage (05-runner.md sections 6.1 steps
6-7, 6.4, 5.3-5.5, 10.4; 03-api.md sections 6.1, 7.1, 7.2, 10; R-6.1).

The journal comes from the fake guest; transitions, events (sealed with the workgroup key and the device key at
the push and checked by the rig's hub against the contracts) and outbox items are real.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_guest.recorder import Recorder
from agent_runner.guestlink import cursor
from agent_runner.guestlink.client import JournalRecord
from agent_runner.model import usage as usage_module
from agent_runner.model.journal import HANDLERS as MODEL_HANDLERS
from agent_runner.sessions import journal as journal_module
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.journal import HANDLERS as SESSION_HANDLERS
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..reconcile.rig import Rig, standard_world
from .kit import TASK, WG, Kit, desired, kit, session_id

CLAUDE_ID = "11111111-2222-4333-8444-555555555555"
OTHER_CLAUDE_ID = "11111111-2222-4333-8444-666666666666"
OTHER_SESSION = "01a1ee56-6000-7003-8f1b-3406910000aa"
CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
AT = "2026-11-21T12:00:05.000Z"
# the journal loop of a daemon sees the handlers of every area; `usage` belongs to the model area (RUN-12)
HANDLERS = {**SESSION_HANDLERS, **MODEL_HANDLERS}


def run(rig: Rig, body: Any) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await body(env)

    asyncio.run(main())


async def started(env: Any, **options: Any) -> tuple[Kit, VmJournal]:
    found = await kit(env, **options)
    starter = Starter(found.session_env)
    assert (await starter.start(desired())).started
    row = await vm_records.get(env.pool, "vm-00000001")
    assert row is not None
    found.guest.calls.clear()
    reader = VmJournal(found.session_env, row, handlers=HANDLERS, reached=starter.observe)
    return found, reader


async def consume(found: Kit, reader: VmJournal) -> None:
    """Run the loop until the guest acknowledged its last record, then stop it."""
    stopping = asyncio.Event()
    task = asyncio.ensure_future(reader.run(stopping))
    last = found.guest.records[-1].seq if found.guest.records else 0
    for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
        if found.guest.acked >= last or task.done():
            break
        await asyncio.sleep(0.005)
    stopping.set()
    await task


def state(value: str, **extra: Any) -> dict[str, Any]:
    return {"state": value, "at": AT, **extra}


def emit_conversation(guest: Any, sid: str) -> None:
    guest.emit(sid, "session.state", state("sandbox_ready", harness_version="a" * 64))  # already reported
    guest.emit(sid, "session.state", state("agent_starting"))
    guest.emit(sid, "session.state", state("prompt_delivering", claude_session_id=CLAUDE_ID))
    guest.emit(sid, "session.hook", {"event": "UserPromptSubmit", "decision": "allow"})
    guest.emit(sid, "session.state", {"state": "prompt_accepted", "prompt_id": TASK, "at": AT})
    guest.emit(sid, "session.output", {"message_id": "m1", "role": "assistant", "text": "4", "turn": 1})
    guest.emit(
        sid,
        "session.hook",
        {"event": "PreToolUse", "tool": "Bash", "decision": "deny", "rule": 2, "reason": "no", "path": "/x"},
    )
    guest.emit(
        sid,
        "session.tool",
        {
            "tool_use_id": "t1",
            "name": "Read",
            "input_summary": "a.txt",
            "input": "{}",
            "result_summary": "ok",
            "is_error": False,
            "duration_ms": 3,
        },
    )
    guest.emit(sid, "session.file", {"path": "a.txt", "op": "write", "sha256": "b" * 64, "size": 1})
    guest.emit(sid, "session.terminal", {"offset_ms_start": 0, "frames": [[0, "o", "hi"]], "cols": 160, "rows": 48})
    guest.emit(
        sid,
        "usage",
        {
            "request_id": "r1",
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "provider": "Weird",
            "provider_outside_allowlist": True,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 30,
            "cache_write_tokens": 5,
            "thinking_tokens": 0,
            "cost_usd": 0.0012345,
            "context_window_tokens": 1048576,
            "request_bytes": 10,
            "response_bytes": 20,
            "status": 200,
        },
    )
    guest.emit(sid, "session.state", state("idle"))
    guest.emit(sid, "session.state", state("idle", claude_session_id=OTHER_CLAUDE_ID))  # ID update only
    guest.emit(None, "guest.error", {"code": "gateway_route"})
    guest.emit(None, "guest.error", {"code": "Bad Code"})
    guest.emit(sid, "operation.request", {"pending_id": "p"})  # no handler here: cursor only


def test_records_become_transitions_events_audit_and_usage_in_journal_order(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        guest = found.guest
        emit_conversation(guest, sid)
        await consume(found, reader)

        last = guest.records[-1].seq
        assert guest.acked == last
        assert await cursor.load_cursor(env.pool, "vm-00000001") == last
        assert await found.transitions(session_id()) == [
            ("vm_assigning", None),
            ("vm_ready", None),
            ("sandbox_ready", None),
            ("agent_starting", None),
            ("prompt_delivering", None),
            ("working", None),
            ("idle", None),
        ]
        local = await found.stored(sid)
        assert local.claude_session_id == OTHER_CLAUDE_ID
        assert local.ready_at is not None
        kinds = [kind for kind, _body in await found.outbox()]
        tail = kinds[4:]
        assert tail == [
            "session_transition",  # prompt_delivering
            "event",  # hook allow
            "session_transition",  # working
            "event",  # output
            "event",  # hook deny
            "audit",  # hook.denied
            "event",  # tool
            "event",  # file
            "event",  # terminal
            "usage",
            "audit",  # provider outside allow-list
            "session_transition",  # idle
        ]
        audits = await found.of_kind("audit")
        assert audits[0] == {
            "action": "hook.denied",
            "result": "denied",
            "task_id": TASK,
            "session_id": sid,
            "target_kind": "session",
            "target_id": sid,
            "detail": {"tool": "Bash", "rule": 2},
        }
        assert "/x" not in str(audits)
        assert audits[1]["action"] == "model.provider_outside_allowlist"
        assert audits[1]["detail"] == {"provider": "Weird", "model": "deepseek/deepseek-v4.1-flash-1017"}
        (usage,) = await found.of_kind("usage")
        assert usage == {
            "session_id": sid,
            "delta": {
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 30,
                "cache_write_tokens": 5,
                "thinking_tokens": 0,
                "cost_micro_usd": 1234,
            },
            "cost_basis": "provider",
            "context_used_tokens": 135,
            "context_window_tokens": 1048576,
            "last_provider": "Weird",
        }
        transitions = await found.of_kind("session_transition")
        assert transitions[4]["claude_session_id_present"] is True
        assert transitions[4]["at"] == "2026-11-21T12:00:00.000Z"  # device time, not the guest's `at`
        events = await found.pushed_events()
        assert [header["kind"] for header, _ in events] == [
            "session.hook",
            "session.output",
            "session.hook",
            "session.tool",
            "session.file",
            "session.terminal",
        ]
        for header, _body in events:
            assert (header["scope"], header["scope_id"], header["wg"]) == ("wg", WG, WG)
            assert (header["task"], header["session"], header["epoch"]) == (TASK, sid, 1)
            assert header["ts"] == "2026-11-21T12:00:01.000Z"
        assert events[1][1] == {"message_id": "m1", "role": "assistant", "text": "4", "turn": 1}
        assert events[2][1]["path"] == "/x"  # inside the ciphertext only
        assert any("guest_error vm_id=vm-00000001 code=gateway_route" in line for line in found.lines)
        assert any("guest_error vm_id=vm-00000001 code=unknown" in line for line in found.lines)
        assert reader.handled == len(guest.records) - 1

    run(rig, body)


def test_failed_state_writes_a_status_event_and_refused_prompts_an_audit(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        found.guest.emit(sid, "session.state", {"state": "prompt_refused", "prompt_id": TASK, "reason": "unregistered",
                                                "at": AT, "claude_session_id": CLAUDE_ID})  # fmt: skip
        found.guest.emit(sid, "session.state", {"state": "failed", "reason": "agent_start_failed", "at": AT})
        found.guest.emit(sid, "session.state", {"state": "working", "at": AT})  # nothing leaves failed
        await consume(found, reader)
        assert (await found.transitions(sid))[-1] == ("failed", "agent_start_failed"), found.lines
        audits = await found.of_kind("audit")
        assert [a["action"] for a in audits] == ["session.prompt_refused"]
        assert audits[0]["detail"] == {"reason": "unregistered"}
        assert (await found.stored(sid)).claude_session_id == CLAUDE_ID
        events = await found.pushed_events()
        assert events[-1][0]["kind"] == "session.status"
        assert events[-1][1] == {"state": "failed", "reason": "agent_start_failed"}

    run(rig, body)


def test_prompt_refused_without_new_id_changes_nothing_else(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        record = {"state": "prompt_refused", "prompt_id": TASK, "reason": "delivery_failed", "at": AT}
        found.guest.emit(sid, "session.state", record)
        await consume(found, reader)
        assert len(await found.transitions(sid)) == 4

    run(rig, body)


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("session.state", {"state": "bogus", "at": AT}),
        ("session.state", {"state": "idle", "claude_session_id": "nope", "at": AT}),
        ("session.state", {"state": "idle", "reason": "Bad", "at": AT}),
        ("session.state", {"state": "idle", "reason": 3, "at": AT}),
        ("session.state", {"state": "failed", "reason": "made_up", "at": AT}),
        # reasons only the runner or the hub can know are refused from a guest
        ("session.state", {"state": "failed", "reason": "capacity_exhausted", "at": AT}),
        ("session.state", {"state": "failed", "reason": "unassigned", "at": AT}),
        ("session.state", {"state": "prompt_refused", "prompt_id": TASK, "reason": "other", "at": AT}),
        ("session.state", {"state": "prompt_refused", "reason": "unregistered", "at": AT}),
        ("usage", {"model": "bad model"}),
        ("usage", {"model": "m", "provider": ""}),
        ("usage", {"model": "m", "input_tokens": -1}),
        (
            "usage",
            {
                "model": "m",
                **dict.fromkeys(usage_module.USAGE_COUNTS, 1),
                "cost_usd": "1",
                "context_window_tokens": 1,
            },
        ),
        ("session.terminal", {"frames": [[0, "o", "x" * 70000]]}),  # malformed and beyond the 64 KiB terminal limit
        # well-formed, but far more data than a guest may write into one record: refused, never split
        ("session.terminal", {"offset_ms_start": 0, "frames": [[0, "o", "x" * 70000]], "cols": 160, "rows": 48}),
        ("session.tools_ready", {"tools": -1, "at": AT}),
        ("session.tools_ready", {"tools": True, "at": AT}),
        ("session.tools_ready", {"tools": 3}),
        ("session.tools_ready", {"tools": 3, "at": AT, "names": ["get_context"]}),
    ],
)
def test_invalid_records_are_skipped_and_the_next_one_is_handled(rig: Rig, kind: str, body: dict[str, Any]) -> None:
    async def scenario(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        found.guest.emit(sid, kind, body)
        found.guest.emit(sid, "session.state", state("prompt_delivering"))
        await consume(found, reader)
        assert reader.skipped == 1
        assert any(f"record_skipped vm_id=vm-00000001 kind={kind} seq=1" in line for line in found.lines)
        assert (await found.transitions(sid))[-1] == ("prompt_delivering", None)
        assert found.guest.acked == 2

    run(rig, scenario)


async def terminal_events(found: Kit) -> list[dict[str, Any]]:
    return [body for header, body in await found.pushed_events() if header["kind"] == "session.terminal"]


def test_a_terminal_of_40000_control_characters_arrives_sealed(rig: Rig) -> None:
    # AUD-01 A-03: 16 KiB of raw control characters are about 96 KiB of body JSON; every byte reaches the hub
    async def scenario(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        rec = Recorder()
        rec.output(b"\x1b" * 40000, 5)
        for body in rec.take(final=True):
            found.guest.emit(sid, "session.terminal", body)
        await consume(found, reader)
        assert reader.skipped == 0
        bodies = await terminal_events(found)
        assert "".join(frame[2] for body in bodies for frame in body["frames"]) == "\x1b" * 40000

    run(rig, scenario)


def test_a_terminal_record_beyond_the_event_limit_is_split_not_dropped(rig: Rig) -> None:
    # a guest that measured raw bytes (before KOR-02): the runner splits the record into events within E8
    async def scenario(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        frames: list[list[Any]] = [
            [7, "m", "recorder restarted"],
            [7, "o", "\x1b" * 12000],  # 16 KiB of data in all, as the contract allows, but 74 KiB of body JSON
            [9, "o", "ä" * 1000],
            [12, "o", "end"],
        ]
        found.guest.emit(sid, "session.terminal", {"offset_ms_start": 7, "frames": frames, "cols": 80, "rows": 24})
        await consume(found, reader)
        assert reader.skipped == 0
        bodies = await terminal_events(found)
        assert len(bodies) > 1
        assert next(frame for body in bodies for frame in body["frames"]) == frames[0]
        assert "".join(f[2] for body in bodies for f in body["frames"] if f[1] == "o") == "".join(
            f[2] for f in frames if f[1] == "o"
        )
        for body in bodies:
            assert body["offset_ms_start"] == body["frames"][0][0]
            assert (body["cols"], body["rows"]) == (80, 24)
            assert all(len(frame[2]) <= 16384 for frame in body["frames"])

    run(rig, scenario)


def test_records_of_foreign_or_unknown_sessions_are_ignored(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        other = "01930005-0000-7000-8000-0000000000aa"
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, state) "
                "SELECT %s, task_id, org_id, wg_id, 'vm-0000ffff', 'working' FROM runner.session LIMIT 1",
                (other,),
            )
        found.guest.emit(other, "session.output", {"message_id": "m", "role": "assistant", "text": "x", "turn": 1})
        found.guest.emit("01930005-0000-7000-8000-0000000000bb", "usage", {})
        found.guest.emit(None, "session.state", state("idle"))
        await consume(found, reader)
        assert await found.of_kind("event") == []
        assert await found.of_kind("usage") == []
        assert sum("foreign_session" in line for line in found.lines) == 3
        assert found.guest.acked == 3

    run(rig, body)


def test_ack_only_after_the_commit_and_a_store_failure_rolls_the_page_back(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, _reader = await started(env)
        sid = session_id()

        async def broken(_journal: Any, _conn: Any, _record: JournalRecord) -> None:
            raise RuntimeError("store")

        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        reader = VmJournal(found.session_env, row, handlers={**HANDLERS, "guest.error": broken})
        found.guest.emit(sid, "session.state", state("prompt_delivering"))
        found.guest.emit(None, "guest.error", {"code": "x"})
        before = len(await found.outbox())
        with pytest.raises(RuntimeError):
            await reader.run(asyncio.Event())
        assert len(await found.outbox()) == before  # the transition of record 1 rolled back with the page
        assert "ack" not in found.guest.names()
        assert await cursor.load_cursor(env.pool, "vm-00000001") == 0

    run(rig, body)


def test_resume_from_the_stored_cursor(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        found.guest.emit(sid, "session.state", state("prompt_delivering"))
        found.guest.emit(sid, "session.state", {"state": "prompt_accepted", "prompt_id": TASK, "at": AT})
        found.guest.emit(sid, "session.state", state("idle"))
        await cursor.save_cursor(env.pool, "vm-00000001", 2)
        await consume(found, reader)
        assert ("journal", 2) in found.guest.calls
        assert [s for s, _ in await found.transitions(sid)][-1] == "idle"
        assert "prompt_delivering" not in [s for s, _ in await found.transitions(sid)]

    run(rig, body)


def test_a_rewound_journal_is_read_from_the_start(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        await cursor.save_cursor(env.pool, "vm-00000001", 50)
        found.guest.emit(sid, "session.state", state("prompt_delivering"))
        await consume(found, reader)
        assert any("runner.journal rewound vm_id=vm-00000001 cursor=50" in line for line in found.lines)
        assert (await found.transitions(sid))[-1] == ("prompt_delivering", None)
        assert await cursor.load_cursor(env.pool, "vm-00000001") == 1

    run(rig, body)


def test_observers_see_every_state_record(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, _reader = await started(env)
        seen: list[tuple[str, str]] = []

        async def observe(_journal: Any, _conn: Any, session: LocalSession, record: Any) -> None:
            seen.append((session.state, record["state"]))

        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        reader = VmJournal(found.session_env, row, handlers=HANDLERS, observers=[StateObserver("t", observe)])
        sid = session_id()
        found.guest.emit(sid, "session.state", {"state": "prompt_accepted", "prompt_id": TASK, "at": AT})
        found.guest.emit(sid, "session.state", {"state": "prompt_refused", "prompt_id": TASK,
                                                "reason": "unregistered", "at": AT})  # fmt: skip
        await consume(found, reader)
        assert seen == [("working", "prompt_accepted"), ("working", "prompt_refused")]
        reader.reached("x", "y")  # no measurement callback given: nothing happens

    run(rig, body)


def test_record_time_falls_back_to_device_time() -> None:
    class Journal:
        class env:
            @staticmethod
            def at() -> str:
                return "2026-11-21T12:00:09.000Z"

    for bad in ("bad", "2026-99-99T99:99:99.000Z", "2026-11-21T24:00:00.000Z"):
        record = JournalRecord(seq=1, ts=bad, session_id=None, kind="session.output", body={})
        assert journal_module._ts(record, Journal()) == "2026-11-21T12:00:09.000Z"
    good = JournalRecord(seq=1, ts="2026-11-21T23:59:59.999Z", session_id=None, kind="session.output", body={})
    assert journal_module._ts(good, Journal()) == "2026-11-21T23:59:59.999Z"


def test_usage_without_provider_and_hook_variants(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        usage = {
            "request_id": "r",
            "model": "deepseek/deepseek-v4.1-flash-1017",
            **dict.fromkeys(usage_module.USAGE_COUNTS, 0),
            "context_window_tokens": 1,
            "request_bytes": 0,
            "response_bytes": 0,
            "status": 500,
            "provider_outside_allowlist": True,  # without a provider: no audit
        }
        found.guest.emit(sid, "usage", usage)
        found.guest.emit(sid, "session.hook", {"event": "PreToolUse", "decision": "deny", "tool": "Bash", "rule": 1})
        found.guest.emit(sid, "session.hook", {"event": "PreToolUse", "decision": "deny", "tool": "Bash"})
        found.guest.emit(sid, "session.hook", {"event": "PreToolUse", "decision": "deny", "tool": "B a d", "rule": 3})
        found.guest.emit(sid, "session.hook", {"event": "PostToolUse", "decision": "deny", "tool": "Bash", "rule": 3})
        found.guest.emit(sid, "session.hook", {"event": "PreToolUse", "decision": "allow", "tool": "Read"})
        found.guest.emit(sid, "session.hook", {"event": "PreToolUse", "decision": "deny", "tool": "Write", "rule": 5},
                         ts="bad")  # fmt: skip
        await consume(found, reader)
        (item,) = await found.of_kind("usage")
        assert item["last_provider"] is None
        audits = await found.of_kind("audit")
        assert [a["detail"] for a in audits] == [{"tool": "Write", "rule": 5}]
        events = await found.pushed_events()
        assert events[-1][0]["ts"] == "2026-11-21T12:00:00.000Z"  # device time for a bad record time

    run(rig, body)


def test_records_with_malformed_session_ids_are_skipped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        found.guest.emit("", "session.output", {"message_id": "m", "role": "assistant", "text": "x", "turn": 1})
        found.guest.emit("x", "session.state", state("idle"))
        found.guest.emit("{" + session_id() + "}", "session.state", state("idle"))  # not canonical
        found.guest.emit(session_id(), "session.state", state("prompt_delivering"))
        await consume(found, reader)
        assert sum("foreign_session" in line for line in found.lines) == 3
        assert (await found.transitions(session_id()))[-1] == ("prompt_delivering", None)
        assert found.guest.acked == 4

    run(rig, body)


@pytest.mark.parametrize("seqs", [[2, 1], [1, 1], [1, 2**63]])
def test_pages_out_of_order_or_range_are_refused_before_handling(rig: Rig, seqs: list[int]) -> None:
    async def body(env: Any) -> None:
        found, reader = await started(env)
        for seq in seqs:
            found.guest.records.append(
                JournalRecord(seq=seq, ts=AT, session_id=session_id(), kind="session.state", body=state("idle"))
            )
        from agent_runner.guestlink.errors import GuestProtocolError  # noqa: PLC0415

        with pytest.raises(GuestProtocolError):
            await reader.run(asyncio.Event())
        assert len(await found.transitions(session_id())) == 4
        assert "ack" not in found.guest.names()

    run(rig, body)


def test_a_sessions_guest_error_becomes_a_session_error_event(rig: Rig) -> None:
    """ADR-064 section 5: a code of the closed list for a session on this VM is synchronised as `session.error`;
    VM-level records and unknown codes stay in the log."""

    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        found.guest.emit(sid, "guest.error", {"code": "turn_unconfirmed"})
        found.guest.emit(sid, "guest.error", {"code": "not_a_code"})
        found.guest.emit(None, "guest.error", {"code": "hook_handler_failed"})
        found.guest.emit(OTHER_SESSION, "guest.error", {"code": "hook_handler_failed"})  # not this VM's session
        await consume(found, reader)
        events = await found.pushed_events()
        errors = [(header["kind"], payload) for header, payload in events if header["kind"] == "session.error"]
        assert errors == [("session.error", {"code": "turn_unconfirmed"})]
        assert sum("guest_error vm_id=vm-00000001" in line for line in found.lines) == 4

    run(rig, body)


def test_the_guest_error_codes_are_those_of_the_contract() -> None:
    record = json.loads((CONTRACTS / "guest" / "journal" / "record.json").read_text(encoding="utf-8"))
    assert set(record["$defs"]["body_guest_error"]["properties"]["code"]["enum"]) == journal_module.GUEST_ERROR_CODES


def test_a_hook_outage_is_a_failure_a_guest_may_report() -> None:
    assert "hook_unavailable" in journal_module.GUEST_FAILURES


def test_the_tools_at_the_agent_are_a_measured_start_step(rig: Rig) -> None:
    """HOT-09: `session.tools_ready` changes no state and sends nothing; the start logs its own step `tools_ready`
    between `agent_starting` and `prompt_delivering`."""

    async def body(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        found.guest.emit(sid, "session.tools_ready", {"tools": 17, "at": AT})
        found.guest.emit(OTHER_SESSION, "session.tools_ready", {"tools": 17, "at": AT})  # not this VM's session
        found.guest.emit(sid, "session.state", state("prompt_delivering"))
        await consume(found, reader)
        assert reader.skipped == 0
        steps = [line.split(" step=")[1].split(" ")[0] for line in found.lines if "start_step" in line]
        assert steps[-2:] == ["tools_ready", "prompt_delivering"]
        assert (await found.transitions(sid))[-1] == ("prompt_delivering", None)
        assert [header["kind"] for header, _ in await found.pushed_events()] == []

    run(rig, body)
