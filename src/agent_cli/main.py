"""CLI for the local session store."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import socket
import sys
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed, WebSocketException

from .ai_accounts import (
    AISubscription,
    AccountError as AIAccountError,
    load_ai_accounts,
    load_ai_subscriptions,
    load_driver_configuration,
    load_model_configuration,
    load_recorded_names,
)
from .allow import (
    ACTIONS,
    GATE_PAIRS,
    GATE_UNAVAILABLE_KEYS,
    SECOND_REVIEW_KEYS,
    SECOND_REVIEW_NOT_POSTED,
    TERMINAL_STATES,
    both_second_reviews_na,
    evaluate_allow,
    ready_for_done_blocking,
    requires_evidence,
    second_review_na,
    stored_names_hint,
)
from .chain import (
    CLOSE_STATUSES,
    NO_AUTO_CLOSE,
    close_allowed,
    handoff_prompt,
    next_steps,
    required_source,
    step_vendor,
    step_vendor_label,
    steps_for,
    to_json as step_to_json,
)
from .coordinator_common import parse_model_result, redact as redact_secrets
from .drivers import (
    DRIVERS_NOT_CONFIGURED,
    SESSION_ENV_NOT_CONFIGURED,
    DriverSpec,
    PaneMarkers,
    declared_driver_ids,
    driver_refusal,
    lane_driver_ids,
    lane_scrub_env,
)
from .gate_order import latest_gates as durable_latest_gates
from .gate_order import ordered_snapshot_gates
from .github_act import _repo_ok
from .hub import Hub, HubError
from .knock import drain as knock_drain
from .knock import listen_once as knock_listen
from .lane import (
    LANE_ROLES,
    LaneResult,
    launch,
    record_lane_run,
    redact_diagnostic,
    redact_lane_text,
)
from .local_ci import LocalCiError, parse_comment, parse_payload, render_block, verify_comment
from .model_slots import (
    EXTERNAL_SECOND_TASK_CREATION_ERROR,
    SLOT_STAGES,
    STAGE_CHOICES,
    STAGE_SLOTS,
    TASK_MODELS_NOT_CONFIGURED,
    ModelSlots,
    ModelSlotsError,
    PinnedModelSlotsError,
    UnrecordedSlotsError,
    canonical_checklist,
    canonical_key,
    canonical_stage,
    implementer_for_new_record,
    install_recorded_names,
    implementer_from_snapshot,
    model_pins,
    models_for_new_record,
    models_from_snapshot,
    parse_implementer,
    parse_model_slots,
    unrecorded_name_hint,
)
from .pg import PgError, cluster_exists, cluster_running, ensure_cluster, require_loopback_dsn, stop_cluster
from .pr_guard_config import PrGuardLookupError, fetch_trusted_pr_guard, one_review_for
from .preconditions import REGISTRY_KEYS
from .rpc_usage import scan_rpc_usage
from .runner_cmd import cmd_runner
from .runtime import (
    PreflightRuntime,
    Runtime,
    interactive_command_argv,
    interactive_model,
    load_tmux_targets,
    new_interactive_session_id,
    run_argv,
    tmux_name,
)
from .skills import SKILL_NAMES, has_skill, skill_for_agent_role
from .small_change import (
    EVIDENCE_PREFIX,
    MAX_CHANGED_LINES,
    MAX_FILES,
    VERDICT_SMALL,
    check_repo,
    is_small_change_evidence,
)
from .store import Store, StoreConnectionError, StoreError, utcnow
from .timestamps import parse_utc_timestamp
from .token_keepalive import KeepalivePace, keepalive_due
from .usage import (
    USAGE_CHECKED_PREFIX,
    AccountUsageError,
    AuthStale,
    is_spend_snapshot,
    scan_account_usage,
    scan_session_usage,
    scan_window_usage,
    usage_poll_due,
)
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
        "review_1_quality",
        "review_1_logic",
        "review_2_quality",
        "review_2_logic",
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
        "review_1_quality",
        "review_1_logic",
        "review_2_quality",
        "review_2_logic",
        "deviation_declared",
        "deviation_granted",
    ),
    "resolve-conflicts": (
        "session_registered",
        "conflicts_resolved",
        "reviewer_approved",
        "local_check_pass",
        "pushed",
        "review_1_quality",
        "review_1_logic",
        "review_2_quality",
        "review_2_logic",
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
    "gate-blocked",
    "done",
    "failed",
    "merged",
    "superseded",
)
CHECKLIST_STATUSES = ("ja", "nein", "n_a", "pending", "unavailable")
ROUND_START_STATES = frozenset(
    {
        "open",
        "implementing",
        "reviewing",
        "failed",
    }
)
MODEL_CHANGE_CLOSED_STATES = frozenset({"done", "merged", "superseded"})
PING_KINDS = ("review-request", "ping", "question")
ACTIVITY_TYPES = frozenset(
    {
        "session.register",
        "issue.write",
        "pr.open",
        "pr.body",
        "pr.merged",
        "issue.assigned",
        "issue.assigned.ack",
        "comment.post",
        "review.post",
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
        "lane.run",
        "supervise.event",
        "order.event",
        "order.reply",
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
        "lane.run",
        "error.seen",
        "supervise.event",
        "issue.assigned.ack",
        "order.event",
        "order.reply",
    }
)
AGENT_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
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
STATIC = Path(__file__).resolve().parent / "static" / "index.html"
STATIC_NEW_ORDER = Path(__file__).resolve().parent / "static" / "orders_new.html"
SECTION_STATIC = Path(__file__).resolve().parent / "static" / "section.html"
DASHBOARD_SECTIONS = (
    "usage",
    "sessions",
    "tasks",
    "rounds",
    "agents",
    "checks",
    "gates",
    "work",
    "pings",
)


def _dashboard_page(path: str) -> Path | None:
    """Map a dashboard URL path to the static page that serves it."""
    if path in ("/", "/index.html"):
        return STATIC
    if path.startswith("/") and path[1:] in DASHBOARD_SECTIONS:
        return SECTION_STATIC
    return None


def die(msg: str) -> None:
    raise SystemExit(f"agent: {msg}")


def _warn_deprecated_name(supplied: str, canonical: str) -> None:
    if supplied != canonical:
        print(
            f"deprecated stage/key name {supplied}; use {canonical}",
            file=sys.stderr,
        )


def _install_recorded_names(agent_home: Path) -> None:
    """Make the names an earlier version stored readable in this process."""
    try:
        install_recorded_names(load_recorded_names(agent_home))
    except AIAccountError as exc:
        die(str(exc))


def _canonical_input(supplied: str, canonicalize: Callable[[str], str]) -> str:
    """Canonicalize a ``--stage`` or ``--key`` value before the store is opened."""
    _install_recorded_names(home())
    canonical = canonicalize(supplied)
    _warn_deprecated_name(supplied, canonical)
    return canonical


def _refuse_unrecorded(name: str, refusal: str) -> None:
    """Refuse a name an earlier version wrote while no ``recorded_slots`` is set.

    Without that entry such a name is no current stage or key, and every reader
    treats a row stored under it as absent; the refusal names the missing entry.
    """
    hint = unrecorded_name_hint(name)
    if hint:
        die(f"{refusal}{hint}")


def default_home() -> Path:
    return Path.home() / ".local" / "share" / "agent"


def _same_home(a: Path, b: Path) -> bool:
    """True when both paths name the same directory after ~ expansion and symlink resolution."""
    return a.expanduser().resolve() == b.expanduser().resolve()


def _store_home(store: Any) -> Path:
    store_home = getattr(store, "home", None)
    return store_home if isinstance(store_home, Path) else home()


def _store_model_configuration(store: Any) -> tuple[ModelSlots, str]:
    """One manifest read for the task's model check and its pinned selection."""
    return load_model_configuration(_store_home(store))


def _require_vendor(
    vendor: str, agent_home: Path, *, implementer: bool = False
) -> DriverSpec:
    """A ``--vendor`` or a task pin must name a configured driver; returns it.

    Lane roles, gates and model slots need a driver with a lane section; an
    implementer may also name a record-only driver.
    """
    config = load_driver_configuration(agent_home)
    choices = (
        declared_driver_ids(config.drivers)
        if implementer
        else lane_driver_ids(config.drivers)
    )
    refused = driver_refusal(config, vendor, choices)
    if refused is not None:
        die(refused)
    return config.drivers[vendor]


def home() -> Path:
    override = os.environ.get("AGENT_HOME")
    if override == "":
        die("AGENT_HOME is set but empty")
    if override:
        return Path(override).expanduser()
    return default_home()


def _runtime() -> Runtime:
    return Runtime(tmux_targets=load_tmux_targets(home()))


def pg_dsn_from_env() -> str | None:
    """Return AGENT_PG_DSN, None when unset; die("AGENT_PG_DSN is set but empty") when it is the empty string."""
    dsn = os.environ.get("AGENT_PG_DSN")
    if dsn == "":
        die("AGENT_PG_DSN is set but empty")
    return dsn


def _cluster_running(data_dir: Path) -> bool:
    """cluster_running() with PgError (e.g. missing pg_ctl) routed through die()."""
    try:
        return cluster_running(data_dir)
    except PgError as exc:
        die(str(exc))
        return False


def _cluster_exists(data_dir: Path) -> bool:
    """cluster_exists() with PgError routed through die()."""
    try:
        return cluster_exists(data_dir)
    except PgError as exc:
        die(str(exc))
        return False


def open_store(*, allow_legacy_sqlite: bool = False, create: bool = False) -> Store:
    h = home()
    from .daemon import adopt_kept_agent_pg_bin

    adopt_kept_agent_pg_bin(h)
    sqlite_legacy = h / "ledger.sqlite"
    if sqlite_legacy.is_file() and not allow_legacy_sqlite:
        die("found ledger.sqlite; move it aside then run agent restore")
    dsn = pg_dsn_from_env()
    if not dsn:
        try:
            dsn = ensure_cluster(h / "pg", create=create)
        except PgError as exc:
            die(str(exc))
    try:
        require_loopback_dsn(dsn)
    except PgError as exc:
        die(str(exc))
    _install_recorded_names(h)
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


