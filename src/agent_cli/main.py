"""CLI for the local session store."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import socket
import sys
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.exceptions import WebSocketException

from .allow import ACTIONS, evaluate_allow, ready_for_done_blocking
from .chain import (
    NO_AUTO_CLOSE,
    close_allowed,
    handoff_prompt,
    next_steps,
    required_source,
    to_json as step_to_json,
)
from .github_act import _repo_ok
from .hub import Hub, HubError
from .knock import drain as knock_drain
from .knock import listen_once as knock_listen
from .lane import LANE_ROLES, LANE_VENDORS, launch
from .pg import PgError, ensure_cluster, require_loopback_dsn
from .runtime import (
    Runtime,
    grok_model,
    grok_new_session_id,
    grok_tmux_command_argv,
    run_argv,
    tmux_name,
)
from .skills import SKILL_NAMES, has_skill, skill_for_agent_role
from .store import Store, StoreConnectionError, StoreError, utcnow
from .usage import AuthStale, scan_usage, usage_poll_due
from .watch import (
    assigned_session_id,
    assigned_workspace_root,
    dispatch_assigned,
    pending_assigned,
    scan_assigned,
    scan_merged,
)

CHECKLIST = {
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
TASK_STATES = (
    "open",
    "implementing",
    "reviewing",
    "local-check",
    "pushing",
    "pr-review",
    "done",
    "failed",
)
PING_KINDS = ("review-request", "ping", "question")
ACTIVITY_TYPES = frozenset(
    {
        "session.register",
        "issue.write",
        "pr.open",
        "pr.merged",
        "issue.assigned",
        "issue.assigned.ack",
        "comment.post",
        "mail.ingest",
        "mail.seen",
        "mail.reply",
        "investigate.step",
        "error.seen",
        "error.skip",
        "error.fix",
        "message",
        "message.read",
        "query.request",
        "query.result",
        "subscription.set",
        "usage.snapshot",
        "supervise.event",
    }
)
SCRIPT_ONLY_ACTIVITY = frozenset(
    {
        "pr.merged",
        "issue.assigned",
        "mail.ingest",
        "query.result",
        "session.register",
        "usage.snapshot",
        "error.seen",
        "supervise.event",
        "issue.assigned.ack",
    }
)
AGENT_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
VENDORS = ("grok", "codex")
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
STATIC = Path(__file__).resolve().parent / "static" / "index.html"


def die(msg: str) -> None:
    raise SystemExit(f"agent: {msg}")


def home() -> Path:
    override = os.environ.get("AGENT_HOME")
    if override == "":
        die("AGENT_HOME is set but empty")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "agent"


def open_store(*, allow_legacy_sqlite: bool = False) -> Store:
    h = home()
    sqlite_legacy = h / "ledger.sqlite"
    if sqlite_legacy.is_file() and not allow_legacy_sqlite:
        die("found ledger.sqlite; move it aside then run agent restore")
    dsn = os.environ.get("AGENT_PG_DSN")
    if dsn == "":
        die("AGENT_PG_DSN is set but empty")
    if not dsn:
        try:
            dsn = ensure_cluster(h / "pg")
        except PgError as exc:
            die(str(exc))
    try:
        require_loopback_dsn(dsn)
    except PgError as exc:
        die(str(exc))
    return Store(h, dsn)


def flag(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    idx = args.index(name)
    if idx + 1 >= len(args):
        die(f"{name} needs a value")
    return args[idx + 1]


def flags_all(args: list[str], name: str) -> list[str]:
    out: list[str] = []
    idx = 0
    while idx < len(args):
        if args[idx] == name:
            if idx + 1 >= len(args):
                die(f"{name} needs a value")
            out.append(args[idx + 1])
            idx += 2
            continue
        idx += 1
    return out


def require_flag(args: list[str], name: str) -> str:
    value = flag(args, name)
    if value is None or value == "":
        die(f"{name} is required")
    return value


def _bool_flag(args: list[str], name: str) -> bool | None:
    raw = flag(args, name)
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    die(f"{name} must be true|false")
    return None


def should_sync_on_ws(message: dict) -> bool:
    """Whether a hub WebSocket message should trigger push+pull."""
    msg_type = message.get("type")
    if msg_type in ("control", "terminal", "control-ack", "control-ready", "subscription"):
        return False
    if msg_type == "events":
        return True
    if msg_type == "ping" and "id" in message:
        return True
    return False


def packaged_skills_dir() -> Path:
    import agent_cli

    return Path(agent_cli.__file__).resolve().parent / "skills"


def _skills_dir_complete(root: Path) -> bool:
    return all((root / name / "SKILL.md").is_file() for name in ("spine", "review-loop", "pr-review"))


def resolve_skills_dir() -> Path | None:
    override = os.environ.get("AGENT_SKILLS_DIR")
    if override:
        candidate = Path(override)
        if _skills_dir_complete(candidate):
            return candidate.resolve()
        die("AGENT_SKILLS_DIR does not contain spine, review-loop, and pr-review SKILL.md")
    packaged = packaged_skills_dir()
    if _skills_dir_complete(packaged):
        return packaged.resolve()
    return None


def cmd_skills(args: list[str]) -> None:
    if len(args) != 1 or args[0] != "path":
        die("Usage: agent skills path")
    found = resolve_skills_dir()
    if found is None:
        die("skill docs are not installed")
    print(found)


def cmd_init(_: list[str]) -> None:
    from .daemon import agent_argv, install_and_start_service

    store = open_store()
    try:
        install_and_start_service(home=store.home, program=[*agent_argv(), "daemon"])
        daemon_state = "installed" if os.environ.get("PYTEST_CURRENT_TEST") else "running"
        print(f"ok  device={store.device_id()} home={store.home} daemon={daemon_state}")
    finally:
        store.close()


def cmd_session(args: list[str]) -> None:
    if not args:
        die(
            "Usage: agent session register|heartbeat|list|close|start|stop|"
            "input|keep-working|skill"
        )
    store = open_store()
    try:
        sub, rest = args[0], args[1:]
        if sub == "register":
            sid = require_flag(rest, "--id")
            kind = require_flag(rest, "--kind")
            if kind not in ("human", "runner", "other"):
                die("kind must be human|runner|other")
            requested = flags_all(rest, "--skill")
            for name in requested:
                if name not in SKILL_NAMES:
                    die(f"skill must be {'|'.join(SKILL_NAMES)}")
            existing = store.row("session", sid)
            if existing is not None:
                if existing.get("kind") != kind:
                    die(f"session {sid} is already kind={existing.get('kind')}")
                if existing.get("status") == "closed":
                    die(f"session {sid} is closed")
                existing["last_seen_at"] = utcnow()
                existing["status"] = "active"
                existing["host"] = socket.gethostname()
                skills = list(existing.get("skills") or [])
                for name in requested:
                    if name not in skills:
                        skills.append(name)
                existing["skills"] = skills
                store.write("session", "update", sid, _strip(existing))
            else:
                store.write(
                    "session",
                    "insert",
                    sid,
                    {
                        "id": sid,
                        "kind": kind,
                        "started_at": utcnow(),
                        "last_seen_at": utcnow(),
                        "host": socket.gethostname(),
                        "status": "active",
                        "skills": list(requested),
                    },
                )
            print(f"registered {sid} kind={kind}")
            return
        if sub == "heartbeat":
            sid = require_flag(rest, "--id")
            row = _need(store, "session", sid)
            row["last_seen_at"] = utcnow()
            store.write("session", "update", sid, _strip(row))
            print(f"heartbeat {sid}")
            return
        if sub == "list":
            for row in store.rows("session"):
                print(f"{row['id']}  {row['kind']}  {row['status']}  {row['last_seen_at']}")
            return
        if sub == "close":
            sid = require_flag(rest, "--id")
            row = _need(store, "session", sid)
            open_tasks = [
                t
                for t in store.rows("task")
                if t.get("session_id") == sid and t.get("state") not in ("done", "failed")
            ]
            if open_tasks:
                die("session has open tasks")
            open_work = [
                w
                for w in store.rows("open_work")
                if w.get("session_id") == sid and w.get("status") == "open"
            ]
            if open_work:
                die("session has open work")
            if pending_assigned(store, sid):
                die("session has pending assigned issues")
            pending_fix = [
                a
                for a in store.rows("activity")
                if a.get("session_id") == sid
                and a.get("type") == "error.fix"
                and a.get("execution_status") == "pending"
            ]
            if pending_fix:
                die("session has pending error.fix")
            row["status"] = "closed"
            row["last_seen_at"] = utcnow()
            store.write("session", "update", sid, _strip(row))
            print(f"closed {sid}")
            return
        if sub == "start":
            sid = require_flag(rest, "--id")
            cmd = flag(rest, "--cmd")
            provider = flag(rest, "--provider")
            model = flag(rest, "--model")
            cols = _dim_flag(rest, "--cols")
            rows = _dim_flag(rest, "--rows")
            runtime = Runtime()
            name = _session_start(
                store, runtime, sid, cmd, cols, rows, provider=provider, model=model
            )
            row = store.row("session", sid)
            grok_id = ""
            if isinstance(row, dict):
                meta = row.get("runtime")
                if isinstance(meta, dict) and isinstance(meta.get("grok_session_id"), str):
                    grok_id = meta["grok_session_id"]
            if grok_id:
                print(f"started {sid} tmux={name} grok={grok_id}")
            else:
                print(f"started {sid} tmux={name}")
            return
        if sub == "stop":
            sid = require_flag(rest, "--id")
            runtime = Runtime()
            _session_stop(store, runtime, sid)
            print(f"stopped {sid}")
            return
        if sub == "input":
            sid = require_flag(rest, "--id")
            data = flag(rest, "--data")
            key = flag(rest, "--key")
            runtime = Runtime()
            _session_input(store, runtime, sid, data, key)
            print(f"input {sid}")
            return
        if sub == "keep-working":
            sid = require_flag(rest, "--id")
            once = "--once" in rest
            follow = "--follow" in rest
            if once and follow:
                die("keep-working takes at most one of --once or --follow")
            runtime = Runtime()
            _session_keep_working(store, runtime, sid, once=once or not follow)
            return
        if sub == "skill":
            if not rest:
                die("Usage: agent session skill attach|list …")
            action, skill_rest = rest[0], rest[1:]
            if action == "attach":
                sid = require_flag(skill_rest, "--id")
                name = require_flag(skill_rest, "--skill")
                if name not in SKILL_NAMES:
                    die(f"skill must be {'|'.join(SKILL_NAMES)}")
                row = _need(store, "session", sid)
                _require_owned(store, row, "session")
                if row.get("status") != "active":
                    die(f"session {sid} is not active")
                skills = list(row.get("skills") or [])
                if name not in skills:
                    skills.append(name)
                row["skills"] = skills
                store.write("session", "update", sid, _strip(row))
                print(f"skill {name} attached to {sid}")
                return
            if action == "list":
                sid = require_flag(skill_rest, "--id")
                row = _need(store, "session", sid)
                skills = row.get("skills") or []
                if not skills:
                    print(f"{sid}  (none)")
                    return
                print(f"{sid}  {' '.join(str(s) for s in skills)}")
                return
            die(f"unknown session skill command: {action}")
        die(f"unknown session command: {sub}")
    finally:
        store.close()


def cmd_activity(args: list[str]) -> None:
    if not args:
        die("Usage: agent activity add --session ID --type TYPE --payload-file FILE")
    store = open_store()
    try:
        sub, rest = args[0], args[1:]
        if sub != "add":
            die(f"unknown activity command: {sub}")
        sid = require_flag(rest, "--session")
        typ = require_flag(rest, "--type")
        path = Path(require_flag(rest, "--payload-file"))
        if not path.is_file():
            die(f"payload file not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            die("payload file must contain a JSON object")
        session = _need(store, "session", sid)
        _require_owned(store, session, "session")
        if session.get("status") != "active":
            die(f"session {sid} is not active")
        if typ in SCRIPT_ONLY_ACTIVITY:
            die(f"{typ} is written by a script, not agent activity add")
        if typ in ("error.skip", "error.fix"):
            from .error_fix_act import validate_conclusion

            _require_skill(session, "error-fix")
            with store.exclusive("error-fix-act:" + store.device_id()):
                validate_conclusion(store, sid, typ, raw)
                activity_id = str(uuid.uuid4())
                store.write(
                    "activity",
                    "insert",
                    activity_id,
                    {
                        "id": activity_id,
                        "session_id": sid,
                        "type": typ,
                        "payload": raw,
                        "execution_status": "pending",
                    },
                )
            print(f"activity {activity_id} type={typ}")
            return
        if typ == "issue.assigned.ack":
            assigned_id = raw.get("assigned_id")
            pending = pending_assigned(store, sid)
            head = pending[0].get("id") if pending else None
            if not isinstance(assigned_id, str) or assigned_id == "" or assigned_id != head:
                die("issue.assigned.ack assigned_id must be the queue head")
        status = "pending" if typ in ACTIVITY_TYPES else "error"
        activity_id = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": sid,
                "type": typ,
                "payload": raw,
                "execution_status": status,
            },
        )
        print(f"activity {activity_id} type={typ}")
    finally:
        store.close()


def cmd_task(args: list[str]) -> None:
    if not args:
        die("Usage: agent task create|list|show|state|summary")
    store = open_store()
    try:
        sub, rest = args[0], args[1:]
        if sub == "create":
            session_id = require_flag(rest, "--session")
            workflow = require_flag(rest, "--workflow")
            title = require_flag(rest, "--title")
            error_id = flag(rest, "--error-id")
            if error_id is not None and workflow != "implement":
                die("workflow must be implement")
            if workflow not in CHECKLIST:
                die("workflow must be implement|review|resolve-conflicts")
            session = _need(store, "session", session_id)
            if session.get("status") != "active":
                die(f"session {session_id} is not active")
            _require_owned(store, session, "session")
            _require_skill(session, "spine")
            if error_id is not None:
                from .error_fix_act import find_or_create_implement_task

                _require_skill(session, "error-fix")
                tid, _created = find_or_create_implement_task(
                    store,
                    session_id,
                    error_id,
                    title,
                    ref=flag(rest, "--ref"),
                )
                print(f"task {tid}")
                return
            tid = str(uuid.uuid4())
            store.write(
                "task",
                "insert",
                tid,
                {
                    "id": tid,
                    "session_id": session_id,
                    "workflow": workflow,
                    "title": title,
                    "repo": flag(rest, "--repo"),
                    "ref": flag(rest, "--ref"),
                    "state": "open",
                    "current_round": 0,
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                    "change_summary_en": None,
                    "change_summary_de": None,
                },
            )
            source = "runner" if session.get("kind") == "runner" else "human"
            for key in CHECKLIST[workflow]:
                cid = str(uuid.uuid4())
                store.write(
                    "checklist_item",
                    "insert",
                    cid,
                    {
                        "id": cid,
                        "task_id": tid,
                        "key": key,
                        "status": "pending",
                        "evidence": None,
                        "source": source,
                        "deviation_declared": False,
                        "deviation_granted": False,
                        "granted_by": None,
                        "updated_at": utcnow(),
                    },
                )
            print(f"task {tid}")
            return
        if sub == "list":
            sid = flag(rest, "--session")
            if sid:
                _require_skill(_need(store, "session", sid), "spine")
            for row in store.rows("task"):
                if sid and row.get("session_id") != sid:
                    continue
                if not sid:
                    sess = store.row("session", str(row.get("session_id") or ""))
                    if sess is None or not has_skill(sess, "spine"):
                        continue
                print(f"{row['id']}  {row['workflow']}  {row['state']}  r{row['current_round']}  {row['title']}")
            return
        if sub == "show":
            if len(rest) != 1:
                die("Usage: agent task show <id>")
            task = _need(store, "task", rest[0])
            _require_skill(_need(store, "session", task["session_id"]), "spine")
            print(json.dumps(task, indent=2))
            return
        if sub == "state":
            if len(rest) != 2:
                die("Usage: agent task state <id> <state>")
            tid, state = rest
            if state not in TASK_STATES:
                die("unknown state")
            row = _need(store, "task", tid)
            _require_owned(store, row, "task")
            _require_skill(_need(store, "session", row["session_id"]), "spine")
            if state == "done":
                _assert_ready(store, row)
            row["state"] = state
            row["updated_at"] = utcnow()
            store.write("task", "update", tid, _strip(row))
            print(f"task {tid} state={state}")
            return
        if sub == "summary":
            tid = require_flag(rest, "--id")
            en = require_flag(rest, "--en")
            de = require_flag(rest, "--de")
            if "\n" in en or "\n" in de:
                die("summary must be one line per language")
            row = _need(store, "task", tid)
            _require_owned(store, row, "task")
            _require_skill(_need(store, "session", row["session_id"]), "spine")
            row["change_summary_en"] = en
            row["change_summary_de"] = de
            row["updated_at"] = utcnow()
            store.write("task", "update", tid, _strip(row))
            print(f"task {tid} summary set")
            return
        die(f"unknown task command: {sub}")
    finally:
        store.close()


def cmd_checklist(args: list[str]) -> None:
    if not args or args[0] != "set":
        die(
            "Usage: agent checklist set --task ID --key KEY --status ja|nein|n_a|pending "
            "--source human|runner|script "
            "[--evidence TEXT] [--deviation-declared true|false] "
            "[--deviation-granted true|false] [--granted-by TEXT] [--actor-session ID]"
        )
    rest = args[1:]
    tid = require_flag(rest, "--task")
    key = require_flag(rest, "--key")
    status = require_flag(rest, "--status")
    source = require_flag(rest, "--source")
    if status not in ("ja", "nein", "n_a", "pending"):
        die("status must be ja|nein|n_a|pending")
    if source not in ("human", "runner", "script"):
        die("source must be human|runner|script")
    evidence = flag(rest, "--evidence")
    if status == "n_a" and (evidence is None or evidence == ""):
        die("n_a requires --evidence")
    if key == "mergeable" and status == "ja" and (evidence is None or evidence == ""):
        die("mergeable=ja requires --evidence")
    deviation_declared = _bool_flag(rest, "--deviation-declared")
    deviation_granted = _bool_flag(rest, "--deviation-granted")
    granted_by = flag(rest, "--granted-by")
    actor_session = flag(rest, "--actor-session")
    store = open_store()
    try:
        if deviation_granted is True:
            if source != "human":
                die("deviation-granted=true requires --source human")
            if not granted_by:
                die("deviation-granted=true requires --granted-by")
            if not actor_session:
                die("deviation-granted=true requires --actor-session")
            actor = _need(store, "session", actor_session)
            if actor.get("kind") != "human":
                die("--actor-session must be kind=human")
        if key == "deviation_granted" and status == "ja":
            if source != "human":
                die("deviation_granted=ja requires --source human")
            if deviation_granted is not True:
                die("deviation_granted=ja requires --deviation-granted true")
            if not granted_by:
                die("deviation_granted=ja requires --granted-by")
        task = _need(store, "task", tid)
        _require_owned(store, task, "task")
        _require_skill(_need(store, "session", task["session_id"]), "spine")
        items = [r for r in store.rows("checklist_item") if r.get("task_id") == tid and r.get("key") == key]
        if len(items) != 1:
            die(f"checklist {key} for task {tid} not found")
        item = items[0]
        item["status"] = status
        item["source"] = source
        item["evidence"] = evidence
        if deviation_declared is not None:
            item["deviation_declared"] = deviation_declared
        if deviation_granted is not None:
            item["deviation_granted"] = deviation_granted
        if granted_by is not None:
            item["granted_by"] = granted_by
        item["updated_at"] = utcnow()
        store.write("checklist_item", "update", item["id"], _strip(item))
        print(f"checklist {key}={status}")
    finally:
        store.close()


def cmd_round(args: list[str]) -> None:
    if not args or args[0] != "start":
        die("Usage: agent round start --task UUID")
    rest = args[1:]
    tid = require_flag(rest, "--task")
    store = open_store()
    try:
        task = _need(store, "task", tid)
        session = _need(store, "session", task["session_id"])
        if session.get("status") != "active":
            die(f"session {task['session_id']} is not active")
        _require_owned(store, task, "task")
        _require_skill(session, "spine")
        workflow = task.get("workflow")
        if workflow not in ("implement", "resolve-conflicts"):
            die("round start requires workflow implement|resolve-conflicts")
        if task.get("state") == "done":
            die("cannot start a round on a done task")
        current = int(task.get("current_round") or 0)
        for agent in store.rows("agent"):
            if agent.get("task_id") == tid and agent.get("status") == "working":
                die("round still has a working agent")
        n = current + 1
        task["current_round"] = n
        task["state"] = "implementing"
        task["updated_at"] = utcnow()
        store.write("task", "update", tid, _strip(task))
        rid = str(uuid.uuid4())
        store.write(
            "task_round",
            "insert",
            rid,
            {
                "id": rid,
                "task_id": tid,
                "round": n,
                "implementer_verdict": None,
                "reviewer_verdict": None,
                "started_at": utcnow(),
                "finished_at": None,
            },
        )
        print(f"task {tid} round {n}")
    finally:
        store.close()


def cmd_agent(args: list[str]) -> None:
    if not args or args[0] not in ("start", "finish"):
        die("Usage: agent agent start|finish …")
    sub, rest = args[0], args[1:]
    store = open_store()
    try:
        if sub == "start":
            session_id = require_flag(rest, "--session")
            tid = require_flag(rest, "--task")
            role = require_flag(rest, "--role")
            vendor = require_flag(rest, "--vendor")
            if role not in AGENT_ROLES:
                die(f"role must be {'|'.join(AGENT_ROLES)}")
            if vendor not in VENDORS:
                die("vendor must be grok|codex")
            round_raw = flag(rest, "--round")
            round_num: int | None = None
            if role in ("implementer", "reviewer"):
                if round_raw is None or round_raw == "":
                    die(f"{role} requires --round")
                if vendor != "grok":
                    die(f"{role} requires --vendor grok")
            if round_raw is not None and round_raw != "":
                if not round_raw.isdigit():
                    die("--round must be an integer")
                round_num = int(round_raw)
            session = _need(store, "session", session_id)
            if session.get("status") != "active":
                die(f"session {session_id} is not active")
            _require_owned(store, session, "session")
            try:
                _require_skill(session, skill_for_agent_role(role))
            except ValueError:
                die(f"unknown agent role: {role}")
            task = _need(store, "task", tid)
            if task.get("session_id") != session_id:
                die("session does not own this task")
            tr = None
            if round_num is not None:
                tr = _find_round(store, tid, round_num)
            if role in ("implementer", "reviewer"):
                if round_num != int(task.get("current_round") or 0):
                    die("agent round must match task.current_round")
                for other in store.rows("agent"):
                    if (
                        other.get("task_id") == tid
                        and other.get("round") == round_num
                        and other.get("role") == role
                        and other.get("status") == "working"
                    ):
                        die("role already taken for this round")
                if tr is None:
                    die(f"task_round task={tid} round={round_num} not found")
                verdict_key = (
                    "implementer_verdict" if role == "implementer" else "reviewer_verdict"
                )
                if tr.get(verdict_key) is not None:
                    die("role already taken for this round")
            if role == "implementer":
                if task.get("state") != "implementing":
                    die("task state must be implementing")
            if role == "reviewer":
                if task.get("state") != "reviewing":
                    die("task state must be reviewing")
                if tr is None or tr.get("implementer_verdict") != "done":
                    die("implementer_verdict must be done before reviewer start")
            _require_owned(store, task, "task")
            if tr is not None:
                _require_owned(store, tr, "task_round")
            aid = str(uuid.uuid4())
            store.write(
                "agent",
                "insert",
                aid,
                {
                    "id": aid,
                    "session_id": session_id,
                    "task_id": tid,
                    "round": round_num,
                    "role": role,
                    "vendor": vendor,
                    "status": "working",
                    "started_at": utcnow(),
                    "finished_at": None,
                    "note": None,
                },
            )
            print(f"agent {aid}")
            return
        if sub == "finish":
            aid = require_flag(rest, "--id")
            verdict = require_flag(rest, "--verdict")
            note = flag(rest, "--note")
            agent = _need(store, "agent", aid)
            if agent.get("status") != "working":
                die(f"agent {aid} is not working")
            _require_owned(store, agent, "agent")
            role = agent.get("role")
            task = _need(store, "task", agent["task_id"])
            session = _need(store, "session", task["session_id"])
            if session.get("status") != "active":
                die("session is not active")
            _require_owned(store, session, "session")
            _require_owned(store, task, "task")
            try:
                _require_skill(session, skill_for_agent_role(str(role)))
            except ValueError:
                die(f"unknown agent role: {role}")
            if role == "implementer":
                if verdict not in ("done", "blocked"):
                    die("implementer verdict must be done|blocked")
                if agent.get("round") != int(task.get("current_round") or 0):
                    die("agent round is not the current round")
                if task.get("state") != "implementing":
                    die("task state must be implementing")
                tr = _find_round(store, agent["task_id"], agent["round"])
                if tr.get("implementer_verdict") is not None:
                    die("implementer already finished this round")
                _require_owned(store, task, "task")
                _require_owned(store, tr, "task_round")
                tr["implementer_verdict"] = verdict
                if verdict == "blocked":
                    tr["finished_at"] = utcnow()
                store.write("task_round", "update", tr["id"], _strip(tr))
                task["state"] = "reviewing" if verdict == "done" else "failed"
                task["updated_at"] = utcnow()
                store.write("task", "update", task["id"], _strip(task))
            elif role == "reviewer":
                if verdict not in ("approved", "rejected"):
                    die("reviewer verdict must be approved|rejected")
                if agent.get("round") != int(task.get("current_round") or 0):
                    die("agent round is not the current round")
                if task.get("state") != "reviewing":
                    die("task state must be reviewing")
                tr = _find_round(store, agent["task_id"], agent["round"])
                if tr.get("implementer_verdict") != "done":
                    die("implementer_verdict must be done before reviewer finish")
                if tr.get("reviewer_verdict") is not None:
                    die("reviewer already finished this round")
                _require_owned(store, task, "task")
                _require_owned(store, tr, "task_round")
                tr["reviewer_verdict"] = verdict
                tr["finished_at"] = utcnow()
                store.write("task_round", "update", tr["id"], _strip(tr))
                task["state"] = "local-check" if verdict == "approved" else "implementing"
                task["updated_at"] = utcnow()
                store.write("task", "update", task["id"], _strip(task))
            elif role in ("pr-reviewer-quality", "pr-reviewer-logic"):
                if verdict not in ("approved", "rejected"):
                    die("pr-reviewer verdict must be approved|rejected")
                _require_owned(store, task, "task")
            else:
                die(f"unknown agent role: {role}")
            agent["status"] = "done"
            agent["finished_at"] = utcnow()
            if note is not None:
                agent["note"] = note
            store.write("agent", "update", aid, _strip(agent))
            print(f"agent {aid} verdict={verdict}")
            return
    finally:
        store.close()


def cmd_check(args: list[str]) -> None:
    if not args or args[0] != "record":
        die(
            "Usage: agent check record --task UUID --name NAME --command CMD "
            "--result pass|fail|skip [--output TEXT]"
        )
    rest = args[1:]
    tid = require_flag(rest, "--task")
    name = require_flag(rest, "--name")
    command = require_flag(rest, "--command")
    result = require_flag(rest, "--result")
    output = flag(rest, "--output")
    if result not in ("pass", "fail", "skip"):
        die("result must be pass|fail|skip")
    if result == "skip" and (output is None or output == ""):
        die("skip requires --output")
    store = open_store()
    try:
        task = _need(store, "task", tid)
        session = _need(store, "session", task["session_id"])
        if session.get("status") != "active":
            die(f"session {task['session_id']} is not active")
        _require_owned(store, task, "task")
        _require_skill(session, "spine")
        cid = str(uuid.uuid4())
        store.write(
            "local_check",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "name": name,
                "command": command,
                "result": result,
                "output": output,
                "ran_at": utcnow(),
            },
        )
        if result == "fail":
            task["state"] = "failed"
            task["updated_at"] = utcnow()
            store.write("task", "update", tid, _strip(task))
        print(f"check {name}={result}")
    finally:
        store.close()


def _task_pull_request(task: dict) -> tuple[str, int] | None:
    """The task's pull request as (repo, number), or None when it has none."""
    repo = _repo_ok(task.get("repo"))
    ref = task.get("ref")
    if repo is None:
        return None
    if not isinstance(ref, str) or not ref.isdigit() or int(ref) <= 0:
        return None
    return repo, int(ref)


