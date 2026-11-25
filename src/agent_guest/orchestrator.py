"""The main orchestrator inside a session (ADR-082 sections 6 and 9.7; 05-runner.md section 17).

The user's main orchestrator - its agent tool with the agent's own key - may run in one main session of this VM. The
control process (root) never holds the key: it runs the tool under the system account ``agent-orch`` (created by
``images/provision/install.sh``), whose state directory ``/var/lib/agent-orch`` is ``0700`` of that account, so the
session users (and Claude Code) can neither read the key nor start the tool.

* ``keys(session_id, data)`` (``POST /v1/sessions/{id}/orchestrator/keys``): only for an active main session in a
  running state (``working``/``idle``) while no other session of the VM is the orchestrator (``state_conflict``). The
  tool draws the keys (``agent-platform pair --session``) and the public part ``{pairing_id, pub, pop, fp}`` is
  recorded (``guest_meta`` key ``orchestrator``) and returned; a repeat for the same session before the handover draws
  fresh keys that replace the pending pairing (the runner asks only while the hub has no live pairing of the session:
  an expired one, or a handover not claimed in time).
* ``handover(session_id, data)`` (``POST /v1/sessions/{id}/orchestrator/handover``): the handover the runner checked
  under the task owner's key, for the recorded pairing (another pairing, or no keys: ``state_conflict``; a repeat of
  the same handover is accepted, a different one after it is ``state_conflict``). In the background the tool claims
  (``agent-platform pair --session --handover``, its hub requests go through ``relay``); on success the tool's MCP
  server starts (``agent-platform mcp --socket``, unit ``agent-orch-mcp``) and, once the session is ``idle``, its
  agent runs again with ``--resume`` and the second MCP configuration (``sessions.reload_session``). A failed claim
  ends the phase ``failed`` (the runner may ask for new keys).
* ``relay(args)`` (op ``relay`` on ``orch.sock``, ``root:agent-orch 0660``): a signed hub request of the tool,
  journaled as the pending call ``relay`` (``hub.relay``) of the orchestrator session while the handover or the claim
  is recorded; waits ``RELAY_WAIT_SECONDS`` for the runner's answer (``POST /v1/relay/{id}/answer``) and returns it,
  or the runner's refusal (``refused``, ``hub_unreachable``, ``too_large``) or ``timeout`` as the error code.
* ``mcp_socket(session_id)`` (op ``orchestrator.mcp`` on ``api.sock``): the tool's MCP socket for the session that is
  the claimed orchestrator, ``not_found`` for every other session.
* the end (``Tool.stop``: the unit stopped, the state directory's content deleted, keys included; the record removed):
  when the session's agent run ends because the session is stopped or failed (``agent_ended`` with the session
  ``stopping``, ``stopped``, ``failed`` or unknown; never for a suspend or the orchestrator's own reload: the hub keeps
  a suspended session's orchestrator, and the runner never suspends it when idle), when the runner ends it
  (``end_pairing``, ``POST /v1/sessions/{id}/orchestrator/end``: revoked at the hub, or the session ended while it was
  suspended), and when a control process starts while the recorded session no longer runs.

``Tool`` is the boundary to the operating system (``LinuxTool``: ``runuser``, ``systemd-run``, ``systemctl``); the
tests replace it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_guest.journal import JournalError, canonical
from agent_guest.state import INACTIVE_STATES

ACCOUNT = "agent-orch"
TOOL = "/opt/agent-orch/bin/agent-platform"
STATE_DIR = "/var/lib/agent-orch/state"
MCP_UNIT = "agent-orch-mcp"
MCP_CONFIG = "/opt/agent-guest/mcp-orchestrator.json"
META_KEY = "orchestrator"
RUNNING_STATES = frozenset({"working", "idle"})
ENDING_STATES = frozenset({"stopping", "stopped", "failed"})
PHASES = ("keys", "handover", "claimed", "failed")
RELAY_PHASES = frozenset({"handover", "claimed"})
RELAY_ERRORS = frozenset({"refused", "hub_unreachable", "too_large"})
RELAY_WAIT_SECONDS = 40.0
TOOL_TIMEOUT_SECONDS = 120.0
RELOAD_POLL_SECONDS = 1.0
RELOAD_MAX_SECONDS = 24 * 3600.0
SYSTEM_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
SENTENCE = (
    "You are now the user's main orchestrator: the tools of the MCP server orchestrator (search and run) act for the "
    "user on agent.dfx.swiss, every call a signed request of the user's agent. Read its guide first: run with args "
    '["guide"].'
)


class OrchestratorError(Exception):
    """A refusal with a control-protocol code (``state_conflict``, ``not_found``, ``unavailable``, ...)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ToolFailed(Exception):
    """The tool ended without ``{"ok": true, ...}`` (``code``: its error code or ``tool_failed``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Record:
    """The orchestrator of this VM (``guest_meta`` key ``orchestrator``)."""

    session_id: str
    phase: str
    keys: dict[str, Any]
    handover_sha256: str | None = None

    @property
    def pairing_id(self) -> str:
        return str(self.keys["pairing_id"])

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "keys": self.keys,
            "handover_sha256": self.handover_sha256,
        }


def parse_record(text: str) -> Record | None:
    """The record of the meta row; None for anything malformed (treated as no orchestrator)."""
    try:
        value = json.loads(text)
        record = Record(
            session_id=value["session_id"],
            phase=value["phase"],
            keys=value["keys"],
            handover_sha256=value["handover_sha256"],
        )
    except (ValueError, KeyError, TypeError):
        return None
    if record.phase not in PHASES or not isinstance(record.keys, dict) or "pairing_id" not in record.keys:
        return None
    return record


class Tool(Protocol):
    def run(self, args: list[str], stdin: Mapping[str, Any]) -> dict[str, Any]: ...

    def serve(self) -> None: ...

    def stop(self) -> None: ...


def tool_output(code: int, stdout: str) -> dict[str, Any]:
    """The result object of one tool command (``ToolFailed`` with the tool's error code otherwise)."""
    try:
        document = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise ToolFailed("tool_failed")
    if code != 0 or document.get("ok") is not True:
        error = document.get("error")
        raise ToolFailed(error if isinstance(error, str) else "tool_failed")
    return document


@dataclass
class LinuxTool:
    """The agent tool as the account ``agent-orch``; nothing of the control process's environment is inherited."""

    relay_socket: str
    mcp_socket: str
    tool: str = TOOL
    state_dir: str = STATE_DIR
    account: str = ACCOUNT
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def _env(self) -> dict[str, str]:
        return {**SYSTEM_ENV, "AGENT_PLATFORM_HOME": self.state_dir, "AGENT_PLATFORM_RELAY": self.relay_socket}

    def run(self, args: list[str], stdin: Mapping[str, Any]) -> dict[str, Any]:
        env = self._env()
        argv = [
            "runuser",
            "-u",
            self.account,
            "--",
            "env",
            "-i",
            *(f"{k}={v}" for k, v in env.items()),
            self.tool,
            *args,
        ]
        try:
            done = self.run_command(
                argv,
                input=json.dumps(dict(stdin)),
                capture_output=True,
                text=True,
                check=False,
                timeout=TOOL_TIMEOUT_SECONDS,
                env=SYSTEM_ENV,
            )
        except (OSError, subprocess.SubprocessError):
            raise ToolFailed("tool_failed") from None
        return tool_output(done.returncode, done.stdout)

    def serve(self) -> None:
        env = [f"--setenv={key}={value}" for key, value in self._env().items()]
        argv = [
            "systemd-run",
            "--quiet",
            "--collect",
            f"--unit={MCP_UNIT}",
            f"--uid={self.account}",
            f"--gid={self.account}",
            "--property=NoNewPrivileges=yes",
            "--property=UMask=0077",
            "--property=Restart=on-failure",
            *env,
            "--",
            self.tool,
            "mcp",
            "--socket",
            self.mcp_socket,
        ]
        self._call(["systemctl", "stop", f"{MCP_UNIT}.service"])
        if self._call(argv) != 0:
            raise ToolFailed("serve_failed")

    def stop(self) -> None:
        self._call(["systemctl", "stop", f"{MCP_UNIT}.service"])
        root = Path(self.state_dir)
        if root.is_dir():
            for entry in root.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)

    def _call(self, argv: list[str]) -> int:
        try:
            done = self.run_command(argv, capture_output=True, text=True, check=False, env=SYSTEM_ENV)
        except (OSError, subprocess.SubprocessError):
            return -1
        return done.returncode


