"""CLI for the local agent ledger."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sys
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .hub import Hub, HubError
from .store import Store, StoreError, utcnow

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


def open_store() -> Store:
    return Store(home() / "ledger.sqlite")


def flag(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    idx = args.index(name)
    if idx + 1 >= len(args):
        die(f"{name} needs a value")
    return args[idx + 1]


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
    if msg_type == "events":
        return True
    if msg_type == "ping" and "id" in message:
        return True
    return False


def cmd_init(_: list[str]) -> None:
    store = open_store()
    print(f"ok  device={store.device_id()} db={store.path}")
    store.close()


def cmd_session(args: list[str]) -> None:
    if not args:
        die("Usage: agent session register|heartbeat|list|close")
    store = open_store()
    try:
        sub, rest = args[0], args[1:]
        if sub == "register":
            sid = require_flag(rest, "--id")
            kind = require_flag(rest, "--kind")
            if kind not in ("human", "runner", "other"):
                die("kind must be human|runner|other")
            existing = store.row("session", sid)
            if existing is not None:
                if existing.get("kind") != kind:
                    die(f"session {sid} is already kind={existing.get('kind')}")
                if existing.get("status") == "closed":
                    die(f"session {sid} is closed")
                existing["last_seen_at"] = utcnow()
                existing["status"] = "active"
                existing["host"] = socket.gethostname()
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
            row["status"] = "closed"
            row["last_seen_at"] = utcnow()
            store.write("session", "update", sid, _strip(row))
            print(f"closed {sid}")
            return
        die(f"unknown session command: {sub}")
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
            if workflow not in CHECKLIST:
                die("workflow must be implement|review|resolve-conflicts")
            session = _need(store, "session", session_id)
            if session.get("status") != "active":
                die(f"session {session_id} is not active")
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
            for row in store.rows("task"):
                if sid and row.get("session_id") != sid:
                    continue
                print(f"{row['id']}  {row['workflow']}  {row['state']}  r{row['current_round']}  {row['title']}")
            return
        if sub == "show":
            if len(rest) != 1:
                die("Usage: agent task show <id>")
            print(json.dumps(_need(store, "task", rest[0]), indent=2))
            return
        if sub == "state":
            if len(rest) != 2:
                die("Usage: agent task state <id> <state>")
            tid, state = rest
            if state not in TASK_STATES:
                die("unknown state")
            row = _need(store, "task", tid)
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
            "--source human|runner|script"
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
        workflow = task.get("workflow")
        if workflow not in ("implement", "resolve-conflicts"):
            die("round start requires workflow implement|resolve-conflicts")
        if task.get("state") == "done":
            die("cannot start a round on a done task")
        current = int(task.get("current_round") or 0)
        for agent in store.rows("agent"):
            if (
                agent.get("task_id") == tid
                and agent.get("round") == current
                and agent.get("status") == "working"
            ):
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
            role = agent.get("role")
            task = _need(store, "task", agent["task_id"])
            session = _need(store, "session", task["session_id"])
            if session.get("status") != "active":
                die("session is not active")
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
                tr["implementer_verdict"] = verdict
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
                tr["reviewer_verdict"] = verdict
                tr["finished_at"] = utcnow()
                store.write("task_round", "update", tr["id"], _strip(tr))
                task["state"] = "local-check" if verdict == "approved" else "implementing"
                task["updated_at"] = utcnow()
                store.write("task", "update", task["id"], _strip(task))
            elif role in ("pr-reviewer-quality", "pr-reviewer-logic"):
                if verdict not in ("approved", "rejected"):
                    die("pr-reviewer verdict must be approved|rejected")
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


def cmd_gate(args: list[str]) -> None:
    if not args or args[0] != "record":
        die(
            "Usage: agent gate record --task UUID --stage grok-pr|codex-pr "
            "--dimension quality|logic --vendor grok|codex --verdict approved|rejected "
            "--head SHA --agent UUID"
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
    if not re.fullmatch(r"[0-9a-f]{7,40}", head):
        die("--head must be a git SHA (lowercase hex, length 7–40)")
    store = open_store()
    try:
        task = _need(store, "task", tid)
        session = _need(store, "session", task["session_id"])
        if session.get("status") != "active":
            die(f"session {task['session_id']} is not active")
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
        if (
            verdict == "rejected"
            and task.get("workflow") in ("implement", "resolve-conflicts")
            and task.get("state") == "done"
        ):
            die("cannot reject a gate on a done task")
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
        print(f"gate {stage}/{dimension}={verdict}")
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
            for row in store.rows("open_work"):
                if sid and row.get("session_id") != sid:
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


def cmd_sync(args: list[str]) -> None:
    follow = "--follow" in args
    store = open_store()
    try:
        _sync_once(store)
        if not follow:
            return
        hub = _hub_from_store(store)
        try:
            try:
                ws = hub.connect_sync_ws()
            except HubError as exc:
                die(str(exc))
            try:
                for raw in ws:
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    if should_sync_on_ws(message):
                        _sync_once(store)
            except HubError as exc:
                die(str(exc))
            except Exception as exc:
                die(f"websocket error: {exc}")
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            die("websocket closed")
        finally:
            hub.close()
    finally:
        store.close()


def cmd_restore(_: list[str]) -> None:
    store = open_store()
    try:
        hub = _hub_from_store(store)
        try:
            body = hub.restore()
        finally:
            hub.close()
        if body.get("device_id") != store.device_id():
            die("restore device_id does not match this device")
        events = body.get("events") or body.get("own_events") or []
        for event in events:
            store.apply_remote(event)
            store.mark_origin(event["origin_device_id"], int(event["origin_seq"]))
        print(f"restored events={len(events)}")
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
            if isinstance(payload, dict):
                store.apply_replica_row(
                    {
                        "table": "ping",
                        "row_id": pid,
                        "origin_device_id": row["_origin_device_id"] if row else "web",
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

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                body = json.dumps(store.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
        print(f"sync pushed={len(pending)} pulled={len(events)}")
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


def _strip(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


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


def _assert_ready(store: Store, task: dict) -> None:
    workflow = task.get("workflow")
    if workflow not in CHECKLIST:
        die("unknown workflow")
    if not task.get("change_summary_en") or not task.get("change_summary_de"):
        die("task is not done: summaries missing")
    items = {
        r["key"]: r
        for r in store.rows("checklist_item")
        if r.get("task_id") == task["id"]
    }
    for key in CHECKLIST[workflow]:
        item = items.get(key)
        status = item["status"] if item else None
        if status not in ("ja", "n_a"):
            die(f"task is not done: checklist {key}={status}")
        if status == "n_a" and key not in N_A_ALLOWED:
            die(f"task is not done: checklist {key}=n_a is not allowed")
    declared = items.get("deviation_declared")
    granted = items.get("deviation_granted")
    if declared is not None and declared.get("status") == "ja":
        if granted is None or granted.get("status") != "ja":
            die("task is not done: deviation_declared=ja requires deviation_granted=ja")
    latest_checks = _latest_checks(store, task["id"])
    for name, check in latest_checks.items():
        if check.get("result") == "fail":
            die(f"task is not done: local_check {name}=fail")
    local_pass = items.get("local_check_pass")
    if local_pass is not None and local_pass.get("status") == "ja":
        ok = any(c.get("result") in ("pass", "skip") for c in latest_checks.values())
        if not ok:
            die("task is not done: local_check_pass=ja requires a pass or skip check")
    latest_gates = _latest_gates(store, task["id"])
    heads: set[str] = set()
    for stage, dimension, vendor in GATE_PAIRS:
        g = latest_gates.get((stage, dimension))
        if g is None:
            die(f"task is not done: missing gate {stage}/{dimension}")
        if g.get("vendor") != vendor:
            die(f"task is not done: gate {stage}/{dimension} vendor mismatch")
        if g.get("verdict") != "approved":
            die(f"task is not done: gate {stage}/{dimension} not approved")
        head = g.get("head_sha") or ""
        if not head:
            die(f"task is not done: gate {stage}/{dimension} missing head_sha")
        heads.add(head)
    if len(heads) != 1:
        die("task is not done: gate heads must match")


COMMANDS = {
    "init": cmd_init,
    "session": cmd_session,
    "task": cmd_task,
    "checklist": cmd_checklist,
    "round": cmd_round,
    "agent": cmd_agent,
    "check": cmd_check,
    "gate": cmd_gate,
    "work": cmd_work,
    "pair": cmd_pair,
    "sync": cmd_sync,
    "restore": cmd_restore,
    "ping": cmd_ping,
    "status": cmd_status,
    "dashboard": cmd_dashboard,
}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        die(
            "Usage: agent <init|session|task|checklist|round|agent|check|gate|work|"
            "pair|sync|restore|ping|status|dashboard> …"
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
