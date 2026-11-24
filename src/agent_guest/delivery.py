"""Prompt delivery into the Claude Code TUI and the TUI-ready signal (05-runner.md §6.1 steps 5-6, §6.2, §6.3, §7).

The only writers to a session pane are this module's ``Delivery.deliver`` (registered prompts) and
``Delivery.interrupt`` (``Escape``); both go through the root tmux server (``/run/agent-guest/tmux.sock``) and hold
the session's pane lock, so keystrokes of two writers never interleave. There is no other input path (ADR-016).

Delivery of one registered prompt (``POST /v1/sessions/{id}/prompts`` after ``PromptRegistry.register``):

1. Wait until the session can take input: state ``prompt_delivering`` or ``idle`` and the input box visible in the
   pane. A ``working`` session is waited for without limit (the turn ends with the Stop hook); every other live
   state (``sandbox_ready``, ``agent_starting``, ``suspended``: a start or resume in progress) at most
   ``WAIT_SECONDS``. A session that ends (``stopping``, ``stopped``, ``failed``) refuses the prompt.
2. Empty the input: capture the pane and send ``C-e C-u`` (three times per round; each removes one visual line of the
   input) until the input box reads empty, at most ``CLEAR_ROUNDS`` rounds (text that two rounds do not change is no
   input, for example a hint, and is left to the check of step 3). Claude Code puts an interrupted prompt and
   a prompt the UserPromptSubmit hook blocked back into its input box, possibly after the session is ``idle`` again
   (the interrupt marks it ``idle`` when it sent ``Escape``); such text must never be submitted together with the
   next prompt. An input that cannot be emptied (or no input box) refuses the prompt before anything is written.
   Text of at most 200 characters on one line is typed with
   ``send-keys -l --`` in 40-character chunks 40 ms apart; everything else is pasted as bracketed paste
   (``load-buffer`` from stdin into a buffer of its own, ``paste-buffer -p -d``; the buffer is deleted if the paste
   fails). Typing would let a leading ``!``, ``#``, ``/``, ``?`` or ``&`` switch the TUI into another input mode and a
   tab or ``@`` open a completion: such texts are pasted as well. Text that is never written (``deliverable``) is
   refused at once: control characters other than newline and tab (``ESC`` could end a bracketed paste early and
   inject keys), text that is no valid UTF-8 (a lone surrogate reaches tmux as a raw byte), and text Claude Code would
   run as a bash or slash command (``!…``, ``/name …``), which might never reach the UserPromptSubmit hook.
3. Wait 300 ms, then check (polling for one second) that the input box shows the text and nothing else (``shows``);
   otherwise (text put back by the TUI in the meantime) start again at step 1. ``Enter``. The delivery is verified
   when the hook accepted the prompt (its registry row is used) or the input box no longer shows the first 30
   characters of the text (white space ignored: the TUI wraps long lines, also inside words) nor the paste placeholder
   (``[Pasted text``); each check polls for one second, ``Enter`` is retried up to three times. A prompt that is not
   delivered in the end is emptied from the input and refused.
4. A prompt verified by the pane must still be accepted by the hook within ``ACCEPT_SECONDS``; otherwise it is
   refused (the hook is authoritative). Only then the next prompt of the session is taken, so it sees the session
   ``working`` and waits for ``idle``.

A hook refusal (``prompt_refused unregistered`` of the session journaled after the ``Enter``) while the prompt's own
registration is unused is not final: the submitted text was not the registered one (text the TUI put back was mixed
in), so the delivery starts again at step 1, at most ``RETRIES`` times; then the prompt is refused.

Refusal: journal ``session.state prompt_refused {prompt_id, reason: "delivery_failed"}`` and withdraw the unused
registry row in the same transaction, so text left in the TUI can never be accepted later; if the hook accepted the
prompt in the meantime nothing is refused. Prompts of a session are delivered one at a time in arrival order by one
worker thread; the same ``prompt_id`` is queued only once.

Ready for a prompt (05 §6.1 step 6; Claude Code 2.1.330 sends no ``SessionStart`` hook, ADR-029): when a session
reaches ``agent_starting`` (``SESSION_EVENTS`` of ``control_routes/prompts.py``) a watcher polls until the input box
appears in the pane **and** the agent listed the platform tools (``toolsready.ready``, HOT-09: Claude Code connects its
MCP servers in the background and would run a turn without them), then moves ``agent_starting`` ->
``prompt_delivering`` in one transaction with the state check. It polls every 50 ms for ``READY_SECONDS``, then once
a second, and ends when the state changes. Then the session's initial prompt (``sessions.deferred_prompt``: it never
goes on the command line) is queued; the runner sends no follow-up before the first prompt is accepted. The same holds
for every start of the agent: a new session, a resume, a restart.

Tools missing: when the agent has not listed the tools ``tools_seconds`` (``AGENT_GUEST_TOOLS_SECONDS``, default 25)
after the watcher began, the watcher journals ``guest.error tools_unavailable`` for the session (the runner shows it in
the task as ``session.error``) and starts the agent again (``sessions.restart_session``: new run, new token, the
prompt keeps waiting), at most ``TOOLS_RESTARTS`` times per session and control process; then - or when the run
cannot be restarted - the session ends ``failed`` with ``agent_start_failed`` and its waiting prompt is refused. A
prompt is never delivered to an agent without the tools.

Input box: the last line of the pane that starts with the prompt marker (U+276F) below a horizontal rule (``─``); it
reaches to the next rule. ``input_box(screen)`` returns its text (marker removed) or ``None``.

Interface: ``Pane`` (protocol), ``TmuxPane``, ``Delivery``, ``manager(context)``, ``install(context, delivery)``,
``input_box``, ``shows``, ``typed``, ``deliverable``, ``literal_chunks``.
"""

