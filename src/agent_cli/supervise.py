"""Static supervise loop over one assigned work item.

The model never chooses the next state. The shipped CLI follow loop
(`ask=False`) watches the pane, confirms a tool-approval modal, and pages
Telegram only when the Grok tmux session is gone. Locked closed questions
and acks exist for `tick(..., ask=True)` (tests).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .grok_pane import grok_pane_is_working, grok_permission_prompt, strip_ansi
from .telegram_act import TELEGRAM_IDLE_TICKS
from .runtime import Completed, Runtime
from .store import Store, StoreError, utcnow
from .watch import (
    _ensure_assigned_session,
    _issue_number,
    _paired_login,
    _policy_admits,
    assigned_workspace_root,
    dispatch_assigned,
    pending_assigned,
)

ANSWER_YES = "Ja"
ANSWER_NO = "Nein"
ANSWER_CAN = "ich kann es eigenständig fertigstellen"
ANSWER_BLOCKED = (
    "es gibt ein Problem, ich kann diese Sache nicht eigenständig fertig stellen"
)
ALLOWED_ANSWERS = (ANSWER_YES, ANSWER_NO, ANSWER_CAN, ANSWER_BLOCKED)
QUESTION_DONE = 'Bist du fertig? Antworte ausschliesslich mit "Ja" oder "Nein"'
QUESTION_WHY = (
    "Warum bist du nicht fertig? Fehlt dir etwas um es fertig zu stellen? "
    'Antworte entweder mit: "ich kann es eigenständig fertigstellen" oder mit '
    '"es gibt ein Problem, ich kann diese Sache nicht eigenständig fertig stellen"'
)
CONTINUE_TEXT = "Setze die Arbeit fort."
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
EXCERPT_MAX = 500
FOLLOW_SECONDS = 60
QUIET_SECONDS = 120
LAST_WORKING_KEY = "supervise_last_working_at"
STREAK_KEY = "supervise_idle_streak"


_CHROME = (
    "Worked for",
    "Shift+Tab",
    "Help improve",
    "Enter:send",
    "Esc:cancel",
    "Ctrl+x",
    "Off by default",
    "Read Terms",
    "always-approve",
    "Grok 4.6",
)


def parse_closed_answer(pane: str) -> str | None:
    """Only the newest content line counts. Scrollback 'Ja' is not an answer."""
    for raw in reversed(pane.splitlines()):
        text = strip_ansi(raw).strip().strip('"').strip("'")
        if text == "":
            continue
        if text.startswith("│") or text.startswith("╰") or text.startswith("╭"):
            continue
        if any(text.startswith(p) or p in text for p in _CHROME):
            continue
        if text in ALLOWED_ANSWERS:
            return text
        return None
    return None


def latest_supervise(
    store: Store, session_id: str, assigned_id: str
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_t = -1
    for row in store.rows("activity"):
        if row.get("type") != "supervise.event":
            continue
        if row.get("session_id") != session_id:
            continue
        if row.get("_origin_device_id") != store.device_id():
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("assigned_id") != assigned_id:
            continue
        raw_t = payload.get("t")
        t = raw_t if isinstance(raw_t, int) and not isinstance(raw_t, bool) else 0
        if t >= best_t:
            best_t = t
            best = row
    return best


def _log(
    store: Store,
    session_id: str,
    assigned_id: str,
    *,
    kind: str,
    phase: str,
    question: str | None = None,
    answer: str | None = None,
    repo: str | None = None,
    number: int | None = None,
    excerpt: str | None = None,
) -> str:
    activity_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "kind": kind,
        "phase": phase,
        "assigned_id": assigned_id,
        "t": time.time_ns(),
    }
    if question is not None:
        payload["question"] = question
    if answer is not None:
        payload["answer"] = answer
    if repo is not None:
        payload["repo"] = repo
    if number is not None:
        payload["number"] = number
    if excerpt is not None:
        payload["excerpt"] = excerpt[:EXCERPT_MAX]
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "supervise.event",
            "payload": payload,
            "execution_status": "done",
        },
    )
    return activity_id


def _send(runtime: Runtime, session_id: str, text: str) -> None:
    runtime.input_text(session_id, text)
    runtime.input_key(session_id, "enter")


def _item_ref(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    repo = payload.get("repo")
    repo_s = repo if isinstance(repo, str) and repo else None
    number = _issue_number(payload.get("number"))
    return repo_s, number


def _ack(store: Store, session_id: str, assigned_id: str) -> None:
    pending = pending_assigned(store, session_id)
    head = pending[0].get("id") if pending else None
    if head != assigned_id:
        raise StoreError("supervise ack assigned_id must be the queue head")
    ack_id = str(uuid.uuid4())
    store.write(
        "activity",
        "insert",
        ack_id,
        {
            "id": ack_id,
            "session_id": session_id,
            "type": "issue.assigned.ack",
            "payload": {"assigned_id": assigned_id},
            "execution_status": "done",
        },
    )


def pending_repo_number(store: Store, session_id: str, repo: str, number: int) -> str | None:
    repo_key = repo.lower()
    for row in pending_assigned(store, session_id):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        raw_repo = payload.get("repo")
        if not isinstance(raw_repo, str) or raw_repo.lower() != repo_key:
            continue
        if _issue_number(payload.get("number")) != number:
            continue
        aid = row.get("id")
        if isinstance(aid, str) and aid:
            return aid
    return None


def enqueue_assigned(
    store: Store,
    session_id: str,
    repo: str,
    number: int,
    runner: Callable[[list[str]], Completed],
) -> str:
    if REPO_RE.match(repo) is None:
        raise StoreError("repo must be OWNER/REPO")
    if number < 1:
        raise StoreError("number must be a positive integer")
    existing = pending_repo_number(store, session_id, repo, number)
    if existing is not None:
        return existing
    now = utcnow()
    assigned_by = _paired_login(store, runner)
    _ensure_assigned_session(store, session_id, now)
    url = f"https://github.com/{repo}/issues/{number}"
    title = ""
    body = ""
    assignee = ""
    try:
        info = runner(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{number}",
                "--jq",
                "{title,body,html_url,assignee:.assignee.login}",
            ]
        )
        if info.returncode == 0 and info.stdout.strip():
            data = json.loads(info.stdout)
            if isinstance(data, dict):
                if isinstance(data.get("html_url"), str) and data["html_url"]:
                    url = data["html_url"]
                if isinstance(data.get("title"), str):
                    title = data["title"]
                if isinstance(data.get("body"), str):
                    body = data["body"]
                if isinstance(data.get("assignee"), str):
                    assignee = data["assignee"]
    except (OSError, json.JSONDecodeError):
        pass
    activity_id = str(uuid.uuid4())
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "issue.assigned",
            "payload": {
                "repo": repo,
                "number": number,
                "url": url,
                "title": title,
                "body": body,
                "assigned_at": now,
                "assigned_by": assigned_by,
                "assignee": assignee,
                "mandate": "github-assignment",
            },
            "execution_status": "done",
        },
    )
    return activity_id


def _mark_working(store: Store, now: float) -> None:
    store.sync_set(LAST_WORKING_KEY, str(now))
    store.sync_set(STREAK_KEY, "0")


def idle_streak(store: Store) -> int:
    raw = store.sync_get(STREAK_KEY)
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _bump_idle_streak(store: Store) -> int:
    n = idle_streak(store) + 1
    store.sync_set(STREAK_KEY, str(n))
    return n


def _in_quiet(store: Store, now: float, quiet_seconds: int) -> bool:
    if quiet_seconds <= 0:
        return False
    raw = store.sync_get(LAST_WORKING_KEY)
    if raw is None or raw == "":
        return False
    try:
        last = float(raw)
    except ValueError:
        return False
    return now - last < quiet_seconds


def tick(
    store: Store,
    runtime: Runtime,
    session_id: str,
    *,
    start: Callable[[str, Path], None],
    knock: Callable[[str], Any],
    workspace_root: Path | None = None,
    quiet_seconds: int = QUIET_SECONDS,
    now: float | None = None,
    ask: bool = False,
    pane: str | None = None,
    working: bool | None = None,
    runner: Callable[[list[str]], Completed] | None = None,
) -> str:
    if SESSION_RE.match(session_id) is None:
        raise StoreError("session id may contain only A-Za-z0-9_-")
    clock = time.time() if now is None else now
    pending = pending_assigned(store, session_id)
    if not pending:
        return "supervise idle"
    head = pending[0]
    assigned_id = head.get("id")
    if not isinstance(assigned_id, str) or assigned_id == "":
        raise StoreError("queue head is missing an id")
    payload = head.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    repo, number = _item_ref(payload)
    root = workspace_root if workspace_root is not None else assigned_workspace_root(store)
    last = latest_supervise(store, session_id, assigned_id)
    last_kind = None
    last_phase = None
    last_answer = None
    if last is not None:
        last_payload = last.get("payload")
        if isinstance(last_payload, dict):
            last_kind = last_payload.get("kind")
            last_phase = last_payload.get("phase")
            last_answer = last_payload.get("answer")
    pane_missing = not runtime.exists(session_id)
    if last_kind is None and not pane_missing:
        if not _policy_admits(store, head, runner):
            return f"supervise denied assigned={assigned_id}"
        _log(
            store,
            session_id,
            assigned_id,
            kind="commission",
            phase="work",
            repo=repo,
            number=number,
        )
        _mark_working(store, clock)
        return f"supervise commission assigned={assigned_id} dispatch=held"
    if last_kind is not None and pane_missing:
        return f"supervise missing assigned={assigned_id}"
    if last_kind is None and pane_missing:
        dispatched = dispatch_assigned(
            store,
            assigned_id,
            sync=lambda: None,
            start=start,
            knock=knock,
            workspace_root=root,
            pane_up=lambda sid: runtime.exists(sid),
            runner=runner,
        )
        if dispatched == "denied":
            return f"supervise denied assigned={assigned_id}"
    else:
        dispatched = "held"
    if last_kind is None:
        _log(
            store,
            session_id,
            assigned_id,
            kind="commission",
            phase="work",
            repo=repo,
            number=number,
        )
        _mark_working(store, clock)
        return f"supervise commission assigned={assigned_id} dispatch={dispatched}"
    if pane is None:
        pane = runtime.capture(session_id) if runtime.exists(session_id) else ""
    if grok_permission_prompt(pane):
        runtime.input_key(session_id, "enter")
        _log(
            store,
            session_id,
            assigned_id,
            kind="approve",
            phase="work",
            repo=repo,
            number=number,
        )
        _mark_working(store, clock)
        return f"supervise approve assigned={assigned_id}"
    busy = working if working is not None else (
        grok_pane_is_working(pane) or runtime.is_busy(session_id)
    )
    if busy:
        _mark_working(store, clock)
        return f"supervise busy assigned={assigned_id}"
    if ask:
        if _in_quiet(store, clock, quiet_seconds):
            return f"supervise quiet assigned={assigned_id}"
    else:
        streak = _bump_idle_streak(store)
        if streak < TELEGRAM_IDLE_TICKS:
            return f"supervise quiet assigned={assigned_id} streak={streak}"
        return f"supervise stalled assigned={assigned_id} streak={streak}"
    answer = parse_closed_answer(pane)
    if last_kind == "ask" and answer is not None:
        _log(
            store,
            session_id,
            assigned_id,
            kind="answer",
            phase=str(last_phase or ""),
            answer=answer,
            repo=repo,
            number=number,
        )
        if last_phase == "done" and answer == ANSWER_YES:
            _ack(store, session_id, assigned_id)
            return f"supervise done assigned={assigned_id}"
        if last_phase == "done" and answer == ANSWER_NO:
            _send(runtime, session_id, QUESTION_WHY)
            _log(
                store,
                session_id,
                assigned_id,
                kind="ask",
                phase="why",
                question=QUESTION_WHY,
                repo=repo,
                number=number,
            )
            return f"supervise ask phase=why assigned={assigned_id}"
        if last_phase == "why" and answer == ANSWER_CAN:
            _send(runtime, session_id, CONTINUE_TEXT)
            _log(
                store,
                session_id,
                assigned_id,
                kind="continue",
                phase="work",
                repo=repo,
                number=number,
            )
            return f"supervise continue assigned={assigned_id}"
        if last_phase == "why" and answer == ANSWER_BLOCKED:
            _log(
                store,
                session_id,
                assigned_id,
                kind="skip",
                phase="why",
                answer=answer,
                excerpt=pane,
                repo=repo,
                number=number,
            )
            _ack(store, session_id, assigned_id)
            return f"supervise skip assigned={assigned_id}"
        return f"supervise answer ignored assigned={assigned_id}"
    if last_kind == "continue":
        _send(runtime, session_id, QUESTION_DONE)
        _log(
            store,
            session_id,
            assigned_id,
            kind="ask",
            phase="done",
            question=QUESTION_DONE,
            repo=repo,
            number=number,
        )
        return f"supervise ask phase=done assigned={assigned_id}"
    if last_kind in ("commission", "answer") or (
        last_kind == "ask" and answer is None
    ):
        if last_kind == "ask" and last_phase == "why":
            _send(runtime, session_id, QUESTION_WHY)
            _log(
                store,
                session_id,
                assigned_id,
                kind="ask",
                phase="why",
                question=QUESTION_WHY,
                repo=repo,
                number=number,
            )
            return f"supervise ask phase=why assigned={assigned_id}"
        _send(runtime, session_id, QUESTION_DONE)
        _log(
            store,
            session_id,
            assigned_id,
            kind="ask",
            phase="done",
            question=QUESTION_DONE,
            repo=repo,
            number=number,
        )
        return f"supervise ask phase=done assigned={assigned_id}"
    return f"supervise wait assigned={assigned_id} last={last_kind}/{last_phase}/{last_answer}"
