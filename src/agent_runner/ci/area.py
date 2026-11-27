"""The CI area of `agent-runner`: what the CI controller asks over the local socket (spec 16 section 17.4).

| Operation | Body | Answer |
|---|---|---|
| `pull` | `repo`, `number` | `owner`: task, workgroup, organization and creating session of the pull request |
| `capacity` | `size` | `fits`: a CI VM of the size would start now (`WorkVms.can_start`, kind `ci`) |
| `vm` | `size`, `owner` | `workvm_id`, `ready_ms`: a fresh running CI VM whose row names the owner |
| `run` | `workvm_id`, `jit` | the GitHub runner started in the VM with the JIT configuration |
| `rebind` | `workvm_id`, `owner` | the VM's row names another owner (the job GitHub gave the runner) |
| `end` | `workvm_id` | the VM is deleted (`ci_cancelled`) |
| `audit` | `task_id`, `target_id`, `result`, `detail` | an audit row `ci.job` through the outbox |
| `vms` | - | `vms`: the CI VMs that are not deleted, with their state |
| `browser` | `owner`, `tool`, `arguments` | `url`, `title`, `ok`, `error` of one platform browser action (setup) |

- **pull**: a pull request counts only when a session of this device created it: a succeeded
  `github.create_pull_request` of `runner.operation` with that repository (case-insensitive) and number. The owner
  is that operation's task, workgroup and organization, and its session bears the runtime.
- **vm**: `WorkVms.obtain` with scope `ci` and kind `ci`: the warm pool, else the device's queue and admission (the
  same transaction as every other VM, `agent_runner.admission`) and a cold clone; an organization without credit gets
  none (`credit_exhausted`). A refusal of the work VMs is answered with its code (`workvm_unavailable`, `timeout`,
  `limit_exceeded`, `credit_exhausted`). The bearer is a stand-in of the owner's session (`bearer`): the work VM
  machinery needs only its IDs.
- **run**: the runner process starts as a job of the VM's job helper; the JIT configuration goes on its standard
  input (`RUN_COMMAND` reads it into `ACTIONS_RUNNER_INPUT_JITCONFIG`), never into an argument list on the host. Then
  `watch` follows the job every `POLL_S` seconds until it ended (`ci_ended`) or `JOB_LIMIT_S` passed on the host clock
  (`ci_timeout`, GitHub's default job limit of 360 minutes; the job is killed) or the VM stopped answering
  (`ci_ended`), and deletes the VM - in a `finally`, shielded from cancellation.
- **end** cancels the watch and deletes the VM (`ci_cancelled`): a runner that never got its job, or a job the
  controller does not take after GitHub gave it to the runner.
- After a restart of the runner the work VM reconciliation deletes every VM left over, CI VMs included (decision D-14
  is open: no drain or takeover yet); the job fails at GitHub and is never repeated silently.

Every step is a log line with IDs only (`runner.ci ...`); refusals and failures never carry the request's content.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from agent_runner.admission import CI
from agent_runner.ci.attribution import Owner
from agent_runner.ci.jobs import NAME, NUMBER_MAX, OWNER
from agent_runner.ci.link import LinkRefused
from agent_runner.operations.extensions import OperationFailed
from agent_runner.sessions.records import LocalSession
from agent_runner.workvm import jobs, records
from agent_runner.workvm.jobs import WorkVmError
from agent_runner.workvm.manager import CI_SCOPE, SIZES, CallFailed, WorkVms
from agent_runner.workvm.records import WorkVm

POLL_S = 10.0
JOB_LIMIT_S = 360 * 60
RUNNER_DIR = "/opt/actions-runner"
RUN_COMMAND = (
    "IFS= read -r ACTIONS_RUNNER_INPUT_JITCONFIG && export ACTIONS_RUNNER_INPUT_JITCONFIG RUNNER_ALLOW_RUNASROOT=1"
    f" && exec {RUNNER_DIR}/run.sh"
)
JIT = re.compile(r"[A-Za-z0-9+/=_-]{16,65536}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
WORKVM_ID = re.compile(r"wvm-[0-9a-f]{8}")
TARGET = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
RESULTS = frozenset({"ok", "denied", "failed"})
DETAIL_KEYS = ("stage", "event", "basis", "reason", "result", "size", "duration_ms", "workvm_id")
REFUSALS = frozenset({"workvm_unavailable", "timeout", "limit_exceeded", "credit_exhausted"})
BROWSER_TOOLS = frozenset({"browser_navigate", "browser_evaluate", "browser_click", "browser_snapshot", "browser_wait"})
"""The actions `agent-ci setup` needs; never typing, uploads or downloads."""


def _owner(value: object) -> Owner:
    if not isinstance(value, dict):
        raise LinkRefused("request_invalid")
    ids = [value.get(key) for key in ("org_id", "wg_id", "task_id", "session_id")]
    if not all(isinstance(ident, str) and UUID.fullmatch(ident) for ident in ids):
        raise LinkRefused("request_invalid")
    return Owner(*(str(ident) for ident in ids))


def _owner_answer(owner: Owner) -> dict[str, str]:
    return {"org_id": owner.org_id, "wg_id": owner.wg_id, "task_id": owner.task_id, "session_id": owner.session_id}


def bearer(owner: Owner, at: datetime) -> LocalSession:
    """The stand-in session of a CI VM's owner: the work VM machinery reads its IDs (task, organization, workgroup,
    session) and nothing else of a CI VM's session."""
    return LocalSession(
        id=owner.session_id,
        task_id=owner.task_id,
        org_id=owner.org_id,
        wg_id=owner.wg_id,
        vm_id=None,
        parent_id=None,
        depth=0,
        state="working",
        claude_session_id=None,
        model_access=None,
        model_id=None,
        owner_id=None,
        created_at=at,
        ready_at=None,
        ended_at=None,
    )


