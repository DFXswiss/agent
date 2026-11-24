"""The context archive on the runner host (ADR-072 sections 2 and 3; decisions/KOR-33.md).

The guest's export (`POST /v1/sessions/{id}/context/export`) is untrusted data of the session's VM. It is read only
with the rules of `github.tarsafe` (normalised relative paths, sizes and counts checked; nothing is ever extracted on
the host):

- `inspect(path, limits)`: the first member is `context.json` (at most `CONTEXT_JSON_MAX` bytes, `parse_context`);
  every further member lies below `transcript/` or `work/`, and no workspace path has a protected name
  (`tarsafe.protected`). Regular files and directories everywhere; symbolic links only below `work/` and only with
  a relative target that stays inside the workspace (the guest keeps such links, review KOR-33 no. 5). Transcript
  lines are not checked one by one (review no. 13): the archive's integrity is the AEAD of its chunks, the digest
  and the signed manifest, and Claude Code tolerates damaged lines of its own transcripts; the export already cut an
  incomplete last line of every `.jsonl`. The highest `version` field of the transcript lines that parse is kept
  (`Context.max_version`, the version rule of `restore.compatible`); a line that does not parse, a version with
  absurdly many digits or a nesting deeper than the parser takes is ignored for it. The conversation file itself
  (`transcript/projects/<key of cwd>/<claude_session_id>.jsonl`) must hold a complete turn - a `user` and an
  `assistant` record -, otherwise the archive is `context_incomplete`: a checkpoint taken before Claude Code wrote
  its first turn is never sealed, and restoring one could only fail the agent's start (`agent_start_failed`), so a
  restore takes the reported emergency path (`context_invalid`) instead. `Context.content_sha256` is the
  SHA-256 over the transcript and workspace members only (path, kind, executable bit, link target, content) - not
  over `context.json`, whose `created_at` differs in every export - so an unchanged conversation is recognised
  (review no. 11). Anything else is `ArchiveInvalid` with the tarsafe code or `context_invalid`.
- `compress_chunks(source, emit)`: gzip (level 6) of the archive, cut into pieces of exactly `CHUNK_BYTES` (the last
  one shorter) that `emit(index, piece)` receives one after the other; memory stays bounded by two pieces and every
  byte is copied a bounded number of times (review no. 8).
- `Inflater`: the inverse, chunk by chunk into a file, refusing more than `archive_bytes(limits)` of archive - the
  content limits plus `ENTRY_OVERHEAD_BYTES` for each entry the limits allow (tar header, PAX records of a long path
  and link target, padding; review no. 21) - and a stream that is not complete gzip (`context_invalid`).

Every function here is synchronous and CPU or disk bound; the callers run them in a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import IO, Any, BinaryIO

from agent_runner.crypto.context import CHUNK_BYTES
from agent_runner.github import tarsafe

CONTEXT_JSON = "context.json"
CONTEXT_JSON_MAX = 1024 * 1024
TRANSCRIPT = "transcript"
WORK = "work"
GZIP_LEVEL = 6
GZIP_WBITS = 31
READ_BYTES = 1024 * 1024
MAX_ENTRIES = 200_000
BLOCK = 512
ENTRY_OVERHEAD_BYTES = 3 * BLOCK + 2 * 4096
"""Archive bytes one entry may need besides its content: its header, a PAX header with a path and a link target of
up to 4096 bytes each, and the padding of its content."""
AGENT_KIND = "claude-code"
WORKSPACE_STATES = frozenset({"included", "omitted_too_large", "empty"})
OPTIONAL_KEYS = frozenset({"workspace_omitted"})
"""Keys a guest before review KOR-33 did not write (its export left out links, empty directories and protected
names without counting them)."""
CONTEXT_KEYS = frozenset(
    {
        "v",
        "agent_kind",
        "agent_version",
        "claude_session_id",
        "session_id",
        "task_id",
        "cwd",
        "created_at",
        "transcript_files",
        "workspace",
        "workspace_files",
        "workspace_bytes",
    }
)
TURN_KINDS = frozenset({"user", "assistant"})
"""The record types of a complete turn (`inspect`)."""
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KEY_RE = re.compile(r"[^A-Za-z0-9]")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class ArchiveInvalid(Exception):
    """A refused archive; `code` is a tarsafe code or `context_invalid`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Context:
    """The checked `context.json` of an archive."""

    agent_kind: str
    agent_version: str
    claude_session_id: str
    session_id: str
    task_id: str
    cwd: str
    created_at: str
    workspace: str
    workspace_files: int
    workspace_bytes: int
    workspace_omitted: int = 0
    """Workspace entries the export left out (protected names, links leaving the workspace, devices, unreadable)."""
    max_version: tuple[int, ...] | None = None
    """The highest Claude Code `version` written into the transcript lines (`inspect`), None when there is none."""
    content_sha256: str = ""
    """SHA-256 of the transcript and workspace members (`inspect`, see the module docstring)."""

    @property
    def workspace_state(self) -> str:
        """`full`, `partial`, `omitted` or `empty` (audit `session.context_restored`, the restored sentence)."""
        if self.workspace == "omitted_too_large":
            return "omitted"
        if self.workspace_omitted:
            return "partial"
        return "full" if self.workspace == "included" else "empty"


