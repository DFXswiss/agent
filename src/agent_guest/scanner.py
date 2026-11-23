"""Workspace scanner of the control process (05-runner.md sections 5.5 and 6.4; 03-api.md section 7.2 ``session.file``).

The control process (root) reads the session's workspace ``/srv/sessions/<id>/work``. The session owns that tree, so
every access goes through directory file descriptors opened with ``O_NOFOLLOW`` from the root-owned session base
(``open_dir``, ``open_file``): a symlink or a swapped directory never leads root code outside the workspace, and
only regular files owned by the session user are read. Symlinks, devices, FIFOs and ``.git`` segments (not allowed in
a ``rel_path``) are skipped.

``WorkspaceScanner``:

* ``start()`` - loads the index file ``<base>/capture-index.json`` (root 0600, written after every change, so a
  restart of the control process reports what changed meanwhile) or, without one, takes the current tree as the
  baseline without reporting it (files the runner imported before the agent started are not session changes)
* ``scan()`` - every ``SCAN_SECONDS``: compares mtime and size with the index; a changed file of at most
  ``HASH_LIMIT`` bytes is hashed (SHA-256) and reported when its content differs; larger files are neither hashed
  nor reported (05 section 6.4). New files are ``write``, changed ones ``edit``, vanished ones ``delete``.
  Deletions are only reported after a complete walk (at most ``MAX_ENTRIES`` entries, ``MAX_DEPTH`` levels).
* ``check(path, op=None)`` - one workspace-relative path now (PostToolUse of ``Write``/``Edit`` with its op, or a file
  hint); always hashes (up to ``HASH_LIMIT``), so a change that kept mtime and size is still found.

The known state changes only together with the journal records: a failed journal write restores it, so nothing is
lost for good. Names that are no UTF-8 are skipped (they could not be journaled).

A record is ``session.file {path, op, sha256, size, patch?}`` and only written for a change not reported before. The
patch is a unified diff against the last known content when both versions are UTF-8 text of at most
``TEXT_CACHE_FILE`` bytes (a new file is diffed against nothing) and the diff has at most ``PATCH_LIMIT`` bytes; the
known contents of one session are kept in memory up to ``TEXT_CACHE_TOTAL`` bytes.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from agent_guest.journal import Journal

SCAN_SECONDS = 5.0
HASH_LIMIT = 10 * 1024 * 1024
PATCH_LIMIT = 64 * 1024
TEXT_CACHE_FILE = 256 * 1024
TEXT_CACHE_TOTAL = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_DEPTH = 64
MAX_REL_PATH = 4096
CHUNK = 1024 * 1024
INDEX_FILE = "capture-index.json"
WORK = "work"
SKIP_SEGMENTS = frozenset({".git"})
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
OPS = frozenset({"write", "edit"})


def open_dir(base: str, parts: Sequence[str]) -> int:
    """Descriptor of ``base/parts...``; no component may be a symlink (``OSError`` otherwise)."""
    fd = os.open(base, DIR_FLAGS)
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part:
                raise OSError(f"invalid path segment {part!r}")
            following = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = following
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_file(dir_fd: int, name: str, uid: int) -> tuple[int, os.stat_result]:
    """Descriptor and status of the regular file ``name`` in ``dir_fd`` owned by ``uid`` (``OSError`` otherwise)."""
    fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    status = os.fstat(fd)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != uid:
        os.close(fd)
        raise OSError(f"not a regular file of the session: {name!r}")
    return fd, status


def read_file(fd: int, keep: int) -> tuple[str, int, bytes | None]:
    """SHA-256, size and - for files of at most ``keep`` bytes - the content of the open file."""
    digest = hashlib.sha256()
    size = 0
    content: list[bytes] = []
    while chunk := os.read(fd, CHUNK):
        digest.update(chunk)
        size += len(chunk)
        if size <= keep:
            content.append(chunk)
    return digest.hexdigest(), size, b"".join(content) if size <= keep else None


def valid_rel(rel: str) -> bool:
    """A path the ``rel_path`` contract accepts: no control characters, no ``.git``/``..`` segment, bounded."""
    if not rel or len(rel) > MAX_REL_PATH or any(ord(char) < 0x20 or ord(char) == 0x7F for char in rel):
        return False
    try:
        # A name that is no UTF-8 (listdir gives lone surrogates) could not be journaled.
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    segments = rel.split("/")
    return all(segment not in ("", ".", "..") and segment not in SKIP_SEGMENTS for segment in segments)


def make_patch(rel: str, old: bytes | None, new: bytes | None, created: bool = False) -> str | None:
    """Unified diff of two UTF-8 texts (``created``: a new file, ``old`` ``b""``); ``None`` if unknown, binary or
    too long."""
    if old is None or new is None:
        return None
    try:
        before, after = old.decode("utf-8").splitlines(keepends=True), new.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    source = "/dev/null" if created else f"a/{rel}"
    patch = "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(before, after, source, f"b/{rel}")
    )
    return patch if len(patch.encode("utf-8")) <= PATCH_LIMIT else None


@dataclass(frozen=True)
class Known:
    mtime_ns: int
    size: int
    sha256: str


class WorkspaceScanner:
    """Reports the file changes of one session's workspace; thread-safe."""

    def __init__(self, journal: Journal, session_id: str, base: str, uid: int) -> None:
        self.journal = journal
        self.session_id = session_id
        self.base = base
        self.uid = uid
        self.known: dict[str, Known] = {}
        self._texts: dict[str, bytes] = {}
        self._text_bytes = 0
        self._lock = threading.Lock()

    @property
    def index_file(self) -> str:
        return f"{self.base}/{INDEX_FILE}"

    # -- index ------------------------------------------------------------------------------------------------

    def _load_index(self) -> bool:
        try:
            with open(self.index_file, encoding="utf-8") as handle:
                value: Any = json.load(handle)
            known = {rel: Known(int(item[0]), int(item[1]), str(item[2])) for rel, item in value.items()}
        except (OSError, ValueError, TypeError, AttributeError, IndexError):
            return False
        self.known = known
        return True

    def _save_index(self) -> None:
        value = {rel: [item.mtime_ns, item.size, item.sha256] for rel, item in sorted(self.known.items())}
        descriptor, temporary = tempfile.mkstemp(dir=self.base, prefix=".capture-index-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"))
            os.replace(temporary, self.index_file)
        except BaseException:
            os.unlink(temporary)
            raise

    # -- walking ------------------------------------------------------------------------------------------------

    def _walk(self, visit: Callable[[int, str, os.stat_result], None]) -> bool:
        """Calls ``visit(dir_fd, rel, status)`` for every regular file; ``False`` if the walk was cut short."""
        try:
            root = open_dir(self.base, [WORK])
        except OSError:
            return False
        count = [0]
        try:
            return self._walk_dir(root, "", 0, visit, count)
        finally:
            os.close(root)

    def _walk_dir(
        self, fd: int, prefix: str, depth: int, visit: Callable[[int, str, os.stat_result], None], count: list[int]
    ) -> bool:
        complete = True
        for name in sorted(os.listdir(fd)):
            count[0] += 1
            if count[0] > MAX_ENTRIES:
                return False
            rel = f"{prefix}{name}"
            if name in SKIP_SEGMENTS or not valid_rel(rel):
                continue
            try:
                status = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                complete = False
                continue
            if stat.S_ISREG(status.st_mode) and status.st_uid == self.uid:
                visit(fd, rel, status)
            elif stat.S_ISDIR(status.st_mode):
                if depth + 1 >= MAX_DEPTH:
                    complete = False
                    continue
                try:
                    child = os.open(name, DIR_FLAGS, dir_fd=fd)
                except OSError:
                    complete = False
                    continue
                try:
                    complete = self._walk_dir(child, f"{rel}/", depth + 1, visit, count) and complete
                finally:
                    os.close(child)
                if count[0] > MAX_ENTRIES:
                    return False
        return complete

    # -- public -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Index file or baseline (nothing reported)."""
        with self._lock:
            if self._load_index():
                return
            found: dict[str, Known] = {}

            def visit(fd: int, rel: str, status: os.stat_result) -> None:
                hashed = self._hash(fd, rel)
                if hashed is not None:
                    found[rel] = Known(hashed[1].st_mtime_ns, hashed[1].st_size, hashed[0])
                    self._remember(rel, hashed[2])

            self._walk(visit)
            self.known = found
            self._save_index()

    def scan(self) -> int:
        """One pass; returns the number of records written."""
        with self._lock:
            seen: set[str] = set()
            records: list[dict[str, Any]] = []

            def visit(fd: int, rel: str, status: os.stat_result) -> None:
                seen.add(rel)
                known = self.known.get(rel)
                if known is not None and (known.mtime_ns, known.size) == (status.st_mtime_ns, status.st_size):
                    return
                record = self._update(fd, rel, None)
                if record is not None:
                    records.append(record)

            with self._staged():
                complete = self._walk(visit)
                if complete:
                    for rel in sorted(set(self.known) - seen):
                        records.append(self._forget(rel))
                self._report(records)
            return len(records)

    def check(self, rel: str, op: str | None = None) -> bool:
        """Report ``rel`` now if its content changed; ``op`` (``write``/``edit``) as given by the hook."""
        if not valid_rel(rel) or (op is not None and op not in OPS):
            return False
        parent, _, name = rel.rpartition("/")
        with self._lock, self._staged():
            record, exists = None, False
            try:
                fd = open_dir(self.base, [WORK, *parent.split("/")] if parent else [WORK])
            except OSError:
                fd = -1
            if fd >= 0:
                try:
                    record = self._update(fd, rel, op)
                    exists = record is not None or _exists(fd, name)
                finally:
                    os.close(fd)
            if record is None and rel in self.known and not exists:
                record = self._forget(rel)
            if record is None:
                return False
            self._report([record])
            return True

    # -- helpers ------------------------------------------------------------------------------------------------

    def _hash(self, dir_fd: int, rel: str) -> tuple[str, os.stat_result, bytes | None] | None:
        try:
            fd, status = open_file(dir_fd, rel.rpartition("/")[2], self.uid)
        except OSError:
            return None
        if status.st_size > HASH_LIMIT:
            # 05 section 6.4: only files of at most 10 MiB are hashed (and reported).
            os.close(fd)
            return None
        try:
            digest, _, content = read_file(fd, TEXT_CACHE_FILE)
            # The size actually read can differ from the earlier status while the file is written: keep the status
            # after the read, so the next pass sees a later change.
            status = os.fstat(fd)
        finally:
            os.close(fd)
        return digest, status, content

    def _update(self, dir_fd: int, rel: str, op: str | None) -> dict[str, Any] | None:
        hashed = self._hash(dir_fd, rel)
        if hashed is None:
            return None
        digest, status, content = hashed
        known = self.known.get(rel)
        self.known[rel] = Known(status.st_mtime_ns, status.st_size, digest)
        if known is not None and known.sha256 == digest:
            return None
        old = b"" if known is None else self._texts.get(rel)
        record: dict[str, Any] = {
            "path": rel,
            "op": op or ("write" if known is None else "edit"),
            "sha256": digest,
            "size": status.st_size,
        }
        patch = make_patch(rel, old, content, created=known is None)
        if patch is not None:
            record["patch"] = patch
        self._remember(rel, content)
        return record

    def _forget(self, rel: str) -> dict[str, Any]:
        del self.known[rel]
        self._remember(rel, None)
        return {"path": rel, "op": "delete", "sha256": None, "size": None}

    def _remember(self, rel: str, content: bytes | None) -> None:
        previous = self._texts.pop(rel, None)
        if previous is not None:
            self._text_bytes -= len(previous)
        if content is not None and self._text_bytes + len(content) <= TEXT_CACHE_TOTAL:
            self._texts[rel] = content
            self._text_bytes += len(content)

    @contextmanager
    def _staged(self) -> Iterator[None]:
        """Changes of the known state count only if their records were journaled: a failed write restores them, so
        the next pass reports the changes again."""
        known, texts, text_bytes = dict(self.known), dict(self._texts), self._text_bytes
        try:
            yield
        except BaseException:
            self.known, self._texts, self._text_bytes = known, texts, text_bytes
            raise

    def _report(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self.journal.transaction() as db:
            for record in records:
                self.journal.insert(db, "session.file", record, self.session_id)
        self._save_index()


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return True
