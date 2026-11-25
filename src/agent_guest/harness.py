"""The harness of a session (05-runner.md section 8): fixed rules plus a generated context block.

The control process renders ``/run/agent-guest/harness/<session_id>.md`` (root, 0444, directory 0711) = the static
rules ``/opt/agent-guest/harness/RULES.md`` + the context of this session; Claude Code gets it through
``--append-system-prompt-file``. ``harness_version`` is the SHA-256 hex of ``RULES.md`` (journaled in
``session.state``).

The file reaches the model only when a conversation starts. Claude Code (2.1.330) keeps the system prompt of a
conversation in its transcript and reuses it on ``--resume``, ignoring a new ``--append-system-prompt-file``: a
resumed or restored conversation still sees the facts of its first start (KOR-33 E2E). What changed since then
reaches it as a session notice instead (``notice``; ``sessions.py`` keeps it pending in the session's root-only meta
file, ``hook_addons/notice.py`` attaches it once to the first decorated hook answer): the sentence about what
changed and every fact line (``fact_lines``) the conversation does not know yet.

The context block has a fixed structure (decisions/GST-02.md):

1. ``# Context of this session``
2. the runner's ``harness.context_markdown`` of ``POST /v1/sessions`` verbatim: the confidential part the guest cannot
   know (organization, workgroup and task names and IDs, task owner display name, rights, subagent limits, the task
   owner's GitHub connection with the rule for GitHub tasks, KOR-25); RUN-04 builds it with ``context_markdown(...)``
   of this module
3. ``## Session``: what the guest knows itself (session and task ID, parent session and depth, model, resource limits)
4. ``## Platform tools``: every MCP tool of 03-api.md section 8.1 with one example call
5. ``## Restarted session`` (only for a restarted session): read the history with ``get_task_messages`` - written
   into the file of a session started with ``--resume``, which Claude Code does not read (see below); a resumed
   session learns what changed from the session notice

Interface:

* ``SessionFacts`` - the guest-side facts of block 3 and 5
* ``load_rules(path=RULES_FILE) -> Rules`` - text and ``harness_version``
* ``render(rules, facts, context_markdown) -> str``
* ``context_markdown(org, workgroup, task, owner, rights, limits, github=None) -> str`` - block 2 in its documented
  form, every name collapsed to one line (``inline``)
* ``write(path, text)`` - atomic write, root 0444, directory 0711
* ``TOOLS`` - ``(name, purpose, example arguments)`` of every platform tool
* ``fact_lines(facts, context_markdown) -> list[str]`` - the fact lines of blocks 2 and 3 (headings and blank lines
  left out)
* ``notice(sentences, current, known) -> str`` - the session notice: ``NOTICE_PREFIX``, the sentences, and the lines
  of ``current`` that are not in ``known`` (all of them when ``known`` is None: the conversation's facts are unknown)
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RULES_FILE = "/opt/agent-guest/harness/RULES.md"
FILE_MODE = 0o444
DIR_MODE = 0o711
MAX_CONTEXT_CHARS = 65536
GITHUB_CONNECTED = "- GitHub of the task owner: connected as {login}"
GITHUB_NOT_CONNECTED = (
    "- GitHub of the task owner: not connected. If the task needs GitHub, request the GitHub operation anyway; the"
    " platform asks the task owner to connect and then continues it. Do not explain what is missing and do not offer"
    " workarounds."
)
NOTICE_PREFIX = "agent.dfx.swiss session notice: "
"""Start of a session notice (``contracts/mcp/objects.json#/$defs/session_notice``)."""
FACTS_HEADING = "Current facts of this session (they replace the ones in your instructions):"
CHANGED_SENTENCE = "Facts of this session changed since your instructions were written."
"""The sentence of a notice that has changed facts but no sentence of its own."""

TOOLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("get_context", "organization, workgroup, task, session, your rights and limits", {}),
    ("list_workgroups", "the workgroups of the organization", {}),
    ("list_members", "the members of the organization or of the workgroup", {"scope": "workgroup"}),
    (
        "list_tasks",
        "the tasks of the workgroup (or of another workgroup you may read, with `workgroup_id`)",
        {"state": "working", "limit": 20},
    ),
    ("get_task", "one task with its sessions and operations", {"task_id": "<task id>"}),
    ("get_task_messages", "the chat of a task (prompts and answers)", {"task_id": "<task id>", "limit": 50}),
    ("get_whiteboard", "the whiteboard of the organization, workgroup or task", {"level": "workgroup"}),
    ("get_notifications", "the pending platform notices (marks them delivered)", {}),
    (
        "request_operation",
        "every other action, checked and executed by the platform",
        {"type": "task.set_title", "params": {"title": "Short task title"}},
    ),
    ("get_operation", "the state of an operation", {"operation_id": "<operation id>", "wait_seconds": 10}),
    ("list_operations", "the operations of this task", {"limit": 20}),
    ("get_subagent_result", "the result of a subagent session you requested", {"session_id": "<session id>"}),
    (
        "workvm_exec",
        "run a command as root in your work VM (workspace mirrored to /workspace first)",
        {"command": "npm ci && npm test", "wait_seconds": 30},
    ),
    ("workvm_output", "follow the output of a running job", {"job_id": "j-0123456789ab", "wait_seconds": 30}),
    ("workvm_kill", "stop a job of your work VM", {"job_id": "j-0123456789ab"}),
    ("workvm_status", "your work VM and its last jobs", {}),
    ("workvm_sync", "bring files of the work VM back into your workspace", {"direction": "from_vm", "path": "out"}),
    (
        "workvm_browse",
        "open a web page in the Chromium of your work VM (read, screenshot, click, fill)",
        {"url": "https://example.com/", "action": "screenshot"},
    ),
    (
        "browser_navigate",
        "open a page in the platform browser (recorded, watched live; opens it on first use)",
        {"url": "https://example.com/"},
    ),
    ("browser_snapshot", "the page of the platform browser with fresh refs", {}),
    ("browser_click", "click an element (ref or selector) or a viewport point", {"target": "e12"}),
    ("browser_hover", "move the mouse over an element", {"target": "e12"}),
    ("browser_type", "fill a field, optionally submit", {"target": "e3", "text": "user@example.com", "submit": False}),
    ("browser_select", "choose options of a select", {"target": "e5", "values": ["CH"]}),
    ("browser_press", "press a key or chord", {"key": "Enter"}),
    ("browser_scroll", "scroll the page or an element into view", {"dy": 600}),
    (
        "browser_wait",
        "wait for text, its absence, a URL or some milliseconds",
        {"text": "Signed in", "timeout_ms": 10000},
    ),
    ("browser_evaluate", "the JSON value of a JavaScript expression in the page", {"expression": "document.title"}),
    ("browser_upload", "workspace files into a file input", {"target": "e7", "paths": ["out/report.pdf"]}),
    ("browser_dialog", "how the next dialogs are answered", {"action": "dismiss"}),
    ("browser_tabs", "list, open, select or close tabs", {"action": "list"}),
    ("browser_download", "download a file into the workspace", {"target": "e9"}),
    ("browser_screenshot", "a PNG of the page into the workspace (view it with Read)", {"full_page": True}),
)


