"""Claude Code HTTP hooks of the API process: ``POST /hooks/<Event>`` for the 18 events (05-runner.md section 5.2).

Every request:

1. ``Authorization: Bearer <session token>`` -> ``session.resolve`` over ``api.sock`` (constant-time in the control
   process, bound to the UID of the connecting process, ``peer.py``); a missing, unknown or foreign token answers
   ``401 unauthorized``, and after too many failed checks of that UID ``429 rate_limited`` (``authguard.py``).
2. Fault switch (``fault.py``): with ``AGENT_GUEST_FAULT_HOOKS`` ``500`` or ``open`` in a TEST_MODE golden
   (``test_mode`` true in the golden's ``install.json``) the hook is journaled with ``decision: "none"`` and answered
   ``500``; ``500`` keeps the fail-closed reaction of step 6, ``open`` suppresses it (attack suite: the OS layer alone
   must hold). Elsewhere the switch has no effect.
3. The body (at most ``HOOK_MAX_BODY`` bytes, strict JSON, ``contracts/mcp/hooks/<Event>.request.json``) is read. A
   body that cannot be read fails closed (decision 36 of CTR-02): PreToolUse ``deny`` ``input_invalid`` (rule 4),
   UserPromptSubmit ``block``, every other event ``{}``; it never answers with an error.
4. The decision (03-api.md section 8.2):

   - ``PreToolUse``: ``hook.pre_tool_use`` (policy as root, journal) -> allow / deny with the fixed reason; for
     ``AskUserQuestion`` the op also carries the ``question`` built from the tool input (rule 1a: the control process
     requests ``human.question`` for a main session)
   - ``UserPromptSubmit``: ``prompt.check`` (registry, journal, ``prompt_accepted`` / ``prompt_refused``) -> ``{}`` or
     the block
   - every other event: journal ``session.hook {event, tool?, decision: "none"}`` and answer ``{}``;
     ``SubagentStart`` also ``guest.error subagent_hook_seen``; ``PostToolUse`` for ``Write``/``Edit`` also
     ``session.tool`` (``hook.post_tool_use``) and the file hint ``file_changed(session, path)``. ``Stop`` and
     ``StopFailure`` are only journaled here; the state change belongs to the addons of GST-06 and RUN-11.

5. Hook addons (``hook_addons/<name>.py:ADDON``, a ``HookAddon``, ADR-027 section 4) run in module order for the
   events they list, after the decision, with a ``HookCall``. For PreToolUse, PostToolUse and an accepted
   UserPromptSubmit (``call.decorate``) a returned text becomes ``hookSpecificOutput.additionalContext`` (several
   texts joined by a blank line); for every other event (``Stop`` included) and for a blocked prompt the answer stays
   exactly as decided, and an addon must not consume anything it could not deliver. An addon that raises, returns
   text where it cannot be attached, or makes the answer violate its response schema is skipped (its text dropped),
   counted and journaled as ``guest.error hook_handler_failed``; the decision itself never changes.
6. Fail closed (ADR-064 section 2): a decision hook (``DECISION_EVENTS``: ``UserPromptSubmit``, ``PreToolUse``,
   ``Stop``, ``StopFailure``) that fails - the fault switch ``500``, a control call that failed, an unexpected
   exception, a failing addon of ``Stop``/``StopFailure`` (it ends the turn), or an answer that took
   ``HOOK_TIMEOUT_SECONDS`` or longer (Claude Code went on without it) - is reported to the control process
   (``session.hook_failed``), which ends the session with ``hook_unavailable``. ``PreToolUse`` and
   ``UserPromptSubmit`` still get their closed answers; the other events only journal ``guest.error``.

If the control process cannot be reached or an op fails, PreToolUse and UserPromptSubmit still fail closed (deny
``input_invalid`` / block, nothing journaled); every other event answers ``503`` (Claude Code then continues: layer
1 and the OS sandbox still hold, 05 section 5.5). ``STATS`` counts errors, injected faults,
denies and blocks of this process (``agent_runner_session_hook_errors_total`` is derived by the runner from the
``guest.error hook_handler_failed`` records).
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from typing import Any, cast

from agent_guest import peer, registry
from agent_guest.apisock import ApiClient, ApiSockError, AuthLimited
from agent_guest.config import GuestConfig
from agent_guest.fault import fault_mode, guard_suppressed
from agent_guest.policy import (
    ASK_TOOL,
    BLOCK_REASON,
    PATH_KEYS,
    PATTERN_KEYS,
    TOOL_NAME_RE,
    WRITE_TOOLS,
    deny_reason,
    question_text,
)
from agent_guest.routing import (
    CHUNK,
    BodyReader,
    HttpError,
    Request,
    Response,
    Route,
    json_response,
    parse_json,
    rate_limited,
)
from agent_guest.sandbox.settings import HOOK_TIMEOUT_SECONDS
from agent_guest.server import ApiContext

EVENTS: tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "Notification",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "ConfigChange",
    "InstructionsLoaded",
    "CwdChanged",
)
DECORATED_EVENTS = frozenset({"PreToolUse", "PostToolUse", "UserPromptSubmit"})
HOOK_MAX_BODY = 16 * 1024 * 1024
DRAIN_MAX_BODY = 64 * 1024 * 1024
ROUTE_MAX_BODY = 10**16 - 1
MAX_POLICY_INPUT = 1024 * 1024
MAX_TOOL_TEXT = 65536
MAX_SUMMARY = 256
MAX_COUNT = 9007199254740991
FAULT_STATUS = 500
FAULT_REASON = "fault injected"
BODY_INVALID_REASON = "hook body invalid"
_TOOL_USE_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


@dataclass(frozen=True)
class HookCall:
    """What an addon sees: the event, the session, the parsed body (``None`` if unreadable) and the decision."""

    event: str
    session_id: str
    body: Mapping[str, Any] | None
    decision: str
    decorate: bool


AddonHandler = Callable[[ApiContext, HookCall], str | None]


@dataclass(frozen=True)
class HookAddon:
    """``hook_addons/<name>.py:ADDON``: the events it wants and the function returning an optional context text."""

    events: frozenset[str]
    handle: AddonHandler


class HookStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {"errors": 0, "faults": 0, "denies": 0, "blocks": 0, "unauthorized": 0}

    def count(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


STATS = HookStats()


def load_addons(package: str = registry.HOOK_ADDONS) -> list[tuple[str, HookAddon]]:
    """The discovered addons in module order; anything but a ``HookAddon`` for known events refuses the start."""
    found: list[tuple[str, HookAddon]] = []
    for name, addon in registry.hook_addons(package).items():
        if not isinstance(addon, HookAddon) or not addon.events or not addon.events <= set(EVENTS):
            raise registry.RegistryError("registry_invalid", f"{package}.{name}.ADDON")
        found.append((name, addon))
    return found


def fault_active(config: GuestConfig) -> bool:
    """The hooks answer 500 (``fault.fault_mode``: ``500`` or ``open`` in a TEST_MODE golden)."""
    return fault_mode(config) is not None


def bearer(headers: Message) -> str | None:
    values = headers.get_all("Authorization") or []
    if len(values) != 1:
        return None
    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def file_changed(control: ApiClient, session_id: str, path: str) -> None:
    """Hint for the workspace scanner (GST-05) that ``path`` of the session was written."""
    control.call("file.changed", {"session_id": session_id, "path": path})


def _note(context: ApiContext, kind: str, body: dict[str, Any], session_id: str) -> None:
    """Journal a record whose loss must not change the answer (errors, faults)."""
    with contextlib.suppress(ApiSockError):
        context.control.append(kind, body, session_id)


def _failure(context: ApiContext, session_id: str) -> None:
    STATS.count("errors")
    _note(context, "guest.error", {"code": "hook_handler_failed"}, session_id)


def _hook_failed(context: ApiContext, session_id: str, event: str) -> None:
    """A decision hook failed (ADR-064 section 2): the control process ends the session (``hook_unavailable``). Not
    for the other events, and not with the fault switch ``open``; an unreachable control process changes nothing."""
    if event not in DECISION_EVENTS or guard_suppressed(context.config):
        return
    with contextlib.suppress(ApiSockError):
        context.control.call("session.hook_failed", {"session_id": session_id, "event": event})


def _read_body(context: ApiContext, event: str, request: Request) -> dict[str, Any] | None:
    stream = cast(BodyReader, request.stream)
    if stream.remaining > HOOK_MAX_BODY:
        # Read and drop a moderately large body so that the client still receives the closed answer.
        while 0 < stream.remaining <= DRAIN_MAX_BODY:
            stream.read(CHUNK)
        return None
    try:
        value = parse_json(stream.read())
        # Lone surrogates parse but are no text: such a body cannot be checked (and could not cross api.sock).
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except ValueError:
        return None
    if not isinstance(value, dict) or context.schemas.errors(f"mcp/hooks/{event}.request.json", value):
        return None
    return value


def closed_answer(event: str) -> dict[str, Any] | None:
    """The fail-closed answer of PreToolUse and UserPromptSubmit (``None`` for events without a decision)."""
    if event == "PreToolUse":
        output = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_reason("input_invalid"),
        }
        return {"hookSpecificOutput": output}
    if event == "UserPromptSubmit":
        return {"decision": "block", "reason": BLOCK_REASON}
    return None


def _tool(body: Mapping[str, Any] | None) -> str | None:
    name = body.get("tool_name") if body is not None else None
    return name if isinstance(name, str) and TOOL_NAME_RE.fullmatch(name) else None


def _pre_tool_use(context: ApiContext, session_id: str, body: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    decision: dict[str, Any]
    reduced: dict[str, Any] = {}
    if body is not None:
        tool_input = body["tool_input"]
        reduced = {key: tool_input[key] for key in (*PATH_KEYS, *PATTERN_KEYS.values()) if key in tool_input}
    if body is None or len(json.dumps(reduced)) > MAX_POLICY_INPUT:
        record: dict[str, Any] = {"event": "PreToolUse", "decision": "deny", "rule": 4}
        tool = _tool(body)
        if tool is not None:
            record["tool"] = tool
        record["reason"] = deny_reason("input_invalid")
        context.control.append("session.hook", record, session_id)
        decision = {"decision": "deny", "reason": record["reason"]}
    else:
        args: dict[str, Any] = {"session_id": session_id, "tool_name": body["tool_name"], "tool_input": reduced}
        question = question_text(body["tool_input"]) if body["tool_name"] == ASK_TOOL else ""
        if question:
            args["question"] = question
        decision = context.control.call("hook.pre_tool_use", args)
    output: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": decision["decision"]}
    if decision["decision"] == "deny":
        STATS.count("denies")
        output["permissionDecisionReason"] = decision["reason"]
    return {"hookSpecificOutput": output}, decision["decision"]


def _user_prompt_submit(
    context: ApiContext, session_id: str, body: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    prompt = body["prompt"] if body is not None else None
    answer = context.control.call("prompt.check", {"session_id": session_id, "prompt": prompt})
    if answer["accepted"]:
        return {}, "allow"
    STATS.count("blocks")
    return {"decision": "block", "reason": BLOCK_REASON}, "block"


def _text(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text[:MAX_TOOL_TEXT]


def _written(context: ApiContext, session_id: str, workspace: str, body: dict[str, Any]) -> None:
    """PostToolUse of Write/Edit: ``session.tool`` and the file hint (05 section 5.5)."""
    tool_input = body["tool_input"]
    file_path = tool_input.get("file_path")
    tool_use_id = body.get("tool_use_id")
    if isinstance(tool_use_id, str) and len(tool_use_id) <= 128 and set(tool_use_id) <= _TOOL_USE_ID_CHARS:
        duration = body.get("duration_ms")
        record = {
            "tool_use_id": tool_use_id,
            "name": body["tool_name"],
            "input_summary": file_path[:MAX_SUMMARY] if isinstance(file_path, str) else "",
            "input": _text(tool_input),
            "result_summary": _text(body.get("tool_response", "")),
            "is_error": False,
            "duration_ms": duration if type(duration) is int and 0 <= duration <= MAX_COUNT else 0,
        }
        context.control.call("hook.post_tool_use", {"session_id": session_id, "body": record})
    if isinstance(file_path, str) and file_path and "\x00" not in file_path:
        absolute = file_path if file_path.startswith("/") else f"{workspace}/{file_path}"
        file_changed(context.control, session_id, absolute)


def _other(
    context: ApiContext, event: str, session: Mapping[str, Any], body: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    record: dict[str, Any] = {"event": event, "decision": "none"}
    tool = _tool(body)
    if tool is not None:
        record["tool"] = tool
    if body is None:
        record["reason"] = BODY_INVALID_REASON
    context.control.append("session.hook", record, session["id"])
    if event == "SubagentStart":
        context.control.append("guest.error", {"code": "subagent_hook_seen"}, session["id"])
    if event == "PostToolUse" and body is not None and body["tool_name"] in WRITE_TOOLS:
        _written(context, session["id"], session["workspace"], body)
    return {}, "none"


def _decide(
    context: ApiContext, event: str, session: Mapping[str, Any], body: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    if event == "PreToolUse":
        return _pre_tool_use(context, session["id"], body)
    if event == "UserPromptSubmit":
        return _user_prompt_submit(context, session["id"], body)
    return _other(context, event, session, body)


DECISION_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "Stop", "StopFailure"})
"""The hooks whose failure leaves Claude Code without a decision (or the turn without its end): a failure ends the
session (ADR-064 section 2)."""
TURN_EVENTS = frozenset({"Stop", "StopFailure"})
"""The hooks whose addon (``hook_addons/idle.py``) ends the turn: a failing addon is a failure of the hook."""
MONOTONIC: Callable[[], float] = time.monotonic
LATE_MARGIN_SECONDS = 1.0
"""An answer this close to Claude Code's hook timeout may arrive after it gave up (transport, its own clock): late."""


