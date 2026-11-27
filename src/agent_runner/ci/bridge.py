"""The scale-set listener as a child process: the Go sidecar `agent-ci-listener` over `actions/scaleset` (decision
D-2 (a), spec 16 section 17.2).

The sidecar holds one message session of one scale set (long poll, acknowledgement) and speaks JSON lines on its
standard input and output; it decides nothing. Lines of the sidecar:

| `type` | Fields | Meaning |
|---|---|---|
| `ready` | `scale_set_id` | the session is open |
| `batch` | `message_id`, `statistics`, `available`, `assigned`, `started`, `completed` | one message (see below) |
| `acquired` | `message_id`, `request_ids` | the jobs the service gave the scale set |
| `jit` | `ref`, `encoded` | the encoded JIT configuration of a runner (answer to `jit`) |
| `removed` | `ref` | a runner registration was removed (answer to `remove`) |
| `failed` | `ref`, `error` | a request failed (closed code of the sidecar) |

For a `batch` the sidecar waits for `acquire` with the same `message_id` before it acknowledges the message.
Lines of the controller: `acquire {message_id, request_ids}`, `jit {ref, name}`, `remove {ref, runner_id}` and
`capacity {max}` (the most runners the scale set offers now; `SetMaxRunners`).

`Bridge.run(on_batch, on_acquired)` reads the sidecar until it exits: `on_batch(batch)` answers the request IDs to
acquire (an exception there is logged and answered with none, so a message is never left unacknowledged), and
`on_acquired` gets the acquired IDs. `jit(name)` and `remove(runner_id)` wait for their answer at most
`ANSWER_TIMEOUT` seconds (`BridgeError` otherwise). The JIT configuration is held only in memory, on its way to the
runner, and never logged. A line that is no JSON object is skipped and counted (`skipped`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

ANSWER_TIMEOUT = 60.0
LINE_MAX = 4 * 1024 * 1024

OnBatch = Callable[[Mapping[str, Any]], Awaitable[Sequence[int]]]
OnAcquired = Callable[[Sequence[int]], Awaitable[None]]


class BridgeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Bridge:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        log: Callable[[str], None] = lambda _text: None,
        answer_timeout: float = ANSWER_TIMEOUT,
    ) -> None:
        self.argv = list(argv)
        self.env = dict(env) if env is not None else None
        self.log = log
        self.answer_timeout = answer_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.waiting: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self.next_ref = 0
        self.ready = asyncio.Event()
        self.skipped = 0

    async def _send(self, data: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise BridgeError("listener_down")
        try:
            process.stdin.write(json.dumps(data, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise BridgeError("listener_down") from None

    async def _ask(self, data: Mapping[str, Any]) -> Mapping[str, Any]:
        self.next_ref += 1
        ref = str(self.next_ref)
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self.waiting[ref] = future
        try:
            await self._send({**data, "ref": ref})
            answer = await asyncio.wait_for(future, self.answer_timeout)
        except TimeoutError:
            raise BridgeError("timeout") from None
        finally:
            self.waiting.pop(ref, None)
        if answer.get("type") == "failed":
            raise BridgeError(str(answer.get("error", "failed")))
        return answer

    async def jit(self, name: str) -> str:
        """The encoded JIT configuration of a new runner named `name`."""
        answer = await self._ask({"type": "jit", "name": name})
        encoded = answer.get("encoded")
        if answer.get("type") != "jit" or not isinstance(encoded, str) or not encoded:
            raise BridgeError("answer_invalid")
        return encoded

    async def remove(self, runner_id: int) -> None:
        answer = await self._ask({"type": "remove", "runner_id": runner_id})
        if answer.get("type") != "removed":
            raise BridgeError("answer_invalid")

    async def capacity(self, most: int) -> None:
        await self._send({"type": "capacity", "max": max(0, most)})

    async def _batch(self, line: Mapping[str, Any], on_batch: OnBatch) -> None:
        message_id = line.get("message_id")
        try:
            chosen = [int(ident) for ident in await on_batch(line)]
        except Exception as error:  # the message is answered anyway, so the service does not redeliver forever
            self.log(f"ci.listener batch_failed error={type(error).__name__}")
            chosen = []
        await self._send({"type": "acquire", "message_id": message_id, "request_ids": chosen})

    async def run(self, on_batch: OnBatch, on_acquired: OnAcquired) -> int:
        """Start the sidecar and serve it until it exits; returns its exit code."""
        self.process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self.env,
            limit=LINE_MAX,
        )
        assert self.process.stdout is not None
        try:
            while raw := await self.process.stdout.readline():
                try:
                    line = json.loads(raw)
                except ValueError:
                    line = None
                if not isinstance(line, dict):
                    self.skipped += 1
                    continue
                await self._dispatch(line, on_batch, on_acquired)
        finally:
            for future in self.waiting.values():
                with contextlib.suppress(asyncio.InvalidStateError):  # answered just before the end
                    future.set_result({"type": "failed", "error": "listener_down"})
            with contextlib.suppress(ProcessLookupError):
                if self.process.returncode is None:
                    self.process.terminate()
            code = await self.process.wait()
            self.ready.clear()
        return code

    async def _dispatch(self, line: Mapping[str, Any], on_batch: OnBatch, on_acquired: OnAcquired) -> None:
        kind = line.get("type")
        if kind == "ready":
            self.ready.set()
            self.log(f"ci.listener ready scale_set_id={line.get('scale_set_id')}")
        elif kind == "batch":
            await self._batch(line, on_batch)
        elif kind == "acquired":
            ids = line.get("request_ids")
            if isinstance(ids, list):
                await on_acquired([ident for ident in ids if isinstance(ident, int) and not isinstance(ident, bool)])
        elif kind in ("jit", "removed", "failed"):
            future = self.waiting.get(str(line.get("ref")))
            if future is not None and not future.done():
                future.set_result(line)
        else:
            self.skipped += 1