from __future__ import annotations

import collections
import contextlib
import re
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from agent_guest import sessions, tmux, toolsready
from agent_guest.clock import iso_ms
from agent_guest.control import ControlContext
from agent_guest.journal import JournalError
from agent_guest.state import Session

TYPED_MAX = 200
CHUNK = 40
CHUNK_PAUSE = 0.04
CLEARS = 3
CLEAR_ROUNDS = 20
CLEAR_PAUSE = 0.1
ENTER_PAUSE = 0.3
RETRIES = 3
CHECK_SECONDS = 1.0
POLL_SECONDS = 0.05
WAIT_SECONDS = 120.0
ACCEPT_SECONDS = 10.0
READY_SECONDS = 30.0
READY_SLOW_POLL = 1.0
TOOLS_SECONDS = 25.0
TOOLS_RESTARTS = 2
TOOLS_UNAVAILABLE = "tools_unavailable"
READY, ENDED, RESTARTED = "ready", "ended", "restarted"
MARKER_LENGTH = 30
PASTE_PLACEHOLDER = "[Pasted text"
# One collapsed paste and nothing else (Claude Code 2.1.330: ``[Pasted text #<n>]``, ``[Pasted text #<n> +<k> lines]``).
_PLACEHOLDER_RE = re.compile(r"\[Pasted text[^\[\]]*\]")
PROMPT_MARK = "\u276f"
RULE = "─" * 8
# Characters that switch Claude Code's input mode when typed into an empty input (bash, memory, slash command,
# shortcuts, background) or open a completion (``@`` files, tab): such prompts are pasted, never typed.
MODE_CHARS = frozenset("!#/?&")
COMPLETION_CHARS = frozenset("@\t")
# Claude Code decides bash mode (``!``) and slash commands from the submitted text, and such input may never reach
# the UserPromptSubmit hook: a prompt that looks like one is never written to the pane.
_COMMAND_RE = re.compile(r"(?:!|/[A-Za-z0-9_:-]+(?:\s|$))")
READY_STATES = frozenset({"prompt_delivering", "idle"})
STARTING_STATES = frozenset({"sandbox_ready", "agent_starting", "suspended"})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACE_RE = re.compile(r"\s+")


class Pane(Protocol):
    """Writes to and reads from the pane of the tmux session ``name`` (raises on failure)."""

    def send_keys(self, name: str, *keys: str, literal: bool = False) -> None: ...

    def capture(self, name: str) -> str: ...

    def paste(self, name: str, text: str) -> None: ...


