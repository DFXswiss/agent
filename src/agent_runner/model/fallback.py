"""Running sessions on a used-up access: moved, paused and continued by themselves (KOR-50, ADR-081 sections 4 and 5).

`run_once(deliverer)` is the reconcile step `model_fallback` (order 57, after `billing`, on every desired state) and
the daemon task of the same name (every `TICK_S`). Each session is handled under the deliverer's lock of that session,
so no prompt is handed over in between; every pause is recorded in `runner.model_pause` (`model.pauses`) before the
guest is asked anything.

For a running session (`working`, `idle`) without a pause whose access is used up (`model.limits`):

1. **Same kind** - a session on a used-up operator file whose pool has another selectable file is left to its renewal
   (`model.gateways`, in place, the model stays);
2. a **subagent session** (one turn) is never paused: without such a file it is stopped with `model_quota_exhausted`
   (towards a hub without `model_fallback.api`: `model_access_unavailable`); its operation fails and the parent learns
   it like any ended subagent;
3. **other kind** - another access is allowed now (`model.access.decide`): the session is paused like a credit pause
   (guest `interrupt` when a turn runs, `suspend`; reason `model_switched`) and resumed at once; the resume resolves
   the access anew and, when a turn was interrupted, the session gets the switch sentence (`SWITCHED`);
4. **nothing allowed** - the session is suspended with the reason `model_quota_exhausted` and, towards a hub with
   `model_fallback.api`, `wait_until` and `dfx_choice` in its transition; prompts that arrive meanwhile stay pending
   (`billing.pause.deferred`). Without any release time (only refused files) nothing is done: the gateway keeps what
   it holds.

An open pause (also the one a new main session got at its start, `model.access.hold`) is carried on at every run:
a running session is suspended first when it is not yet; a session that started on the access with the earliest
release keeps its first prompt back (`model.session_request`) and is not suspended while it is still in the start
chain (the hub refuses a start state going straight to `suspended`; it gets no `wait_until` then) - its pause just
ends once any access is allowed, and its prompt is delivered (should its access still be used up, it is moved like a
running session); then the pause
ends as soon as an access is allowed again (its `until` passed, a quota reading or a renewed file released one
earlier, the owner's setting or the task's consent allows a DFX access). An interrupted session is resumed and gets
the switch sentence (`SWITCHED`) when its access kind changed, else `AVAILABLE`; a session that was idle stays
suspended until its next prompt, which is then delivered. **Credit first**: while the session's credit pause holds
(`billing.pause.credit_holds`) nothing is continued here. **No flapping**: a running session is never moved back;
the next resume or start resolves anew.

**Refused access** (HOT-26, `renew`; ADR-092 section 5, ADR-081 section 4 point 7): a main session whose access the
provider refused (`401` after a renewal revoked the former token, `model.revocation`) holds its prompts from that
record on. Once its turn has ended (`idle`) and the credit allows it, the run renews its provisioning
(`model.gateways.refresh`: the renewed record or file of the same subscription, else another usable file of the
operator's pool by the rules of ADR-052) and, as soon as a valid access is there (`revocation.renewed`), hands it the
renewal sentence (`RENEWED`) - no person needs to write anything; the held prompts follow it in their order. Towards a
hub with `model_fallback.api >= 2` the audit `session.model_switched` with the cause `renewed` (metadata only) gives
the task its quiet line. After `revocation.MAX_TRIES` continuations without a successful answer, without a valid access
within `revocation.WAIT_S`, or when the session ended, the refusal is given up: the error stays visible as before and
the held prompts are delivered. The session is never suspended for it and no `runner.model_pause` row is written.

The sentences have a stable prompt ID per pause (`prompt_id`; a refusal: its time), so a repeated hand-over is the same
prompt for the guest; they carry no user text.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

from agent_runner.billing import pause as credit
from agent_runner.crypto.ids import uuid7
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.model import access, exhaustion, gateways, limits, operator, pauses, policy, revocation
from agent_runner.model.access import Resolved, Wait
from agent_runner.prompts.resume import RESUMED, RETRY, desired_of, vm_running
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.records import LocalSession, time_text
from agent_runner.sessions.stop import stop_session
from agent_runner.sync.outbox import LOCK_ID

TICK_S = 5.0
RUNNING = frozenset({"working", "idle"})
SUSPENDABLE = RUNNING
"""Only a running session is suspended (the hub refuses a start state going straight to `suspended`): a waiting start
that is still in the start chain (`prompt_delivering` without its first prompt) only holds its prompts."""
RESUMABLE = frozenset({"suspended", "resuming"})
SWITCH_REASON = "model_switched"
NAMES = {
    "subscription": "the own Claude subscription",
    "api_key": "the own Anthropic API key",
    "operator": "the operator's Claude subscription",
    "platform": "DFX model access",
}
SWITCHED = (
    "The platform switched this session's model access from {source} to {target} because {source} is used up until"
    " {time}. Continue the interrupted work."
)
AVAILABLE = "The model quota is available again. Continue the interrupted work."
RENEWED = (
    "The platform renewed this session's model access; the last request was refused with the former access."
    " Continue the interrupted work."
)
RENEWED_CAUSE = "renewed"
LOCK = "_model_fallback_lock"


def sentence(source: str, target: str, until: datetime | None) -> str:
    """The continuation prompt of a pause (ADR-081 section 5)."""
    if source == target:
        return AVAILABLE
    when = "further notice" if until is None else until.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return SWITCHED.format(source=NAMES[source], target=NAMES[target], time=when)


def prompt_id(session_id: str, paused_at: datetime) -> str:
    """The stable ID of the continuation prompt of one pause."""
    seed = hashlib.sha256(f"model:{session_id}:{paused_at.timestamp()}".encode()).digest()[:10]
    return uuid7(int(paused_at.timestamp() * 1000), seed)


def _line(env: SessionEnv, text: str) -> None:
    env.log(f"runner.model {text}")


async def label_of(env: SessionEnv, session: LocalSession) -> tuple[str, str | None]:
    """The access label (`model.access.Resolved.label`) and access key of the session's current access."""
    name = await operator.bound(env.context, session.id) if session.model_access == "subscription" else None
    label = "operator" if name is not None else str(session.model_access)
    return label, exhaustion.access_key(env, session, name)


