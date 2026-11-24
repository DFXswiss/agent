"""Transcript capture of the control process (05-runner.md section 6.4, 03-api.md section 7.2; ADR-025).

Claude Code writes its transcript to ``<session base>/home/.claude/projects/<cwd key>/<claude session id>.jsonl``
(``CLAUDE_CONFIG_DIR`` of the session unit; the cwd key is the workspace path with every character other than
``A-Za-z0-9`` replaced by ``-``). The file is display content only (usage comes from the gateway, 05 section 10.4).

``TranscriptParser`` (pure) turns transcript entries into journal bodies:

* the ``assistant`` text blocks of one API message (Claude Code writes one line per content block, all with the same
  ``message.id``) -> one ``session.output {message_id, role, text, turn, stop_reason?, truncated?}``, written when
  the next entry belongs to something else or the file stayed quiet for ``QUIET_SECONDS`` (``flush``); a block that
  arrives after its message was written becomes a further record ``<message_id>-<n>``. Blocks are joined by a blank
  line; text longer than ``MAX_TEXT`` UTF-8 bytes is cut (``truncated``). ``thinking`` blocks and side chains are
  not shown.
* a ``tool_use`` block and the ``tool_result`` with its ID -> ``session.tool {tool_use_id, name, input_summary,
  input, result_summary, is_error, duration_ms}``: input as JSON text and the result text, each cut at ``MAX_TEXT``
  bytes, the summary the first path or pattern argument (else the compact input) cut at ``MAX_SUMMARY``
  characters, the duration between the two entries' timestamps. Successful ``Write``/``Edit`` calls are journaled by
  the PostToolUse hook (GST-03); the parser marks them ``hook_owned`` and the follower writes them only if the hook
  record did not arrive within ``HOOK_GRACE_SECONDS``. A ``tool_use`` without result when the session ends is
  written with ``is_error`` and the result ``interrupted``.
* ``turn`` counts the user's prompts (``user`` entries with text that are neither meta nor tool results).
* ``claude_session_id`` is the ``sessionId`` of the entries (informational; the follower reports the file name).
* an entry whose ``uuid`` was already fed is skipped (a file read again does not repeat records or turns).

``TranscriptFollower`` (one per session, a thread of the control process) polls every ``POLL_SECONDS``: it opens the
newest ``<uuid>.jsonl`` of the project directory (or the one of the known Claude session ID) through
``scanner.open_dir``/``open_file`` (no symlinks, owner the session user), reads the complete lines after its offset
and keeps an unfinished last line for the next poll (``LineReader``); a file whose inode changed (replaced) or that
became shorter (truncated) is read again from its start, a newer transcript file (``/clear``, a resume under a new
ID) is switched to after the old one was read to its end. Records already in the journal (same ``message_id`` /
``tool_use_id`` of the session) are never written twice, so a restart of the control process may read the file again.

Work per poll is bounded (``MAX_READ`` bytes; text of one message beyond ``MAX_TEXT`` is not kept). When the Claude
session ID - the name of the transcript file being followed - is first seen, the follower stores it in the session
row (``tui_up``); a session past ``agent_starting`` journals its current state again with the ID. A starting session
is only stored: the move ``agent_starting`` -> ``prompt_delivering`` belongs to the watcher of ``delivery.py`` alone,
which waits for the platform tools as well (HOT-09, ADR-068), and its record carries the stored ID.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from agent_guest.clock import iso_ms
from agent_guest.journal import Journal
from agent_guest.scanner import open_dir, open_file
from agent_guest.state import State

POLL_SECONDS = 0.2
QUIET_SECONDS = 1.0
HOOK_GRACE_SECONDS = 5.0
MAX_TEXT = 64 * 1024
MAX_SUMMARY = 256
MAX_LINE = 32 * 1024 * 1024
MAX_READ = 8 * 1024 * 1024
READ_CHUNK = 1024 * 1024
MAX_COUNT = 9007199254740991
HOOK_TOOLS = frozenset({"Write", "Edit"})
SUMMARY_KEYS = ("file_path", "path", "pattern", "glob", "type")
INTERRUPTED = "interrupted"
# States of the session.state contract in which a session can be while its transcript is written.
REPEATABLE_STATES = frozenset({"prompt_delivering", "working", "idle", "resuming"})
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_STOP_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FILE_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl")
_KEY_RE = re.compile(r"[^A-Za-z0-9]")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def cwd_key(path: str) -> str:
    """Claude Code's project directory name of the working directory ``path``."""
    return _KEY_RE.sub("-", path)


