"""Reconcile step 8: catch-up wrapping (05-runner.md sections 2.2 step 8 and 2.5, 04-crypto.md section 5.11,
03-api.md section 6.1 `POST /v1/device/wraps`).

After every reconcile run (order 80, after the wraps of step 3 were stored) the device plans the wraps it may create
(`catchup.plan`), opens the keys it holds only for the moment of wrapping, wraps each key for the recipient's X25519
key from the verified log (HPKE, `crypto.wrap`) and signs it with the device key. At most 200 wraps per run go to the
hub in one request. The hub stores new slots and ignores existing ones (`{stored, ignored}`); both count as done.

The hub refuses the whole request when one wrap violates W1-W3 by its own (possibly newer) log state
(`wrap_unauthorized`, `header_invalid`, `signature_invalid`): the device then halves the request until the refused
wraps are isolated (a few refused slots cost a few requests, not one per wrap), so a single stale slot never blocks
the others, and remembers the refused ones until a verified log head changes. Other
refusals and transport errors fail the step (logged by the engine; the next run tries again). Devices never rotate:
a scope with a pending rotation is wrapped for its current epoch only, as the log says.
"""

from __future__ import annotations

from agent_runner.catchup import plan
from agent_runner.crypto.log.wraps import WrapSlot
from agent_runner.crypto.wrap import sign_wrap, wrap_key
from agent_runner.encoding import b64u_decode
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import held_slots, load_scope_key
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sync.http import HubRefusal

TARGET = "/v1/device/wraps"
REFUSALS = frozenset({"wrap_unauthorized", "header_invalid", "signature_invalid"})


def _line(run: Run, text: str) -> None:
    run.context.log.write(f"runner.catchup {text}\n")
    run.context.log.flush()


async def _post(run: Run, wraps: list[dict[str, object]]) -> None:
    answer = await run.sync.client.request("POST", TARGET, {"wraps": wraps})
    stored, ignored = answer.get("stored"), answer.get("ignored")
    _line(run, f"uploaded stored={stored} ignored={ignored}")


async def upload(run: Run, slots: list[WrapSlot], wraps: list[dict[str, object]]) -> tuple[int, int]:
    """Send the wraps; returns `(done, refused)` counts and updates the memory."""
    memory = plan.memory_for(run.context)
    at = plan.heads(run.book)
    try:
        await _post(run, wraps)
    except HubRefusal as refusal:
        if refusal.code not in REFUSALS:
            raise
        if len(wraps) == 1:
            plan.refuse(memory, slots, at)
            _line(run, f"refused code={refusal.code} scope={slots[0].scope} recipient={slots[0].recipient}")
            return 0, 1
        half = len(wraps) // 2
        first = await upload(run, slots[:half], wraps[:half])
        second = await upload(run, slots[half:], wraps[half:])
        return first[0] + second[0], first[1] + second[1]
    plan.finish(memory, slots)
    return len(slots), 0


async def catch_up(run: Run) -> int:
    """Plan, wrap and upload; returns the number of wraps the hub accepted (stored or already present)."""
    if run.desired is None:
        return 0
    book = run.book
    keys = run.context.identity.keys
    memory = plan.memory_for(run.context)
    async with session(run.pool) as conn:
        held = await held_slots(conn)
        slots = plan.plan(book, held, memory)
        wraps: list[dict[str, object]] = []
        for slot in slots:
            key = await load_scope_key(conn, run.sync.storage_key, slot.scope, slot.scope_id, slot.epoch)
            assert key is not None
            wrapped = wrap_key(
                b64u_decode(plan.recipient_kem(book, slot)),
                scope=slot.scope,
                scope_id=slot.scope_id,
                epoch=slot.epoch,
                recipient=slot.recipient,
                key=key,
            )
            wraps.append(
                {
                    "scope": slot.scope,
                    "scope_id": slot.scope_id,
                    "epoch": slot.epoch,
                    "recipient_id": slot.recipient,
                    "wrap": wrapped,
                    "sig": sign_wrap(
                        keys.signing_key(),
                        scope=slot.scope,
                        scope_id=slot.scope_id,
                        epoch=slot.epoch,
                        recipient=slot.recipient,
                        wrap=wrapped,
                    ),
                }
            )
    if not slots:
        return 0
    done, refused = await upload(run, slots, wraps)
    _line(run, f"planned={len(slots)} done={done} refused={refused}")
    return done


async def step(run: Run) -> None:
    await catch_up(run)


STEPS = [ReconcileStep(80, "catchup", step)]
