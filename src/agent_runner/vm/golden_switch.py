"""Workgroup VMs move to the current golden by themselves; paused sessions move with them (ADR-093, KOR-70;
05-runner.md section 3.6).

A live workgroup VM (`running` or `stopped`) whose golden is not the newest `ready` one is **outdated**. `judge(env,
retire, row)` says whether it may move (`Verdict.holds` empty) and which paused sessions move with it:

- every session on the VM that is not `stopped` or `failed` is a main session in state `suspended` - else the hold
  `session_active` (a main session in any other state) or `subagent` (a child session: it lives in its parent's VM and
  is never moved);
- each paused session has a complete newest checkpoint or nothing to save, as the retire hooks judge it
  (`Retire.checkpoints`: `checkpoint_due`, `checkpoint_missing`, `workspace_incomplete`, `checkpoint_impossible`; a
  session no hook vouches for is held). A session that never worked holds nothing (HOT-24).

`GoldenSwitch.run_pass()` (task `vm-pool`, after every refill) visits the outdated VMs - the ones a member asked for
(`request`, from the desired state's `vm_switch`, section 5) first, then by VM ID - each under its workgroup's lock, so
no session start chooses the VM meanwhile:

1. a hold by a session, an incomplete workspace or a session that can never be checkpointed on this VM
   (`checkpoint_impossible`): the VM stays as it is (reported) - it is neither started nor stopped for the switch. A
   request by hand covers the `checkpoint_impossible` holds the device had reported before the time of the request
   (`forced`, only with a hub that knows the reason): those sessions move without the state of the old VM;
2. a stopped VM held only by a due or missing checkpoint (e.g. a checkpoint of before migration `0034_golden_switch`,
   whose completeness is unknown, or a final checkpoint the stop did not finish in time) is started to take fresh
   checkpoints, when a slot is free without stopping another workgroup's VM (`capacity` otherwise); attempts follow
   `retry_delay`, at most `MAX_REFRESHES` until a new request by hand or until the VM ran for its sessions (a start
   without a free slot counts no attempt). The attempts and the requests taken over are kept in the store
   (`runner.golden_switch_attempt`, `runner.golden_switch_request`), so a restart neither starts the VM again nor
   counts a request still listed as new (HOT-24);
3. a running VM is stopped first (`VmManager._stop`: the retire hooks take the final checkpoints of its sessions) and
   judged again; a hold now leaves it stopped and as it is;
4. the new VM - a pool VM of the newest golden, else a clone (no other workgroup's VM is stopped for it) - is
   assigned and running; only then the old VM is deleted (`vm_state` `deleted` with `replaced_by`) and the switch
   recorded (`runner.golden_switch`). A failure deletes nothing: `capacity` or `switch_failed`, the next attempt after
   `retry_delay` (5 min doubling, at most 1 h; a request by hand tries again at once).

The paused sessions keep their `vm_id`; their next prompt resumes them on another VM of the workgroup (normally the new
one) from their checkpoint (`prompts.resume`, `contexts.restore`, place `golden`).

Every outdated VM's status goes to the hub with its `vm_state` (`upgrade`: `{golden_version, golden_built_at, state:
waiting|switching, holds: [{reason, session_id?, since}], sessions}`), whenever it changes, and `upgrade: null` once a
VM that had one is current again - only to a hub that knows these fields (its desired state names `vm_switch`;
`hub_reads_status`, set by the reconcile step `vm_switch`), since an older hub refuses unknown fields. `since` is the
time the runner first saw the cause (`runner.golden_switch_hold`, kept across restarts). At most `MAX_HOLDS` holds and
`MAX_SESSIONS` sessions are listed. A `checkpoint_impossible` hold carries `checkpoint_at`, the checkpoint the session
would continue from after a switch by hand (`Retire.fallbacks`; absent: it starts fresh) - only to a hub whose desired
state names `vm_upgrade_version` 2 or later (`hub_reads_impossible`); an older one gets `checkpoint_missing` instead.
Such a hub gets up to `MAX_HOLDS_SHOWN` holds, those a request does not cover yet first, and the runner records when
each hold was first reported to it (`reported_at`): a request covers only holds reported before it.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.errors import RunnerError
from agent_runner.vm import records
from agent_runner.vm.extensions import Retire
from agent_runner.vm.lima import LimaError
from agent_runner.vm.pool import format_time
from agent_runner.vm.records import GoldenRef, VmRow

if TYPE_CHECKING:
    from agent_runner.vm.lifecycle import VmManager
    from agent_runner.vm.pool import VmEnv

SESSION_HOLDS = frozenset({"session_active", "subagent"})
REFRESHABLE = frozenset({"checkpoint_due", "checkpoint_missing"})
"""Holds a fresh checkpoint may end (the VM must run to take it)."""
FORCEABLE = frozenset({"checkpoint_impossible"})
"""Holds a switch requested by hand ignores: the session continues without the state of the old VM (HOT-24)."""
STAYING = frozenset({"workspace_incomplete"}) | FORCEABLE
"""Holds of a paused session that no start or stop of the VM can end."""
SWITCH_STATES = ("running", "stopped")
MAX_HOLDS = 16
"""Holds reported to a hub before HOT-24 (its contract allowed 16)."""
MAX_HOLDS_SHOWN = 64
"""Holds reported to a hub of `vm_upgrade_version` 2 or later (HOT-24): as many as sessions are listed."""
MAX_SESSIONS = 64
MAX_REFRESHES = 3
"""Starts of a stopped VM for fresh checkpoints before the switch gives up until a new request by hand (a checkpoint
that keeps being dropped never becomes complete; the VM is not started and stopped forever, also not across
restarts)."""
RETRY_BASE_S = 300.0
RETRY_CAP_S = 3600.0
_VM_ID_RE = re.compile(r"^vm-[0-9a-f]{8}$")


def retry_delay(failures: int) -> float:
    """5 min doubling per failed attempt, at most one hour."""
    return float(min(RETRY_CAP_S, RETRY_BASE_S * 2 ** min(failures - 1, 10)))


def vm_ids(value: Any) -> list[str]:
    """The VM IDs of the desired state's `vm_switch` (anything malformed is ignored)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and _VM_ID_RE.fullmatch(item)]


