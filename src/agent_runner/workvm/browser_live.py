"""The live picture of the platform browser, runner side (docs/platform/spec/13-work-vm.md section 13.6, ADR-085
section 6; 03-api.md section 6.1 `POST /v1/device/sessions/{session}/browser/live`, section 6.2 hint `browser`).

The hub sends the hint `browser {session_id}` while someone watches the session's browser (with every lease write).
`LiveStreams.start` starts one stream per session at a time, only for an open browser session with a running VM (none:
the hint is ignored). A stream:

1. posts an empty body first: `{until, kbps}` keeps it going, `409 not_watched` ends it before anything starts;
2. runs `workvm-browser stream 5 60 1280` over the slot's `limactl shell` as a long-lived process (`Link.spawn`) and
   reads its frames: a header line of at most `HEADER_LIMIT` bytes with a closed schema, then the JPEG bytes (at most
   `FRAME_LIMIT`). A frame outside that schema is a broken helper: the stream ends and the VM goes (`vm_lost`);
3. seals each frame it sends (`crypto.browserframe.seal_frame`, the workgroup's current key and epoch, `seq` the
   browser session's own frame counter `frame_seq`) and posts `{frame}` within the byte budget: after a post of `n`
   bytes the next waits `max(1 / MAX_FPS, n * 8 / (kbps * 1000))` seconds, `kbps` the hub's answer capped by
   `BROWSER_LIVE_MAX_KBPS`. Only the newest frame that did not fit is kept and posted as soon as the budget allows
   (a page that stops changing still shows its last state);
4. posts an empty body when it posted nothing for `KEEPALIVE_SECONDS`.

It ends on `409 not_watched`, when its browser session closes or loses its VM (`stop`), when the helper's output ends,
when no post succeeded for `GIVE_UP_SECONDS`, and at the runner's shutdown (`stop_all`); the process is always ended.
Log line `runner.browser live browser_id= frames= bytes=`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_runner.crypto.browserframe import seal_frame
from agent_runner.crypto.errors import CryptoError
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.workvm import browser_records as rows
from agent_runner.workvm.browser_helper import HEADER_LIMIT, parse_header, stream_argv
from agent_runner.workvm.browser_records import BrowserRow
from agent_runner.workvm.jobs import WorkVmError
from agent_runner.workvm.records import WorkVm

if TYPE_CHECKING:
    from agent_runner.workvm.browser import Browsers

TARGET = "/v1/device/sessions/{session}/browser/live"
KEEPALIVE_SECONDS = 10.0
MAX_FPS = 5
GIVE_UP_SECONDS = 30.0
STOP_WAIT = 5.0
DEFAULT_KBPS = 1000


@dataclass
class Link:
    """How a stream reaches the hub, the workgroup key and the VM (injected; production in `service.py`)."""

    post: Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
    """A signed POST to the hub: the verified answer, or `HubRefusal` / `HubTransportError`."""
    key: Callable[[str], Awaitable[tuple[int, bytes] | None]]
    """The workgroup's current epoch and key (None: the device holds none)."""
    spawn: Callable[[WorkVm, Sequence[str]], Awaitable[Any]]
    """Start a long-lived process in the VM (an `asyncio.subprocess.Process` with stdin and stdout pipes)."""


async def spawn_process(shell: Any, argv: Sequence[str]) -> asyncio.subprocess.Process:
    """`argv` in the VM of a `VmShell` as a process with stdin and stdout pipes (stderr dropped)."""
    return await asyncio.create_subprocess_exec(
        *shell.argv(argv),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=HEADER_LIMIT + 2,
    )


@dataclass
class _Slot:
    """The newest frame read and not yet sent, and the state of the reader."""

    newest: bytes | None = None
    ended: bool = False
    invalid: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Totals:
    frames: int = 0
    bytes: int = 0
    kbps: int = DEFAULT_KBPS
    last_ok: float = 0.0
    key_missing: bool = False


class _Stop(Exception):
    """The stream ends (`409 not_watched`, or no successful post for `GIVE_UP_SECONDS`)."""


