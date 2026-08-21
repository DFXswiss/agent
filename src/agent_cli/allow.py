"""Pure done-gate logic for the session-store spine.

No Postgres, no network, no subprocess. Hooks and the CLI call evaluate_allow
with a snapshot of the current session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACTIONS = ("claim-done", "pr-ready", "pr-create", "task-done")
# Ready may arrive before the last gate (draft CI); gate-blocked is not enough.
PR_READY_STATES = ("pushing", "pr-review")

CHECKLIST_KEYS: dict[str, tuple[str, ...]] = {
    "implement": (
        "session_registered",
        "spec_written",
        "implementer_done",
        "reviewer_approved",
        "local_check_pass",
        "pushed",
        "grok_pr_quality",
        "grok_pr_logic",
        "codex_pr_quality",
        "codex_pr_logic",
        "contributing_ok",
        "deviation_declared",
        "deviation_granted",
    ),
    "review": (
        "session_registered",
        "contributing_read",
        "contributing_ok",
        "coverage_ok",
        "handbook_ok",
        "grok_pr_quality",
        "grok_pr_logic",
        "codex_pr_quality",
        "codex_pr_logic",
        "deviation_declared",
        "deviation_granted",
    ),
    "resolve-conflicts": (
        "session_registered",
        "conflicts_resolved",
        "reviewer_approved",
        "local_check_pass",
        "pushed",
        "grok_pr_quality",
        "grok_pr_logic",
        "codex_pr_quality",
        "codex_pr_logic",
        "mergeable",
    ),
}

N_A_ALLOWED = frozenset(
    {
        "coverage_ok",
        "handbook_ok",
        "contributing_read",
        "contributing_ok",
        "deviation_declared",
        "deviation_granted",
    }
)

GATE_PAIRS = (
    ("grok-pr", "quality", "grok"),
    ("grok-pr", "logic", "grok"),
    ("codex-pr", "quality", "codex"),
    ("codex-pr", "logic", "codex"),
)

# Every `ja` needs --evidence (all keys of all workflows).
JA_EVIDENCE_REQUIRED: frozenset[str] = frozenset(
    key for keys in CHECKLIST_KEYS.values() for key in keys
)

_TERMINAL_STATES = frozenset({"done", "failed"})


@dataclass(frozen=True)
class AllowResult:
    allowed: bool
    action: str
    session: str | None
    task_id: str | None
    reason: str
    blocking: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "session": self.session,
            "task_id": self.task_id,
            "reason": self.reason,
            "blocking": list(self.blocking),
        }


def requires_evidence(status: str, key: str) -> bool:
    """True when checklist set may only write this status with --evidence."""
    if status in ("n_a", "unavailable"):
        return True
    if status == "ja":
        return True
    return False


def _tid(task: dict[str, Any]) -> str:
    return str(task.get("id", "?"))


def _latest_gates(gates: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for g in gates:
        stage = g.get("stage")
        dim = g.get("dimension")
        if stage is None or dim is None:
            continue
        out[(str(stage), str(dim))] = g
    return out


def _latest_checks_by_name(checks: list[dict[str, Any]]) -> dict[str, str]:
    by_name: dict[str, str] = {}
    for c in checks:
        name = c.get("name")
        if not name:
            continue
        by_name[str(name)] = str(c.get("result") or "")
    return by_name


def ready_for_done_blocking(task: dict[str, Any]) -> list[str]:
    """Empty list = ready_for_done; else blocking entries "{tid}:{key}={status}"."""
    tid = _tid(task)
    workflow = task.get("workflow")
    required = CHECKLIST_KEYS.get(str(workflow) if workflow is not None else "")
    blocking: list[str] = []
    if not required:
        return [f"{tid}:workflow={workflow}"]

    checklist = task.get("checklist") or {}
    for key in required:
        st = checklist.get(key)
        if st is None:
            blocking.append(f"{tid}:{key}=missing")
            continue
        if st == "ja":
            continue
        if st == "n_a" and key in N_A_ALLOWED:
            continue
        blocking.append(f"{tid}:{key}={st}")

    summaries = task.get("summaries") or {}
    en = str(summaries.get("en") or "").strip()
    de = str(summaries.get("de") or "").strip()
    if not en or not de:
        blocking.append(f"{tid}:summary=missing")

    if checklist.get("deviation_declared") == "ja" and checklist.get("deviation_granted") != "ja":
        blocking.append(
            f"{tid}:deviation_granted={checklist.get('deviation_granted', 'missing')}"
        )

    by_name = _latest_checks_by_name(list(task.get("local_checks") or []))
    for name, result in by_name.items():
        if result == "fail":
            blocking.append(f"{tid}:local_check:{name}=fail")
    if checklist.get("local_check_pass") == "ja":
        ok = [r for r in by_name.values() if r in ("pass", "skip")]
        if not ok:
            blocking.append(f"{tid}:local_check_pass=ja_without_pass_skip")

    latest = _latest_gates(list(task.get("gates") or []))
    heads: set[str] = set()
    all_gates_ok = True
    for stage, dim, vendor in GATE_PAIRS:
        got = latest.get((stage, dim))
        if got is None:
            blocking.append(f"{tid}:gate:{stage}/{dim}=missing")
            all_gates_ok = False
            continue
        got_vendor = got.get("vendor")
        verdict = got.get("verdict")
        head = str(got.get("head_sha") or "")
        if got_vendor != vendor or verdict != "approved":
            blocking.append(f"{tid}:gate:{stage}/{dim}={got_vendor}/{verdict}")
            all_gates_ok = False
        if not head:
            blocking.append(f"{tid}:gate:{stage}/{dim}=no_head")
            all_gates_ok = False
        else:
            heads.add(head)
    if all_gates_ok and len(heads) != 1:
        blocking.append(f"{tid}:gate:head_sha=mismatch")

    return blocking


def _filter_session(
    session_id: str | None, session_tasks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if session_id is None:
        return list(session_tasks)
    sid = str(session_id)
    return [t for t in session_tasks if str(t.get("session_id")) == sid]


def _open_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in tasks if str(t.get("state")) not in _TERMINAL_STATES]


def evaluate_allow(
    action: str,
    *,
    session_id: str | None,
    task_id: str | None,
    session_tasks: list[dict[str, Any]],
    create_has_draft: bool = False,
) -> AllowResult:
    """Decide allow/deny from the passed session tasks only."""
    if action not in ACTIONS:
        return AllowResult(
            allowed=False,
            action=action,
            session=session_id,
            task_id=task_id,
            reason=f"unknown action, expected: {','.join(ACTIONS)}",
            blocking=[],
        )

    tasks = _filter_session(session_id, session_tasks)
    open_tasks = _open_tasks(tasks)

    if action == "claim-done":
        # No session / no open tasks: nothing to block.
        if not open_tasks:
            return AllowResult(
                allowed=True,
                action=action,
                session=session_id,
                task_id=task_id,
                reason="allow",
                blocking=[],
            )
        blocking: list[str] = []
        for t in open_tasks:
            blocking.extend(ready_for_done_blocking(t))
        if blocking:
            return AllowResult(
                allowed=False,
                action=action,
                session=session_id,
                task_id=task_id,
                reason="open tasks are not ready_for_done",
                blocking=blocking,
            )
        return AllowResult(
            allowed=True,
            action=action,
            session=session_id,
            task_id=task_id,
            reason="allow",
            blocking=[],
        )

    if action == "pr-ready":
        if not open_tasks:
            return AllowResult(
                allowed=False,
                action=action,
                session=session_id,
                task_id=task_id,
                reason="no open task in this session — run agent task first",
                blocking=[],
            )
        if any(str(t.get("state")) in PR_READY_STATES for t in open_tasks):
            return AllowResult(
                allowed=True,
                action=action,
                session=session_id,
                task_id=task_id,
                reason="allow",
                blocking=[],
            )
        states = sorted({str(t.get("state")) for t in open_tasks})
        return AllowResult(
            allowed=False,
            action=action,
            session=session_id,
            task_id=task_id,
            reason=f"no task in pushing/pr-review (states: {','.join(states)})",
            blocking=[f"{_tid(t)}:state={t.get('state')}" for t in open_tasks],
        )

    if action == "pr-create":
        if create_has_draft:
            return AllowResult(
                allowed=True,
                action=action,
                session=session_id,
                task_id=task_id,
                reason="allow",
                blocking=[],
            )
        return AllowResult(
            allowed=False,
            action=action,
            session=session_id,
            task_id=task_id,
            reason="gh pr create requires --draft",
            blocking=[],
        )

    # task-done
    if not task_id:
        return AllowResult(
            allowed=False,
            action=action,
            session=session_id,
            task_id=task_id,
            reason="task-done requires --task",
            blocking=[],
        )
    # Session set: only that session's tasks; else the full list.
    pool = tasks if session_id is not None else list(session_tasks)
    match = [t for t in pool if str(t.get("id")) == str(task_id)]
    if not match:
        return AllowResult(
            allowed=False,
            action=action,
            session=session_id,
            task_id=task_id,
            reason=f"task {task_id} not found in this session",
            blocking=[],
        )
    blocking = ready_for_done_blocking(match[0])
    if blocking:
        return AllowResult(
            allowed=False,
            action=action,
            session=session_id,
            task_id=task_id,
            reason="task is not ready_for_done",
            blocking=blocking,
        )
    return AllowResult(
        allowed=True,
        action=action,
        session=session_id,
        task_id=task_id,
        reason="allow",
        blocking=[],
    )
