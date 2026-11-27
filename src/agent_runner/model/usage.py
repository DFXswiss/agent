"""Journal kind `usage` -> outbox `usage` delta (05-runner.md section 10.4, 03-api.md sections 6.1 and 7.2; ADR-010,
ADR-025).

The guest model gateway writes one `usage` record per model request (tokens from the stream, OpenRouter's
`usage.cost` and `provider`, the catalog context window of the model). For a session this device runs on that VM the
handler turns it into the body of an outbox item `usage` and merges it into the session's pending item in the journal
page's transaction (`model.merge`: one item per session and 10 s, totals unchanged; KOR-35):

- `delta`: one request and the token counts of the record; `cost_micro_usd` by the cost basis of the session's access
  kind (the recorded `model_access` of the local session):
  * `platform`: OpenRouter's `usage.cost` (`cost_basis: provider`); a response without cost (refused or failed
    request) costs 0;
  * `api_key`: catalog list price of the record's model times its tokens (`list_price`);
  * `subscription`: the same list price as an equivalent (`subscription_equivalent`);
  Claude Code's own cost figures are never used (they price unknown models as Opus, Spike C).
- `context_used_tokens` = `input + cache_read + cache_write` of the latest request and `context_window_tokens` =
  the catalog window the gateway used. A request that carried no context (a refusal before the upstream, a failed
  request: no input tokens or no window) keeps the last figures of the session, so the context fill does not drop
  to 0 because a request was refused. The last figures are kept per daemon in memory (`ContextMemory`, one small entry
  per session of the daemon's lifetime; updated before the page commits, so a rolled-back page can leave a figure
  that the replayed page writes again).
- `last_provider`: the record's `provider` (OpenRouter only).
- Billing (KOR-31, ADR-062, AUD-04 W10), only towards a hub whose device state has `billing.api`: `model` (the
  record's model), `access_origin` (`platform`, `operator` or `own`, `agent_runner.billing.state.origin`) and `at` (the
  record's time) - the hub charges `platform` and `operator`. An older hub refuses unknown fields, so they are left
  out there.
- `triggered_by` (ADR-083 section 2), only towards a hub whose device state has the section `prompt_queue`: the person
  whose instruction the request follows from, `credentials.acting.stamp` when the record is handled (a main session:
  the acting user of its turn; a child: the acting user of its `subagent.request`); null when unknown.
- `subscription_id` (ADR-103), only towards a hub whose device state has `subscriptions.api >= 2`: the Claude
  subscription record that served the request (`model.served`); null for any other access.

For a session bound to a token file of the operator's subscriptions the record's `status` is handed to the pool
(`model.operator.observed`: `401`/`403` block the file the session was provisioned from, `429` defers it; KOR-23).
The status of every session on an access that can run out (the owner's own subscription, an operator file, the DFX
key of an org device) is handed to `model.exhaustion.observe` (KOR-50, ADR-081 section 2: a `429` starts the check
whether the access is used up, a `402` of the DFX key marks it, a success ends a streak).

A `provider` outside the catalog allow-list (the gateway flags `provider_outside_allowlist`) also writes the audit
`model.provider_outside_allowlist` (metadata: provider and model), a fact line and counts it in the daemon counter
`provider_outside_allowlist` (`counters`; 08-monitoring.md names no metric for it, so it is not exported); the request
was not blocked.

A record that cannot be turned into an item raises `JournalRecordInvalid` (skipped with a fact line by the journal
loop).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection

from agent_runner.billing import state as billing
from agent_runner.credentials import acting
from agent_runner.guestlink.client import JournalRecord
from agent_runner.model import access, exhaustion, merge, operator, served
from agent_runner.prompts import hub_queue
from agent_runner.sessions.journal import MAX_COUNT, JournalRecordInvalid
from agent_runner.sessions.records import LocalSession

USAGE_COUNTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "thinking_tokens")
COST_BASIS = {"platform": "provider", "api_key": "list_price", "subscription": "subscription_equivalent"}
OUTSIDE_COUNTER = "provider_outside_allowlist"
ATTRIBUTE = "model_context_memory"
COUNTERS_ATTRIBUTE = "model_counters"
_LABEL_RE = re.compile(r"^[ -~]{1,64}$")
_TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


@dataclass
class ContextMemory:
    """Session ID -> (context used, context window) of its latest request with context."""

    last: dict[str, tuple[int, int]] = field(default_factory=dict)


def _holder(journal: Any) -> Any:
    """The daemon context when the session env has one, else the env (tests without a daemon context)."""
    env = journal.env
    return env.context if env.context is not None else env


def memory(journal: Any) -> ContextMemory:
    holder = _holder(journal)
    found = getattr(holder, ATTRIBUTE, None)
    if isinstance(found, ContextMemory):
        return found
    created = ContextMemory()
    setattr(holder, ATTRIBUTE, created)
    return created


def counters(holder: Any) -> Counter[str]:
    """Counters of the model area of a daemon context (`provider_outside_allowlist`)."""
    found = getattr(holder, COUNTERS_ATTRIBUTE, None)
    if isinstance(found, Counter):
        return found
    created: Counter[str] = Counter()
    setattr(holder, COUNTERS_ATTRIBUTE, created)
    return created


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise JournalRecordInvalid("usage")
    return value


def _provider_cost(body: Mapping[str, Any]) -> int:
    cost = body.get("cost_usd")
    if cost is None:
        return 0
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost < 1e9:
        raise JournalRecordInvalid("usage")
    return round(cost * 1_000_000)


def usage_item(session: LocalSession, body: Mapping[str, Any], catalog: Any, last: tuple[int, int] | None) -> Any:
    """The outbox body of one `usage` record of `session` (module docstring); `last` = the session's latest context."""
    model, provider = body.get("model"), body.get("provider")
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise JournalRecordInvalid("usage")
    if provider is not None and (not isinstance(provider, str) or not _LABEL_RE.fullmatch(provider)):
        raise JournalRecordInvalid("usage")
    counts = {name: _count(body.get(name)) for name in USAGE_COUNTS}
    window = _count(body.get("context_window_tokens"))
    basis = COST_BASIS.get(session.model_access or "platform", "provider")
    if basis == "provider":
        cost = _provider_cost(body)
    else:
        _provider_cost(body)  # still checked: a malformed record is refused whatever the access
        cost = min(catalog.list_cost_micro_usd(model, counts), MAX_COUNT)
    used = min(counts["input_tokens"] + counts["cache_read_tokens"] + counts["cache_write_tokens"], MAX_COUNT)
    context = (used, window) if used > 0 and window > 0 else (last or (0, window))
    return {
        "session_id": session.id,
        "delta": {"requests": 1, **counts, "cost_micro_usd": cost},
        "cost_basis": basis,
        "context_used_tokens": context[0],
        "context_window_tokens": context[1],
        "last_provider": provider,
    }


