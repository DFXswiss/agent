"""Session sandboxes of the control process (05-runner.md sections 4.3, 4.4, 6.1 steps 4-5, 6.3, 7; ADR-008, ADR-025).

``POST /v1/sessions`` (``control_routes/sessions.py``) calls ``SessionManager.create``:

* user ``unit.user_name(id)`` (``s-`` and 30 hex digits of the ID) with the lowest free UID of 20000-29999 (neither a
  user nor a group ID), its own group with the same ID, shell ``/usr/sbin/nologin``, no home created by ``useradd``
* ``/srv/sessions/<id>/{work,home,home/.claude}`` owned by the session user, 0700, and a pre-seeded
  ``home/.claude/.claude.json`` (onboarding done, ``work`` trusted); ``/srv/sessions/<id>`` itself stays root's
  (0711), so the session cannot swap ``work``/``home`` for links that root code (transcript, scanner) would follow
* the session token: 32 random bytes b64u; only its SHA-256 goes into the journal database and the session cache
  (``State.save_session``); the token itself stays in memory until the unit gets it as environment (through the
  root-only launch file and ``systemd-run``'s environment, never on a command line)
* the harness file ``/run/agent-guest/harness/<id>.md`` (``harness.py``)
* journal ``session.state sandbox_ready`` with ``harness_version``

The agent starts (``SessionManager.start``, 05 section 6.1 step 5) once the runner provisioned the model credential:
``PUT /v1/sessions/{id}/credential`` (GST-04) calls ``credential_provisioned(context, id)``; a start before the
credential exists would make the first model request fail. The start registers the initial prompt (with its sender
line, ADR-083) with GST-03's ``PromptRegistry``, journals
``session.state agent_starting`` and opens the tmux session ``s-<id>`` (160x48) whose pane runs ``systemd-run --pty``
with the unit properties of ``sandbox/unit.py`` around the Landlock launcher and the command line of ``driver.py``.
The initial prompt never goes on the command line (HOT-09: Claude Code would start the turn while its MCP servers still
connect): it waits in ``deferred_prompt`` until the TUI is up and the agent listed the platform tools, and is then
typed or pasted like every prompt (``delivery.py``).

``restart`` (HOT-09) starts the agent of a session that is still ``agent_starting`` again: the run is stopped like
``stop`` does (unit, tmux session), the session gets a new token (so the tools mark of the old run no longer counts,
``toolsready.py``) and the same command line runs in a new tmux session; the registered, undelivered initial prompt
stays waiting. ``agent_ended`` of the old run and ``agent_starting`` of the new one are emitted, the state stays
``agent_starting`` (nothing is journaled for it).

A watcher thread per started session reads the exit status of the pane command (``tmux.py``, one status file per run):
status 126 (the launcher) fails the session with ``sandbox_failed``, any other end within ``START_WINDOW_SECONDS``
with ``agent_start_failed``, a later one with ``agent_exited`` - unless the session is being stopped or suspended; a
failed session's unit is stopped as well.

``stop`` (``POST /v1/sessions/{id}/control {"action":"stop"}``): ``systemctl stop agent-session-<id>`` (SIGTERM, 10 s,
SIGKILL: ``TimeoutStopSec=10s``), close the tmux session, ``stopping`` then ``stopped``. Directories, user and harness
file are kept ``RETENTION_SECONDS`` (7 days) and then removed by the cleanup job (``cleanup``). ``stop(..., final=
"suspended")`` is the building block of GST-06's suspend; the session keeps its state (``idle``) until the stop ended,
so the suspend is recorded in the session's root-only meta file (``"suspending": true``) until then, and a restarted
control process finishes it as ``suspended`` (``reattach``; KOR-09, AUD-02 NA-03).

A request with ``restore {claude_session_id, object_id}`` (ADR-072 section 7) creates the sandbox from the context
staged before with ``POST /v1/contexts/{id}/stage`` (``contexts.py``): without a staging of this session ID whose
``context.json`` names the same Claude session the request is ``state_conflict`` and nothing is created. The stage
already rewrote the transcript for this session's directory and wrote the record keys of the restored transcript;
the create only moves the staged transcript, workspace and ``transcript-baseline.json`` into the new directories and
hands them to the session user (``contexts.restore``, KOR-33 review no. 9: no heavy work under the manager's lock or
inside the runner's create timeout) - before ``sandbox_ready``, so the workspace scanner takes the restored files as
its baseline and the transcript follower does not journal the earlier turns again -, removes the staging and starts
the agent with ``--resume <claude_session_id>``. A ``suspended`` session of this ID (it ran here before, moved to
another VM and comes back, review no. 14) is resumed the same way: its conversation files and workspace are replaced
by the staged ones first. ``restore`` and ``resume_claude_session_id`` together are ``request_invalid``.

A ``suspended`` session is started again by a new ``POST /v1/sessions`` with the same ID (resume, normally with
``resume_claude_session_id``): same user and directories, a new token and harness file.

The session notice (KOR-33, ADR-072 section 7): Claude Code keeps the system prompt of a conversation in its
transcript and ignores ``--append-system-prompt-file`` on ``--resume``, so the harness file reaches the model only
when a conversation starts fresh. The meta file therefore records the fact lines the conversation knows (``facts``,
``harness.fact_lines``) and a pending notice (``notice {sentences, facts}``), see ``notice_keys``: a fresh start
knows the current facts; a restore (or a resume into a new sandbox) knows nothing certain - its notice carries the
runner's ``restore.note`` (or ``CONTINUED_SENTENCE``) and every current fact line; a resume of this sandbox's own
conversation keeps what it knows and gets ``RESUMED_SENTENCE`` with the changed lines only when a fact changed.
``take_notice`` (api.sock op ``session.notice``, ``hook_addons/notice.py``) hands the notice out once - the first
decorated hook answer after the start - and records its facts as known; the meta file is root-only and replaced
atomically, so a restart of the control process neither loses nor repeats it. Repeating the request of an
active session answers again (the runner may retry; a pending start lost with a restart of the control process is
prepared again when the request arrives before the start-up hook ended the session, see ``reattach``); a different
request, or a resume with another task, parent or depth, is ``id_conflict``; a ``stopped`` or ``failed`` session is
``state_conflict``.

Extension points (modules of ``control_routes/``, ADR-027 section 4, discovered at call time):

* ``SESSION_EVENTS``: mapping event -> ``callback(context, session_id)`` for ``sandbox_ready``, ``agent_starting``
  (the tmux session exists) and ``agent_ended`` (the pane command ended, for whatever reason): the attachment points
  of recorder, transcript and scanner (GST-05). A failing callback is logged and never changes the session.
* ``CONTROL_ACTIONS``: mapping action -> ``handler(context, session_id)`` for the control actions other than
  ``stop`` (GST-06: ``interrupt``, ``suspend``); see ``control_routes/sessions.py``.

A guest decision ends a live session with ``SessionManager.fail_running`` (``hook_unavailable``, ADR-064).

Interface: ``SessionManager``, ``manager(context)``, ``install(context, manager)``, ``credential_provisioned``,
``stop_session``, ``restart_session``, ``deferred_prompt``, ``take_notice``, ``notice_keys``, ``free_uid``, ``Paths``,
``System``, ``LinuxSystem``.
"""