class HarnessError(ValueError):
    """The rules file is missing or unreadable, or the context is unusable."""


@dataclass(frozen=True)
class Rules:
    text: str
    version: str


@dataclass(frozen=True)
class SessionFacts:
    session_id: str
    task_id: str
    parent_session_id: str | None
    depth: int
    model_id: str | None
    memory_max: int
    cpu_quota: int
    tasks_max: int
    runtime_max_sec: int
    restarted: bool = False


def load_rules(path: str = RULES_FILE) -> Rules:
    try:
        data = Path(path).read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise HarnessError(f"rules unreadable: {path}") from None
    if not text.strip():
        raise HarnessError(f"rules empty: {path}")
    return Rules(text=text, version=hashlib.sha256(data).hexdigest())


def _example(name: str, arguments: dict[str, Any]) -> str:
    return f"`mcp__platform__{name}` with `{json.dumps(arguments, separators=(',', ':'))}`"


def _session_block(facts: SessionFacts) -> list[str]:
    parent = (
        "none (main session of the task)"
        if facts.parent_session_id is None
        else f"`{facts.parent_session_id}` (you are a subagent session, depth {facts.depth})"
    )
    return [
        "## Session",
        "",
        f"- Session ID: `{facts.session_id}`",
        f"- Task ID: `{facts.task_id}`",
        f"- Parent session: {parent}",
        f"- Model: `{facts.model_id}`" if facts.model_id is not None else "- Model: platform default",
        f"- Resource limits: memory {facts.memory_max} bytes, CPU {facts.cpu_quota} % of one core, "
        f"{facts.tasks_max} tasks, runtime {facts.runtime_max_sec} s",
    ]