class TmuxPane:
    """``Pane`` on the root tmux server of ``tmux.Tmux``; buffers are loaded from stdin (never a command line)."""

    def __init__(self, server: tmux.Tmux, run: tmux.Runner = subprocess.run) -> None:
        self.server = server
        self._run = run

    def send_keys(self, name: str, *keys: str, literal: bool = False) -> None:
        self.server.send_keys(name, *keys, literal=literal)

    def capture(self, name: str) -> str:
        return self.server.capture(name)

    def paste(self, name: str, text: str) -> None:
        buffer = f"p-{name[2:]}"
        loaded = self._run(
            self.server.argv("load-buffer", "-b", buffer, "-"),
            input=text,
            capture_output=True,
            text=True,
            timeout=tmux.TIMEOUT,
            check=False,
            env=tmux.CLIENT_ENV,
        )
        if loaded.returncode != 0:
            raise tmux.TmuxError(f"tmux load-buffer failed with status {loaded.returncode}")
        pasted = self._run(
            self.server.argv("paste-buffer", "-p", "-d", "-b", buffer, "-t", f"={name}:"),
            capture_output=True,
            text=True,
            timeout=tmux.TIMEOUT,
            check=False,
            env=tmux.CLIENT_ENV,
            stdin=subprocess.DEVNULL,
        )
        if pasted.returncode != 0:
            # The text must not stay in the root server's memory.
            self._run(
                self.server.argv("delete-buffer", "-b", buffer),
                capture_output=True,
                text=True,
                timeout=tmux.TIMEOUT,
                check=False,
                env=tmux.CLIENT_ENV,
                stdin=subprocess.DEVNULL,
            )
            raise tmux.TmuxError(f"tmux paste-buffer failed with status {pasted.returncode}")


def input_box(screen: str) -> str | None:
    """Text of the TUI's input box (without the prompt marker), ``None`` when no input box is visible."""
    lines = screen.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].lstrip()
        if not stripped.startswith(PROMPT_MARK):
            continue
        if not any(RULE in line for line in lines[:index]):
            return None
        box = [stripped[len(PROMPT_MARK) :]]
        for line in lines[index + 1 :]:
            if RULE in line:
                break
            box.append(line)
        return " ".join(part.strip() for part in box).strip()
    return None


def _squeezed(text: str) -> str:
    return _SPACE_RE.sub("", text)


def shows(box: str, text: str, pasted: bool) -> bool:
    """``True`` when the input box holds the inserted ``text`` and nothing else: one paste placeholder alone (only for a
    pasted text), or the text itself compared without white space (the TUI wraps long lines, also inside words).
    Nothing may stand before the text and the box may not hold more characters than the text (the pane may render
    fewer, never more)."""
    if pasted and _PLACEHOLDER_RE.fullmatch(box):
        return True
    shown = _squeezed(box)
    return shown.startswith(_squeezed(marker(text))) and len(shown) <= len(_squeezed(text))


def deliverable(text: str) -> bool:
    """``False`` for text that is never written to a pane: control characters other than newline and tab, text that
    is no valid UTF-8 (a lone surrogate would reach tmux as a raw byte, for example a C1 control), and text that
    Claude Code would take as a bash or slash command (``!…``, ``/name …``, judged on the stripped text)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return _CONTROL_RE.search(text) is None and _COMMAND_RE.match(text.strip()) is None


def typed(text: str) -> bool:
    """``True`` when the text is typed (short, one line, no tab or ``@``, no mode character first), else pasted."""
    return (
        len(text) <= TYPED_MAX
        and "\n" not in text
        and not COMPLETION_CHARS & set(text)
        and text.lstrip()[:1] not in MODE_CHARS
    )


def literal_chunks(text: str, size: int = CHUNK) -> list[str]:
    """``text`` in ``size``-character chunks as ``send-keys -l`` arguments: tmux ends a command at an argument that
    ends with ``;`` unless the semicolon is escaped, so a trailing ``;`` becomes ``\\;``."""
    chunks = []
    for start in range(0, len(text), size):
        chunk = text[start : start + size]
        if chunk.endswith(";"):
            chunk = chunk[:-1] + "\\;"
        chunks.append(chunk)
    return chunks


def marker(text: str) -> str:
    """The first 30 characters of the prompt's first non-blank line, whitespace collapsed (the verification probe)."""
    first = text.strip().split("\n", 1)[0]
    return _SPACE_RE.sub(" ", first[:MARKER_LENGTH]).strip()


