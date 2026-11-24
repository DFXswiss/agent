"""Control-side halves of the hook handlers (``api.sock`` ops, 05-runner.md sections 5.3 and 5.5).

The API process authenticates the session and parses the hook body; decisions that need root or the journal are
made here:

* op ``hook.pre_tool_use {session_id, tool_name, tool_input} -> {decision, rule, reason?}`` - evaluates
  ``policy.Policy`` for the session's workspace and harness file as root (the session directories are 0700, the API
  process could not see a symlink there) and journals ``session.hook {event: "PreToolUse", tool, decision, rule,
  reason?, path?}``. ``tool_input`` carries only the keys the policy reads (``file_path``, ``path``, ``pattern``,
  ``glob``). The runner turns a deny into the audit item ``hook.denied {tool, rule}`` (never the path).
  Rule 1a (KOR-39, ADR-076 section 3): for ``AskUserQuestion`` the API process adds ``question`` (built by
  ``policy.question_text``, 1-4000 characters). For a main session (depth 0 by the session's stored identity,
  ``session_depth``) the operation ``human.question {question}`` is requested for the session through the same
  pending path an MCP ``request_operation`` takes (``operation.request`` with a new ``operation_id``,
  ``local_check:"passed"``; the runner checks, seals and registers it as usual, nobody waits for its answer), then
  the call is denied with ``question_passed`` and journaled as rule 1 (no ``hook.denied`` audit: the operation is the
  record). A subagent's call is denied by rule 2 as before.
* op ``hook.post_tool_use {session_id, body}`` - journals ``session.tool`` for ``Write``/``Edit`` (05 section 5.5);
  other tools come from the transcript (GST-05)
* op ``file.changed {session_id, path}`` - the file hint ``file_changed(session, path)`` of PostToolUse
  ``Write``/``Edit``: the absolute path (lexically inside the session workspace) is queued in ``FILE_HINTS`` for
  the workspace scanner of GST-05, which reads the file as root and journals ``session.file``. At most
  ``MAX_HINTS`` paths per session are queued (the scanner's periodic pass finds the rest).

Unknown or inactive sessions answer ``not_found``; malformed arguments ``request_invalid``.
"""

from __future__ import annotations

import threading
from typing import Any

from agent_guest import sessions
from agent_guest.clock import iso_ms, uuid7
from agent_guest.control import ControlContext
from agent_guest.journal import JournalError
from agent_guest.policy import (
    ASK_TOOL,
    PATH_KEYS,
    PATTERN_KEYS,
    QUESTION_MAX,
    TOOL_NAME_RE,
    WRITE_TOOLS,
    Policy,
    harness_path,
    lexical,
)
from agent_guest.state import INACTIVE_STATES, Session, StateError

MAX_HINTS = 1024
MAX_JOURNAL_PATH = 4096
POLICY_KEYS = frozenset(PATH_KEYS) | frozenset(PATTERN_KEYS.values())
QUESTION_TYPE = "human.question"


class FileHints:
    """Paths written by Write/Edit, per session, in order and without repeats, until the scanner takes them."""

    def __init__(self, limit: int = MAX_HINTS) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._paths: dict[str, dict[str, None]] = {}

    def add(self, session_id: str, path: str) -> None:
        with self._lock:
            paths = self._paths.setdefault(session_id, {})
            if len(paths) < self.limit:
                paths[path] = None

    def take(self, session_id: str) -> list[str]:
        with self._lock:
            return list(self._paths.pop(session_id, {}))


FILE_HINTS = FileHints()


def _session(context: ControlContext, session_id: object) -> Session:
    session = context.state.session(session_id) if isinstance(session_id, str) else None
    if session is None or session.state in INACTIVE_STATES:
        raise StateError("not_found")
    return session


def _journal(context: ControlContext, kind: str, body: dict[str, Any], session_id: str) -> None:
    try:
        context.journal.append(kind, body, session_id)
    except JournalError as error:
        if error.code == "unavailable":
            raise
        raise JournalError("request_invalid", error.detail) from None


def session_depth(context: ControlContext, session_id: str) -> int | None:
    """The session's depth from its stored identity (0: main session), None when unknown."""
    return sessions.manager(context).depth(session_id)


def _question(args: dict[str, Any], tool_name: object) -> str | None:
    if "question" not in args:
        return None
    question = args["question"]
    if tool_name != ASK_TOOL or not isinstance(question, str) or not 1 <= len(question) <= QUESTION_MAX:
        raise StateError("request_invalid")
    return question


def _ask(context: ControlContext, session_id: str, question: str) -> None:
    """Request ``human.question`` for the session like ``request_operation`` does (without waiting)."""
    now = context.journal.clock.now()
    body = {
        "operation_id": uuid7(now),
        "type": QUESTION_TYPE,
        "params": {"question": question},
        "requested_at": iso_ms(now),
        "local_check": "passed",
    }
    try:
        context.pending.create(session_id, "operation", body)
    except JournalError as error:
        if error.code == "unavailable":
            raise
        raise JournalError("request_invalid", error.detail) from None


def pre_tool_use(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    tool_name, tool_input = args.get("tool_name"), args.get("tool_input")
    if (
        not {"session_id", "tool_name", "tool_input"}
        <= set(args)
        <= {"session_id", "tool_name", "tool_input", "question"}
        or not isinstance(tool_name, str)
        or not isinstance(tool_input, dict)
        or not set(tool_input) <= POLICY_KEYS
    ):
        raise StateError("request_invalid")
    question = _question(args, tool_name)
    session = _session(context, args["session_id"])
    ask = question is not None and session_depth(context, session.id) == 0
    decision = Policy(session.workspace, harness_path(session.id)).evaluate(tool_name, tool_input, ask=ask)
    if ask:
        assert question is not None
        _ask(context, session.id, question)
    record: dict[str, Any] = {"event": "PreToolUse", "decision": decision.decision, "rule": decision.rule}
    if TOOL_NAME_RE.fullmatch(tool_name):
        record["tool"] = tool_name
    if decision.reason is not None:
        record["reason"] = decision.reason
    if decision.path is not None and len(decision.path) <= MAX_JOURNAL_PATH:
        record["path"] = decision.path
    _journal(context, "session.hook", record, session.id)
    answer: dict[str, Any] = {"decision": decision.decision, "rule": decision.rule}
    if decision.reason is not None:
        answer["reason"] = decision.reason
    return answer


def post_tool_use(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    body = args.get("body")
    if set(args) != {"session_id", "body"} or not isinstance(body, dict) or body.get("name") not in WRITE_TOOLS:
        raise StateError("request_invalid")
    session = _session(context, args["session_id"])
    _journal(context, "session.tool", body, session.id)
    return {}


def file_changed(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    if (
        set(args) != {"session_id", "path"}
        or not isinstance(path, str)
        or not path.startswith("/")
        or "\x00" in path
        or len(path) > MAX_JOURNAL_PATH
    ):
        raise StateError("request_invalid")
    session = _session(context, args["session_id"])
    if not lexical(path).startswith(session.workspace.rstrip("/") + "/"):
        raise StateError("request_invalid")
    FILE_HINTS.add(session.id, path)
    return {}


API_OPS = {"hook.pre_tool_use": pre_tool_use, "hook.post_tool_use": post_tool_use, "file.changed": file_changed}