async def handle(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    remembered = memory(journal)
    body = record.body
    item = usage_item(session, body, journal.env.catalog, remembered.last.get(session.id))
    if billing.api(journal.env.desired):
        item.update(model=body["model"], access_origin=await billing.origin(conn, session))
        if _TIME_RE.fullmatch(record.ts):
            item["at"] = record.ts
    if hub_queue.api(journal.env.desired):
        item["triggered_by"] = await acting.stamp(conn, session)
    if served.api(journal.env.desired):
        item["subscription_id"] = await served.subscription(conn, session)
    await merge.add(conn, journal.env, item)
    remembered.last[session.id] = (item["context_used_tokens"], item["context_window_tokens"])
    bound: tuple[str, bool] | None = None
    if session.model_access == "subscription":
        context = journal.env.context
        stamp = access.provisions(context).stamps.get(session.id) if context is not None else None
        provisioned = stamp.operator[1] if stamp is not None and stamp.operator is not None else None
        bound = await operator.observed(context, session.id, body.get("status"), provisioned)
    key = exhaustion.access_key(journal.env, session, None if bound is None else bound[0])
    exhaustion.observe(journal.env, body.get("status"), key, True if bound is None else bound[1])
    provider = item["last_provider"]
    if body.get("provider_outside_allowlist") is True and provider is not None:
        audit = {
            "action": "model.provider_outside_allowlist",
            "result": "ok",
            "task_id": session.task_id,
            "session_id": session.id,
            "target_kind": "session",
            "target_id": session.id,
            "detail": {"provider": provider, "model": body["model"]},
        }
        await journal.env.outbox.enqueue(conn, "audit", audit)
        counters(_holder(journal))[OUTSIDE_COUNTER] += 1
        journal.env.log(f"runner.model provider_outside_allowlist session_id={session.id} provider={provider}")
