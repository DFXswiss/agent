"""U-RUN-model: gateway `usage` records -> outbox `usage` deltas (05-runner.md section 10.4, 03-api.md sections 6.1 and
7.2; ADR-010).

Sessions are started for each access kind, then the journal loop of RUN-04 hands the records to the handlers of every
area (`registry.journal_handlers`); the items are pushed to the contract-checking hub.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.guestlink.client import JournalRecord
from agent_runner.model import merge, usage
from agent_runner.model.catalog import load_catalog
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..sessions.kit import desired, session_id
from .kit import FLASH, OPUS, Env, Kit, api_key_body, model_env, model_kit, store_record, subscription_body

Body = Callable[[Env], Awaitable[None]]
ZERO = dict.fromkeys(usage.USAGE_COUNTS, 0)


@pytest.fixture(autouse=True)
def immediate_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `usage` item per record, as before the merge (KOR-35); the merge has its own suite (model/test_merge.py)."""
    monkeypatch.setattr(merge, "WINDOW_S", 0.0)


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            await body(env)

    asyncio.run(main())


def record(model: str, **values: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": "req_1",
        "model": model,
        **ZERO,
        "context_window_tokens": 1_000_000,
        "request_bytes": 10,
        "response_bytes": 20,
        "status": 200,
    }
    body.update(values)
    return body


async def started(env: Env, preference: str) -> tuple[Kit, VmJournal]:
    store_record(env, "claude_subscription", subscription_body())
    store_record(env, "anthropic_api_key", api_key_body())
    found = await model_kit(env)
    starter = Starter(found.session_env)
    assert (await starter.start(desired(model_access_pref=preference))).started
    row = await vm_records.get(env.pool, "vm-00000001")
    assert row is not None
    return found, VmJournal(found.session_env, row, handlers=registry.journal_handlers())


async def consume(found: Kit, reader: VmJournal) -> None:
    stopping = asyncio.Event()
    task = asyncio.ensure_future(reader.run(stopping))
    last = found.guest.records[-1].seq
    for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
        if found.guest.acked >= last or task.done():
            break
        await asyncio.sleep(0.005)
    stopping.set()
    await task


TOKENS = {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 5000, "cache_write_tokens": 100}


@pytest.mark.parametrize(
    ("preference", "model", "values", "basis", "cost"),
    [
        ("platform", FLASH, {**TOKENS, "cost_usd": 0.001234, "provider": "DeepInfra"}, "provider", 1234),
        # a refused or failed platform request without OpenRouter cost costs nothing
        ("platform", FLASH, {**TOKENS, "status": 502}, "provider", 0),
        # opus list price: 4 / 20 / 0.2 / 5 USD per Mtok; Claude Code's own figures are never read
        ("api_key", OPUS, {**TOKENS, "thinking_tokens": 50}, "list_price", 9500),
        ("subscription", OPUS, TOKENS, "subscription_equivalent", 9500),
        ("api_key", "claude-unknown", TOKENS, "list_price", 0),
    ],
)
def test_cost_per_basis(
    store_dsn: str, *, preference: str, model: str, values: dict[str, Any], basis: str, cost: int
) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, preference)
        found.guest.emit(session_id(), "usage", record(model, **values))
        await consume(found, reader)
        (item,) = await found.of_kind("usage")
        assert item["cost_basis"] == basis
        assert item["delta"]["cost_micro_usd"] == cost
        assert item["delta"]["requests"] == 1
        assert item["delta"]["input_tokens"] == 1000
        assert item["context_used_tokens"] == 1000 + 5000 + 100
        assert item["context_window_tokens"] == 1_000_000
        assert item["last_provider"] == values.get("provider")
        await env.sync.pusher.drain()  # the hub checks the item against contracts/outbox/usage.json
        assert [i["item_kind"] for i in env.hub.items()].count("usage") == 1

    run(store_dsn, body)