def limits_for(transcript_bytes: int, workspace_bytes: int) -> tarsafe.Limits:
    """The tarsafe limits (content bytes and entries) of an archive under the runner's transcript and workspace
    limits."""
    return tarsafe.Limits(max_bytes=transcript_bytes + workspace_bytes + CONTEXT_JSON_MAX, max_entries=MAX_ENTRIES)


def archive_bytes(limits: tarsafe.Limits) -> int:
    """The largest archive (headers included) that `limits` allow (see the module docstring)."""
    return limits.max_bytes + (limits.max_entries + 2) * ENTRY_OVERHEAD_BYTES


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
        raise ArchiveInvalid("context_invalid")
    return value


def _text(value: object, pattern: re.Pattern[str] | None = None, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        raise ArchiveInvalid("context_invalid")
    if pattern is not None and not pattern.fullmatch(value):
        raise ArchiveInvalid("context_invalid")
    return value


def parse_context(raw: bytes) -> Context:
    """The checked `context.json` (exact key set, version 1, formats); `context_invalid` otherwise."""
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveInvalid("context_invalid") from None
    if not isinstance(data, dict) or not CONTEXT_KEYS <= set(data) <= CONTEXT_KEYS | OPTIONAL_KEYS:
        raise ArchiveInvalid("context_invalid")
    if data["v"] != 1 or isinstance(data["v"], bool):
        raise ArchiveInvalid("context_invalid")
    files = data["transcript_files"]
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ArchiveInvalid("context_invalid")
    workspace = data["workspace"]
    if workspace not in WORKSPACE_STATES:
        raise ArchiveInvalid("context_invalid")
    return Context(
        agent_kind=_text(data["agent_kind"], maximum=64),
        agent_version=_text(data["agent_version"], _VERSION_RE),
        claude_session_id=_text(data["claude_session_id"], _UUID_RE),
        session_id=_text(data["session_id"], _UUID_RE),
        task_id=_text(data["task_id"], _UUID_RE),
        cwd=_text(data["cwd"]),
        created_at=_text(data["created_at"], _TIME_RE),
        workspace=workspace,
        workspace_files=_count(data["workspace_files"]),
        workspace_bytes=_count(data["workspace_bytes"]),
        workspace_omitted=_count(data.get("workspace_omitted", 0)),
    )


def _read_exact(reader: IO[bytes] | None, size: int) -> bytes:
    parts: list[bytes] = []
    assert reader is not None
    tarsafe.copy_exact(reader, size, parts.append)
    return b"".join(parts)


_VERSION_KEY_RE = re.compile(r"\d{1,9}(?!\d)(?:\.\d{1,9}(?!\d)){0,7}")


def version_key(text: object) -> tuple[int, ...] | None:
    """`(2, 1, 330)` of a version text that starts with dotted numbers (at most nine digits each); None otherwise."""
    if not isinstance(text, str):
        return None
    found = _VERSION_KEY_RE.match(text)
    return None if found is None else tuple(int(part) for part in found.group(0).split("."))


def cwd_key(path: str) -> str:
    """Claude Code's project directory name of the working directory `path` (`agent_guest.transcript.cwd_key`)."""
    return _KEY_RE.sub("-", path)


def _line_facts(line: bytes) -> tuple[tuple[int, ...] | None, str | None]:
    """The `version` and the record `type` of one transcript line; `(None, None)` for a line that does not parse
    (see the module docstring)."""
    try:
        value: Any = json.loads(line)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    kind = value.get("type")
    return version_key(value.get("version")), kind if isinstance(kind, str) else None


class _Lines:
    """Splits a stream into lines; a line's pieces are joined once, at its end (linear in the line's length)."""

    def __init__(self, take: Callable[[bytes], None]) -> None:
        self._take = take
        self._pending: list[bytes] = []

    def feed(self, data: bytes) -> None:
        start = 0
        while (end := data.find(b"\n", start)) >= 0:
            self._pending.append(data[start:end])
            self._take(b"".join(self._pending))
            self._pending = []
            start = end + 1
        if start < len(data):
            self._pending.append(data[start:])

    def finish(self) -> None:
        if self._pending:
            self._take(b"".join(self._pending))
            self._pending = []


def transcript_version(
    reader: IO[bytes] | None, size: int, digest: Any, kinds: set[str] | None = None
) -> tuple[int, ...] | None:
    """The highest version the lines of one transcript file name (see the module docstring); the content goes into
    `digest` as well, and the turn record types found (`TURN_KINDS`) into `kinds`."""
    highest: tuple[int, ...] | None = None

    def take(line: bytes) -> None:
        nonlocal highest
        found, kind = _line_facts(line)
        if found is not None and (highest is None or found > highest):
            highest = found
        if kinds is not None and kind in TURN_KINDS:
            kinds.add(kind)

    lines = _Lines(take)

    def feed(data: bytes) -> None:
        digest.update(data)
        lines.feed(data)

    assert reader is not None
    tarsafe.copy_exact(reader, size, feed)
    lines.finish()
    return highest


def _link_ok(path: str, target: str | None) -> bool:
    """A link below `work/` whose relative target stays inside the workspace."""
    if not target or target.startswith("/") or len(target) > 4096:
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved == WORK or resolved.startswith(f"{WORK}/")


def _member_head(entry: tarsafe.Entry) -> bytes:
    executable = "x" if entry.mode & 0o111 else "-"
    target = entry.target or ""
    return f"{entry.kind}\0{entry.path}\0{executable}\0{entry.size}\0{target}\0".encode("utf-8", "surrogateescape")


def inspect(source: BinaryIO, limits: tarsafe.Limits) -> Context:
    """Check the whole archive (see the module docstring) and return its `context.json`."""
    found: Context | None = None
    highest: tuple[int, ...] | None = None
    digest = hashlib.sha256()
    conversation = ""
    kinds: set[str] = set()
    try:
        for entry in tarsafe.entries(source, limits=limits, links="symlinks"):
            if found is None:
                if entry.path != CONTEXT_JSON or entry.kind != "file" or entry.size > CONTEXT_JSON_MAX:
                    raise ArchiveInvalid("context_invalid")
                found = parse_context(_read_exact(entry.reader, entry.size))
                conversation = f"{TRANSCRIPT}/projects/{cwd_key(found.cwd)}/{found.claude_session_id}.jsonl"
                continue
            top, _, rest = entry.path.partition("/")
            if top not in (TRANSCRIPT, WORK) or (not rest and entry.kind != "dir"):
                raise ArchiveInvalid("context_invalid")
            if top == WORK and rest and tarsafe.protected(rest):
                raise ArchiveInvalid("path_invalid")
            if entry.kind == "symlink" and (top != WORK or not _link_ok(entry.path, entry.target)):
                raise ArchiveInvalid("path_invalid")
            digest.update(_member_head(entry))
            if entry.kind != "file":
                continue
            if top == TRANSCRIPT and entry.path.endswith(".jsonl"):
                turn = kinds if entry.path == conversation else None
                version = transcript_version(entry.reader, entry.size, digest, turn)
                if version is not None and (highest is None or version > highest):
                    highest = version
            else:
                assert entry.reader is not None
                tarsafe.copy_exact(entry.reader, entry.size, digest.update)
    except tarsafe.UnsafeArchive as refused:
        raise ArchiveInvalid(refused.code) from None
    if found is None or kinds != TURN_KINDS:
        raise ArchiveInvalid("context_invalid" if found is None else "context_incomplete")
    return replace(found, max_version=highest, content_sha256=digest.hexdigest())


def compress_chunks(source: BinaryIO, emit: Callable[[int, bytes], None]) -> int:
    """gzip `source` and hand it on in pieces of `CHUNK_BYTES`; returns the number of pieces (at least one)."""
    compressor = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, GZIP_WBITS)
    parts: list[bytes] = []
    size = 0
    index = 0

    def push(data: bytes, final: bool) -> None:
        nonlocal size, index, parts
        if data:
            parts.append(data)
            size += len(data)
        # the last piece is emitted by the caller, so a stream that ends on a piece boundary keeps a full last piece
        while size > CHUNK_BYTES or (size == CHUNK_BYTES and not final):
            joined = b"".join(parts)
            emit(index, joined[:CHUNK_BYTES])
            index += 1
            rest = joined[CHUNK_BYTES:]
            parts = [rest] if rest else []
            size = len(rest)

    while data := source.read(READ_BYTES):
        push(compressor.compress(data), final=False)
    push(compressor.flush(), final=True)
    emit(index, b"".join(parts))
    return index + 1


class Inflater:
    """Decompresses consecutive gzip pieces into `target`; at most `max_bytes` of archive (`archive_bytes`)."""

    def __init__(self, target: BinaryIO, max_bytes: int) -> None:
        self.target = target
        self.max_bytes = max_bytes
        self.size = 0
        self._decompressor = zlib.decompressobj(GZIP_WBITS)

    def feed(self, piece: bytes) -> None:
        if self._decompressor.eof:
            raise ArchiveInvalid("context_invalid")
        data = piece
        while data:
            try:
                out = self._decompressor.decompress(data, READ_BYTES)
            except zlib.error:
                raise ArchiveInvalid("context_invalid") from None
            self.size += len(out)
            if self.size > self.max_bytes:
                raise ArchiveInvalid("size_exceeded")
            self.target.write(out)
            if self._decompressor.unused_data:
                raise ArchiveInvalid("context_invalid")
            data = self._decompressor.unconsumed_tail

    def finish(self) -> int:
        """The archive size once the gzip stream ended exactly with the last piece."""
        if not self._decompressor.eof:
            raise ArchiveInvalid("context_invalid")
        return self.size
