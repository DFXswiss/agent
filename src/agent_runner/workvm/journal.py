"""Journal handler `workvm.call` (`<area>/journal.py:HANDLERS`, ADR-027 section 4; docs/platform/spec/13-work-vm.md
section 3; contracts/changes/KOR-29.md).

The guest journals every call of a work VM tool and of a platform browser tool (`browser_*`, section 13.4) as
`workvm.call {pending_id, tool, arguments}` and waits for the answer. The handler runs inside the journal page's
transaction: it resolves the session from the journal of the workgroup VM that carried the record (`journal.session`: a
session of another VM is ignored, so a guest can only drive the work VM of its own sessions), checks the body against
the closed argument rules of the contracts (the guest has already validated them; the runner does not trust it) and
answers the call in the background with the result of `WorkVms.call` (`Browsers.call` for a browser tool), or with a
tool error. A pending call is taken once even when its page is read again. Nothing of the call is recorded as a read:
the call is visible as the `session.tool` event of the transcript.

A session whose rights profile does not allow the type a tool stands for (`workvm.request` for the work VM tools,
`browser.open` for the browser tools; ADR-090 section 3) gets the tool error `forbidden` with the profile's sentence
before anything starts.

A body that is not `{pending_id, tool, arguments}` with a UUID, a known tool and valid arguments (`RULES`, and the
combinations of `COMBINED`) is refused
(`JournalRecordInvalid`); the journal moves on and the guest's call times out.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.errors import RunnerError
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestError
from agent_runner.operations.journal import storable
from agent_runner.profiles import check as profile_check
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.sessions.records import LocalSession
from agent_runner.vm.records import VmRow
from agent_runner.workvm.browser import Browsers
from agent_runner.workvm.manager import NOT_ENABLED, CallFailed, WorkVms
from agent_runner.workvm.service import browsers_for, workvms_for

KIND = "workvm.call"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_JOB_RE = re.compile(r"^j-[0-9a-f]{12}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CWD_RE = re.compile(r"^/[^\x00-\x1f\x7f]*$")
_REL_RE = re.compile(r"^[^/\x00-\x1f\x7f][^\x00-\x1f\x7f]*$")
_URL_RE = re.compile(r"^https?://[^\x00-\x20\x7f]+$")
MAX_SAFE = 2**53 - 1
PENDING_MEMORY = 4096
MAX_CALLS_PER_SESSION = 16
ANSWER_ATTEMPTS = 3


def _text(value: object, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= len(value) <= high


def _integer(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _rel_path(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 4096 and _REL_RE.fullmatch(value) is not None


def _env(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 64
        and all(
            isinstance(item, dict)
            and set(item) == {"name", "value"}
            and isinstance(item["name"], str)
            and _ENV_NAME_RE.fullmatch(item["name"]) is not None
            and _text(item["value"], 0, 32768)
            for item in value
        )
    )


RULES: Mapping[str, tuple[Mapping[str, Callable[[object], bool]], frozenset[str]]] = {
    "workvm_exec": (
        {
            "command": lambda v: _text(v, 1, 65536),
            "cwd": lambda v: isinstance(v, str) and len(v) <= 4096 and _CWD_RE.fullmatch(v) is not None,
            "env": _env,
            "stdin": lambda v: _text(v, 0, 1048576),
            "timeout_seconds": lambda v: _integer(v, 1, 604800),
            "wait_seconds": lambda v: _integer(v, 0, 30),
            "sync": lambda v: v in ("to_vm", "none"),
        },
        frozenset({"command"}),
    ),
    "workvm_output": (
        {
            "job_id": lambda v: isinstance(v, str) and _JOB_RE.fullmatch(v) is not None,
            "offset": lambda v: _integer(v, 0, MAX_SAFE),
            "limit": lambda v: _integer(v, 1, 32768),
            "wait_seconds": lambda v: _integer(v, 0, 30),
        },
        frozenset({"job_id"}),
    ),
    "workvm_kill": (
        {
            "job_id": lambda v: isinstance(v, str) and _JOB_RE.fullmatch(v) is not None,
            "signal": lambda v: v in ("TERM", "INT", "HUP", "KILL"),
        },
        frozenset({"job_id"}),
    ),
    "workvm_status": ({}, frozenset()),
    "workvm_sync": (
        {
            "direction": lambda v: v in ("to_vm", "from_vm"),
            "path": _rel_path,
        },
        frozenset({"direction"}),
    ),
    "workvm_browse": (
        {
            "url": lambda v: isinstance(v, str) and len(v) <= 4096 and _URL_RE.fullmatch(v) is not None,
            "action": lambda v: v in ("read", "screenshot", "click", "fill"),
            "selector": lambda v: _text(v, 1, 1024),
            "text": lambda v: _text(v, 0, 65536),
            "wait_ms": lambda v: _integer(v, 0, 30000),
            "path": _rel_path,
        },
        frozenset({"url"}),
    ),
}
"""Argument rules per tool (`contracts/mcp/objects.json#/$defs/input_workvm_*`; a test compares the key sets)."""


def _strings(low: int, high: int, length: int) -> Callable[[object], bool]:
    return lambda value: (
        isinstance(value, list) and low <= len(value) <= high and all(_text(item, 0, length) for item in value)
    )


def _paths(value: object) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= 16 and all(_rel_path(item) for item in value)


def _url(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 4096 and _URL_RE.fullmatch(value) is not None


def _bool(value: object) -> bool:
    return isinstance(value, bool)


def _target(value: object) -> bool:
    return _text(value, 1, 1024)


def _browser(fields: Mapping[str, Callable[[object], bool]], required: frozenset[str] = frozenset()) -> Any:
    """A browser tool's rules: its own fields and `snapshot` (section 13.4)."""
    return ({**fields, "snapshot": _bool}, required)


