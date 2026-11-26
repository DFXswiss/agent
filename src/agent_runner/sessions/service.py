"""The session service of one daemon (05-runner.md sections 2.2 step 5, 6.1, 6.3; 1.1 task `guestlink`).

`service_for(context)` builds it once per daemon context (None while the device is not enrolled) from the daemon's
collaborators (`build_env`): the store pool, the sync runtime (outbox and push), the VM manager of VM-02 (whose
`on_unreachable` it takes over), the verified logs, the model catalog, the platform key file and the extension points
of `sessions.extensions`.

- `reconcile(desired, book)` (reconcile step 50): compare the desired sessions with the local ones and start or stop
  them in background tasks (`schedule_start`, `schedule_stop`); the reconcile run never waits for a VM.
  * a desired main session in state `requested` that is not known locally, in a workgroup this device is assigned to
    by the verified log, is started when its owner has access to the workgroup in that log (otherwise it is refused
    and audited once, `runner.inbound_rejected` with code `owner_not_member`);
  * a desired session in state `stopping`, a local session the hub no longer lists, and a local session whose
    workgroup is no longer assigned (reason `unassigned`) are stopped;
  * a local session left in a start state before `agent_starting` without a start task (the daemon restarted during
    its start) fails with `device_shutdown`;
  * subagent sessions (depth > 0) belong to RUN-11 and are left alone.
- `vm_unreachable(row)` (VM-02 health pass): the active sessions of the VM fail with `guest_unreachable`.
- `vms_lost()` (task `guestlink`, once after the startup reconciliation of the VM area, ADR-087 section 3.7): a
  session that has not ended and ran in a VM whose instance is gone (a host reboot or a deleted instance: its row is
  `deleted` or missing) fails with `guest_unreachable`, so the hub places a successor instead of showing the task
  working for ever. `idle` and `suspended` sessions run nothing and are left alone (their next prompt resumes them on
  another VM with the context restore, ADR-072).
- `vms_stopped()` (task `guestlink` after `vms_lost`, and at the end of every `reconcile`; HOT-25): a main session
  still `working` or `idle` in a VM whose row is `stopped` (the device restarted - a power cut - while it worked; the
  VM reconciliation found the instance stopped, so no agent process of it runs) becomes `suspended` with the reason
  `device_restart`. Like any paused session its next prompt, or one that already waits, resumes it from its
  transcript on that VM (restarted) or from its newest context checkpoint on another (`prompts.resume`); left
  `working` it would never get a prompt again. Sessions with a start or stop task are left to it.
- `run_journals(stopping)` (task `guestlink`): one journal loop (`consume.VmJournal`) per running VM, started when a
  VM becomes running (at the latest after `POLL_S`, at once for a session start) and cancelled when it no longer
  runs; a loop that fails is restarted with backoff (1 s doubling to 30 s).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from agent_runner import registry
from agent_runner.inbox.pull import inbox_for
from agent_runner.model import dfxkey, selection
from agent_runner.orgs import audit
from agent_runner.orgs.book import book_for
from agent_runner.sessions import records
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.desired import Desired
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.events import fail
from agent_runner.sessions.extensions import first_prompt_provider, request_amenders, state_observers
from agent_runner.sessions.start import Starter, StartReport
from agent_runner.sessions.stop import stop_guest, stop_session
from agent_runner.sync.runtime import runtime
from agent_runner.vm import records as vm_records
from agent_runner.vm.records import VmRow
from agent_runner.vm.tasks import manager_for

ATTRIBUTE = "session_service"
POLL_S = 5.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
OWNER_REFUSED = "owner_not_member"
INTERRUPTED = frozenset({"requested", "vm_assigning", "vm_ready", "sandbox_ready"})
RESTING = frozenset({"idle", "suspended"})
"""Session states that run nothing in their VM (`vms_lost` leaves them alone)."""
STRANDED = frozenset({"working", "idle"})
"""States of a main session that need its agent process: in a `stopped` VM they are suspended (`vms_stopped`)."""
DEVICE_RESTART = "device_restart"
NO_LOCAL = frozenset({"requested", "stopped", "failed"})
"""Desired states for which a missing local session is expected: not started yet (`requested`, started by
`schedule_start`) or ended."""


class SessionService:
    def __init__(
        self,
        env: SessionEnv,
        *,
        handlers: Mapping[str, Callable[..., Any]] | None = None,
        observers: Iterable[Any] = (),
        sleep: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> None:
        self.env = env
        self.starter = Starter(env)
        self.handlers = dict(handlers if handlers is not None else {})
        self.observers = list(observers)
        self.sleep = sleep
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.readers: dict[str, asyncio.Task[None]] = {}
        self.reports: list[StartReport] = []
        self.wake = asyncio.Event()
        env.watchers.append(self.watch)

    # -- background work -----------------------------------------------------------------------------------------

    def _spawn(self, session_id: str, work: Coroutine[Any, Any, Any]) -> bool:
        if session_id in self.tasks:
            work.close()
            return False
        task = asyncio.ensure_future(work)
        self.tasks[session_id] = task
        task.add_done_callback(functools.partial(self._done, session_id))
        return True

    def _done(self, session_id: str, task: asyncio.Task[Any]) -> None:
        self.tasks.pop(session_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.env.line(f"task_failed session_id={session_id} error={type(exc).__name__}")
        elif isinstance(task.result(), StartReport):
            self.reports.append(task.result())

    def schedule_start(self, desired: Desired) -> bool:
        return self._spawn(desired.session_id, self.starter.start(desired))

    def schedule_stop(self, session_id: str, reason: str | None = None) -> bool:
        return self._spawn(session_id, stop_session(self.env, session_id, reason))

    async def idle(self) -> None:
        """Until every scheduled start and stop finished (tests, shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    # -- reconcile -----------------------------------------------------------------------------------------------

    async def reconcile(self, desired: list[Desired], book: Any) -> None:
        env = self.env
        assigned = set(book.assignments())
        wanted = {item.session_id: item for item in desired}
        async with env.transaction() as conn:
            local = await records.listed(conn)
            known = await records.known_ids(conn, list(wanted))
        local_ids = {session.id for session in local}
        for item in desired:
            if item.depth != 0:
                continue
            if (item.org_id, item.wg_id) not in assigned:
                env.line(f"not_assigned session_id={item.session_id}")
                continue
            await self._desired(item, book, known, local_ids)
        for session in local:
            if session.depth != 0 or session.id in self.tasks:
                continue
            if session.id not in wanted:
                self.schedule_stop(session.id)
            elif (session.org_id, session.wg_id) not in assigned:
                self.schedule_stop(session.id, "unassigned")
            elif session.state in INTERRUPTED:
                await self._interrupted(session)
        await self.vms_stopped()

    async def _desired(self, item: Desired, book: Any, known: set[str], local_ids: set[str]) -> None:
        """One assigned main session of the desired state against the store."""
        if item.state == "requested" and item.session_id not in known:
            if self._owner_member(book, item):
                self.schedule_start(item)
            else:
                await self._refuse_owner(book, item)
        elif item.state == "stopping" and item.session_id in local_ids:
            self.schedule_stop(item.session_id)
        elif item.state not in NO_LOCAL and item.session_id not in known:
            await self.store_lost(item)

    @staticmethod
    def _owner_member(book: Any, item: Desired) -> bool:
        """The task owner named by the hub has explicit access to the workgroup in the verified log (B2: the hub may
        not run sessions for someone outside the workgroup, nor use their credentials; an admin's inherited read does
        not make it a session owner, ADR-058 section 1)."""
        log = book.log(item.org_id)
        return log is not None and log.state.explicit_access(item.owner_id, item.wg_id) is not None

    async def _refuse_owner(self, book: Any, item: Desired) -> None:
        if not book.remember_refusal(("session_owner", item.session_id)):
            return
        async with self.env.transaction() as conn:
            await audit.reject(
                self.env.context,
                cast(Any, self.env.outbox),
                conn,
                source="assignment",
                code=OWNER_REFUSED,
                target_kind="session",
                target_id=item.session_id,
            )

    async def store_lost(self, item: Desired) -> None:
        """A session the hub still lists for this device but the store does not know: the store was wiped
        (02-data-model.md section 4), its VM, transcript and guest session are gone. It is recorded and reported
        `failed` (`device_shutdown`), so the hub starts a new main session for the next follow-up (03-api.md section
        4.6.3) instead of routing it to a session nobody runs (DR-01). A session the hub wants stopped is reported
        `stopped` (it never ran here, or no longer does). A session whose start is running right now, or that was
        recorded since the store was read (a child start of the operation executor), is left to it.
        Child sessions take the same path from the subagent controller's reconcile (KOR-09, AUD-02 NA-06); their parent
        gets the finished ping of an ended child from the controller's next pass."""
        if item.session_id in self.tasks:
            return
        async with self.env.transaction() as conn:
            created = await records.insert(
                conn,
                session_id=item.session_id,
                task_id=item.task_id,
                org_id=item.org_id,
                wg_id=item.wg_id,
                parent_id=item.parent_session_id,
                depth=item.depth,
                owner_id=item.owner_id,
                created_at=datetime.fromtimestamp(self.env.wall(), UTC),
            )
            if not created:
                return  # recorded in the meantime (a start that ended after the store was read): not lost
            if item.state == "stopping":
                await records.advance(conn, self.env.outbox, item.session_id, "stopped", at=self.env.at())
            else:
                await fail(conn, self.env.outbox, item.session_id, "device_shutdown", at=self.env.at())
        self.env.line(f"store_lost session_id={item.session_id} state={item.state}")

    async def _interrupted(self, session: records.LocalSession) -> None:
        """A start cut short by a daemon restart: `failed` (`device_shutdown`), and its guest session - whose agent
        may already run with the platform credential - is stopped."""
        async with self.env.transaction() as conn:
            await fail(conn, self.env.outbox, session.id, "device_shutdown", at=self.env.at())
        self.env.line(f"start_interrupted session_id={session.id}")
        if session.vm_id is not None:
            self._spawn(session.id, stop_guest(self.env, session.id))

    async def vm_unreachable(self, row: VmRow) -> None:
        async with self.env.transaction() as conn:
            for session in await records.listed(conn, vm_id=row.id):
                await fail(conn, self.env.outbox, session.id, "guest_unreachable", at=self.env.at())

    async def vms_lost(self) -> int:
        """Fail the sessions whose VM is gone after the startup VM reconciliation (module docstring); their number."""
        env = self.env
        lost: list[str] = []
        async with env.transaction() as conn:
            for session in await records.listed(conn):
                if session.vm_id is None or session.state in RESTING:
                    continue
                row = await vm_records.get(env.db, session.vm_id)
                if (row is None or row.state == "deleted") and await fail(
                    conn, env.outbox, session.id, "guest_unreachable", at=env.at()
                ):
                    lost.append(session.id)
        for session_id in lost:
            env.line(f"vm_lost session_id={session_id}")
        return len(lost)

    async def vms_stopped(self) -> int:
        """Suspend the main sessions left working or idle in a stopped VM (module docstring); their number."""
        env = self.env
        suspended: list[str] = []
        async with env.transaction() as conn:
            for session in await records.listed(conn):
                if session.depth != 0 or session.vm_id is None or session.state not in STRANDED:
                    continue
                if session.id in self.tasks:
                    continue
                row = await vm_records.get(env.db, session.vm_id)
                if row is None or row.state != "stopped":
                    continue
                if await records.advance(conn, env.outbox, session.id, "suspended", at=env.at(), reason=DEVICE_RESTART):
                    suspended.append(session.id)
        for session_id in suspended:
            env.line(f"vm_stopped session_id={session_id} state=suspended reason={DEVICE_RESTART}")
        return len(suspended)

    # -- journal loops -------------------------------------------------------------------------------------------

    async def vms_ready(self) -> None:
        """Until the startup reconciliation of the VM area ran (task `vm-pool`), so no loop reads a stale VM."""
        await self.env.vms.wait_reconciled()

    def watch(self, row: VmRow) -> None:
        """A VM became ready for a session: start its journal loop at once."""
        if row.id not in self.readers:
            self.wake.set()

    def journal(self, row: VmRow) -> VmJournal:
        return VmJournal(
            self.env, row, handlers=self.handlers, observers=self.observers, reached=self.starter.observe_journal
        )

    async def _read(self, row: VmRow, stopping: asyncio.Event) -> None:
        delay = BACKOFF_MIN_S
        while not stopping.is_set():
            try:
                await self.journal(row).run(stopping)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                self.env.log(
                    f"runner.journal reader_failed vm_id={row.id} error={type(exc).__name__}"
                    + (f" code={code}" if isinstance(code, str) else "")
                )
            await self.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    async def sync_readers(self, stopping: asyncio.Event) -> None:
        """Start loops for running VMs without one, cancel loops of VMs that no longer run."""
        running = {row.id: row for row in await vm_records.live(self.env.db, "running")}
        for vm_id in list(self.readers):
            if vm_id not in running or self.readers[vm_id].done():
                task = self.readers.pop(vm_id)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for vm_id, row in running.items():
            if vm_id not in self.readers:
                self.readers[vm_id] = asyncio.ensure_future(self._read(row, stopping))

    async def run_journals(self, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                self.wake.clear()
                await self.sync_readers(stopping)
                await self._wait(stopping)
        finally:
            for task in self.readers.values():
                task.cancel()
            await asyncio.gather(*self.readers.values(), return_exceptions=True)
            self.readers.clear()

    async def _wait(self, stopping: asyncio.Event) -> None:
        waiters = {
            asyncio.ensure_future(self.wake.wait()),
            asyncio.ensure_future(stopping.wait()),
            asyncio.ensure_future(self.sleep(POLL_S)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)


# -- production wiring -------------------------------------------------------------------------------------------


def build_env(context: Any) -> SessionEnv | None:
    """The `SessionEnv` of a daemon context; None while the device is not enrolled."""
    sync = runtime(context)
    if sync is None:
        return None
    config = context.config
    package = context.host.package

    async def refresh() -> None:
        inbox = inbox_for(context)
        if inbox is not None:
            await inbox.pull_all()

    def log(text: str) -> None:
        context.log.write(text + "\n")
        context.log.flush()

    return SessionEnv(
        db=context.pool,
        outbox=sync,
        vms=manager_for(context),
        book=book_for(context),
        device_id=context.identity.device.device_id,
        device_kind=context.identity.device.kind,
        catalog=selection.load_catalog(config.model_catalog),
        strategies=selection.strategies(package),
        storage_key=sync.storage_key,
        platform_key=lambda org_id=None: dfxkey.platform_key(context, org_id),
        upstream_openrouter=config.model_upstream_openrouter,
        context=context,
        notify=sync.pusher.notify,
        first_prompt=first_prompt_provider(package),
        amenders=request_amenders(package),
        refresh_profiles=refresh,
        log=log,
        clock=context.host.clock,
        wall=sync.wall,
    )


def service_for(context: Any) -> SessionService | None:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, SessionService):
        return found
    env = build_env(context)
    if env is None:
        return None
    package = context.host.package
    service = SessionService(
        env,
        handlers=registry.journal_handlers(package),
        observers=state_observers(package),
        sleep=context.host.sleep,
    )
    vms = cast(Any, env.vms)
    vms.on_unreachable = service.vm_unreachable
    setattr(context, ATTRIBUTE, service)
    return service
