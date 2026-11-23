"""Reconcile step 1: the desired state (05-runner.md section 2.2, 03-api.md section 6.1 `GET /v1/device/state`).

A device that is still `pending_certification` (platform enrollment, 05 section 2.1) halts the run: it polls the
state on every run (every 30 s) until the hub answers `active`, and does nothing else. For an active device the
answer is kept on the run for the later steps, together with the verified org heads taken just before the request
(`Run.asked_heads`, the reference of step 2 for a rollback), and the hub's `last_origin_seq` is handed to the outbox,
which adopts it only while its own position is unknown (fresh or wiped store, decisions/RUN-02.md item 13).
"""

from __future__ import annotations

from agent_runner.orgs.db import session
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Run
from agent_runner.sync import outbox

TARGET = "/v1/device/state"
PENDING = "pending_certification"


async def fetch_state(run: Run) -> None:
    book = run.book
    async with book.lock, session(run.pool) as conn:
        await book.ensure_loaded(conn)
        run.asked_heads = {org: log.head for org, log in book.logs.items()}
    answer = await run.sync.client.request("GET", TARGET)
    run.device_state = wire.device_state(answer)
    if run.device_state == PENDING:
        run.halt(PENDING)
        return
    wire.desired_orgs(answer)
    last = wire.last_origin_seq(answer)
    run.desired = answer
    async with session(run.pool) as conn:
        await outbox.adopt_hub_position(conn, run.sync.storage_key, last)