BROWSER_RULES: Mapping[str, tuple[Mapping[str, Callable[[object], bool]], frozenset[str]]] = {
    "browser_navigate": _browser(
        {
            "url": _url,
            "history": lambda v: v in ("back", "forward", "reload"),
            "query": lambda v: _text(v, 1, 512),
            "wait_until": lambda v: v in ("load", "domcontentloaded", "networkidle"),
        }
    ),
    "browser_snapshot": ({}, frozenset()),
    "browser_click": _browser(
        {
            "target": _target,
            "x": lambda v: _integer(v, 0, 1279),
            "y": lambda v: _integer(v, 0, 799),
            "double": _bool,
            "button": lambda v: v in ("left", "right", "middle"),
        }
    ),
    "browser_hover": _browser({"target": _target}, frozenset({"target"})),
    "browser_type": _browser(
        {"target": _target, "text": lambda v: _text(v, 0, 65536), "submit": _bool, "clear": _bool},
        frozenset({"target", "text"}),
    ),
    "browser_select": _browser({"target": _target, "values": _strings(1, 64, 1024)}, frozenset({"target", "values"})),
    "browser_press": _browser({"key": lambda v: _text(v, 1, 64)}, frozenset({"key"})),
    "browser_scroll": _browser({"target": _target, "dy": lambda v: _integer(v, -100000, 100000)}),
    "browser_wait": _browser(
        {
            "text": lambda v: _text(v, 1, 1024),
            "gone": lambda v: _text(v, 1, 1024),
            "url": lambda v: _text(v, 1, 4096),
            "ms": lambda v: _integer(v, 0, 20000),
            "timeout_ms": lambda v: _integer(v, 0, 20000),
        }
    ),
    "browser_evaluate": _browser({"expression": lambda v: _text(v, 1, 65536)}, frozenset({"expression"})),
    "browser_upload": _browser({"target": _target, "paths": _paths}, frozenset({"target", "paths"})),
    "browser_dialog": _browser(
        {"action": lambda v: v in ("accept", "dismiss"), "text": lambda v: _text(v, 0, 4096)}, frozenset({"action"})
    ),
    "browser_tabs": _browser(
        {
            "action": lambda v: v in ("list", "new", "select", "close"),
            "index": lambda v: _integer(v, 0, 63),
            "url": _url,
        },
        frozenset({"action"}),
    ),
    "browser_download": _browser({"target": _target, "url": _url, "path": _rel_path}),
    "browser_screenshot": _browser({"path": _rel_path, "full_page": _bool}),
}
"""Argument rules of the platform browser tools (`contracts/mcp/objects.json#/$defs/input_browser_*`, section 13.4;
a test compares the key sets)."""

TOOL_RULES = {**RULES, **BROWSER_RULES}


def _one_of(arguments: Mapping[str, Any], *names: str) -> int:
    return sum(1 for name in names if name in arguments)


COMBINED: Mapping[str, Callable[[Mapping[str, Any]], bool]] = {
    "browser_navigate": lambda a: _one_of(a, "url", "history", "query") == 1,
    "browser_click": lambda a: ("target" in a) != ("x" in a or "y" in a) and ("x" in a) == ("y" in a),
    "browser_wait": lambda a: _one_of(a, "text", "gone", "url", "ms") >= 1,
    "browser_download": lambda a: _one_of(a, "target", "url") == 1,
}
"""Rules across the arguments of a tool (exactly one of, at least one of; section 13.4)."""