async def suspend(env: SessionEnv, session_id: str, reason: str, extra: dict[str, Any]) -> None:
    """The `suspended` transition with `reason` and the wait fields of ADR-081 (`records.advance` with a body that
    carries `extra`)."""
    async with env.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        found = await records.get(conn, session_id, lock=True)
        if found is not None and records.allowed(found.state, "suspended"):
            await records.update(conn, session_id, state="suspended")
            updated = await records.get(conn, session_id)
            assert updated is not None
            version = await records.agent_version(conn, updated.vm_id)
            body = records.transition_body(updated, "suspended", reason, env.at(), version)
            await env.outbox.enqueue(conn, "session_transition", {**body, **extra})
        await pauses.update(conn, session_id, suspended=True)


async def _pause_guest(env: SessionEnv, session: LocalSession, pause: pauses.Pause) -> bool:
    """Interrupt (a running turn) and suspend the session's guest session; True when it is suspended."""
    working = session.state == "working"
    if working and not await credit.guest_control(env, session, "interrupt", label="model"):
        return False
    tries = credit.SUSPEND_TRIES if working else 1
    if not await credit.guest_control(env, session, "suspend", tries, label="model"):
        return False
    if pause.kind == pauses.WAIT:
        extra: dict[str, Any] = {}
        if policy.api(env.desired):
            until = pause.until
            extra = {
                "wait_until": None if until is None else time_text(until.timestamp()),
                "dfx_choice": pause.dfx_choice,
            }
        await suspend(env, session.id, access.QUOTA, extra)
    else:
        await suspend(env, session.id, SWITCH_REASON, {})
    _line(env, f"model_paused session_id={session.id} kind={pause.kind}")
    return True


async def _used_up(env: SessionEnv, session: LocalSession) -> tuple[str, limits.Limit] | None:
    """The label and the limit of a running session's used-up access that its renewal cannot fix in place (step 1),
    None when its access is not used up."""
    label, key = await label_of(env, session)
    limit = None if key is None else limits.limits_for(env.context).active(key)
    if key is None or limit is None:
        return None
    if key.startswith(limits.OPERATOR):
        pool = operator.pool_for(env.context)
        assert pool is not None  # the session is bound to one of its files
        if pool.choose(exclude=(key[len(limits.OPERATOR) :],), org_id=session.org_id) is not None:
            return None  # step 1: the renewal moves it to another file
    return label, limit


