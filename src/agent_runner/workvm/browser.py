"""The platform browser (KOR-44, ADR-085; docs/platform/spec/13-work-vm.md section 13).

`Browsers` owns the browser sessions of the device (`runner.browser_session`, `browser_records`), each driving one VM
of scope `browser` (`WorkVms.obtain`: warm pool, FIFO queue, slot, RAM, disk, boundary, billing as a run VM):

- **open** (`browser.open` executor) and **implicit open** (a browser tool of a session without an open browser
  session): the formal checks (`WorkVms.check_new`), `browser.state` `starting`, a standard VM, the helper's
  `selftest`, the row and `browser.state` `open` (`ready_ms`: request to a browser that answers). One open browser
  session per session: `browser.open` returns the open one (`created: false`) and opens its `url`. The operation
  fails only when no browser came up; the tool answers `unavailable` with the reason.
- **calls** of the fifteen tools (`call`, from `journal.py`): `browser_navigate {query}` becomes the URL of
  `BROWSER_SEARCH_URL` (or `ok: false` while it is off), `browser_upload` copies the workspace files into the VM
  (`UPLOADS/<random>/<n>/<name>`, passed as `files`), the helper runs the action (`workvm-browser call`, untrusted
  answer, `browser_helper`), a download or screenshot is brought into the workspace (`WorkVms.fetch_file`), the
  `browser.action` event records it (password text masked, the picture when it fits) and the answer carries
  `browser_id` and `seq`. A failed action is `ok: false`, not a tool error.
- **failure and restart**: a call that runs into its time limit is `ok: false` `timeout` and keeps the VM; only a VM
  that then fails the `selftest` probe (10 s), or whose helper answered outside the closed schema, is deleted
  (`vm_lost`). The next call of an open browser session without a running VM (lost, or deleted by a runner restart or
  a boundary failure) obtains a fresh VM, writes `browser.state` `restarted` (`vm_lost` or `restart`) and answers with
  `RESTART_NOTICE`; only `browser_navigate {url}` runs on it, any other action answers `ok: false` and the blank page.
- **teardown**: `browser.close` (`browser_closed`), the idle pass (`browser_idle`: no call for the session's
  `idle_seconds`), the session's end (`session_ended`), the task's close (`task_closed`) and the credit stop
  (`credit_exhausted`) close the browser session with that reason; the VM is deleted with it, shielded from
  cancellation (a failed Lima deletion stays `deleting` for the sweep). A restart deletes the VMs but keeps the rows.

Calls of one session are serialised (one browser, one page); the teardowns do not wait for a running call.
Log lines `runner.browser opened|restarted|closed|lost ...`.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from agent_runner.encoding import b64u_encode
from agent_runner.errors import RunnerError
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.sessions.records import LocalSession
from agent_runner.vm.lima import EXIT_TIMEOUT
from agent_runner.workvm import browser_records as rows
from agent_runner.workvm import records as vm_records
from agent_runner.workvm import sync
from agent_runner.workvm.browser_helper import (
    ANSWER_LIMIT,
    NOTICE_LIMIT,
    UPLOADS,
    Answer,
    File,
    call_argv,
    clean,
    parse_answer,
    selftest_argv,
    selftest_ok,
)
from agent_runner.workvm.browser_records import BrowserRow, new_browser_id
from agent_runner.workvm.jobs import WorkVmError
from agent_runner.workvm.manager import (
    BROWSER_SCOPE,
    SYNC_TIMEOUT,
    CallFailed,
    Identity,
    WorkVms,
    export_directory,
    new_workvm_id,
)
from agent_runner.workvm.records import WorkVm

IMPLICIT_NOTICE = "A browser was opened for this call (browser.open)."
RESTART_NOTICE = "The browser VM failed and was started again; open pages, cookies and sign-ins are gone."
RESTARTED_ERROR = "The browser was started again before this action, so it did not run: open a URL first."
SEARCH_OFF = "Automated search is not switched on: open a search page with url, like a person."
LOST_ERROR = "The browser VM stopped answering and was removed; the next action starts a fresh one."
FAILED_ERROR = "The browser did not answer as expected."
BROKEN_ERROR = "The action did not complete on the runner."
TIMEOUT_ERROR = "timeout"
CLOSED_TEXT = "The browser session was closed. Try again."
BLANK = "about:blank"
MASK = "••••"
CALL_TIMEOUT = 27.0
PROBE_TIMEOUT = 10.0
START_PROBE_TIMEOUT = 45.0
ACTION_LIMIT = 64
EVENT_TEXT_LIMIT = 1024
IMAGE_BYTES = 100 * 1024
IMAGE_WIDTH = 960
UPLOAD_BYTES = 100 * 1024 * 1024
UPLOAD_SCRIPT = 'umask 022 && mkdir -p "$1" && exec tar -x --no-same-owner -f - -C "$1"'
FILE_TOOLS = frozenset({"browser_download", "browser_screenshot"})


@dataclass(frozen=True)
class BrowserSettings:
    idle_seconds: int = 600
    """`BROWSER_IDLE_SECONDS`: idle time of a browser session opened without its own."""
    search_url: str = ""
    """`BROWSER_SEARCH_URL`: the template of `browser_navigate {query}` (`{q}`); empty = off."""


class _Failed(Exception):
    """An action that did not run (`ok: false` with the message)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _blank(url: str, error: str) -> Answer:
    return Answer(
        ok=False,
        error=error,
        notice=None,
        url=url,
        title="",
        tab=0,
        tabs=[],
        snapshot="",
        masked=False,
        result=None,
        image=None,
        file=None,
    )