def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="agent-guest-orchestrator", daemon=True).start()


def _digest(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(data)).encode("utf-8")).hexdigest()


@dataclass
class Orchestrator:
    """The orchestrator of one control process (module docstring)."""

    context: Any
    tool: Tool
    spawn: Callable[[Callable[[], None]], None] = _spawn
    reload: Callable[[str], bool] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    depth: Callable[[str], int | None] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _reloading: set[str] = field(default_factory=set, init=False)

    # -- record -----------------------------------------------------------------------------------------------

    def record(self) -> Record | None:
        with self.context.journal.transaction() as db:
            row = db.execute("SELECT value FROM guest_meta WHERE key = ?", (META_KEY,)).fetchone()
        return None if row is None else parse_record(row[0])

    def _save(self, record: Record | None) -> None:
        with self.context.journal.transaction() as db:
            if record is None:
                db.execute("DELETE FROM guest_meta WHERE key = ?", (META_KEY,))
            else:
                db.execute(
                    "INSERT OR REPLACE INTO guest_meta (key, value) VALUES (?, ?)",
                    (META_KEY, canonical(record.to_json())),
                )

    def _session_state(self, session_id: str) -> str | None:
        session = self.context.state.session(session_id)
        return None if session is None else str(session.state)

    def _main(self, session_id: str) -> bool:
        depth = self.depth(session_id) if self.depth is not None else _depth(self.context, session_id)
        return depth == 0

    # -- keys and handover (runner) -----------------------------------------------------------------------------

    def keys(self, session_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._session_state(session_id)
            if state is None or state in INACTIVE_STATES:
                raise OrchestratorError("not_found")
            if state not in RUNNING_STATES or not self._main(session_id):
                raise OrchestratorError("state_conflict")
            current = self.record()
            if current is not None and current.session_id != session_id:
                if self._session_state(current.session_id) in RUNNING_STATES:
                    raise OrchestratorError("state_conflict")
                self._end(current)
                current = None
            if current is not None and current.phase not in ("keys", "failed"):
                raise OrchestratorError("state_conflict")
            try:
                result = self.tool.run(["pair", "--session"], {"session_id": session_id, **data})
            except ToolFailed:
                raise OrchestratorError("unavailable") from None
            keys = {name: result.get(name) for name in ("pairing_id", "pub", "pop", "fp")}
            errors = self.context.schemas.errors("guest/orchestrator-keys.response.json", keys)
            if errors:
                raise OrchestratorError("unavailable")
            self._save(Record(session_id=session_id, phase="keys", keys=keys))
            return keys

    def handover(self, session_id: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            current = self.record()
            if current is None or current.session_id != session_id or current.pairing_id != data["pairing_id"]:
                raise OrchestratorError("state_conflict")
            digest = _digest(data)
            if current.handover_sha256 is not None:
                if current.handover_sha256 != digest:
                    raise OrchestratorError("state_conflict")
                return
            if current.phase != "keys" or self._session_state(session_id) not in RUNNING_STATES:
                raise OrchestratorError("state_conflict")
            claiming = Record(session_id, "handover", current.keys, digest)
            self._save(claiming)
        payload = dict(data)
        self.spawn(lambda: self._claim(claiming, payload))

    def _claim(self, record: Record, data: Mapping[str, Any]) -> None:
        claimed = Record(record.session_id, "claimed", record.keys, record.handover_sha256)
        try:
            self.tool.run(["pair", "--session", "--handover"], data)
            with self._lock:
                if self.record() != record:
                    return  # the session ended meanwhile
                self.tool.serve()
                self._save(claimed)
        except ToolFailed as failed:
            sys.stderr.write(f"agent-guest-control: orchestrator claim failed {failed.code}\n")
            with self._lock:
                if self.record() == record:
                    self.tool.stop()
                    self._save(Record(record.session_id, "failed", record.keys, record.handover_sha256))
            return
        self._reload_when_idle(claimed)

    def _reload_when_idle(self, record: Record) -> None:
        """Run the session's agent again with the second MCP server once it is ``idle`` (between turns)."""
        deadline = self.monotonic() + RELOAD_MAX_SECONDS
        while self.monotonic() < deadline:
            with self._lock:
                if self.record() != record:
                    return
                state = self._session_state(record.session_id)
                if state not in RUNNING_STATES:
                    return
                if state == "idle":
                    self._reloading.add(record.session_id)
            if state == "idle":
                try:
                    if self._reload(record.session_id):
                        return
                finally:
                    with self._lock:
                        self._reloading.discard(record.session_id)
            self.sleep(RELOAD_POLL_SECONDS)

    def _reload(self, session_id: str) -> bool:
        if self.reload is not None:
            return self.reload(session_id)
        from agent_guest import sessions  # noqa: PLC0415 - sessions imports the control routes, which import this

        return sessions.reload_session(self.context, session_id, (MCP_CONFIG,), SENTENCE)

    # -- relay and MCP (tool, API process) ------------------------------------------------------------------------

    def relay(self, args: Mapping[str, Any]) -> dict[str, Any]:
        record = self.record()
        if record is None or record.phase not in RELAY_PHASES:
            raise OrchestratorError("refused")
        session_id = record.session_id
        if self._session_state(session_id) in (None, *INACTIVE_STATES):
            raise OrchestratorError("refused")
        if set(args) != {"method", "target", "headers", "body"}:
            raise OrchestratorError("request_invalid")
        try:
            pending_id = self.context.pending.create(session_id, "relay", dict(args))
        except JournalError as failure:
            code = "unavailable" if failure.code == "unavailable" else "request_invalid"
            raise OrchestratorError(code) from None
        answer = self.context.pending.wait(pending_id, RELAY_WAIT_SECONDS, session_id)
        if answer is None:
            raise OrchestratorError("timeout")
        refused = answer.get("error")
        if refused is not None:
            raise OrchestratorError(refused if refused in RELAY_ERRORS else "unavailable")
        return dict(answer)

    def mcp_socket(self, session_id: str) -> str:
        record = self.record()
        if record is None or record.session_id != session_id or record.phase != "claimed":
            raise OrchestratorError("not_found")
        if self._session_state(session_id) not in RUNNING_STATES:
            raise OrchestratorError("not_found")
        return str(self.context.config.orch_mcp_socket)

    # -- end ------------------------------------------------------------------------------------------------------

    def ended(self, session_id: str) -> None:
        """The agent run of ``session_id`` ended: the orchestrator ends with it when the session is stopped or failed
        (module docstring), never for a suspend or its own reload."""
        with self._lock:
            if session_id in self._reloading or self._session_state(session_id) not in (None, *ENDING_STATES):
                return
            record = self.record()
            if record is not None and record.session_id == session_id:
                self._end(record)

    def end_pairing(self, session_id: str, pairing_id: str) -> None:
        """The runner ends the orchestrator of ``session_id`` when it is still ``pairing_id`` (else nothing)."""
        with self._lock:
            record = self.record()
            if record is not None and record.session_id == session_id and record.pairing_id == pairing_id:
                self._end(record)

    def check(self) -> None:
        """At the start of the control process: an orchestrator whose session no longer runs ends."""
        with self._lock:
            record = self.record()
            if record is not None and self._session_state(record.session_id) not in RUNNING_STATES:
                self._end(record)

    def _end(self, record: Record) -> None:
        self.tool.stop()
        self._save(None)


def _depth(context: Any, session_id: str) -> int | None:
    from agent_guest import sessions  # noqa: PLC0415 - sessions imports the control routes, which import this

    return sessions.manager(context).depth(session_id)


_ATTRIBUTE = "_orchestrator"
_create_lock = threading.Lock()


def install(context: Any, created: Orchestrator) -> Orchestrator:
    """Attach ``created`` to ``context`` (tests and ``orchestrator``)."""
    vars(context)[_ATTRIBUTE] = created
    return created


def orchestrator(context: Any) -> Orchestrator:
    """The orchestrator of this control process, created on first use with the golden image's tool."""
    with _create_lock:
        existing = vars(context).get(_ATTRIBUTE)
        if isinstance(existing, Orchestrator):
            return existing
        config = context.config
        return install(context, Orchestrator(context, LinuxTool(config.orch_socket, config.orch_mcp_socket)))