class LiveStreams:
    def __init__(
        self,
        browsers: Browsers,
        link: Link,
        *,
        max_kbps: int,
        keepalive: float = KEEPALIVE_SECONDS,
        give_up: float = GIVE_UP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.browsers = browsers
        self.link = link
        self.max_kbps = max_kbps
        self.keepalive = keepalive
        self.give_up = give_up
        self.clock = clock
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.starting: set[str] = set()

    async def start(self, session_id: str) -> bool:
        """Start the stream of the session's open browser session unless one runs; False: nothing started."""
        if session_id in self.tasks or session_id in self.starting:
            return False
        self.starting.add(session_id)
        try:
            found = await self.browsers.running(session_id)
        finally:
            self.starting.discard(session_id)
        if found is None:
            return False
        task = asyncio.ensure_future(self._stream(*found))
        self.tasks[session_id] = task
        task.add_done_callback(lambda _done: self.tasks.pop(session_id, None))
        return True

    def stop(self, session_id: str) -> None:
        """End the session's stream (its browser session closed or lost its VM)."""
        task = self.tasks.get(session_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def stop_all(self) -> None:
        """End every stream (runner shutdown) and wait for their processes to end."""
        running = list(self.tasks.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    # -- one stream --------------------------------------------------------------------------------------------

    async def _stream(self, row: BrowserRow, vm: WorkVm) -> None:
        totals = _Totals(kbps=min(DEFAULT_KBPS, self.max_kbps), last_ok=self.clock())
        slot = _Slot()
        process: Any = None
        reader: asyncio.Future[None] | None = None
        try:
            await self._post(row, {}, totals)
            process = await self.link.spawn(vm, stream_argv())
            reader = asyncio.ensure_future(_read(process.stdout, slot))
            await self._pace(row, slot, totals)
        except _Stop:
            pass
        finally:
            if reader is not None:
                reader.cancel()
            if process is not None:
                await _end(process)
            self.browsers.log(f"live browser_id={row.browser_id} frames={totals.frames} bytes={totals.bytes}")
        if slot.invalid:
            await self.browsers.lose(row.browser_id, vm.id)

    async def _pace(self, row: BrowserRow, slot: _Slot, totals: _Totals) -> None:
        last_post = next_at = self.clock()
        while True:
            slot.changed.clear()
            if slot.ended:
                return
            now = self.clock()
            if slot.newest is not None and now >= next_at:
                jpeg, slot.newest = slot.newest, None
                size = await self._frame(row, jpeg, totals)
                last_post = self.clock()
                next_at = last_post + max(1.0 / MAX_FPS, size * 8 / (totals.kbps * 1000))
                continue
            if slot.newest is None and now - last_post >= self.keepalive:
                await self._post(row, {}, totals)
                last_post = self.clock()
                continue
            wait = next_at - now if slot.newest is not None else last_post + self.keepalive - now
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(slot.changed.wait(), wait)

    async def _frame(self, row: BrowserRow, jpeg: bytes, totals: _Totals) -> int:
        """Seal and post one frame; the bytes posted (0: not sealed)."""
        key = await self.link.key(row.wg_id)
        if key is None:
            if not totals.key_missing:
                self.browsers.log(f"live_key_missing browser_id={row.browser_id}")
            totals.key_missing = True
            return 0
        epoch, workgroup_key = key
        seq = await rows.next_frame(self.browsers.db, row.browser_id)
        try:
            frame = seal_frame(
                workgroup_key,
                browser=row.browser_id,
                session=row.session_id,
                wg=row.wg_id,
                epoch=epoch,
                seq=seq,
                ts=_iso_now(self.browsers),
                jpeg=jpeg,
            )
        except CryptoError as error:
            self.browsers.log(f"live_seal_failed browser_id={row.browser_id} code={error}")
            return 0
        size = len(json.dumps(frame))
        if await self._post(row, {"frame": frame}, totals):
            totals.frames += 1
            totals.bytes += size
        return size

    async def _post(self, row: BrowserRow, body: Mapping[str, Any], totals: _Totals) -> bool:
        """One post; True when the hub took it. `409 not_watched` and a hub unreachable for `give_up` end it."""
        try:
            answer = await self.link.post(TARGET.format(session=row.session_id), body)
        except HubRefusal as refused:
            if refused.status == 409:
                raise _Stop from None
            self.browsers.log(f"live_post_failed browser_id={row.browser_id} code={refused.code}")
        except HubTransportError as failed:
            self.browsers.log(f"live_post_failed browser_id={row.browser_id} {failed.describe()}")
        else:
            kbps = answer.get("kbps")
            if type(kbps) is int and kbps > 0:
                totals.kbps = min(kbps, self.max_kbps)
            totals.last_ok = self.clock()
            return True
        if self.clock() - totals.last_ok >= self.give_up:
            raise _Stop
        return False


async def _read(stdout: asyncio.StreamReader, slot: _Slot) -> None:
    """Frames of the helper's screencast into `slot` (only the newest is kept)."""
    try:
        while True:
            try:
                line = await stdout.readuntil(b"\n")
            except asyncio.IncompleteReadError as error:
                slot.invalid = bool(error.partial)
                return
            header = parse_header(line[:-1])
            slot.newest = await stdout.readexactly(header.bytes)
            slot.changed.set()
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, WorkVmError, ValueError):
        slot.invalid = True
    finally:
        slot.ended = True
        slot.changed.set()


async def _end(process: Any) -> None:
    """Close the screencast's stdin (it ends with it), kill it when it does not, wait for it."""
    if process.stdin is not None:
        process.stdin.close()
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(process.wait()), STOP_WAIT)


def _iso_now(browsers: Browsers) -> str:
    value = browsers.now()
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