def _short(value: object) -> str | None:
    return None if value is None else clean(str(value), EVENT_TEXT_LIMIT)


def describe(tool: str, arguments: Mapping[str, Any]) -> tuple[str, str | None, str | None]:  # noqa: PLR0911
    """`(action, target, text)` of the `browser.action` event of a call (unmasked, uncut)."""
    action = tool.removeprefix("browser_")
    target = arguments.get("target")
    if tool == "browser_navigate":
        if "history" in arguments:
            return str(arguments["history"]), None, None
        if "query" in arguments:
            return "search", None, arguments["query"]
        return action, arguments.get("url"), None
    if tool == "browser_click":
        name = "double_click" if arguments.get("double") is True else action
        return name, target if "x" not in arguments else f"{arguments['x']},{arguments['y']}", None
    if tool in ("browser_dialog", "browser_tabs"):
        detail = arguments.get("text") if tool == "browser_dialog" else arguments.get("url", arguments.get("index"))
        return f"{action}.{arguments['action']}", None, detail
    if tool == "browser_download":
        return action, target or arguments.get("url"), None
    texts = {
        "browser_type": arguments.get("text"),
        "browser_select": ", ".join(arguments.get("values", [])),
        "browser_press": arguments.get("key"),
        "browser_scroll": arguments.get("dy"),
        "browser_evaluate": arguments.get("expression"),
        "browser_upload": ", ".join(arguments.get("paths", [])),
        "browser_wait": next(
            (arguments[key] for key in ("text", "gone", "url") if key in arguments),
            f"{arguments['ms']} ms" if "ms" in arguments else None,
        ),
    }
    return action, target, texts.get(tool)


def picture(answer: Answer) -> tuple[dict[str, Any] | None, str | None]:
    """`(image, image_omitted)` of the event: the viewport JPEG when it fits (at most `IMAGE_BYTES`, `IMAGE_WIDTH`)."""
    if answer.image is None:
        return None, "unavailable"
    if len(answer.image.jpeg) > IMAGE_BYTES or answer.image.width > IMAGE_WIDTH:
        return None, "too_large"
    image = answer.image
    return {"type": "image/jpeg", "width": image.width, "height": image.height, "data": b64u_encode(image.jpeg)}, None