from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from agent_guest import contexts, harness, registry, tmux, toolsready
from agent_guest.clock import iso_ms
from agent_guest.control import ControlContext
from agent_guest.driver import AgentDriver, ClaudeCodeDriver, DriverError, Launch
from agent_guest.journal import canonical
from agent_guest.prompts import PromptRegistry
from agent_guest.sandbox import unit
from agent_guest.state import Session, StateError

UID_RANGE = range(20000, 30000)
NOLOGIN = "/usr/sbin/nologin"
LAUNCH_DIR = "/run/agent-guest/launch"
META_DIR = "/var/lib/agent-guest/sessions"
RETENTION_SECONDS = 7 * 24 * 3600
CLEANUP_INTERVAL_SECONDS = 3600.0
START_WINDOW_SECONDS = 10.0
WATCH_INTERVAL_SECONDS = 0.25
TMUX_CHECK_SECONDS = 2.0
STOP_TIMEOUT = "10s"
SANDBOX_EXIT = 126
BASE_MODE = 0o711
SECRET_ENV = frozenset({"PLATFORM_SESSION_TOKEN", "ANTHROPIC_AUTH_TOKEN"})
TOKEN_BYTES = 32
# States in which the unit may run (the watcher owns the end); suspended/stopped/failed sessions have no unit.
ENDED_STATES = frozenset({"suspended", "stopped", "failed"})
FINAL_STATES = frozenset({"stopped", "failed"})
EVENTS = ("sandbox_ready", "agent_starting", "agent_ended")
SYSTEM_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
CGROUP_ROOT = "/sys/fs/cgroup"
UNIT_SLICE = "system.slice"
OOM_RESULT = "oom-kill"
# States of a session whose agent runs (the watcher owns its end); re-attached after a restart of the control process.
RUNNING_STATES = frozenset({"agent_starting", "prompt_delivering", "working", "idle"})
RESUMED_SENTENCE = (
    "Your session was paused and runs again on the same VM; your conversation and workspace are as you left them."
)
CONTINUED_SENTENCE = "Your conversation continues in a new sandbox of the platform."
"""The sentence of a restore without the runner's ``restore.note`` and of a resume into a new sandbox."""


