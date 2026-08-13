"""CLI for the local agent ledger."""

from __future__ import annotations

import json
import os
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
        die("Usage: agent checklist set --task ID --key KEY --status ja|nein|n_a|pending --source human|runner|script")
    rest = args[1:]
    tid = require_flag(rest, "--task")
    key = require_flag(rest, "--key")
    status = require_flag(rest, "--status")
    source = require_flag(rest, "--source")
    if status not in ("ja", "nein", "n_a", "pending"):
        die("status must be ja|nein|n_a|pending")
    if source not in ("human", "runner", "script"):
        die("source must be human|runner|script")
    store = open_store()
    try:
        items = [r for r in store.rows("checklist_item") if r.get("task_id") == tid and r.get("key") == key]
        if len(items) != 1:
            die(f"checklist {key} for task {tid} not found")
        item = items[0]
        item["status"] = status
        item["source"] = source
        item["evidence"] = flag(rest, "--evidence")
        item["updated_at"] = utcnow()
        store.write("checklist_item", "update", item["id"], _strip(item))
        print(f"checklist {key}={status}")
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
        while True:
            time.sleep(1)
            _sync_once(store)
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
        print(
            f"device={data['device_id']} login={data['login'] or '-'} "
            f"tasks_open={len(open_tasks)} pings={len(data['pings'])}"
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


def _assert_ready(store: Store, task: dict) -> None:
    workflow = task.get("workflow")
    if workflow not in CHECKLIST:
        die("unknown workflow")
    if not task.get("change_summary_en") or not task.get("change_summary_de"):
        die("task is not done: summaries missing")
    items = {r["key"]: r["status"] for r in store.rows("checklist_item") if r.get("task_id") == task["id"]}
    for key in CHECKLIST[workflow]:
        status = items.get(key)
        if status not in ("ja", "n_a"):
            die(f"task is not done: checklist {key}={status}")


COMMANDS = {
    "init": cmd_init,
    "session": cmd_session,
    "task": cmd_task,
    "checklist": cmd_checklist,
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
        die("Usage: agent <init|session|task|checklist|pair|sync|restore|ping|status|dashboard> …")
    cmd = args[0]
    if cmd not in COMMANDS:
        die(f"unknown command: {cmd}")
    try:
        COMMANDS[cmd](args[1:])
    except (StoreError, HubError) as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