def _queue_gate_findings(
    store: Store,
    task: dict,
    stage: str,
    dimension: str,
    vendor: str,
    head: str,
    evidence: str,
) -> str | None:
    """Queue a rejected gate's evidence as a pull-request comment.

    A rejection that is only recorded stops the task without telling the author
    what was found. `agent github pending` performs the HTTP.
    """
    pull_request = _task_pull_request(task)
    if pull_request is None:
        return None
    repo, number = pull_request
    # Derived, not random: a retried `gate record` must reuse the id so the marker
    # github_act writes matches and the findings are not posted twice. The key holds
    # everything that defines the comment — where it goes (repo, number) and what it
    # says (lane, head, evidence) — so anything that would read differently on the
    # pull request gets its own identity rather than being taken for a repeat.
    activity_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"gate-findings:{task['id']}:{repo}:{number}:{stage}:{dimension}:{head}:{evidence}",
        )
    )
    def _settled() -> bool:
        # An errored row is not settled: the executor gave up on it, so a later
        # `gate record` has to hand it back rather than treat it as delivered.
        row = store.row("activity", activity_id)
        return row is not None and row.get("execution_status") != "error"

    payload = {
        "id": activity_id,
        "session_id": task["session_id"],
        "type": "comment.post",
        "payload": {
            "repo": repo,
            "number": number,
            "target": "pr",
            "body": f"`{stage}` / `{dimension}` ({vendor}) rejected at `{head}`\n\n{evidence}",
        },
        "execution_status": "pending",
    }

    def _queue(op: str) -> dict | None:
        return store.write_with_advisory(
            "activity",
            op,
            activity_id,
            payload,
            lock_key=f"gate-findings:{activity_id}",
            skip=_settled,
        )

    # One read serves both questions: whether this comment is already delivered, and
    # whether there is a row to update. `skip` re-reads under the lock, which is what
    # makes the decision authoritative — this read only avoids taking the lock at all.
    existing = store.row("activity", activity_id)
    if existing is not None and existing.get("execution_status") != "error":
        return None
    op = "update" if existing is not None else "insert"
    try:
        written = _queue(op)
    except StoreError:
        # Only a stale `insert` is recoverable: the row appeared between that read
        # and the lock. Any other StoreError is a real failure and must surface.
        if op != "insert":
            raise
        written = _queue("update")
    return None if written is None else activity_id