class SessionError(Exception):
    """A request the manager refuses; ``code`` is a guest error code (``guest/error.response.json``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Paths:
    """Every path of the session machinery; the defaults are those of the golden image."""

    sessions_root: str = unit.SESSIONS_ROOT
    harness_dir: str = unit.HARNESS_DIR
    rules_file: str = harness.RULES_FILE
    launch_dir: str = LAUNCH_DIR
    meta_dir: str = META_DIR
    claude: str = unit.CLAUDE_BINARY
    launcher: str = unit.LAUNCHER
    mcp_config: str = "/opt/agent-guest/mcp.json"
    exec_paths_file: str = unit.EXEC_PATHS_FILE
    build_file: str = unit.BUILD_FILE
    python: str = "/usr/bin/python3"
    helper: str = tmux.HELPER
    helper_python: str = sys.executable

    def session_dir(self, session_id: str) -> str:
        return f"{self.sessions_root}/{session_id}"

    def harness_file(self, session_id: str) -> str:
        return f"{self.harness_dir}/{session_id}.md"


class System(Protocol):
    """The root-only operating-system calls (injected; ``LinuxSystem`` in the guest)."""

    def used_ids(self) -> set[int]: ...

    def user_uid(self, name: str) -> int | None: ...

    def add_user(self, name: str, uid: int, home: str) -> None: ...

    def remove_user(self, name: str) -> bool: ...

    def chown(self, path: str, uid: int) -> None: ...

    def stop_unit(self, name: str) -> None: ...

    def oom_killed(self, name: str, since: float | None = None) -> bool: ...


class LinuxSystem:
    """``groupadd``/``useradd``/``userdel``, ``chown`` and ``systemctl`` as root."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self._run = run

    def _call(self, argv: list[str]) -> int:
        try:
            result = self._run(argv, capture_output=True, text=True, check=False, env=SYSTEM_ENV, timeout=60)
        except subprocess.TimeoutExpired:
            return -1
        return result.returncode

    def used_ids(self) -> set[int]:
        return {entry.pw_uid for entry in pwd.getpwall()} | {entry.gr_gid for entry in grp.getgrall()}

    def user_uid(self, name: str) -> int | None:
        try:
            return pwd.getpwnam(name).pw_uid
        except KeyError:
            return None

    def add_user(self, name: str, uid: int, home: str) -> None:
        if self._call(["groupadd", "--gid", str(uid), name]) != 0:
            raise OSError(f"groupadd {name} failed")
        useradd = ["useradd", "--uid", str(uid), "--gid", str(uid), "--no-create-home", "--home-dir", home]
        if self._call([*useradd, *NO_SUBORDINATE_IDS, "--shell", NOLOGIN, name]) != 0:
            self._call(["groupdel", name])
            raise OSError(f"useradd {name} failed")

    def remove_user(self, name: str) -> bool:
        removed = self._call(["userdel", name]) == 0
        self._call(["groupdel", name])
        return removed

    def chown(self, path: str, uid: int) -> None:
        os.chown(path, uid, uid, follow_symlinks=False)

    def stop_unit(self, name: str) -> None:
        self._call(["systemctl", "stop", name])

    def oom_killed(self, name: str, since: float | None = None) -> bool:
        """The kernel's OOM killer killed a process of unit ``name`` (since the wall time ``since``): ``oom_kill`` of
        its cgroup's ``memory.events`` while the cgroup exists, else the result ``oom-kill`` that systemd itself
        (``_PID=1``, field ``UNIT``) logged for the unit - the unit is collected with ``--collect`` once it ended, and
        messages the session writes to the journal cannot claim to come from PID 1."""
        try:
            events = Path(f"{CGROUP_ROOT}/{UNIT_SLICE}/{name}.service/memory.events").read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            events = ""
        for line in events.splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill" and value.strip().isdigit() and int(value) > 0:
                return True
        argv = ["journalctl", "_PID=1", f"UNIT={name}.service", "--output", "cat", "--no-pager", "--quiet"]
        if since is not None:
            argv.append(f"--since=@{int(since)}")
        try:
            result = self._run(argv, capture_output=True, text=True, check=False, env=SYSTEM_ENV, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and OOM_RESULT in result.stdout


# useradd gives every new user a range of subordinate UIDs and GIDs (/etc/subuid, /etc/subgid; login.defs
# SUB_UID_COUNT 65536 on Ubuntu 24.04). Session users never need them (no user namespaces), and the ranges ran out
# after a handful of sessions, failing every further session with `sandbox_failed` (attack suite ATS-01).
NO_SUBORDINATE_IDS = ("-K", "SUB_UID_COUNT=0", "-K", "SUB_GID_COUNT=0")


def free_uid(used: set[int]) -> int:
    """Lowest UID of 20000-29999 that is neither a user nor a group ID."""
    for candidate in UID_RANGE:
        if candidate not in used:
            return candidate
    raise SessionError("sandbox_failed")


def new_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


def claude_json(work: str) -> str:
    """The pre-seeded Claude Code state: onboarding done and the workspace trusted (no dialogs at start)."""
    value = {"hasCompletedOnboarding": True, "theme": "dark", "projects": {work: {"hasTrustDialogAccepted": True}}}
    return json.dumps(value, indent=1) + "\n"


@dataclass
class _Start:
    """What the start needs from the request; the token and prompt never leave memory."""

    token: str
    kind: str
    model_id: str | None
    model_env: dict[str, str]
    access: str
    limits: dict[str, int]
    resume: str | None
    prompt: tuple[str, str] | None
    harness_version: str
    speaker: str | None = None
    """The sender line of the initial prompt (ADR-083 section 1), stored with its registration."""


@dataclass
class _Running:
    status_file: str
    started: float
    thread: threading.Thread | None = None
    started_at: float | None = None
    """Wall time of the run's start (``journalctl --since``); ``None`` when unknown."""
    stopped: bool = False
    """``stop`` ended this run: its watcher may see the pane end only after a resume and must not fail the session."""
    start: _Start | None = None
    """What ``restart`` needs to run the agent again (its token blanked: a restart makes a new one)."""


@dataclass
class SessionManager:
    """Sessions of one control process; thread-safe (one lock for transitions, OS calls of ``stop`` outside it)."""

    context: ControlContext
    system: System
    tmux: tmux.Tmux
    driver: AgentDriver
    paths: Paths = field(default_factory=Paths)
    interpreter: tuple[str, str] | None = None
    exec_paths: tuple[str, ...] | None = None
    monotonic: Callable[[], float] = time.monotonic
    watch_interval: float = WATCH_INTERVAL_SECONDS
    tmux_check: float = TMUX_CHECK_SECONDS
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _closed: threading.Event = field(default_factory=threading.Event, init=False)
    _cleanup: threading.Thread | None = field(default=None, init=False)
    _starts: dict[str, _Start] = field(default_factory=dict, init=False)
    _running: dict[str, _Running] = field(default_factory=dict, init=False)
    _stopping: set[str] = field(default_factory=set, init=False)
    _deferred: dict[str, tuple[str, str]] = field(default_factory=dict, init=False)

    # -- creation ---------------------------------------------------------------------------------------------

    def create(self, data: Mapping[str, Any]) -> None:
        """``POST /v1/sessions`` after schema validation; raises ``SessionError``."""
        session_id: str = data["session_id"]
        self._check(data)
        digest = hashlib.sha256(canonical(dict(data)).encode("utf-8", "surrogatepass")).hexdigest()
        with self._lock:
            existing = self.context.state.session(session_id)
            if existing is None:
                self._create(session_id, data, digest, self._staged(session_id, data))
                return
            if existing.state in FINAL_STATES:
                raise SessionError("state_conflict")
            meta = self._meta(session_id)
            if meta.get("identity") != _identity(data):
                raise SessionError("id_conflict")
            if existing.state == "suspended":
                self._resume(existing, data, digest, restarted=True, staged=self._staged(session_id, data))
                return
            if meta.get("request_sha256") != digest:
                raise SessionError("id_conflict")
            if existing.state == "sandbox_ready" and session_id not in self._starts:
                # The pending start was lost with a restart of the control process: prepare it again.
                self._resume(existing, data, digest, restarted=data.get("resume_claude_session_id") is not None)

    def _staged(self, session_id: str, data: Mapping[str, Any]) -> contexts.Staged | None:
        """The staged context a ``restore`` request needs (``state_conflict`` without it); None without ``restore``."""
        restore = data.get("restore")
        if restore is None:
            return None
        found = contexts.staged(self.paths.sessions_root, session_id)
        if found is None or found.context["claude_session_id"] != restore["claude_session_id"]:
            raise SessionError("state_conflict")
        return found

    def _check(self, data: Mapping[str, Any]) -> None:
        if (data["depth"] == 0) != (data["parent_session_id"] is None):
            raise SessionError("request_invalid")
        if data.get("restore") is not None and data.get("resume_claude_session_id") is not None:
            raise SessionError("request_invalid")
        model = data["model"]
        try:
            self.driver.model_env(model["access"], model["env"])
        except DriverError:
            raise SessionError("request_invalid") from None
        context = data["harness"]["context_markdown"]
        prompt = data.get("initial_prompt")
        texts = [context] if prompt is None else [context, prompt["text"]]
        if not context.strip() or any("\x00" in text or not _encodable(text) for text in texts):
            raise SessionError("request_invalid")

    def _facts(self, data: Mapping[str, Any], restarted: bool) -> harness.SessionFacts:
        limits = data["limits"]
        return harness.SessionFacts(
            session_id=data["session_id"],
            task_id=data["task_id"],
            parent_session_id=data["parent_session_id"],
            depth=data["depth"],
            model_id=data["model"]["model_id"],
            memory_max=limits["memory_max"],
            cpu_quota=limits["cpu_quota"],
            tasks_max=limits["tasks_max"],
            runtime_max_sec=limits["runtime_max_sec"],
            restarted=restarted,
        )

    def _fact_lines(self, data: Mapping[str, Any]) -> list[str]:
        return harness.fact_lines(self._facts(data, restarted=False), data["harness"]["context_markdown"])

    def _prepare(self, data: Mapping[str, Any], restarted: bool, resume: str | None) -> tuple[_Start, str]:
        """Token, harness text and a checked unit definition; ``sandbox_failed`` when the image is incomplete."""
        try:
            rules = harness.load_rules(self.paths.rules_file)
        except harness.HarnessError:
            raise SessionError("sandbox_failed") from None
        text = harness.render(rules, self._facts(data, restarted), data["harness"]["context_markdown"])
        model, prompt = data["model"], data.get("initial_prompt")
        start = _Start(
            token=new_token(),
            kind="main" if data["depth"] == 0 else "subagent",
            model_id=model["model_id"],
            model_env=dict(model["env"]),
            access=model["access"],
            limits=dict(data["limits"]),
            resume=resume,
            prompt=None if prompt is None else (prompt["prompt_id"], prompt["text"]),
            harness_version=rules.version,
            speaker=None if prompt is None else prompt.get("speaker"),
        )
        try:
            self._command(data["session_id"], start)
        except DriverError:
            raise SessionError("request_invalid") from None
        except (unit.UnitError, OSError, ValueError):
            raise SessionError("sandbox_failed") from None
        return start, text

    def _create(
        self, session_id: str, data: Mapping[str, Any], digest: str, staged: contexts.Staged | None = None
    ) -> None:
        resume = data.get("resume_claude_session_id")
        if staged is not None:
            resume = data["restore"]["claude_session_id"]
        try:
            start, text = self._prepare(data, restarted=resume is not None, resume=resume)
        except SessionError as error:
            if staged is not None:
                contexts.discard(staged)
            if error.code == "sandbox_failed":
                self._fail_new(session_id)
            raise
        name = unit.user_name(session_id)
        base = self.paths.session_dir(session_id)
        undo: list[Callable[[], object]] = []
        try:
            if self.system.user_uid(name) is not None:
                raise SessionError("sandbox_failed")
            uid = free_uid(self.system.used_ids())
            self.system.add_user(name, uid, f"{base}/home")
            undo.append(lambda: self.system.remove_user(name))
            os.makedirs(self.paths.sessions_root, mode=0o755, exist_ok=True)
            # The base stays root's (0711): a session cannot replace work/ or home/ by links that root code follows.
            os.mkdir(base, BASE_MODE)
            undo.append(lambda: shutil.rmtree(base, ignore_errors=True))
            os.chmod(base, BASE_MODE)
            for sub in ("work", "home", "home/.claude"):
                os.mkdir(f"{base}/{sub}", 0o700)
            seeded = f"{base}/home/.claude/.claude.json"
            Path(seeded).write_text(claude_json(f"{base}/work"), encoding="utf-8")
            os.chmod(seeded, 0o600)
            for path in (f"{base}/work", f"{base}/home", f"{base}/home/.claude", seeded):
                self.system.chown(path, uid)
            if staged is not None:
                contexts.restore(staged, base, lambda path: self.system.chown(path, uid))
            harness.write(self.paths.harness_file(session_id), text)
            undo.append(lambda: Path(self.paths.harness_file(session_id)).unlink(missing_ok=True))
            keys = notice_keys(
                {},
                self._fact_lines(data),
                start="fresh" if resume is None else "replaced",
                sentence=_note(data),
            )
            meta = {
                "request_sha256": digest,
                "identity": _identity(data),
                "access": data["model"]["access"],
                "stopped_at": None,
                **keys,
            }
            self._write_meta(session_id, meta)
        except (OSError, SessionError):
            for step in reversed(undo):
                step()
            self._fail_new(session_id)
            raise SessionError("sandbox_failed") from None
        finally:
            if staged is not None:
                contexts.discard(staged)
        now = self.context.journal.clock.now()
        session = Session(
            id=session_id,
            uid=uid,
            token_sha256=_token_sha256(start.token),
            state="sandbox_ready",
            workspace=f"{base}/work",
            claude_session_id=start.resume,
            created_at=iso_ms(now),
        )
        self._ready(session, start)

    def _fail_new(self, session_id: str) -> None:
        """A session whose sandbox could not be created: recorded as ``failed`` so the ID is not reused."""
        now = self.context.journal.clock.now()
        session = Session(
            id=session_id,
            uid=-1,
            token_sha256=_token_sha256(new_token()),
            state="failed",
            workspace=f"{self.paths.session_dir(session_id)}/work",
            claude_session_id=None,
            created_at=iso_ms(now),
        )
        with self.context.journal.transaction() as db:
            self.context.state.save_session(session, db)
            body = {"state": "failed", "reason": "sandbox_failed", "at": iso_ms(now)}
            self.context.journal.insert(db, "session.state", body, session_id)

    def _resume(
        self,
        existing: Session,
        data: Mapping[str, Any],
        digest: str,
        restarted: bool,
        staged: contexts.Staged | None = None,
    ) -> None:
        """A suspended session (or one whose pending start was lost) gets a new token and harness file; with a
        ``restore`` (``staged``) its conversation files and workspace are first replaced by the staged ones."""
        try:
            self._resume_prepared(existing, data, digest, restarted, staged)
        finally:
            if staged is not None:
                contexts.discard(staged)

    def _resume_prepared(
        self,
        existing: Session,
        data: Mapping[str, Any],
        digest: str,
        restarted: bool,
        staged: contexts.Staged | None,
    ) -> None:
        resume = data.get("resume_claude_session_id") or existing.claude_session_id
        if staged is not None:
            resume = data["restore"]["claude_session_id"]
        start, text = self._prepare(data, restarted=restarted, resume=resume)
        name = unit.user_name(existing.id)
        if self.system.user_uid(name) != existing.uid:
            raise SessionError("sandbox_failed")
        if resume is None:
            start_kind, sentence = "fresh", None
        elif staged is not None:
            start_kind, sentence = "replaced", _note(data)
        else:
            # this sandbox's own conversation; a start lost with a restart of the control process adds no sentence
            start_kind, sentence = "kept", RESUMED_SENTENCE if existing.state == "suspended" else None
        keys = notice_keys(self._meta(existing.id), self._fact_lines(data), start=start_kind, sentence=sentence)
        try:
            if staged is not None:
                base = self.paths.session_dir(existing.id)
                contexts.restore(staged, base, lambda path: self.system.chown(path, existing.uid))
            harness.write(self.paths.harness_file(existing.id), text)
            meta = {
                "request_sha256": digest,
                "identity": _identity(data),
                "access": data["model"]["access"],
                "stopped_at": None,
                **keys,
            }
            self._write_meta(existing.id, meta)
        except OSError:
            raise SessionError("sandbox_failed") from None
        session = Session(
            id=existing.id,
            uid=existing.uid,
            token_sha256=_token_sha256(start.token),
            state="sandbox_ready",
            workspace=existing.workspace,
            claude_session_id=resume,
            created_at=existing.created_at,
        )
        self._ready(session, start)

    def _ready(self, session: Session, start: _Start) -> None:
        with self.context.journal.transaction() as db:
            self.context.state.save_session(session, db)
            body = {"state": "sandbox_ready", "harness_version": start.harness_version, "at": self._at()}
            self.context.journal.insert(db, "session.state", body, session.id)
        self._starts[session.id] = start
        self._emit("sandbox_ready", session.id)

    # -- start --------------------------------------------------------------------------------------------

    def _spec(self, session_id: str, start: _Start) -> unit.SessionSpec:
        if self.interpreter is None:
            self.interpreter = unit.interpreter_paths(self.paths.python)
        if self.exec_paths is None:
            self.exec_paths = unit.load_exec_paths(self.paths.exec_paths_file, self.paths.build_file)
        python, lib_dynload = self.interpreter
        return unit.SessionSpec(
            session_id=session_id,
            token=start.token,
            python=python,
            lib_dynload=lib_dynload,
            exec_paths=self.exec_paths,
            kind="main" if start.kind == "main" else "subagent",
            model_env=start.model_env,
            claude=self.paths.claude,
            launcher=self.paths.launcher,
        )

    def _command(self, session_id: str, start: _Start) -> tuple[list[str], dict[str, str]]:
        """The pane command (``systemd-run --pty`` with the session properties, launcher and agent command line) and
        the environment that carries the token: ``--setenv=NAME`` without value takes it from ``systemd-run``'s own
        environment, so the token never appears on a command line (``/proc/<pid>/cmdline`` is world-readable)."""
        spec = self._spec(session_id, start)
        launch = Launch(
            session_id=session_id,
            harness_file=self.paths.harness_file(session_id),
            resume_claude_session_id=start.resume,
            model_id=start.model_id,
            access=start.access,
        )
        limits = start.limits
        extra = [
            f"MemoryMax={limits['memory_max']}",
            f"CPUQuota={limits['cpu_quota']}%",
            f"TasksMax={limits['tasks_max']}",
            f"RuntimeMaxSec={limits['runtime_max_sec']}",
            f"TimeoutStopSec={STOP_TIMEOUT}",
        ]
        command = unit.launcher_command(spec, self.driver.argv(launch))
        argv = unit.systemd_run(
            spec, command, unit_name=unit.UNIT_PREFIX + session_id, mode="pty", extra_properties=extra
        )
        env: dict[str, str] = {}
        for index, arg in enumerate(argv[: argv.index("--")]):
            name, _, value = arg.removeprefix("--setenv=").partition("=")
            if arg.startswith("--setenv=") and name in SECRET_ENV:
                argv[index] = f"--setenv={name}"
                env[name] = value
        return argv, env

    def start(self, session_id: str) -> bool:
        """Step 5 of 05 section 6.1; ``False`` when there is nothing to start (already started, ended)."""
        with self._lock:
            session = self.context.state.session(session_id)
            if session is None:
                raise SessionError("not_found")
            start = self._starts.get(session_id)
            if session.state != "sandbox_ready" or start is None:
                return False
            del self._starts[session_id]
            self._register_prompt(session_id, start)
            if not self._launch(session, start, first=True):
                return False
        self._emit("agent_starting", session_id)
        return True

    def _launch(self, session: Session, start: _Start, *, first: bool) -> bool:
        """Write the launch file, open the tmux session and watch the run (under the lock); the first launch of a start
        journals ``agent_starting``. ``False`` when the session failed instead."""
        session_id = session.id
        launch_file = f"{self.paths.launch_dir}/{session_id}.json"
        # One status file per run: a watcher of an earlier run never reads the status of a later one.
        status_file = f"{self.paths.launch_dir}/{session_id}.{secrets.token_hex(4)}.status"
        try:
            command, env = self._command(session_id, start)
            tmux.write_launch(launch_file, command, status_file, env)
        except (unit.UnitError, DriverError, OSError, ValueError):
            self._fail(session, "sandbox_failed")
            return False
        if first:
            self._starting(session, start)
        name = tmux.session_name(session_id)
        try:
            self.tmux.new_session(name, [self.paths.helper_python, "-I", self.paths.helper, "exec", launch_file])
        except (tmux.TmuxError, OSError, subprocess.SubprocessError):
            Path(launch_file).unlink(missing_ok=True)
            self._fail(self.context.state.session(session_id) or session, "agent_start_failed")
            return False
        running = _Running(status_file=status_file, started=self.monotonic(), start=replace(start, token=""))
        self._running[session_id] = running
        self._remember_run(session_id, status_file, deferred=self._deferred.get(session_id))
        running.started_at = self.context.journal.clock.now()
        running.thread = threading.Thread(
            target=self._watch, args=(session_id, running), name="agent-guest-session", daemon=True
        )
        running.thread.start()
        return True

    def restart(self, session_id: str) -> bool:
        """Run the agent of a session that is still ``agent_starting`` again (module docstring); ``False`` when the
        session is not starting any more (accepted, stopped, failed, being stopped) or its run is unknown (a run
        re-attached after a restart of the control process: its command line is not known)."""
        with self._lock:
            session = self.context.state.session(session_id)
            running = self._running.get(session_id)
            if (
                session is None
                or session.state != "agent_starting"
                or session_id in self._stopping
                or running is None
                or running.start is None
            ):
                return False
            running.stopped = True  # its watcher must not report the end as an exit of the agent
            del self._running[session_id]
            start = replace(running.start, token=new_token())
        self.system.stop_unit(unit.UNIT_PREFIX + session_id)
        self.tmux.kill_session(tmux.session_name(session_id))
        self._emit("agent_ended", session_id)
        with self._lock:
            session = self.context.state.session(session_id)
            if session is None or session.state != "agent_starting" or session_id in self._stopping:
                return False
            renewed = replace(session, token_sha256=_token_sha256(start.token))
            with self.context.journal.transaction() as db:
                self.context.state.save_session(renewed, db)
            if not self._launch(renewed, start, first=False):
                return False
        self._emit("agent_starting", session_id)
        return True

    def _register_prompt(self, session_id: str, start: _Start) -> None:
        """Register the initial prompt through the prompt registry of GST-03 (05 §6.1 step 5, §4.6 no. 7) with its
        sender line (ADR-083 section 1); a prompt the registry refuses (its ``prompt_id`` was accepted before, or
        names another text) is not delivered."""
        if start.prompt is None:
            return
        prompt_id, text = start.prompt
        try:
            PromptRegistry(self.context.journal, self.context.state).register(
                session_id, prompt_id, text, start.speaker
            )
        except StateError:
            start.prompt = None
            return
        self._deferred[session_id] = start.prompt

    def _starting(self, session: Session, start: _Start) -> None:
        now = self._at()
        with self.context.journal.transaction() as db:
            self.context.state.save_session(_with_state(session, "agent_starting"), db)
            body: dict[str, Any] = {"state": "agent_starting", "harness_version": start.harness_version, "at": now}
            if session.claude_session_id is not None:
                body["claude_session_id"] = session.claude_session_id
            self.context.journal.insert(db, "session.state", body, session.id)

    def deferred_prompt(self, session_id: str) -> tuple[str, str] | None:
        """The initial prompt that did not fit on the command line (``(prompt_id, text)``), handed out once."""
        with self._lock:
            Path(self._deferred_file(session_id)).unlink(missing_ok=True)
            return self._deferred.pop(session_id, None)

    def take_notice(self, session_id: str) -> str | None:
        """The pending session notice (see the module docstring), handed out once; its facts become known."""
        with self._lock:
            meta = self._meta(session_id)
            pending = meta.get("notice")
            if not isinstance(pending, dict):
                return None
            current = _lines(pending.get("facts")) or []
            text = harness.notice(_lines(pending.get("sentences")) or [], current, _lines(meta.get("facts")))
            del meta["notice"]
            meta["facts"] = current
            self._write_meta(session_id, meta)
            return text

    # -- end of the agent ---------------------------------------------------------------------------------

    def _watch(self, session_id: str, running: _Running) -> None:
        name = tmux.session_name(session_id)
        last_check = self.monotonic()
        while True:
            code = tmux.read_status(running.status_file)
            if code is None and self.monotonic() - last_check >= self.tmux_check:
                last_check = self.monotonic()
                if not self.tmux.has_session(name):
                    code = tmux.read_status(running.status_file)
                    code = -1 if code is None else code
            if code is not None:
                break
            if self._closed.wait(self.watch_interval):
                return
        self._ended(session_id, running, code, self.monotonic() - running.started)

    def _ended(self, session_id: str, running: _Running, code: int, elapsed: float) -> None:
        failed = False
        # outside the lock: the journal lookup may take a moment; a stop of the session needs no answer
        oom = (
            code != SANDBOX_EXIT
            and not (running.stopped or session_id in self._stopping)
            and self.system.oom_killed(unit.UNIT_PREFIX + session_id, running.started_at)
        )
        with self._lock:
            if running.status_file:
                Path(running.status_file).unlink(missing_ok=True)
            if self._running.get(session_id) is not running:
                return  # an earlier run of a resumed session
            del self._running[session_id]
            session = self.context.state.session(session_id)
            stopped = running.stopped or session_id in self._stopping
            if session is not None and not stopped and session.state not in ENDED_STATES:
                if code == SANDBOX_EXIT:
                    reason = "sandbox_failed"
                elif oom:
                    reason = "oom"
                elif elapsed < START_WINDOW_SECONDS:
                    reason = "agent_start_failed"
                else:
                    reason = "agent_exited"
                self._fail(session, reason)
                failed = True
        if failed:
            # The pane ended; the unit may outlive it (a killed tmux server): stop it, close what is left.
            self.system.stop_unit(unit.UNIT_PREFIX + session_id)
            self.tmux.kill_session(tmux.session_name(session_id))
        self._emit("agent_ended", session_id)

    def fail_running(self, session_id: str, reason: str) -> bool:
        """End a live session ``failed`` with ``reason`` (the guest's own decision, e.g. ``hook_unavailable``): unit
        stopped, pane closed. False when the session is unknown, has ended or is being stopped."""
        with self._lock:
            session = self.context.state.session(session_id)
            if session is None or session.state in ENDED_STATES or session_id in self._stopping:
                return False
            running = self._running.get(session_id)
            if running is not None:
                running.stopped = True  # the watcher must not report the end it sees as an exit of the agent
            self._fail(session, reason)
        self.system.stop_unit(unit.UNIT_PREFIX + session_id)
        self.tmux.kill_session(tmux.session_name(session_id))
        return True

    def _fail(self, session: Session, reason: str) -> None:
        with self.context.journal.transaction() as db:
            self.context.state.save_session(_with_state(session, "failed"), db)
            body = {"state": "failed", "reason": reason, "at": self._at()}
            if session.claude_session_id is not None:
                body["claude_session_id"] = session.claude_session_id
            self.context.journal.insert(db, "session.state", body, session.id)
        self._starts.pop(session.id, None)
        self._mark_stopped(session.id)

    # -- stop ---------------------------------------------------------------------------------------------

    def stop(self, session_id: str, final: str = "stopped") -> None:
        """Stop the unit and close the tmux session; the session ends ``stopped`` (or ``suspended``, GST-06)."""
        if final not in ("stopped", "suspended"):
            raise ValueError(final)
        with self._lock:
            session = self.context.state.session(session_id)
            if session is None:
                raise SessionError("not_found")
            if session.state in FINAL_STATES or session.state == final or session_id in self._stopping:
                return
            if final == "suspended" and session.state == "sandbox_ready":
                raise SessionError("state_conflict")
            self._stopping.add(session_id)
            running = self._running.get(session_id)
            if running is not None:
                running.stopped = True
            self._starts.pop(session_id, None)
            self._deferred.pop(session_id, None)
            Path(self._deferred_file(session_id)).unlink(missing_ok=True)
            if final == "stopped":
                self._transition(session, "stopping")
            else:
                self._set_suspending(session_id, True)
        try:
            self.system.stop_unit(unit.UNIT_PREFIX + session_id)
            self.tmux.kill_session(tmux.session_name(session_id))
            # A unit registered by systemd-run between the first stop and the closed window is stopped as well.
            self.system.stop_unit(unit.UNIT_PREFIX + session_id)
        finally:
            with self._lock:
                self._transition(self.context.state.session(session_id) or session, final)
                self._mark_stopped(session_id)
                if final == "suspended":
                    self._set_suspending(session_id, False)
                self._stopping.discard(session_id)

    def _set_suspending(self, session_id: str, value: bool) -> None:
        """Record (or clear) a running suspend in the meta file: the session's state says nothing until it ended."""
        meta = self._meta(session_id)
        if value:
            meta["suspending"] = True
        else:
            meta.pop("suspending", None)
        try:
            self._write_meta(session_id, meta)
        except OSError:
            sys.stderr.write("agent-guest-control: suspend of a session not recorded\n")

    def _transition(self, session: Session, state: str) -> None:
        with self.context.journal.transaction() as db:
            self.context.state.save_session(_with_state(session, state), db)
            self.context.journal.insert(db, "session.state", {"state": state, "at": self._at()}, session.id)

    def _mark_stopped(self, session_id: str) -> None:
        meta = self._meta(session_id)
        if meta:
            meta["stopped_at"] = self.context.journal.clock.now()
            self._write_meta(session_id, meta)

    # -- restart of the control process ---------------------------------------------------------------

    def _remember_run(self, session_id: str, status_file: str, deferred: tuple[str, str] | None) -> None:
        """What a restarted control process needs to watch this run (meta file, root only; never the prompt text)."""
        meta = self._meta(session_id)
        meta["run"] = {
            "status_file": status_file,
            "started_at": self.context.journal.clock.now(),
            "deferred_prompt_id": None if deferred is None else deferred[0],
        }
        try:
            self._write_meta(session_id, meta)
            if deferred is not None:
                self._deferred_file_write(session_id, deferred)
        except OSError:
            sys.stderr.write("agent-guest-control: run of a session not recorded\n")

    def _deferred_file(self, session_id: str) -> str:
        return f"{self.paths.meta_dir}/{session_id}.prompt"

    def _deferred_file_write(self, session_id: str, prompt: tuple[str, str]) -> None:
        """The deferred initial prompt until it is handed out (root 0600 in the root-only meta directory; the VM
        holds the plaintext of its workgroup anyway, 00-overview.md section 3 B4)."""
        descriptor, temporary = tempfile.mkstemp(dir=self.paths.meta_dir, prefix=".prompt-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"prompt_id": prompt[0], "text": prompt[1]}, handle, ensure_ascii=False)
        os.replace(temporary, self._deferred_file(session_id))

    def _deferred_file_read(self, session_id: str) -> tuple[str, str] | None:
        try:
            value = json.loads(Path(self._deferred_file(session_id)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        prompt_id, text = (value.get("prompt_id"), value.get("text")) if isinstance(value, dict) else (None, None)
        return (prompt_id, text) if isinstance(prompt_id, str) and isinstance(text, str) else None

    def reattach(self) -> dict[str, str]:
        """The start-up hook of a restarted control process (05 section 6.1; AUD-01 A-05): ``{session_id: action}``.

        * a running agent (``RUNNING_STATES``) is watched again from the status file and start time of its run
          (meta file; without them only the tmux session's end is seen); a deferred initial prompt that was not
          handed out yet is restored from its root-only file, so the TUI-ready watcher still delivers it
        * a ``sandbox_ready`` session whose pending start (token, model, prompt) was lost cannot start any more: it
          fails with ``agent_start_failed`` (unless the runner already repeated the request)
        * a ``stopping`` session finishes its stop, and a running session whose suspend was recorded but not finished
          (meta ``suspending``) finishes it as ``suspended`` - not watched again, which would end it ``agent_exited``
        """
        actions: dict[str, str] = {}
        for listed in self.context.state.sessions():
            with self._lock:
                session = self.context.state.session(listed.id)
                busy = listed.id in self._running or listed.id in self._starts or listed.id in self._stopping
                if session is None or busy:
                    continue
                # A recorded suspend is finished below, outside the lock (``stop`` takes it).
                suspending = session.state in RUNNING_STATES and self._meta(session.id).get("suspending") is True
                if session.state in RUNNING_STATES and not suspending:
                    self._watch_again(session)
                    actions[session.id] = "watched"
                elif session.state == "sandbox_ready":
                    self._fail(session, "agent_start_failed")
                    actions[session.id] = "failed"
            if suspending:
                self.stop(session.id, "suspended")
                actions[session.id] = "suspended"
            elif session.state == "stopping":
                self.stop(session.id)
                actions[session.id] = "stopped"
        return actions

    def _watch_again(self, session: Session) -> None:
        run = self._meta(session.id).get("run")
        run = run if isinstance(run, dict) else {}
        status_file, started_at, deferred = run.get("status_file"), run.get("started_at"), run.get("deferred_prompt_id")
        if not isinstance(status_file, str) or not status_file.startswith(f"{self.paths.launch_dir}/{session.id}."):
            status_file = ""  # unknown: the end is seen when the tmux session is gone
        now = self.context.journal.clock.now()
        elapsed = now - started_at if isinstance(started_at, int | float) and 0 <= started_at <= now else 0.0
        prompt = self._deferred_file_read(session.id)
        if isinstance(deferred, str) and prompt is not None and prompt[0] == deferred:
            self._deferred[session.id] = prompt
        started = started_at if isinstance(started_at, int | float) and elapsed else None
        running = _Running(status_file=status_file, started=self.monotonic() - elapsed, started_at=started)
        self._running[session.id] = running
        running.thread = threading.Thread(
            target=self._watch, args=(session.id, running), name="agent-guest-session", daemon=True
        )
        running.thread.start()

    # -- retention --------------------------------------------------------------------------------------

    def cleanup(self) -> list[str]:
        """Remove directories, harness file and user of sessions that ended (``stopped``/``failed``) 7 days ago, and
        context stagings that were never used (``contexts.prune``)."""
        contexts.prune(self.paths.sessions_root, time.time())
        now = self.context.journal.clock.now()
        removed = []
        for session in self.context.state.sessions():
            if session.state not in FINAL_STATES:
                continue
            meta = self._meta(session.id)
            stopped_at = meta.get("stopped_at")
            if not isinstance(stopped_at, int | float) or now - stopped_at < RETENTION_SECONDS:
                continue
            # Ended sessions never change again (a new request for them is state_conflict): no lock needed.
            shutil.rmtree(self.paths.session_dir(session.id), ignore_errors=True)
            Path(self.paths.harness_file(session.id)).unlink(missing_ok=True)
            name = unit.user_name(session.id)
            if session.uid >= 0 and self.system.user_uid(name) == session.uid and not self.system.remove_user(name):
                continue  # retried by the next run; the metadata file keeps the end time
            Path(self._meta_file(session.id)).unlink(missing_ok=True)
            Path(self._deferred_file(session.id)).unlink(missing_ok=True)
            toolsready.forget(self.context, session.id)
            removed.append(session.id)
        return removed

    def cleanup_loop(self, interval: float = CLEANUP_INTERVAL_SECONDS) -> None:
        while not self._closed.wait(interval):
            try:
                self.cleanup()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def close(self) -> None:
        """Stop the watcher and cleanup threads and wait for them (units and tmux sessions keep running)."""
        self._closed.set()
        with self._lock:
            threads = [running.thread for running in self._running.values() if running.thread is not None]
        if self._cleanup is not None:
            threads.append(self._cleanup)
        for thread in threads:
            thread.join(5)

    # -- helpers ----------------------------------------------------------------------------------------

    def task_id(self, session_id: str) -> str | None:
        """The task of a session (its recorded identity), None when unknown."""
        identity = self._meta(session_id).get("identity")
        task = identity.get("task_id") if isinstance(identity, dict) else None
        return task if isinstance(task, str) else None

    def _at(self) -> str:
        return iso_ms(self.context.journal.clock.now())

    def _meta_file(self, session_id: str) -> str:
        return f"{self.paths.meta_dir}/{session_id}.json"

    def access(self, session_id: str) -> str | None:
        """The model access of the session's current start (``subscription``, ``api_key``, ``platform``; recorded with
        every create or resume, it may change between resumes), None when unknown."""
        access = self._meta(session_id).get("access")
        return access if isinstance(access, str) else None

    def depth(self, session_id: str) -> int | None:
        """The depth of the session's stored identity (0: the main session of its task), None when unknown."""
        identity = self._meta(session_id).get("identity")
        depth = identity.get("depth") if isinstance(identity, dict) else None
        return depth if type(depth) is int else None

    def _meta(self, session_id: str) -> dict[str, Any]:
        try:
            value = json.loads(Path(self._meta_file(session_id)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_meta(self, session_id: str, value: dict[str, Any]) -> None:
        os.makedirs(self.paths.meta_dir, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.paths.meta_dir, prefix=".meta-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        os.replace(temporary, self._meta_file(session_id))

    def _emit(self, event: str, session_id: str) -> None:
        for callback in session_events().get(event, ()):
            try:
                callback(self.context, session_id)
            except Exception:
                sys.stderr.write(f"agent-guest-control: session event {event} failed\n")
                traceback.print_exc(file=sys.stderr)


def _identity(data: Mapping[str, Any]) -> dict[str, Any]:
    """What a session is and stays: a resume or retry with other values is ``id_conflict``."""
    return {key: data[key] for key in ("task_id", "parent_session_id", "depth")}


def _lines(value: object) -> list[str] | None:
    """A list of texts from the meta file; None for anything else (unknown)."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _note(data: Mapping[str, Any]) -> str:
    """The sentence of a restore (the runner's ``restore.note``) or of a resume into a new sandbox."""
    restore = data.get("restore")
    note = restore.get("note") if restore is not None else None
    return note if isinstance(note, str) else CONTINUED_SENTENCE


def notice_keys(previous: Mapping[str, Any], current: list[str], *, start: str, sentence: str | None) -> dict[str, Any]:
    """The ``facts`` and ``notice`` keys of a session's new meta file (see the module docstring).

    ``start`` is ``fresh`` (no ``--resume``: the harness file reaches the model, which then knows ``current``),
    ``replaced`` (the conversation files came from elsewhere: what it knows is unknown, an earlier pending notice is
    void, ``sentence`` is always told) or ``kept`` (this sandbox's own conversation: the known facts and a pending
    notice of ``previous`` stay, ``sentence`` is told only when a fact line changed).
    """
    if start == "fresh":
        return {"facts": current}
    kept = start == "kept"
    known = _lines(previous.get("facts")) if kept else None
    pending = previous.get("notice") if kept else None
    sentences = (_lines(pending.get("sentences")) if isinstance(pending, dict) else None) or []
    changed = known is None or any(line not in known for line in current)
    if sentence is not None and sentence not in sentences and (not kept or changed):
        sentences.append(sentence)
    keys: dict[str, Any] = {} if known is None else {"facts": known}
    if sentences or changed:
        keys["notice"] = {"sentences": sentences, "facts": current}
    return keys


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _with_state(session: Session, state: str) -> Session:
    return Session(
        id=session.id,
        uid=session.uid,
        token_sha256=session.token_sha256,
        state=state,
        workspace=session.workspace,
        claude_session_id=session.claude_session_id,
        created_at=session.created_at,
    )


# -- extension points and process-wide access -----------------------------------------------------------------

EventCallback = Callable[[ControlContext, str], None]
ActionHandler = Callable[[ControlContext, str], None]


def _discovered(attribute: str) -> list[tuple[str, Mapping[str, Callable[[ControlContext, str], None]]]]:
    found = []
    for module in registry.modules(registry.CONTROL_ROUTES):
        value = getattr(module, attribute, None)
        if value is None:
            continue
        if not isinstance(value, Mapping) or not all(isinstance(k, str) and callable(v) for k, v in value.items()):
            raise registry.RegistryError("registry_invalid", f"{module.__name__}.{attribute}")
        found.append((module.__name__, value))
    return found


def session_events() -> dict[str, list[EventCallback]]:
    """``SESSION_EVENTS`` of every ``control_routes`` module, in module order; unknown events are refused."""
    events: dict[str, list[EventCallback]] = {}
    for name, mapping in _discovered("SESSION_EVENTS"):
        for event, callback in mapping.items():
            if event not in EVENTS:
                raise registry.RegistryError("registry_invalid", f"{name}.SESSION_EVENTS")
            events.setdefault(event, []).append(callback)
    return events


def control_actions() -> dict[str, ActionHandler]:
    """``CONTROL_ACTIONS`` of every ``control_routes`` module; ``stop`` and duplicates are refused."""
    actions: dict[str, ActionHandler] = {}
    for _, mapping in _discovered("CONTROL_ACTIONS"):
        for action, handler in mapping.items():
            if action == "stop" or action in actions:
                raise registry.RegistryError("registry_duplicate", action)
            actions[action] = handler
    return actions


_MANAGER = "_session_manager"
_manager_lock = threading.Lock()


def install(context: ControlContext, created: SessionManager) -> SessionManager:
    """Attach ``created`` to ``context`` (tests and ``manager``); it closes with the control process."""
    vars(context)[_MANAGER] = created
    context.on_close(created.close)
    return created


def manager(context: ControlContext) -> SessionManager:
    """The manager of this control process, created on first use with the golden image's defaults."""
    with _manager_lock:
        existing = vars(context).get(_MANAGER)
        if isinstance(existing, SessionManager):
            return existing
        created = SessionManager(
            context=context,
            system=LinuxSystem(),
            tmux=tmux.Tmux(),
            driver=ClaudeCodeDriver(context.info.claude_version),
        )
        created._cleanup = threading.Thread(target=created.cleanup_loop, name="agent-guest-cleanup", daemon=True)
        created._cleanup.start()
        return install(context, created)


def credential_provisioned(context: ControlContext, session_id: str) -> bool:
    """Called by ``PUT /v1/sessions/{id}/credential`` (GST-04) after the credential is stored: starts the agent."""
    return manager(context).start(session_id)


def stop_session(context: ControlContext, session_id: str, final: str = "stopped") -> None:
    manager(context).stop(session_id, final)


def restart_session(context: ControlContext, session_id: str) -> bool:
    return manager(context).restart(session_id)


def deferred_prompt(context: ControlContext, session_id: str) -> tuple[str, str] | None:
    return manager(context).deferred_prompt(session_id)


def take_notice(context: ControlContext, session_id: str) -> str | None:
    return manager(context).take_notice(session_id)