def request_times(value: Any) -> dict[str, datetime]:
    """The times of the desired state's `vm_switch_at` (anything malformed is ignored)."""
    if not isinstance(value, dict):
        return {}
    found: dict[str, datetime] = {}
    for vm_id, text in value.items():
        if isinstance(vm_id, str) and _VM_ID_RE.fullmatch(vm_id) and isinstance(text, str):
            with contextlib.suppress(CryptoError):
                found[vm_id] = parse_time(text)
    return found


@dataclass(frozen=True)
class Hold:
    reason: str
    session_id: str | None = None


@dataclass(frozen=True)
class Verdict:
    holds: tuple[Hold, ...]
    sessions: tuple[str, ...]
    """The paused main sessions that move with the VM."""

    def blocking(self, forced: frozenset[str] = frozenset()) -> tuple[Hold, ...]:
        """The holds that keep the VM: all of them but the `FORCEABLE` ones of the sessions a request covers."""
        return tuple(hold for hold in self.holds if not (hold.reason in FORCEABLE and hold.session_id in forced))


async def judge(env: VmEnv, retire: Retire, row: VmRow) -> Verdict:
    """Whether the VM may move to a newer golden (module docstring)."""
    holds: list[Hold] = []
    paused: list[str] = []
    for session in await records.sessions_on(env.db, row.id):
        if session.depth > 0:
            holds.append(Hold("subagent", session.id))
        elif session.state != "suspended":
            holds.append(Hold("session_active", session.id))
        else:
            paused.append(session.id)
    if paused:
        reasons = await retire.checkpoints(row, paused)
        holds.extend(Hold(reason, sid) for sid in paused if (reason := reasons[sid]) is not None)
    return Verdict(tuple(holds), tuple(paused))