@dataclass
class Delivery:
    """Prompt delivery, interrupt and TUI-ready watcher of one control process (thread-safe)."""

    context: ControlContext
    pane: Pane
    sleep: Callable[[float], None] | None = None
    spawn: Callable[[Callable[[], object]], None] | None = None
    poll: float = POLL_SECONDS
    wait_seconds: float = WAIT_SECONDS
    accept_seconds: float = ACCEPT_SECONDS
    ready_seconds: float = READY_SECONDS
    tools_seconds: float = TOOLS_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _closed: threading.Event = field(default_factory=threading.Event, init=False)
    _queues: dict[str, collections.deque[tuple[str, str]]] = field(default_factory=dict, init=False)
    _queued: set[tuple[str, str]] = field(default_factory=set, init=False)
    _pane_locks: dict[str, threading.Lock] = field(default_factory=dict, init=False)
    _restarts: dict[str, int] = field(default_factory=dict, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)

    # -- plumbing -----------------------------------------------------------------------------------------------

    def _pause(self, seconds: float) -> bool:
        """Sleep ``seconds``; ``False`` once the delivery is closed (control process shutting down)."""
        if self.sleep is not None:
            self.sleep(seconds)
        else:
            self._closed.wait(seconds)
        return not self._closed.is_set()

    def _start(self, work: Callable[[], object]) -> None:
        if self.spawn is not None:
            self.spawn(work)
            return
        with self._lock:
            if self._closed.is_set():
                return  # the control process stops: the prompt stays registered for a restarted process
            thread = threading.Thread(target=work, name="agent-guest-delivery", daemon=True)
            self._threads = [running for running in self._threads if running.is_alive()]
            self._threads.append(thread)
            thread.start()

    def pane_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            return self._pane_locks.setdefault(session_id, threading.Lock())

    def close(self) -> None:
        """End the workers (their next pause returns ``False``) and wait for them; the control process stops."""
        with self._lock:
            self._closed.set()
            threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(5)

    def _session(self, session_id: str) -> Session | None:
        return self.context.state.session(session_id)

    def _box(self, session_id: str) -> str | None:
        try:
            return input_box(self.pane.capture(tmux.session_name(session_id)))
        except (tmux.TmuxError, OSError, subprocess.SubprocessError):
            return None

    # -- queue ----------------------------------------------------------------------------------------------------

    def submit(self, session_id: str, prompt_id: str, text: str) -> None:
        """Queue a registered prompt; one worker per session delivers in arrival order."""
        with self._lock:
            if (session_id, prompt_id) in self._queued:
                return
            self._queued.add((session_id, prompt_id))
            queue = self._queues.get(session_id)
            if queue is not None:
                queue.append((prompt_id, text))
                return
            self._queues[session_id] = collections.deque([(prompt_id, text)])
        self._start(lambda: self._work(session_id))

    def _work(self, session_id: str) -> None:
        while True:
            with self._lock:
                queue = self._queues[session_id]
                if not queue or self._closed.is_set():
                    del self._queues[session_id]
                    return
                prompt_id, text = queue[0]
            try:
                self.deliver(session_id, prompt_id, text)
            except JournalError:
                pass  # the control process is shutting down: nothing can be journaled any more
            except Exception:
                sys.stderr.write("agent-guest-control: prompt delivery failed\n")
                traceback.print_exc(file=sys.stderr)
                try:
                    self._refuse(session_id, prompt_id)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
            with self._lock:
                queue.popleft()
                self._queued.discard((session_id, prompt_id))

    # -- delivery ---------------------------------------------------------------------------------------------

    def deliver(self, session_id: str, prompt_id: str, text: str) -> bool:
        """Deliver one registered prompt (steps 1-4 of the module text); ``True`` when accepted."""
        if self._used(session_id, prompt_id) is not False:
            return True  # accepted before (the initial prompt, a repeated request) or withdrawn
        if not deliverable(text):
            self._refuse(session_id, prompt_id)
            return False
        name = tmux.session_name(session_id)
        attempts = 0
        while True:
            if not self._wait_ready(session_id):
                if not self._closed.is_set():
                    self._refuse(session_id, prompt_id)
                return False
            with self.pane_lock(session_id):
                session = self._session(session_id)
                if session is None or session.state not in READY_STATES:
                    continue  # suspended or stopped between the check and the lock: wait again
                try:
                    outcome, mark = self._type(name, session_id, prompt_id, text)
                except (tmux.TmuxError, OSError, subprocess.SubprocessError):
                    outcome, mark = False, 0
            if outcome:
                outcome = self._await_acceptance(session_id, prompt_id, mark)
                if outcome:
                    return True
            if outcome is None and attempts < RETRIES:
                if self._used(session_id, prompt_id) is not False:
                    return True  # accepted in the meantime (a late answer of the hook)
                attempts += 1  # the TUI showed or submitted other text than the prompt: empty it, start again
                continue
            break
        # Not verified, or the text left the input but the hook never took it (changed by a completion, blocked).
        if not self._closed.is_set():
            self._give_up(name, session_id, prompt_id)
        return False

    def _give_up(self, name: str, session_id: str, prompt_id: str) -> None:
        """Nothing of the prompt may stay in the input, and it is refused, so the runner learns the outcome and the row
        can never be matched by a later prompt (unless the hook accepted it meanwhile)."""
        if self._used(session_id, prompt_id) is False:
            with self.pane_lock(session_id), contextlib.suppress(tmux.TmuxError, OSError, subprocess.SubprocessError):
                self._empty(name, session_id)  # a pane that is gone keeps nothing
        self._refuse(session_id, prompt_id)

    def _wait_ready(self, session_id: str) -> bool:
        waited = 0.0
        while True:
            session = self._session(session_id)
            if session is None or session.state not in READY_STATES | STARTING_STATES | {"working"}:
                return False
            if session.state in READY_STATES and self._box(session_id) is not None:
                return True
            if session.state != "working":
                if waited >= self.wait_seconds:
                    return False
                waited += self.poll
            if not self._pause(self.poll):
                return False

    def _clear(self, name: str) -> None:
        for _ in range(CLEARS):
            self.pane.send_keys(name, "C-e", "C-u")

    def _empty(self, name: str, session_id: str) -> bool:
        """Clear the input until the input box reads empty, at most ``CLEAR_ROUNDS`` rounds ``CLEAR_PAUSE`` apart.
        ``True`` when it reads empty, or when its text stayed the same over two rounds of clearing: that is no input
        (a hint the TUI may show in an empty box), and the check before ``Enter`` still refuses anything the box shows
        besides the prompt. ``False`` when no input box is visible or the text keeps changing without becoming empty."""
        previous: list[str | None] = []
        for _ in range(CLEAR_ROUNDS):
            box = self._box(session_id)
            if box == "" or (box is not None and previous[-2:] == [box, box]):
                return True
            previous.append(box)
            if box is not None:
                self._clear(name)
            if not self._pause(CLEAR_PAUSE):
                return False
        return self._box(session_id) == ""

    def _type(self, name: str, session_id: str, prompt_id: str, text: str) -> tuple[bool | None, int]:
        """One attempt: ``(True, mark)`` verified, ``(False, mark)`` failed, ``(None, mark)`` start again (the input
        showed other text, or the hook refused what was submitted); ``mark`` is the journal head before ``Enter``."""
        if not self._empty(name, session_id):
            return False, 0
        if typed(text):
            for chunk in literal_chunks(text):
                self.pane.send_keys(name, chunk, literal=True)
                self._pause(CHUNK_PAUSE)
        else:
            self.pane.paste(name, text)
        self._pause(ENTER_PAUSE)
        if not self._shows(session_id, text, not typed(text)):
            return (False if self._closed.is_set() else None), 0
        mark = self._head()
        probe = _squeezed(marker(text))
        for _ in range(RETRIES + 1):
            if self._closed.is_set():
                return False, mark
            self.pane.send_keys(name, "Enter")
            outcome = self._check(session_id, prompt_id, probe, mark)
            if outcome is not False:
                return outcome, mark
        return False, mark

    def _shows(self, session_id: str, text: str, pasted: bool) -> bool:
        """Poll for ``CHECK_SECONDS``: the input box shows the text and nothing else."""
        polls = max(1, round(CHECK_SECONDS / self.poll))
        for _ in range(polls):
            box = self._box(session_id)
            if box is not None and shows(box, text, pasted):
                return True
            if not self._pause(self.poll):
                return False
        return False

    def _check(self, session_id: str, prompt_id: str, probe: str, mark: int) -> bool | None:
        """Poll for ``CHECK_SECONDS``: accepted by the hook or the text left the input box (``True``), the hook
        refused a submission (``None``)."""
        polls = max(1, round(CHECK_SECONDS / self.poll))
        for _ in range(polls):
            if self._used(session_id, prompt_id) is not False:
                return True
            if self._refused_since(session_id, mark):
                return None
            box = self._box(session_id)
            if box is not None and PASTE_PLACEHOLDER not in box and probe not in _squeezed(box):
                return True
            if not self._pause(self.poll):
                return False
        return False

    def _await_acceptance(self, session_id: str, prompt_id: str, mark: int) -> bool | None:
        """``True`` accepted, ``None`` the hook refused a submission since ``mark``, ``False`` neither in time."""
        waited = 0.0
        while self._used(session_id, prompt_id) is False:
            if self._refused_since(session_id, mark):
                return None
            if waited >= self.accept_seconds or not self._pause(self.poll):
                return False
            waited += self.poll
        return True

    def _head(self) -> int:
        with self.context.journal.transaction() as db:
            row = db.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()
        return int(row[0])

    def _refused_since(self, session_id: str, mark: int) -> bool:
        """The hook refused a submission of the session (``prompt_refused unregistered``) after journal seq ``mark``."""
        with self.context.journal.transaction() as db:
            row = db.execute(
                "SELECT 1 FROM journal WHERE seq > ? AND session_id = ? AND kind = 'session.state'"
                " AND json_extract(body, '$.state') = 'prompt_refused'"
                " AND json_extract(body, '$.reason') = 'unregistered' LIMIT 1",
                (mark, session_id),
            ).fetchone()
        return row is not None

    def _used(self, session_id: str, prompt_id: str) -> bool | None:
        """``True`` accepted, ``False`` registered and unused, ``None`` no such registration."""
        with self.context.journal.transaction() as db:
            row = db.execute(
                "SELECT used_at FROM prompt_registry WHERE session_id = ? AND prompt_id = ?", (session_id, prompt_id)
            ).fetchone()
        return None if row is None else row[0] is not None

    def _refuse(self, session_id: str, prompt_id: str) -> None:
        """``prompt_refused delivery_failed`` and the registration withdrawn, unless the hook accepted it already."""
        at = iso_ms(self.context.journal.clock.now())
        with self._lock:
            # A runner retry after this refusal registers the prompt again and must be queued again.
            self._queued.discard((session_id, prompt_id))
        with self.context.journal.transaction() as db:
            row = db.execute(
                "SELECT used_at FROM prompt_registry WHERE session_id = ? AND prompt_id = ?", (session_id, prompt_id)
            ).fetchone()
            if row is not None and row[0] is not None:
                return
            db.execute(
                "DELETE FROM prompt_registry WHERE session_id = ? AND prompt_id = ? AND used_at IS NULL",
                (session_id, prompt_id),
            )
            body = {"state": "prompt_refused", "prompt_id": prompt_id, "reason": "delivery_failed", "at": at}
            self.context.journal.insert(db, "session.state", body, session_id)

    # -- interrupt ----------------------------------------------------------------------------------------------

    def interrupt(self, session_id: str) -> None:
        """``Escape`` into the pane (raises ``tmux.TmuxError`` when the pane is gone)."""
        with self.pane_lock(session_id):
            self.pane.send_keys(tmux.session_name(session_id), "Escape")

    # -- TUI ready ----------------------------------------------------------------------------------------------

    def agent_starting(self, session_id: str) -> None:
        """``SESSION_EVENTS["agent_starting"]``: watch for the TUI in the background."""
        self._start(lambda: self.watch_ready(session_id))

    def watch_ready(self, session_id: str) -> bool:
        """Poll while the session is ``agent_starting`` until the input box appears and the agent listed the platform
        tools, then move it to ``prompt_delivering``; ``True`` when that happened. Polls every ``poll`` seconds for
        ``ready_seconds``, then every ``READY_SLOW_POLL`` seconds; it ends when the state changes (failed, stopped) or
        the delivery closes. Then the initial prompt is queued - unless the agent was started again (its run's watcher
        takes over), see the module docstring for missing tools."""
        outcome = self._watch(session_id)
        if outcome == RESTARTED:
            return False
        with self._lock:
            self._restarts.pop(session_id, None)
        deferred = sessions.deferred_prompt(self.context, session_id)
        if deferred is not None:
            self.submit(session_id, *deferred)
        return outcome == READY

    def _watch(self, session_id: str) -> str:
        waited = 0.0
        while True:
            session = self._session(session_id)
            if session is None or session.state != "agent_starting":
                return ENDED
            tools = toolsready.ready(self.context, session_id)
            if tools and self._box(session_id) is not None:
                return READY if self._delivering(session_id) else ENDED
            if not tools and waited >= self.tools_seconds:
                return self._tools_missing(session_id)
            interval = self.poll if waited < self.ready_seconds else READY_SLOW_POLL
            if not self._pause(interval):
                return ENDED
            waited += interval

    def _tools_missing(self, session_id: str) -> str:
        """The agent did not list the tools in time: ``guest.error tools_unavailable``, then a restart of the agent or,
        after ``TOOLS_RESTARTS`` of them (or when it cannot be restarted), the session ends ``agent_start_failed``."""
        with contextlib.suppress(JournalError):
            self.context.journal.append("guest.error", {"code": TOOLS_UNAVAILABLE}, session_id)
        with self._lock:
            attempt = self._restarts.get(session_id, 0)
            self._restarts[session_id] = attempt + 1
        try:
            if attempt < TOOLS_RESTARTS and sessions.restart_session(self.context, session_id):
                return RESTARTED
            sessions.manager(self.context).fail_running(session_id, "agent_start_failed")
        except JournalError:
            pass  # the control process is shutting down: its restart finishes or fails the start (``reattach``)
        return ENDED

    def _delivering(self, session_id: str) -> bool:
        at = iso_ms(self.context.journal.clock.now())
        with self.context.journal.transaction() as db:
            session = self._session(session_id)
            if session is None or session.state != "agent_starting":
                return False
            self.context.state.save_session(replace(session, state="prompt_delivering"), db)
            body: dict[str, str] = {"state": "prompt_delivering", "at": at}
            if session.claude_session_id is not None:
                body["claude_session_id"] = session.claude_session_id
            self.context.journal.insert(db, "session.state", body, session_id)
        return True


# -- process-wide access ------------------------------------------------------------------------------------------

_DELIVERY = "_prompt_delivery"
_delivery_lock = threading.Lock()


def install(context: ControlContext, created: Delivery) -> Delivery:
    """Attach ``created`` to ``context`` (tests and ``manager``); it closes with the control process."""
    vars(context)[_DELIVERY] = created
    context.on_close(created.close)
    return created


def manager(context: ControlContext) -> Delivery:
    """The delivery of this control process, on the session manager's tmux server (created on first use)."""
    with _delivery_lock:
        existing = vars(context).get(_DELIVERY)
        if isinstance(existing, Delivery):
            return existing
        pane = TmuxPane(sessions.manager(context).tmux)
        return install(context, Delivery(context, pane, tools_seconds=float(context.config.tools_seconds)))
