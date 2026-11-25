"""Test runs as platform operations (KOR-43, ADR-080; docs/platform/spec/13-work-vm.md section 12).

`TestRuns.execute` is the executor of `test.run`. One run:

1. **Checks** (the operation fails, nothing ran): credit on a platform device (`credit_exhausted`), size cap, a ready
   work image and boundary (`workvm_unavailable`), the organization and owner VM limits (`limit_exceeded`).
2. **Workspace**: the guest exports the session workspace; its digest (SHA-256 over the export manifest) together with
   `kind`, `command`, `cwd`, `size` and `timeout_seconds` is the **duplicate key**: a request of the same session with
   the same key while a run is queued, starting or running joins that run (same `run_id`, same result). A finished
   run is never joined.
3. **VM**: a fresh VM of scope `run` (`WorkVms.obtain`: warm pool, else the FIFO queue and a cold clone); a failed
   start fails the operation (retryable: nothing ran). `ready_ms` is measured from here.
4. The run row (`runner.test_run`, state `running`) and the `test.run` event `running` are written; the workspace is
   mirrored into `/workspace` and the command started (`bash -lc` as root, `cwd`). Then the runner waits in steps of
   `POLL_SECONDS` until the job ended, the time limit passed on the **host clock** (`timed_out`: the job is killed and
   the VM goes - a VM root cannot extend it), the credit of the organization was used up (`cancelled`,
   `credit_exhausted`) or the run was cancelled (`cancelled`: its session ended or its task was closed). A transfer
   failure or a VM that stops answering ends the run `error` (`transfer_failed`, `vm_lost`).
5. **Always**: the complete output is stored as an encrypted log (`capture`, at most `log_max_bytes`, its last part)
   within `CAPTURE_TIMEOUT`, and the VM is deleted (`run_ended`, or the cancel reason) - in a `finally`, shielded from
   cancellation. Then the row and the final `test.run` event get the result, and the operation succeeds with it (also
   when the tests failed).

After a restart (`reconcile`, before the work VM reconciliation deletes the VMs left over, `tasks.py`) every row
still `running` ends `error` (`restart`) with its final event, and the pipeline fails the operation found `executing`
(`timeout`): a run never runs twice. **The log stays** (ADR-087 section 5): when the run's VM is still `running`, its
job (the newest of the VM's jobs with the run's command) is killed and its output kept like in step 5 (`_keep`: tail,
counts and the log of kind `log` with the run ID as `ref`, within `CAPTURE_TIMEOUT`), and the result and the final
event carry `log`, `tail` and `summary`; only then is the VM deleted (`restart`). When the VM is gone, does not answer
or the capture fails, the run ends as before without log.

`summary` counts tests from the last summary line of the output tail the runner recognises (`summarize`: pytest,
vitest/jest, go test, cargo test, mocha, ESLint/ruff problems); the tail is at most `TAIL_CHARS` characters and every
pattern is linear, so untrusted output cannot slow the runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.sessions.records import LocalSession
from agent_runner.workvm import jobs, records, sync
from agent_runner.workvm.jobs import WorkVmError
from agent_runner.workvm.manager import (
    RUN_COMMAND_LIMIT,
    RUN_SCOPE,
    WORKSPACE,
    CallFailed,
    Identity,
    WorkVms,
    export_workspace,
)
from agent_runner.workvm.records import WorkVm

DEFAULT_TIMEOUT = 3600
POLL_SECONDS = 5.0
TAIL_CHARS = 8192
PIECE_BYTES = 1024 * 1024
CAPTURE_TIMEOUT = 300.0
FINAL_STATES = frozenset({"passed", "failed", "error", "timed_out", "cancelled"})

Capture = Callable[[WorkVm, Identity, str, int, str], Awaitable[dict[str, Any] | None]]
"""Stores the output of a job as a log (`vm, identity, job_id, output_bytes, ref`); returns its `log_ref`."""


def new_run_id() -> str:
    return "tr-" + secrets.token_hex(6)


# -- summary ---------------------------------------------------------------------------------------------------------

_COUNT = r"(\d{1,9})"
_PYTEST = re.compile(r"(\d{1,9}) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)\b")
_VITEST = re.compile(r"^\s*Tests\s{1,8}(.{1,400})$", re.MULTILINE)
_JEST = re.compile(r"^Tests:\s{1,8}(.{1,400})$", re.MULTILINE)
_WORDS = re.compile(r"(\d{1,9}) (passed|failed|skipped|todo|pending)\b")
_CARGO = re.compile(rf"^test result: \w+\. {_COUNT} passed; {_COUNT} failed; {_COUNT} ignored", re.MULTILINE)
_MOCHA_PASS = re.compile(rf"^\s*{_COUNT} passing\b", re.MULTILINE)
_MOCHA_FAIL = re.compile(rf"^\s*{_COUNT} failing\b", re.MULTILINE)
_MOCHA_PENDING = re.compile(rf"^\s*{_COUNT} pending\b", re.MULTILINE)
_GO = re.compile(r"^(?:--- (PASS|FAIL|SKIP):|(ok|FAIL)\s{1,8}\S{1,400})", re.MULTILINE)
_PROBLEMS = re.compile(rf"(?:✖ |^Found ){_COUNT} (?:problems?|errors?)\b", re.MULTILINE)
_PYTEST_LINE = re.compile(r"^=+ (.{1,600}) in [\d.]{1,12}s(?: \([^)]{0,40}\))? =+$", re.MULTILINE)


def _summary(passed: int | None, failed: int | None, skipped: int | None, errors: int | None) -> dict[str, int | None]:
    return {"passed": passed, "failed": failed, "skipped": skipped, "errors": errors}


def _last(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    found = None
    for found in pattern.finditer(text):  # noqa: B007 - the last match counts
        pass
    return found


def summarize(tail: str) -> dict[str, int | None] | None:  # noqa: PLR0911 - one return per recognised format
    """Counts from the last summary line of `tail` (module docstring); None when no format is recognised."""
    line = _last(_PYTEST_LINE, tail)
    if line is not None:
        counts: dict[str, int] = {}
        for number, word in _PYTEST.findall(line.group(1)):
            counts[word.rstrip("s") if word in ("errors", "warnings") else word] = int(number)
        if counts:
            return _summary(
                counts.get("passed", 0), counts.get("failed", 0), counts.get("skipped"), counts.get("error")
            )
    line = _last(_VITEST, tail) or _last(_JEST, tail)
    if line is not None:
        counts = {word: int(n) for n, word in _WORDS.findall(line.group(1))}
        if counts:
            skipped = counts.get("skipped", 0) + counts.get("todo", 0) + counts.get("pending", 0)
            return _summary(counts.get("passed", 0), counts.get("failed", 0), skipped or None, None)
    cargo = _last(_CARGO, tail)
    if cargo is not None:
        return _summary(int(cargo.group(1)), int(cargo.group(2)), int(cargo.group(3)), None)
    passing = _last(_MOCHA_PASS, tail)
    if passing is not None:
        failing, pending = _last(_MOCHA_FAIL, tail), _last(_MOCHA_PENDING, tail)
        return _summary(
            int(passing.group(1)),
            int(failing.group(1)) if failing else 0,
            int(pending.group(1)) if pending else None,
            None,
        )
    go = _GO.findall(tail)
    if go:
        passed = sum(1 for test, _package in go if test == "PASS")
        failed = sum(1 for test, package in go if test == "FAIL" or package == "FAIL")
        skipped = sum(1 for test, _package in go if test == "SKIP")
        return _summary(passed, failed, skipped or None, None)
    problems = _last(_PROBLEMS, tail)
    if problems is not None:
        return _summary(None, int(problems.group(1)), None, None)
    return None


# -- runs ------------------------------------------------------------------------------------------------------------


@dataclass
class Active:
    """A run of this daemon that has not ended (the join target of duplicates)."""

    run_id: str
    key: tuple[str, ...]
    session_id: str
    task_id: str
    org_id: str
    result: asyncio.Future[dict[str, Any]]
    cancel: str | None = None


@dataclass
class Store:
    """The run rows and events (injected; production in `service.py`)."""

    db: Any
    emit: Callable[[Identity, str, Mapping[str, Any]], Awaitable[None]]
    now: Callable[[], datetime]
    log: Callable[[str], None]


@dataclass
class TestRuns:
    workvms: WorkVms
    store: Store
    capture: Capture
    poll_seconds: float = POLL_SECONDS
    clock: Callable[[], float] = time.monotonic
    active: dict[str, Active] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    # -- executor ------------------------------------------------------------------------------------------------

    async def execute(self, ctx: OperationContext) -> dict[str, Any]:
        session, params = ctx.session, ctx.params
        size = str(params.get("size", "standard"))
        await self.workvms.check_new(session, scope=RUN_SCOPE, size=size)
        with tempfile.TemporaryDirectory(dir=self.workvms.deps.scratch) as scratch:
            exported = Path(scratch) / "export.tar"
            # one export and registration per session at a time: two identical requests arriving together join
            async with self.locks.setdefault(session.id, asyncio.Lock()):
                try:
                    await export_workspace(ctx.env, session, exported)
                except CallFailed as failed:
                    raise OperationFailed("upstream_error", retryable=True, message=failed.message) from None
                manifest = sync.read_export(exported).manifest
                digest = hashlib.sha256(
                    json.dumps({path: entry.sha256 for path, entry in manifest.items()}, sort_keys=True).encode()
                ).hexdigest()
                key = (
                    session.id,
                    str(params["kind"]),
                    str(params["command"]),
                    str(params.get("cwd", WORKSPACE)),
                    size,
                    str(params.get("timeout_seconds", DEFAULT_TIMEOUT)),
                    digest,
                )
                joined = next((found for found in self.active.values() if found.key == key and not found.cancel), None)
                if joined is None:
                    active = Active(
                        run_id=new_run_id(),
                        key=key,
                        session_id=session.id,
                        task_id=session.task_id,
                        org_id=session.org_id,
                        result=asyncio.get_running_loop().create_future(),
                    )
                    self.active[active.run_id] = active
            if joined is not None:
                self.store.log(f"runner.testrun joined run_id={joined.run_id} operation_id={ctx.operation.id}")
                return await asyncio.shield(joined.result)
            try:
                result = await self._run(ctx, active, exported, digest)
            except BaseException as error:
                active.result.set_exception(error)
                active.result.exception()  # retrieved: a run nobody joined must not warn
                raise
            finally:
                self.active.pop(active.run_id, None)
            active.result.set_result(result)
            return result

    async def _run(self, ctx: OperationContext, active: Active, exported: Path, digest: str) -> dict[str, Any]:
        session, params = ctx.session, ctx.params
        size = str(params.get("size", "standard"))
        begun = self.clock()
        vm = await self.workvms.obtain(session, scope=RUN_SCOPE, size=size, triggered_by=ctx.operation.actor_id)
        ready_ms = int((self.clock() - begun) * 1000)
        run = Run(
            run_id=active.run_id,
            operation_id=ctx.operation.id,
            identity=Identity(session.org_id, session.wg_id, session.task_id, session.id),
            kind=str(params["kind"]),
            command=str(params["command"]),
            cwd=str(params.get("cwd", WORKSPACE)),
            size=size,
            timeout=int(params.get("timeout_seconds", DEFAULT_TIMEOUT)),
            started_at=self.store.now(),
            ready_ms=ready_ms,
        )
        outcome = Outcome()
        try:  # from here on the VM goes on every path
            self.store.log(f"runner.testrun ready run_id={active.run_id} ready_ms={ready_ms} workvm_id={vm.id}")
            await self._insert(run, vm, digest)
            await self._event(run, "running")
            await self._execute_job(vm, session, run=run, active=active, exported=exported, outcome=outcome)
        finally:
            await asyncio.shield(asyncio.ensure_future(self._finish(vm, run, outcome, active)))
        return run.result

    async def _execute_job(
        self, vm: WorkVm, session: LocalSession, *, run: Run, active: Active, exported: Path, outcome: Outcome
    ) -> None:
        try:
            await self.workvms.push_export(vm, session, ".", exported)
        except (WorkVmError, CallFailed):
            outcome.end("error", reason="transfer_failed")
            return
        spec = jobs.start_input(run.command, [], None)
        job_id = jobs.new_job_id()
        shell = self.workvms.deps.shell_for(vm)
        started = await shell(jobs.start_argv(job_id, run.cwd, run.timeout), stdin=spec, timeout=30.0, limit=4096)
        if started.returncode != 0:
            outcome.end("error", reason="start_failed")
            return
        outcome.job_id = job_id
        deadline = self.clock() + run.timeout
        while True:
            try:
                piece = jobs.parse_piece(
                    await shell(
                        jobs.wait_argv(job_id, None, 0, self.poll_seconds),
                        stdin=None,
                        timeout=self.poll_seconds + 10.0,
                        limit=jobs.HEADER_LIMIT + 1,
                    ),
                    limit=0,
                )
            except WorkVmError:
                stop = self._stop_reason(active)  # a closed task may have taken the VM before this poll saw it
                outcome.end("error" if stop is None else "cancelled", reason=stop or "vm_lost")
                return
            outcome.output_bytes, outcome.duration_ms = piece.output_bytes, piece.duration_ms
            if piece.state != "running":
                if piece.state == "timed_out":
                    outcome.end("timed_out")
                elif piece.state == "exited" and piece.exit_code is not None:
                    outcome.end("passed" if piece.exit_code == 0 else "failed", exit_code=piece.exit_code)
                else:
                    outcome.end("cancelled", reason="cancelled", exit_code=piece.exit_code)
                return
            stop = self._stop_reason(active)
            if stop is not None or self.clock() >= deadline:
                with contextlib.suppress(WorkVmError):
                    await shell(jobs.kill_argv(job_id, "KILL"), stdin=None, timeout=10.0, limit=4096)
                if stop is None:
                    outcome.end("timed_out")
                else:
                    outcome.end("cancelled", reason=stop)
                return

    def _stop_reason(self, active: Active) -> str | None:
        if active.cancel is not None:
            return active.cancel
        return "credit_exhausted" if self.workvms.deps.exhausted(active.org_id) else None

    async def _keep(self, vm: WorkVm, run: Run, outcome: Outcome, job_id: str) -> tuple[str, dict[str, Any] | None]:
        """The tail and the stored log of the run's job (no log without output or when the capture fails)."""
        tail = ""
        log: dict[str, Any] | None = None
        shell = self.workvms.deps.shell_for(vm)
        with contextlib.suppress(WorkVmError):
            piece = jobs.parse_piece(
                await shell(
                    jobs.wait_argv(job_id, None, TAIL_CHARS * 4, 0),
                    stdin=None,
                    timeout=30.0,
                    limit=jobs.HEADER_LIMIT + TAIL_CHARS * 4 + 1,
                ),
                limit=TAIL_CHARS * 4,
            )
            tail = jobs.text(piece.body)[-TAIL_CHARS:]
            outcome.output_bytes = max(outcome.output_bytes, piece.output_bytes)
            outcome.duration_ms = max(outcome.duration_ms, piece.duration_ms)
        if outcome.output_bytes > 0:
            try:
                log = await asyncio.wait_for(
                    self.capture(vm, run.identity, job_id, outcome.output_bytes, run.run_id), CAPTURE_TIMEOUT
                )
            except Exception as error:  # the run keeps its tail; the log line keeps the fact
                self.store.log(f"runner.testrun capture_failed run_id={run.run_id} error={type(error).__name__}")
        return tail, log

    async def _finish(self, vm: WorkVm, run: Run, outcome: Outcome, active: Active) -> None:
        """Keep the log, delete the VM, record the result (runs on every path, module docstring step 5)."""
        if outcome.state is None:
            outcome.end("error", reason="vm_lost")  # the executor was cancelled while the job ran
        tail = ""
        log: dict[str, Any] | None = None
        reason = outcome.reason if outcome.reason in ("credit_exhausted", "task_closed", "session_ended") else None
        try:
            if outcome.job_id is not None:
                tail, log = await self._keep(vm, run, outcome, outcome.job_id)
        finally:
            await self.workvms.delete(vm, reason=reason or "run_ended")
        state = str(outcome.state)
        run.result = {
            "run_id": run.run_id,
            "state": state,
            "exit_code": outcome.exit_code,
            "duration_ms": outcome.duration_ms,
            "ready_ms": run.ready_ms,
            "summary": summarize(tail),
            "log": log,
            "tail": tail,
            "reason": _result_reason(outcome.reason),
        }
        await self._end(run, state)
        self.store.log(
            f"runner.testrun ended run_id={run.run_id} state={state} exit_code={outcome.exit_code}"
            f" output_bytes={outcome.output_bytes} logged={int(log is not None)}"
        )

    # -- cancellation ----------------------------------------------------------------------------------------------

    def cancel(self, *, reason: str, session_id: str | None = None, task_id: str | None = None) -> list[str]:
        """Ask the matching runs to end (`cancelled` with `reason`); they end within one poll."""
        found: list[str] = []
        for active in self.active.values():
            if (session_id is not None and active.session_id == session_id) or (
                task_id is not None and active.task_id == task_id
            ):
                active.cancel = active.cancel or reason
                found.append(active.run_id)
        return found

    async def reconcile(self) -> int:
        """After a restart: every run still `running` ends `error` (`restart`), with the log of its VM when that still
        runs (module docstring); returns their number."""
        async with self.store.db.connection() as conn:
            cursor = await conn.execute(
                "SELECT run_id, operation_id::text, session_id::text, task_id::text, org_id::text, wg_id::text, kind,"
                " command, cwd, size, timeout_seconds, started_at, workvm_id FROM runner.test_run"
                " WHERE state = 'running' ORDER BY started_at, run_id"
            )
            rows = await cursor.fetchall()
        for run_id, operation_id, session_id, task_id, org_id, wg_id, kind, command, cwd, size, timeout, at, vm in rows:
            run = Run(
                run_id=run_id,
                operation_id=operation_id,
                identity=Identity(org_id, wg_id, task_id, session_id),
                kind=kind,
                command=command,
                cwd=cwd,
                size=size,
                timeout=timeout,
                started_at=at,
                ready_ms=0,
            )
            outcome = Outcome()
            tail, log = await self._salvage(vm, run, outcome)
            run.result = {
                "run_id": run_id,
                "state": "error",
                "exit_code": None,
                "duration_ms": max(outcome.duration_ms, int((self.store.now() - at).total_seconds() * 1000)),
                "ready_ms": 0,
                "summary": summarize(tail),
                "log": log,
                "tail": tail,
                "reason": "restart",
            }
            await self._end(run, "error")
            self.store.log(
                f"runner.testrun ended run_id={run_id} state=error reason=restart logged={int(log is not None)}"
            )
        return len(rows)

    async def _salvage(self, workvm_id: str | None, run: Run, outcome: Outcome) -> tuple[str, dict[str, Any] | None]:
        """The output of a run whose VM outlived the restart, then that VM goes (module docstring)."""
        vm = None if workvm_id is None else await records.get(self.store.db, workvm_id)
        if vm is None or vm.state != "running":
            return "", None
        try:
            job_id = await self._job_of(vm, run)
            if job_id is None:
                return "", None
            with contextlib.suppress(WorkVmError):
                await self.workvms.deps.shell_for(vm)(
                    jobs.kill_argv(job_id, "KILL"), stdin=None, timeout=10.0, limit=4096
                )
            return await self._keep(vm, run, outcome, job_id)
        finally:
            await self.workvms.delete(vm, reason="restart")

    async def _job_of(self, vm: WorkVm, run: Run) -> str | None:
        """The run's job in its VM: the newest job with the run's command; None when there is none or the VM does not
        answer."""
        shell = self.workvms.deps.shell_for(vm)
        try:
            listed = jobs.parse_list(await shell(jobs.list_argv(), stdin=None, timeout=10.0, limit=jobs.LIST_LIMIT))
        except WorkVmError:
            return None
        label = jobs.command_label(run.command)
        found = [job for job in listed if job["command"] == label]
        return str(max(found, key=lambda job: job["started_at"])["job_id"]) if found else None

    # -- rows and events -------------------------------------------------------------------------------------------

    async def _insert(self, run: Run, vm: WorkVm, digest: str) -> None:
        identity = run.identity
        async with self.store.db.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.test_run (run_id, operation_id, session_id, task_id, org_id, wg_id, workvm_id,"
                " kind, command, cwd, size, timeout_seconds, digest, state, started_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', %s)",
                (
                    run.run_id,
                    run.operation_id,
                    identity.session_id,
                    identity.task_id,
                    identity.org_id,
                    identity.wg_id,
                    vm.id,
                    run.kind,
                    run.command,
                    run.cwd,
                    run.size,
                    run.timeout,
                    digest,
                    run.started_at,
                ),
            )

    async def _end(self, run: Run, state: str) -> None:
        async with self.store.db.connection() as conn:
            await conn.execute(
                "UPDATE runner.test_run SET state = %s, ended_at = %s, result = %s::jsonb WHERE run_id = %s",
                (state, self.store.now(), json.dumps(run.result), run.run_id),
            )
        await self._event(run, state)

    async def _event(self, run: Run, state: str) -> None:
        final = state in FINAL_STATES
        result = run.result if final else {}
        body = {
            "run_id": run.run_id,
            "operation_id": run.operation_id,
            "kind": run.kind,
            "command": run.command[:RUN_COMMAND_LIMIT],
            "cwd": run.cwd[:RUN_COMMAND_LIMIT],
            "size": run.size,
            "state": state,
            "exit_code": result.get("exit_code"),
            "started_at": _iso(run.started_at),
            "ended_at": _iso(self.store.now()) if final else None,
            "duration_ms": result.get("duration_ms", 0),
            "ready_ms": run.ready_ms,
            "summary": result.get("summary"),
            "log": result.get("log"),
            "tail": result.get("tail", ""),
            "reason": result.get("reason"),
        }
        try:
            await self.store.emit(run.identity, "test.run", body)
        except Exception as error:  # the event cannot be written: the operation result still carries the run
            self.store.log(f"runner.testrun event_failed run_id={run.run_id} error={type(error).__name__}")