async def check(deliverer: Any, session: LocalSession) -> bool:
    """A running session without a pause (steps 1-4 of the module docstring); True when it now holds its prompts."""
    env: SessionEnv = deliverer.env
    found = await _used_up(env, session)
    if found is None:
        return False
    label, limit = found
    if session.depth > 0:
        reason = access.QUOTA if policy.api(env.desired) else access.UNAVAILABLE
        await stop_session(env, session.id, reason=reason)
        _line(env, f"model_subagent_stopped session_id={session.id}")
        return False
    outcome = await access.decide(env, desired_of(env, session))
    if outcome is None:
        return False
    wait = isinstance(outcome, Wait)
    async with deliverer.lock(session.id):
        async with env.transaction() as conn:
            current = await records.get(conn, session.id)
            if current is None or current.state not in RUNNING:
                return False
            await pauses.record(
                conn,
                session.id,
                kind=pauses.WAIT if wait else pauses.SWITCH,
                interrupted=current.state == "working",
                at=env.wall(),
                until=outcome.until if isinstance(outcome, Wait) else limit.until,
                choice=outcome.choice if isinstance(outcome, Wait) else False,
                from_access=label,
                from_model=current.model_id,
            )
            if isinstance(outcome, Wait):
                await pauses.report(conn, env, session.id, outcome.until, outcome.choice)
            pause = await pauses.get(conn, session.id)
        assert pause is not None
        paused = await _pause_guest(env, current, pause)
    if wait or not paused:
        return wait
    return await _continue(deliverer, session.id, pause)


async def _end(env: SessionEnv, session_id: str) -> None:
    async with env.transaction() as conn:
        await pauses.end(conn, env, session_id)
    _line(env, f"model_pause_ended session_id={session_id}")


async def _continue(deliverer: Any, session_id: str, pause: pauses.Pause) -> bool:
    """End the pause: resume an interrupted session and hand it the sentence (module docstring); True while it must
    be tried again."""
    env: SessionEnv = deliverer.env
    async with deliverer.lock(session_id):
        async with env.transaction() as conn:
            session = await records.get(conn, session_id)
        if pause.interrupted and session is not None and session.state not in records.FINAL:
            if session.state in RESUMABLE:
                outcome = await deliverer.resumer.resume(session)
                if outcome == RETRY:
                    return True
                if outcome != RESUMED:
                    await _end(env, session_id)
                    return False
            async with env.transaction() as conn:
                current = await records.get(conn, session_id)
            assert current is not None
            row = await vm_running(env, current)
            if row is None:
                return True
            target, _key = await label_of(env, current)
            text = sentence(pause.from_access, target, pause.until)
            try:
                async with await env.client(row) as guest:
                    await guest.register_prompt(session_id, prompt_id(session_id, pause.paused_at), text)
            except (GuestError, RunnerError) as exc:
                _line(env, f"model_continue_failed session_id={session_id} code={exc.code}")
                return True
            _line(env, f"model_continued session_id={session_id} from={pause.from_access} to={target}")
        await _end(env, session_id)
    return False


async def carry_on(deliverer: Any, session: LocalSession, pause: pauses.Pause) -> bool:
    """An open pause (module docstring); True while the session holds its prompts."""
    env: SessionEnv = deliverer.env
    waiting = pause.kind == pauses.WAIT
    outcome = await access.decide(env, desired_of(env, session)) if waiting else None
    if isinstance(outcome, Wait):
        await _follow(env, session.id, pause, outcome)
    if await credit.credit_holds(env, session):
        return waiting
    if not pause.suspended:
        if session.state in RESUMABLE:
            async with env.transaction() as conn:
                await pauses.update(conn, session.id, suspended=True)
        else:
            _label, key = await label_of(env, session)
            starting = session.state not in SUSPENDABLE
            if isinstance(outcome, Resolved) and not pause.interrupted and (starting or outcome.key == key):
                # free again before it was suspended; a start that waits on another access than the one now free
                # gets its prompt there and, should that access still be used up, is moved like a running session
                await _end(env, session.id)
                return False
            async with deliverer.lock(session.id):
                async with env.transaction() as conn:
                    current = await records.get(conn, session.id)
                assert current is not None  # listed by this run; only this device ends it
                if current.state not in SUSPENDABLE or not await _pause_guest(env, current, pause):
                    return waiting
    if waiting and not isinstance(outcome, Resolved):
        return True
    return await _continue(deliverer, session.id, pause)


async def _follow(env: SessionEnv, session_id: str, pause: pauses.Pause, outcome: Wait) -> None:
    """The wait changed (another release time, the DFX choice): the row follows it and the hub is told; an unchanged
    wait sends nothing (`pauses.report`)."""
    until = pauses.stamp(outcome.until)
    async with env.transaction() as conn:
        if (until, outcome.choice) != (pause.until, pause.dfx_choice):
            await pauses.update(conn, session_id, until=until, dfx_choice=outcome.choice)
        await pauses.report(conn, env, session_id, outcome.until, outcome.choice)