def test_context_keeps_the_last_figures_over_requests_without_context(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "api_key")
        sid = session_id()
        # a refusal before any request: no context known yet
        found.guest.emit(sid, "usage", record(OPUS, status=400, context_window_tokens=0))
        found.guest.emit(sid, "usage", record(OPUS, input_tokens=40_000, cache_read_tokens=10_000))
        found.guest.emit(sid, "usage", record(OPUS, status=429, context_window_tokens=0))  # budget refusal
        found.guest.emit(sid, "usage", record(OPUS, status=500, output_tokens=3))  # failed upstream, no input
        found.guest.emit(sid, "usage", record(OPUS, input_tokens=60_000))
        await consume(found, reader)
        items = await found.of_kind("usage")
        figures = [(i["context_used_tokens"], i["context_window_tokens"]) for i in items]
        assert figures == [(0, 0), (50_000, 1_000_000), (50_000, 1_000_000), (50_000, 1_000_000), (60_000, 1_000_000)]
        assert sum(i["delta"]["requests"] for i in items) == 5

    run(store_dsn, body)


def test_provider_outside_the_allow_list_is_audited_and_counted(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        sid = session_id()
        found.guest.emit(sid, "usage", record(FLASH, provider="Elsewhere", provider_outside_allowlist=True))
        found.guest.emit(sid, "usage", record(FLASH, provider_outside_allowlist=True))  # no provider: no audit
        found.guest.emit(sid, "usage", record(FLASH, provider="Fine"))
        await consume(found, reader)
        audits = [a for a in await found.of_kind("audit") if a["action"] == "model.provider_outside_allowlist"]
        assert audits == [
            {
                "action": "model.provider_outside_allowlist",
                "result": "ok",
                "task_id": desired().task_id,
                "session_id": sid,
                "target_kind": "session",
                "target_id": sid,
                "detail": {"provider": "Elsewhere", "model": FLASH},
            }
        ]
        assert usage.counters(env.context)[usage.OUTSIDE_COUNTER] == 1
        assert f"provider_outside_allowlist session_id={sid} provider=Elsewhere" in "\n".join(found.lines)
        await env.sync.pusher.drain()

    run(store_dsn, body)


@pytest.mark.parametrize(
    "body",
    [
        {"model": "bad model"},
        record(FLASH, provider=""),
        record(FLASH, input_tokens=-1),
        record(FLASH, output_tokens=True),
        record(FLASH, context_window_tokens=None),
        record(FLASH, cost_usd="1"),
        record(FLASH, cost_usd=True),
        record(FLASH, cost_usd=1e10),
    ],
)
def test_malformed_records_are_refused(body: dict[str, Any]) -> None:
    catalog = load_catalog(None)
    for access in ("platform", "api_key"):
        session = SimpleNamespace(id="s", model_access=access)
        with pytest.raises(JournalRecordInvalid):
            usage.usage_item(session, body, catalog, None)  # type: ignore[arg-type]


def test_a_session_without_recorded_access_counts_as_provider_cost() -> None:
    session = SimpleNamespace(id="s", model_access=None)
    item = usage.usage_item(session, record(FLASH, cost_usd=0.5), load_catalog(None), None)  # type: ignore[arg-type]
    assert (item["cost_basis"], item["delta"]["cost_micro_usd"]) == ("provider", 500_000)


def test_memory_and_counters_live_on_the_daemon_context_or_the_env() -> None:
    env = SimpleNamespace(context=None)
    journal = SimpleNamespace(env=env)
    assert usage.memory(journal) is usage.memory(journal)
    assert isinstance(env.model_context_memory, usage.ContextMemory)
    context = SimpleNamespace()
    journal.env = SimpleNamespace(context=context)
    assert usage.memory(journal) is context.model_context_memory
    assert usage.counters(context) is usage.counters(context)
    assert isinstance(usage.counters(context), Counter)


def test_records_of_unknown_sessions_are_skipped(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        found.guest.emit("01930005-0000-7000-8000-0000000000bb", "usage", record(FLASH))
        await consume(found, reader)
        assert await found.of_kind("usage") == []
        assert isinstance(found.guest.records[-1], JournalRecord)

    run(store_dsn, body)