def render(rules: Rules, facts: SessionFacts, context_markdown: str) -> str:
    """``RULES.md`` + the context block of this session (structure in the module docstring)."""
    context = context_markdown.strip()
    if not context or len(context_markdown) > MAX_CONTEXT_CHARS or "\x00" in context:
        raise HarnessError("context_markdown")
    lines = [rules.text.rstrip("\n"), "", "# Context of this session", "", context, ""]
    lines += _session_block(facts)
    lines += ["", "## Platform tools", ""]
    lines += [f"- {purpose}: {_example(name, arguments)}" for name, purpose, arguments in TOOLS]
    if facts.restarted:
        example = _example("get_task_messages", {"task_id": facts.task_id})
        lines += [
            "",
            "## Restarted session",
            "",
            f"This session was restarted. Read the earlier messages of the task with {example} before you continue.",
        ]
    return "\n".join(lines) + "\n"


def fact_lines(facts: SessionFacts, context_markdown: str) -> list[str]:
    """The facts the harness tells the model (blocks 2 and 3 of the module docstring), one per line; headings and
    blank lines are left out."""
    lines = [*context_markdown.strip().splitlines(), *_session_block(facts)]
    return [line.rstrip() for line in lines if line.strip() and not line.startswith("#")]


def notice(sentences: Sequence[str], current: Sequence[str], known: Sequence[str] | None) -> str:
    """The session notice (see the module docstring); every line of ``current`` when ``known`` is None."""
    changed = [line for line in current if known is None or line not in known]
    text = NOTICE_PREFIX + " ".join(sentences or [CHANGED_SENTENCE])
    if changed:
        text += "\n\n" + "\n".join([FACTS_HEADING, *changed])
    return text


def context_markdown(
    *,
    org: tuple[str, str],
    workgroup: tuple[str, str, bool],
    task: tuple[str, str],
    owner: str,
    rights: str,
    limits: tuple[int | None, int | None, int | None, int | None],
    github: str | None = None,
) -> str:
    """Block 2 as RUN-04 sends it: ``org=(id, name)``, ``workgroup=(id, name, is_default)``, ``task=(id, title)``,
    ``owner`` display name, ``rights`` (``read``/``write``), ``limits`` = the subagent settings of the platform
    (depth, running, total, runtime in seconds; 0 = no limit, None = unknown, ADR-088 section 2), ``github`` = the
    task owner's GitHub login (None: not connected, with the rule to request GitHub operations anyway)."""
    wg_id, wg_name, is_default = workgroup
    default = " (default workgroup)" if is_default else ""
    return "\n".join(
        [
            f"- Organization: {inline(org[1])} (`{inline(org[0])}`)",
            f"- Workgroup: {inline(wg_name)}{default} (`{inline(wg_id)}`)",
            f"- Task: {inline(task[1])} (`{inline(task[0])}`)",
            f"- Task owner: {inline(owner)}",
            f"- Rights of the task owner in the workgroup: {inline(rights)}",
            f"- Subagent limits (settings of the platform): {subagent_limits(limits)}",
            GITHUB_NOT_CONNECTED if github is None else GITHUB_CONNECTED.format(login=inline(github)),
        ]
    )


def subagent_limits(limits: tuple[int | None, int | None, int | None, int | None]) -> str:
    """The subagent limits in words: ``unknown`` when any is unknown, 0 is no limit."""
    depth, running, total, runtime = limits
    if depth is None or running is None or total is None or runtime is None:
        return "unknown"
    return ", ".join(
        [
            f"depth {depth}" if depth else "no depth limit",
            f"{running} running at a time" if running else "no limit of running subagents",
            f"{total} per task" if total else "no limit per task",
            f"runtime at most {runtime} s" if runtime else "no runtime limit",
        ]
    )


def inline(value: str) -> str:
    """One line of text: names are user-chosen (a task title can be set by a session), so line breaks and other
    white space collapse to single spaces and a name can never start a heading or a rule of its own."""
    return " ".join(value.replace("`", "'").split())


def write(path: str, text: str) -> None:
    """Write ``text`` atomically as ``path`` (mode 0444); the directory is created 0711."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    os.chmod(directory, DIR_MODE)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".harness-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
