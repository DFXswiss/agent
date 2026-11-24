"""Context archives of a session: export, staging and restore (ADR-072 sections 2 and 7; 03-api.md section 7.1
``context/export`` and ``contexts/{id}/stage``; 05-runner.md sections 4.3 and 6.3; review KOR-33 nos. 5, 9, 13, 14 and
19).

A context archive (v1) is a tar stream with the members, in this order:

1. ``context.json`` - canonical JSON ``{v: 1, agent_kind, agent_version, claude_session_id, session_id, task_id, cwd,
   created_at, transcript_files, workspace: "included"|"omitted_too_large"|"empty", workspace_files,
   workspace_bytes, workspace_omitted}``; ``cwd`` is the session's work directory, ``transcript_files`` the exported
   transcript paths relative to the session's Claude directory ``home/.claude`` (sorted), ``workspace_omitted`` the
   number of workspace entries the export left out (see below)
2. ``transcript/<path>`` - only the conversation's own files below ``home/.claude``: ``projects/<cwd key>/<id>.jsonl``,
   the companion directory ``projects/<cwd key>/<id>/**`` (tool results, subagents), ``todos/<id>*`` and
   ``file-history/<id>/**``. Every ``.jsonl`` among them is cut after its last line feed (an incomplete last line -
   the agent was writing - is left out; nothing else of a transcript is ever changed). Settings, ``.claude.json``,
   commands, agents, skills, hooks and credentials are never exported, so a session cannot plant configuration into
   its successor
3. ``work/<path>`` - the workspace: every directory (empty ones too, ``0755``), every regular file of the session user
   (``0755`` when any execute bit is set, else ``0644``) and every symbolic link whose target stays inside the
   workspace (a relative target as it is, an absolute one below the work directory made relative)

Every file is read with ``O_NOFOLLOW`` from root-owned parents through the walk of ``files.py``; members are owned
by 0 with time 0. Left out of the workspace and counted in ``workspace_omitted``: names that are no valid path or
have a protected name (``files.path_ok``: ``.git``, ``.claude``, ``CLAUDE.md``, ``AGENTS.md``, ...), links that leave
the workspace, devices and FIFOs, files of another owner, unreadable entries and directories deeper than
``files.MAX_DEPTH``. The runner tells the restored session and the user about them.

Before anything is read the conversation file must be quiet (``settle``, KOR-33 E2E: the runner asks for a
checkpoint when the session becomes ``idle``, which the ``Stop`` hook reports before Claude Code has written the
turn to its transcript - such checkpoints held 545-623 bytes and failed the restored start). The export waits,
polling every ``SETTLE_POLL_SECONDS`` and at most ``SETTLE_MAX_SECONDS``, until the file was not written for
``SETTLE_SECONDS`` and - while the session is ``idle`` - its last conversation record (``user`` or ``assistant``,
side chains left out, within the last ``TAIL_BYTES``) is a final assistant answer (no ``tool_use`` block). A
``working`` session (the periodic checkpoint) or one that is still starting is exported at once. When the wait runs
out, what is there is exported. A conversation file that then holds no complete turn - no ``user`` and no
``assistant`` record - is ``state_conflict``: there is nothing to save yet (``has_turn``; the runner refuses such
an archive as well).

``export_context(session, ...)`` builds the archive in a staging directory of the session (``files.make_stage``):
transcript and workspace are written to two part files, and the answer stream (``files.Stream``) sends the
``context.json`` member, the parts without their end-of-archive blocks and one end. The transcript is limited by the
caller's ``transcript_max`` (``size_exceeded``); a workspace over ``workspace_max`` bytes (or over the entry limit of
the walk) is left out (``omitted_too_large``) and the transcript is still exported. A session that never had a
conversation (no Claude session ID, no transcript file) or whose sandbox was never created is ``state_conflict``;
directories removed by the retention are ``not_found``.

``stage(sessions_root, session_id, stream, now=...)`` takes such an archive for a session that is about to be created
(or restored under its own ID) into ``<sessions_root>/.contexts/<session_id>`` - root 0700, invisible to sessions
(their units mount an empty ``/srv/sessions``) and on the file system of the session directories, so the restore
moves instead of copying. The archive is extracted with the import rules of ``files.py`` (regular files and
directories, path rules, entry and header limits, at most ``MAX_STAGE_BYTES``; execute bits kept; symbolic links only
below ``work/`` with a relative target inside it) into a fresh directory first and then checked: top level only
``context.json`` (regular, at most ``MAX_CONTEXT_JSON`` bytes, a valid v1 object), ``transcript/`` and ``work/``;
``transcript/`` holds only the files of the conversation ``claude_session_id`` (``projects/<key of cwd>/<id>.jsonl``,
``projects/<key of cwd>/<id>/``, ``todos/<id>*``, ``file-history/<id>/``; review no. 19). Transcript lines are not
parsed one by one (review no. 13): the runner verified the archive (AEAD, digest, signed manifest) and Claude Code
tolerates damaged lines of its own transcripts. Then the heavy part of the restore is done here, not in the
``POST /v1/sessions`` that follows (review no. 9): in the contents of every transcript file the old session directory
``/srv/sessions/<old id>/`` and the old cwd key are replaced byte for byte by those of the staged session ID, and so
is the key in every file name (``projects/<cwd key>/`` itself included) - nothing else of the transcript changes,
entries are never filtered or reordered (spike KOR-33: filtering broke compacted transcripts) -, and the record keys
of the conversation files are written to ``transcript-baseline.json`` of the staging. A refused archive leaves an
earlier staging of the session as it was; an accepted one replaces it. Stagings older than ``STAGE_TTL_SECONDS`` are
removed at every stage and by the session cleanup (``prune``).

``restore(staged, base, chown)`` (``sessions.SessionManager`` while it creates or resumes the sandbox, before
``sandbox_ready``, so the workspace scanner takes the restored tree as its baseline) only renames: the transcript
files into ``home/.claude`` (an existing file or directory of the same name - a suspended session restored under its
own ID, A->B->A, review no. 14 - is replaced), the staged workspace replaces ``work``, the baseline file goes to the
session directory; then everything below ``home/.claude`` and ``work`` is handed to the session user (``chown``
without following links). ``discard(staged)`` removes the staging afterwards. Claude Code resumes the conversation
under the same ID and appends to the same file (``--resume``, never ``--fork-session``).
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_guest import files, transcript
from agent_guest.driver import AGENT_KIND
from agent_guest.files import FilesError
from agent_guest.journal import canonical
from agent_guest.state import Session
from agent_guest.transcript import cwd_key

VERSION = 1
CONTEXT_FILE = "context.json"
TRANSCRIPT = "transcript"
WORK = "work"
TRASH = ".replaced"
TRANSCRIPT_TOPS = frozenset({"projects", "todos", "file-history"})
STAGE_DIR = ".contexts"
MAX_STAGE_BYTES = 16 * 1024 * 1024 * 1024
MAX_CONTEXT_JSON = 1024 * 1024
MAX_LINK = 4096
STAGE_TTL_SECONDS = 6 * 3600
MEMBER_MODE = 0o644
EXEC_MODE = 0o755
DIR_MODE = 0o755
END = b"\0" * (2 * tarfile.BLOCKSIZE)
SETTLE_SECONDS = 1.0
SETTLE_POLL_SECONDS = 0.1
SETTLE_MAX_SECONDS = 20.0
TAIL_BYTES = 8 * 1024 * 1024
TURN_KINDS = frozenset({"user", "assistant"})
UNSETTLED_STATES = frozenset({"agent_starting", "prompt_delivering", "working"})
"""States whose transcript is written all the time: exported at once (the periodic checkpoint)."""
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# -- export --------------------------------------------------------------------------------------------------------


def complete_length(fd: int, size: int) -> int:
    """Length of the first ``size`` bytes of ``fd`` up to and including their last line feed (0 without one)."""
    end = size
    while end > 0:
        start = max(0, end - files.CHUNK)
        data = os.pread(fd, end - start, start)
        found = data.rfind(b"\n")
        if found >= 0:
            return start + found + 1
        end = start
    return 0


class _Reader:
    """``read`` of exactly ``length`` bytes of ``fd`` for ``tarfile``; a file that became shorter meanwhile (it was
    replaced or truncated) is ``unavailable`` - the runner tries the checkpoint again later."""

    def __init__(self, fd: int, length: int) -> None:
        self._fd, self._left = fd, length

    def read(self, size: int = -1) -> bytes:
        size = self._left if size < 0 else min(size, self._left)
        data = os.read(self._fd, size) if size else b""
        if len(data) != size:
            raise FilesError("unavailable")
        self._left -= size
        return data


@dataclass
class _ContextExport(files._Export):
    """The walk of ``files._Export`` with the context rules (see the module docstring): execute bits, directories
    and links inside the workspace are kept (``workspace``), every ``.jsonl`` is cut after its last line feed, and
    every entry left out is counted (``omitted``)."""

    workspace: str = ""
    """The work directory when this walk exports it (directories and links are kept); empty for the transcript."""
    omitted: int = 0
    dirs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def skip(self, path: str, reason: str) -> None:
        super().skip(path, reason)
        self.omitted += 1

    def add_file(self, fd: int, path: str) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            self.skip(path, "special")
            return
        if info.st_uid != self.session.uid:
            self.skip(path, "unreadable")
            return
        mode = EXEC_MODE if info.st_mode & 0o111 else MEMBER_MODE
        if path.endswith(".jsonl"):
            # a transcript: exactly its complete lines (a file that shrinks meanwhile is `unavailable`)
            length = complete_length(fd, info.st_size)
            if self.bytes + length > self.limit:
                raise FilesError("size_exceeded")
            os.lseek(fd, 0, os.SEEK_SET)
            self.archive.addfile(files.member_info(self.prefix + path, length, mode), _Reader(fd, length))
        else:
            # any other file as far as it can be read now (the session may be writing it)
            with tempfile.SpooledTemporaryFile(max_size=files.SPOOL_MEMORY, dir=self.stage) as spool:
                length = 0
                while True:
                    try:
                        data = os.read(fd, files.CHUNK)
                    except OSError:
                        self.skip(path, "unreadable")
                        return
                    if not data:
                        break
                    length += len(data)
                    if self.bytes + length > self.limit:
                        raise FilesError("size_exceeded")
                    spool.write(data)
                spool.seek(0)
                self.archive.addfile(files.member_info(self.prefix + path, length, mode), spool)
        self.bytes += length
        self.added.add(path)
        self.files.append({"path": path, "size": length})

    def entry(self, parent: int, name: str, path: str, depth: int) -> None:
        if path in self.added:
            return
        if not files.path_ok(path):
            self.added.add(path)
            self.omitted += 1
            return
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) and self.workspace:
            self.link(parent, name, path)
            return
        if stat.S_ISDIR(info.st_mode) and self.workspace and depth + 1 <= files.MAX_DEPTH:
            self.archive.addfile(_dir_info(self.prefix + path))
            self.dirs.append(path)
        super().entry(parent, name, path, depth)

    def link(self, parent: int, name: str, path: str) -> None:
        """A link whose target stays inside the workspace, as a relative link; any other one is left out."""
        try:
            target = os.readlink(name, dir_fd=parent)
        except OSError:
            self.skip(path, "unreadable")
            return
        relative = inside_target(self.workspace, path, target)
        if relative is None:
            self.skip(path, "symlink")
            return
        info = tarfile.TarInfo(self.prefix + path)
        info.type = tarfile.SYMTYPE
        info.linkname = relative
        info.mode = 0o777
        info.mtime = 0
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        self.archive.addfile(info)
        self.added.add(path)
        self.links.append(path)


def _dir_info(path: str) -> tarfile.TarInfo:
    info = files.member_info(path, 0, DIR_MODE)
    info.type = tarfile.DIRTYPE
    return info


def inside_target(workspace: str, path: str, target: str) -> str | None:
    """The relative target of the link ``path`` (relative to the workspace) when ``target`` stays inside the
    workspace; None otherwise."""
    if not target or len(target) > MAX_LINK:
        return None
    if target.startswith("/"):
        if not target.startswith(f"{workspace}/"):
            return None
        target = posixpath.relpath(target, posixpath.join(workspace, posixpath.dirname(path)))
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../"):
        return None
    return target


def _record(line: bytes) -> dict[str, Any] | None:
    """A transcript line as an object, None when it does not parse (Claude Code tolerates such lines)."""
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _lines_forward(fd: int, length: int) -> Iterator[bytes]:
    """The complete lines of the first ``length`` bytes of ``fd``, from the start."""
    carry = b""
    offset = 0
    while offset < length:
        data = carry + os.pread(fd, min(files.CHUNK, length - offset), offset)
        offset += files.CHUNK
        *lines, carry = data.split(b"\n")
        yield from lines


def _lines_backward(fd: int, length: int, budget: int) -> Iterator[bytes]:
    """The complete lines of the first ``length`` bytes of ``fd``, from the end, reading at most ``budget`` bytes; a
    line that does not fit into the budget ends the iteration."""
    end = length
    carry = b""
    while end > 0 and length - end < budget:
        start = max(0, end - files.CHUNK, length - budget)
        data = os.pread(fd, end - start, start) + carry
        end = start
        first, *lines = data.split(b"\n")
        yield from reversed(lines)
        carry = first
    if end == 0:
        yield carry


def has_turn(lines: Iterable[bytes]) -> bool:
    """A ``user`` and an ``assistant`` record among ``lines`` (stops reading once both were seen)."""
    seen: set[str] = set()
    for line in lines:
        kind = (_record(line) or {}).get("type")
        if isinstance(kind, str) and kind in TURN_KINDS:
            seen.add(kind)
            if seen == TURN_KINDS:
                return True
    return False


def turn_ended(lines_from_end: Iterable[bytes]) -> bool:
    """The last conversation record (``user``/``assistant``, side chains left out) is a final assistant answer: an
    ``assistant`` record without a ``tool_use`` block."""
    for line in lines_from_end:
        record = _record(line)
        if record is None or record.get("type") not in TURN_KINDS or record.get("isSidechain") is True:
            continue
        if record["type"] != "assistant":
            return False
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        return not any(isinstance(block, dict) and block.get("type") == "tool_use" for block in blocks)
    return False


def _open_conversation(base: int, slug: str, claude_session_id: str, uid: int) -> int | None:
    """The conversation file of the session user (read-only, never through a link); None when there is none."""
    try:
        project = files.walk(base, ["home", ".claude", "projects", slug])
    except (FilesError, OSError):
        return None
    try:
        fd = os.open(f"{claude_session_id}.jsonl", files.READ_FLAGS, dir_fd=project)
    except OSError:
        return None
    finally:
        os.close(project)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        os.close(fd)
        return None
    return fd


@dataclass
class Settle:
    """The clocks and the session state of ``settle`` (injected by tests)."""

    state: Callable[[], str | None]
    wall: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def settle(base: int, session: Session, slug: str, clock: Settle) -> None:
    """Wait until the conversation file is quiet (see the module docstring); raises ``state_conflict`` when it then
    holds no complete turn."""
    claude_session_id = str(session.claude_session_id)
    deadline = clock.monotonic() + SETTLE_MAX_SECONDS
    while True:
        fd = _open_conversation(base, slug, claude_session_id, session.uid)
        if fd is None:
            return  # no conversation file: the export answers `state_conflict` itself
        try:
            info = os.fstat(fd)
            length = complete_length(fd, info.st_size)
            state = clock.state()
            quiet = state in UNSETTLED_STATES or clock.wall() - info.st_mtime >= SETTLE_SECONDS
            ended = state != "idle" or turn_ended(_lines_backward(fd, length, TAIL_BYTES))
            if (quiet and ended) or clock.monotonic() >= deadline:
                if not has_turn(_lines_forward(fd, length)):
                    raise FilesError("state_conflict")
                return
        finally:
            os.close(fd)
        clock.sleep(SETTLE_POLL_SECONDS)


def _transcript(export: _ContextExport, base: int, claude_session_id: str, slug: str) -> None:
    try:
        claude = files.walk(base, ["home", ".claude"])
    except FilesError:
        raise FilesError("state_conflict") from None
    try:
        name = f"projects/{slug}/{claude_session_id}.jsonl"
        export.listed(claude, name)
        if name not in {item["path"] for item in export.files}:
            raise FilesError("state_conflict")
        export.listed(claude, f"projects/{slug}/{claude_session_id}")
        for todo in _todos(claude, claude_session_id):
            export.listed(claude, f"todos/{todo}")
        export.listed(claude, f"file-history/{claude_session_id}")
    finally:
        os.close(claude)


def _todos(claude: int, claude_session_id: str) -> list[str]:
    try:
        todos = files.walk(claude, ["todos"])
    except FilesError:
        return []
    try:
        names = os.listdir(todos)
    finally:
        os.close(todos)
    return sorted(name for name in names if name.startswith(claude_session_id))


def _workspace(export: _ContextExport, session: Session) -> str:
    try:
        root = files.open_workspace(session)
    except FilesError:
        return "empty"
    try:
        export.children(root, "", 0)
    except FilesError as error:
        if error.code != "size_exceeded":
            raise
        return "omitted_too_large"
    finally:
        os.close(root)
    return "included" if export.files or export.dirs or export.links else "empty"


def _part(stage: str, name: str) -> tuple[str, tarfile.TarFile]:
    path = os.path.join(stage, name)
    return path, tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT)


def export_context(
    session: Session,
    *,
    task_id: str,
    agent_version: str,
    created_at: str,
    workspace_max: int,
    transcript_max: int,
    clock: Settle | None = None,
) -> files.Stream:
    """The context archive of ``session`` as a stream (see the module docstring); ``clock`` (the session's current
    state and the clocks) lets the export wait for a quiet transcript - without it the session counts as ``working``.
    """
    claude_session_id = session.claude_session_id
    if session.uid < 0 or claude_session_id is None:
        raise FilesError("state_conflict")
    try:
        stage = files.make_stage(session)
    except FileNotFoundError:
        raise FilesError("not_found") from None
    try:
        slug = cwd_key(session.workspace)
        base = os.open(os.path.dirname(session.workspace), files.DIR_FLAGS)
        try:
            settle(base, session, slug, clock or Settle(state=lambda: "working"))
        except BaseException:
            os.close(base)
            raise
        transcript_path, archive = _part(stage, "transcript.tar")
        try:
            with archive:
                part = _ContextExport(
                    session, stage, archive, prefix=f"{TRANSCRIPT}/", limit=transcript_max, mode=MEMBER_MODE
                )
                _transcript(part, base, claude_session_id, slug)
                transcript_end = archive.offset
        finally:
            os.close(base)
        work_path, archive = _part(stage, "work.tar")
        with archive:
            work = _ContextExport(
                session,
                stage,
                archive,
                prefix=f"{WORK}/",
                limit=workspace_max,
                mode=MEMBER_MODE,
                workspace=session.workspace,
            )
            included = _workspace(work, session)
            work_end = archive.offset
        context = {
            "v": VERSION,
            "agent_kind": AGENT_KIND,
            "agent_version": agent_version,
            "claude_session_id": claude_session_id,
            "session_id": session.id,
            "task_id": task_id,
            "cwd": session.workspace,
            "created_at": created_at,
            "transcript_files": sorted(item["path"] for item in part.files),
            "workspace": included,
            "workspace_files": len(work.files) if included == "included" else 0,
            "workspace_bytes": work.bytes if included == "included" else 0,
            "workspace_omitted": work.omitted if included != "omitted_too_large" else 0,
        }
        head = files.member_bytes(CONTEXT_FILE, canonical(context).encode("utf-8"), MEMBER_MODE)
        parts: list[tuple[str, int | None]] = [(transcript_path, transcript_end)]
        if included == "included":
            parts.append((work_path, work_end))
    except BaseException:
        files.remove_stage(stage)
        raise
    return files.Stream(head, parts, stage, END)


# -- staging -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Staged:
    """A staged context: its directory and its ``context.json``."""

    path: str
    context: dict[str, Any]


def stage_root(sessions_root: str) -> str:
    return f"{sessions_root}/{STAGE_DIR}"


def _ensure_root(sessions_root: str) -> str:
    root = stage_root(sessions_root)
    os.makedirs(sessions_root, mode=0o755, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(root, 0o700)
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise FilesError("unavailable")
    os.chmod(root, 0o700)
    return root


def prune(sessions_root: str, now: float, ttl: float = STAGE_TTL_SECONDS) -> list[str]:
    """Remove stagings (and unfinished extractions) older than ``ttl`` seconds; returns their names."""
    root = stage_root(sessions_root)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    removed = []
    for name in names:
        path = f"{root}/{name}"
        try:
            age = now - os.lstat(path).st_mtime
        except OSError:
            continue
        if age > ttl:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    return removed


def check_context(data: bytes) -> dict[str, Any]:
    """The checked ``context.json`` of a staged archive (``request_invalid`` otherwise)."""
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise FilesError("request_invalid") from None
    if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != VERSION:
        raise FilesError("request_invalid")
    for key in ("claude_session_id", "session_id", "task_id"):
        if not isinstance(value.get(key), str) or not _UUID_RE.fullmatch(value[key]):
            raise FilesError("request_invalid")
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/") or not files.path_ok(cwd[1:]):
        raise FilesError("request_invalid")
    return value


def staged_mode(path: str, member: tarfile.TarInfo) -> int:
    """Workspace files keep their execute bit (``0755``/``0644``); transcript files are ``0600``."""
    if not path.startswith(f"{WORK}/"):
        return files.FILE_MODE
    return EXEC_MODE if member.mode & 0o111 else MEMBER_MODE


def staged_link_ok(path: str, target: str) -> bool:
    """A staged link: only below ``work/``, with a relative target that stays inside it."""
    if not path.startswith(f"{WORK}/") or target.startswith("/"):
        return False
    return inside_target(f"/{WORK}", path.removeprefix(f"{WORK}/"), target) is not None


def _entries(path: str) -> Iterator[os.DirEntry[str]]:
    with os.scandir(path) as found:
        yield from list(found)


def _is_dir(entry: os.DirEntry[str]) -> bool:
    return entry.is_dir(follow_symlinks=False)


def _is_file(entry: os.DirEntry[str]) -> bool:
    return entry.is_file(follow_symlinks=False)


def _check_transcript(path: str, context: dict[str, Any]) -> None:
    """Only the files of the conversation ``claude_session_id`` (see the module docstring)."""
    cid = context["claude_session_id"]
    for top in _entries(path):
        if top.name not in TRANSCRIPT_TOPS or not _is_dir(top):
            raise FilesError("request_invalid")
        for item in _entries(top.path):
            if top.name == "projects":
                if item.name != cwd_key(context["cwd"]) or not _is_dir(item):
                    raise FilesError("request_invalid")
                for found in _entries(item.path):
                    jsonl = found.name == f"{cid}.jsonl" and _is_file(found)
                    if not jsonl and not (found.name == cid and _is_dir(found)):
                        raise FilesError("request_invalid")
            elif top.name == "todos":
                if not item.name.startswith(cid) or not _is_file(item):
                    raise FilesError("request_invalid")
            elif item.name != cid or not _is_dir(item):
                raise FilesError("request_invalid")


def _check_tree(tree: str) -> dict[str, Any]:
    names = {entry.name: entry for entry in _entries(tree)}
    found = names.pop(CONTEXT_FILE, None)
    if found is None or not _is_file(found) or found.stat(follow_symlinks=False).st_size > MAX_CONTEXT_JSON:
        raise FilesError("request_invalid")
    with open(found.path, "rb") as source:
        context = check_context(source.read())
    for name, entry in names.items():
        if name not in (TRANSCRIPT, WORK) or not _is_dir(entry):
            raise FilesError("request_invalid")
    if TRANSCRIPT in names:
        _check_transcript(names[TRANSCRIPT].path, context)
    return context


def _rewrite(tree: str, context: dict[str, Any], base: str) -> None:
    """The path rewrite and the transcript baseline of the module docstring, inside the staging ``tree``."""
    old_cwd = str(context["cwd"])
    old_key, new_key = cwd_key(old_cwd), cwd_key(f"{base}/work")
    pairs = [
        (f"{os.path.dirname(old_cwd)}/".encode(), f"{base}/".encode()),
        (old_key.encode(), new_key.encode()),
    ]
    staged_transcript = f"{tree}/{TRANSCRIPT}"
    conversations: list[str] = []
    if os.path.isdir(staged_transcript):
        _rename_inside(staged_transcript, old_key, new_key)
        for path in _tree(staged_transcript):
            if os.path.isfile(path):
                replace_bytes(path, pairs)
        project = f"{staged_transcript}/projects/{new_key}"
        conversations = sorted(
            f"{project}/{name}"
            for name in (os.listdir(project) if os.path.isdir(project) else ())
            if name.endswith(".jsonl") and os.path.isfile(f"{project}/{name}")
        )
    transcript.write_baseline(tree, transcript.baseline_keys(conversations))


def stage(sessions_root: str, session_id: str, stream: files.Readable, *, now: float) -> dict[str, int]:
    """Stage the context archive ``stream`` for ``session_id`` (see the module docstring); ``{files, bytes}``."""
    root = _ensure_root(sessions_root)
    prune(sessions_root, now)
    temporary = f"{root}/.new-{uuid.uuid4().hex}"
    os.mkdir(temporary, 0o700)
    try:
        tree = os.open(temporary, files.DIR_FLAGS)
        try:
            owner = files.Owner(os.getuid(), os.getgid())
            done = files._extract(stream, tree, owner, MAX_STAGE_BYTES, modes=staged_mode, link_ok=staged_link_ok)
        finally:
            os.close(tree)
        context = _check_tree(temporary)
        _rewrite(temporary, context, f"{sessions_root}/{session_id}")
        target = f"{root}/{session_id}"
        shutil.rmtree(target, ignore_errors=True)
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"files": done.files, "bytes": done.bytes}


def staged(sessions_root: str, session_id: str) -> Staged | None:
    """The staging of ``session_id``, or None when there is none (or it is unreadable)."""
    path = f"{stage_root(sessions_root)}/{session_id}"
    try:
        with open(f"{path}/{CONTEXT_FILE}", "rb") as source:
            return Staged(path, check_context(source.read(MAX_CONTEXT_JSON + 1)))
    except (OSError, FilesError):
        return None


def discard(found: Staged) -> None:
    shutil.rmtree(found.path, ignore_errors=True)


# -- restore -------------------------------------------------------------------------------------------------------


def replace_bytes(path: str, pairs: Sequence[tuple[bytes, bytes]]) -> None:
    """Replace every occurrence of each ``old`` by its ``new`` in the file ``path`` (streamed; a match across two
    reads is found because the last ``len(old) - 1`` bytes are carried into the next read)."""
    pairs = [(old, new) for old, new in pairs if old != new]
    if not pairs:
        return
    keep = max(len(old) for old, _ in pairs) - 1
    temporary = f"{path}.restore"
    with open(path, "rb") as source, open(temporary, "wb") as target:
        carry = b""
        while chunk := source.read(files.CHUNK):
            data = carry + chunk
            for old, new in pairs:
                data = data.replace(old, new)
            cut = max(0, len(data) - keep)
            target.write(data[:cut])
            carry = data[cut:]
        target.write(carry)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _rename_inside(path: str, old: str, new: str) -> None:
    """Names below ``path`` that contain ``old`` get ``new`` instead (deepest first)."""
    for directory, names, file_names in os.walk(path, topdown=False):
        for name in names + file_names:
            if old in name:
                os.rename(os.path.join(directory, name), os.path.join(directory, name.replace(old, new)))


def _ensure_dir(path: str) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)


def _tree(path: str) -> Iterator[str]:
    """``path`` and everything below it, links included but never followed."""
    yield path
    for directory, names, file_names in os.walk(path):
        for name in sorted(names) + sorted(file_names):
            yield os.path.join(directory, name)


def _move(source: str, target: str, trash: str) -> None:
    """``source`` becomes ``target``; an existing ``target`` (file, directory or link) is moved into ``trash``."""
    if os.path.lexists(target):
        os.rename(target, f"{trash}/{uuid.uuid4().hex}")
    os.rename(source, target)


def restore(found: Staged, base: str, chown: Callable[[str], None]) -> None:
    """Move the staged context into the sandbox ``base`` (see the module docstring)."""
    work = f"{base}/work"
    claude = f"{base}/home/.claude"
    trash = f"{found.path}/{TRASH}"
    _ensure_dir(trash)
    transcript_dir = f"{found.path}/{TRANSCRIPT}"
    for top in sorted(os.listdir(transcript_dir)) if os.path.isdir(transcript_dir) else ():
        _ensure_dir(f"{claude}/{top}")
        inner = ["projects", cwd_key(work)] if top == "projects" else [top]
        source = "/".join([transcript_dir, *inner])
        if top == "projects":
            _ensure_dir(f"{claude}/projects/{inner[1]}")
        for name in sorted(os.listdir(source)) if os.path.isdir(source) else ():
            _move(f"{source}/{name}", "/".join([claude, *inner, name]), trash)
    staged_work = f"{found.path}/{WORK}"
    if os.path.isdir(staged_work):
        _move(staged_work, work, trash)
        os.chmod(work, 0o700)
    _move(f"{found.path}/{transcript.BASELINE_FILE}", f"{base}/{transcript.BASELINE_FILE}", trash)
    for top in (claude, work):
        for path in _tree(top):
            chown(path)