def cut(text: str, limit: int = MAX_TEXT) -> tuple[str, bool]:
    """``text`` as valid UTF-8 of at most ``limit`` bytes (lone surrogates replaced) and whether it was cut."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return data.decode("utf-8"), False
    return data[:limit].decode("utf-8", "ignore"), True


def _millis(entry: Mapping[str, Any]) -> int | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict) and isinstance(block.get("type"), str):
                parts.append(f"[{block['type']}]")
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, sort_keys=True, ensure_ascii=False)


def summary(tool_input: object) -> str:
    """Short description of a tool call: its first path/pattern/type argument, else the compact input."""
    if isinstance(tool_input, dict):
        for key in SUMMARY_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value[:MAX_SUMMARY]
    return json.dumps(tool_input, sort_keys=True, ensure_ascii=False, separators=(",", ":"))[:MAX_SUMMARY]


@dataclass(frozen=True)
class Record:
    kind: str
    body: dict[str, Any]
    key: str
    hook_owned: bool = False


@dataclass
class _Message:
    message_id: str
    turn: int
    texts: list[str] = field(default_factory=list)
    size: int = 0
    stop_reason: str | None = None


@dataclass
class _ToolUse:
    name: str
    input: object
    started_ms: int | None


class TranscriptParser:
    def __init__(self) -> None:
        self.turn = 0
        self.claude_session_id: str | None = None
        self._message: _Message | None = None
        self._written: dict[str, int] = {}
        self._tools: dict[str, _ToolUse] = {}
        self._entries: set[str] = set()

    @property
    def idle(self) -> bool:
        return self._message is None and not self._tools

    def feed(self, entry: Mapping[str, Any]) -> list[Record]:
        uuid = entry.get("uuid")
        if isinstance(uuid, str):
            # An entry read again (file replaced or truncated, history copied into a new file) counts once.
            if uuid in self._entries:
                return []
            self._entries.add(uuid)
        session = entry.get("sessionId")
        if isinstance(session, str) and _UUID_RE.fullmatch(session):
            self.claude_session_id = session
        kind = entry.get("type")
        message = entry.get("message")
        if entry.get("isSidechain") is True or not isinstance(message, dict):
            return self._other(kind)
        content = message.get("content")
        if kind == "assistant" and isinstance(content, list):
            return self._assistant(entry, message, content)
        if kind == "user":
            return self._user(entry, content)
        return self._other(kind)

    def _other(self, kind: object) -> list[Record]:
        # Bookkeeping entries (api requests, queue operations, attachments) end nothing; others end the message.
        if kind in ("user", "assistant", "system", "last-prompt"):
            return self.flush()
        return []

    def _assistant(self, entry: Mapping[str, Any], message: Mapping[str, Any], content: list[Any]) -> list[Record]:
        records: list[Record] = []
        raw = message.get("id")
        raw = raw if isinstance(raw, str) and raw else str(entry.get("uuid") or "message")
        message_id = _UNSAFE_RE.sub("_", raw)[:120]
        if self._message is not None and self._message.message_id != message_id:
            records.extend(self.flush())
        if self._message is None:
            self._message = _Message(message_id, max(self.turn, 1))
        stop = message.get("stop_reason")
        if isinstance(stop, str) and _STOP_RE.fullmatch(stop):
            self._message.stop_reason = stop
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                if self._message.size <= MAX_TEXT:
                    # Beyond 64 KiB the text is cut anyway: further blocks are not kept in memory.
                    self._message.texts.append(block["text"])
                    self._message.size += len(block["text"])
            elif block.get("type") == "tool_use":
                tool_id, name = block.get("id"), block.get("name")
                if isinstance(tool_id, str) and _ID_RE.fullmatch(tool_id) and isinstance(name, str):
                    self._tools[tool_id] = _ToolUse(name, block.get("input"), _millis(entry))
        return records

    def _user(self, entry: Mapping[str, Any], content: object) -> list[Record]:
        records = self.flush()
        if isinstance(content, str):
            prompt = True
        elif isinstance(content, list):
            prompt = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    records.extend(self._result(entry, block))
                elif block.get("type") == "text":
                    prompt = True
        else:
            prompt = False
        if prompt and entry.get("isMeta") is not True:
            self.turn += 1
        return records

    def _result(self, entry: Mapping[str, Any], block: Mapping[str, Any]) -> list[Record]:
        tool_id = block.get("tool_use_id")
        if not isinstance(tool_id, str) or tool_id not in self._tools:
            return []
        use = self._tools.pop(tool_id)
        ended = _millis(entry)
        duration = 0 if use.started_ms is None or ended is None else min(max(ended - use.started_ms, 0), MAX_COUNT)
        is_error = block.get("is_error") is True
        record = self._tool(tool_id, use, _result_text(block.get("content")), is_error, duration)
        if record is None:
            return []
        return [replace(record, hook_owned=use.name in HOOK_TOOLS and not is_error)]

    def _tool(self, tool_id: str, use: _ToolUse, result: str, is_error: bool, duration: int) -> Record | None:
        if not _NAME_RE.fullmatch(use.name):
            return None
        body = {
            "tool_use_id": tool_id,
            "name": use.name,
            "input_summary": cut(summary(use.input), MAX_SUMMARY * 4)[0][:MAX_SUMMARY],
            "input": cut(json.dumps(use.input, sort_keys=True, ensure_ascii=False))[0],
            "result_summary": cut(result)[0],
            "is_error": is_error,
            "duration_ms": duration,
        }
        return Record("session.tool", body, tool_id)

    def flush(self) -> list[Record]:
        """Write the open message (if it has text)."""
        message, self._message = self._message, None
        if message is None or not message.texts:
            return []
        count = self._written.get(message.message_id, 0) + 1
        self._written[message.message_id] = count
        message_id = message.message_id if count == 1 else f"{message.message_id}-{count}"
        text, truncated = cut("\n\n".join(message.texts))
        body: dict[str, Any] = {"message_id": message_id, "role": "assistant", "text": text, "turn": message.turn}
        if message.stop_reason is not None:
            body["stop_reason"] = message.stop_reason
        if truncated:
            body["truncated"] = True
        return [Record("session.output", body, message_id)]

    def finish(self) -> list[Record]:
        """End of the session: the open message and every tool call without result."""
        records = self.flush()
        for tool_id, use in sorted(self._tools.items()):
            record = self._tool(tool_id, use, INTERRUPTED, True, 0)
            if record is not None:
                records.append(record)
        self._tools.clear()
        return records


class LineReader:
    """Complete lines of a growing file, robust against partial lines, replacement and truncation."""

    def __init__(self) -> None:
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self._partial = b""
        self._skipping = False
        self.more = False

    def read(self, fd: int, status: os.stat_result) -> list[bytes]:
        identity = (status.st_dev, status.st_ino)
        if identity != self.identity or status.st_size < self.offset:
            self.identity, self.offset, self._partial, self._skipping = identity, 0, b"", False
        os.lseek(fd, self.offset, os.SEEK_SET)
        lines: list[bytes] = []
        taken = 0
        self.more = False
        while chunk := os.read(fd, READ_CHUNK):
            self.offset += len(chunk)
            taken += len(chunk)
            pieces = (self._partial + chunk).split(b"\n")
            self._partial = pieces.pop()
            for piece in pieces:
                if not self._skipping:
                    lines.append(piece)
                self._skipping = False
            if len(self._partial) > MAX_LINE:
                # An absurdly long line (never a transcript entry we show) is dropped up to its end.
                self._partial, self._skipping = b"", True
            if taken >= MAX_READ:
                # Bounded work per poll; the rest is read by the next one.
                self.more = True
                break
        return lines


def entries(lines: list[bytes]) -> list[dict[str, Any]]:
    found = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def tui_up(journal: Journal, state: State, session_id: str, claude_session_id: str | None = None) -> bool:
    """Store a new Claude session ID: journaled with the current state when the session is past ``agent_starting``,
    only stored while it starts (module docstring); ``True`` if something was written."""
    with journal.transaction() as db:
        row = db.execute("SELECT state, claude_session_id FROM session WHERE id = ?", (session_id,)).fetchone()
        session = state.session(session_id)
        if row is None or session is None:
            return False
        current, known = row
        if claude_session_id is None or claude_session_id == known:
            return False
        if current == "agent_starting":
            state.save_session(replace(session, claude_session_id=claude_session_id), db)
            return True
        if current not in REPEATABLE_STATES:
            return False
        target, stored = current, claude_session_id
        state.save_session(replace(session, state=target, claude_session_id=stored), db)
        body: dict[str, Any] = {"state": target, "at": iso_ms(journal.clock.now()), "claude_session_id": stored}
        journal.insert(db, "session.state", body, session_id)
        return True


def journaled_keys(journal: Journal, session_id: str) -> set[str]:
    """``message_id``/``tool_use_id`` of the transcript records of the session already in the journal."""
    with journal.transaction() as db:
        rows = db.execute(
            "SELECT kind, body FROM journal WHERE session_id = ? AND kind IN ('session.output', 'session.tool')",
            (session_id,),
        ).fetchall()
    keys = set()
    for kind, body in rows:
        value = json.loads(body)
        keys.add(f"{kind}:{value['message_id'] if kind == 'session.output' else value['tool_use_id']}")
    return keys


def hook_wrote(journal: Journal, session_id: str, tool_use_id: str) -> bool:
    """Whether the PostToolUse hook already journaled ``session.tool`` for ``tool_use_id``."""
    with journal.transaction() as db:
        row = db.execute(
            "SELECT 1 FROM journal WHERE session_id = ? AND kind = 'session.tool'"
            " AND json_extract(body, '$.tool_use_id') = ? LIMIT 1",
            (session_id, tool_use_id),
        ).fetchone()
    return row is not None


@dataclass
class _Deferred:
    record: Record
    until: float


class TranscriptFollower:
    """Follows the transcript of one session and journals its records."""

    def __init__(
        self,
        journal: Journal,
        state: State,
        session_id: str,
        base: str,
        uid: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        poll: float = POLL_SECONDS,
    ) -> None:
        self.journal = journal
        self.state = state
        self.session_id = session_id
        self.base = base
        self.uid = uid
        self.monotonic = monotonic
        self.poll = poll
        self.parser = TranscriptParser()
        self.reader = LineReader()
        self.current: str | None = None
        self._keys = journaled_keys(journal, session_id)
        self._deferred: list[_Deferred] = []
        self._last_data = monotonic()
        self._reported: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def project_parts(self) -> list[str]:
        return ["home", ".claude", "projects", cwd_key(f"{self.base}/work")]

    def _candidates(self, fd: int) -> dict[str, int]:
        found = {}
        for name in os.listdir(fd):
            if not _FILE_RE.fullmatch(name):
                continue
            try:
                status = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if status.st_uid == self.uid and (status.st_mode & 0o170000) == 0o100000:
                found[name] = status.st_mtime_ns
        return found

    def _choose(self, candidates: Mapping[str, int]) -> str | None:
        if self.current is None:
            session = self.state.session(self.session_id)
            preferred = f"{session.claude_session_id}.jsonl" if session and session.claude_session_id else None
            if preferred in candidates:
                return preferred
        if not candidates:
            return None
        newest = max(candidates, key=lambda name: (candidates[name], name))
        current = self.current
        if current is None or current not in candidates or candidates[newest] > candidates[current]:
            return newest
        return current

    def _read(self, dir_fd: int, name: str) -> list[Record]:
        try:
            fd, status = open_file(dir_fd, name, self.uid)
        except OSError:
            return []
        try:
            lines = self.reader.read(fd, status)
        finally:
            os.close(fd)
        records: list[Record] = []
        for entry in entries(lines):
            records.extend(self.parser.feed(entry))
        if lines:
            self._last_data = self.monotonic()
        return records

    def step(self) -> int:
        """One poll; returns the number of records written."""
        records: list[Record] = []
        try:
            fd = open_dir(self.base, self.project_parts)
        except OSError:
            fd = -1
        if fd >= 0:
            try:
                candidates = self._candidates(fd)
                chosen = self._choose(candidates)
                if chosen is not None and chosen != self.current:
                    if self.current is not None:
                        records.extend(self._read(fd, self.current))
                        records.extend(self.parser.flush())
                    self.current, self.reader = chosen, LineReader()
                if self.current is not None:
                    records.extend(self._read(fd, self.current))
            finally:
                os.close(fd)
        if self.monotonic() - self._last_data >= QUIET_SECONDS:
            records.extend(self.parser.flush())
        # The ID is the file name Claude Code chose (what --resume needs), not a value inside the session's lines.
        claude_id = None if self.current is None else self.current.removesuffix(".jsonl")
        if claude_id is not None and claude_id != self._reported:
            tui_up(self.journal, self.state, self.session_id, claude_id)
            self._reported = claude_id
        return self._write(records, final=False)

    def finish(self) -> int:
        """Last poll of an ended session: everything still open is written."""
        written = self.step()
        while self.reader.more:
            written += self.step()
        records = self.parser.finish()
        return written + self._write(records, final=True)

    def _write(self, records: list[Record], final: bool) -> int:
        now = self.monotonic()
        self._deferred.extend(_Deferred(record, now + HOOK_GRACE_SECONDS) for record in records if record.hook_owned)
        ready = [record for record in records if not record.hook_owned]
        waiting = []
        for item in self._deferred:
            if not (final or item.until <= now):
                waiting.append(item)
            elif not hook_wrote(self.journal, self.session_id, item.record.key):
                ready.append(item.record)
        self._deferred = waiting
        fresh = []
        for record in ready:
            key = f"{record.kind}:{record.key}"
            if key not in self._keys:
                self._keys.add(key)
                fresh.append(record)
        if fresh:
            with self.journal.transaction() as db:
                for record in fresh:
                    self.journal.insert(db, record.kind, record.body, self.session_id)
        return len(fresh)

    # -- thread -----------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="agent-guest-transcript", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.step()
            except Exception:
                sys.stderr.write("agent-guest-control: transcript capture failed\n")
                traceback.print_exc(file=sys.stderr)

    def stop(self, final: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            if self._thread.is_alive():
                # Never read concurrently with a poll that is stuck; its records are written by that poll.
                sys.stderr.write("agent-guest-control: transcript capture did not stop in time\n")
                return
        if final:
            self.finish()