@dataclass
class GoldenSwitch:
    """The switch of outdated workgroup VMs of one `VmManager` (module docstring)."""

    manager: VmManager
    requested: list[str] = field(default_factory=list)
    hub_reads_status: bool = False
    """The hub knows `upgrade` and `replaced_by` (its desired state has `vm_switch`); an older hub refuses them."""
    hub_reads_impossible: bool = False
    """The hub knows `checkpoint_impossible` and `checkpoint_at` (its desired state has `vm_upgrade_version` >= 2)."""
    _reported: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    _loaded: bool = False

    @property
    def env(self) -> VmEnv:
        return self.manager.env

    async def request(self, ids: Iterable[str], times: Mapping[str, datetime] | None = None) -> None:
        """VMs a member asked to switch now (desired state `vm_switch`, with `vm_switch_at`: the time of each request;
        without one the runner's time, once): first in the next pass, without waiting for a retry; a VM no longer
        listed is no longer requested. Only a request the store does not hold yet, or one with a newer time, is new
        (its attempts start again); one still listed after a restart, or listed without a time, is not."""
        wanted = list(dict.fromkeys(ids))
        found = times or {}
        new = await records.take_requests(self.env.db, [(vm_id, found.get(vm_id)) for vm_id in wanted], self.env.now())
        self.requested = wanted
        self._loaded = True
        if new:
            self.manager.wake_refill()

    async def run_pass(self) -> list[str]:
        """Visit every outdated VM once; returns the IDs of the VMs replaced."""
        env = self.env
        if not self._loaded:  # after a restart, before the first desired state: the requests taken over before
            self.requested = await records.switch_requests(env.db)
            self._loaded = True
        golden = await records.ready_golden(env.db)
        rows = [row for row in await records.live(env.db, *SWITCH_STATES) if row.wg_id is not None]
        order = {vm_id: index for index, vm_id in enumerate(self.requested)}
        rows.sort(key=lambda row: (order.get(row.id, len(order)), row.id))
        switched: list[str] = []
        for row in rows:
            if env.stopping():
                break
            if golden is None or row.golden_version == golden.version:
                await self._report(row, None)
                continue
            if await self._visit(row, golden):
                switched.append(row.id)
        live = {row.id for row in rows}
        for vm_id in [vm_id for vm_id in self._reported if vm_id not in live]:
            await self._forget(vm_id)
        await records.prune_attempts(env.db, sorted(live))
        return switched

    async def _visit(self, row: VmRow, golden: GoldenRef) -> bool:
        assert row.wg_id is not None
        async with self.manager.lock(row.wg_id):
            current = await records.get(self.env.db, row.id)
            if current is None or current.state not in SWITCH_STATES:
                return False
            try:
                return await self._switch(current, golden)
            except (RunnerError, LimaError) as exc:
                await self._failed(current, golden, "switch_failed", exc.code)
                return False

    async def forced(self, row: VmRow) -> frozenset[str]:
        """The sessions whose `checkpoint_impossible` hold a request by hand of the VM covers: the ones reported to a
        hub that shows the hold and its consequence (`vm_upgrade_version` 2 or later) before the time of the request -
        what the member saw when asking (HOT-24)."""
        if not self.hub_reads_impossible or row.id not in self.requested:
            return frozenset()
        at = await records.request_time(self.env.db, row.id)
        if at is None:
            return frozenset()
        reported = await records.hold_reported(self.env.db, row.id)
        return frozenset(
            key for (reason, key), first in reported.items() if reason in FORCEABLE and key and first <= at
        )

    async def _switch(self, row: VmRow, golden: GoldenRef) -> bool:
        env, manager = self.env, self.manager
        forced = await self.forced(row)
        if row.state == "running":
            await records.forget_attempt(env.db, row.id, "refresh")  # it ran for its sessions: fresh chances
        verdict = await judge(env, manager.retire, row)
        held = await self._held(row, verdict.blocking(forced))
        if held is not None:
            await self._report(row, golden, "waiting", verdict, *held)
            return False
        if row.state == "stopped" and verdict.blocking(forced):
            # only due or missing checkpoints: start the VM to take them (with a backoff between attempts)
            started = await self._refresh(row, golden, verdict)
            if started is None:
                return False
            row = started
        if row.state == "running":
            await self._report(row, golden, "switching", verdict)
            row = await manager.stop_for_switch(row)
            verdict = await judge(env, manager.retire, row)
            if verdict.blocking(forced):
                await self._report(row, golden, "waiting", verdict)
                return False
        return await self._replace(row, golden, verdict)

    async def _held(self, row: VmRow, holds: tuple[Hold, ...]) -> tuple[Hold, ...] | None:
        """The extra holds when the VM must wait now (none: `()`), None when the switch may go on; `holds` are the
        ones that keep it (`Verdict.blocking`)."""
        reasons = {hold.reason for hold in holds}
        if reasons & SESSION_HOLDS or reasons & STAYING:
            return ()  # no start or stop can end these: the VM stays as it is
        failure = await records.attempt(self.env.db, row.id, "failure")
        if failure is not None and self.env.now() < failure.next_at:
            return (Hold(failure.reason),)
        refresh = await records.attempt(self.env.db, row.id, "refresh")
        if (
            row.state == "stopped"
            and holds
            and refresh is not None
            and (self.env.now() < refresh.next_at or refresh.count >= MAX_REFRESHES)
        ):
            return ()
        return None

    async def _refresh(self, row: VmRow, golden: GoldenRef, verdict: Verdict) -> VmRow | None:
        """Start the stopped VM so its checkpoints can be taken; None when no slot is free (reported)."""
        env = self.env
        before = await records.attempt(env.db, row.id, "refresh")
        count = 1 if before is None else before.count + 1
        try:
            started = await self.manager.restart_for_switch(row)
        except RunnerError as exc:
            if exc.code != "capacity_exhausted":
                raise
            await self._report(row, golden, "waiting", verdict, Hold("capacity"))
            return None  # no slot: no attempt counted
        next_at = env.now() + timedelta(seconds=retry_delay(count))
        await records.put_attempt(env.db, row.id, "refresh", records.Attempt(count, "refresh", next_at))
        env.log("vm.golden_refresh", {"vm_id": row.id, "attempt": count})
        return started

    async def _replace(self, row: VmRow, golden: GoldenRef, verdict: Verdict) -> bool:
        env, manager = self.env, self.manager
        await self._report(row, golden, "switching", verdict)
        try:
            new = await manager.replacement(golden, row, replaced_by=self.hub_reads_status)
        except RunnerError as exc:
            if exc.code != "capacity_exhausted":
                raise
            await self._failed(row, golden, "capacity", exc.code, verdict)
            return False
        await self._forget(row.id)
        env.log(
            "vm.golden_switched",
            {"vm_id": row.id, "new_vm_id": new.id, "golden_version": golden.version, "sessions": len(verdict.sessions)},
        )
        return True

    async def _failed(
        self, row: VmRow, golden: GoldenRef, reason: str, code: str, verdict: Verdict | None = None
    ) -> None:
        env = self.env
        before = await records.attempt(env.db, row.id, "failure")
        count = 1 if before is None else before.count + 1
        next_at = env.now() + timedelta(seconds=retry_delay(count))
        await records.put_attempt(env.db, row.id, "failure", records.Attempt(count, reason, next_at))
        env.log("vm.golden_switch_failed", {"vm_id": row.id, "reason": reason, "code": code, "attempt": count})
        current = await records.get(env.db, row.id)
        if current is not None and current.state != "deleted":
            found = verdict if verdict is not None else await judge(env, self.manager.retire, current)
            await self._report(current, golden, "waiting", found, Hold(reason))

    # -- status ------------------------------------------------------------------------------------------------

    async def _report(
        self,
        row: VmRow,
        golden: GoldenRef | None,
        state: str | None = None,
        verdict: Verdict | None = None,
        *extra: Hold,
    ) -> None:
        """Send the VM's `upgrade` status to the hub when it changed (`None`: current)."""
        status = None if golden is None or state is None else await self._status(row, golden, state, verdict, extra)
        if not self.hub_reads_status or self._reported.get(row.id, None) == status:
            return  # nothing new (a VM never reported counts as current), or a hub that would refuse the field
        self._reported[row.id] = status
        await self.env.emit(row, upgrade=status)

    async def _status(
        self, row: VmRow, golden: GoldenRef, state: str, verdict: Verdict | None, extra: tuple[Hold, ...]
    ) -> dict[str, Any]:
        env = self.env
        holds = [*(() if verdict is None else verdict.holds), *extra]
        keys = [(hold.reason, hold.session_id or "") for hold in holds]
        since = await records.hold_times(env.db, row.id, keys, env.now())
        built_at = await records.golden_built_at(env.db, golden.version)
        if self.hub_reads_impossible:
            # the holds a request does not cover yet first, so a member always sees what still keeps the VM
            covered = await self.forced(row)
            holds.sort(key=lambda hold: hold.reason in FORCEABLE and hold.session_id in covered)
            shown = holds[:MAX_HOLDS_SHOWN]
            if self.hub_reads_status:
                await records.mark_reported(
                    env.db, row.id, [(hold.reason, hold.session_id or "") for hold in shown], env.now()
                )
        else:
            shown = holds[:MAX_HOLDS]
        impossible = [hold.session_id for hold in shown if hold.reason in FORCEABLE and hold.session_id is not None]
        fallbacks = await self.manager.retire.fallbacks(row, impossible) if impossible else {}
        return {
            "golden_version": golden.version,
            "golden_built_at": None if built_at is None else _time(built_at),
            "state": state,
            "holds": [self._hold_json(hold, since[(hold.reason, hold.session_id or "")], fallbacks) for hold in shown],
            "sessions": [] if verdict is None else list(verdict.sessions[:MAX_SESSIONS]),
        }

    def _hold_json(self, hold: Hold, since: datetime, fallbacks: dict[str, datetime | None]) -> dict[str, Any]:
        found: dict[str, Any] = {"reason": hold.reason, "since": _time(since)}
        if hold.session_id is not None:
            found["session_id"] = hold.session_id
        if hold.reason in FORCEABLE:
            if not self.hub_reads_impossible:
                found["reason"] = "checkpoint_missing"  # the nearest reason an older hub knows
            elif hold.session_id is not None and (at := fallbacks.get(hold.session_id)) is not None:
                found["checkpoint_at"] = _time(at)
        return found

    async def _forget(self, vm_id: str) -> None:
        self._reported.pop(vm_id, None)
        await records.forget_attempts(self.env.db, vm_id)
        await records.forget_holds(self.env.db, vm_id)


def _time(value: datetime) -> str:
    return format_time(value)