async def renew(deliverer: Any, session: LocalSession) -> bool:
    """A main session whose access the provider refused (module docstring: refused access); True while it holds its
    prompts."""
    env: SessionEnv = deliverer.env
    context = env.context
    found = revocation.revocations(context)
    refused = found.refused[session.id]
    tries = found.tries.get(session.id, 0)
    if tries >= revocation.MAX_TRIES:
        return _give_up(env, session.id, "tries")
    if session.state == "idle" and not await credit.credit_holds(env, session):
        await gateways.refresh(env, session)
        stamp = access.provisions(context).stamps.get(session.id)
        if revocation.renewed(context, session.id, refused, stamp):
            return await _renewed(deliverer, session.id, refused, tries)
    if env.clock() - refused.at >= revocation.WAIT_S:
        return _give_up(env, session.id, "no_access")
    return True


def _give_up(env: SessionEnv, session_id: str, reason: str) -> bool:
    revocation.settle(env.context, session_id, continued=False)
    _line(env, f"access_renewal_given_up session_id={session_id} reason={reason}")
    return False


async def _renewed(deliverer: Any, session_id: str, refused: revocation.Refusal, tries: int) -> bool:
    """Continue the refused session on its valid access: the renewal sentence (its prompt ID stable per refusal and
    try), then the audit line; True while it must be tried again (it is no longer idle, its VM or guest cannot be
    reached now)."""
    env: SessionEnv = deliverer.env
    async with deliverer.lock(session_id):
        async with env.transaction() as conn:
            current = await records.get(conn, session_id)
        assert current is not None  # listed by this run; only this device ends it
        row = await vm_running(env, current) if current.state == "idle" else None
        if row is None:
            return True
        target, _key = await label_of(env, current)
        try:
            async with await env.client(row) as guest:
                prompt = prompt_id(f"{session_id}:renewed:{tries}", refused.paused_at)
                await guest.register_prompt(session_id, prompt, RENEWED)
        except (GuestError, RunnerError) as exc:
            _line(env, f"model_continue_failed session_id={session_id} code={exc.code}")
            return True
    revocation.settle(env.context, session_id, continued=True)
    _line(env, f"model_continued session_id={session_id} from={refused.label} to={target} cause={RENEWED_CAUSE}")
    if policy.renewed_api(env.desired):
        detail = {
            "from_access": access.KIND_OF[refused.label],
            "to_access": access.KIND_OF[target],
            "from_model": current.model_id,
            "to_model": current.model_id,
            "cause": RENEWED_CAUSE,
            "until": None,
        }
        audit = {
            "action": "session.model_switched",
            "result": "ok",
            "task_id": current.task_id,
            "session_id": session_id,
            "target_kind": "session",
            "target_id": session_id,
            "detail": detail,
        }
        async with env.transaction() as conn:
            await env.outbox.enqueue(conn, "audit", audit)
    return False


def run_lock(context: Any) -> asyncio.Lock:
    """One run at a time per daemon: the reconcile step and the daemon task never overlap."""
    found = getattr(context, LOCK, None)
    if isinstance(found, asyncio.Lock):
        return found
    created = asyncio.Lock()
    setattr(context, LOCK, created)
    return created


async def run_once(deliverer: Any) -> None:
    """One run over the sessions of this device (module docstring), serialized per daemon (`run_lock`). The sessions
    it holds replace the held set as a whole; a wait recorded meanwhile by a start or resume (`pauses.pend`) stays held
    until a run has seen its row."""
    env: SessionEnv = deliverer.env
    context = env.context
    if context is None:
        return
    async with run_lock(context):
        await _run(deliverer, env, context)


async def _run(deliverer: Any, env: SessionEnv, context: Any) -> None:
    await limits.prune(context)
    async with env.transaction() as conn:
        local = await records.listed(conn)
        open_pauses = {pause.session_id: pause for pause in await pauses.open_pauses(conn)}
    state = pauses.state_of(context)
    before = set(state.sessions)
    held_now: set[str] = set()
    live = {session.id for session in local}
    for session_id in [sid for sid in open_pauses if sid not in live]:
        await _end(env, session_id)
    revocation.prune(context, live)
    refused = revocation.revocations(context).refused
    for session in local:
        found = open_pauses.get(session.id)
        try:
            if found is not None:
                held = await carry_on(deliverer, session, found)
            elif session.id in refused:
                held = await renew(deliverer, session)
            elif session.state in RUNNING:
                held = await check(deliverer, session)
            else:
                held = False
        except Exception as exc:
            _line(env, f"model_fallback_failed session_id={session.id} error={type(exc).__name__}")
            held = session.id in before  # unknown: as before
        if held:
            held_now.add(session.id)
    state.sessions = held_now
    state.pending -= set(open_pauses)
    for session_id in [sid for sid in state.reported if sid not in live]:
        del state.reported[session_id]