def cmd_gate(args: list[str]) -> None:
    if not args or args[0] != "record":
        die(
            "Usage: agent gate record --task UUID --stage grok-pr|codex-pr "
            "--dimension quality|logic --vendor grok|codex --verdict approved|rejected "
            "--head SHA --agent UUID [--evidence TEXT; required when rejected]"
        )
    rest = args[1:]
    tid = require_flag(rest, "--task")
    stage = require_flag(rest, "--stage")
    dimension = require_flag(rest, "--dimension")
    vendor = require_flag(rest, "--vendor")
    verdict = require_flag(rest, "--verdict")
    head = require_flag(rest, "--head").lower()
    agent_id = require_flag(rest, "--agent")
    evidence = flag(rest, "--evidence")
    if stage not in ("grok-pr", "codex-pr"):
        die("stage must be grok-pr|codex-pr")
    if dimension not in ("quality", "logic"):
        die("dimension must be quality|logic")
    if vendor not in VENDORS:
        die("vendor must be grok|codex")
    expected_vendor = "grok" if stage == "grok-pr" else "codex"
    if vendor != expected_vendor:
        die(f"stage {stage} requires vendor {expected_vendor}")
    if verdict not in ("approved", "rejected"):
        die("verdict must be approved|rejected")
    if verdict == "rejected" and not (evidence or "").strip():
        die("--evidence is required when --verdict is rejected")
    if not re.fullmatch(r"[0-9a-f]{7,40}", head):
        die("--head must be a git SHA (lowercase hex, length 7–40)")
    store = open_store()
    try:
        task = _need(store, "task", tid)
        session = _need(store, "session", task["session_id"])
        if session.get("status") != "active":
            die(f"session {task['session_id']} is not active")
        _require_owned(store, session, "session")
        _require_owned(store, task, "task")
        _require_skill(session, "pr-review")
        if stage == "codex-pr":
            latest = _latest_gates(store, tid)
            gq = latest.get(("grok-pr", "quality"))
            gl = latest.get(("grok-pr", "logic"))
            for label, g in (("quality", gq), ("logic", gl)):
                if g is None:
                    die(f"codex-pr requires approved grok-pr/{label} at the same head")
                if g.get("vendor") != "grok" or g.get("verdict") != "approved":
                    die(f"codex-pr requires approved grok-pr/{label} at the same head")
                if g.get("head_sha") != head:
                    die(f"codex-pr requires approved grok-pr/{label} at the same head")
        agent = _need(store, "agent", agent_id)
        if agent.get("task_id") != tid:
            die("agent task_id does not match --task")
        if agent.get("status") != "done":
            die("agent must be status=done")
        if agent.get("vendor") != vendor:
            die("agent vendor does not match --vendor")
        expected_role = f"pr-reviewer-{dimension}"
        if agent.get("role") != expected_role:
            die(f"agent role must be {expected_role}")
        if verdict == "rejected" and task.get("state") == "done":
            die("cannot reject a gate on a done task")
        _require_owned(store, task, "task")
        gid = str(uuid.uuid4())
        store.write(
            "review_gate",
            "insert",
            gid,
            {
                "id": gid,
                "task_id": tid,
                "stage": stage,
                "dimension": dimension,
                "vendor": vendor,
                "verdict": verdict,
                "evidence": evidence,
                "head_sha": head,
                "agent_id": agent_id,
                "recorded_at": utcnow(),
            },
        )
        if verdict == "rejected" and task.get("workflow") in ("implement", "resolve-conflicts"):
            if task.get("state") != "implementing":
                task["state"] = "implementing"
                task["updated_at"] = utcnow()
                store.write("task", "update", tid, _strip(task))
        queued = None
        if verdict == "rejected":
            queued = _queue_gate_findings(
                store, task, stage, dimension, vendor, head, evidence or ""
            )
        print(f"gate {stage}/{dimension}={verdict}")
        if queued is not None:
            print(f"activity {queued} type=comment.post")
        elif verdict == "rejected" and _task_pull_request(task) is None:
            print("no pull request on this task: findings not queued")
    finally:
        store.close()