def parse(body: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """`(pending_id, tool, arguments)` of a `workvm.call` body (`JournalRecordInvalid` otherwise)."""
    pending_id, tool, arguments = body.get("pending_id"), body.get("tool"), body.get("arguments")
    if (
        set(body) != {"pending_id", "tool", "arguments"}
        or not isinstance(pending_id, str)
        or not _UUID_RE.fullmatch(pending_id)
        or not isinstance(tool, str)
        or tool not in TOOL_RULES
        or not isinstance(arguments, dict)
        or not storable(arguments)
    ):
        raise JournalRecordInvalid(KIND)
    fields, required = TOOL_RULES[tool]
    if not required <= set(arguments) or not set(arguments) <= set(fields):
        raise JournalRecordInvalid(KIND)
    if not all(fields[name](value) for name, value in arguments.items()):
        raise JournalRecordInvalid(KIND)
    if not COMBINED.get(tool, _anything)(arguments):
        raise JournalRecordInvalid(KIND)
    return pending_id, tool, arguments


def _anything(_arguments: Mapping[str, Any]) -> bool:
    return True


class Calls:
    """The background answering of work VM and browser calls of one daemon; without `WorkVms` (no work VMs on this
    device) every call is answered `unavailable` at once."""

    def __init__(self, workvms: WorkVms | None, browsers: Browsers | None = None) -> None:
        self.workvms = workvms
        self.browsers = browsers
        self.answered: dict[str, None] = {}
        self.running: dict[str, int] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def spawn(
        self,
        env: Any,
        vm: VmRow,
        session: LocalSession,
        *,
        pending_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        refused: str | None = None,
    ) -> asyncio.Task[None] | None:
        if pending_id in self.answered:
            return None
        self.answered[pending_id] = None
        while len(self.answered) > PENDING_MEMORY:
            del self.answered[next(iter(self.answered))]
        task = asyncio.ensure_future(
            self._answer(env, vm, session, pending_id=pending_id, tool=tool, arguments=arguments, refused=refused)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _answer(
        self,
        env: Any,
        vm: VmRow,
        session: LocalSession,
        *,
        pending_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        refused: str | None = None,
    ) -> None:
        self.running[session.id] = self.running.get(session.id, 0) + 1
        try:
            if refused is not None:
                answer = _error("forbidden", refused)
            elif self.running[session.id] > MAX_CALLS_PER_SESSION:
                answer = _error("unavailable", "Too many work VM calls of this session run at once. Try again.")
            else:
                answer = await self._outcome(env, session, tool, arguments)
        finally:
            self.running[session.id] -= 1
        for attempt in range(ANSWER_ATTEMPTS):
            try:
                async with await env.client(vm) as client:
                    await client.answer_pending(pending_id, answer)
                return
            except (GuestError, RunnerError) as error:
                env.log(f"runner.workvm answer_failed pending_id={pending_id} attempt={attempt + 1} error={error.code}")
                await asyncio.sleep(0.2 * (attempt + 1))

    async def _outcome(
        self, env: Any, session: LocalSession, tool: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.workvms is None:
            return _error("unavailable", NOT_ENABLED)
        try:
            if tool in BROWSER_RULES:
                if self.browsers is None:
                    return _error("unavailable", NOT_ENABLED)
                return {"status": "ok", "result": await self.browsers.call(env, session, tool, arguments)}
            return {"status": "ok", "result": await self.workvms.call(env, session, tool, arguments)}
        except CallFailed as failed:
            return _error(failed.error, failed.message)
        except Exception as error:  # a call must always be answered; the type goes to the log only
            env.log(f"runner.workvm call_failed tool={tool} session_id={session.id} error={type(error).__name__}")
            return _error("unavailable", "The work VM call failed. Try again.")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"error": code, "message": message}}


ATTRIBUTE = "_workvm_calls"


def calls_for(context: Any) -> Calls:
    """The daemon's `Calls` (created on first use with the daemon's `WorkVms`, or none)."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Calls):
        return found
    calls = Calls(workvms_for(context), browsers_for(context))
    setattr(context, ATTRIBUTE, calls)
    return calls


async def workvm_call(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    pending_id, tool, arguments = parse(record.body)
    refused = await profile_check.tool(conn, session, tool)
    calls_for(journal.env.context).spawn(
        journal.env, journal.vm, session, pending_id=pending_id, tool=tool, arguments=arguments, refused=refused
    )


HANDLERS: dict[str, Callable[..., Any]] = {KIND: workvm_call}
