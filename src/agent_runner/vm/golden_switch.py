"""Workgroup VMs move to the current golden by themselves; paused sessions move with them (ADR-093, KOR-70;
05-runner.md section 3.6).

A live workgroup VM (`running` or `stopped`) whose golden is not the newest `ready` one is **outdated**. `judge(env,
retire, row)` says whether it may move (`Verdict.holds` empty) and which paused sessions move with it:

- every session on the VM that is not `stopped` or `failed` is a main session in state `suspended` - else the hold
  `session_active` (a main session in any other state) or `subagent` (a child session: it lives in its parent's VM and
  is never moved);
- each paused session has a complete newest checkpoint, as the retire hooks judge it (`Retire.checkpoints`:
  `checkpoint_due`, `checkpoint_missing`, `workspace_incomplete`; a session no hook vouches for is held).

`GoldenSwitch.run_pass()` (task `vm-pool`, after every refill) visits the outdated VMs - the ones a member asked for
(`request`, from the desired state's `vm_switch`, section 5) first, then by VM ID - each under its workgroup's lock, so
no session start chooses the VM meanwhile:

1. a hold by a session or an incomplete workspace: the VM stays as it is (reported);
2. a stopped VM held only by a due or missing checkpoint (e.g. a checkpoint of before migration `0034_golden_switch`,
   whose completeness is unknown, or a final checkpoint the stop did not finish in time) is started to take fresh
   checkpoints, when a slot is free without stopping another workgroup's VM (`capacity` otherwise); attempts follow
   `retry_delay`, at most `MAX_REFRESHES` until a request by hand;
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
`MAX_SESSIONS` sessions are listed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

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
SWITCH_STATES = ("running", "stopped")
MAX_HOLDS = 16
MAX_SESSIONS = 64
MAX_REFRESHES = 3
"""Starts of a stopped VM for fresh checkpoints before the switch gives up until a request by hand (a checkpoint that
keeps being dropped never becomes complete; the VM is not started and stopped forever)."""
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


@dataclass(frozen=True)
class Hold:
    reason: str
    session_id: str | None = None


@dataclass(frozen=True)
class Verdict:
    holds: tuple[Hold, ...]
    sessions: tuple[str, ...]
    """The paused main sessions that move with the VM."""


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
class _Backoff:
    count: int
    reason: str
    next_at: datetime


@dataclass
class GoldenSwitch:
    """The switch of outdated workgroup VMs of one `VmManager` (module docstring)."""

    manager: VmManager
    requested: list[str] = field(default_factory=list)
    hub_reads_status: bool = False
    """The hub knows `upgrade` and `replaced_by` (its desired state has `vm_switch`); an older hub refuses them."""
    _reported: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    _refreshes: dict[str, _Backoff] = field(default_factory=dict)
    _failures: dict[str, _Backoff] = field(default_factory=dict)

    @property
    def env(self) -> VmEnv:
        return self.manager.env

    def request(self, ids: Iterable[str]) -> None:
        """VMs a member asked to switch now (desired state `vm_switch`): first in the next pass, without waiting for a
        retry; a VM no longer listed is no longer requested."""
        wanted = list(dict.fromkeys(ids))
        new = [vm_id for vm_id in wanted if vm_id not in self.requested]
        for vm_id in new:
            self._refreshes.pop(vm_id, None)
            self._failures.pop(vm_id, None)
        self.requested = wanted
        if new:
            self.manager.wake_refill()

    async def run_pass(self) -> list[str]:
        """Visit every outdated VM once; returns the IDs of the VMs replaced."""
        env = self.env
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

    async def _switch(self, row: VmRow, golden: GoldenRef) -> bool:
        env, manager = self.env, self.manager
        verdict = await judge(env, manager.retire, row)
        held = await self._held(row, golden, verdict)
        if held is not None:
            await self._report(row, golden, "waiting", verdict, *held)
            return False
        if row.state == "stopped" and verdict.holds:
            # only due or missing checkpoints: start the VM to take them (with a backoff between attempts)
            started = await self._refresh(row, golden, verdict)
            if started is None:
                return False
            row = started
        if row.state == "running":
            await self._report(row, golden, "switching", verdict)
            row = await manager.stop_for_switch(row)
            verdict = await judge(env, manager.retire, row)
            if verdict.holds:
                await self._report(row, golden, "waiting", verdict)
                return False
        return await self._replace(row, golden, verdict)

    async def _held(self, row: VmRow, golden: GoldenRef, verdict: Verdict) -> tuple[Hold, ...] | None:
        """The extra holds when the VM must wait now (none: `()`), None when the switch may go on."""
        reasons = {hold.reason for hold in verdict.holds}
        if reasons & SESSION_HOLDS or "workspace_incomplete" in reasons:
            return ()
        failure = self._failures.get(row.id)
        if failure is not None and self.env.now() < failure.next_at:
            return (Hold(failure.reason),)
        refresh = self._refreshes.get(row.id)
        if (
            row.state == "stopped"
            and verdict.holds
            and refresh is not None
            and (self.env.now() < refresh.next_at or refresh.count >= MAX_REFRESHES)
        ):
            return ()
        return None

    async def _refresh(self, row: VmRow, golden: GoldenRef, verdict: Verdict) -> VmRow | None:
        """Start the stopped VM so its checkpoints can be taken; None when no slot is free (reported)."""
        env = self.env
        before = self._refreshes.get(row.id)
        count = 1 if before is None else before.count + 1
        self._refreshes[row.id] = _Backoff(count, "refresh", env.now() + timedelta(seconds=retry_delay(count)))
        try:
            started = await self.manager.restart_for_switch(row)
        except RunnerError as exc:
            if exc.code != "capacity_exhausted":
                raise
            await self._report(row, golden, "waiting", verdict, Hold("capacity"))
            return None
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
        before = self._failures.get(row.id)
        count = 1 if before is None else before.count + 1
        self._failures[row.id] = _Backoff(count, reason, env.now() + timedelta(seconds=retry_delay(count)))
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
        return {
            "golden_version": golden.version,
            "golden_built_at": None if built_at is None else _time(built_at),
            "state": state,
            "holds": [_hold_json(hold, since[key]) for hold, key in zip(holds, keys, strict=True)][:MAX_HOLDS],
            "sessions": [] if verdict is None else list(verdict.sessions[:MAX_SESSIONS]),
        }

    async def _forget(self, vm_id: str) -> None:
        self._reported.pop(vm_id, None)
        self._refreshes.pop(vm_id, None)
        self._failures.pop(vm_id, None)
        await records.forget_holds(self.env.db, vm_id)


def _time(value: datetime) -> str:
    return format_time(value)


def _hold_json(hold: Hold, since: datetime) -> dict[str, Any]:
    found: dict[str, Any] = {"reason": hold.reason, "since": _time(since)}
    if hold.session_id is not None:
        found["session_id"] = hold.session_id
    return found