@dataclass
class Run:
    run_id: str
    operation_id: str
    identity: Identity
    kind: str
    command: str
    cwd: str
    size: str
    timeout: int
    started_at: datetime
    ready_ms: int
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    state: str | None = None
    reason: str | None = None
    exit_code: int | None = None
    job_id: str | None = None
    output_bytes: int = 0
    duration_ms: int = 0

    def end(self, state: str, *, reason: str | None = None, exit_code: int | None = None) -> None:
        self.state, self.reason, self.exit_code = state, reason, exit_code


def _result_reason(reason: str | None) -> str | None:
    """The contract's reason (`test_run_reason`): a session end or task close is `cancelled`."""
    if reason in ("session_ended", "task_closed"):
        return "cancelled"
    return reason


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


async def read_output(shell: jobs.Shell, job_id: str, *, total: int, keep: int) -> AsyncIterator[bytes]:
    """The last `keep` bytes of a job's `total` output, in pieces of at most `PIECE_BYTES` (raw bytes)."""
    offset = max(0, total - keep)
    while offset < total:
        limit = min(PIECE_BYTES, total - offset)
        piece = jobs.parse_piece(
            await shell(
                jobs.wait_argv(job_id, offset, limit, 0),
                stdin=None,
                timeout=60.0,
                limit=jobs.HEADER_LIMIT + limit + 1,
            ),
            limit=limit,
        )
        if not piece.body:
            return
        offset += len(piece.body)
        yield piece.body
