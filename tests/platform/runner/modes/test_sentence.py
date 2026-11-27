"""U-RUN-modes (KOR-75, ADR-098 section 3): the sentence - the quote check of `task.mode` against the verified
instruction of the turn, its pre-check, its formal rules and its executor."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent_runner.modes import executors, op_prechecks, quote
from agent_runner.operations import checks
from agent_runner.operations.extensions import CheckFailed, OperationFailed
from agent_runner.prompts.verify import PromptRefused
from agent_runner.sessions import records
from agent_runner.sync.http import HubRefusal, HubTransportError
from tests.platform.runner.modes.support import MAIN, ORG, OWNER, TASK, WG, session, with_conn

EVENT = "0193000c-0000-7000-8000-000000000020"
OLDER = "0193000c-0000-7000-8000-000000000021"
TEXT = "Bitte baue Feature X.\n  Nutze   den TESTMODUS dafür."


async def _prompt(conn: AsyncConnection[Any], event: str, seq: int, author: str, state: str = "accepted") -> None:
    await conn.execute(
        "INSERT INTO runner.prompt_event (event_id, org_id, wg_id, task_id, scope_seq, author_sig, sealed, author_id)"
        " VALUES (%s, %s, %s, %s, %s, 'sig', %s, %s)",
        (event, ORG, WG, TASK, seq, Jsonb({"header": {"id": event}}), author),
    )
    await conn.execute(
        "INSERT INTO runner.prompt_delivery (event_id, session_id, state, task_id, scope_seq)"
        " VALUES (%s, %s, %s, %s, %s)",
        (event, MAIN, state, TASK, seq),
    )


def test_fold() -> None:
    assert quote.fold("  Nutze \n den\tTESTMODUS ") == "nutze den testmodus"


def test_the_quote_is_checked_against_the_instruction_of_the_turn(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts = {EVENT: TEXT, OLDER: "halte dich an A38"}

    async def opened(_conn: Any, _key: bytes, prompt: Any) -> str:
        if prompt.event_id not in texts:
            raise PromptRefused("prompt_unreadable")
        return texts[prompt.event_id]

    monkeypatch.setattr(quote, "open_text", opened)

    async def body(conn: AsyncConnection[Any]) -> None:
        await session(conn)
        main = await records.get(conn, MAIN)
        assert main is not None
        assert not await quote.instructed(conn, b"k", main, OWNER, "nutze den testmodus")  # no prompt yet
        await _prompt(conn, OLDER, 1, OWNER)
        await _prompt(conn, EVENT, 2, OWNER)
        assert await quote.instructed(conn, b"k", main, OWNER, "nutze den   Testmodus")
        assert not await quote.instructed(conn, b"k", main, OWNER, "halte dich an A38")  # not this turn's prompt
        assert not await quote.instructed(conn, b"k", main, OWNER, "nutze den orchestrator")
        assert not await quote.instructed(conn, b"k", main, OWNER, " x ")  # too short once folded
        assert not await quote.instructed(conn, b"k", main, None, "nutze den testmodus")  # several persons or none
        assert not await quote.instructed(conn, b"k", main, ORG, "nutze den testmodus")  # another author
        assert not await quote.instructed(conn, b"k", replace(main, depth=1), OWNER, "nutze den testmodus")
        texts.pop(EVENT)
        assert not await quote.instructed(conn, b"k", main, OWNER, "nutze den testmodus")  # unreadable

    with_conn(store_dsn, body)


def test_the_precheck(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []
    answer = {"value": True}

    async def instructed(_conn: Any, key: bytes, session: Any, actor: Any, text: str) -> bool:
        seen.append((key, session, actor, text))
        return answer["value"]

    monkeypatch.setattr(quote, "instructed", instructed)
    monkeypatch.setattr(op_prechecks, "runtime", lambda _context: SimpleNamespace(storage_key=b"key"))

    async def main() -> None:
        async with AsyncConnectionPool(store_dsn, open=False) as pool:
            await pool.open()

            def request(params: dict[str, Any]) -> Any:
                operation = SimpleNamespace(actor_id=OWNER)
                return SimpleNamespace(
                    context=SimpleNamespace(pool=pool), operation=operation, session="s", params=params
                )

            await op_prechecks.mode_check(request({"mode": "test", "on": False}))
            assert seen == []  # off needs no quote
            await op_prechecks.mode_check(request({"mode": "test", "on": True, "quote": "nutze den testmodus"}))
            assert seen == [(b"key", "s", OWNER, "nutze den testmodus")]
            answer["value"] = False
            with pytest.raises(CheckFailed) as refused:
                await op_prechecks.mode_check(request({"mode": "test", "on": True}))
            assert refused.value.error() == {"code": "mode_not_instructed", "message": op_prechecks.NOT_INSTRUCTED}
            assert seen[-1][3] == ""

    asyncio.run(main())
    assert set(op_prechecks.PRECHECKS) == {"task.mode"}


def test_the_formal_rules() -> None:
    rules = checks.TYPES["task.mode"]
    assert rules.required == ("mode", "on")
    assert rules.fields["mode"]("merge_lane")
    assert not rules.fields["mode"]("Merge")
    assert not rules.fields["mode"](1)
    assert rules.fields["on"](True)
    assert not rules.fields["on"]("true")
    assert rules.fields["quote"]("abc")
    assert not rules.fields["quote"]("ab")


class Client:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, str, Any]] = []

    async def request(self, method: str, target: str, body: Any = None) -> dict[str, object]:
        self.calls.append((method, target, body))
        if self.failure is not None:
            raise self.failure
        return {"mode": None}


def _execute(monkeypatch: pytest.MonkeyPatch, client: Client, on: bool = True) -> dict[str, Any]:
    monkeypatch.setattr(executors, "runtime", lambda _context: SimpleNamespace(client=client))
    operation = SimpleNamespace(id="op", task_id=TASK)
    ctx = SimpleNamespace(context=None, operation=operation, params={"mode": "test", "on": on})
    return asyncio.run(executors.execute(ctx))  # type: ignore[arg-type]


def test_the_executor_reports_the_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client()
    assert _execute(monkeypatch, client) == {"mode": "test", "on": True}
    assert _execute(monkeypatch, client, on=False) == {"mode": "test", "on": False}
    target = f"/v1/device/tasks/{TASK}/modes/test"
    assert client.calls == [
        ("PUT", target, {"on": True, "operation_id": "op"}),
        ("PUT", target, {"on": False, "operation_id": "op"}),
    ]
    assert set(executors.EXECUTORS) == {"task.mode"}


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (HubRefusal(409, "mode_needs", {"modes": ["a38"]}), "mode_needs", False),
        (HubRefusal(409, "mode_needs"), "mode_needs", False),
        (HubRefusal(403, "forbidden"), "rights_denied", False),
        (HubRefusal(404, "not_found"), "not_found", False),
        (HubRefusal(409, "id_conflict"), "upstream_error", False),
        (HubTransportError("unreachable"), "hub_unreachable", True),
    ],
)
def test_the_executor_maps_refusals(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, code: str, retryable: bool
) -> None:
    with pytest.raises(OperationFailed) as failed:
        _execute(monkeypatch, Client(failure))
    assert (failed.value.code, failed.value.retryable) == (code, retryable)
    if code == "mode_needs":
        named = "a38" if isinstance(failure, HubRefusal) and failure.detail else "another mode"
        assert (
            failed.value.message == f"The mode needs {named} to be on first; ask the person whether to switch on both."
        )