def cmd_work(args: list[str]) -> None:
    if not args:
        die("Usage: agent work add|set|list …")
    sub, rest = args[0], args[1:]
    store = open_store()
    try:
        if sub == "add":
            session_id = require_flag(rest, "--session")
            key = require_flag(rest, "--key")
            closable_by = require_flag(rest, "--closable-by")
            note = flag(rest, "--note")
            if closable_by not in ("agent", "human"):
                die("closable-by must be agent|human")
            session = _need(store, "session", session_id)
            if session.get("status") != "active":
                die(f"session {session_id} is not active")
            _require_owned(store, session, "session")
            _require_skill(session, "spine")
            for row in store.rows("open_work"):
                if row.get("session_id") == session_id and row.get("key") == key:
                    die(f"work {key} already exists for session {session_id}")
            wid = str(uuid.uuid4())
            store.write(
                "open_work",
                "insert",
                wid,
                {
                    "id": wid,
                    "session_id": session_id,
                    "key": key,
                    "status": "open",
                    "closable_by": closable_by,
                    "note": note,
                    "updated_at": utcnow(),
                },
            )
            print(f"work {key} open closable_by={closable_by}")
            return
        if sub == "set":
            session_id = require_flag(rest, "--session")
            key = require_flag(rest, "--key")
            status = require_flag(rest, "--status")
            source = require_flag(rest, "--source")
            actor_session = flag(rest, "--actor-session")
            if status not in ("open", "done", "cancelled"):
                die("status must be open|done|cancelled")
            if source not in ("human", "runner", "script"):
                die("source must be human|runner|script")
            session = _need(store, "session", session_id)
            if session.get("status") != "active":
                die(f"session {session_id} is not active")
            _require_owned(store, session, "session")
            _require_skill(session, "spine")
            matches = [
                r
                for r in store.rows("open_work")
                if r.get("session_id") == session_id and r.get("key") == key
            ]
            if len(matches) != 1:
                die(f"work {key} for session {session_id} not found")
            row = matches[0]
            if row.get("closable_by") == "human":
                if source != "human":
                    die("work closable_by=human requires --source human")
                if not actor_session:
                    die("work closable_by=human requires --actor-session")
                actor = _need(store, "session", actor_session)
                if actor.get("kind") != "human":
                    die("--actor-session must be kind=human")
            row["status"] = status
            row["updated_at"] = utcnow()
            store.write("open_work", "update", row["id"], _strip(row))
            print(f"work {key}={status}")
            return
        if sub == "list":
            sid = flag(rest, "--session")
            if sid:
                _require_skill(_need(store, "session", sid), "spine")
            for row in store.rows("open_work"):
                if sid and row.get("session_id") != sid:
                    continue
                if not sid:
                    sess = store.row("session", str(row.get("session_id") or ""))
                    if sess is None or not has_skill(sess, "spine"):
                        continue
                print(f"{row['session_id']}  {row['key']}  {row['status']}  {row['closable_by']}")
            return
        die(f"unknown work command: {sub}")
    finally:
        store.close()


def cmd_pair(args: list[str]) -> None:
    hub_url = flag(args, "--hub") or os.environ.get("AGENT_HUB")
    if not hub_url:
        die("pair needs --hub or AGENT_HUB")
    name = flag(args, "--name") or socket.gethostname()
    timeout_raw = flag(args, "--timeout") or "180"
    if not timeout_raw.isdigit():
        die("--timeout must be an integer")
    store = open_store()
    try:
        if store.meta("device_token"):
            die("this device is already paired")
        challenge = store.meta("pair_challenge") or secrets.token_hex(16)
        store.set_meta("pair_challenge", challenge)
        store.set_meta("hub_url", hub_url.rstrip("/"))
        hub = Hub(hub_url)
        try:
            prepared = hub.prepare(store.device_id(), challenge, name)
        finally:
            hub.close()
        pair_url = prepared["pair_url"]
        print(pair_url)
        webbrowser.open(pair_url)
        deadline = time.time() + int(timeout_raw)
        hub = Hub(hub_url)
        try:
            while time.time() < deadline:
                status = hub.wait(store.device_id(), challenge)
                if status.get("status") == "paired":
                    token = status.get("token")
                    login = status.get("login")
                    if not isinstance(token, str) or not isinstance(login, str):
                        die("pair response missing token or login")
                    store.set_meta("device_token", token)
                    store.set_meta("github_login", login)
                    store.set_meta("hub_url", hub_url.rstrip("/"))
                    print(f"paired as {login} device={store.device_id()}")
                    return
                time.sleep(1)
        finally:
            hub.close()
        die("pairing timed out")
    finally:
        store.close()


_MAX_BACKOFF = 30.0


def cmd_sync(args: list[str]) -> None:
    follow = "--follow" in args
    store = open_store()
    try:
        if not follow:
            _sync_once(store)
            return
        hub: Hub | None = None
        runtime = Runtime()
        terminal_seq: dict[str, int] = {}
        last_capture: dict[str, str] = {}
        try:
            backoff = 1.0
            while True:
                established: dict[str, float] = {}
                try:
                    if hub is not None:
                        try:
                            hub.close()
                        except Exception:
                            pass
                        hub = None
                    # Rebuild every iteration so a rotated device_token / hub URL
                    # from the store is picked up (Hub freezes both at init).
                    hub = _hub_from_store(store)
                    _sync_once(store)
                    _run_sync_ws_session(store, hub, runtime, terminal_seq, last_capture, established)
                except (HubError, StoreConnectionError, OSError, WebSocketException) as exc:
                    started = established.get("at")
                    failed_at = time.monotonic() if started is not None else None
                    print(f"agent: sync connection lost, reconnecting: {exc}", file=sys.stderr)
                    if isinstance(exc, StoreConnectionError):
                        try:
                            store.reconnect()
                        except StoreConnectionError as reconnect_exc:
                            print(f"agent: postgres reconnect failed, will retry: {reconnect_exc}", file=sys.stderr)
                    if failed_at is not None and failed_at - started >= _MAX_BACKOFF:
                        backoff = 1.0
                    time.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
        finally:
            if hub is not None:
                hub.close()
    finally:
        store.close()


def _run_sync_ws_session(
    store: Store,
    hub: Hub,
    runtime: Runtime,
    terminal_seq: dict[str, int],
    last_capture: dict[str, str],
    established: dict[str, float],
) -> None:
    """Run one websocket connection's message loop. Raises on disconnect/error;
    the caller in cmd_sync reconnects with backoff instead of exiting the process.
    Sets established["at"] once the handshake actually succeeds, so the caller can
    measure connection stability from there rather than from the connect attempt."""
    ws = hub.connect_sync_ws()
    try:
        ws.send(json.dumps({"type": "control-ready"}))
        established["at"] = time.monotonic()
        last_capture.clear()  # a new connection has no idea what a prior one already sent
        _publish_terminals(store, runtime, ws, terminal_seq, last_capture)
        for raw in ws:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "control":
                ack = apply_control(store, runtime, message)
                ws.send(json.dumps(ack))
            if message.get("type") == "subscription":
                rows = message.get("rows")
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict) or not row.get("table"):
                            continue
                        try:
                            store.apply_replica_row(row)
                        except StoreConnectionError:
                            raise  # a lost DB connection must reach cmd_sync's reconnect loop
                        except (StoreError, KeyError, TypeError):
                            continue
            if should_sync_on_ws(message):
                _sync_once(store)
            _publish_terminals(store, runtime, ws, terminal_seq, last_capture)
        raise HubError("websocket closed")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def cmd_restore(_: list[str]) -> None:
    store = open_store(allow_legacy_sqlite=True)
    try:
        hub = _hub_from_store(store)
        try:
            body = hub.restore()
        finally:
            hub.close()
        if body.get("device_id") != store.device_id():
            die("restore device_id does not match this device")
        if "own_events" in body:
            events = body.get("own_events")
        else:
            events = body.get("events")
        if not isinstance(events, list):
            die("restore response missing own_events")
        for event in events:
            store.apply_remote(event, wake=False)
            store.mark_origin(event["origin_device_id"], int(event["origin_seq"]))
        snapshots = list(body.get("inbox") or []) + list(body.get("pings") or [])
        for row in snapshots:
            if not isinstance(row, dict):
                die("restore snapshot is not an object")
            store.apply_replica_row(row, wake=False)
        print(f"restored events={len(events)} snapshots={len(snapshots)}")
    finally:
        store.close()


def cmd_ping(args: list[str]) -> None:
    if not args:
        die("Usage: agent ping send|list|ack")
    store = open_store()
    try:
        sub, rest = args[0], args[1:]
        if sub == "send":
            to = require_flag(rest, "--to").lower()
            kind = require_flag(rest, "--kind")
            if kind not in PING_KINDS:
                die("kind must be review-request|ping|question")
            pid = str(uuid.uuid4())
            login = store.meta("github_login")
            if not login:
                die("device is not paired")
            store.write(
                "ping",
                "insert",
                pid,
                {
                    "id": pid,
                    "from_login": login,
                    "to_login": to,
                    "kind": kind,
                    "task_id": flag(rest, "--task"),
                    "body": flag(rest, "--note") or "",
                    "created_at": utcnow(),
                    "acked_at": None,
                },
            )
            print(f"ping {pid}")
            return
        if sub == "list":
            for row in store.rows("ping"):
                print(f"{row['id']}  {row['from_login']} → {row['to_login']}  {row['kind']}  acked={row.get('acked_at')}")
            return
        if sub == "ack":
            pid = require_flag(rest, "--id")
            row = store.row("ping", pid)
            login = store.meta("github_login")
            if row is not None and row.get("to_login") != login:
                die("only the recipient can ack")
            hub = _hub_from_store(store)
            try:
                result = hub.ack(pid)
            finally:
                hub.close()
            payload = result.get("payload")
            origin = row.get("_origin_device_id") if row is not None else None
            if isinstance(payload, dict) and isinstance(origin, str) and origin:
                store.apply_replica_row(
                    {
                        "table": "ping",
                        "row_id": pid,
                        "origin_device_id": origin,
                        "payload": payload,
                        "updated_at": payload.get("acked_at") or utcnow(),
                    }
                )
            print(f"acked {pid}")
            return
        die(f"unknown ping command: {sub}")
    finally:
        store.close()


def cmd_status(_: list[str]) -> None:
    store = open_store()
    try:
        data = store.snapshot()
        open_tasks = [t for t in data["tasks"] if t.get("state") not in ("done", "failed")]
        agents_working = [a for a in data["agents"] if a.get("status") == "working"]
        work_open = [w for w in data["work"] if w.get("status") == "open"]
        print(
            f"device={data['device_id']} login={data['login'] or '-'} "
            f"tasks_open={len(open_tasks)} pings={len(data['pings'])} "
            f"agents_working={len(agents_working)} work_open={len(work_open)}"
        )
        for t in open_tasks:
            print(f"  task {t['id']} {t['workflow']} {t['state']} {t['title']}")
    finally:
        store.close()


