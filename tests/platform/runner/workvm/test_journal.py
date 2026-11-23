"""U-RUN-workvm journal: handler `workvm.call` (13-work-vm.md section 3; contracts/changes/KOR-29.md).

`parse` is checked rule by rule against the closed argument rules (and the rule key sets against
`contracts/mcp/objects.json`); `Calls` answers through a fake guest client with `answer_pending` (failures injected)
and a fake `WorkVms`; the handler gets a fake journal that resolves the session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner.errors import RunnerError
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestError
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.sessions.records import LocalSession
from agent_runner.vm.records import VmRow
from agent_runner.workvm import journal
from agent_runner.workvm.journal import Calls
from agent_runner.workvm.manager import NOT_ENABLED, CallFailed, WorkVms

ROOT = Path(__file__).resolve().parents[4]
PENDING = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"
JOB = "j-0123456789ab"
SESSION_A = "0192f3a4-0000-7000-8000-00000000000a"
SESSION_B = "0192f3a4-0000-7000-8000-00000000000b"
Json = dict[str, Any]


def pending(n: int) -> str:
    return f"0192f3a4-5b6c-7d8e-9f01-{n:012x}"


def session(session_id: str = SESSION_A) -> LocalSession:
    return cast(LocalSession, SimpleNamespace(id=session_id))


VM = cast(VmRow, SimpleNamespace(id="vm-0a1b2c3d"))


# -- contracts ---------------------------------------------------------------------------------------------------


def resolve(definition: Json, base: Path) -> Json:
    ref = definition.get("$ref")
    if ref is None:
        return definition
    file, _, pointer = str(ref).partition("#")
    path = (base.parent / file).resolve() if file else base
    document = json.loads(path.read_text(encoding="utf-8"))
    target = document
    for part in pointer.strip("/").split("/"):
        target = target[part]
    return resolve(target, path)


def workvm_tools() -> dict[str, Path]:
    """The tools of `contracts/mcp/INDEX.json` answered through `workvm.call`, with their input schema files."""
    contracts = ROOT / "contracts"
    index = json.loads((contracts / "mcp" / "INDEX.json").read_text(encoding="utf-8"))
    return {entry["tool"]: contracts / entry["input"] for entry in index["tools"] if entry.get("via") == journal.KIND}


def test_rules_match_the_contracts() -> None:
    tools = workvm_tools()
    assert "workvm_browse" in tools
    assert set(tools) == set(journal.RULES)
    path = ROOT / "contracts" / "mcp" / "objects.json"
    defs = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    assert {name.removeprefix("input_") for name in defs if name.startswith("input_workvm_")} == set(tools)
    for tool, (fields, required) in journal.RULES.items():
        schema = resolve(json.loads(tools[tool].read_text(encoding="utf-8")), tools[tool])
        assert schema == resolve(defs[f"input_{tool}"], path), tool
        assert schema["additionalProperties"] is False, tool
        assert set(schema.get("properties", {})) == set(fields), tool
        assert set(schema.get("required", [])) == required, tool


# -- parse -------------------------------------------------------------------------------------------------------


def body(tool: str, arguments: object, **changes: object) -> Json:
    value: Json = {"pending_id": PENDING, "tool": tool, "arguments": arguments}
    value.update(changes)
    return value


ENV_64 = [{"name": f"V{i}", "value": "x"} for i in range(64)]

ACCEPTED: list[tuple[str, Json]] = [
    ("workvm_exec", {"command": "x"}),
    ("workvm_exec", {"command": "a" * 65536}),
    (
        "workvm_exec",
        {
            "command": "make",
            "cwd": "/",
            "env": ENV_64,
            "stdin": "",
            "timeout_seconds": 1,
            "wait_seconds": 0,
            "sync": "to_vm",
        },
    ),
    (
        "workvm_exec",
        {
            "command": "make",
            "cwd": "/" + "a" * 4095,
            "env": [{"name": "_a" + "B" * 126, "value": "v" * 32768}],
            "stdin": "s" * 1048576,
            "timeout_seconds": 604800,
            "wait_seconds": 30,
            "sync": "none",
        },
    ),
    ("workvm_exec", {"command": "x", "env": []}),
    ("workvm_output", {"job_id": JOB}),
    ("workvm_output", {"job_id": JOB, "offset": 0, "limit": 1, "wait_seconds": 0}),
    ("workvm_output", {"job_id": JOB, "offset": journal.MAX_SAFE, "limit": 32768, "wait_seconds": 30}),
    ("workvm_kill", {"job_id": JOB}),
    *[("workvm_kill", {"job_id": JOB, "signal": signal}) for signal in ("TERM", "INT", "HUP", "KILL")],
    ("workvm_status", {}),
    ("workvm_sync", {"direction": "to_vm"}),
    ("workvm_sync", {"direction": "from_vm", "path": "src/a b.py"}),
    ("workvm_sync", {"direction": "from_vm", "path": "a" * 4096}),
    ("workvm_browse", {"url": "https://example.com/"}),
    ("workvm_browse", {"url": "http://a"}),
    ("workvm_browse", {"url": "https://example.com/" + "a" * (4096 - 20)}),
    *[("workvm_browse", {"url": "https://example.com/", "action": action}) for action in ("read", "screenshot")],
    (
        "workvm_browse",
        {
            "url": "https://example.com/form?q=1#x",
            "action": "fill",
            "selector": "s" * 1024,
            "text": "t" * 65536,
            "wait_ms": 30000,
            "path": "shots/a b.png",
        },
    ),
    ("workvm_browse", {"url": "https://example.com/", "action": "click", "selector": "#ok", "text": "", "wait_ms": 0}),
    ("workvm_browse", {"url": "https://example.com/", "path": "a" * 4096}),
]


@pytest.mark.parametrize(("tool", "arguments"), ACCEPTED)
def test_parse_accepts_valid_calls(tool: str, arguments: Json) -> None:
    assert journal.parse(body(tool, arguments)) == (PENDING, tool, arguments)


REFUSED_ARGUMENTS: list[tuple[str, Json]] = [
    ("workvm_exec", {}),
    ("workvm_exec", {"command": ""}),
    ("workvm_exec", {"command": "a" * 65537}),
    ("workvm_exec", {"command": 5}),
    ("workvm_exec", {"command": "x", "unknown": 1}),
    ("workvm_exec", {"command": "x", "cwd": "relative"}),
    ("workvm_exec", {"command": "x", "cwd": "/a\nb"}),
    ("workvm_exec", {"command": "x", "cwd": "/a\x7f"}),
    ("workvm_exec", {"command": "x", "cwd": "/" + "a" * 4096}),
    ("workvm_exec", {"command": "x", "cwd": 5}),
    ("workvm_exec", {"command": "x", "env": {"A": "1"}}),
    ("workvm_exec", {"command": "x", "env": [*ENV_64, {"name": "X", "value": ""}]}),
    ("workvm_exec", {"command": "x", "env": ["A=1"]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "A"}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "A", "value": "1", "extra": 1}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": 1, "value": "1"}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "1A", "value": "1"}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "A-B", "value": "1"}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "A" * 129, "value": "1"}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "A", "value": "v" * 32769}]}),
    ("workvm_exec", {"command": "x", "env": [{"name": "A", "value": 1}]}),
    ("workvm_exec", {"command": "x", "stdin": "s" * 1048577}),
    ("workvm_exec", {"command": "x", "stdin": None}),
    ("workvm_exec", {"command": "x", "timeout_seconds": 0}),
    ("workvm_exec", {"command": "x", "timeout_seconds": 604801}),
    ("workvm_exec", {"command": "x", "timeout_seconds": True}),
    ("workvm_exec", {"command": "x", "timeout_seconds": 1.0}),
    ("workvm_exec", {"command": "x", "wait_seconds": -1}),
    ("workvm_exec", {"command": "x", "wait_seconds": 31}),
    ("workvm_exec", {"command": "x", "sync": "from_vm"}),
    ("workvm_exec", {"command": "x", "sync": ["to_vm"]}),
    ("workvm_output", {}),
    ("workvm_output", {"job_id": "j-0123"}),
    ("workvm_output", {"job_id": 5}),
    ("workvm_output", {"job_id": JOB, "offset": -1}),
    ("workvm_output", {"job_id": JOB, "offset": journal.MAX_SAFE + 1}),
    ("workvm_output", {"job_id": JOB, "limit": 0}),
    ("workvm_output", {"job_id": JOB, "limit": 32769}),
    ("workvm_output", {"job_id": JOB, "wait_seconds": 31}),
    ("workvm_output", {"job_id": JOB, "signal": "TERM"}),
    ("workvm_kill", {}),
    ("workvm_kill", {"job_id": "J-0123456789AB"}),
    ("workvm_kill", {"job_id": None}),
    ("workvm_kill", {"job_id": JOB, "signal": "USR1"}),
    ("workvm_kill", {"job_id": JOB, "signal": "term"}),
    ("workvm_status", {"verbose": True}),
    ("workvm_sync", {}),
    ("workvm_sync", {"path": "a"}),
    ("workvm_sync", {"direction": "none"}),
    ("workvm_sync", {"direction": "to_vm", "path": ""}),
    ("workvm_sync", {"direction": "to_vm", "path": "/abs"}),
    ("workvm_sync", {"direction": "to_vm", "path": "a\x01b"}),
    ("workvm_sync", {"direction": "to_vm", "path": "a" * 4097}),
    ("workvm_sync", {"direction": "to_vm", "path": 5}),
    ("workvm_browse", {}),
    ("workvm_browse", {"action": "read"}),
    ("workvm_browse", {"url": 5}),
    ("workvm_browse", {"url": "ftp://example.com/"}),
    ("workvm_browse", {"url": "https://"}),
    ("workvm_browse", {"url": "https://exa mple.com/"}),
    ("workvm_browse", {"url": "https://example.com/\x7f"}),
    ("workvm_browse", {"url": "https://example.com/\n"}),
    ("workvm_browse", {"url": "HTTPS://example.com/"}),
    ("workvm_browse", {"url": "https://example.com/" + "a" * (4097 - 20)}),
    ("workvm_browse", {"url": "https://example.com/", "action": "submit"}),
    ("workvm_browse", {"url": "https://example.com/", "action": None}),
    ("workvm_browse", {"url": "https://example.com/", "selector": ""}),
    ("workvm_browse", {"url": "https://example.com/", "selector": "s" * 1025}),
    ("workvm_browse", {"url": "https://example.com/", "selector": 1}),
    ("workvm_browse", {"url": "https://example.com/", "text": "t" * 65537}),
    ("workvm_browse", {"url": "https://example.com/", "text": ["t"]}),
    ("workvm_browse", {"url": "https://example.com/", "wait_ms": -1}),
    ("workvm_browse", {"url": "https://example.com/", "wait_ms": 30001}),
    ("workvm_browse", {"url": "https://example.com/", "wait_ms": False}),
    ("workvm_browse", {"url": "https://example.com/", "wait_ms": 1.5}),
    ("workvm_browse", {"url": "https://example.com/", "path": ""}),
    ("workvm_browse", {"url": "https://example.com/", "path": "/abs"}),
    ("workvm_browse", {"url": "https://example.com/", "path": "a" * 4097}),
    ("workvm_browse", {"url": "https://example.com/", "direction": "to_vm"}),
]


@pytest.mark.parametrize(("tool", "arguments"), REFUSED_ARGUMENTS)
def test_parse_refuses_invalid_arguments(tool: str, arguments: Json) -> None:
    with pytest.raises(JournalRecordInvalid) as caught:
        journal.parse(body(tool, arguments))
    assert caught.value.kind == journal.KIND


@pytest.mark.parametrize(
    "record",
    [
        {"pending_id": PENDING, "tool": "workvm_status"},
        body("workvm_status", {}, extra=1),
        body("workvm_status", {}, pending_id=5),
        body("workvm_status", {}, pending_id=PENDING.upper()),
        body("workvm_status", {}, pending_id="not-a-uuid"),
        {"pending_id": PENDING, "tool": 5, "arguments": {}},
        {"pending_id": PENDING, "tool": "workvm_reboot", "arguments": {}},
        body("workvm_status", []),
        body("workvm_status", None),
        body("workvm_exec", {"command": "a\x00b"}),
        body("workvm_exec", {"command": "a\udc80"}),
    ],
)
def test_parse_refuses_invalid_bodies(record: Json) -> None:
    with pytest.raises(JournalRecordInvalid):
        journal.parse(record)


# -- Calls -------------------------------------------------------------------------------------------------------


@dataclass
class FakeClient:
    env: FakeEnv

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def answer_pending(self, pending_id: str, answer: Mapping[str, Any]) -> None:
        self.env.attempts.append((pending_id, dict(answer)))
        if self.env.answer_errors:
            raise self.env.answer_errors.pop(0)
        self.env.answers.append((pending_id, dict(answer)))


@dataclass
class FakeEnv:
    context: Any = field(default_factory=SimpleNamespace)
    answers: list[tuple[str, Json]] = field(default_factory=list)
    attempts: list[tuple[str, Json]] = field(default_factory=list)
    answer_errors: list[BaseException] = field(default_factory=list)
    client_errors: list[BaseException] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    vms: list[Any] = field(default_factory=list)

    async def client(self, vm: Any) -> FakeClient:
        self.vms.append(vm)
        if self.client_errors:
            raise self.client_errors.pop(0)
        return FakeClient(self)

    def log(self, line: str) -> None:
        self.lines.append(line)


@dataclass
class FakeWorkVms:
    outcome: Callable[[str, Mapping[str, Any]], Awaitable[Json]] | None = None
    calls: list[tuple[Any, str, str, Json]] = field(default_factory=list)

    async def call(self, env: Any, session: LocalSession, tool: str, arguments: Mapping[str, Any]) -> Json:
        self.calls.append((env, session.id, tool, dict(arguments)))
        if self.outcome is None:
            return {"tool": tool}
        return await self.outcome(tool, arguments)


def calls_with(workvms: FakeWorkVms | None) -> Calls:
    return Calls(cast(WorkVms, workvms))


async def settle(calls: Calls) -> None:
    while calls.tasks:
        await asyncio.gather(*list(calls.tasks))


def answer_of(calls: Calls, env: FakeEnv, tool: str = "workvm_status", arguments: Json | None = None) -> Json:
    async def run() -> None:
        task = calls.spawn(env, VM, session(), pending_id=PENDING, tool=tool, arguments=arguments or {})
        assert task is not None
        await settle(calls)

    asyncio.run(run())
    assert len(env.answers) == 1
    pending_id, answer = env.answers[0]
    assert pending_id == PENDING
    return answer


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    original = asyncio.sleep

    async def fast(delay: float, result: Any = None) -> Any:
        if delay:
            recorded.append(delay)
        return await original(0, result)

    monkeypatch.setattr(asyncio, "sleep", fast)
    return recorded


def test_without_workvms_every_call_is_unavailable() -> None:
    env = FakeEnv()
    assert answer_of(calls_with(None), env) == {
        "status": "error",
        "error": {
            "error": "unavailable",
            "message": NOT_ENABLED,
        },
    }
    assert env.vms == [VM]


def test_a_result_is_answered_ok() -> None:
    env, workvms = FakeEnv(), FakeWorkVms()
    calls = calls_with(workvms)
    assert answer_of(calls, env, "workvm_kill", {"job_id": JOB}) == {"status": "ok", "result": {"tool": "workvm_kill"}}
    assert workvms.calls == [(env, SESSION_A, "workvm_kill", {"job_id": JOB})]
    assert calls.running == {SESSION_A: 0}
    assert calls.tasks == set()


def test_call_failed_becomes_a_tool_error() -> None:
    async def fail(tool: str, arguments: Mapping[str, Any]) -> Json:
        raise CallFailed("not_found", "No work VM.")

    env = FakeEnv()
    assert answer_of(calls_with(FakeWorkVms(fail)), env) == {
        "status": "error",
        "error": {"error": "not_found", "message": "No work VM."},
    }
    assert env.lines == []


def test_an_unexpected_failure_is_unavailable_and_logged() -> None:
    async def crash(tool: str, arguments: Mapping[str, Any]) -> Json:
        raise KeyError("secret-value")

    env = FakeEnv()
    assert answer_of(calls_with(FakeWorkVms(crash)), env) == {
        "status": "error",
        "error": {"error": "unavailable", "message": "The work VM call failed. Try again."},
    }
    assert env.lines == [f"runner.workvm call_failed tool=workvm_status session_id={SESSION_A} error=KeyError"]
    assert "secret-value" not in env.lines[0]


def test_the_answer_is_retried_on_guest_errors(sleeps: list[float]) -> None:
    env = FakeEnv(
        answer_errors=[GuestError("guest_unreachable", "answer_pending")],
        client_errors=[RunnerError("vm_token_invalid")],
    )
    answer_of(calls_with(FakeWorkVms()), env)
    assert len(env.attempts) == 2
    assert env.lines == [
        f"runner.workvm answer_failed pending_id={PENDING} attempt=1 error=vm_token_invalid",
        f"runner.workvm answer_failed pending_id={PENDING} attempt=2 error=guest_unreachable",
    ]
    assert sleeps == [0.2, 0.4]


def test_the_answer_is_given_up_after_three_attempts(sleeps: list[float]) -> None:
    env = FakeEnv(answer_errors=[GuestError("guest_failed", "answer_pending") for _ in range(4)])
    calls = calls_with(FakeWorkVms())

    async def run() -> None:
        calls.spawn(env, VM, session(), pending_id=PENDING, tool="workvm_status", arguments={})
        await settle(calls)

    asyncio.run(run())
    assert env.answers == []
    assert len(env.attempts) == journal.ANSWER_ATTEMPTS == 3
    assert [line.split()[3] for line in env.lines] == ["attempt=1", "attempt=2", "attempt=3"]
    assert sleeps == [0.2, 0.4, pytest.approx(0.6)]


def test_a_pending_call_is_taken_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journal, "PENDING_MEMORY", 2)
    env, workvms = FakeEnv(), FakeWorkVms()
    calls = calls_with(workvms)

    async def run() -> list[bool]:
        taken = []
        for n in (1, 1, 2, 3, 2, 1):
            task = calls.spawn(env, VM, session(), pending_id=pending(n), tool="workvm_status", arguments={})
            taken.append(task is not None)
        await settle(calls)
        return taken

    # the third ID evicts the first, so a late repeat of the first is taken again; the second is still known
    assert asyncio.run(run()) == [True, False, True, True, False, True]
    assert list(calls.answered) == [pending(3), pending(1)]
    assert [pending_id for pending_id, _ in env.answers] == [pending(1), pending(2), pending(3), pending(1)]


def test_calls_per_session_are_limited() -> None:
    release = asyncio.Event()

    async def blocked(tool: str, arguments: Mapping[str, Any]) -> Json:
        await release.wait()
        return {}

    env, workvms = FakeEnv(), FakeWorkVms(blocked)
    calls = calls_with(workvms)
    limit = journal.MAX_CALLS_PER_SESSION

    async def run() -> None:
        for n in range(limit + 1):
            calls.spawn(env, VM, session(), pending_id=pending(n), tool="workvm_status", arguments={})
        calls.spawn(env, VM, session(SESSION_B), pending_id=pending(100), tool="workvm_status", arguments={})
        for _ in range(5):
            await asyncio.sleep(0)
        assert env.answers == [
            (
                pending(limit),
                {
                    "status": "error",
                    "error": {
                        "error": "unavailable",
                        "message": "Too many work VM calls of this session run at once. Try again.",
                    },
                },
            )
        ]
        assert calls.running == {SESSION_A: limit, SESSION_B: 1}
        release.set()
        await settle(calls)

    asyncio.run(run())
    assert len(workvms.calls) == limit + 1
    assert calls.running == {SESSION_A: 0, SESSION_B: 0}
    assert sum(answer["status"] == "ok" for _, answer in env.answers) == limit + 1


# -- calls_for and the handler -----------------------------------------------------------------------------------


def test_calls_for_creates_once_per_context(monkeypatch: pytest.MonkeyPatch) -> None:
    workvms = FakeWorkVms()
    seen: list[Any] = []

    def workvms_for(context: Any) -> Any:
        seen.append(context)
        return workvms

    monkeypatch.setattr(journal, "workvms_for", workvms_for)
    context = SimpleNamespace(_workvm_calls="not calls")
    first = journal.calls_for(context)
    assert cast(Any, first.workvms) is workvms
    assert journal.calls_for(context) is first
    assert seen == [context]
    monkeypatch.setattr(journal, "workvms_for", lambda context: None)
    assert journal.calls_for(SimpleNamespace()).workvms is None


@dataclass
class FakeJournal:
    env: FakeEnv
    found: LocalSession | None
    vm: VmRow = field(default_factory=lambda: VM)
    records: list[JournalRecord] = field(default_factory=list)

    async def session(self, conn: Any, record: JournalRecord) -> LocalSession | None:
        self.records.append(record)
        return self.found


def record(record_body: Json) -> JournalRecord:
    return JournalRecord(
        seq=7, ts="2026-11-23T10:00:00.000Z", session_id=SESSION_A, kind=journal.KIND, body=record_body
    )


def test_handler_answers_the_call_of_an_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    workvms = FakeWorkVms()
    monkeypatch.setattr(journal, "workvms_for", lambda context: workvms)
    env = FakeEnv()
    target = FakeJournal(env, session())
    entry = record(body("workvm_output", {"job_id": JOB}))

    async def run() -> None:
        await journal.HANDLERS[journal.KIND](target, cast(Any, None), entry)
        await settle(journal.calls_for(env.context))

    asyncio.run(run())
    assert target.records == [entry]
    assert workvms.calls == [(env, SESSION_A, "workvm_output", {"job_id": JOB})]
    assert env.answers == [(PENDING, {"status": "ok", "result": {"tool": "workvm_output"}})]
    assert env.vms == [VM]


def test_handler_ignores_a_session_of_another_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journal, "workvms_for", lambda context: pytest.fail("no Calls needed"))
    env = FakeEnv()
    target = FakeJournal(env, None)
    asyncio.run(journal.workvm_call(target, cast(Any, None), record({"garbage": True})))
    assert env.attempts == []
    assert not hasattr(env.context, journal.ATTRIBUTE)


def test_handler_refuses_an_invalid_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journal, "workvms_for", lambda context: None)
    target = FakeJournal(FakeEnv(), session())
    with pytest.raises(JournalRecordInvalid):
        asyncio.run(journal.workvm_call(target, cast(Any, None), record(body("workvm_exec", {}))))