def cmd_init(args: list[str]) -> None:
    from .daemon import (
        SERVICE_LABEL,
        agent_argv,
        install_and_start_service,
        service_home,
        service_path,
    )

    if any(arg != "--no-service" for arg in args) or args.count("--no-service") > 1:
        die("Usage: agent init [--no-service]")
    no_service = "--no-service" in args
    if no_service:
        unit = service_path(sys.platform, home())
        try:
            recorded = service_home(unit, sys.platform)
        except StoreError as exc:
            die(str(exc))
        if recorded is not None and _same_home(recorded, home()):
            die(
                f"the device daemon is installed ({unit}) for this AGENT_HOME; "
                "run agent daemon --uninstall first"
            )
    external = pg_dsn_from_env()
    override = os.environ.get("AGENT_HOME")
    if override and not _same_home(home(), default_home()):
        if external is None:
            if no_service:
                warning = (
                    f"init creates a separate postgres cluster and device identity there; "
                    "no user service is installed"
                )
            else:
                warning = (
                    f"init creates a separate postgres cluster and device identity there and repoints the "
                    f"user service {SERVICE_LABEL} at it; agent daemon --uninstall stops that cluster and "
                    "removes the service again, the device.json stays"
                )
        else:
            service = (
                "no user service is installed"
                if no_service
                else f"the user service {SERVICE_LABEL} is repointed at it"
            )
            warning = (
                f"init writes a separate device identity there and {service}; the cluster logic is "
                "bypassed by AGENT_PG_DSN"
            )
        print(
            f"agent: AGENT_HOME={home()} is not the default {default_home()}; {warning}",
            file=sys.stderr,
        )
    store = open_store(create=True)
    try:
        program = [*agent_argv(), "daemon"]
        if no_service:
            print(f"ok  device={store.device_id()} home={store.home} daemon=not-installed")
            print(f"daemon argv: {shlex.join(program)}")
            print(f"environment: AGENT_HOME={shlex.quote(str(store.home))}")
            pg_bin = os.environ.get("AGENT_PG_BIN")
            if pg_bin:
                print(f"environment: AGENT_PG_BIN={shlex.quote(pg_bin)}")
            if external:
                print("environment: AGENT_PG_DSN (set; keep the current value)")
        else:
            install_and_start_service(home=store.home, program=program)
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
            from .error_fix_act import is_retryable_configuration_error

            open_tasks = [
                t
                for t in store.rows("task")
                if t.get("session_id") == sid
                and t.get("state") not in TERMINAL_STATES
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
                and (
                    a.get("execution_status") == "pending"
                    or is_retryable_configuration_error(a)
                )
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
            runtime = _runtime()
            name = _session_start(
                store,
                runtime,
                sid,
                cmd,
                cols,
                rows,
                provider=provider,
                model=model,
            )
            row = store.row("session", sid)
            interactive_id = ""
            if isinstance(row, dict):
                meta = row.get("runtime")
                if isinstance(meta, dict) and isinstance(
                    meta.get(INTERACTIVE_SESSION_KEY), str
                ):
                    interactive_id = meta[INTERACTIVE_SESSION_KEY]
            if interactive_id:
                print(f"started {sid} tmux={name} interactive={interactive_id}")
            else:
                print(f"started {sid} tmux={name}")
            return
        if sub == "stop":
            sid = require_flag(rest, "--id")
            runtime = _runtime()
            _session_stop(store, runtime, sid)
            print(f"stopped {sid}")
            return
        if sub == "input":
            sid = require_flag(rest, "--id")
            data = flag(rest, "--data")
            key = flag(rest, "--key")
            runtime = _runtime()
            _session_input(store, runtime, sid, data, key)
            print(f"input {sid}")
            return
        if sub == "keep-working":
            sid = require_flag(rest, "--id")
            once = "--once" in rest
            follow = "--follow" in rest
            if once and follow:
                die("keep-working takes at most one of --once or --follow")
            runtime = _runtime()
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
        inner = raw.get("payload")
        if isinstance(inner, dict) and inner.get("kind") == "a38.second-review/v1":
            from .coordinator_a38 import validate_second_review_payload
            from .coordinator_common import CoordinatorError

            if typ != "message":
                die("a38.second-review/v1 must use activity type message")
            to_session = raw.get("to_session")
            if not isinstance(to_session, str) or not to_session:
                die("a38.second-review/v1 requires payload.to_session")
            try:
                raw["payload"] = validate_second_review_payload(inner)
            except CoordinatorError as exc:
                die(str(exc))
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
        die("Usage: agent task create|list|show|state|summary|pr|models|close-from-pr")
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
            models, implementer = _store_model_configuration(store)
            if models.second is None:
                die(EXTERNAL_SECOND_TASK_CREATION_ERROR)
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
                    **model_pins(models, implementer),
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
            current = row.get("state")
            if state == "merged":
                die("merged may only be set by task close-from-pr")
            if current == "merged":
                die("cannot change state of a merged task")
            if state == "superseded" and current == "done":
                die("cannot supersede a done task")
            if state == "done" and current == "gate-blocked":
                die("cannot mark done from gate-blocked")
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
        if sub == "pr":
            tid = require_flag(rest, "--id")
            repo = require_flag(rest, "--repo")
            raw_pr = require_flag(rest, "--pr")
            if (
                repo.count("/") != 1
                or any(not part for part in repo.split("/"))
                or any(char.isspace() for char in repo)
            ):
                die("--repo must be owner/name")
            if not raw_pr.isascii() or not raw_pr.isdigit() or int(raw_pr) < 1:
                die("--pr must be a pull request number")
            pr = int(raw_pr)
            row = _need(store, "task", tid)
            _require_owned(store, row, "task")
            _require_skill(_need(store, "session", row["session_id"]), "spine")
            if str(row.get("state") or "") in TERMINAL_STATES:
                die("cannot attach a pull request to a terminal task")
            payload = row.get("payload")
            if isinstance(payload, dict) and payload.get("error_id"):
                die(
                    "task pr refuses error-fix tasks: their ref is the checkout start point"
                )
            old_repo = str(row.get("repo") or "")
            if old_repo and old_repo != repo:
                die(f"task repo is {old_repo}; refusing to change it to {repo}")
            old_ref = str(row.get("ref") or "")
            if old_ref and not old_ref.isdigit():
                die(
                    f"task ref is {old_ref}; it is not a pull request number, refusing to replace it"
                )
            if old_ref.isdigit() and old_ref != str(pr):
                die(f"task already has pull request {old_ref}")
            row["repo"] = repo
            row["ref"] = str(pr)
            row["updated_at"] = utcnow()
            store.write("task", "update", tid, _strip(row))
            print(f"task {tid} pr={repo}#{pr}")
            return
        if sub == "models":
            tid = require_flag(rest, "--id")
            slot = require_flag(rest, "--slot")
            vendor = require_flag(rest, "--vendor")
            reason = require_flag(rest, "--reason").strip()
            if slot not in ("first", "second", "implementer"):
                die("slot must be first|second|implementer")
            if not reason:
                die("--reason must not be empty")
            _require_vendor(
                vendor, _store_home(store), implementer=slot == "implementer"
            )
            row = _need(store, "task", tid)
            _require_owned(store, row, "task")
            _require_skill(_need(store, "session", row["session_id"]), "spine")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("coordinator"):
                die(
                    "coordinator tasks keep the model slots pinned from ai-accounts.json when the coordinator accepted them"
                )
            if str(row.get("state") or "") in MODEL_CHANGE_CLOSED_STATES:
                die("cannot change the model slots of a closed task")
            if row.get("models") is None:
                die(TASK_MODELS_NOT_CONFIGURED)
            models = models_for_new_record(row)
            if slot == "second" and models.second is None:
                die(
                    "cannot change models.second: the task's second review is external"
                )
            implementer = implementer_for_new_record(row)
            old = implementer if slot == "implementer" else models.for_slot(slot)
            if slot == "first":
                new_models = ModelSlots(vendor, models.second)
                new_implementer = (
                    vendor if row.get("implementer") is None else implementer
                )
            elif slot == "second":
                new_models = ModelSlots(models.first, vendor)
                new_implementer = implementer
            else:
                new_models = models
                new_implementer = vendor
            new_models = parse_model_slots(new_models.as_dict())
            new_implementer = parse_implementer(new_implementer, new_models)
            pins = model_pins(new_models, new_implementer)
            row["models"] = pins["models"]
            if "implementer" in pins:
                row["implementer"] = pins["implementer"]
            else:
                row.pop("implementer", None)
            changes = row.get("model_changes")
            if changes is None:
                changes = []
            elif not isinstance(changes, list):
                die("task model_changes history is not a list")
            now = utcnow()
            row["model_changes"] = [
                *changes,
                {
                    "slot": slot,
                    "from": old,
                    "to": vendor,
                    "reason": reason,
                    "at": now,
                },
            ]
            row["updated_at"] = now
            store.write("task", "update", tid, _strip(row))
            label = f"models.{slot}" if slot in ("first", "second") else "implementer"
            print(f"task {tid} {label}={vendor} (was {old})")
            return
        if sub == "close-from-pr":
            tid = flag(rest, "--id")
            session_id = flag(rest, "--session")
            if bool(tid) == bool(session_id):
                die("Usage: agent task close-from-pr (--id ID | --session ID)")
            if session_id:
                session = _need(store, "session", session_id)
                _require_owned(store, session, "session")
                _require_skill(session, "spine")
                tasks = []
                for task in store.rows("task"):
                    if task.get("session_id") != session_id:
                        continue
                    repo, number = _task_repo_and_pr(task)
                    state = str(task.get("state") or "")
                    working = any(
                        agent.get("task_id") == task.get("id")
                        and agent.get("status") == "working"
                        for agent in store.rows("agent")
                    )
                    if (state not in TERMINAL_STATES and repo and number) or (
                        state in ("done", "merged") and working
                    ):
                        tasks.append(task)
            else:
                tasks = [_need(store, "task", str(tid))]
            failed = False
            for task in tasks:
                try:
                    _close_task_from_pr(store, task)
                except SystemExit as exc:
                    failed = True
                    reason = str(exc).removeprefix("agent: ")
                    print(f"task {task['id']} not closed: {reason}")
                    if tid:
                        raise SystemExit(1)
            if failed:
                raise SystemExit(1)
            return
        die(
            f"unknown task command: {sub}\n"
            "Usage: agent task create|list|show|state|summary|pr|models|close-from-pr"
        )
    finally:
        store.close()


def cmd_checklist(args: list[str]) -> None:
    if not args or args[0] != "set":
        die(
            "Usage: agent checklist set --task ID --key KEY --status ja|nein|n_a|pending|unavailable "
            "--source human|runner|script "
            "[--evidence TEXT] [--deviation-declared true|false] "
            "[--deviation-granted true|false] [--granted-by TEXT] [--actor-session ID]"
        )
    rest = args[1:]
    tid = require_flag(rest, "--task")
    key = _canonical_input(require_flag(rest, "--key"), canonical_key)
    _refuse_unrecorded(key, f"checklist {key} for task {tid} not found")
    if key in REGISTRY_KEYS:
        die(f"{key} is a coordinator precondition check; its resolver or agent order override closes it")
    status = require_flag(rest, "--status")
    source = require_flag(rest, "--source")
    if status not in CHECKLIST_STATUSES:
        die("status must be ja|nein|n_a|pending|unavailable")
    if source not in ("human", "runner", "script"):
        die("source must be human|runner|script")
    evidence = flag(rest, "--evidence")
    if status == "unavailable" and key not in GATE_UNAVAILABLE_KEYS:
        die(f"unavailable is not allowed for {key}")
    if requires_evidence(status, key) and not (evidence or "").strip():
        die(f"{status} requires --evidence")
    if (
        status == "n_a"
        and key in SECOND_REVIEW_KEYS
        and not second_review_na(key, evidence)
    ):
        die(f"n_a for {key} requires evidence {SECOND_REVIEW_NOT_POSTED}")
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
        items = [
            r
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid and canonical_key(str(r.get("key"))) == key
        ]
        if len(items) != 1:
            die(f"checklist {key} for task {tid} not found")
        if status == "n_a" and key in SECOND_REVIEW_KEYS:
            login, config = _live_one_review(store, task)
            if not one_review_for(config, login):
                die(
                    f"n_a for {key} requires one_review_authors "
                    "in the trusted .github/pr-guard.json on the default branch. "
                    "A missing file, a missing key, or an empty list keeps two reviews required. "
                    "An entry that exists only on the pull request head does not count."
                )
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            payload = dict(payload)
            payload["one_review_author"] = {
                "pull_opener": login,
                "trusted_pr_guard": config,
            }
            task["payload"] = payload
            task["updated_at"] = utcnow()
            store.write("task", "update", tid, _strip(task))
        _update_checklist_item(
            store,
            items[0],
            status=status,
            source=source,
            evidence=evidence,
            deviation_declared=deviation_declared,
            deviation_granted=deviation_granted,
            granted_by=granted_by,
        )
        print(f"checklist {key}={status}")
    finally:
        store.close()


def _update_checklist_item(
    store: Store,
    item: dict,
    *,
    status: str,
    source: str,
    evidence: str | None,
    deviation_declared: bool | None = None,
    deviation_granted: bool | None = None,
    granted_by: str | None = None,
    writes: list[tuple[str, str, str, dict]] | None = None,
) -> None:
    """Shared checklist mutation used by ``checklist set`` and trusted closers."""
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
    write = ("checklist_item", "update", str(item["id"]), _strip(item))
    if writes is None:
        store.write(*write)
    else:
        writes.append(write)


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
        if task.get("state") not in ROUND_START_STATES:
            die("round start requires state open|implementing|reviewing|failed")
        latest = _latest_gates(store, tid)
        if any(g.get("verdict") == "unavailable" for g in latest.values()):
            die("round start refused while a gate is unavailable")
        current = int(task.get("current_round") or 0)
        for agent in store.rows("agent"):
            if agent.get("task_id") == tid and agent.get("status") == "working":
                die("round still has a working agent")
        if current > 0:
            open_round = [
                r
                for r in store.rows("task_round")
                if r.get("task_id") == tid and r.get("round") == current
            ]
            if (
                task.get("state") == "reviewing"
                and len(open_round) == 1
                and open_round[0].get("implementer_verdict") == "done"
                and open_round[0].get("reviewer_verdict") is None
            ):
                die(
                    f"round {current} awaits its reviewer: start the reviewer "
                    f"for round {current} before round start"
                )
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
            _require_vendor(
                vendor, _store_home(store), implementer=role == "implementer"
            )
            round_raw = flag(rest, "--round")
            round_num: int | None = None
            supplied_stage = flag(rest, "--stage")
            stage: str | None = None
            if role in ("implementer", "reviewer"):
                if round_raw is None or round_raw == "":
                    die(f"{role} requires --round")
                if supplied_stage is not None:
                    die("--stage is only valid for PR-reviewer roles")
            else:
                if supplied_stage is not None:
                    stage = canonical_stage(supplied_stage)
                    _warn_deprecated_name(supplied_stage, stage)
                    if stage not in STAGE_SLOTS:
                        die(
                            f"stage must be {STAGE_CHOICES}"
                            f"{unrecorded_name_hint(stage)}"
                        )
            # Preserve the historical vendor-precondition order for an unknown
            # task and use a present task pin when it is valid. A legacy task
            # without ``models`` must not turn the display-only mapping into a
            # vendor requirement for a new row. Invalid pins are diagnosed only
            # after the existing start preconditions below.
            task_snapshot = store.row("task", tid)
            models: ModelSlots | None = None
            if task_snapshot is None or task_snapshot.get("models") is not None:
                try:
                    models = models_from_snapshot(task_snapshot)
                except PinnedModelSlotsError:
                    pass
            if role == "reviewer" and models is not None and vendor != models.first:
                die(f"reviewer requires --vendor {models.first} (configured model slot first)")
            if stage is not None and models is not None:
                expected_vendor = models.for_stage(stage)
                if vendor != expected_vendor:
                    die(
                        f"stage {stage} requires --vendor {expected_vendor} "
                        "(model slot pinned on the task)"
                    )
            implementer: str | None = None
            if task_snapshot is None or models is not None:
                try:
                    implementer = implementer_from_snapshot(task_snapshot)
                except PinnedModelSlotsError:
                    pass
            if role == "implementer" and implementer is not None and vendor != implementer:
                die(f"implementer requires --vendor {implementer} (configured implementer)")
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
            # New agent rows require explicit, valid task pins. Keep this after
            # every pre-existing start condition and immediately before writing.
            models = models_for_new_record(task)
            implementer = implementer_for_new_record(task)
            if role in ("pr-reviewer-quality", "pr-reviewer-logic") and stage is None:
                slot = models.slot_of(vendor)
                if slot is None:
                    die("PR-reviewer vendor is not pinned on the task")
                stage = SLOT_STAGES[slot]
            if role == "reviewer" and vendor != models.first:
                die(f"reviewer requires --vendor {models.first} (configured model slot first)")
            if role == "implementer" and vendor != implementer:
                die(f"implementer requires --vendor {implementer} (configured implementer)")
            if stage is not None:
                expected_vendor = models.for_stage(stage)
                if vendor != expected_vendor:
                    die(
                        f"stage {stage} requires --vendor {expected_vendor} "
                        "(model slot pinned on the task)"
                    )
                if stage == "review-2":
                    latest = _latest_gates(store, tid)
                    review_1_heads: dict[str, str] = {}
                    for dimension in ("quality", "logic"):
                        gate = latest.get(("review-1", dimension))
                        if (
                            gate is None
                            or gate.get("verdict") != "approved"
                            or gate.get("vendor") != models.first
                        ):
                            die(
                                "review-2 requires approved "
                                f"review-1/{dimension} at the same head"
                            )
                        review_1_heads[dimension] = str(gate.get("head_sha") or "")
                    for dimension in ("quality", "logic"):
                        if not review_1_heads[dimension]:
                            die(
                                "review-2 requires approved "
                                f"review-1/{dimension} at the same head"
                            )
                    if review_1_heads["quality"] != review_1_heads["logic"]:
                        die(
                            "review-2 requires approved "
                            "review-1/logic at the same head"
                        )
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
                    "stage": stage,
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
                if verdict not in ("approved", "rejected", "unavailable"):
                    die("pr-reviewer verdict must be approved|rejected|unavailable")
                _require_owned(store, task, "task")
            else:
                die(f"unknown agent role: {role}")
            agent["status"] = "done"
            agent["finished_at"] = utcnow()
            if note is not None:
                agent["note"] = redact_secrets(note)
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
        if result == "fail" and task.get("state") not in ("done", "merged"):
            task["state"] = "failed"
            task["updated_at"] = utcnow()
            store.write("task", "update", tid, _strip(task))
        print(f"check {name}={result}")
    finally:
        store.close()


def _full_sha(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", text) is None:
        return None
    return text.lower()


def cmd_small_change(args: list[str]) -> None:
    """Measure the committed HEAD; exit 0 small, 2 not-small, 1 usage."""
    tid = require_flag(args, "--task")
    repo_path = Path(flag(args, "--repo") or os.getcwd())
    as_json = "--json" in args
    store = open_store()
    try:
        task = _need(store, "task", tid)
        original = _strip(task)
        session = _need(store, "session", str(task.get("session_id") or ""))
        if session.get("status") != "active":
            die(f"session {session.get('id')} is not active")
        _require_owned(store, task, "task")
        _require_skill(session, "spine")
        if task.get("workflow") != "implement":
            die("small-change requires workflow implement")
        if str(task.get("state") or "") in TERMINAL_STATES:
            die("cannot record a small-change verdict on a terminal task")
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        if payload.get("coordinator") or payload.get("error_id"):
            die("small-change is for spine tasks only")
        repo, number = _task_repo_and_pr(task)
        if not repo or not number:
            die(
                "attach the pull request first: agent task pr --id <task> "
                "--repo <owner/name> --pr <number>"
            )
        from .github_accounts import AccountError

        runner = _bound_review_runner(store, task)
        try:
            response = runner(["gh", "api", f"repos/{repo}/pulls/{number}"])
        except AccountError as exc:
            die(str(exc))
        if response.returncode != 0:
            die("pull request could not be read")
        try:
            pull = json.loads(response.stdout or "{}")
        except json.JSONDecodeError:
            die("pull request could not be read")
        base = pull.get("base") if isinstance(pull, dict) else None
        head = pull.get("head") if isinstance(pull, dict) else None
        base_sha = _full_sha(base.get("sha") if isinstance(base, dict) else None)
        head_sha = _full_sha(head.get("sha") if isinstance(head, dict) else None)
        raw_base_ref = base.get("ref") if isinstance(base, dict) else None
        if not isinstance(raw_base_ref, str) or not raw_base_ref:
            die("pull request could not be read")
        base_ref = raw_base_ref
        if base_sha is None or head_sha is None:
            die("pull request could not be read")
        verdict = check_repo(repo_path, base_sha, head_sha)
        verdict = replace(verdict, base_ref=base_ref)
        record = verdict.to_json()
        record["recorded_at"] = utcnow()
        written = dict(original)
        written["small_change"] = record
        written["updated_at"] = utcnow()

        def check() -> None:
            row = store.row("task", tid)
            if row is None or _strip(row) != original:
                die("task changed during small-change; retry")
            if str(row.get("state") or "") in TERMINAL_STATES:
                die("cannot record a small-change verdict on a terminal task")

        store.write_many([("task", "update", tid, written)], check=check)
    finally:
        store.close()

    if as_json:
        print(json.dumps(verdict.to_json(), ensure_ascii=False))
    else:
        head_label = verdict.head or "-"
        print(
            f"small-change {verdict.verdict} at {head_label}: "
            f"{verdict.lines} lines in {len(verdict.files)} files "
            f"(limit {MAX_CHANGED_LINES} lines, {MAX_FILES} files)"
        )
        for reason in verdict.reasons:
            print(f"- {reason}")
        if verdict.verdict == VERDICT_SMALL:
            print(f"evidence: {EVIDENCE_PREFIX}{verdict.head}")
    if verdict.verdict != VERDICT_SMALL:
        raise SystemExit(2)


def _read_text_arg(args: list[str]) -> str:
    path = flag(args, "--file")
    if path is not None:
        return Path(path).read_text(encoding="utf-8")
    return sys.stdin.read()


def cmd_local_ci(args: list[str]) -> None:
    if not args or args[0] not in ("verify", "parse", "render"):
        die(
            "Usage: agent local-ci verify|parse|render [--file PATH] "
            "[--require-ids id,id] [--expect-head SHA] [--expect-private] [--json]"
        )
    action = args[0]
    rest = args[1:]
    as_json = "--json" in rest
    expect_private = "--expect-private" in rest
    expect_head = flag(rest, "--expect-head")
    require_raw = flag(rest, "--require-ids")
    require_ids = None
    if require_raw:
        require_ids = frozenset(part.strip() for part in require_raw.split(",") if part.strip())
        if not require_ids:
            die("--require-ids must list at least one id")
    raw = _read_text_arg(rest)
    if action == "render":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            die(f"JSON is invalid: {exc.msg}")
        if not isinstance(payload, dict):
            die("render input must be a JSON object")
        try:
            report = parse_payload(payload)
        except LocalCiError as exc:
            die(str(exc))
        sys.stdout.write(render_block(report))
        return
    try:
        if action == "parse":
            report = parse_comment(raw)
            if as_json:
                print(
                    json.dumps(
                        {
                            "schema": report.schema,
                            "repo": report.repo,
                            "head": report.head,
                            "private": report.private,
                            "recorded_at": report.recorded_at,
                            "required": list(report.required),
                            "runs": [run.id for run in report.runs],
                        },
                        sort_keys=True,
                    )
                )
            else:
                print(f"local-ci repo={report.repo} head={report.head} required={len(report.required)}")
            return
        verdict = verify_comment(
            raw,
            require_ids=require_ids,
            expect_head=expect_head,
            expect_private=True if expect_private else None,
        )
    except LocalCiError as exc:
        die(str(exc))
    if as_json:
        print(json.dumps(verdict.to_json(), sort_keys=True))
    else:
        extra = ""
        if verdict.reasons:
            extra = " " + "; ".join(verdict.reasons)
        print(f"local-ci {verdict.status}{extra}")
    if not verdict.ok:
        raise SystemExit(1)


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
    """Queue a rejected gate's evidence as a pull-request review.

    A rejection that is only recorded stops the task without telling the author what
    was found. A review rather than a plain comment: it is where an author looks, and
    it is the artefact a runner counts when asking whether a review happened at all.
    `agent github pending` performs the HTTP.
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
        # Preserve the executor's pinned identity from the authoritative read
        # under the lock, including recovery from a stale initial insert choice.
        payload.pop("execution_account", None)
        if row is not None and "execution_account" in row:
            payload["execution_account"] = row["execution_account"]
        return row is not None and row.get("execution_status") != "error"

    payload = {
        "id": activity_id,
        "session_id": task["session_id"],
        "type": "review.post",
        "payload": {
            "repo": repo,
            "number": number,
            "body": (
                f"**{vendor.capitalize()} {dimension} — rejected** at `{head[:7]}`\n\n"
                f"{evidence}"
            ),
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
            f"Usage: agent gate record --task UUID --stage {STAGE_CHOICES} "
            "--dimension quality|logic --vendor DRIVER --verdict approved|rejected|unavailable "
            "--head SHA --agent UUID [--evidence TEXT; required when rejected or unavailable]"
        )
    rest = args[1:]
    tid = require_flag(rest, "--task")
    stage = _canonical_input(require_flag(rest, "--stage"), canonical_stage)
    dimension = require_flag(rest, "--dimension")
    vendor = require_flag(rest, "--vendor")
    verdict = require_flag(rest, "--verdict")
    head = require_flag(rest, "--head").lower()
    agent_id = require_flag(rest, "--agent")
    evidence = flag(rest, "--evidence")
    if stage not in STAGE_SLOTS:
        die(f"stage must be {STAGE_CHOICES}{unrecorded_name_hint(stage)}")
    if dimension not in ("quality", "logic"):
        die("dimension must be quality|logic")
    _require_vendor(vendor, home())
    if verdict not in ("approved", "rejected", "unavailable"):
        die("verdict must be approved|rejected|unavailable")
    if verdict == "rejected" and not (evidence or "").strip():
        die("--evidence is required when --verdict is rejected")
    if verdict == "unavailable" and not (evidence or "").strip():
        die("--evidence is required when --verdict is unavailable")
    if not re.fullmatch(r"[0-9a-f]{7,40}", head):
        die("--head must be a git SHA (lowercase hex, length 7–40)")
    store = open_store()
    try:
        task = _need(store, "task", tid)
        # A historical slot mapping may display existing rows, but it must not
        # authorize or constrain a new gate record. The strict pin check remains
        # at the write boundary so the existing precondition order is preserved.
        models = None if task.get("models") is None else models_from_snapshot(task)
        if stage == "review-2" and models is not None and models.second is None:
            die(
                "gate record --stage review-2 is unavailable: the second review is external; "
                "record it through the review_2_quality/review_2_logic checklist evidence"
            )
        if models is not None:
            expected_vendor = models.for_stage(stage)
            if vendor != expected_vendor:
                die(
                    f"stage {stage} requires vendor {expected_vendor} "
                    "(model slot pinned on the task)"
                )
        session = _need(store, "session", task["session_id"])
        if session.get("status") != "active":
            die(f"session {task['session_id']} is not active")
        _require_owned(store, session, "session")
        _require_owned(store, task, "task")
        _require_skill(session, "pr-review")
        if stage == "review-2":
            latest = _latest_gates(store, tid)
            gq = latest.get(("review-1", "quality"))
            gl = latest.get(("review-1", "logic"))
            for label, g in (("quality", gq), ("logic", gl)):
                if g is None:
                    die(f"review-2 requires approved review-1/{label} at the same head")
                if g.get("verdict") != "approved":
                    die(f"review-2 requires approved review-1/{label} at the same head")
                if models is not None and g.get("vendor") != models.first:
                    die(f"review-2 requires approved review-1/{label} at the same head")
                if g.get("head_sha") != head:
                    die(f"review-2 requires approved review-1/{label} at the same head")
        agent = _need(store, "agent", agent_id)
        if agent.get("task_id") != tid:
            die("agent task_id does not match --task")
        if agent.get("status") != "done":
            die("agent must be status=done")
        if agent.get("vendor") != vendor:
            die("agent vendor does not match --vendor")
        agent_stage = agent.get("stage")
        if agent_stage is not None and canonical_stage(str(agent_stage)) != stage:
            die("agent stage does not match --stage")
        expected_role = f"pr-reviewer-{dimension}"
        if agent.get("role") != expected_role:
            die(f"agent role must be {expected_role}")
        if verdict == "rejected" and task.get("state") in ("done", "merged"):
            die(f"cannot reject a gate on a {task.get('state')} task")
        _require_owned(store, task, "task")
        # Gate-row write: legacy tasks without a model pin cannot add gates.
        models = models_for_new_record(task)
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
                "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            },
        )
        if verdict == "unavailable" and task.get("workflow") in (
            "implement",
            "resolve-conflicts",
            "review",
        ):
            if task.get("state") in ("pr-review", "pushing"):
                task["state"] = "gate-blocked"
                task["updated_at"] = utcnow()
                store.write("task", "update", tid, _strip(task))
        elif verdict == "approved" and task.get("state") == "gate-blocked":
            if _all_gate_pairs_approved_same_head(_latest_gates(store, tid), models):
                task["state"] = "pr-review"
                task["updated_at"] = utcnow()
                store.write("task", "update", tid, _strip(task))
        elif verdict == "rejected" and task.get("workflow") in ("implement", "resolve-conflicts"):
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
            print(f"activity {queued} type=review.post")
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
        challenge = secrets.token_hex(16)
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
SYNC_IDLE_SECONDS = 30.0


def cmd_sync(args: list[str]) -> None:
    follow = "--follow" in args
    store = open_store()
    try:
        if not follow:
            _sync_once(store)
            return
        hub: Hub | None = None
        runtime = _runtime()
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
    measure connection stability from there rather than from the connect attempt.
    The push is due every SYNC_IDLE_SECONDS whether or not frames arrive, and a frame
    already being handled when the deadline passes delays it by that handling."""
    ws = hub.connect_sync_ws()
    try:
        ws.send(json.dumps({"type": "control-ready"}))
        established["at"] = time.monotonic()
        last_capture.clear()  # a new connection has no idea what a prior one already sent
        _publish_terminals(store, runtime, ws, terminal_seq, last_capture)
        next_sync = time.monotonic() + SYNC_IDLE_SECONDS
        while True:
            remaining = next_sync - time.monotonic()
            if remaining <= 0:
                _sync_once(store)
                next_sync = time.monotonic() + SYNC_IDLE_SECONDS
                _publish_terminals(store, runtime, ws, terminal_seq, last_capture)
                continue
            try:
                raw = ws.recv(timeout=remaining)
            except TimeoutError:
                _sync_once(store)
                next_sync = time.monotonic() + SYNC_IDLE_SECONDS
                _publish_terminals(store, runtime, ws, terminal_seq, last_capture)
                continue
            except ConnectionClosed as exc:
                raise HubError("websocket closed") from exc
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
                next_sync = time.monotonic() + SYNC_IDLE_SECONDS
            _publish_terminals(store, runtime, ws, terminal_seq, last_capture)
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
        open_tasks = [t for t in data["tasks"] if t.get("state") not in TERMINAL_STATES]
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


def _canonicalize_snapshot_names(snapshot: dict[str, Any]) -> None:
    """Canonicalize legacy agent, gate, and checklist names in snapshot output."""
    for agent in snapshot.get("agents") or []:
        if isinstance(agent, dict) and agent.get("stage") is not None:
            agent["stage"] = canonical_stage(str(agent["stage"]))
    for gate in snapshot.get("gates") or []:
        if isinstance(gate, dict) and gate.get("stage") is not None:
            gate["stage"] = canonical_stage(str(gate["stage"]))
    for item in snapshot.get("checklist") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            item["key"] = canonical_key(str(item["key"]))


def _usage_account(payload: dict[str, Any]) -> tuple[Any, Any]:
    """Provider and account named by a usage snapshot payload.

    A spend snapshot names the usage account; any other snapshot names an e-mail.
    """
    provider = payload.get("provider") or payload.get("vendor")
    if is_spend_snapshot(payload):
        return provider, payload.get("account")
    return provider, payload.get("account_email")


def _usage_dashboard_rows(activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest usage snapshot per provider account for the dashboard.

    A spend snapshot names a usage account by its device-local manifest name,
    so replicated devices may reuse that name for different accounts. The
    origin device is part of a spend snapshot's identity.
    """
    seen: set[tuple[str, str, str]] = set()
    rows: list[dict[str, Any]] = []
    for event in activity:
        if not isinstance(event, dict) or event.get("type") != "usage.snapshot":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        provider = payload.get("provider") or payload.get("vendor")
        if is_spend_snapshot(payload):
            origin = event.get("_origin_device_id")
            key = (
                str(provider),
                origin if isinstance(origin, str) else "",
                str(payload.get("account")),
            )
        else:
            key = (str(provider), "", str(payload.get("account_email")))
        if key in seen:
            continue
        seen.add(key)
        rows.append(event)
    return rows


def _usage_identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Provider and case-folded account a usage snapshot belongs to."""
    provider, account = _usage_account(payload)
    if not isinstance(provider, str) or not isinstance(account, str):
        return None
    return provider, account.casefold()


def _subscription_dashboard_rows(
    inventory: tuple[AISubscription, ...],
    usage: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join every listed subscription with its newest usage snapshots.

    A listed subscription without a snapshot keeps one row with ``usage`` null.
    A snapshot whose account is not listed gets its own row with ``listed``
    false, so that no recorded usage is hidden.
    """
    identities = [
        _usage_identity(event.get("payload") if isinstance(event.get("payload"), dict) else {})
        for event in usage
    ]
    joined: set[int] = set()
    rows: list[dict[str, Any]] = []
    for item in inventory:
        entry = {
            "provider": item.provider,
            "account": item.account,
            "label": item.label,
            "status": item.status,
            "note": item.note,
            "listed": True,
        }
        key = (item.provider, item.account.casefold())
        matches = [index for index, identity in enumerate(identities) if identity == key]
        if not matches:
            rows.append({**entry, "usage": None})
        for index in matches:
            joined.add(index)
            rows.append({**entry, "usage": usage[index]})
    for index, event in enumerate(usage):
        if index in joined:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        provider, account = _usage_account(payload)
        rows.append(
            {
                "provider": provider,
                "account": account,
                "label": None,
                "status": None,
                "note": None,
                "listed": False,
                "usage": event,
            }
        )
    return rows


def _usage_check_times(
    agent_home: Path, checks: Mapping[str, str]
) -> tuple[dict[tuple[str, str], str], set[str], list[str]]:
    """Last successful usage check of each configured ``usage_accounts`` account.

    ``checks`` holds the ``usage.checked.`` meta values, read by the caller
    with the store's other reads. Keyed like ``_usage_identity``: a spend
    account by its name, which its snapshots carry, and a windows or rpc
    account by the identity of its login, which its snapshots carry. A check
    applies only while the identity it read is still the account's identity:
    after another login in the same configuration directory the old check time
    is dropped. The second value names the providers whose accounts are spend
    accounts. A windows or rpc account without a usable login has no snapshot
    of this device to match and is left out; any other read error is returned
    as text.
    """
    from .rpc_usage import load_rpc_account
    from .usage import load_login_account, parse_usage_check, usage_check_key

    try:
        accounts = load_ai_accounts(agent_home, defer_model_configuration=True)
    except StoreError as exc:
        return {}, set(), [str(exc)]
    checked: dict[tuple[str, str], str] = {}
    spend_providers: set[str] = set()
    errors: list[str] = []
    for name in accounts.usage_accounts:
        account = accounts.accounts[name]
        driver = accounts.drivers.get(account.provider)
        snapshot = (
            driver.usage.snapshot
            if driver is not None and driver.usage is not None
            else None
        )
        login = snapshot in ("windows", "rpc")
        if not login:
            spend_providers.add(account.provider)
        meta_key = usage_check_key(account.provider, name)
        if meta_key not in checks:
            continue
        try:
            value, checked_identity = parse_usage_check(checks[meta_key])
        except StoreError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if driver is not None and login:
            # The identity of the login the account's directory holds now.
            read_identity = load_rpc_account if snapshot == "rpc" else load_login_account
            try:
                identity = read_identity(driver, account.config_dir)
            except AuthStale:
                continue
            except StoreError as exc:
                errors.append(f"{name}: {exc}")
                continue
        else:
            identity = name
        if checked_identity != identity.casefold():
            # The check read another identity than the account has now.
            continue
        key = (account.provider, identity.casefold())
        if key not in checked or value > checked[key]:
            checked[key] = value
    return checked, spend_providers, errors


def _subscription_checked_at(
    row: dict[str, Any],
    checked: dict[tuple[str, str], str],
    spend_providers: set[str],
    device: str,
) -> str | None:
    """When this device last read the row's account; ``None`` when it does not poll it."""
    provider, account = row.get("provider"), row.get("account")
    if not isinstance(provider, str) or not isinstance(account, str):
        return None
    usage = row.get("usage")
    payload = usage.get("payload") if isinstance(usage, dict) else None
    spend = (
        is_spend_snapshot(payload) if isinstance(payload, dict) else provider in spend_providers
    )
    if spend:
        # Spend account names are device-local: only a snapshot of this device
        # names an account of this device's configuration.
        if not isinstance(usage, dict) or usage.get("_origin_device_id") != device:
            return None
    key = (provider, account.casefold())
    return checked[key] if key in checked else None


def _subscriptions_dashboard(
    agent_home: Path,
    usage: list[dict[str, Any]],
    checks: Mapping[str, str],
    device: str,
) -> dict[str, Any]:
    """The subscription overview of ``/api/state``; a file error is data, not a failure.

    Each row carries ``checked_at``, the last successful usage read of its
    account on this device, next to the snapshot's ``fetched_at``, the last
    change. ``checks`` are the ``usage.checked.`` meta values of this device.
    """
    error: str | None = None
    try:
        inventory = load_ai_subscriptions(agent_home)
    except StoreError as exc:
        inventory = None
        error = str(exc)
    checked, spend_providers, check_errors = _usage_check_times(agent_home, checks)
    return {
        "configured": inventory is not None,
        "error": error,
        "check_error": "; ".join(check_errors) if check_errors else None,
        "rows": [
            {
                **row,
                "checked_at": _subscription_checked_at(row, checked, spend_providers, device),
            }
            for row in _subscription_dashboard_rows(inventory or (), usage)
        ],
    }


USAGE_NOT_READ = "not read"
USAGE_NO_DATA = "no data"


def _usage_time(value: Any) -> datetime | None:
    """An ISO-8601 time of a usage snapshot as aware UTC, or ``None``.

    Snapshots may come from other devices, so a time that cannot be shown in
    UTC is unknown instead of an error of the whole ``/api/state``.
    """
    try:
        return parse_utc_timestamp(value)
    except (ValueError, OverflowError):
        return None


def _usage_duration_text(seconds: float) -> str:
    """The countdown to a future quota reset, such as ``5 d 19 h``,
    ``3 h 12 min`` or ``4 min``; elapsed times are shown by the browser."""
    minutes = int(max(seconds, 0) // 60)
    if minutes < 1:
        return "<1 min"
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def _usage_resets_in(resets_at: Any, now: datetime) -> str | None:
    """Time until a reset; ``due`` once it has passed; ``None`` when unknown."""
    when = _usage_time(resets_at)
    if when is None:
        return None
    seconds = (when - now).total_seconds()
    return "due" if seconds <= 0 else _usage_duration_text(seconds)


def _usage_rank(window: dict[str, Any]) -> float:
    """Order windows by use; an unreported value ranks below 0 %."""
    value = window.get("used_percent")
    return value if isinstance(value, float) else -1.0


def _usage_percent_text(value: float) -> str:
    """A percentage without trailing zeros, such as ``14%`` or ``21.5%``."""
    return f"{value:g}%"


def _usage_windows(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Quota windows of one snapshot, the index of its headline window (``None``
    when the snapshot has none, such as a window snapshot without a weekly
    window), and a figure without a percentage (a spend snapshot without a
    limit).

    The snapshot kind is read from the payload, as the subscription table does:
    a ``windows`` object is a window snapshot (``windows`` and ``rpc``
    adapters), a usage ``account`` a spend snapshot, and a top-level
    ``used_percent`` a period snapshot. Each window is ``{"name",
    "used_percent", "resets_at"}``; a value the snapshot does not report stays
    ``None``, never 0 %.
    """

    def percent(value: Any) -> float | None:
        """A finite number, or ``None``; replicated data may hold any JSON number."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except OverflowError:
            return None
        return number if math.isfinite(number) else None

    def entry(name: str, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "used_percent": percent(raw.get("used_percent")),
            "resets_at": raw.get("resets_at") if isinstance(raw.get("resets_at"), str) else None,
        }

    def window(name: str, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        return entry(name, raw)

    if isinstance(payload.get("windows"), dict):
        raw = payload["windows"]
        windows = [w for w in (window("5h", raw.get("five_hour")), window("Week", raw.get("weekly"))) if w]
        models = raw.get("weekly_models")
        for item in models if isinstance(models, list) else []:
            model = item.get("model") if isinstance(item, dict) else None
            found = window("Week " + (model if isinstance(model, str) and model else "scoped"), item)
            if found:
                windows.append(found)
        # Windows of a length the rpc adapter maps to no named window.
        others = raw.get("other")
        for item in others if isinstance(others, list) else []:
            if not isinstance(item, dict):
                continue
            minutes = item.get("window_minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                continue
            windows.append(entry(f"{minutes} min", item))
        weekly = [i for i, w in enumerate(windows) if w["name"] == "Week"]
        return windows, weekly[0] if weekly else None, None
    if is_spend_snapshot(payload):
        usage = percent(payload.get("usage_usd"))
        limit = percent(payload.get("limit_usd"))
        remaining = percent(payload.get("limit_remaining_usd"))
        reset = payload.get("limit_reset") if isinstance(payload.get("limit_reset"), str) else None
        used: float | None = None
        if limit and limit > 0 and remaining is not None:
            ratio = (limit - remaining) / limit * 100
            used = round(ratio, 1) if math.isfinite(ratio) else None
        resets_at = reset if _usage_time(reset) else None
        text = None
        if usage is not None:
            text = f"${usage:g} / " + (f"${limit:g}" if limit is not None else "unlimited")
            if reset and resets_at is None:
                text += f" ({reset})"
        return [{"name": "Limit", "used_percent": used, "resets_at": resets_at}], 0, text
    if "used_percent" in payload:
        end = payload.get("period_end") if isinstance(payload.get("period_end"), str) else None
        windows = [{"name": "Period", "used_percent": percent(payload.get("used_percent")), "resets_at": end}]
        products = payload.get("products")
        for item in products if isinstance(products, list) else []:
            if isinstance(item, dict) and isinstance(item.get("product"), str):
                windows.append(
                    {"name": item["product"], "used_percent": percent(item.get("used_percent")), "resets_at": end}
                )
        return windows, 0, None
    return [], None, None


def _usage_overview(
    subscription_rows: list[dict[str, Any]],
    now: datetime,
    drivers: Mapping[str, DriverSpec] | None,
) -> list[dict[str, Any]]:
    """One compact entry per provider for the dashboard.

    Built from the joined subscription rows, so it uses only recorded
    ``usage.snapshot`` payloads and the inventory. Every configured driver and
    every provider of a row has an entry, named by the driver's
    ``display_name`` or else by its id. One without a ``usage`` section in
    ``drivers.json`` (any of ``period``, ``spend``, ``windows`` or ``rpc``)
    and without any recorded snapshot is ``not read``; one with it but no
    figure is ``no data``. ``drivers`` is ``None`` when ``drivers.json``
    cannot be read; then no provider is claimed ``not read``. A provider
    window shows the highest use among its accounts; the headline is the
    headline window of the most used account.
    """
    providers = sorted(
        set(drivers or ())
        | {row["provider"] for row in subscription_rows if isinstance(row.get("provider"), str)}
    )
    overview: list[dict[str, Any]] = []
    for provider in providers:
        spec = drivers.get(provider) if drivers is not None else None
        missing = (
            USAGE_NOT_READ if drivers is not None and (spec is None or spec.usage is None) else USAGE_NO_DATA
        )
        accounts: list[dict[str, Any]] = []
        windows: dict[str, dict[str, Any]] = {}
        headline: dict[str, Any] | None = None
        text: str | None = None
        newest: datetime | None = None
        newest_raw: str | None = None
        for row in subscription_rows:
            if row.get("provider") != provider:
                continue
            event = row.get("usage") if isinstance(row.get("usage"), dict) else None
            payload = event.get("payload") if event and isinstance(event.get("payload"), dict) else None
            account = {
                "account": row.get("account"),
                "label": row.get("label"),
                "listed": row.get("listed"),
                "origin": event.get("_origin_device_id") if event else None,
                "has_snapshot": payload is not None,
                "status": None,
                "used_percent": None,
                "resets_in": None,
                "text": None,
                "recorded": None,
                # The last successful read on this device; "recorded" is the last change.
                "checked_at": row.get("checked_at"),
            }
            accounts.append(account)
            if payload is None:
                account["status"] = missing
                continue
            own, primary, figure = _usage_windows(payload)
            fetched = _usage_time(payload.get("fetched_at"))
            if fetched is not None:
                # The browser shows the age with elapsedText.
                account["recorded"] = payload["fetched_at"]
                if newest is None or fetched > newest:
                    newest, newest_raw = fetched, payload["fetched_at"]
            main = own[primary] if primary is not None else None
            if main is not None:
                account["used_percent"] = main["used_percent"]
                account["resets_in"] = _usage_resets_in(main["resets_at"], now)
            account["text"] = figure
            if account["used_percent"] is None and figure is None:
                account["status"] = USAGE_NO_DATA
            for item in own:
                shown = windows.get(item["name"])
                if shown is None or _usage_rank(item) > _usage_rank(shown):
                    windows[item["name"]] = {
                        "name": item["name"],
                        "used_percent": item["used_percent"],
                        "resets_in": _usage_resets_in(item["resets_at"], now),
                    }
            if main is not None and main["used_percent"] is not None and (
                headline is None or main["used_percent"] > headline["used_percent"]
            ):
                headline = {**main, "resets_in": account["resets_in"]}
            if text is None:
                text = figure
        status: str | None = None
        if headline is None and text is None:
            # A recorded snapshot was read on some device, even without a figure.
            status = USAGE_NO_DATA if any(a["has_snapshot"] for a in accounts) else missing
        name = spec.display_name if spec is not None and spec.display_name else provider
        if headline is not None:
            summary = f"{name} {_usage_percent_text(headline['used_percent'])}"
            if headline["resets_in"]:
                summary += f" {headline['resets_in']}"
        else:
            summary = f"{name} {status or text}"
        overview.append(
            {
                "provider": provider,
                "name": name,
                "status": status,
                "used_percent": headline["used_percent"] if headline else None,
                "window": headline["name"] if headline else None,
                "resets_in": headline["resets_in"] if headline else None,
                "text": None if headline else text,
                "summary": summary,
                "recorded": newest_raw,
                "windows": list(windows.values()),
                "accounts": accounts,
            }
        )
    return overview


def cmd_dashboard(args: list[str]) -> None:
    from .cli_bridge import MAX_REQUEST_BYTES
    from .dashboard_orders import PREFIX as ORDER_PREFIX, handle_order_request

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

        def _html(self, static: Path, missing: str) -> None:
            if not static.is_file():
                self.send_error(500, missing)
                return
            body = static.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def _orders(self, method: str) -> bool:
            parsed = urlparse(self.path)
            if not parsed.path.startswith(ORDER_PREFIX):
                return False
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self.send_error(400)
                return True
            raw = (
                self.rfile.read(min(length, MAX_REQUEST_BYTES + 1))
                if method == "POST" and length > 0
                else b""
            )
            try:
                status, payload = handle_order_request(
                    store,
                    home(),
                    method=method,
                    path=parsed.path,
                    query=parsed.query,
                    headers=self.headers,
                    body=raw,
                )
            except StoreConnectionError as exc:
                try:
                    store.reconnect()
                except StoreConnectionError as rec_exc:
                    self._json(503, {"ok": False, "error": str(rec_exc)})
                    return True
                self._json(503, {"ok": False, "error": str(exc)})
                return True
            self._json(status, payload)
            return True

        def do_GET(self) -> None:
            if self._orders("GET"):
                return
            path = urlparse(self.path).path
            if path == "/api/state":
                try:
                    snap = store.snapshot()
                    device = store.device_id()
                    checks = store.meta_prefix(USAGE_CHECKED_PREFIX)
                except StoreConnectionError:
                    try:
                        store.reconnect()
                    except StoreConnectionError as exc:
                        self._json(503, {"ok": False, "error": str(exc)})
                        return
                    try:
                        snap = store.snapshot()
                        device = store.device_id()
                        checks = store.meta_prefix(USAGE_CHECKED_PREFIX)
                    except StoreError as exc:
                        self._json(503, {"ok": False, "error": str(exc)})
                        return
                except StoreError as exc:
                    self._json(503, {"ok": False, "error": str(exc)})
                    return
                for session in snap.get("sessions") or []:
                    session["can_control"] = session.get("_origin_device_id") == device
                    session["control_connected"] = True
                snap["usage"] = _usage_dashboard_rows(snap.get("activity") or [])
                store_home = getattr(store, "home", None)
                snap["subscriptions"] = _subscriptions_dashboard(
                    store_home if isinstance(store_home, Path) else home(),
                    snap["usage"],
                    checks,
                    device,
                )
                try:
                    drivers: Mapping[str, DriverSpec] | None = load_driver_configuration(
                        store_home if isinstance(store_home, Path) else home()
                    ).drivers
                except AIAccountError:
                    drivers = None
                snap["usage_overview"] = _usage_overview(
                    snap["subscriptions"]["rows"], datetime.now(timezone.utc), drivers
                )
                _canonicalize_snapshot_names(snap)
                self._json(200, snap)
                return
            if path in ("/orders/new", "/orders/new.html"):
                self._html(STATIC_NEW_ORDER, "new order page missing")
                return
            page = _dashboard_page(path)
            if page is not None:
                if not page.is_file():
                    self.send_error(500, "dashboard missing")
                    return
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self._orders("POST"):
                return
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
                ack = apply_control(store, _runtime(), message)
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


def cmd_cli_bridge(args: list[str]) -> None:
    from .cli_bridge import DEFAULT_HOST, DEFAULT_PORT, serve

    port = DEFAULT_PORT
    if args:
        if args[0] != "--port" or len(args) != 2 or not args[1].isdigit():
            die("Usage: agent cli-bridge [--port PORT]")
        port = int(args[1])
    host = os.environ.get("AGENT_CLI_BRIDGE_BIND", DEFAULT_HOST)
    if host == "":
        die("AGENT_CLI_BRIDGE_BIND is set but empty")
    if host not in ("127.0.0.1", "::1"):
        die("cli-bridge bind must be 127.0.0.1 or ::1")
    serve(host=host, port=port)


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


# Neutral keys of an interactive session in ``session.runtime``.
INTERACTIVE_SESSION_KEY = "interactive_session_id"
INTERACTIVE_PENDING_KEY = "interactive_start_pending"


def _runtime_meta(meta: dict[str, Any], drivers: Mapping[str, DriverSpec]) -> dict[str, Any]:
    """Read a session runtime with earlier per-driver keys under neutral names.

    Earlier versions stored the interactive session id and the start marker
    under keys named after the driver (``<driver>_session_id``,
    ``<driver>_start_pending``). Each configured interactive driver's keys map
    to the neutral keys; the next session write persists the mapped runtime.
    """
    out = dict(meta)
    for driver_id, spec in drivers.items():
        if spec.interactive is None:
            continue
        for legacy, key in (
            (f"{driver_id}_session_id", INTERACTIVE_SESSION_KEY),
            (f"{driver_id}_start_pending", INTERACTIVE_PENDING_KEY),
        ):
            if legacy != key and legacy in out:
                value = out.pop(legacy)
                out.setdefault(key, value)
    return out


def _interactive_preflight(
    store: Store,
    sid: str,
    *,
    meta: dict[str, Any] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> tuple[Any, dict[str, Any], str, Any]:
    """Validate an interactive binding before touching its runtime.

    ``provider`` is the driver a start requested; ``None`` accepts the driver
    of the session's interactive role. Returns the role, its binding, the
    stored session id and the loaded AI configuration.
    """
    from .ai_accounts import load_ai_accounts

    if meta is None:
        row = _need(store, "session", sid)
        raw = row.get("runtime")
        meta = dict(raw) if isinstance(raw, dict) else {}
    ai = load_ai_accounts(store.home, defer_model_configuration=True)
    if provider is not None:
        ai.interactive(provider)
    selected = ai.for_session(sid)
    ai.require_model_configuration()
    ai.require_lane_execution()
    if provider is not None and selected.account.provider != provider:
        die("configured interactive role does not match the requested provider")
    ai.interactive(selected.account.provider)
    if model is not None and model != selected.model:
        die("--model does not match the configured interactive role")
    binding = {
        "role": selected.name,
        "account": selected.account.name,
        "provider": selected.account.provider,
        "model": selected.model,
        "access": selected.access,
        "configuration": hashlib.sha256(selected.account.config_dir.encode()).hexdigest(),
    }
    current = _runtime_meta(meta, ai.drivers)
    existing = current.get(INTERACTIVE_SESSION_KEY)
    existing_s = existing if isinstance(existing, str) and existing else ""
    stored_binding = current.get("ai_binding")
    comparable_binding = (
        dict(stored_binding) if isinstance(stored_binding, dict) else stored_binding
    )
    if isinstance(comparable_binding, dict):
        comparable_binding.pop("lane_execution", None)
    if _is_interactive_runtime(current) and comparable_binding != binding:
        die(
            "interactive AI binding changed or is missing; "
            "configure a new session instead of resuming"
        )
    return selected, binding, existing_s, ai


def _interactive_runtime_preflight(
    store: Store,
    sid: str,
    *,
    meta: dict[str, Any] | None = None,
    interactive: bool = False,
) -> str | None:
    """Preflight an interactive runtime while leaving raw terminals unchanged.

    ``interactive`` marks a session about to start as interactive.
    """
    if meta is None:
        row = store.row("session", sid)
        if row is None:
            if not interactive:
                die(f"unknown session: {sid}")
            meta = {}
        else:
            raw = row.get("runtime")
            meta = dict(raw) if isinstance(raw, dict) else {}
    if not _is_interactive_runtime(meta):
        raw_terminal = isinstance(meta.get("tmux_session"), str) and bool(
            meta["tmux_session"]
        )
        if not interactive or raw_terminal:
            return None
    selected, _binding, _existing, _ai = _interactive_preflight(store, sid, meta=meta)
    return selected.account.config_dir


def _is_interactive_runtime(meta: dict[str, Any]) -> bool:
    """True once a start bound the runtime to an interactive driver.

    Every interactive start records ``provider`` and ``ai_binding`` together
    with its session id, so runtimes written by earlier versions qualify too.
    """
    binding = meta.get("ai_binding")
    return (
        meta.get(INTERACTIVE_PENDING_KEY) is True
        or bool(meta.get(INTERACTIVE_SESSION_KEY))
        or bool(meta.get("provider"))
        or (isinstance(binding, dict) and bool(binding.get("provider")))
    )


def _pane_markers(store: Store, sid: str) -> PaneMarkers:
    from .ai_accounts import load_ai_accounts

    return load_ai_accounts(store.home, defer_model_configuration=True).pane_markers(sid)


def _preflight_runtime(
    store: Store,
    runtime: Runtime,
    *,
    pending_meta: dict[str, Any] | None = None,
) -> PreflightRuntime:
    """Return the single access path for operations on session runtimes."""
    if isinstance(runtime, PreflightRuntime) and pending_meta is None:
        return runtime
    if pending_meta is None:
        return PreflightRuntime(
            runtime,
            lambda sid: _interactive_runtime_preflight(store, sid),
            markers_for=lambda sid: _pane_markers(store, sid),
        )
    return PreflightRuntime(
        runtime,
        lambda sid: _interactive_runtime_preflight(store, sid, meta=pending_meta),
        markers_for=lambda sid: _pane_markers(store, sid),
    )


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
    *,
    interactive: bool = False,
) -> str:
    """Start the session's tmux runtime: a raw command or its interactive driver.

    ``interactive`` starts the driver of the session's interactive role;
    ``provider`` does the same and requires that driver.
    """
    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    if row.get("status") != "active":
        die(f"session {sid} is not active")
    interactive = interactive or provider is not None
    if interactive and command:
        die("provider and --cmd cannot be used together")
    if model is not None and not interactive:
        die("--model requires --provider")
    name = tmux_name(sid)
    raw = row.get("runtime")
    meta = dict(raw) if isinstance(raw, dict) else {}
    original = _strip(row)
    command_argv: list[str] | None = None
    start_command = command
    existing_s = ""
    marker_meta: dict[str, Any] | None = None
    if interactive:
        selected, binding, existing_s, ai = _interactive_preflight(
            store, sid, meta=meta, model=model, provider=provider
        )
        spec = ai.interactive(selected.account.provider)
        meta = _runtime_meta(meta, ai.drivers)
        new_id = ""
        if not existing_s:
            # Stored until the new session is attached: a start is in flight.
            marker_meta = {**meta, INTERACTIVE_PENDING_KEY: True, "ai_binding": binding}
            new_id = new_interactive_session_id()
            meta[INTERACTIVE_SESSION_KEY] = new_id
        resolved = interactive_model(selected.model)
        meta["provider"] = selected.account.provider
        meta["model"] = resolved
        meta["ai_binding"] = binding
        runtime = _preflight_runtime(store, runtime, pending_meta=meta)
        command_argv = [*selected.env_prefix(ai.drivers), *interactive_command_argv(
            spec,
            existing=existing_s,
            model=resolved,
            new_id=new_id,
            scrub=lane_scrub_env(ai.drivers),
        )]
        if selected.access == "read-only":
            command_argv.extend(spec.read_only_args)
        command_argv.extend(spec.hardening_args)
        start_command = None
    else:
        if _is_interactive_runtime(meta):
            meta = _runtime_meta(meta, load_driver_configuration(store.home).drivers)
        runtime = _preflight_runtime(store, runtime)
    if marker_meta is not None:
        row["runtime"] = marker_meta
        store.write("session", "update", sid, _strip(row))
    try:
        if interactive:
            if not runtime.available(sid):
                die("tmux is not installed")
            runtime_exists = runtime.exists(sid)
            if runtime_exists and not existing_s:
                runtime.stop(sid)
        started = runtime.start(
            sid,
            start_command,
            cols,
            rows,
            command_argv=command_argv,
            cwd=cwd,
            require_new=interactive and not existing_s,
        )
        if interactive and not existing_s and not started:
            die("tmux session already exists; the interactive driver was not started")
    except BaseException:
        if interactive and not existing_s:
            store.write("session", "update", sid, original)
        raise
    if interactive:
        meta.pop(INTERACTIVE_PENDING_KEY, None)
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
    _preflight_runtime(store, runtime).stop(sid)
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
    runtime = _preflight_runtime(store, runtime)
    if not runtime.available(sid):
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
    runtime = _preflight_runtime(store, runtime)
    if has_data:
        runtime.input_text(sid, data or "")
    else:
        runtime.input_key(sid, key or "")


def _session_resize(store: Store, runtime: Runtime, sid: str, cols: int, rows: int) -> None:
    row = _need(store, "session", sid)
    _require_owned(store, row, "session")
    _preflight_runtime(store, runtime).resize(sid, cols, rows)


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
            interactive = payload.get("interactive")
            if interactive is not None and not isinstance(interactive, bool):
                die("interactive must be a boolean")
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
                interactive=interactive is True,
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
        try:
            config_dir = _interactive_runtime_preflight(store, sid, meta=meta)
            text = redact_lane_text(
                runtime.capture(sid), config_dir=config_dir, limit=None
            )
        except StoreConnectionError:
            raise
        except (SystemExit, StoreError, ValueError) as exc:
            if not _is_interactive_runtime(meta):
                raise
            error = exc.args[0] if getattr(exc, "args", None) else str(exc)
            message = str(error).removeprefix("agent: ")
            print(f"terminal {sid} configuration error: {message}", file=sys.stderr)
            continue
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
    return durable_latest_gates(store, task_id)


def _all_gate_pairs_approved_same_head(
    latest: dict[tuple[str, str], dict], models: ModelSlots
) -> bool:
    if models.second is None:
        return False
    heads: set[str] = set()
    for stage, dim, slot in GATE_PAIRS:
        got = latest.get((stage, dim))
        if got is None:
            return False
        if got.get("vendor") != models.for_slot(slot) or got.get("verdict") != "approved":
            return False
        head = str(got.get("head_sha") or "")
        if not head:
            return False
        heads.add(head)
    return len(heads) == 1


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
    checklist_rows = [r for r in store.rows("checklist_item") if r.get("task_id") == tid]
    checklist = canonical_checklist(
        {str(r["key"]): str(r["status"]) for r in checklist_rows}
    )
    checklist_evidence = canonical_checklist({
        str(r["key"]): r.get("evidence") if isinstance(r.get("evidence"), str) else ""
        for r in checklist_rows
    })
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
    ordered_gates = ordered_snapshot_gates(store, tid)
    gates = [
        {
            "stage": canonical_stage(str(g.get("stage"))),
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
        "payload": task.get("payload"),
        "checklist": checklist,
        "checklist_evidence": checklist_evidence,
        "summaries": {
            "en": task.get("change_summary_en") or "",
            "de": task.get("change_summary_de") or "",
        },
        "gates": gates,
        "local_checks": local_checks,
        "pull_opener": "",
        "trusted_pr_guard": None,
        # Recorded-state read: task display keeps legacy gate rows readable.
        "models": models_from_snapshot(task).as_dict(),
        "implementer": implementer_from_snapshot(task),
        "small_change": (
            task.get("small_change")
            if isinstance(task.get("small_change"), dict)
            else None
        ),
    }


def load_session_tasks(store: Store, session_id: str) -> list[dict]:
    tasks = [t for t in store.rows("task") if t.get("session_id") == session_id]
    tasks.sort(key=lambda t: str(t.get("id") or ""))
    return [load_task_dict(store, str(t["id"])) for t in tasks]


def _chain_snapshot(store: Store, tid: str, extra_head: str | None = None) -> dict:
    task = load_task_dict(store, tid)
    task_row = store.row("task", tid) or {}
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
        # Preserve absence so write/start resolvers can reject legacy tasks;
        # recorded-row readers apply the historical mapping themselves.
        "models": task_row.get("models"),
        "implementer": implementer_from_snapshot(task),
        "models_pinned": task_row.get("models") is not None,
        "gates": task.get("gates") or [],
        "local_checks": task.get("local_checks") or [],
        "head_sha": head,
        "implementer_verdict": last_round.get("implementer_verdict") or "",
        "reviewer_verdict": last_round.get("reviewer_verdict") or "",
        "workflow": task.get("workflow"),
        "state": task.get("state"),
        "checklist": task.get("checklist") or {},
        "session_id": sid,
        "small_change": task.get("small_change"),
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
    items = canonical_checklist(
        {
            str(r["key"]): r
            for r in store.rows("checklist_item")
            if r.get("task_id") == task["id"]
        }
    )
    for key in CHECKLIST[workflow]:
        item = items.get(key)
        status = item["status"] if item else None
        if status == "n_a":
            evidence = item.get("evidence") if item else None
            stored = evidence if isinstance(evidence, str) else None
            if key in N_A_ALLOWED:
                if not isinstance(evidence, str) or evidence == "":
                    die(f"task is not done: checklist {key}=n_a requires evidence")
            elif not second_review_na(key, stored):
                if key in SECOND_REVIEW_KEYS:
                    die(
                        "task is not done: checklist "
                        f"{key}=n_a requires evidence {SECOND_REVIEW_NOT_POSTED}"
                    )
                die(f"task is not done: checklist {key}=n_a is not allowed")
        if key == "mergeable" and status == "ja":
            evidence = item.get("evidence") if item else None
            if not isinstance(evidence, str) or evidence == "":
                die("task is not done: checklist mergeable=ja requires evidence")
    snap = load_task_dict(store, tid)
    _attach_live_one_review(store, snap, row=task)
    blocking = ready_for_done_blocking(snap)
    if not blocking:
        return
    # Rows stored under names of an earlier version read as absent without
    # recorded_slots; the refusal then names that configuration.
    die(f"{_not_done_reason(tid, blocking)}{stored_names_hint([snap])}")


def _not_done_reason(tid: str, blocking: list[str]) -> str:
    """The refusal of ``task done`` for these blocking entries."""
    if any(":summary=missing" in b for b in blocking):
        return "task is not done: summaries missing"
    for b in blocking:
        if ":gate:" in b and b.endswith("=missing"):
            # "{tid}:gate:{stage}/{dim}=missing"
            rest = b.split(":gate:", 1)[1]
            pair = rest.rsplit("=", 1)[0]
            return f"task is not done: missing gate {pair}"
        if ":gate:" in b and "=no_head" in b:
            rest = b.split(":gate:", 1)[1]
            pair = rest.rsplit("=", 1)[0]
            return f"task is not done: gate {pair} missing head_sha"
        if ":gate:head_sha=mismatch" in b:
            return "task is not done: gate heads must match"
        if ":gate:" in b:
            rest = b.split(":gate:", 1)[1]
            pair, _, detail = rest.partition("=")
            if "/" in detail:
                return f"task is not done: gate {pair} vendor mismatch"
            return f"task is not done: gate {pair} not approved"
        if ":local_check:" in b and b.endswith("=fail"):
            name = b.split(":local_check:", 1)[1].rsplit("=", 1)[0]
            return f"task is not done: local_check {name}=fail"
        if b.endswith(":local_check_pass=ja_without_pass_skip"):
            return "task is not done: local_check_pass=ja requires a pass or skip check"
        if ":deviation_granted=" in b:
            return "task is not done: deviation_declared=ja requires deviation_granted=ja"
        prefix = f"{tid}:"
        if b.startswith(prefix):
            body = b[len(prefix) :]
            if body.startswith("workflow="):
                return "unknown workflow"
            if "=" in body and not body.startswith("gate:") and not body.startswith("local_check"):
                key, _, status = body.partition("=")
                if key not in ("summary",):
                    return f"task is not done: checklist {key}={status}"
    return f"task is not done: {', '.join(blocking)}"


def _driver_session_env(agent_home: Path) -> str | None:
    """The interactive session id a driver exports to its hooks.

    Reads the first set variable among the configured ``interactive.session_env``
    names, in configuration order. A variable set to the empty string counts as
    set and yields the empty session id; ``None`` means no configured variable
    is set. Without ``drivers.json``, or when no driver configures
    ``interactive.session_env``, the variable names are unknown, so the call
    fails instead of deciding without a session.
    """
    config = load_driver_configuration(agent_home)
    if not config.present:
        die(DRIVERS_NOT_CONFIGURED)
    names = [
        spec.interactive.session_env
        for spec in config.drivers.values()
        if spec.interactive is not None and spec.interactive.session_env is not None
    ]
    if not names:
        die(SESSION_ENV_NOT_CONFIGURED)
    for name in names:
        if name in os.environ:
            return os.environ[name]
    return None


def cmd_allow(args: list[str]) -> None:
    """Done-gate: exit 0 allow, exit 2 deny, exit 1 usage."""
    action = flag(args, "--action")
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
    session_id = flag(args, "--session") or _driver_session_env(home())

    store = open_store()
    try:
        session_tasks: list[dict] = []
        if action == "task-done":
            if not task_id:
                die("Usage: agent allow --action task-done --task ID")
            else:
                snap = load_task_dict(store, task_id)
                _attach_live_one_review(store, snap)
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
        elif action == "claim-done":
            # Same done predicate as task-done. Read the trusted list live
            # only for an open task whose both second-review keys are already
            # n_a with evidence exactly "second review not posted".
            if session_id:
                session = _need(store, "session", session_id)
                _require_skill(session, "spine")
                session_tasks = load_session_tasks(store, session_id)
                for snap in session_tasks:
                    if str(snap.get("state") or "") in TERMINAL_STATES:
                        continue
                    _attach_live_one_review(store, snap)
            result = evaluate_allow(
                action,
                session_id=session_id,
                task_id=task_id,
                session_tasks=session_tasks,
                create_has_draft=create_has_draft,
            )
        else:
            # pr-ready stays on the local snapshot and does not call GitHub.
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
        models = models_for_new_record(snap)
        ready = next_steps(wf, snap["checklist"], spine_only=True)
        if as_json:
            print(
                json.dumps(
                    {
                        "task": tid,
                        "workflow": wf,
                        "next": [step_to_json(s, models, snapshot=snap) for s in ready],
                        "handoff": (
                            handoff_prompt(
                                ready[0],
                                task_id=str(tid),
                                session_id=str(snap.get("session_id")),
                                models=models,
                                snapshot=snap,
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
                line += (
                    f" role={s.role} vendor={step_vendor_label(s, models, snapshot=snap)}"
                )
            print(line)
        if len(ready) == 1 and ready[0].kind == "agent":
            sys.stdout.write(
                handoff_prompt(
                    ready[0],
                    task_id=str(tid),
                    session_id=str(snap.get("session_id")),
                    models=models,
                    snapshot=snap,
                )
            )
    finally:
        store.close()


def cmd_close_step(args: list[str]) -> None:
    tid = flag(args, "--task")
    supplied_key = flag(args, "--key")
    key = (
        _canonical_input(supplied_key, canonical_key)
        if supplied_key is not None
        else None
    )
    source = flag(args, "--source")
    evidence = flag(args, "--evidence")
    status = flag(args, "--status") or "ja"
    head = flag(args, "--head")
    if not tid or not key or not source or evidence is None or evidence == "":
        die(
            "Usage: agent close-step --task ID --key KEY --source script|human|runner "
            "--evidence TEXT [--status ja|n_a|unavailable] [--head SHA]"
        )
    _refuse_unrecorded(key, f"unknown key {key}")
    if key in REGISTRY_KEYS:
        die(f"{key} is a coordinator precondition check; its resolver or agent order override closes it")
    if status not in CLOSE_STATUSES:
        die("close-step --status must be ja|n_a|unavailable")
    if status == "n_a" and key not in N_A_ALLOWED:
        if not second_review_na(key, evidence):
            if key in SECOND_REVIEW_KEYS:
                die(f"n_a for {key} requires evidence {SECOND_REVIEW_NOT_POSTED}")
            die(f"n_a is not allowed for {key}")
    if status == "unavailable" and key not in GATE_UNAVAILABLE_KEYS:
        die(f"unavailable is not allowed for {key}")
    if source not in ("script", "human", "runner"):
        die("source must be script|human|runner")
    chain_source = "script" if source == "runner" else source
    store = open_store()
    try:
        _require_task_session_active(store, tid)
        snap = _chain_snapshot(store, tid, extra_head=head)
        if status == "n_a" and key in SECOND_REVIEW_KEYS:
            login, config = _live_one_review(store, store.row("task", tid) or {})
            snap["pull_opener"] = login
            snap["trusted_pr_guard"] = config
        wf = str(snap["workflow"])
        verdict = close_allowed(
            wf,
            key,
            checklist=snap["checklist"],
            source=chain_source,
            evidence=evidence,
            snapshot=snap,
            status=status,
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


def _task_repo_and_pr(task: dict) -> tuple[str, str]:
    """Target repository and pull number.

    ``source.repo`` is the pull request target. ``task["repo"]`` is the
    fallback. ``publication_repo`` is only the push remote and is not used.
    """
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    coord = payload.get("coordinator") if isinstance(payload.get("coordinator"), dict) else {}
    source = coord.get("source") if isinstance(coord.get("source"), dict) else {}
    repo = str(source.get("repo") or task.get("repo") or "")
    number = coord.get("pr_number")
    if number is None:
        ref = str(task.get("ref") or "")
        number = ref if ref.isdigit() else ""
    return repo, str(number or "")


def _attach_live_one_review(
    store: Store, snap: dict, row: dict | None = None
) -> None:
    """Copy the live exception onto one open done snapshot.

    ``load_task_dict`` leaves the opener empty and the trusted config unset.
    GitHub is read only for an open task whose two second-review keys are already
    ``n_a`` with evidence exactly ``second review not posted``. A terminal
    task is not read. A lookup error stops the command and does not grant
    the exception. The stored payload copy is not read. ``pr-ready`` does
    not call this.
    """
    if str(snap.get("state") or "") in TERMINAL_STATES:
        return
    if row is None:
        row = store.row("task", str(snap.get("id") or ""))
    if row is None:
        return
    login, config = _done_one_review(
        store,
        row,
        snap.get("checklist") or {},
        snap.get("checklist_evidence") or {},
    )
    snap["pull_opener"] = login
    snap["trusted_pr_guard"] = config


def _done_one_review(
    store: Store, task: dict, checklist: dict, evidence: dict | None = None
) -> tuple[str, dict | None]:
    """Live opener and trusted config for a done decision.

    Callers are task state done, ``allow task-done``, and ``allow claim-done``.
    ``load_task_dict`` stays local and does not call this. The stored payload
    copy is not this decision. Both second-review keys must already be ``n_a`` with
    evidence exactly ``second review not posted`` before a live read. Any
    other evidence stays local. A missing pull, a missing file, or a lookup
    error does not grant the exception. The live read uses the session's
    bound GitHub account. A missing binding, an authentication failure, or a
    login mismatch stops the command. There is no ambient login.
    """
    if not both_second_reviews_na(checklist, evidence):
        return "", None
    try:
        return _live_one_review(store, task)
    except PrGuardLookupError:
        die("trusted review list could not be read")


def _bound_review_runner(store: Store, task: dict) -> Any:
    """Session GitHub account. A missing binding does not fall back to ambient gh."""
    from .github_accounts import AccountError, load_accounts

    try:
        return load_accounts(store.home).for_session(
            str(task.get("session_id") or "")
        ).runner(_exec_argv, require_git=False)
    except AccountError as exc:
        die(str(exc))


def _live_one_review(store: Store, task: dict) -> tuple[str, dict | None]:
    """Opener and trusted default-branch config. Never reads the pull request head.

    Both calls use the session's bound GitHub account. A missing binding, an
    authentication failure, or a login mismatch stops the command. There is
    no ambient login.
    """
    from .github_accounts import AccountError

    repo, number = _task_repo_and_pr(task)
    if not repo or not number:
        return "", None
    runner = _bound_review_runner(store, task)
    try:
        view = runner(["gh", "pr", "view", number, "--repo", repo, "--json", "author"])
    except AccountError as exc:
        die(str(exc))
    if view.returncode != 0:
        die("trusted review list could not be read")
    try:
        body = json.loads(view.stdout or "{}")
    except json.JSONDecodeError:
        die("trusted review list could not be read")
    author = body.get("author") if isinstance(body, dict) else None
    login = str(author.get("login") or "") if isinstance(author, dict) else ""
    if not login:
        return "", None
    try:
        config = fetch_trusted_pr_guard(runner, repo)
    except AccountError as exc:
        die(str(exc))
    except PrGuardLookupError:
        die("trusted review list could not be read")
    return login, config


def _live_merge_base(runner: Any, repo: str, base_sha: str, head_sha: str) -> str | None:
    """Merge base of two commits from the GitHub compare API; None when unreadable."""
    from .github_accounts import AccountError

    try:
        response = runner(
            ["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}"]
        )
    except AccountError:
        return None
    if response.returncode != 0:
        return None
    try:
        compare = json.loads(response.stdout or "{}")
    except json.JSONDecodeError:
        return None
    commit = compare.get("merge_base_commit") if isinstance(compare, dict) else None
    return _full_sha(commit.get("sha") if isinstance(commit, dict) else None)


def _merge_commit_first_parent(runner: Any, repo: str, merge_sha: str) -> str | None:
    """First parent of a merge commit, the base tip before it; None when unreadable."""
    from .github_accounts import AccountError

    try:
        response = runner(["gh", "api", f"repos/{repo}/commits/{merge_sha}"])
    except AccountError:
        return None
    if response.returncode != 0:
        return None
    try:
        body = json.loads(response.stdout or "{}")
    except json.JSONDecodeError:
        return None
    parents = body.get("parents") if isinstance(body, dict) else None
    if not isinstance(parents, list) or not parents or not isinstance(parents[0], dict):
        return None
    return _full_sha(parents[0].get("sha"))


def _close_task_from_pr(store: Store, task: dict) -> None:
    """Close one spine task from fail-closed live pull-request merge evidence."""
    from .github_accounts import AccountError

    tid = str(task.get("id") or "")
    session = _need(store, "session", str(task.get("session_id") or ""))
    _require_owned(store, session, "session")
    _require_owned(store, task, "task")
    _require_skill(session, "spine")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    if payload.get("coordinator"):
        die("coordinator tasks are closed by phase_await_merge")
    if payload.get("error_id"):
        die("error-fix tasks cannot be closed from a pull request")
    repo, number = _task_repo_and_pr(task)
    if not repo or not number:
        die("no pull request attached")

    def ledger_evidence() -> tuple:
        return (
            store.row("task", tid),
            _latest_gates(store, tid),
            [row for row in store.rows("checklist_item") if row.get("task_id") == tid],
            [
                {key: row.get(key) for key in ("id", "name", "result", "ran_at")}
                for row in store.rows("local_check")
                if row.get("task_id") == tid
            ],
        )

    original_evidence = ledger_evidence()
    if original_evidence[0] is None or _strip(original_evidence[0]) != _strip(task):
        die("ledger changed during close; retry")

    runner = _bound_review_runner(store, task)
    try:
        response = runner(["gh", "api", f"repos/{repo}/pulls/{number}"])
    except AccountError as exc:
        die(str(exc))
    if response.returncode != 0:
        die("pull request merge evidence could not be read")
    try:
        pull = json.loads(response.stdout or "{}")
    except json.JSONDecodeError:
        die("pull request merge evidence is invalid")
    if not isinstance(pull, dict) or pull.get("merged") is not True:
        die("pull request is not merged")
    merged_by = pull.get("merged_by")
    if not isinstance(merged_by, dict) or merged_by.get("type") != "User":
        die("pull request was not merged by a human user")
    merged_login = str(merged_by.get("login") or "").strip()
    merged_at = str(pull.get("merged_at") or "").strip()
    merge_commit_sha = str(pull.get("merge_commit_sha") or "").strip()
    head = pull.get("head")
    head_sha = str(head.get("sha") or "").strip() if isinstance(head, dict) else ""
    base = pull.get("base")
    base_ref = base.get("ref") if isinstance(base, dict) else None
    if not merged_login or not merged_at or not merge_commit_sha or not head_sha:
        die("pull request merge evidence is incomplete")

    small_base_verified = False
    if str(task.get("workflow") or "") == "implement":
        evidence = {
            canonical_key(str(row.get("key") or "")): row.get("evidence")
            for row in store.rows("checklist_item")
            if row.get("task_id") == tid
        }
        if any(
            is_small_change_evidence(evidence.get(key))
            for key in ("implementer_done", "reviewer_approved")
        ):
            small = task.get("small_change")
            if not (
                isinstance(small, dict)
                and small.get("verdict") == VERDICT_SMALL
                and small.get("head") == head_sha
            ):
                die(
                    "small change was not measured at the merged head; "
                    "run agent small-change at that head"
                )
            measured_base_ref = (
                small.get("base_ref") if isinstance(small, dict) else None
            )
            if not (
                isinstance(measured_base_ref, str)
                and measured_base_ref
                and base_ref
                and measured_base_ref == base_ref
            ):
                die(
                    "small change was measured against another base branch; "
                    "run agent small-change again"
                )
            # The verdict measured merge_base..head. A rewritten base branch moves the
            # merge base and with it the merged diff; a fast-forward keeps it. After the
            # merge, base.sha may already contain the head, so the base tip is read as
            # the first parent of the merge commit (merge, squash and rebase merges).
            pre_merge_tip = _merge_commit_first_parent(runner, repo, merge_commit_sha)
            measured_merge_base = _full_sha(small.get("merge_base"))
            if pre_merge_tip is None or measured_merge_base is None:
                die(
                    "small change base could not be verified; "
                    "run agent small-change again"
                )
            live_merge_base = _live_merge_base(runner, repo, pre_merge_tip, head_sha)
            if live_merge_base != measured_merge_base:
                die(
                    "the merge base changed since the small change was measured; "
                    "run agent small-change again"
                )
            small_base_verified = True

    latest = _latest_gates(store, tid)
    try:
        first_vendor = models_from_snapshot(task).first
    except (TypeError, ValueError) as exc:
        die(str(exc))
    reviewed_head = ""
    for dimension in ("quality", "logic"):
        gate = latest.get(("review-1", dimension))
        if gate is None:
            die(f"missing approved review-1/{dimension} gate")
        if gate.get("verdict") != "approved":
            die(f"review-1/{dimension} gate is not approved")
        if gate.get("vendor") != first_vendor:
            die(f"review-1/{dimension} gate vendor does not match slot first")
        gate_head = str(gate.get("head_sha") or "").strip()
        if not gate_head or (reviewed_head and gate_head != reviewed_head):
            die("review-1 gates must share one reviewed head")
        reviewed_head = gate_head

    pre_merge_base = ""
    if reviewed_head != head_sha:
        try:
            commit = runner(["gh", "api", f"repos/{repo}/commits/{merge_commit_sha}"])
        except AccountError as exc:
            die(str(exc))
        if commit.returncode != 0:
            die("pre-merge base could not be read")
        try:
            body = json.loads(commit.stdout or "{}")
        except json.JSONDecodeError:
            die("pre-merge base is invalid")
        parents = body.get("parents") if isinstance(body, dict) else None
        if isinstance(parents, list) and parents and isinstance(parents[0], dict):
            pre_merge_base = str(parents[0].get("sha") or "").strip()
        if not pre_merge_base:
            die("pre-merge base is missing")
        for start, end, statuses in (
            (reviewed_head, head_sha, {"ahead", "identical"}),
            (pre_merge_base, reviewed_head, {"ahead", "diverged"}),
        ):
            try:
                comparison = runner(["gh", "api", f"repos/{repo}/compare/{start}...{end}"])
            except AccountError as exc:
                die(str(exc))
            if comparison.returncode != 0:
                die("reviewed head ancestry could not be read")
            try:
                body = json.loads(comparison.stdout or "{}")
            except json.JSONDecodeError:
                die("reviewed head ancestry is invalid")
            if not isinstance(body, dict) or body.get("status") not in statuses:
                die("reviewed head is not after the base and contained in the merged head")
            if start == pre_merge_base:
                ahead_by = body.get("ahead_by")
                if type(ahead_by) is not int or ahead_by <= 0:
                    die("reviewed head is not after the pre-merge base")

    state = str(task.get("state") or "")
    writes: list[tuple[str, str, str]] = []
    if state not in TERMINAL_STATES:
        snap = _chain_snapshot(store, tid, extra_head=reviewed_head)
        checklist = dict(snap.get("checklist") or {})
        for step in steps_for(str(snap["workflow"])):
            key = step.key
            if key not in {"pushed", "review_1_quality", "review_1_logic"}:
                continue
            if checklist.get(key) == "ja":
                continue
            evidence = (
                reviewed_head if key == "pushed"
                else f"gate review-1/{key.removeprefix('review_1_')} approved at {reviewed_head}"
            )
            verdict = close_allowed(
                str(snap["workflow"]),
                key,
                checklist=checklist,
                source="script",
                evidence=evidence,
                snapshot={**snap, "checklist": checklist},
                status="ja",
            )
            if verdict.allowed:
                writes.append((key, "ja", evidence))
                checklist[key] = "ja"
        review_2_exists = any(
            canonical_stage(str(gate.get("stage") or "")) == "review-2"
            for gate in store.rows("review_gate")
            if gate.get("task_id") == tid
        )
        models = models_from_snapshot(task)
        if not review_2_exists and models.second is not None:
            try:
                opener, config = _live_one_review(store, task)
            except PrGuardLookupError:
                die("trusted review list could not be read")
            if one_review_for(config, opener):
                snap["pull_opener"] = opener
                snap["trusted_pr_guard"] = config
                proposed = dict(checklist)
                allowed = True
                for key in ("review_2_quality", "review_2_logic"):
                    if proposed.get(key) == "n_a":
                        continue
                    verdict = close_allowed(
                        str(snap["workflow"]),
                        key,
                        checklist=proposed,
                        source="script",
                        evidence=SECOND_REVIEW_NOT_POSTED,
                        snapshot={**snap, "checklist": proposed},
                        status="n_a",
                    )
                    if not verdict.allowed:
                        allowed = False
                        break
                    proposed[key] = "n_a"
                if allowed:
                    for key in ("review_2_quality", "review_2_logic"):
                        if checklist.get(key) != "n_a":
                            writes.append((key, "n_a", SECOND_REVIEW_NOT_POSTED))

        checklist_items = {
            canonical_key(str(item.get("key") or "")): item
            for item in store.rows("checklist_item")
            if item.get("task_id") == tid
        }
        for key, _status, _evidence in writes:
            if key not in checklist_items:
                die(f"checklist {key} for task {tid} not found")
        current = _need(store, "task", tid)
        current_payload = dict(current.get("payload") or {})
        if any(status == "n_a" for _, status, _ in writes):
            current_payload["one_review_author"] = {
                "pull_opener": snap["pull_opener"],
                "trusted_pr_guard": snap["trusted_pr_guard"],
            }
        batch: list[tuple[str, str, str, dict]] = []
        for key, status, evidence in writes:
            item = checklist_items.get(key)
            assert item is not None
            _update_checklist_item(
                store, item, status=status, source="script", evidence=evidence, writes=batch
            )

        # Decide against the exact checklist and evidence collected for this transaction.
        proposed_snap = load_task_dict(store, tid)
        for key, status, evidence in writes:
            proposed_snap["checklist"][key] = status
            proposed_snap["checklist_evidence"][key] = evidence
        if both_second_reviews_na(
            proposed_snap["checklist"], proposed_snap["checklist_evidence"]
        ):
            if not isinstance(snap.get("trusted_pr_guard"), dict):
                _attach_live_one_review(store, proposed_snap, row=current)
            else:
                proposed_snap["pull_opener"] = snap["pull_opener"]
                proposed_snap["trusted_pr_guard"] = snap["trusted_pr_guard"]
        snap = proposed_snap
        if small_base_verified:
            # Only this live check proves the measured diff is the merged diff.
            snap["small_change_base_verified"] = True
        result = evaluate_allow(
            "task-done",
            session_id=str(current.get("session_id") or ""),
            task_id=tid,
            session_tasks=[snap],
        )
        blocking = [
            item.removeprefix(f"{tid}:") for item in ready_for_done_blocking(snap)
        ]
        if result.allowed:
            current["state"] = "done"
        else:
            current["state"] = "merged"
        current_payload["merge_close"] = {
            "repo": repo,
            "pr": int(number) if number.isdigit() else number,
            "head_sha": head_sha,
            "reviewed_head": reviewed_head,
            "merge_commit_sha": merge_commit_sha,
            "pre_merge_base": pre_merge_base,
            "merged_by": merged_login,
            "merged_at": merged_at,
            "unbooked": blocking,
        }
        current["payload"] = current_payload
        current["updated_at"] = utcnow()
        batch.append(("task", "update", tid, _strip(current)))

        def check_ledger() -> None:
            if ledger_evidence() != original_evidence:
                die("ledger changed during close; retry")

        store.write_many(batch, check=check_ledger)
        state = str(current["state"])

    note = f"task closed from merged PR {repo}#{number}"
    for agent in store.rows("agent"):
        if agent.get("task_id") != tid or agent.get("status") != "working":
            continue
        _require_owned(store, agent, "agent")
        _abort_incomplete_agent(store, agent, note)

    short_head = head_sha[:7]
    if state == "merged":
        closed = _need(store, "task", tid)
        closed_payload = closed.get("payload") if isinstance(closed.get("payload"), dict) else {}
        merge_close = closed_payload.get("merge_close") if isinstance(closed_payload.get("merge_close"), dict) else {}
        unbooked = ", ".join(str(item) for item in merge_close.get("unbooked") or [])
        print(f"task {tid} merged from {repo}#{number} (head {short_head}) unbooked: {unbooked}")
    else:
        print(f"task {tid} {state} from {repo}#{number} (head {short_head})")


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


def _abort_incomplete_agent(store: Store, agent: dict, note: str) -> None:
    """End a lane row without inventing a semantic task/round verdict.

    Agent rows have only ``working``/``done`` lifecycle state; implementer and
    inner-reviewer verdicts live on the round.  Infrastructure and protocol
    failures therefore finish the row with an abort note while deliberately
    leaving the round verdict empty, so the same round can be retried.
    """
    agent["status"] = "done"
    agent["finished_at"] = utcnow()
    agent["note"] = redact_secrets(f"aborted incomplete lane: {note}")
    store.write("agent", "update", str(agent["id"]), _strip(agent))


# Implementer RESULT values that close the lane. ``done`` and ``no-change`` are
# both an implemented round; ``blocked`` ends the task. Missing, duplicate or
# role-invalid values (ask/approved/rejected) stay retryable.
_IMPLEMENTER_VERDICTS = {"done": "done", "no-change": "done", "blocked": "blocked"}


def _lane_diagnostic_note(result: LaneResult) -> str:
    """Bounded, secret-redacted lane diagnostic for notes, evidence and stderr."""
    raw = result.stderr.strip() or result.stdout.strip() or f"lane STATUS={result.status}"
    return redact_diagnostic(raw, config_dir=result.config_dir)


def _agent_handoff_exit(
    step,
    tid: str,
    session_id: str | None,
    models: ModelSlots | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    sys.stdout.write(
        handoff_prompt(
            step,
            task_id=str(tid),
            session_id=session_id,
            models=models,
            snapshot=snapshot,
        )
    )
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
                        step,
                        task_id=str(tid),
                        session_id=str(snap.get("session_id")),
                        models=models_for_new_record(snap),
                        snapshot=snap,
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
            from .github_accounts import load_accounts

            try:
                account = load_accounts(store.home).for_session(str(snap.get("session_id") or ""))
                sha = push_branch(
                    cwd=cwd, runner=account.runner(lambda argv: _exec_argv(argv, cwd=cwd), require_git=True)
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
            from .github_accounts import load_accounts

            try:
                expected = str(snap.get("head_sha") or head or "").strip() or None
                account = load_accounts(store.home).for_session(str(snap.get("session_id") or ""))
                pull_request = _task_pull_request(_need(store, "task", tid))
                if pull_request is not None:
                    pr_repo, pr_number = pull_request
                    evidence = measure_mergeable(
                        cwd=cwd,
                        runner=account.runner(
                            lambda argv: _exec_argv(argv, cwd=cwd), require_git=False
                        ),
                        expected_head=expected,
                        repo=pr_repo,
                        number=pr_number,
                    )
                else:
                    evidence = measure_mergeable(
                        cwd=cwd,
                        runner=account.runner(
                            lambda argv: _exec_argv(argv, cwd=cwd), require_git=True
                        ),
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
                # Lane/agent start: old tasks must be recreated with a model pin.
                models = models_for_new_record(snap)
                vendor = step_vendor(step, models, snapshot=snap)
                if vendor is None:
                    if step.slot == "second" and models.second is None:
                        die("model slot second is external; no agent lane can be started")
                    die(f"{step.key} has no model provider")
                session_id = str(snap.get("session_id") or "")
                if _require_vendor(vendor, store.home, implementer=True).lane is None:
                    # A record-only driver runs outside this launcher.
                    _agent_handoff_exit(step, tid, session_id, models, snap)
                current_round = int(task.get("current_round") or 0)
                round_num: int | None = None
                if role in ("implementer", "reviewer"):
                    round_num = current_round
                # Resolve once before creating a working agent, then launch with
                # that exact binding so audit metadata cannot drift.
                from .ai_accounts import load_ai_accounts

                ai = load_ai_accounts(
                    store.home, defer_model_configuration=True
                )
                selected_lane = ai.for_lane(
                    session_id, role, vendor, require_execution=False
                )
                # Only a coordinator lane start chooses a pool account; refuse
                # before an agent row is written for this lane.
                selected_lane.require_single_account()
                ai.require_model_configuration()
                ai.require_lane_execution()
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
                    if role in ("pr-reviewer-quality", "pr-reviewer-logic"):
                        stage = SLOT_STAGES.get(str(step.slot))
                        if stage is None:
                            die(f"{step.key} has no review stage")
                        start_args.extend(["--stage", stage])
                    cmd_agent(start_args)
                result = launch(
                    role=role,
                    vendor=vendor,
                    spec_file=spec_file,
                    cwd=cwd,
                    tmux=tmux,
                    config_home=store.home,
                    session_id=session_id,
                    binding=selected_lane,
                )
                working = _find_working_agent(
                    store, tid, role=role, vendor=vendor, round_num=round_num
                )
                try:
                    record_lane_run(
                        store,
                        session_id=session_id,
                        result=result,
                        task_id=tid,
                        agent_id=str(working["id"]) if working is not None else None,
                        round_num=round_num,
                    )
                except (Exception, SystemExit) as exc:  # noqa: BLE001 — StoreError is a SystemExit; audit failure cannot change verdict
                    print(
                        f"lane.run write failed: {redact_secrets(str(exc))}",
                        file=sys.stderr,
                    )
                _print_lane_result(result)
                # Re-read the task after the lane: finish/abort writes above the
                # round and checklist, and a pure read of stale state could hand
                # off with an outdated snapshot.
                snap = _chain_snapshot(store, tid, extra_head=head)
                protocol_status = result.status
                semantic_result = result.protocol_result
                implementer_verdict: str | None = None
                if (
                    role == "implementer"
                    and protocol_status == "complete"
                    and semantic_result in _IMPLEMENTER_VERDICTS
                ):
                    implementer_verdict = _IMPLEMENTER_VERDICTS[semantic_result]
                if implementer_verdict is not None:
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
                            implementer_verdict,
                            "--note",
                            f"lane STATUS=complete RESULT={semantic_result}",
                        ]
                    )
                    snap = _chain_snapshot(store, tid, extra_head=head)
                    task = _need(store, "task", tid)
                    if implementer_verdict == "blocked":
                        _agent_handoff_exit(
                            step,
                            tid,
                            str(snap.get("session_id")),
                            models_for_new_record(snap),
                            snap,
                        )
                elif role == "implementer":
                    working = _find_working_agent(
                        store, tid, role=role, vendor=vendor, round_num=round_num
                    )
                    if working is None:
                        die("implementer working agent not found after lane")
                    note = _lane_diagnostic_note(result)
                    if result.status == "complete":
                        # A provider may report complete after selecting the last
                        # of several STATUS records. The joint parser above still
                        # requires exactly one STATUS and one role-valid RESULT.
                        note = (
                            "invalid implementer STATUS/RESULT: "
                            f"{semantic_result or 'missing, duplicate, or ambiguous'}; {note}"
                        )
                    _abort_incomplete_agent(store, working, note)
                    snap = _chain_snapshot(store, tid, extra_head=head)
                    _agent_handoff_exit(
                        step,
                        tid,
                        str(snap.get("session_id")),
                        models_for_new_record(snap),
                        snap,
                    )
                elif result.status != "complete":
                    working = _find_working_agent(
                        store, tid, role=role, vendor=vendor, round_num=round_num
                    )
                    if working is None:
                        die(f"{role} working agent not found after lane")
                    note = _lane_diagnostic_note(result)
                    if role == "reviewer":
                        _abort_incomplete_agent(store, working, note)
                    elif role in ("pr-reviewer-quality", "pr-reviewer-logic"):
                        if not head:
                            die(f"{role} unavailable result requires --head")
                        stage = SLOT_STAGES.get(str(step.slot))
                        if stage is None:
                            die(f"{step.key} has no review stage")
                        dimension = role.removeprefix("pr-reviewer-")
                        cmd_agent(
                            [
                                "finish",
                                "--id",
                                str(working["id"]),
                                "--verdict",
                                "unavailable",
                                "--note",
                                note,
                            ]
                        )
                        cmd_gate(
                            [
                                "record",
                                "--task",
                                tid,
                                "--stage",
                                stage,
                                "--dimension",
                                dimension,
                                "--vendor",
                                vendor,
                                "--verdict",
                                "unavailable",
                                "--head",
                                head,
                                "--agent",
                                str(working["id"]),
                                "--evidence",
                                note,
                            ]
                        )
                        cmd_close_step(
                            [
                                "--task",
                                tid,
                                "--key",
                                str(step.key),
                                "--source",
                                "script",
                                "--evidence",
                                note,
                                "--status",
                                "unavailable",
                                "--head",
                                head,
                            ]
                        )
                    else:
                        die(f"unknown incomplete lane role: {role}")
                    _agent_handoff_exit(
                        step,
                        tid,
                        str(snap.get("session_id")),
                        models_for_new_record(snap),
                        snap,
                    )
                else:
                    _agent_handoff_exit(
                        step,
                        tid,
                        str(snap.get("session_id")),
                        models_for_new_record(snap),
                        snap,
                    )
            else:
                _agent_handoff_exit(
                    step,
                    tid,
                    str(snap.get("session_id")),
                    # Handoff starts new work outside this adapter and also needs a pin.
                    models_for_new_record(snap),
                    snap,
                )
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


def _read_body_file(body_file: str) -> str:
    """Return the text of a body file, or stop with the existing text."""
    path = Path(body_file)
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        die(f"cannot read body file {path}: {exc}")
    return body


def _read_request_text(
    repo: str,
    title: str,
    body_value: str | None,
    body_file: str | None,
) -> str:
    """Validate repo and title and return the body (1..60000 characters)."""
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+",
        repo,
    ) or repo.split("/", 1)[1] in {".", ".."}:
        die("repo must be Owner/Name")
    if not 1 <= len(title) <= 256:
        die("title must be 1..256 characters")
    body = _read_body_file(body_file) if body_file is not None else body_value or ""
    if not 1 <= len(body) <= 60000:
        die("body must be 1..60000 characters")
    return body


def cmd_assign(args: list[str]) -> None:
    from .hub_assign import AssignmentFailure, create_assignment

    if "--no-deviation" not in args:
        die(
            "assign requires --no-deviation "
            "(deviations need an explicit grant on the issue)"
        )
    agent = require_flag(args, "--agent")
    repo = require_flag(args, "--repo")
    title = require_flag(args, "--title")
    body_value = flag(args, "--body")
    body_file = flag(args, "--body-file")
    if (body_value is None) == (body_file is None):
        die("assign requires exactly one of --body or --body-file")
    body = _read_request_text(repo, title, body_value, body_file)
    store = open_store()
    try:
        hub = _hub_from_store(store)
        try:
            try:
                result = create_assignment(
                    hub, agent=agent, repo=repo, title=title, body=body
                )
            except AssignmentFailure as exc:
                print(exc.detail, file=sys.stderr)
                if exc.url:
                    print(exc.url, file=sys.stderr)
                raise SystemExit(1) from None
        finally:
            hub.close()
    finally:
        store.close()
    number = result.get("number")
    url = result.get("url")
    issue = _assignment_issue_url(url)
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or issue is None
        or issue[0].casefold() != repo.casefold()
        or issue[1] != number
    ):
        die("hub returned an invalid assignment response")
    print(f"assigned {repo}#{number} {url}")


_GITHUB_ISSUE_URL_RE = re.compile(
    r"https:\/\/github\.com\/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)"
)


def _assignment_issue_url(url: object) -> tuple[str, int] | None:
    """Return ``(owner/name, number)`` for a credential-free HTTPS GitHub issue URL."""
    if not isinstance(url, str) or url.strip() != url or not url:
        return None
    match = _GITHUB_ISSUE_URL_RE.fullmatch(url)
    if match is None:
        return None
    try:
        number = int(match.group(3))
    except ValueError:
        return None
    return f"{match.group(1)}/{match.group(2)}", number


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


def _print_lane_result(result: LaneResult, status: str | None = None) -> None:
    # The vendor's actual review/implementation text lives only in result.stdout —
    # by the time launch() returns, the tmux pane it was captured from is already
    # killed, so this is the only remaining chance to surface it. Without this, a
    # caller sees STATUS/rc but never the content a gate record --evidence needs.
    # Vendor output can echo a provider credential; every printed diagnostic is
    # bounded and passed through the shared redaction, like the stored notes.
    if result.stdout.strip():
        print(
            redact_diagnostic(
                result.stdout.rstrip(), config_dir=result.config_dir
            )
        )
    if result.stderr.strip():
        print(
            redact_diagnostic(
                result.stderr.rstrip(), config_dir=result.config_dir
            ),
            file=sys.stderr,
        )
    print(
        f"lane role={result.role} vendor={result.vendor} "
        f"STATUS={result.status if status is None else status} "
        f"rc={result.returncode}"
    )


def cmd_models(args: list[str]) -> None:
    """Print model slots or implementer: device setting, or the selection pinned on a task.

    ``agent models [first|second|implementer] [--task UUID] [--json]``
    """
    usage = "Usage: agent models [first|second|implementer] [--task UUID] [--json]"
    as_json = "--json" in args
    tid = flag(args, "--task")
    rest = [a for a in args if a != "--json"]
    if tid is not None:
        i = rest.index("--task")
        rest = rest[:i] + rest[i + 2 :]
    positional = [a for a in rest if not a.startswith("--")]
    if len(positional) > 1 or any(a.startswith("--") for a in rest) or tid == "":
        die(usage)
    if tid is None:
        models, implementer = load_model_configuration(home())
    else:
        store = open_store()
        try:
            task = _need(store, "task", tid)
            models = models_for_new_record(task)
            implementer = implementer_for_new_record(task)
        finally:
            store.close()
    if positional:
        slot = positional[0]
        if slot not in ("first", "second", "implementer"):
            die(usage)
        vendor = implementer if slot == "implementer" else models.for_slot(slot)
        if as_json:
            print(json.dumps({slot: vendor}, sort_keys=True))
        else:
            print(vendor or "external")
        return
    if as_json:
        print(json.dumps(models.as_dict(), sort_keys=True))
        return
    for slot, vendor in models.as_dict().items():
        print(f"{slot}={vendor or 'external'}")


def cmd_lane(args: list[str]) -> None:
    if not args or args[0] != "run":
        die(
            "Usage: agent lane run --role ROLE --vendor DRIVER "
            "--session ID --spec-file PATH [--cwd PATH] [--dry-run] [--no-tmux]"
        )
    rest = args[1:]
    role = require_flag(rest, "--role")
    vendor = require_flag(rest, "--vendor")
    session_id = require_flag(rest, "--session")
    spec_file = require_flag(rest, "--spec-file")
    cwd = _resolve_run_cwd(rest)
    dry_run = "--dry-run" in rest
    tmux = "--no-tmux" not in rest
    if role not in LANE_ROLES:
        die(f"role must be {'|'.join(LANE_ROLES)}")
    _require_vendor(vendor, home())
    result = launch(
        role=role,
        vendor=vendor,
        spec_file=spec_file,
        cwd=cwd,
        dry_run=dry_run,
        tmux=tmux,
        config_home=home(),
        session_id=session_id,
    )
    if dry_run:
        print(" ".join(result.argv))
        return
    lane_store: Store | None = None
    try:
        lane_store = open_store()
        record_lane_run(
            lane_store,
            session_id=session_id,
            result=result,
        )
    except (Exception, SystemExit):  # audit failure cannot change verdict
        print("lane.run audit write failed", file=sys.stderr)
    finally:
        if lane_store is not None:
            lane_store.close()
    # A standalone lane is judged by its single STATUS record. Its RESULT line
    # belongs to the caller's protocol; the joint STATUS/RESULT verdict in
    # ``result.status`` stays with the callers that act on RESULT.
    lane_status = result.lane_status or result.status
    _print_lane_result(result, lane_status)
    if lane_status != "complete":
        raise SystemExit(2)


def _print_account_usage(
    scan: Callable[[Store], list[str]], store: Store
) -> None:
    """Run one ``usage_accounts`` scanner; print new ids and errors."""
    try:
        activity_ids = scan(store)
    except AccountUsageError as exc:
        for activity_id in exc.activity_ids:
            print(f"usage.snapshot {activity_id}")
        for message in exc.errors:
            print(f"usage.snapshot error: {message}", file=sys.stderr)
        return
    except StoreError as exc:
        print(f"usage.snapshot error: {exc}", file=sys.stderr)
        return
    for activity_id in activity_ids:
        print(f"usage.snapshot {activity_id}")


def _print_keepalive(results: list[Any], *, show_fresh: bool) -> bool:
    """Print keep-alive results; ``True`` when an account failed."""
    from .token_keepalive import describe

    failed = False
    for result in results:
        if result.action == "fresh" and not show_fresh:
            continue
        line = describe(result)
        if result.action in ("error", "skipped"):
            print(line, file=sys.stderr)
            if result.action == "error":
                failed = True
        else:
            print(line)
    return failed


def _token_keepalive_tick(agent_home: Path, pace: KeepalivePace) -> None:
    """One daemon keep-alive pass; a failure never stops the usage tick.

    ``pace`` holds the per-account not-before times across passes, so an
    account that failed waits a growing time before its next refresh run.
    """
    # The keep-alive loads the manifest itself on its own five-minute clock; an
    # unreadable manifest is reported as a keep-alive error.
    from . import token_keepalive
    from .ai_accounts import load_ai_accounts as load_manifest

    try:
        accounts = load_manifest(agent_home, defer_model_configuration=True)
        if accounts.token_keepalive_seconds is None:
            return
        results = token_keepalive.keep_alive(accounts, agent_home=agent_home, pace=pace)
        _print_keepalive(results, show_fresh=False)
    except StoreError as exc:
        print(f"{token_keepalive.OUTPUT_PREFIX} error: {exc}", file=sys.stderr)


def cmd_knock(args: list[str]) -> None:
    once = "--once" in args
    store = open_store()
    try:
        runtime = _preflight_runtime(store, _runtime())

        if once:
            for activity_id, status in knock_drain(store, runtime):
                print(f"knock {activity_id} {status}")
            return
        from .pending import scan_pending
        from .runtime import run_argv

        last_poll: float | None = None
        last_keepalive: float | None = None
        # Per-account keep-alive backoff after errors and short renewals.
        keepalive_pace = KeepalivePace()
        # Not-before times per window-usage account; the usage tick stays the only clock.
        window_pace: dict[str, float] = {}
        scan_window_paced = functools.partial(scan_window_usage, pace=window_pace)
        # Each rpc read starts the driver's local server; paced by its poll_seconds.
        rpc_pace: dict[str, float] = {}
        scan_rpc_paced = functools.partial(scan_rpc_usage, pace=rpc_pace)
        from .coordinator import shared_github_runner

        github_runner = shared_github_runner(run_argv, home=store.home)
        while True:
            if usage_poll_due(last_poll, time.monotonic()):
                try:
                    usage_id = scan_session_usage(store)
                    if usage_id:
                        print(f"usage.snapshot {usage_id}")
                except AuthStale:
                    pass
                except StoreError as exc:
                    print(f"usage.snapshot error: {exc}", file=sys.stderr)
                # Before the account scanners, so a renewed token is read this tick.
                if keepalive_due(last_keepalive, time.monotonic()):
                    last_keepalive = time.monotonic()
                    _token_keepalive_tick(store.home, keepalive_pace)
                try:
                    usage_accounts = load_ai_accounts(
                        store.home, defer_model_configuration=True
                    ).usage_accounts
                except StoreError as exc:
                    usage_accounts = ()
                    print(f"usage.snapshot error: {exc}", file=sys.stderr)
                if usage_accounts:
                    # Spend, window-usage and rpc-usage accounts; each scanner
                    # reads only the accounts of its own adapter kind.
                    for scan_accounts in (
                        scan_account_usage,
                        scan_window_paced,
                        scan_rpc_paced,
                    ):
                        _print_account_usage(scan_accounts, store)
                try:
                    created, skipped = scan_merged(store, github_runner)
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
                    for line in scan_github(store, github_runner):
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
                    for line in scan_error_fix(store, github_runner):
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
        adopt_kept_agent_pg_bin,
        agent_argv,
        install_and_start_service,
        run_supervisor,
        service_home,
        service_path,
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
        external = pg_dsn_from_env()
        data_dir = home() / "pg"
        unit = service_path(sys.platform, home())
        recorded = service_home(unit, sys.platform)
        if recorded is not None and not _same_home(recorded, home()):
            die(f"the service unit at {unit} was installed for AGENT_HOME={recorded}; run agent daemon --uninstall with that AGENT_HOME")
        adopt_kept_agent_pg_bin(home())
        uninstall_service(home=home())
        if external is None and _cluster_running(data_dir):
            try:
                stop_cluster(data_dir)
            except PgError as exc:
                die(str(exc))
            print(f"stopped {data_dir}")
        return
    die("Usage: agent daemon [--install|--uninstall]")


def cmd_pg(args: list[str]) -> None:
    if args not in (["status"], ["stop"]):
        die("Usage: agent pg status|stop")
    from .daemon import adopt_kept_agent_pg_bin, service_home, service_path

    adopt_kept_agent_pg_bin(home())
    external = pg_dsn_from_env()
    data_dir = home() / "pg"
    if args == ["status"]:
        if external:
            print(f"home={home()} dsn=external")
            return
        port_file = data_dir / "port"
        try:
            port = port_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            port = "-"
        except OSError as exc:
            die(f"cannot read {port_file}: {exc}")
        exists = _cluster_exists(data_dir)
        print(
            f"home={home()} cluster={data_dir} exists={'yes' if exists else 'no'} "
            f"running={'yes' if _cluster_running(data_dir) else 'no'} port={port}"
        )
        return
    if external:
        die("AGENT_PG_DSN is set; nothing to stop")
    if not _cluster_exists(data_dir):
        die(f"no local postgres cluster under {data_dir}; run agent init")

    unit = service_path(sys.platform, home())
    recorded = service_home(unit, sys.platform)
    if recorded is not None and _same_home(recorded, home()):
        die(f"the device daemon is installed ({unit}) and would start the cluster again; run agent daemon --uninstall instead")
    if not _cluster_running(data_dir):
        print(f"not running {data_dir}")
        return
    try:
        stop_cluster(data_dir)
    except PgError as exc:
        die(str(exc))
    print(f"stopped {data_dir}")


WATCH_USAGE = (
    "Usage: agent watch "
    "pr-merged|pending|assigned [--follow]|session-usage|account-usage|window-usage|"
    "token-keepalive|errors|error-fix"
)


def cmd_watch(args: list[str]) -> None:
    if not args or args[0] not in (
        "pr-merged",
        "pending",
        "assigned",
        "session-usage",
        "account-usage",
        "window-usage",
        "token-keepalive",
        "errors",
        "error-fix",
    ):
        die(WATCH_USAGE)
    if args[0] == "token-keepalive":
        # Needs only the manifest and the driver profiles, not the store.
        if args[1:]:
            die("Usage: agent watch token-keepalive")
        from . import token_keepalive

        accounts = load_ai_accounts(home(), defer_model_configuration=True)
        if accounts.token_keepalive_seconds is None:
            die("token_keepalive is not configured")
        # A manual pass has no backoff; the profile lock still guards each refresh.
        results = token_keepalive.keep_alive(accounts, agent_home=home(), pace=None)
        if not results:
            print(f"{token_keepalive.OUTPUT_PREFIX} none")
        if _print_keepalive(results, show_fresh=True):
            raise SystemExit(1)
        return
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
        if args[0] == "session-usage":
            try:
                activity_id = scan_session_usage(store)
            except AuthStale:
                print("usage.snapshot skipped")
                return
            if activity_id:
                print(f"usage.snapshot {activity_id}")
            else:
                print("usage.snapshot none")
            return
        if args[0] == "account-usage":
            try:
                activity_ids = scan_account_usage(store)
            except AccountUsageError as exc:
                for activity_id in exc.activity_ids:
                    print(f"usage.snapshot {activity_id}")
                for message in exc.errors:
                    print(f"usage.snapshot error: {message}", file=sys.stderr)
                raise SystemExit(1) from None
            for activity_id in activity_ids:
                print(f"usage.snapshot {activity_id}")
            if not activity_ids:
                print("usage.snapshot none")
            return
        if args[0] == "window-usage":
            # Every window-shaped usage adapter: the windows GET and the rpc
            # read. A failing scanner does not stop the other one.
            skipped: list[str] = []
            snapshot_ids: list[str] = []
            scan_errors: list[str] = []
            scanners: tuple[Callable[..., list[str]], ...] = (
                scan_window_usage,
                scan_rpc_usage,
            )
            for scan_windows in scanners:
                try:
                    snapshot_ids.extend(scan_windows(store, skipped=skipped))
                except AccountUsageError as exc:
                    snapshot_ids.extend(exc.activity_ids)
                    scan_errors.extend(exc.errors)
            for activity_id in snapshot_ids:
                print(f"usage.snapshot {activity_id}")
            for message in skipped:
                print(f"usage.snapshot skipped {message}")
            for message in scan_errors:
                print(f"usage.snapshot error: {message}", file=sys.stderr)
            if scan_errors:
                raise SystemExit(1)
            if not snapshot_ids and not skipped:
                print("usage.snapshot none")
            return
        if args[0] == "assigned":
            from .knock import deliver
            from .runtime import run_argv

            extra = args[1:]
            if extra not in ([], ["--follow"]):
                die(WATCH_USAGE)
            follow = extra == ["--follow"]
            while True:
                created, skipped = scan_assigned(store, run_argv, now=utcnow())
                workspace_root = assigned_workspace_root(store)
                sid = assigned_session_id(store.home)
                pending = pending_assigned(store, sid)
                if pending:
                    head_id = pending[0].get("id")
                    if isinstance(head_id, str) and head_id:
                        runtime = _preflight_runtime(store, _runtime())

                        def start(session_id: str, cwd: Path) -> None:
                            _session_start(
                                store,
                                runtime,
                                session_id,
                                None,
                                None,
                                None,
                                cwd=str(cwd),
                                interactive=True,
                            )

                        def pane_up(session_id: str) -> bool:
                            return runtime.exists(session_id)

                        def knock(activity_id: str) -> str:
                            return deliver(store, runtime, activity_id)

                        dispatch_assigned(
                            store,
                            head_id,
                            sync=lambda: _sync_once(store),
                            start=start,
                            knock=knock,
                            workspace_root=workspace_root,
                            pane_up=pane_up,
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
    from .coordinator_config import load_coordinator_config
    if sid in load_coordinator_config(home()):
        die("session belongs to the static issue coordinator")
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
        _interactive_runtime_preflight(store, sid, interactive=True)
        runtime = _preflight_runtime(store, _runtime())
        from .telegram_act import reset_idle_clock

        def runtime_exists(session_id: str) -> bool:
            return runtime.exists(session_id)

        def runtime_capture(session_id: str) -> str:
            return runtime.capture(session_id)

        def knock(activity_id: str) -> str:
            return deliver(store, runtime, activity_id)

        reset_idle_clock(store, working=runtime_exists(sid))

        def start(session_id: str, cwd: Path) -> None:
            _session_start(
                store,
                runtime,
                session_id,
                None,
                None,
                None,
                cwd=str(cwd),
                interactive=True,
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
            pane = runtime_capture(sid) if runtime_exists(sid) else ""
            from .pane_state import pane_is_working
            from .telegram_act import notify_status

            working = pane_is_working(pane, runtime.markers_for(sid))
            line = tick(
                store,
                runtime,
                sid,
                start=start,
                knock=knock,
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
                    working=runtime_exists(sid),
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


def cmd_coordinate(args: list[str]) -> None:
    """Run bounded coordinator work; only this script owns the follow loop."""
    from .coordinator_config import load_coordinator_config

    sid = None
    follow = False
    i = 0
    while i < len(args):
        if args[i] == "--session" and i + 1 < len(args) and sid is None:
            sid = args[i + 1]
            i += 2
        elif args[i] == "--follow" and not follow:
            follow = True
            i += 1
        else:
            die("Usage: agent coordinate --session ID [--follow]")
    config = load_coordinator_config(home())
    if sid is None:
        if not config and not follow:
            print("coordinator unconfigured")
            return
        die("coordinate requires an explicitly selected --session")
    if sid not in config:
        die("coordinator session is not configured")
    from .coordinator import tick

    while True:
        config = load_coordinator_config(home())
        worker = config.get(sid)
        if worker is None:
            print("coordinator disabled by configuration")
            return
        store = open_store()
        try:
            for line in tick(store, worker):
                print(line, flush=True)
        except StoreError as exc:
            if not follow:
                raise
            print(f"coordinator error: {exc}", file=sys.stderr, flush=True)
        finally:
            store.close()
        if not follow:
            return
        time.sleep(worker.poll_seconds)


def cmd_conflicts(args: list[str]) -> None:
    """Run the script-owned comment-mandate conflict worker."""
    from .conflict_mandate_config import load_conflict_config

    sid = None
    follow = False
    once = False
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--session" and i + 1 < len(args) and sid is None:
            sid = args[i + 1]
            i += 2
        elif args[i] == "--follow" and not follow and not once:
            follow = True
            i += 1
        elif args[i] == "--once" and not follow and not once:
            once = True
            i += 1
        elif args[i] == "--dry-run" and not dry_run:
            dry_run = True
            i += 1
        else:
            die("Usage: agent conflicts --session ID [--once|--follow] [--dry-run]")
    config = load_conflict_config(home())
    if sid is None:
        if not config and not follow:
            print("conflicts unconfigured")
            return
        die("conflicts requires an explicitly selected --session")
    if sid not in config:
        die("conflicts session is not configured")
    from .conflict_mandate import tick

    while True:
        config = load_conflict_config(home())
        worker = config.get(sid)
        if worker is None:
            print("conflicts disabled by configuration")
            return
        store = open_store()
        try:
            session = _need(store, "session", sid)
            _require_owned(store, session, "session")
            if session.get("status") != "active":
                die(f"session {sid} is not active")
            _require_skill(session, "conflicts")
            for line in tick(store, worker, dry_run=dry_run):
                print(line, flush=True)
        except StoreError as exc:
            if not follow:
                raise
            print(f"conflicts error: {exc}", file=sys.stderr, flush=True)
        finally:
            store.close()
        if not follow:
            return
        time.sleep(worker.poll_seconds)


def cmd_order(args: list[str]) -> None:
    """Local orders for the coordinator: the command line of the order operations."""
    from .orders import run_order_command

    run_order_command(args)


ACCOUNT_LOGIN_USAGE = "Usage: agent account-login ACCOUNT --email ADDRESS [--no-mailbox]"


def cmd_account_login(args: list[str]) -> None:
    """Sign one subscription into the profile of a configured account."""
    account_name: str | None = None
    email: str | None = None
    no_mailbox = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--email" and email is None and index + 1 < len(args):
            email = args[index + 1]
            index += 2
        elif arg == "--no-mailbox" and not no_mailbox:
            no_mailbox = True
            index += 1
        elif not arg.startswith("-") and account_name is None:
            account_name = arg
            index += 1
        else:
            die(ACCOUNT_LOGIN_USAGE)
    if account_name is None or email is None:
        die(ACCOUNT_LOGIN_USAGE)
    from . import account_login

    accounts = load_ai_accounts(home(), defer_model_configuration=True)
    account = accounts.accounts.get(account_name)
    if account is None:
        die(f"unknown AI account {account_name}")
    account_login.login(
        account,
        email,
        drivers=accounts.drivers,
        agent_home=home(),
        use_mailbox=not no_mailbox,
    )


def cmd_a38(args: list[str]) -> None:
    from .a38 import main as a38_main

    raise SystemExit(a38_main(args))


def cmd_pr_guard(args: list[str]) -> None:
    from .a38_guard import main as guard_main

    raise SystemExit(guard_main(args))


KEY_USAGE = (
    "Usage: agent key create --id ID | list | "
    "pin-hub --id HUB_ID --public-key VALUE [--replace] | whoami --id ID | "
    "release --id ID --repo OWNER/NAME --pr NUMBER"
)


def _key_args(args: list[str], valued: tuple[str, ...], switches: tuple[str, ...]) -> None:
    """Refuse anything but the named flags, each given at most once."""
    seen: set[str] = set()
    idx = 0
    while idx < len(args):
        name = args[idx]
        if name in seen:
            die(KEY_USAGE)
        if name in valued:
            if idx + 1 >= len(args):
                die(f"{name} needs a value")
            seen.add(name)
            idx += 2
            continue
        if name in switches:
            seen.add(name)
            idx += 1
            continue
        die(KEY_USAGE)


def cmd_key(args: list[str]) -> None:
    """Key identity: create a key, pin the hub key, ask the hub which key signed a request
    and whether a pull request may leave draft.

    ``release`` exits 0 when the verified answer releases the pull request, 3
    when it does not, and 1 (``die``) for every error.
    """
    import httpx

    from . import keys

    if not args:
        die(KEY_USAGE)
    sub, rest = args[0], args[1:]
    try:
        if sub == "create":
            _key_args(rest, ("--id",), ())
            print(keys.create_key(home(), require_flag(rest, "--id")))
            return
        if sub == "list":
            _key_args(rest, (), ())
            for key_id, public_key in keys.list_keys(home()):
                print(f"{key_id}  {public_key}")
            return
        if sub == "pin-hub":
            _key_args(rest, ("--id", "--public-key"), ("--replace",))
            hub_id = require_flag(rest, "--id")
            keys.pin_hub_key(
                home(), hub_id, require_flag(rest, "--public-key"), "--replace" in rest
            )
            print(f"pinned hub key {hub_id}")
            return
        if sub in ("whoami", "release"):
            release = sub == "release"
            if release:
                _key_args(rest, ("--id", "--repo", "--pr"), ())
                key_id = require_flag(rest, "--id")
                repository = require_flag(rest, "--repo")
                pull_request = require_flag(rest, "--pr")
                keys.check_release_args(repository, pull_request)
            else:
                _key_args(rest, ("--id",), ())
                key_id = require_flag(rest, "--id")
            pin = keys.load_hub_pin(home())
            store = open_store()
            try:
                hub_url = store.meta("hub_url")
            finally:
                store.close()
            if not hub_url:
                die("device is not paired")
            with httpx.Client(timeout=30.0, follow_redirects=False) as client:
                if not release:
                    answer = keys.whoami(home(), key_id, pin, hub_url, client, utcnow)
                else:
                    answer = keys.release_query(
                        home(), key_id, pin, hub_url, client, utcnow, repository, pull_request
                    )
            if not release:
                print(json.dumps(answer, indent=2))
                return
            print(json.dumps(answer, sort_keys=True))
            if not answer["released"]:
                print(f"not released: {answer['failed']}", file=sys.stderr)
                raise SystemExit(3)
            return
    except keys.KeyProtocolError as exc:
        die(str(exc))
    except OSError as exc:
        die(f"key files under {home()}: {exc}")
    die(f"unknown key command: {sub}")


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
    "small-change": cmd_small_change,
    "local-ci": cmd_local_ci,
    "a38": cmd_a38,
    "pr-guard": cmd_pr_guard,
    "gate": cmd_gate,
    "work": cmd_work,
    "allow": cmd_allow,
    "next": cmd_next,
    "close-step": cmd_close_step,
    "run": cmd_run,
    "pair": cmd_pair,
    "key": cmd_key,
    "sync": cmd_sync,
    "restore": cmd_restore,
    "ping": cmd_ping,
    "status": cmd_status,
    "dashboard": cmd_dashboard,
    "cli-bridge": cmd_cli_bridge,
    "daemon": cmd_daemon,
    "pg": cmd_pg,
    "knock": cmd_knock,
    "lane": cmd_lane,
    "models": cmd_models,
    "watch": cmd_watch,
    "account-login": cmd_account_login,
    "supervise": cmd_supervise,
    "coordinate": cmd_coordinate,
    "conflicts": cmd_conflicts,
    "order": cmd_order,
    "github": cmd_github,
    "query": cmd_query,
    "assign": cmd_assign,
    "subscribe": cmd_subscribe,
    "mail": cmd_mail,
    "runner": cmd_runner,
}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        die(
            "Usage: agent <init|session|skills|activity|task|checklist|round|agent|check|small-change|local-ci|a38|pr-guard|gate|work|"
            "allow|next|close-step|run|pair|key|sync|restore|ping|status|dashboard|cli-bridge|daemon|pg|knock|lane|models|watch|account-login|"
            "github|query|assign|subscribe|mail|supervise|coordinate|conflicts|order|runner> …"
        )
    cmd = args[0]
    if cmd not in COMMANDS:
        die(f"unknown command: {cmd}")
    try:
        COMMANDS[cmd](args[1:])
    except (StoreError, HubError) as exc:
        die(str(exc))
    except UnrecordedSlotsError as exc:
        die(str(exc))
    except PinnedModelSlotsError as exc:
        die(f"invalid models pinned on the task: {exc}")
    except ModelSlotsError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
