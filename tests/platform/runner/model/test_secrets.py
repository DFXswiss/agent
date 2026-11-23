"""U-RUN-hygiene for model access and quotas: the owner's API key and subscription token and the platform key never
reach a log line, the outbox, the hub, the store in plaintext or the session request; they go only to the gateway
(`PUT .../credential`) and to the usage endpoints (05-runner.md sections 2.6, 10.2, 10.6; repository rule: no
secrets in output).

Sessions of every kind are started, their usage is journaled, the credential is renewed and withdrawn, quotas and the
platform spend are polled against the usage double and the outbox is pushed; afterwards every log line, every outbox
body (opened), every item and request the hub received and every row of every table of the runner schema are scanned
for the secrets' markers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_runner import registry
from agent_runner.credentials import cache
from agent_runner.model.reconcile import STEPS
from agent_runner.orgs.book import book_for
from agent_runner.quota.openrouter import SpendPoller
from agent_runner.quota.subscription import SubscriptionPoller
from agent_runner.reconcile.engine import Run
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..quota.double import USAGE_PATH, UsageDouble
from ..sessions.kit import KEY, desired, session_id
from .kit import (
    ALICE,
    API_KEY,
    MARKERS,
    TOKEN,
    Env,
    Kit,
    api_key_body,
    attach,
    model_env,
    model_kit,
    store_record,
    subscription_body,
)

NOW = 1_795_000_000.0
USAGE = {"model": "claude-opus-5-5", "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
         "cache_write_tokens": 0, "thinking_tokens": 0, "context_window_tokens": 1_000_000, "request_bytes": 1,
         "response_bytes": 1, "status": 200}  # fmt: skip


async def journal_usage(found: Kit, row: Any) -> None:
    for n in (1, 2, 3):
        found.guest.emit(session_id(n), "usage", {"request_id": f"r{n}", **USAGE})
    stopping = asyncio.Event()
    reader = VmJournal(found.session_env, row, handlers=registry.journal_handlers())
    task = asyncio.ensure_future(reader.run(stopping))
    for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
        if found.guest.acked == found.guest.records[-1].seq:
            break
        await asyncio.sleep(0.005)
    stopping.set()
    await task


async def scanned(found: Kit, env: Env) -> list[str]:
    """Every log line, outbox body, hub item and request, store row and session request as text."""
    texts = [*found.lines, env.log()]
    texts += [json.dumps(body) for _kind, body in await found.outbox()]
    texts += [json.dumps(item) for item in env.hub.items()]
    texts += [request.raw.decode("utf-8", "replace") + json.dumps(request.body) for request in env.hub.requests]
    tables = [row[0] for row in await env.rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'runner'")]  # fmt: skip
    for table in tables:
        texts += [str(row[0]) for row in await env.rows(f"SELECT row_to_json(t)::text FROM runner.{table} t")]
    texts += [json.dumps(value) for name, value in found.guest.calls if name == "create_session"]
    return texts


def test_no_secret_leaves_the_gateway_path(store_dsn: str) -> None:
    assert all(any(marker in secret for marker in MARKERS) for secret in (KEY, API_KEY, TOKEN))

    async def main() -> None:
        async with model_env(store_dsn) as env:
            store_record(env, "claude_subscription", subscription_body())
            store_record(env, "anthropic_api_key", api_key_body())
            found = await model_kit(env)
            attach(found)
            starter = Starter(found.session_env)
            for n, preference in enumerate(("subscription", "api_key", "platform"), start=1):
                assert (await starter.start(desired(n, model_access_pref=preference))).started
            row = await vm_records.get(env.pool, "vm-00000001")
            assert row is not None
            await journal_usage(found, row)
            # renewal with a new version, then withdrawal of the subscription
            store_record(env, "anthropic_api_key", api_key_body(API_KEY + "-2"), version=2)
            await cache.refresh_owner(env.context, ALICE)
            run = Run(context=env.context, sync=env.sync, book=book_for(env.context))
            run.desired = {}
            await STEPS[0].run(run)
            del env.hub.records[(ALICE, "claude_subscription")]
            await cache.refresh_owner(env.context, ALICE)
            assert (await found.stored(session_id(1))).state == "failed"  # withdrawn
            # quota and spend polling
            store_record(env, "claude_subscription", subscription_body(), version=2)
            await cache.refresh_owner(env.context, ALICE)
            with UsageDouble(TOKEN, offset=3) as double:
                polls = SubscriptionPoller(context=env.context, url=double.url + USAGE_PATH)
                await polls.tick({"quota_poll_owners": [ALICE]}, NOW)
                double.token = KEY
                spend = SpendPoller(context=env.context, upstream=double.url + "/api", key=lambda: KEY)
                await spend.tick(NOW)
                assert len(double.requests) == 2
                assert spend.fetched_at == NOW
            await env.sync.pusher.drain()

            sent = [json.dumps(value) for name, value in found.guest.calls if name == "put_credential"]
            assert any(TOKEN in text for text in sent)
            assert any(API_KEY in text for text in sent)
            assert any(KEY in text for text in sent)
            texts = await scanned(found, env)
            assert len(texts) > 20
            assert [kind for kind, _body in await found.outbox()].count("quota") == 1
            assert not [text for text in texts if any(marker in text for marker in MARKERS)]

    asyncio.run(main())
