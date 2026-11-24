"""Capture of transcript, terminal and workspace at the session lifecycle (05-runner.md sections 5.5, 6.4, 7; GST-05).

Attached through ``SESSION_EVENTS`` of GST-02 (``sessions.py``), no change to the session manager:

* ``sandbox_ready`` - the workspace scanner starts (index file or baseline, ``scanner.py``) with its thread: every
  ``SCAN_SECONDS`` it checks the file hints of PostToolUse (``control_routes/hooks.py:FILE_HINTS``) and scans
* ``agent_starting`` - the tmux session exists: terminal recorder (``recorder.py``) and transcript follower
  (``transcript.py``) of this run start; the transcript file names the Claude session (``transcript.tui_up``); the
  move to ``prompt_delivering`` is the delivery watcher's alone (``delivery.py``: TUI up and platform tools listed,
  HOT-09)
* ``agent_ended`` - the pane command ended (exit, stop, suspend, failure): recorder and follower read and write what
  is left, the scanner makes a last pass; everything of the session stops

api.sock op ``capture.file_written {session_id, path, op}`` (the PostToolUse addon ``hook_addons/file_changes.py``)
reports one written file at once: ``path`` absolute or relative to the workspace, lexically inside it, ``op``
``write`` or ``edit`` -> ``{"reported": bool}``. Unknown or inactive sessions answer ``not_found``, malformed
arguments ``request_invalid``.

``import_guard(session)`` is the ``guard`` of ``files.import_tar`` (``files/import``: ``github.checkout`` and every
other platform import): the imported tree becomes the baseline of the session's scanner (``scanner.py``
``materializing``) - the running one, or for a session without capture (suspended, stopping) one that loads and
writes the index file under the capture lock, so a later start does not report the import either.

A restart of ``agent-guest-control`` loses the threads; the capture of a session that is still running is attached
again at once by the start-up hook ``STARTUP`` of the restarted process (KOR-02), and by every later event or op
(``Capture.attach_running``): the recorder continues its cast file with a marker, the
follower reads the transcript again and skips what the journal already holds, the scanner reports what changed
since its index was written.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from agent_guest import files, sessions, tmux
from agent_guest.control import ControlContext
from agent_guest.control_routes.hooks import FILE_HINTS
from agent_guest.policy import JS_WHITESPACE, lexical
from agent_guest.recorder import FIFO_DIR, TerminalRecorder
from agent_guest.scanner import SCAN_SECONDS, Survey, WorkspaceScanner
from agent_guest.state import Session, StateError
from agent_guest.transcript import POLL_SECONDS, TranscriptFollower

RUNNING_STATES = frozenset({"agent_starting", "prompt_delivering", "working", "idle"})
# States in which a session's workspace may change; any other state ends its capture.
CAPTURE_STATES = RUNNING_STATES | {"sandbox_ready", "resuming"}
MAX_PATH = 4096


@dataclass
class SessionCapture:
    session: Session
    scanner: WorkspaceScanner
    stop: threading.Event
    thread: threading.Thread
    follower: TranscriptFollower | None = None
    recorder: TerminalRecorder | None = None


def _logged(what: str, action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:
        sys.stderr.write(f"agent-guest-control: {what} failed\n")
        traceback.print_exc(file=sys.stderr)


class Capture:
    """The captures of one control process."""

    def __init__(
        self,
        context: ControlContext,
        terminal: tmux.Tmux | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        *,
        fifo_dir: str = FIFO_DIR,
        monotonic: Callable[[], float] = time.monotonic,
        scan_seconds: float = SCAN_SECONDS,
        poll: float = POLL_SECONDS,
    ) -> None:
        self.context = context
        self.terminal = terminal or tmux.Tmux(run=run)
        self.run = run
        self.fifo_dir = fifo_dir
        self.monotonic = monotonic
        self.scan_seconds = scan_seconds
        self.poll = poll
        self.sessions: dict[str, SessionCapture] = {}
        self._lock = threading.RLock()

    def _session(self, session_id: str) -> Session:
        session = self.context.state.session(session_id)
        if session is None or session.state not in CAPTURE_STATES or session.uid < 0:
            raise StateError("not_found")
        return session

    @staticmethod
    def base(session: Session) -> str:
        return session.workspace.rpartition("/")[0]

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def ensure_scanner(self, session_id: str) -> SessionCapture:
        with self._lock:
            existing = self.sessions.get(session_id)
            if existing is not None:
                return existing
            session = self._session(session_id)
            scanner = WorkspaceScanner(self.context.journal, session_id, self.base(session), session.uid)
            scanner.start()
            stop = threading.Event()
            thread = threading.Thread(
                target=self._scan_loop, args=(session, scanner, stop), name="agent-guest-scanner", daemon=True
            )
            capture = SessionCapture(session, scanner, stop, thread)
            self.sessions[session_id] = capture
            thread.start()
            return capture

    def import_guard(self, session: Session) -> files.Guard:
        """The ``guard`` of ``files.import_tar`` for ``session`` (module docstring); it surveys the staged tree at
        once, before the tree is moved."""

        def guard(directory: str, tree: int) -> AbstractContextManager[object]:
            try:
                running = self.ensure_scanner(session.id).scanner
            except StateError:
                transient = WorkspaceScanner(self.context.journal, session.id, self.base(session), session.uid)
                return self._transient(transient, directory, transient.survey(tree, directory))
            return running.materializing(directory, running.survey(tree, directory))

        return guard

    @contextmanager
    def _transient(self, scanner: WorkspaceScanner, directory: str, survey: Survey) -> Iterator[None]:
        with self._lock:
            # A capture attached meanwhile (the session started) owns the index: its scanner adopts the import.
            running = self.sessions.get(scanner.session_id)
            if running is None:
                scanner.start()
            else:
                scanner = running.scanner
            with scanner.materializing(directory, survey):
                yield

    def sandbox_ready(self, session_id: str) -> None:
        self.ensure_scanner(session_id)

    def _scan_loop(self, session: Session, scanner: WorkspaceScanner, stop: threading.Event) -> None:
        while not stop.wait(self.scan_seconds):
            current = self.context.state.session(session.id)
            if current is None or current.state not in CAPTURE_STATES:
                # Ended without agent_ended (stopped before the start, or attached again after a restart).
                self.agent_ended(session.id)
                return
            _logged("workspace scan", lambda: self.scan(session, scanner))

    def scan(self, session: Session, scanner: WorkspaceScanner) -> None:
        prefix = session.workspace.rstrip("/") + "/"
        for path in FILE_HINTS.take(session.id):
            scanner.check(path.removeprefix(prefix))
        scanner.scan()

    def agent_starting(self, session_id: str) -> None:
        with self._lock:
            capture = self.ensure_scanner(session_id)
            self._stop_run(capture)
            session = self._session(session_id)
            journal, state = self.context.journal, self.context.state
            capture.follower = TranscriptFollower(
                journal, state, session_id, self.base(session), session.uid, monotonic=self.monotonic, poll=self.poll
            )
            capture.follower.start()
            recorder = TerminalRecorder(
                journal,
                session_id,
                self.base(session),
                self.terminal,
                self.run,
                fifo_dir=self.fifo_dir,
                monotonic=self.monotonic,
            )
            try:
                recorder.start()
            except (OSError, tmux.TmuxError):
                sys.stderr.write("agent-guest-control: terminal capture could not start\n")
                return
            capture.recorder = recorder

    def _stop_run(self, capture: SessionCapture) -> None:
        recorder, follower = capture.recorder, capture.follower
        capture.recorder = capture.follower = None
        if recorder is not None:
            _logged("terminal capture", recorder.stop)
        if follower is not None:
            _logged("transcript capture", follower.stop)

    def agent_ended(self, session_id: str) -> None:
        with self._lock:
            capture = self.sessions.pop(session_id, None)
        if capture is None:
            return
        capture.stop.set()
        if capture.thread is not threading.current_thread():
            capture.thread.join(5)
        self._stop_run(capture)
        _logged("workspace scan", lambda: self.scan(capture.session, capture.scanner))

    def close(self) -> None:
        """Stop every capture of this process and wait for its threads (the control process stops); recorder and
        follower write what they read. A restarted process attaches again (``attach_running``)."""
        with self._lock:
            captures = list(self.sessions.values())
            self.sessions.clear()
        for capture in captures:
            capture.stop.set()
            capture.thread.join(5)
            self._stop_run(capture)

    def attach_running(self, exclude: str | None = None) -> None:
        """Attach the capture of running sessions that have none (after a restart of the control process)."""
        for session in self.context.state.sessions():
            with self._lock:
                attached = session.id in self.sessions
            if attached or session.id == exclude or session.state not in RUNNING_STATES:
                continue
            if self.terminal.has_session(tmux.session_name(session.id)):
                _logged("capture attach", partial(self.agent_starting, session.id))

    # -- op -------------------------------------------------------------------------------------------------------

    def file_written(self, session_id: str, path: str, op: str) -> bool:
        if self.context.state.session(session_id) is None:
            raise StateError("not_found")
        try:
            session = self._session(session_id)
        except StateError:
            return False  # the session is ending: its last scan reports the file
        given = path.strip(JS_WHITESPACE)
        absolute = lexical(given if given.startswith("/") else f"{session.workspace}/{given}")
        prefix = session.workspace.rstrip("/") + "/"
        if not absolute.startswith(prefix):
            return False
        capture = self.ensure_scanner(session_id)
        return capture.scanner.check(absolute.removeprefix(prefix), op)


_CAPTURE = "_capture"
_capture_lock = threading.Lock()


def install(context: ControlContext, created: Capture) -> Capture:
    vars(context)[_CAPTURE] = created
    context.on_close(created.close)
    return created


def capture(context: ControlContext) -> Capture:
    """The capture of this control process; the tmux client of the session manager is shared."""
    with _capture_lock:
        existing = vars(context).get(_CAPTURE)
        if isinstance(existing, Capture):
            return existing
        terminal = sessions.manager(context).tmux
        # pipe-pane is not part of the tmux client of GST-02: the same runner (and socket) is used for it.
        return install(context, Capture(context, terminal, terminal._run))


def _event(handler: Callable[[Capture, str], None]) -> Callable[[ControlContext, str], None]:
    def callback(context: ControlContext, session_id: str) -> None:
        current = capture(context)
        handler(current, session_id)
        current.attach_running(exclude=session_id)

    return callback


def file_written(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    path, op = args.get("path"), args.get("op")
    if (
        set(args) != {"session_id", "path", "op"}
        or not isinstance(args["session_id"], str)
        or not isinstance(path, str)
        or not path
        or "\x00" in path
        or len(path) > MAX_PATH
        or op not in ("write", "edit")
    ):
        raise StateError("request_invalid")
    current = capture(context)
    current.attach_running()
    return {"reported": current.file_written(args["session_id"], path, op)}


def startup(context: ControlContext) -> None:
    if any(session.state in RUNNING_STATES for session in context.state.sessions()):
        capture(context).attach_running()


STARTUP = startup
SESSION_EVENTS = {
    "sandbox_ready": _event(Capture.sandbox_ready),
    "agent_starting": _event(Capture.agent_starting),
    "agent_ended": _event(Capture.agent_ended),
}
API_OPS = {"capture.file_written": file_written}
