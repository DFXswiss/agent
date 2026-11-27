"""PreToolUse policy: deterministic, formal checks of every tool call (05-runner.md sections 5.3 and 4.6 no. 2).

Rules in this order; the first match decides (rule numbers as in 05 section 5.3, deny texts of
``contracts/mcp/hook-deny-reasons.json``):

1. ``mcp__platform__<tool>`` with ``<tool>`` in the tool list of 03-api.md section 8.1 -> allow; also
   ``mcp__orchestrator__search`` and ``mcp__orchestrator__run``, the agent tool's server of the main orchestrator's
   session (05-runner.md section 17.5; only that session has the server, ``/mcp-orchestrator`` refuses every other)
1a. ``AskUserQuestion`` of a main session whose question could be built (``ask``) -> deny ``question_passed``
   (journaled as rule 1); the caller requests the operation ``human.question`` with ``question_text(tool_input)``
   before it answers (KOR-39, ADR-076 section 3). A subagent's call, or one without any question text, falls
   through to rule 2
1b. ``WebSearch`` of a session whose model access offers Claude Code's own web search (``web_search``: access
   ``api_key`` or ``subscription``, 13-work-vm.md section 13.7) -> allow (journaled as rule 1); for the platform
   access it falls through to rule 2. The search runs at the model provider, not in the sandbox; ``WebFetch`` stays
   denied
2. a tool other than ``Read``, ``Glob``, ``Grep``, ``Edit``, ``Write`` -> deny ``tool_forbidden``
3. ``Glob.pattern`` / ``Grep.glob`` that starts with ``/`` or ``~``, has a ``..`` segment or a NUL -> deny
   ``pattern_outside_workspace``; brace alternatives (``{/etc,x}``, ``{..,x}``) and backslash escapes (``\\.\\.``)
   count as well
4. the path (``file_path`` and ``path``, each when present; default the workspace) is resolved; a value that is not a
   non-empty string, starts with ``~``, contains a NUL or cannot be resolved -> deny ``path_unresolvable``
5. ``Write``/``Edit``: every candidate path lies strictly inside the workspace and no segment below the workspace is
   protected -> else deny ``write_outside_workspace`` / ``write_protected``
6. ``Read``/``Glob``/``Grep``: every candidate path lies inside the workspace or equals the own harness file -> else
   deny ``read_outside_workspace``
7. allow

Path candidates. Claude Code may hand the path to the kernel unchanged (physical resolution) or normalise it
lexically first (Node ``path.resolve``) and let the kernel follow symlinks afterwards. Both readings must stay inside,
so rules 5 and 6 check three candidates: the lexical normalisation, ``resolve(path)`` and
``resolve(lexical(path))``. ``resolve`` follows symlinks component by component like ``os.path.realpath`` (at most
40 links); a component that does not exist ends the physical part and the rest is appended (the deepest existing
parent decides, 05 section 4.6 no. 2); any other error (permission, loop, a file used as a directory, a name too
long) makes the path unresolvable. An existing non-directory with more than one hard link is treated as outside
(its other names cannot be found, ATK-35).

Protected names (05 section 5.3: ``*/.claude/*``, ``*/.claude.json``, ``*/.mcp.json``, ``*/CLAUDE.md``, ``*/.git/*``,
``*/.gitconfig``; ADR-045 adds ``*/CLAUDE.local.md`` and ``*/AGENTS.md``) are compared case-insensitively on every
segment below the workspace: ``.claude`` and ``.git`` anywhere (also as the last segment), the file names anywhere.

The functions run in the control process (root): the API process (user ``platform``) cannot look into the session
directories (0700), so it could not see a symlink there.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

HARNESS_DIR = "/run/agent-guest/harness"
PLATFORM_PREFIX = "mcp__platform__"
PLATFORM_TOOLS = frozenset(
    {
        "get_context",
        "list_workgroups",
        "list_members",
        "list_tasks",
        "get_task",
        "get_task_messages",
        "get_whiteboard",
        "get_notifications",
        "request_operation",
        "get_operation",
        "list_operations",
        "get_usage",
        "list_audit",
        "list_workgroup_operations",
        "get_metrics",
        "get_subagent_result",
        "workvm_exec",
        "workvm_output",
        "workvm_kill",
        "workvm_status",
        "workvm_sync",
        "workvm_browse",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_hover",
        "browser_type",
        "browser_select",
        "browser_press",
        "browser_scroll",
        "browser_wait",
        "browser_evaluate",
        "browser_upload",
        "browser_dialog",
        "browser_tabs",
        "browser_download",
        "browser_screenshot",
        "dfx_debug",
    }
)
ORCHESTRATOR_TOOLS = frozenset({"mcp__orchestrator__search", "mcp__orchestrator__run"})
FILE_TOOLS = frozenset({"Read", "Glob", "Grep", "Edit", "Write"})
WEB_SEARCH_TOOL = "WebSearch"
# 13-work-vm.md section 13.7: model accesses whose provider runs Claude Code's own web search.
WEB_SEARCH_ACCESS = frozenset({"api_key", "subscription"})
WRITE_TOOLS = frozenset({"Edit", "Write"})
ASK_TOOL = "AskUserQuestion"
QUESTION_MAX = 4000
PATH_KEYS = ("file_path", "path")
PATTERN_KEYS = {"Glob": "pattern", "Grep": "glob"}
MAX_SYMLINKS = 40
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
PROTECTED_DIRS = frozenset({".claude", ".git"})
# `CLAUDE.local.md` and `AGENTS.md` are instruction files Claude Code 2.1.330 loads like `CLAUDE.md` (root, ancestors,
# nested directories); 05 section 5.3 lists only `CLAUDE.md` (ATS-03, ADR-045).
PROTECTED_FILES = frozenset({".claude.json", ".mcp.json", "claude.md", "claude.local.md", "agents.md", ".gitconfig"})
# Characters JavaScript's String.prototype.trim removes (Claude Code trims every path before using it).
JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_DOTDOT_RE = re.compile(r"(?:^|[/{,])\.\.(?:$|[/},])")
_BRACE_ABSOLUTE_RE = re.compile(r"[{,][/~]")

BLOCK_REASON = "Prompts are accepted only from agent.dfx.swiss."
TOOL_FORBIDDEN = (
    "Tool {name} is forbidden in agent.dfx.swiss sessions. Request actions through the platform tools "
    "(mcp__platform__request_operation)."
)
REASONS: dict[str, str] = {
    "question_passed": (
        "Your question was passed to the person through agent.dfx.swiss. End your turn now; the answer arrives as "
        "your next prompt."
    ),
    "tool_forbidden_unnamed": (
        "This tool is forbidden in agent.dfx.swiss sessions. Request actions through the platform tools "
        "(mcp__platform__request_operation)."
    ),
    "pattern_outside_workspace": (
        "Glob and Grep patterns must stay inside the workspace: no leading / or ~, no .. segment and no NUL character."
    ),
    "path_unresolvable": "The path could not be resolved inside the session workspace.",
    "input_invalid": "The tool call could not be checked and is denied.",
    "write_outside_workspace": "Write and Edit are allowed only inside the session workspace.",
    "write_protected": "This path is protected in agent.dfx.swiss sessions and cannot be written.",
    "read_outside_workspace": (
        "Read, Glob and Grep are allowed only inside the session workspace and the session harness file."
    ),
}


def deny_reason(reason_id: str, tool_name: str = "") -> str:
    """The fixed sentence of ``reason_id``; ``tool_forbidden`` names the tool only if the name is harmless."""
    if reason_id == "tool_forbidden":
        if TOOL_NAME_RE.fullmatch(tool_name):
            return TOOL_FORBIDDEN.format(name=tool_name)
        return REASONS["tool_forbidden_unnamed"]
    return REASONS[reason_id]


def _option(option: object) -> str | None:
    if isinstance(option, str):
        return option if option.strip() else None
    if not isinstance(option, Mapping):
        return None
    label, description = option.get("label"), option.get("description")
    label = label if isinstance(label, str) and label.strip() else None
    description = description if isinstance(description, str) and description.strip() else None
    if label is None:
        return description
    return label if description is None else f"{label}: {description}"


def question_text(tool_input: Mapping[str, object]) -> str:
    """The ``question`` of the ``human.question`` operation for an ``AskUserQuestion`` call (05 section 5.3 rule 1a):
    every question text followed by its options as a list (``- label: description``), questions separated by a blank
    line, NUL characters removed, cut to ``QUESTION_MAX`` characters; empty when the input holds no question text.
    Deterministic and formal: the input is only arranged, never interpreted."""
    blocks: list[str] = []
    questions = tool_input.get("questions")
    for item in questions if isinstance(questions, list) else []:
        if not isinstance(item, Mapping):
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        options = item.get("options")
        found = [_option(option) for option in (options if isinstance(options, list) else [])]
        blocks.append("\n".join([question, *(f"- {option}" for option in found if option is not None)]))
    return "\n\n".join(blocks).replace("\x00", "")[:QUESTION_MAX]


@dataclass(frozen=True)
class Decision:
    """Outcome of one PreToolUse check; ``path`` is the resolved path (journal only, never audited)."""

    decision: Literal["allow", "deny"]
    rule: int
    reason: str | None = None
    path: str | None = None


class Unresolvable(Exception):
    pass


def harness_path(session_id: str, directory: str = HARNESS_DIR) -> str:
    return f"{directory}/{session_id}.md"


def lexical(path: str) -> str:
    """Absolute path normalised by text only: empty and ``.`` segments dropped, ``..`` removes one (never above /)."""
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


Lstat = Callable[[str], os.stat_result]


def resolve(path: str, lstat: Lstat = os.lstat, readlink: Callable[[str], str] = os.readlink) -> str:
    """Physical resolution of the absolute ``path`` (symlinks followed); a missing tail is appended as text."""
    stack = list(reversed(path.split("/")))
    resolved = ""
    links = 0
    missing = False
    while stack:
        part = stack.pop()
        if part in ("", "."):
            continue
        if part == "..":
            resolved = resolved.rpartition("/")[0]
            missing = False
            continue
        candidate = f"{resolved}/{part}"
        if missing:
            resolved = candidate
            continue
        try:
            mode = lstat(candidate).st_mode
        except FileNotFoundError:
            missing = True
            resolved = candidate
            continue
        except (OSError, ValueError):
            # ValueError: a lone surrogate or NUL cannot become a file name (Claude Code would normalise it).
            raise Unresolvable(candidate) from None
        if not stat.S_ISLNK(mode):
            resolved = candidate
            continue
        links += 1
        if links > MAX_SYMLINKS:
            raise Unresolvable(candidate)
        try:
            target = readlink(candidate)
        except OSError:
            raise Unresolvable(candidate) from None
        if target.startswith("/"):
            resolved = ""
        stack.extend(reversed(target.split("/")))
    return resolved or "/"


def _inside(path: str, root: str, *, strict: bool) -> bool:
    if path == root:
        return not strict
    return path.startswith(root.rstrip("/") + "/")


def _protected(path: str, root: str) -> bool:
    segments = [segment.casefold() for segment in path[len(root) :].split("/") if segment]
    return any(segment in PROTECTED_DIRS or segment in PROTECTED_FILES for segment in segments)


def padded(value: str) -> bool:
    """``True`` if JavaScript ``trim()`` or Python ``strip()`` would change the text."""
    return value != value.strip(JS_WHITESPACE) or value != value.strip()


def _pattern_allowed(pattern: object) -> bool:
    if not isinstance(pattern, str) or "\x00" in pattern or padded(pattern):
        return False
    for text in (pattern, pattern.replace("\\", "")):
        if text.startswith(("/", "~")) or _BRACE_ABSOLUTE_RE.search(text) or _DOTDOT_RE.search(text):
            return False
    return True


def _hard_linked(path: str, lstat: Lstat) -> bool:
    try:
        info = lstat(path)
    except OSError:
        return False
    return not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1


class Policy:
    """The rules for one session: its workspace and its harness file (resolver injectable for tests)."""

    def __init__(
        self,
        workspace: str,
        harness: str,
        lstat: Lstat = os.lstat,
        readlink: Callable[[str], str] = os.readlink,
    ) -> None:
        self.workspace = workspace
        self.harness = harness
        self._lstat = lstat
        self._readlink = readlink

    def _resolve(self, path: str) -> str:
        return resolve(path, self._lstat, self._readlink)

    def evaluate(
        self, tool_name: str, tool_input: Mapping[str, object], *, ask: bool = False, web_search: bool = False
    ) -> Decision:
        """The decision for one call; ``ask``: the call is ``AskUserQuestion`` of a main session with a question;
        ``web_search``: the session's model access offers ``WebSearch`` (rule 1b)."""
        platform = tool_name.startswith(PLATFORM_PREFIX) and tool_name[len(PLATFORM_PREFIX) :] in PLATFORM_TOOLS
        if platform or tool_name in ORCHESTRATOR_TOOLS or (web_search and tool_name == WEB_SEARCH_TOOL):
            return Decision("allow", 1)
        if ask and tool_name == ASK_TOOL:
            return Decision("deny", 1, deny_reason("question_passed"))
        if tool_name not in FILE_TOOLS:
            return Decision("deny", 2, deny_reason("tool_forbidden", tool_name))
        key = PATTERN_KEYS.get(tool_name)
        if key is not None and key in tool_input and not _pattern_allowed(tool_input[key]):
            return Decision("deny", 3, deny_reason("pattern_outside_workspace"))
        try:
            return self._paths(tool_name, tool_input)
        except Unresolvable:
            return Decision("deny", 4, deny_reason("path_unresolvable"))

    def _paths(self, tool_name: str, tool_input: Mapping[str, object]) -> Decision:
        """Rules 4 to 7 for every path of the call; ``Unresolvable`` is rule 4."""
        root = self._resolve(self.workspace)
        harness = {lexical(self.harness), self._resolve(self.harness)}
        values = [tool_input[key] for key in PATH_KEYS if key in tool_input] or [self.workspace]
        first: str | None = None
        for value in values:
            if not isinstance(value, str) or not value or "\x00" in value or value.startswith("~") or padded(value):
                raise Unresolvable("path")
            joined = value if value.startswith("/") else f"{self.workspace}/{value}"
            physical = self._resolve(joined)
            candidates = (lexical(joined), physical, self._resolve(lexical(joined)))
            first = first or physical
            denied = self._check(tool_name, candidates, root, harness)
            if denied is not None:
                return Decision("deny", denied[0], deny_reason(denied[1]), physical)
        return Decision("allow", 7, path=first)

    def _linked(self, candidates: tuple[str, str, str]) -> bool:
        return any(_hard_linked(path, self._lstat) for path in candidates[1:])

    def _check(
        self, tool_name: str, candidates: tuple[str, str, str], root: str, harness: set[str]
    ) -> tuple[int, str] | None:
        if tool_name in WRITE_TOOLS:
            if not all(_inside(path, root, strict=True) for path in candidates) or self._linked(candidates):
                return 5, "write_outside_workspace"
            if any(_protected(path, root) for path in candidates):
                return 5, "write_protected"
            return None
        if all(path in harness for path in candidates):
            return None
        if not all(_inside(path, root, strict=False) for path in candidates) or self._linked(candidates):
            return 6, "read_outside_workspace"
        return None