def _decorate(
    context: ApiContext, call: HookCall, answer: dict[str, Any], addons: Sequence[tuple[str, HookAddon]]
) -> dict[str, Any]:
    texts: list[str] = []
    for name, addon in addons:
        if call.event not in addon.events:
            continue
        try:
            text = addon.handle(context, call)
        except Exception:
            sys.stderr.write(f"agent-guest-api: hook addon {name} failed\n")
            traceback.print_exc(file=sys.stderr)
            _failure(context, call.session_id)
            if call.event in TURN_EVENTS:
                _hook_failed(context, call.session_id, call.event)
            continue
        if text is None:
            continue
        if not call.decorate or not isinstance(text, str):
            _failure(context, call.session_id)
            continue
        texts.append(text)
    if not texts:
        return answer
    output = dict(answer.get("hookSpecificOutput", {"hookEventName": call.event}))
    output["additionalContext"] = "\n\n".join(texts)
    decorated = {**answer, "hookSpecificOutput": output}
    if context.schemas.errors(f"mcp/hooks/{call.event}.response.json", decorated):
        _failure(context, call.session_id)
        return answer
    return decorated


def handle(context: ApiContext, request: Request, event: str, addons: Sequence[tuple[str, HookAddon]]) -> Response:
    started = MONOTONIC()
    token = bearer(request.headers)
    closed = closed_answer(event)
    try:
        session = context.control.resolve_session(
            token, peer.peer_uid(context.config.peer_table, request.server, request.peer)
        )
    except AuthLimited as error:
        STATS.count("unauthorized")
        return rate_limited(error.retry_after)
    except ApiSockError:
        STATS.count("errors")
        if closed is not None:
            return json_response(closed)
        raise HttpError("unavailable") from None
    if session is None:
        STATS.count("unauthorized")
        raise HttpError("unauthorized")
    session_id = session["id"]
    if fault_active(context.config):
        STATS.count("faults")
        _note(context, "session.hook", {"event": event, "decision": "none", "reason": FAULT_REASON}, session_id)
        _note(context, "guest.error", {"code": "hook_handler_failed"}, session_id)
        _hook_failed(context, session_id, event)
        return json_response({"error": "unavailable"}, FAULT_STATUS)
    body = _read_body(context, event, request)
    try:
        answer, decision = _decide(context, event, session, body)
    except Exception as error:
        if not isinstance(error, ApiSockError):
            sys.stderr.write(f"agent-guest-api: hook {event} failed\n")
            traceback.print_exc(file=sys.stderr)
        _failure(context, session_id)
        _hook_failed(context, session_id, event)
        if closed is not None:
            return json_response(closed)
        raise HttpError("unavailable") from None
    decorate = event in DECORATED_EVENTS and decision != "block"
    call = HookCall(event=event, session_id=session_id, body=body, decision=decision, decorate=decorate)
    answer = _decorate(context, call, answer, addons)
    if MONOTONIC() - started >= HOOK_TIMEOUT_SECONDS - LATE_MARGIN_SECONDS:
        # Claude Code waited no longer and went on without this answer (ADR-064 section 2)
        _failure(context, session_id)
        _hook_failed(context, session_id, event)
    return json_response(answer)


def routes(addons: Sequence[tuple[str, HookAddon]]) -> list[Route]:
    """One route per event; the body is read by the handler so that an unreadable body still gets an answer."""

    def route(event: str) -> Route:
        def handler(context: ApiContext, request: Request) -> Response:
            return handle(context, request, event, addons)

        return Route("POST", f"/hooks/{event}", handler, body="raw", max_body=ROUTE_MAX_BODY)

    return [route(event) for event in EVENTS]