@dataclass
class Area:
    workvms: WorkVms
    enqueue_audit: Callable[[Mapping[str, Any]], Awaitable[None]]
    log: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    poll_s: float = POLL_S
    job_limit_s: float = JOB_LIMIT_S
    watches: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    browsers: Any = None
    """The device's platform browser (`workvm.browser.Browsers`) for `agent-ci setup`; None: not offered."""
    env: Any = None
    """The session environment the browser calls run with."""

    @property
    def db(self) -> Any:
        return self.workvms.deps.db

    async def handle(self, op: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        handlers: dict[str, Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]] = {
            "pull": self.pull,
            "capacity": self.capacity,
            "vm": self.vm,
            "run": self.run,
            "rebind": self.rebind,
            "end": self.end,
            "audit": self.audit,
            "vms": self.vms,
            "browser": self.browser,
        }
        handler = handlers.get(op)
        if handler is None:
            raise LinkRefused("unknown_op")
        return await handler(body)

    # -- lookups ---------------------------------------------------------------------------------------------------

    async def pull(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        repo, number = body.get("repo"), body.get("number")
        owner, slash, name = repo.partition("/") if isinstance(repo, str) else ("", "", "")
        if (
            not slash
            or OWNER.fullmatch(owner) is None
            or NAME.fullmatch(name) is None
            or not isinstance(number, int)
            or isinstance(number, bool)
            or not 0 < number <= NUMBER_MAX
        ):
            raise LinkRefused("request_invalid")
        async with self.db.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute(
                "SELECT org_id::text, wg_id::text, task_id::text, session_id::text FROM runner.operation"
                " WHERE type = 'github.create_pull_request' AND state = 'succeeded' AND org_id IS NOT NULL"
                " AND jsonb_typeof(params->'repo') = 'string' AND lower(params->>'repo') = lower(%s)"
                " AND jsonb_typeof(result->'number') = 'number' AND (result->>'number')::bigint = %s"
                " ORDER BY updated_at DESC, id LIMIT 1",
                (repo, number),
            )
            row = await cursor.fetchone()
        if row is None:
            return {"owner": None}
        return {"owner": _owner_answer(Owner(row["org_id"], row["wg_id"], row["task_id"], row["session_id"]))}

    async def capacity(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        size = body.get("size")
        if size not in SIZES:
            raise LinkRefused("request_invalid")
        return {"fits": await self.workvms.can_start(str(size), kind=CI)}

    async def vms(self, _body: Mapping[str, Any]) -> Mapping[str, Any]:
        found = [vm for vm in await records.live(self.db) if vm.scope == CI_SCOPE]
        return {"vms": [{"workvm_id": vm.id, "state": vm.state} for vm in found]}

    # -- VMs -------------------------------------------------------------------------------------------------------

    async def _ci_vm(self, body: Mapping[str, Any], *states: str) -> WorkVm:
        ident = body.get("workvm_id")
        if not isinstance(ident, str) or WORKVM_ID.fullmatch(ident) is None:
            raise LinkRefused("request_invalid")
        vm = await records.get(self.db, ident)
        if vm is None or vm.scope != CI_SCOPE or vm.state not in states:
            raise LinkRefused("not_found")
        return vm

    async def vm(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        size, owner = body.get("size"), _owner(body.get("owner"))
        if size not in SIZES:
            raise LinkRefused("request_invalid")
        begun = self.clock()
        try:
            vm = await self.workvms.obtain(
                bearer(owner, self.workvms.deps.now()), scope=CI_SCOPE, size=str(size), kind=CI
            )
        except OperationFailed as failed:
            code = failed.code if failed.code in REFUSALS else "workvm_unavailable"
            self.log(f"runner.ci vm_refused code={code} task_id={owner.task_id}")
            raise LinkRefused(code) from None
        ready_ms = int((self.clock() - begun) * 1000)
        self.log(f"runner.ci vm_ready workvm_id={vm.id} task_id={owner.task_id} ready_ms={ready_ms}")
        return {"workvm_id": vm.id, "ready_ms": ready_ms}

    async def run(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        vm = await self._ci_vm(body, "running")
        jit = body.get("jit")
        if not isinstance(jit, str) or JIT.fullmatch(jit) is None or vm.id in self.watches:
            raise LinkRefused("request_invalid")
        job_id = jobs.new_job_id()
        shell = self.workvms.deps.shell_for(vm)
        spec = jobs.start_input(RUN_COMMAND, [], jit + "\n")
        try:
            started = await shell(jobs.start_argv(job_id, "/", None), stdin=spec, timeout=30.0, limit=4096)
        except WorkVmError:
            started = None
        if started is None or started.returncode != 0:
            await self._delete(vm, "ci_cancelled")
            self.log(f"runner.ci start_failed workvm_id={vm.id}")
            raise LinkRefused("start_failed")
        self.watches[vm.id] = asyncio.ensure_future(self.watch(vm, job_id))
        self.log(f"runner.ci runner_started workvm_id={vm.id}")
        return {}

    async def watch(self, vm: WorkVm, job_id: str) -> None:
        """Follow the runner process until it ends, then delete the VM (module docstring)."""
        reason = "ci_ended"
        deadline = self.clock() + self.job_limit_s
        shell = self.workvms.deps.shell_for(vm)
        try:
            while True:
                try:
                    piece = jobs.parse_piece(
                        await shell(
                            jobs.wait_argv(job_id, None, 0, self.poll_s),
                            stdin=None,
                            timeout=self.poll_s + 10.0,
                            limit=jobs.HEADER_LIMIT + 1,
                        ),
                        limit=0,
                    )
                except WorkVmError:
                    break  # the VM stopped answering: it goes
                if piece.state != "running":
                    break
                if self.clock() >= deadline:
                    reason = "ci_timeout"
                    with contextlib.suppress(WorkVmError):
                        await shell(jobs.kill_argv(job_id, "KILL"), stdin=None, timeout=10.0, limit=4096)
                    break
                await asyncio.sleep(0)  # a wait that answered at once still lets `end` cancel the watch
        finally:
            self.watches.pop(vm.id, None)
            await asyncio.shield(asyncio.ensure_future(self._delete(vm, reason)))

    async def _delete(self, vm: WorkVm, reason: str) -> None:
        await self.workvms.delete(vm, reason=reason)
        self.log(f"runner.ci vm_deleted workvm_id={vm.id} reason={reason}")

    async def rebind(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        vm, owner = await self._ci_vm(body, "running"), _owner(body.get("owner"))
        await records.update(
            self.db,
            vm.id,
            expect=("running",),
            task_id=owner.task_id,
            session_id=owner.session_id,
            requested_by=owner.session_id,
            org_id=owner.org_id,
            wg_id=owner.wg_id,
        )
        self.log(f"runner.ci vm_rebound workvm_id={vm.id} task_id={owner.task_id}")
        return {}

    async def end(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        vm = await self._ci_vm(body, "starting", "running", "stopping", "stopped")
        watch = self.watches.pop(vm.id, None)
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
            return {}
        await self._delete(vm, "ci_cancelled")
        return {}

    # -- the platform browser for `agent-ci setup` ----------------------------------------------------------------

    async def browser(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """One action of the platform browser session of the fixed task `owner` (`agent-ci setup`, spec 16 section
        17.9). It is recorded and watched like every platform browser session; the record masks `code` and `state`
        of a URL (`workvm.browser.redact_url`). The answer to the controller carries the page's URL, title and outcome
        only, and nothing of it is logged here."""
        tool, arguments, owner = body.get("tool"), body.get("arguments"), _owner(body.get("owner"))
        if tool not in BROWSER_TOOLS or not isinstance(arguments, dict):
            raise LinkRefused("request_invalid")
        if self.browsers is None:
            raise LinkRefused("browser_unavailable")
        try:
            answer = await self.browsers.call(self.env, bearer(owner, self.workvms.deps.now()), str(tool), arguments)
        except CallFailed as failed:
            raise LinkRefused(failed.error) from None
        self.log(f"runner.ci browser tool={tool} ok={str(answer.get('ok') is True).lower()}")
        return {key: answer.get(key) for key in ("url", "title", "ok", "error")}

    # -- audit -----------------------------------------------------------------------------------------------------

    async def audit(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        task, target, result, detail = (
            body.get("task_id"),
            body.get("target_id"),
            body.get("result"),
            body.get("detail"),
        )
        if (
            not isinstance(task, str)
            or UUID.fullmatch(task) is None
            or not isinstance(target, str)
            or TARGET.fullmatch(target) is None
            or result not in RESULTS
            or not isinstance(detail, dict)
            or tuple(sorted(detail)) != tuple(sorted(DETAIL_KEYS))
        ):
            raise LinkRefused("request_invalid")
        item = {
            "action": "ci.job",
            "result": result,
            "task_id": task,
            "target_kind": "ci_job",
            "target_id": target,
            "detail": {key: detail[key] for key in DETAIL_KEYS},
        }
        await self.enqueue_audit(item)
        return {}

    async def close(self) -> None:
        """Stop following the runner processes (daemon shutdown); the VMs go with the work VM reconciliation."""
        watches = list(self.watches.values())
        for watch in watches:
            watch.cancel()
        await asyncio.gather(*watches, return_exceptions=True)
        self.watches.clear()