@dataclass
class Browsers:
    workvms: WorkVms
    settings: BrowserSettings = field(default_factory=BrowserSettings)
    clock: Callable[[], float] = time.monotonic
    live: Any = None
    """The live picture (`browser_live.LiveStreams`; stopped when a browser session closes or loses its VM)."""
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    @property
    def db(self) -> Any:
        return self.workvms.deps.db

    def now(self) -> datetime:
        return self.workvms.deps.now()

    def log(self, text: str) -> None:
        self.workvms.deps.log(f"runner.browser {text}")

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    def _prune(self, session_id: str) -> None:
        """Forget the lock of a session whose browser session closed, unless someone holds or waits for it."""
        lock = self.locks.get(session_id)
        if lock is not None and not lock.locked() and not getattr(lock, "_waiters", None):
            del self.locks[session_id]

    def _stop_live(self, session_id: str) -> None:
        if self.live is not None:
            self.live.stop(session_id)

    # -- executors ---------------------------------------------------------------------------------------------

    async def open(self, ctx: OperationContext) -> dict[str, Any]:
        """The `browser.open` executor (module docstring)."""
        session, params = ctx.session, ctx.params
        begun = self.clock()
        idle = int(params.get("idle_seconds", self.settings.idle_seconds))
        async with self._lock(session.id):
            row = await rows.open_of(self.db, session.id)
            created = row is None
            notices: list[str] = []
            if row is None:
                row, vm = await self._create(session, operation_id=ctx.operation.id, idle_seconds=idle)
            else:
                if "idle_seconds" in params:
                    await rows.update(self.db, row.browser_id, idle_seconds=idle)
                    row = replace(row, idle_seconds=idle)
                vm, restarted = await self._ensure(session, row)
                notices = [RESTART_NOTICE] if restarted else []
            ready_ms = int((self.clock() - begun) * 1000)
            url, title = row.url, ""
            if "url" in params:
                answer = await self._act(
                    ctx.env,
                    session,
                    row,
                    vm,
                    tool="browser_navigate",
                    arguments={"url": params["url"], "snapshot": False},
                    begun=self.clock(),
                    notices=notices,
                    restarted=False,
                )
                url, title = str(answer["url"]), str(answer["title"])
        return {
            "browser_id": row.browser_id,
            "created": created,
            "state": "open",
            "workvm_id": vm.id,
            "ready_ms": ready_ms,
            "idle_seconds": row.idle_seconds,
            "url": url,
            "title": title,
        }

    async def close(self, ctx: OperationContext) -> dict[str, Any]:
        """The `browser.close` executor: closes the open browser session, else names how the last one ended."""
        session = ctx.session
        async with self._lock(session.id):
            row = await rows.open_of(self.db, session.id)
            closed = None if row is None else await self._close(row, "browser_closed")
            last = closed or await rows.last_of(self.db, session.id)
        self._prune(session.id)
        if closed is not None:
            return {"closed": True, "browser_id": closed.browser_id, "actions": closed.seq, "reason": "browser_closed"}
        if last is None:
            return {"closed": False, "browser_id": None, "actions": 0, "reason": None}
        return {"closed": False, "browser_id": last.browser_id, "actions": last.seq, "reason": last.close_reason}

    # -- tool calls --------------------------------------------------------------------------------------------

    async def call(self, env: Any, session: LocalSession, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The answer of one browser tool call (raises `CallFailed` for a tool error)."""
        begun = self.clock()
        async with self._lock(session.id):
            row = await rows.open_of(self.db, session.id)
            notices: list[str] = []
            restarted = False
            if row is None:
                try:
                    row, vm = await self._create(session, operation_id=None, idle_seconds=self.settings.idle_seconds)
                except OperationFailed as failed:
                    message = failed.message or failed.code
                    raise CallFailed("unavailable", f"No browser could be opened: {message}") from None
                notices.append(IMPLICIT_NOTICE)
            else:
                try:
                    vm, restarted = await self._ensure(session, row)
                except OperationFailed as failed:
                    message = failed.message or failed.code
                    raise CallFailed("unavailable", f"The browser VM could not be started again: {message}") from None
                if restarted:
                    notices.append(RESTART_NOTICE)
            return await self._act(
                env, session, row, vm, tool=tool, arguments=arguments, begun=begun, notices=notices, restarted=restarted
            )

    async def _act(
        self,
        env: Any,
        session: LocalSession,
        row: BrowserRow,
        vm: WorkVm,
        *,
        tool: str,
        arguments: Mapping[str, Any],
        begun: float,
        notices: Sequence[str],
        restarted: bool,
    ) -> dict[str, Any]:
        seq = await rows.next_seq(self.db, row.browser_id, self.now())
        if seq is None:
            raise CallFailed("unavailable", CLOSED_TEXT)
        try:
            answer, file = await self._perform(
                env, session, row, vm, seq, tool=tool, arguments=arguments, restarted=restarted
            )
        except BaseException:  # the used seq still gets its entry: the record has no gaps
            broken = _blank(row.url, BROKEN_ERROR)
            await asyncio.shield(
                asyncio.ensure_future(
                    self._finish(row, seq, tool=tool, arguments=arguments, answer=broken, notices=notices, begun=begun)
                )
            )
            raise
        notice = await self._finish(
            row, seq, tool=tool, arguments=arguments, answer=answer, notices=notices, begun=begun
        )
        return {
            "browser_id": row.browser_id,
            "seq": seq,
            "ok": answer.ok,
            "error": answer.error,
            "notice": notice,
            "url": answer.url,
            "title": answer.title,
            "tab": answer.tab,
            "tabs": answer.tabs,
            "snapshot": "" if arguments.get("snapshot") is False else answer.snapshot,
            "result": answer.result,
            "file": file,
        }

    async def _perform(
        self,
        env: Any,
        session: LocalSession,
        row: BrowserRow,
        vm: WorkVm,
        seq: int,
        *,
        tool: str,
        arguments: Mapping[str, Any],
        restarted: bool,
    ) -> tuple[Answer, dict[str, Any] | None]:
        """The helper's answer of one action and the file it brought into the workspace."""
        request = dict(arguments)
        file: dict[str, Any] | None = None
        try:
            if tool == "browser_navigate" and "query" in request:
                request["url"] = self._search(str(request.pop("query")))
            if restarted and not (tool == "browser_navigate" and "url" in request):
                raise _Failed(RESTARTED_ERROR)
            if tool == "browser_upload":
                request["files"] = await self._upload(env, vm, session, list(request.pop("paths")))
            request.pop("path", None)
            answer = await self._run(row, vm, tool, request)
            if answer.ok and answer.file is not None and tool in FILE_TOOLS:
                target = str(arguments.get("path") or self._default_path(tool, row, seq, answer.file))
                file, error = await self._bring(env, vm, session, answer.file, target)
                if error is not None:
                    answer = replace(answer, ok=False, error=error)
        except _Failed as failed:
            answer = _blank(BLANK if restarted else row.url, failed.message)
        return answer, file

    async def _finish(
        self,
        row: BrowserRow,
        seq: int,
        *,
        tool: str,
        arguments: Mapping[str, Any],
        answer: Answer,
        notices: Sequence[str],
        begun: float,
    ) -> str | None:
        """Update the row and write the `browser.action` entry of `seq`; the answer's notice."""
        notice = clean(" ".join([*notices, *([answer.notice] if answer.notice else [])]), NOTICE_LIMIT) or None
        await rows.update(self.db, row.browser_id, url=answer.url, last_call_at=self.now())
        duration_ms = max(0, int((self.clock() - begun) * 1000))
        await self._action_event(
            row, seq, tool=tool, arguments=arguments, answer=answer, notice=notice, duration_ms=duration_ms
        )
        return notice

    def _search(self, query: str) -> str:
        if not self.settings.search_url:
            raise _Failed(SEARCH_OFF)
        return self.settings.search_url.replace("{q}", quote_plus(query))

    async def _run(self, row: BrowserRow, vm: WorkVm, tool: str, request: Mapping[str, Any]) -> Answer:
        """One helper call; a time-out keeps the VM unless the probe fails, an answer outside the schema loses it."""
        shell = self.workvms.deps.shell_for(vm)
        stdin = json.dumps({"tool": tool, "arguments": request}).encode("utf-8")
        result = await shell(call_argv(), stdin=stdin, timeout=CALL_TIMEOUT, limit=ANSWER_LIMIT + 1)
        if result.returncode == 0 or result.truncated:
            try:
                return parse_answer(result)
            except WorkVmError:
                await self._lose(row, vm)
                return _blank(row.url, LOST_ERROR)
        if not await self._probe(vm, PROBE_TIMEOUT):
            await self._lose(row, vm)
            return _blank(row.url, LOST_ERROR)
        return _blank(row.url, TIMEOUT_ERROR if result.returncode == EXIT_TIMEOUT else FAILED_ERROR)

    def _default_path(self, tool: str, row: BrowserRow, seq: int, found: File) -> str:
        if tool == "browser_download":
            return f".browser/downloads/{found.name}"
        return f".browser/screenshots/{row.browser_id}-{seq}.png"

    async def _bring(
        self, env: Any, vm: WorkVm, session: LocalSession, found: File, target: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Bring the helper's file into the workspace at `target`: `(file, None)` or `(None, error)`."""
        try:
            await self.workvms.fetch_file(env, vm, session, found.path, target)
        except CallFailed as failed:
            return None, f"The file could not be saved in the workspace: {failed.message}"
        except RunnerError:
            return None, "The file could not be brought into the workspace."
        return {"path": target, "name": found.name, "bytes": found.bytes}, None

    async def _upload(self, env: Any, vm: WorkVm, session: LocalSession, paths: Sequence[str]) -> list[str]:
        """Copy workspace files into the VM (`UPLOADS/<random>/<n>/<name>`); their VM paths in the given order."""
        destination = f"{UPLOADS}/{secrets.token_hex(8)}"
        groups: dict[str, list[tuple[int, str]]] = {}
        for index, path in enumerate(paths):
            directory, _, name = path.rpartition("/")
            groups.setdefault(directory or ".", []).append((index, name))
        files = [f"{destination}/{index}/{path.rpartition('/')[2]}" for index, path in enumerate(paths)]
        with tempfile.TemporaryDirectory(dir=self.workvms.deps.scratch) as scratch:
            packed = Path(scratch) / "upload.tar"
            total = 0
            with tarfile.open(packed, mode="w", format=tarfile.PAX_FORMAT) as out:
                for number, (directory, wanted) in enumerate(groups.items()):
                    exported = Path(scratch) / f"export-{number}.tar"
                    try:
                        await export_directory(env, session, directory, exported)
                        manifest = sync.read_export(exported).manifest
                    except CallFailed as failed:
                        raise _Failed(f"The files could not be read from the workspace: {failed.message}") from None
                    except RunnerError:
                        raise _Failed("The files could not be read from the workspace.") from None
                    for index, name in wanted:
                        if name not in manifest:
                            raise _Failed(f"There is no file {paths[index]} in the workspace.")
                        total += manifest[name].size
                    if total > UPLOAD_BYTES:
                        raise _Failed("The files are larger than the browser accepts (100 MiB).")
                    _copy(exported, out, wanted)
            shell = self.workvms.deps.shell_for(vm)
            argv = ["sudo", "-n", "/bin/sh", "-c", UPLOAD_SCRIPT, "sh", destination]
            copied = await shell(argv, stdin=packed, timeout=SYNC_TIMEOUT, limit=4096)
        if copied.returncode != 0:
            raise _Failed("The files could not be copied into the browser VM.")
        return files

    async def _action_event(
        self,
        row: BrowserRow,
        seq: int,
        *,
        tool: str,
        arguments: Mapping[str, Any],
        answer: Answer,
        notice: str | None,
        duration_ms: int,
    ) -> None:
        action, target, text = describe(tool, arguments)
        image, omitted = picture(answer)
        body = {
            "browser_id": row.browser_id,
            "seq": seq,
            "tool": tool,
            "action": clean(action, ACTION_LIMIT),
            "target": _short(target),
            "text": MASK if answer.masked and text is not None else _short(text),
            "url": answer.url,
            "title": answer.title,
            "tab": answer.tab,
            "ok": answer.ok,
            "error": answer.error,
            "notice": notice,
            "at": _iso(self.now()),
            "duration_ms": duration_ms,
            "image": image,
            "image_omitted": omitted,
        }
        await self._emit(row, "browser.action", body)

    # -- browser sessions and their VMs ------------------------------------------------------------------------

    async def _create(
        self, session: LocalSession, *, operation_id: str | None, idle_seconds: int
    ) -> tuple[BrowserRow, WorkVm]:
        """A new browser session with a running browser (`browser.state` `starting`, then `open`)."""
        begun = self.clock()
        await self.workvms.check_new(session, scope=BROWSER_SCOPE, size="standard")
        opened = self.now()
        draft = BrowserRow(
            browser_id=new_browser_id(),
            operation_id=operation_id,
            session_id=session.id,
            task_id=session.task_id,
            org_id=session.org_id,
            wg_id=session.wg_id,
            workvm_id=new_workvm_id(),
            state="open",
            seq=0,
            frame_seq=0,
            idle_seconds=idle_seconds,
            url=BLANK,
            ready_ms=0,
            implicit=operation_id is None,
            opened_at=opened,
            last_call_at=opened,
            closed_at=None,
            close_reason=None,
        )
        await self._state(draft, "starting")
        try:
            vm = await self._start_vm(session, implicit=draft.implicit, workvm_id=draft.workvm_id)
            try:
                row = await rows.insert(self.db, replace(draft, ready_ms=int((self.clock() - begun) * 1000)))
            except BaseException:
                await self.workvms.delete(vm, reason="failed")
                raise
        except BaseException:  # the countdown ends on every failure
            await asyncio.shield(asyncio.ensure_future(self._state(draft, "closed", reason="failed")))
            raise
        await self._state(row, "open")
        self.log(f"opened browser_id={row.browser_id} ready_ms={row.ready_ms} workvm_id={vm.id}")
        return row, vm

    async def _start_vm(self, session: LocalSession, *, implicit: bool, workvm_id: str | None) -> WorkVm:
        vm = await self.workvms.obtain(
            session, scope=BROWSER_SCOPE, size="standard", implicit=implicit, workvm_id=workvm_id
        )
        try:
            ready = await self._probe(vm, START_PROBE_TIMEOUT)
            if not ready:  # a first Chromium start under load can outlast the helper's own wait: once more
                self.log(f"start_probe_failed workvm_id={vm.id} attempt=1")
                ready = await self._probe(vm, START_PROBE_TIMEOUT)
        except BaseException:
            await self.workvms.delete(vm, reason="failed")
            raise
        if not ready:
            await self.workvms.delete(vm, reason="failed")
            raise OperationFailed("upstream_error", retryable=True, message="The browser did not start in its VM.")
        return vm

    async def _probe(self, vm: WorkVm, timeout: float) -> bool:
        shell = self.workvms.deps.shell_for(vm)
        begun = self.clock()
        result = await shell(selftest_argv(), stdin=None, timeout=timeout, limit=4096)
        ok = selftest_ok(result)
        if not ok:  # the helper's own sentence (untrusted: printable, at most 160 characters) goes to the log only
            said = "".join(char if char.isprintable() else " " for char in result.stdout.decode("utf-8", "replace"))
            self.log(
                f"probe_failed workvm_id={vm.id} rc={result.returncode} ms={int((self.clock() - begun) * 1000)}"
                f" said={json.dumps(said.strip()[:160])}"
            )
        return ok

    async def _running(self, row: BrowserRow) -> WorkVm | None:
        vm = None if row.workvm_id is None else await vm_records.get(self.db, row.workvm_id)
        return vm if vm is not None and vm.state == "running" else None

    async def _ensure(self, session: LocalSession, row: BrowserRow) -> tuple[WorkVm, bool]:
        """The running VM of an open browser session, or a fresh one (`True`: restarted)."""
        vm = await self._running(row)
        if vm is not None:
            return vm, False
        old = None if row.workvm_id is None else await vm_records.get(self.db, row.workvm_id)
        reason = "vm_lost"
        if old is not None:
            if await vm_records.delete_reason(self.db, old.id) == "restart":
                reason = "restart"
            elif old.state not in ("deleted", "deleting"):
                await self.workvms.delete(old, reason="vm_lost")
        vm = await self._start_vm(session, implicit=row.implicit, workvm_id=None)
        if not await rows.update(self.db, row.browser_id, workvm_id=vm.id):  # closed meanwhile: the VM goes
            await self.workvms.delete(vm, reason="browser_closed")
            raise OperationFailed("upstream_error", retryable=True, message=CLOSED_TEXT)
        await self._state(replace(row, workvm_id=vm.id), "restarted", reason=reason)
        self.log(f"restarted browser_id={row.browser_id} reason={reason} workvm_id={vm.id}")
        return vm, True

    async def _lose(self, row: BrowserRow, vm: WorkVm) -> None:
        """The VM stopped answering: it goes (`vm_lost`); the browser session stays open for the next call."""
        self.log(f"lost browser_id={row.browser_id} workvm_id={vm.id}")
        self._stop_live(row.session_id)
        await self.workvms.delete(vm, reason="vm_lost")

    async def lose(self, browser_id: str, workvm_id: str) -> None:
        """The live picture found the helper broken: its VM goes like a call's (`vm_lost`)."""
        row = next((found for found in await rows.open_rows(self.db) if found.browser_id == browser_id), None)
        vm = await vm_records.get(self.db, workvm_id)
        if row is not None and vm is not None and row.workvm_id == workvm_id:
            await self._lose(row, vm)

    async def running(self, session_id: str) -> tuple[BrowserRow, WorkVm] | None:
        """The open browser session of a session with its running VM (for the live picture)."""
        row = await rows.open_of(self.db, session_id)
        vm = None if row is None else await self._running(row)
        return None if row is None or vm is None else (row, vm)

    # -- teardown ----------------------------------------------------------------------------------------------

    async def _close(self, row: BrowserRow, reason: str) -> BrowserRow | None:
        """Close the browser session and delete its VM, shielded from cancellation; None when it was closed."""
        return await asyncio.shield(asyncio.ensure_future(self._closing(row, reason)))

    async def _closing(self, row: BrowserRow, reason: str) -> BrowserRow | None:
        closed = await rows.close(self.db, row.browser_id, reason=reason, at=self.now())
        if closed is None:
            return None
        try:
            self._stop_live(closed.session_id)
            vm = None if closed.workvm_id is None else await vm_records.get(self.db, closed.workvm_id)
            if vm is not None and vm.state != "deleted":
                await self.workvms.delete(vm, reason=reason)
        finally:
            await self._state(closed, "closed", reason=reason)
            self.log(f"closed browser_id={closed.browser_id} reason={reason} actions={closed.seq}")
        return closed

    async def session_ended(self, session_id: str, *, reason: str = "session_ended") -> list[str]:
        """The session ended (or its task closed): its browser session closes with the reason. Under the session's
        lock, so a call that opens a browser right now finishes first and its browser closes too."""
        async with self._lock(session_id):
            row = await rows.open_of(self.db, session_id)
            closed = None if row is None else await self._close(row, reason)
        self._prune(session_id)
        return [] if closed is None else [closed.browser_id]

    async def close_task(self, task_id: str) -> list[str]:
        """Every browser session of a closed task closes (`task_closed`); returns their IDs."""
        return await self._close_all("task_closed", lambda row: row.task_id == task_id)

    async def credit_pass(self) -> list[str]:
        """The browser sessions of organizations whose credit is used up close (`credit_exhausted`)."""
        return await self._close_all("credit_exhausted", lambda row: self.workvms.deps.exhausted(row.org_id))

    async def idle_pass(self) -> list[str]:
        """Browser sessions without a call for their `idle_seconds` close (`browser_idle`); a session whose call
        runs now is skipped. Then browser VMs no open browser session refers to go (`sweep`)."""
        now = self.now()
        closed = await self._close_all(
            "browser_idle",
            lambda row: now - row.last_call_at >= timedelta(seconds=row.idle_seconds),
            wait=False,
        )
        await self.sweep()
        return closed

    async def _close_all(self, reason: str, chosen: Callable[[BrowserRow], bool], *, wait: bool = True) -> list[str]:
        """Close the chosen open browser sessions under their session's lock (a call that starts a VM finishes
        first); without `wait` a session whose call runs now is skipped."""
        closed: list[str] = []
        for row in await rows.open_rows(self.db):
            if not chosen(row):
                continue
            lock = self._lock(row.session_id)
            if not wait and lock.locked():
                continue
            async with lock:
                fresh = await rows.open_of(self.db, row.session_id)
                done = None
                if fresh is not None and fresh.browser_id == row.browser_id and chosen(fresh):
                    done = await self._close(fresh, reason)
            self._prune(row.session_id)
            if done is not None:
                closed.append(row.browser_id)
        return closed

    async def sweep(self) -> list[str]:
        """Delete live browser VMs that no open browser session refers to (left by a race with a teardown) unless
        their session is starting one now; returns their IDs."""
        referenced = {row.workvm_id for row in await rows.open_rows(self.db)}
        removed: list[str] = []
        for vm in await vm_records.live(self.db, "starting", "running", "stopping", "stopped"):
            if vm.scope != BROWSER_SCOPE or vm.id in referenced:
                continue
            lock = self.locks.get(vm.session_id or vm.requested_by)
            if lock is not None and lock.locked():
                continue
            await self.workvms.delete(vm, reason="failed")
            self.log(f"orphan_deleted workvm_id={vm.id}")
            removed.append(vm.id)
        return removed

    # -- events ------------------------------------------------------------------------------------------------

    async def _state(self, row: BrowserRow, state: str, *, reason: str | None = None) -> None:
        closed_at = row.closed_at or (self.now() if state == "closed" else None)
        body = {
            "browser_id": row.browser_id,
            "operation_id": row.operation_id,
            "implicit": row.implicit,
            "state": state,
            "workvm_id": row.workvm_id,
            "reason": reason,
            "url": row.url,
            "actions": row.seq,
            "ready_ms": row.ready_ms,
            "idle_seconds": row.idle_seconds,
            "opened_at": _iso(row.opened_at),
            "closed_at": None if closed_at is None else _iso(closed_at),
        }
        await self._emit(row, "browser.state", body)

    async def _emit(self, row: BrowserRow, kind: str, body: Mapping[str, Any]) -> None:
        try:
            await self.workvms.deps.emit(Identity(row.org_id, row.wg_id, row.task_id, row.session_id), kind, body)
        except Exception as error:  # the record must not break the browser; the log keeps the fact
            self.log(f"event_failed kind={kind} error={type(error).__name__}")


def _copy(exported: Path, out: tarfile.TarFile, wanted: Sequence[tuple[int, str]]) -> None:
    """Add the wanted files of a checked guest export to the upload tar as `<n>/<name>`."""
    names: dict[str, list[int]] = {}
    for index, name in wanted:
        names.setdefault(name, []).append(index)
    with tarfile.open(exported, mode="r|") as source:
        for member in source:
            if member.name not in names:
                continue
            reader = source.extractfile(member)
            assert reader is not None  # a checked export holds regular files only
            data = reader.read()
            for index in names[member.name]:
                info = tarfile.TarInfo(f"{index}/{member.name}")
                info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
                out.addfile(info, _bytes(data))


def _bytes(data: bytes) -> Any:
    import io  # noqa: PLC0415

    return io.BytesIO(data)