def cmd_dashboard(args: list[str]) -> None:
    port = 7845
    if args:
        if args[0] != "--port" or len(args) != 2 or not args[1].isdigit():
            die("Usage: agent dashboard [--port PORT]")
        port = int(args[1])
    store = open_store()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *rest: object) -> None:
            return

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/state":
                try:
                    snap = store.snapshot()
                    device = store.device_id()
                except StoreConnectionError:
                    try:
                        store.reconnect()
                    except StoreConnectionError as exc:
                        self._json(503, {"ok": False, "error": str(exc)})
                        return
                    try:
                        snap = store.snapshot()
                        device = store.device_id()
                    except StoreConnectionError as exc:
                        self._json(503, {"ok": False, "error": str(exc)})
                        return
                for session in snap.get("sessions") or []:
                    session["can_control"] = session.get("_origin_device_id") == device
                    session["control_connected"] = True
                self._json(200, snap)
                return
            if path in ("/", "/index.html"):
                if not STATIC.is_file():
                    self.send_error(500, "dashboard missing")
                    return
                body = STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            match = re.fullmatch(r"/api/sessions/([^/]+)/control", path)
            if match is None:
                self.send_error(404)
                return
            sid = match.group(1)
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (TypeError, ValueError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "invalid JSON"})
                return
            if not isinstance(body, dict):
                self._json(400, {"ok": False, "error": "body must be an object"})
                return

            def lookup_session() -> dict | None:
                row = store.row("session", sid)
                if row is None:
                    self.send_error(404)
                    return None
                if row.get("_origin_device_id") != store.device_id():
                    self.send_error(403)
                    return None
                return row

            try:
                row = lookup_session()
            except StoreConnectionError:
                try:
                    store.reconnect()
                except StoreConnectionError as exc:
                    self._json(503, {"ok": False, "error": str(exc)})
                    return
                try:
                    row = lookup_session()
                except StoreConnectionError as exc:
                    self._json(503, {"ok": False, "error": str(exc)})
                    return
            if row is None:
                return

            payload = body.get("payload")
            if not isinstance(payload, dict):
                payload = {k: v for k, v in body.items() if k != "action"}
            message = {
                "type": "control",
                "session_id": sid,
                "action": body.get("action"),
                "payload": payload,
            }
            try:
                ack = apply_control(store, Runtime(), message)
            except StoreConnectionError as exc:
                try:
                    store.reconnect()
                except StoreConnectionError as rec_exc:
                    self._json(503, {"ok": False, "error": str(rec_exc)})
                    return
                self._json(503, {"ok": False, "error": str(exc)})
                return
            if not ack.get("ok"):
                self._json(400, {"ok": False, "error": ack.get("error")})
                return
            self._json(200, {"ok": True})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    finally:
        store.close()


def _sync_once(store: Store) -> None:
    hub = _hub_from_store(store)
    try:
        pending = store.pending_events()
        if pending:
            hub.push(pending)
            store.mark_pushed(pending[-1]["origin_seq"])
        pulled = hub.pull(store.all_cursors())
        events = pulled.get("events")
        if not isinstance(events, list):
            die("pull response missing events")
        for event in events:
            store.apply_remote(event)
            store.mark_origin(event["origin_device_id"], int(event["origin_seq"]))
        snapshots = (
            list(pulled.get("inbox") or [])
            + list(pulled.get("pings") or [])
            + list(pulled.get("subscriptions") or [])
        )
        for row in snapshots:
            if not isinstance(row, dict):
                die("pull snapshot is not an object")
        sessions = [r for r in snapshots if isinstance(r, dict) and r.get("table") == "session"]
        rest = [r for r in snapshots if not (isinstance(r, dict) and r.get("table") == "session")]
        for row in sessions + rest:
            store.apply_replica_row(row)
        print(f"sync pushed={len(pending)} pulled={len(events)} snapshots={len(snapshots)}")
    finally:
        hub.close()


def _hub_from_store(store: Store) -> Hub:
    url = store.meta("hub_url")
    token = store.meta("device_token")
    if not url or not token:
        die("device is not paired")
    return Hub(url, token)


def _need(store: Store, table: str, row_id: str) -> dict:
    row = store.row(table, row_id)
    if row is None:
        die(f"unknown {table}: {row_id}")
    return row


def _require_owned(store: Store, row: dict, what: str) -> None:
    if row.get("_origin_device_id") != store.device_id():
        die(f"cannot mutate {what} owned by another device")


def _require_skill(session: dict, name: str) -> None:
    if not has_skill(session, name):
        die(f"session {session.get('id')} does not have skill {name}")


def _strip(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _dim_flag(args: list[str], name: str) -> int | None:
    raw = flag(args, name)
    if raw is None:
        return None
    if not raw.isdigit():
        die(f"{name} must be an integer 1..500")
    value = int(raw)
    if value < 1 or value > 500:
        die(f"{name} must be an integer 1..500")
    return value


def _dim_value(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        if isinstance(raw, str) and raw.isdigit():
            value = int(raw)
        else:
            die(f"{label} must be an integer 1..500")
            raise AssertionError("unreachable")
    else:
        value = raw
    if value < 1 or value > 500:
        die(f"{label} must be an integer 1..500")
    return value


def _session_start(
    store: Store,
    runtime: Runtime,
    sid: str,
    command: str | None,
    cols: int | None,
    rows: int | None,
    provider: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> str:
    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    if row.get("status") != "active":
        die(f"session {sid} is not active")
    if not runtime.available():
        die("tmux is not installed")
    if provider is not None and provider != "grok":
        die("provider must be grok")
    if provider == "grok" and command:
        die("provider and --cmd cannot be used together")
    if model is not None and provider != "grok":
        die("--model requires --provider grok")
    name = tmux_name(sid)
    raw = row.get("runtime")
    meta = dict(raw) if isinstance(raw, dict) else {}
    command_argv: list[str] | None = None
    start_command = command
    if provider == "grok":
        existing = meta.get("grok_session_id")
        existing_s = existing if isinstance(existing, str) and existing else ""
        if runtime.exists(sid) and not existing_s:
            runtime.stop(sid)
        new_id = grok_new_session_id() if not existing_s else ""
        resolved = grok_model(model)
        command_argv = grok_tmux_command_argv(existing=existing_s, model=resolved, new_id=new_id)
        start_command = None
        if not existing_s:
            meta["grok_session_id"] = new_id
        meta["provider"] = "grok"
        meta["model"] = resolved
    runtime.start(sid, start_command, cols, rows, command_argv=command_argv, cwd=cwd)
    meta["tmux_session"] = name
    meta["control"] = "attached"
    if cols is not None:
        meta["cols"] = cols
    if rows is not None:
        meta["rows"] = rows
    row["runtime"] = meta
    store.write("session", "update", sid, _strip(row))
    return name


def _session_stop(store: Store, runtime: Runtime, sid: str) -> None:
    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    runtime.stop(sid)
    raw = row.get("runtime")
    meta = dict(raw) if isinstance(raw, dict) else {}
    meta["tmux_session"] = meta.get("tmux_session") or tmux_name(sid)
    meta["control"] = "stopped"
    row["runtime"] = meta
    store.write("session", "update", sid, _strip(row))


def _session_keep_working(
    store: Store,
    runtime: Runtime,
    sid: str,
    *,
    once: bool,
) -> None:
    from .keep_working import tick

    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    if row.get("status") != "active":
        die(f"session {sid} is not active")
    if not runtime.available():
        die("tmux is not installed")
    sleep_s = 30

    def _one() -> str:
        fresh = _need(store, "session", sid)
        if fresh.get("status") != "active":
            print(f"keep-working {sid} inactive")
            return "inactive"
        raw = fresh.get("runtime")
        meta = dict(raw) if isinstance(raw, dict) else {}
        kw_raw = meta.get("keep_working")
        state = dict(kw_raw) if isinstance(kw_raw, dict) else {}
        status = tick(runtime, sid, state)
        latest = _need(store, "session", sid)
        latest_meta = dict(latest.get("runtime") or {}) if isinstance(latest.get("runtime"), dict) else {}
        latest_meta["keep_working"] = state
        latest["runtime"] = latest_meta
        store.write("session", "update", sid, _strip(latest))
        print(f"keep-working {sid} {status}")
        return status

    if _one() == "inactive":
        return
    if once:
        return
    while True:
        time.sleep(sleep_s)
        if _one() == "inactive":
            return


def _session_input(
    store: Store,
    runtime: Runtime,
    sid: str,
    data: str | None,
    key: str | None,
) -> None:
    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    if row.get("status") != "active":
        die(f"session {sid} is not active")
    has_data = data is not None
    has_key = key is not None
    if has_data == has_key:
        die("input requires exactly one of --data or --key")
    if has_data:
        runtime.input_text(sid, data or "")
    else:
        runtime.input_key(sid, key or "")


def _session_resize(store: Store, runtime: Runtime, sid: str, cols: int, rows: int) -> None:
    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    runtime.resize(sid, cols, rows)


def apply_control(store: Store, runtime: Runtime, message: dict) -> dict:
    """Execute a hub control frame on this device. Returns a control-ack dict."""
    sid = message.get("session_id")
    action = message.get("action")
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    ack: dict = {
        "type": "control-ack",
        "session_id": sid,
        "action": action,
        "ok": False,
    }
    try:
        if not isinstance(sid, str) or sid == "":
            die("session_id is required")
        if action == "start":
            command = payload.get("command")
            if command is None:
                command = payload.get("cmd")
            if command is not None and not isinstance(command, str):
                die("command must be a string")
            provider = payload.get("provider")
            if provider is not None and not isinstance(provider, str):
                die("provider must be a string")
            model = payload.get("model")
            if model is not None and not isinstance(model, str):
                die("model must be a string")
            cols = payload.get("cols")
            rows = payload.get("rows")
            cols_i = _dim_value(cols, "cols") if cols is not None else None
            rows_i = _dim_value(rows, "rows") if rows is not None else None
            _session_start(
                store,
                runtime,
                sid,
                command,
                cols_i,
                rows_i,
                provider=provider,
                model=model,
            )
        elif action == "stop":
            _session_stop(store, runtime, sid)
        elif action == "input":
            data = payload.get("data")
            key = payload.get("key")
            if data is not None and not isinstance(data, str):
                die("data must be a string")
            if key is not None and not isinstance(key, str):
                die("key must be a string")
            _session_input(store, runtime, sid, data, key)
        elif action == "resize":
            if "cols" not in payload or "rows" not in payload:
                die("resize requires cols and rows")
            cols_i = _dim_value(payload.get("cols"), "cols")
            rows_i = _dim_value(payload.get("rows"), "rows")
            _session_resize(store, runtime, sid, cols_i, rows_i)
        else:
            die(f"unknown control action: {action}")
        ack["ok"] = True
        return ack
    except StoreConnectionError:
        raise  # a lost DB connection must reach cmd_sync's reconnect loop, not become an acked failure
    except (SystemExit, StoreError, ValueError) as exc:
        err = exc.args[0] if getattr(exc, "args", None) else str(exc)
        if isinstance(err, int):
            err = f"exit {err}"
        text = str(err) if err is not None else "error"
        text = text.removeprefix("agent: ")
        ack["error"] = text
        return ack
    except Exception as exc:
        ack["error"] = str(exc) or "error"
        return ack


def _publish_terminals(
    store: Store,
    runtime: Runtime,
    ws: object,
    terminal_seq: dict[str, int],
    last_capture: dict[str, str],
) -> None:
    device = store.device_id()
    for session in store.rows("session"):
        if session.get("_origin_device_id") != device:
            continue
        meta = session.get("runtime") or {}
        if not isinstance(meta, dict) or meta.get("control") != "attached":
            continue
        sid = session["id"]
        text = runtime.capture(sid)
        if last_capture.get(sid) == text:
            continue
        seq = terminal_seq.get(sid, 0) + 1
        data = base64.b64encode(text.encode("utf-8")).decode("ascii")
        frame = {
            "type": "terminal",
            "session_id": sid,
            "seq": seq,
            "data": data,
        }
        ws.send(json.dumps(frame))  # type: ignore[attr-defined]
        last_capture[sid] = text
        terminal_seq[sid] = seq


def _find_round(store: Store, task_id: str, round_num: object) -> dict:
    matches = [
        r
        for r in store.rows("task_round")
        if r.get("task_id") == task_id and r.get("round") == round_num
    ]
    if len(matches) != 1:
        die(f"task_round task={task_id} round={round_num} not found")
    return matches[0]


def _latest_gates(store: Store, task_id: str) -> dict[tuple[str, str], dict]:
    gates = [g for g in store.rows("review_gate") if g.get("task_id") == task_id]
    # rows() is updated_at DESC; reverse for older-first, then stable sort by recorded_at.
    ordered = list(reversed(gates))
    ordered.sort(key=lambda g: g.get("recorded_at") or "")
    latest: dict[tuple[str, str], dict] = {}
    for g in ordered:
        latest[(g.get("stage"), g.get("dimension"))] = g
    return latest


def _latest_checks(store: Store, task_id: str) -> dict[str, dict]:
    checks = [c for c in store.rows("local_check") if c.get("task_id") == task_id]
    ordered = list(reversed(checks))
    ordered.sort(key=lambda c: c.get("ran_at") or "")
    latest: dict[str, dict] = {}
    for c in ordered:
        latest[c["name"]] = c
    return latest


def load_task_dict(store: Store, tid: str) -> dict:
    """Task snapshot for evaluate_allow / ready_for_done_blocking / chain."""
    task = store.row("task", tid)
    if task is None:
        die(f"unknown task: {tid}")
    checklist = {
        str(r["key"]): str(r["status"])
        for r in store.rows("checklist_item")
        if r.get("task_id") == tid
    }
    checks = [c for c in store.rows("local_check") if c.get("task_id") == tid]
    ordered_checks = list(reversed(checks))
    ordered_checks.sort(key=lambda c: c.get("ran_at") or "")
    latest_by_name: dict[str, dict] = {}
    for c in ordered_checks:
        name = c.get("name")
        if name is None:
            continue
        latest_by_name[str(name)] = c
    local_checks = [
        {"name": name, "result": c.get("result")} for name, c in latest_by_name.items()
    ]
    gates_raw = [g for g in store.rows("review_gate") if g.get("task_id") == tid]
    ordered_gates = list(reversed(gates_raw))
    ordered_gates.sort(key=lambda g: g.get("recorded_at") or "")
    gates = [
        {
            "stage": g.get("stage"),
            "dimension": g.get("dimension"),
            "vendor": g.get("vendor"),
            "verdict": g.get("verdict"),
            "head_sha": g.get("head_sha") or "",
        }
        for g in ordered_gates
    ]
    return {
        "id": task["id"],
        "session_id": task.get("session_id"),
        "workflow": task.get("workflow"),
        "state": task.get("state"),
        "checklist": checklist,
        "summaries": {
            "en": task.get("change_summary_en") or "",
            "de": task.get("change_summary_de") or "",
        },
        "gates": gates,
        "local_checks": local_checks,
    }


def load_session_tasks(store: Store, session_id: str) -> list[dict]:
    tasks = [t for t in store.rows("task") if t.get("session_id") == session_id]
    tasks.sort(key=lambda t: str(t.get("id") or ""))
    return [load_task_dict(store, str(t["id"])) for t in tasks]


def _chain_snapshot(store: Store, tid: str, extra_head: str | None = None) -> dict:
    task = load_task_dict(store, tid)
    sid = str(task.get("session_id") or "")
    session = store.row("session", sid) if sid else None
    agents_raw = [a for a in store.rows("agent") if a.get("task_id") == tid]
    agents_ordered = list(reversed(agents_raw))
    agents_ordered.sort(key=lambda a: (a.get("started_at") or "", str(a.get("id") or "")))
    agents = [
        {
            "role": a.get("role"),
            "vendor": a.get("vendor"),
            "status": a.get("status"),
            "note": a.get("note") or "",
        }
        for a in agents_ordered
    ]
    rounds = [r for r in store.rows("task_round") if r.get("task_id") == tid]
    rounds_ordered = list(reversed(rounds))
    rounds_ordered.sort(key=lambda r: (r.get("round") or 0, str(r.get("id") or "")))
    last_round = rounds_ordered[-1] if rounds_ordered else {}
    head = extra_head or ""
    if not head:
        for g in task.get("gates") or []:
            if g.get("head_sha"):
                head = str(g["head_sha"])
    return {
        "session_active": bool(session is not None and session.get("status") == "active"),
        "agents": agents,
        "gates": task.get("gates") or [],
        "local_checks": task.get("local_checks") or [],
        "head_sha": head,
        "implementer_verdict": last_round.get("implementer_verdict") or "",
        "reviewer_verdict": last_round.get("reviewer_verdict") or "",
        "workflow": task.get("workflow"),
        "checklist": task.get("checklist") or {},
        "session_id": sid,
    }


def _require_task_session_active(store: Store, tid: str) -> dict:
    task = _need(store, "task", tid)
    session = _need(store, "session", str(task.get("session_id") or ""))
    _require_owned(store, session, "session")
    _require_owned(store, task, "task")
    if session.get("status") != "active":
        die(f"session {session.get('id')} is not active")
    _require_skill(session, "spine")
    return task


def _assert_ready(store: Store, task: dict) -> None:
    workflow = task.get("workflow")
    if workflow not in CHECKLIST:
        die("unknown workflow")
    tid = str(task["id"])
    items = {
        r["key"]: r
        for r in store.rows("checklist_item")
        if r.get("task_id") == task["id"]
    }
    for key in CHECKLIST[workflow]:
        item = items.get(key)
        status = item["status"] if item else None
        if status == "n_a":
            if key not in N_A_ALLOWED:
                die(f"task is not done: checklist {key}=n_a is not allowed")
            evidence = item.get("evidence") if item else None
            if not isinstance(evidence, str) or evidence == "":
                die(f"task is not done: checklist {key}=n_a requires evidence")
        if key == "mergeable" and status == "ja":
            evidence = item.get("evidence") if item else None
            if not isinstance(evidence, str) or evidence == "":
                die("task is not done: checklist mergeable=ja requires evidence")
    snap = load_task_dict(store, tid)
    blocking = ready_for_done_blocking(snap)
    if not blocking:
        return
    if any(":summary=missing" in b for b in blocking):
        die("task is not done: summaries missing")
    for b in blocking:
        if ":gate:" in b and b.endswith("=missing"):
            # "{tid}:gate:{stage}/{dim}=missing"
            rest = b.split(":gate:", 1)[1]
            pair = rest.rsplit("=", 1)[0]
            die(f"task is not done: missing gate {pair}")
        if ":gate:" in b and "=no_head" in b:
            rest = b.split(":gate:", 1)[1]
            pair = rest.rsplit("=", 1)[0]
            die(f"task is not done: gate {pair} missing head_sha")
        if ":gate:head_sha=mismatch" in b:
            die("task is not done: gate heads must match")
        if ":gate:" in b:
            rest = b.split(":gate:", 1)[1]
            pair, _, detail = rest.partition("=")
            if "/" in detail:
                die(f"task is not done: gate {pair} vendor mismatch")
            die(f"task is not done: gate {pair} not approved")
        if ":local_check:" in b and b.endswith("=fail"):
            name = b.split(":local_check:", 1)[1].rsplit("=", 1)[0]
            die(f"task is not done: local_check {name}=fail")
        if b.endswith(":local_check_pass=ja_without_pass_skip"):
            die("task is not done: local_check_pass=ja requires a pass or skip check")
        if ":deviation_granted=" in b:
            die("task is not done: deviation_declared=ja requires deviation_granted=ja")
        prefix = f"{tid}:"
        if b.startswith(prefix):
            body = b[len(prefix) :]
            if body.startswith("workflow="):
                die("unknown workflow")
            if "=" in body and not body.startswith("gate:") and not body.startswith("local_check"):
                key, _, status = body.partition("=")
                if key not in ("summary",):
                    die(f"task is not done: checklist {key}={status}")
    die(f"task is not done: {', '.join(blocking)}")


def cmd_allow(args: list[str]) -> None:
    """Done-gate: exit 0 allow, exit 2 deny, exit 1 usage."""
    action = flag(args, "--action")
    session_id = flag(args, "--session") or os.environ.get("GROK_SESSION_ID")
    task_id = flag(args, "--task")
    draft = flag(args, "--draft")
    as_json = "--json" in args
    if action is None or action == "":
        die(
            "Usage: agent allow --action claim-done|pr-ready|pr-create|task-done "
            "[--session ID] [--task ID] [--draft true|false] [--json]"
        )
    if action not in ACTIONS:
        die(f"action must be {'|'.join(ACTIONS)}")
    if draft is not None and draft not in ("true", "false"):
        die("--draft must be true|false")
    create_has_draft = draft == "true"

    store = open_store()
    try:
        session_tasks: list[dict] = []
        if action == "task-done":
            if not task_id:
                die("Usage: agent allow --action task-done --task ID")
            else:
                snap = load_task_dict(store, task_id)
                session = _need(store, "session", str(snap.get("session_id") or ""))
                _require_skill(session, "spine")
                session_tasks = [snap]
                if session_id is None:
                    session_id = str(snap.get("session_id") or "") or None
                result = evaluate_allow(
                    action,
                    session_id=session_id,
                    task_id=str(task_id),
                    session_tasks=session_tasks,
                    create_has_draft=create_has_draft,
                )
        elif action == "pr-create":
            result = evaluate_allow(
                action,
                session_id=session_id,
                task_id=task_id,
                session_tasks=[],
                create_has_draft=create_has_draft,
            )
        else:
            if session_id:
                session = _need(store, "session", session_id)
                _require_skill(session, "spine")
                session_tasks = load_session_tasks(store, session_id)
            result = evaluate_allow(
                action,
                session_id=session_id,
                task_id=task_id,
                session_tasks=session_tasks,
                create_has_draft=create_has_draft,
            )
    finally:
        store.close()

    if as_json:
        print(json.dumps(result.to_json(), ensure_ascii=False))
        if not result.allowed:
            extra = result.reason
            if result.blocking:
                extra = f"{result.reason} [{'; '.join(result.blocking)}]"
            print(extra, file=sys.stderr)
            raise SystemExit(2)
        return

    if result.allowed:
        sess = result.session or "-"
        print(f"allow action={result.action} session={sess}")
        return
    print(f"deny action={result.action} reason={result.reason}")
    if result.blocking:
        print(f"  blocking: {', '.join(result.blocking)}", file=sys.stderr)
    raise SystemExit(2)


def cmd_next(args: list[str]) -> None:
    tid = flag(args, "--task")
    as_json = "--json" in args
    if tid is None or tid == "":
        die("Usage: agent next --task ID [--json]")
    store = open_store()
    try:
        _require_task_session_active(store, tid)
        snap = _chain_snapshot(store, tid)
        wf = str(snap["workflow"])
        ready = next_steps(wf, snap["checklist"], spine_only=True)
        if as_json:
            print(
                json.dumps(
                    {
                        "task": tid,
                        "workflow": wf,
                        "next": [step_to_json(s) for s in ready],
                        "handoff": (
                            handoff_prompt(
                                ready[0],
                                task_id=str(tid),
                                session_id=str(snap.get("session_id")),
                            )
                            if len(ready) == 1 and ready[0].kind == "agent"
                            else None
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return
        if not ready:
            print(f"next task={tid} (no open spine steps)")
            return
        for s in ready:
            line = (
                f"next task={tid} {s.key} kind={s.kind} source={required_source(s)}"
            )
            if s.role:
                line += f" role={s.role} vendor={s.vendor}"
            print(line)
        if len(ready) == 1 and ready[0].kind == "agent":
            sys.stdout.write(
                handoff_prompt(
                    ready[0],
                    task_id=str(tid),
                    session_id=str(snap.get("session_id")),
                )
            )
    finally:
        store.close()


def cmd_close_step(args: list[str]) -> None:
    tid = flag(args, "--task")
    key = flag(args, "--key")
    source = flag(args, "--source")
    evidence = flag(args, "--evidence")
    status = flag(args, "--status") or "ja"
    head = flag(args, "--head")
    if not tid or not key or not source or evidence is None or evidence == "":
        die(
            "Usage: agent close-step --task ID --key KEY --source script|human|runner "
            "--evidence TEXT [--status ja|n_a] [--head SHA]"
        )
    if status not in ("ja", "n_a"):
        die("close-step --status must be ja|n_a")
    if status == "n_a" and key not in N_A_ALLOWED:
        die(f"n_a is not allowed for {key}")
    if source not in ("script", "human", "runner"):
        die("source must be script|human|runner")
    chain_source = "script" if source == "runner" else source
    store = open_store()
    try:
        _require_task_session_active(store, tid)
        snap = _chain_snapshot(store, tid, extra_head=head)
        wf = str(snap["workflow"])
        verdict = close_allowed(
            wf,
            key,
            checklist=snap["checklist"],
            source=chain_source,
            evidence=evidence,
            snapshot=snap,
        )
        if not verdict.allowed:
            die(verdict.reason)
    finally:
        store.close()
    set_args = [
        "set",
        "--task",
        tid,
        "--key",
        key,
        "--status",
        status,
        "--source",
        source,
        "--evidence",
        evidence,
    ]
    cmd_checklist(set_args)


def _exec_argv(argv: list[str], *, cwd: str | None = None) -> "Completed":
    from .runtime import Completed
    import subprocess

    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        return Completed(127, "", str(exc))
    return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


def _resolve_run_cwd(args: list[str]) -> str:
    cwd_flag = flag(args, "--cwd")
    cwd = cwd_flag if cwd_flag is not None else os.getcwd()
    path = Path(cwd).resolve()
    if not path.is_dir():
        die(f"--cwd is not a directory: {cwd}")
    return str(path)


def _find_working_agent(
    store: Store,
    tid: str,
    *,
    role: str,
    vendor: str,
    round_num: int | None,
) -> dict | None:
    for agent in store.rows("agent"):
        if agent.get("task_id") != tid:
            continue
        if agent.get("role") != role:
            continue
        if agent.get("vendor") != vendor:
            continue
        if agent.get("status") != "working":
            continue
        if round_num is not None and agent.get("round") != round_num:
            continue
        return agent
    return None


def _agent_handoff_exit(step, tid: str, session_id: str | None) -> None:
    sys.stdout.write(handoff_prompt(step, task_id=str(tid), session_id=session_id))
    print(
        f"agent: agent step. After the lane: close-step --task {tid} "
        f"--key {step.key} --source script --evidence …",
        file=sys.stderr,
    )
    raise SystemExit(2)


def cmd_run(args: list[str]) -> None:
    tid = flag(args, "--task")
    head = flag(args, "--head")
    dry = "--dry-run" in args
    spec_file = flag(args, "--spec-file")
    # v1: always one spine step (--once implied).
    if tid is None or tid == "":
        die(
            "Usage: agent run --task ID [--dry-run] [--head SHA] "
            "[--cwd PATH] [--spec-file PATH] [--no-tmux]"
        )
    close_key: str | None = None
    close_evidence: str | None = None
    evidence: str | None = None
    store = open_store()
    try:
        task = _require_task_session_active(store, tid)
        snap = _chain_snapshot(store, tid, extra_head=head)
        wf = str(snap["workflow"])
        ready = next_steps(wf, snap["checklist"], spine_only=True)
        if not ready:
            print(f"run task={tid} idle")
            return
        step = ready[0]
        print(
            f"run task={tid} {step.key} kind={step.kind} source={required_source(step)}"
        )
        if dry:
            if step.kind == "agent":
                sys.stdout.write(
                    handoff_prompt(
                        step, task_id=str(tid), session_id=str(snap.get("session_id"))
                    )
                )
            return
        if step.kind == "human":
            print(
                f"agent: human must close {step.key} (close-step --source human)",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if step.key == "pushed":
            cwd = _resolve_run_cwd(args)
            from .git_act import GitActError, push_branch

            try:
                sha = push_branch(
                    cwd=cwd, runner=lambda argv: _exec_argv(argv, cwd=cwd)
                )
            except GitActError as exc:
                die(str(exc))
            if head is not None:
                want = head.lower()
                if want != sha and not (
                    7 <= len(want) < len(sha) and sha.startswith(want)
                ):
                    die(f"--head {head} does not match pushed sha {sha}")
            head = sha
            snap = _chain_snapshot(store, tid, extra_head=head)

        if step.key == "mergeable":
            cwd = _resolve_run_cwd(args)
            from .git_act import GitActError, measure_mergeable

            try:
                expected = str(snap.get("head_sha") or head or "").strip() or None
                evidence = measure_mergeable(
                    cwd=cwd,
                    runner=lambda argv: _exec_argv(argv, cwd=cwd),
                    expected_head=expected,
                )
            except GitActError as exc:
                die(str(exc))
        if step.key in NO_AUTO_CLOSE:
            print(
                f"agent: {step.key} is not auto-closable — "
                "close-step --source script --evidence …",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if step.key == "local_check_pass" and not snap["local_checks"]:
            cwd = _resolve_run_cwd(args)
            env_cmd = os.environ.get("AGENT_CHECK_COMMAND")
            if env_cmd is None:
                command = "pytest -q"
            elif env_cmd == "":
                die("AGENT_CHECK_COMMAND is set but empty")
            else:
                command = env_cmd
            argv = shlex.split(command)
            if not argv:
                die("check command is empty")
            completed = _exec_argv(argv, cwd=cwd)
            result = "pass" if completed.returncode == 0 else "fail"
            output = ((completed.stdout or "") + (completed.stderr or ""))[:8000]
            cmd_check(
                [
                    "record",
                    "--task",
                    tid,
                    "--name",
                    "local",
                    "--command",
                    command,
                    "--result",
                    result,
                    "--output",
                    output or "(no output)",
                ]
            )
            if result == "fail":
                raise SystemExit(2)
            snap = _chain_snapshot(store, tid, extra_head=head)
        if step.kind == "agent":
            already = close_allowed(
                wf,
                step.key,
                checklist=snap["checklist"],
                source="script",
                evidence="run auto",
                snapshot=snap,
            )
            if already.allowed:
                close_key = step.key
                close_evidence = f"run auto:{already.reason}"
            elif spec_file is not None:
                spec_path = Path(spec_file)
                if not spec_path.is_file():
                    die(f"spec-file not found: {spec_file}")
                if not spec_path.read_text(encoding="utf-8").strip():
                    die(f"spec-file is empty: {spec_file}")
                cwd = _resolve_run_cwd(args)
                tmux = "--no-tmux" not in args
                role = str(step.role or "")
                vendor = str(step.vendor or "")
                session_id = str(snap.get("session_id") or "")
                current_round = int(task.get("current_round") or 0)
                round_num: int | None = None
                if role in ("implementer", "reviewer"):
                    round_num = current_round
                working = _find_working_agent(
                    store, tid, role=role, vendor=vendor, round_num=round_num
                )
                if working is None:
                    start_args = [
                        "start",
                        "--session",
                        session_id,
                        "--task",
                        tid,
                        "--role",
                        role,
                        "--vendor",
                        vendor,
                    ]
                    if round_num is not None:
                        start_args.extend(["--round", str(round_num)])
                    cmd_agent(start_args)
                result = launch(
                    role=role,
                    vendor=vendor,
                    spec_file=spec_file,
                    cwd=cwd,
                    tmux=tmux,
                )
                print(
                    f"lane role={result.role} vendor={result.vendor} "
                    f"STATUS={result.status} rc={result.returncode}"
                )
                if role == "implementer" and result.status == "complete":
                    working = _find_working_agent(
                        store, tid, role=role, vendor=vendor, round_num=round_num
                    )
                    if working is None:
                        die("implementer working agent not found after lane")
                    cmd_agent(
                        [
                            "finish",
                            "--id",
                            str(working["id"]),
                            "--verdict",
                            "done",
                            "--note",
                            "lane STATUS=complete",
                        ]
                    )
                    snap = _chain_snapshot(store, tid, extra_head=head)
                    task = _need(store, "task", tid)
                else:
                    _agent_handoff_exit(step, tid, str(snap.get("session_id")))
            else:
                _agent_handoff_exit(step, tid, str(snap.get("session_id")))
        if close_key is None:
            close_ev = (
                evidence
                if step.key == "mergeable"
                else "run auto"
            )
            verdict = close_allowed(
                wf,
                step.key,
                checklist=snap["checklist"],
                source="script",
                evidence=close_ev,
                snapshot=snap,
            )
            if not verdict.allowed:
                print(
                    f"agent: script step {step.key} not closable: {verdict.reason}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            close_key = step.key
            close_evidence = (
                evidence
                if step.key == "mergeable"
                else f"run auto:{verdict.reason}"
            )
    finally:
        store.close()
    close_args = [
        "--task",
        tid,
        "--key",
        str(close_key),
        "--source",
        "script",
        "--evidence",
        str(close_evidence),
    ]
    if head:
        close_args.extend(["--head", head])
    cmd_close_step(close_args)


def cmd_github(args: list[str]) -> None:
    if not args or args[0] != "pending":
        die("Usage: agent github pending")
    from .github_act import scan_github

    store = open_store()
    try:
        lines = scan_github(store, run_argv)
        if not lines:
            print("github pending none")
            return
        for line in lines:
            print(line)
    finally:
        store.close()


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _load_json_file(path: Path) -> Any:
    if not path.is_file():
        die(f"file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        die(f"invalid JSON: {path}")
    return raw


def cmd_query(args: list[str]) -> None:
    if "--match-file" not in args:
        die("Usage: agent query --match-file PATH")
    path = Path(require_flag(args, "--match-file"))
    match = _load_json_file(path)
    if not isinstance(match, dict):
        die("match file must contain a JSON object")
    store = open_store()
    try:
        hub = _hub_from_store(store)
        try:
            _print_json(hub.query(match))
        finally:
            hub.close()
    finally:
        store.close()


def cmd_subscribe(args: list[str]) -> None:
    if not args or args[0] not in ("list", "set", "clear"):
        die("Usage: agent subscribe list|set --file PATH|clear")
    sub = args[0]
    rest = args[1:]
    store = open_store()
    try:
        hub = _hub_from_store(store)
        try:
            if sub == "list":
                if rest:
                    die("Usage: agent subscribe list|set --file PATH|clear")
                _print_json(hub.get_subscriptions())
                return
            if sub == "clear":
                if rest:
                    die("Usage: agent subscribe list|set --file PATH|clear")
                _print_json(hub.put_subscriptions([]))
                return
            if "--file" not in rest:
                die("Usage: agent subscribe list|set --file PATH|clear")
            path = Path(require_flag(rest, "--file"))
            raw = _load_json_file(path)
            if isinstance(raw, dict):
                subscriptions = raw.get("subscriptions")
                if not isinstance(subscriptions, list):
                    die('subscribe file must be a list or {"subscriptions": [...]}')
            elif isinstance(raw, list):
                subscriptions = raw
            else:
                die('subscribe file must be a list or {"subscriptions": [...]}')
            _print_json(hub.put_subscriptions(subscriptions))
        finally:
            hub.close()
    finally:
        store.close()


def cmd_mail(args: list[str]) -> None:
    if not args or args[0] not in ("pending", "ingest"):
        die("Usage: agent mail pending|ingest")
    from .mail_act import scan_mail, scan_mail_ingest

    store = open_store()
    try:
        if args[0] == "pending":
            lines = scan_mail(store, run_argv)
            if not lines:
                print("mail pending none")
                return
            for line in lines:
                print(line)
            return
        lines = scan_mail_ingest(store, run_argv)
        if not lines:
            print("mail ingest none")
            return
        for line in lines:
            print(line)
    finally:
        store.close()


def cmd_lane(args: list[str]) -> None:
    if not args or args[0] != "run":
        die(
            "Usage: agent lane run --role ROLE --vendor grok|codex "
            "--spec-file PATH [--cwd PATH] [--dry-run] [--no-tmux]"
        )
    rest = args[1:]
    role = require_flag(rest, "--role")
    vendor = require_flag(rest, "--vendor")
    spec_file = require_flag(rest, "--spec-file")
    cwd = flag(rest, "--cwd") or os.getcwd()
    dry_run = "--dry-run" in rest
    tmux = "--no-tmux" not in rest
    if role not in LANE_ROLES:
        die(f"role must be {'|'.join(LANE_ROLES)}")
    if vendor not in LANE_VENDORS:
        die("vendor must be grok|codex")
    result = launch(
        role=role,
        vendor=vendor,
        spec_file=spec_file,
        cwd=cwd,
        dry_run=dry_run,
        tmux=tmux,
    )
    if dry_run:
        print(" ".join(result.argv))
        return
    print(
        f"lane role={result.role} vendor={result.vendor} "
        f"STATUS={result.status} rc={result.returncode}"
    )
    if result.status != "complete":
        raise SystemExit(2)


def cmd_knock(args: list[str]) -> None:
    once = "--once" in args
    store = open_store()
    try:
        runtime = Runtime()
        if once:
            for activity_id, status in knock_drain(store, runtime):
                print(f"knock {activity_id} {status}")
            return
        from .pending import scan_pending
        from .runtime import run_argv

        last_poll: float | None = None
        while True:
            if usage_poll_due(last_poll, time.monotonic()):
                try:
                    usage_id = scan_usage(store)
                    if usage_id:
                        print(f"usage.snapshot {usage_id}")
                except AuthStale:
                    pass
                except StoreError as exc:
                    print(f"usage.snapshot error: {exc}", file=sys.stderr)
                try:
                    created, skipped = scan_merged(store, run_argv)
                    for activity_id in created:
                        print(f"pr.merged {activity_id}")
                    if skipped:
                        print(f"watch skipped {skipped} pr.open rows", file=sys.stderr)
                except StoreError as exc:
                    print(f"pr.merged error: {exc}", file=sys.stderr)
                hub_url = store.meta("hub_url")
                hub_token = store.meta("device_token")
                if hub_url and hub_token:
                    hub = Hub(hub_url, hub_token)
                    try:
                        lines = scan_pending(store, hub)
                        for line in lines:
                            print(line)
                    except (HubError, StoreError) as exc:
                        print(f"pending error: {exc}", file=sys.stderr)
                    finally:
                        hub.close()
                from .github_act import scan_github
                from .mail_act import scan_mail

                try:
                    for line in scan_github(store, run_argv):
                        print(line)
                except StoreError as exc:
                    print(f"github pending error: {exc}", file=sys.stderr)
                try:
                    for line in scan_mail(store, run_argv):
                        print(line)
                except StoreError as exc:
                    print(f"mail pending error: {exc}", file=sys.stderr)
                from .errors import config_path, default_fetch, scan_errors

                if config_path(store.home).is_file():
                    try:
                        created, enriched = scan_errors(store, default_fetch)
                        for activity_id in created:
                            print(f"error.seen {activity_id}")
                        for activity_id in enriched:
                            print(f"error.seen enrich {activity_id}")
                    except StoreError as exc:
                        print(f"error.seen error: {exc}", file=sys.stderr)
                from .error_fix_act import scan_error_fix

                try:
                    for line in scan_error_fix(store, run_argv):
                        print(line)
                except StoreError as exc:
                    print(f"error.fix error: {exc}", file=sys.stderr)
                last_poll = time.monotonic()
            activity_id = knock_listen(store, runtime, timeout=30.0)
            if activity_id:
                print(f"knock {activity_id}")
    finally:
        store.close()


def cmd_daemon(args: list[str]) -> None:
    from .daemon import (
        agent_argv,
        install_and_start_service,
        run_supervisor,
        uninstall_service,
    )

    if args == []:
        run_supervisor(home=home(), argv_prefix=agent_argv())
        return
    if args == ["--install"]:
        store = open_store()
        try:
            install_and_start_service(home=store.home, program=[*agent_argv(), "daemon"])
        finally:
            store.close()
        return
    if args == ["--uninstall"]:
        uninstall_service(home=home())
        return
    die("Usage: agent daemon [--install|--uninstall]")


def cmd_watch(args: list[str]) -> None:
    if not args or args[0] not in (
        "pr-merged",
        "pending",
        "assigned",
        "grok-usage",
        "errors",
        "error-fix",
    ):
        die(
            "Usage: agent watch "
            "pr-merged|pending|assigned [--follow]|grok-usage|errors|error-fix"
        )
    store = open_store()
    try:
        if args[0] == "pr-merged":
            from .runtime import run_argv

            created, skipped = scan_merged(store, run_argv)
            for activity_id in created:
                print(f"pr.merged {activity_id}")
            if skipped:
                die(f"watch skipped {skipped} pr.open rows")
            if not created:
                print("pr.merged none")
            return
        if args[0] == "grok-usage":
            try:
                activity_id = scan_usage(store)
            except AuthStale:
                print("usage.snapshot skipped")
                return
            if activity_id:
                print(f"usage.snapshot {activity_id}")
            else:
                print("usage.snapshot none")
            return
        if args[0] == "assigned":
            from .knock import deliver
            from .runtime import run_argv

            extra = args[1:]
            if extra not in ([], ["--follow"]):
                die(
                    "Usage: agent watch "
                    "pr-merged|pending|assigned [--follow]|grok-usage|errors|error-fix"
                )
            follow = extra == ["--follow"]
            while True:
                created, skipped = scan_assigned(store, run_argv, now=utcnow())
                workspace_root = assigned_workspace_root(store)
                sid = assigned_session_id(store.home)
                pending = pending_assigned(store, sid)
                if pending:
                    head_id = pending[0].get("id")
                    if isinstance(head_id, str) and head_id:
                        dispatch_assigned(
                            store,
                            head_id,
                            sync=lambda: _sync_once(store),
                            start=lambda session_id, cwd: _session_start(
                                store,
                                Runtime(),
                                session_id,
                                None,
                                None,
                                None,
                                provider="grok",
                                cwd=str(cwd),
                            ),
                            knock=lambda activity_id: deliver(store, Runtime(), activity_id),
                            workspace_root=workspace_root,
                            pane_up=lambda session_id: Runtime().exists(session_id),
                        )
                for activity_id in created:
                    print(f"issue.assigned {activity_id}")
                if not created:
                    print("assigned none")
                if skipped > 0:
                    msg = f"watch skipped {skipped} assigned issues"
                    if follow:
                        print(msg, file=sys.stderr)
                    else:
                        die(msg)
                if not follow:
                    return
                time.sleep(30)
        if args[0] == "errors":
            from .errors import default_fetch, scan_errors

            created, enriched = scan_errors(store, default_fetch)
            for activity_id in created:
                print(f"error.seen {activity_id}")
            for activity_id in enriched:
                print(f"error.seen enrich {activity_id}")
            return
        if args[0] == "error-fix":
            from .error_fix_act import scan_error_fix
            from .runtime import run_argv

            lines = scan_error_fix(store, run_argv)
            for line in lines:
                print(line)
            return
        from .pending import scan_pending

        hub = _hub_from_store(store)
        try:
            lines = scan_pending(store, hub)
        finally:
            hub.close()
        if not lines:
            print("pending none")
            return
        for line in lines:
            print(line)
    finally:
        store.close()


def cmd_supervise(args: list[str]) -> None:
    import fcntl

    from .knock import deliver
    from .supervise import FOLLOW_SECONDS, SESSION_RE, enqueue_assigned, tick

    if "--session" not in args:
        die("Usage: agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]")
    sid = require_flag(args, "--session")
    if SESSION_RE.match(sid) is None:
        die("session id may contain only A-Za-z0-9_-")
    repo = flag(args, "--repo")
    number_raw = flag(args, "--number")
    if (repo is None) != (number_raw is None):
        die("--repo and --number must be used together")
    follow = "--follow" in args
    if "--once" in args and follow:
        die("use only one of --once or --follow")
    store = open_store()
    lock_fh = None
    try:
        if follow:
            lock_path = store.home / f"supervise-{sid}.lock"
            lock_fh = lock_path.open("w")
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_fh.close()
                die(f"supervise --follow already running for session {sid}")
            lock_fh.write(str(os.getpid()))
            lock_fh.flush()
        runtime = Runtime()
        from .grok_pane import grok_pane_is_working
        from .telegram_act import reset_idle_clock

        startup_working = runtime.exists(sid) and grok_pane_is_working(runtime.capture(sid))
        reset_idle_clock(store, working=startup_working)

        def start(session_id: str, cwd: Path) -> None:
            _session_start(
                store,
                runtime,
                session_id,
                None,
                None,
                None,
                provider="grok",
                cwd=str(cwd),
            )

        queued = False
        while True:
            if repo is not None and number_raw is not None and not queued:
                try:
                    number = int(number_raw)
                except ValueError:
                    die("--number must be a positive integer")
                assigned_id = enqueue_assigned(store, sid, repo, number, run_argv)
                print(f"issue.assigned {assigned_id}")
                queued = True
            pane = runtime.capture(sid) if runtime.exists(sid) else ""
            from .grok_pane import grok_pane_is_working
            from .telegram_act import notify_status

            working = grok_pane_is_working(pane)
            line = tick(
                store,
                runtime,
                sid,
                start=start,
                knock=lambda activity_id: deliver(store, runtime, activity_id),
                pane=pane,
                working=working,
            )
            print(line)
            try:
                # Idle prompt is not "not working". Page only if tmux is gone.
                posted = notify_status(
                    store,
                    sid,
                    line,
                    working=runtime.exists(sid),
                )
                if posted == "telegram sent":
                    print(posted)
            except RuntimeError as exc:
                print(f"telegram error: {exc}", file=sys.stderr)
            if not follow:
                return
            time.sleep(FOLLOW_SECONDS)
    finally:
        if lock_fh is not None:
            lock_fh.close()
        store.close()


COMMANDS = {
    "init": cmd_init,
    "session": cmd_session,
    "skills": cmd_skills,
    "activity": cmd_activity,
    "task": cmd_task,
    "checklist": cmd_checklist,
    "round": cmd_round,
    "agent": cmd_agent,
    "check": cmd_check,
    "gate": cmd_gate,
    "work": cmd_work,
    "allow": cmd_allow,
    "next": cmd_next,
    "close-step": cmd_close_step,
    "run": cmd_run,
    "pair": cmd_pair,
    "sync": cmd_sync,
    "restore": cmd_restore,
    "ping": cmd_ping,
    "status": cmd_status,
    "dashboard": cmd_dashboard,
    "daemon": cmd_daemon,
    "knock": cmd_knock,
    "lane": cmd_lane,
    "watch": cmd_watch,
    "supervise": cmd_supervise,
    "github": cmd_github,
    "query": cmd_query,
    "subscribe": cmd_subscribe,
    "mail": cmd_mail,
}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        die(
            "Usage: agent <init|session|skills|activity|task|checklist|round|agent|check|gate|work|"
            "allow|next|close-step|run|pair|sync|restore|ping|status|dashboard|daemon|knock|lane|watch|"
            "github|query|subscribe|mail|supervise> …"
        )
    cmd = args[0]
    if cmd not in COMMANDS:
        die(f"unknown command: {cmd}")
    try:
        COMMANDS[cmd](args[1:])
    except (StoreError, HubError) as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
