"""Scratch directories, guest access and the workspace snapshot of `github.commit` (05-runner.md section 12 step 3
and 12.1; 03-api.md section 9; decisions CTR-02 no. 8).

Scratch: every operation that needs files works in `<runner home>/tmp/op-<operation id>` (0700; `tmp` is emptied at
every daemon start). The executor removes its directory when it ends, except after a retryable failure (the next
attempt uses it). A commit pre-check deletes the export once it is read and keeps only the blobs it will upload
(`trim`, at most 50 MiB), because operations the hub refuses or finds duplicate never execute; `sweep` removes
directories older than `SCRATCH_MAX_AGE_S` (2 h) and runs before every new snapshot or checkout.

Guest access: the VM of the operation's session (`runner.vm`) through the session area's client factory; a pre-check
reaches it through the pipeline of the daemon (`operations.pipeline.existing`).

Snapshot (`take`): **one** `files/export` of `repos/<owner>/<name>` (with the commit's `paths`) streamed into
`export.tar` (at most `MAX_EXPORT_BYTES`), then read as an untrusted archive (the VM is untrusted, 00-overview.md
section 3 B4) with `tarsafe.entries` (regular files only): the first member must be the manifest whose SHA-256 is the
`X-Manifest-Sha256` header; every other member must be a file of the manifest with its size and SHA-256, each exactly
once, and every manifest file must be present. Contents are stored by git blob SHA in `blobs/` - the only copy the
commit uses later. Any deviation is `SnapshotInvalid`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, BinaryIO

from agent_runner.github import gitobj, tarsafe
from agent_runner.guestlink.client import GuestClient
from agent_runner.guestlink.errors import GuestError, GuestRefused, GuestUnreachable
from agent_runner.operations.pipeline import existing
from agent_runner.sessions.records import LocalSession
from agent_runner.vm import records as vm_records

SCRATCH_MAX_AGE_S = 2 * 3600
"""Longer than any operation can wait for its execution (10 min unregistered, 30 min accepted, retries)."""
MAX_EXPORT_BYTES = 1024 * 1024 * 1024
"""The export stream: at most 512 MiB of files (guest limit) plus headers and the manifest."""
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_FILES = 100_000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MANIFEST = ".agent-export-manifest.json"
SKIP_REASONS = frozenset({"symlink", "special", "unreadable"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SnapshotInvalid(Exception):
    """The export did not match its manifest or was not a plain archive of regular files."""


class TooLarge(Exception):
    """A download or export exceeded its limit."""


@dataclass(frozen=True)
class SnapFile:
    size: int
    blob: str


@dataclass(frozen=True)
class Snapshot:
    files: dict[str, SnapFile]
    skipped: list[dict[str, str]]


# -- scratch -------------------------------------------------------------------------------------------------------


def scratch_root(context: Any) -> Path:
    return Path(context.config.tmp_dir)


def scratch_dir(context: Any, operation_id: str) -> Path:
    return scratch_root(context) / f"op-{operation_id}"


def fresh(context: Any, operation_id: str) -> Path:
    """An empty scratch directory of the operation (a previous attempt's content is removed)."""
    path = scratch_dir(context, operation_id)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(mode=0o700, parents=True)
    return path


def remove(context: Any, operation_id: str) -> None:
    shutil.rmtree(scratch_dir(context, operation_id), ignore_errors=True)


def sweep(context: Any, *, now: Callable[[], float] = time.time) -> int:
    """Remove scratch directories older than `SCRATCH_MAX_AGE_S`; the number removed."""
    root = scratch_root(context)
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob("op-*"):
        try:
            old = now() - path.lstat().st_mtime > SCRATCH_MAX_AGE_S
        except FileNotFoundError:
            continue
        if old:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


class Bounded:
    """A `write` callable that refuses more than `limit` bytes in total (`TooLarge`)."""

    def __init__(self, out: BinaryIO, limit: int) -> None:
        self.out = out
        self.limit = limit
        self.total = 0

    def write(self, data: bytes) -> None:
        self.total += len(data)
        if self.total > self.limit:
            raise TooLarge
        self.out.write(data)


# -- guest ---------------------------------------------------------------------------------------------------------


class NoGuest(Exception):
    """The session has no VM on this device (any more)."""


async def guest_for(env: Any, session: LocalSession) -> GuestClient:
    """A control client of the session's VM (close it after use)."""
    row = None if session.vm_id is None else await vm_records.get(env.db, session.vm_id)
    if row is None:
        raise NoGuest
    client: GuestClient = await env.client(row)
    return client


def env_of(context: Any) -> Any:
    """The session environment of the daemon's operation pipeline (for pre-checks)."""
    pipeline = existing(context)
    if pipeline is None:
        raise NoGuest
    return pipeline.env


# -- snapshot ------------------------------------------------------------------------------------------------------


async def take(
    guest: GuestClient, session_id: str, directory: str, paths: Sequence[str] | None, scratch: Path
) -> Snapshot:
    """One export of `directory` (or `paths` below it) into `scratch`, checked and stored by blob SHA."""
    target = scratch / "export.tar"
    with target.open("wb") as out:
        digest = await guest.export_files(session_id, directory, Bounded(out, MAX_EXPORT_BYTES).write, paths=paths)
    try:
        return await asyncio.to_thread(read_export, target, digest, scratch / "blobs")
    finally:
        target.unlink()


def trim(scratch: Path, keep: set[str | None]) -> None:
    """Remove every stored blob the commit does not upload (the scratch of an operation that never executes then
    holds at most the changed files, at most 50 MiB)."""
    for path in (scratch / "blobs").iterdir():
        if path.name not in keep:
            path.unlink()


def _manifest(raw: bytes, digest: str) -> tuple[dict[str, tuple[int, str]], list[dict[str, str]]]:
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SnapshotInvalid("manifest digest")
    try:
        data = json.loads(raw)
    except ValueError:
        raise SnapshotInvalid("manifest json") from None
    if not isinstance(data, dict) or set(data) != {"files", "skipped"}:
        raise SnapshotInvalid("manifest shape")
    files, skipped = data["files"], data["skipped"]
    if not isinstance(files, list) or not isinstance(skipped, list) or len(files) > MAX_FILES:
        raise SnapshotInvalid("manifest shape")
    expected: dict[str, tuple[int, str]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise SnapshotInvalid("manifest file")
        path, size, sha = item["path"], item["size"], item["sha256"]
        valid_size = type(size) is int and 0 <= size <= MAX_FILE_BYTES
        if not isinstance(path, str) or not valid_size or not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            raise SnapshotInvalid("manifest file")
        if path in expected:
            raise SnapshotInvalid("manifest duplicate")
        expected[path] = (size, sha)
    for item in skipped:
        if not isinstance(item, dict) or set(item) != {"path", "reason"} or item["reason"] not in SKIP_REASONS:
            raise SnapshotInvalid("manifest skip")
        if not isinstance(item["path"], str):
            raise SnapshotInvalid("manifest skip")
    return expected, skipped


def _store(reader: IO[bytes], size: int, blobs: Path) -> tuple[str, str]:
    """Copy one member into `blobs/`; its SHA-256 and git blob SHA."""
    temp = blobs / f".incoming-{os.getpid()}"
    sha256 = hashlib.sha256()
    git = hashlib.sha1(f"blob {size}\0".encode("ascii"), usedforsecurity=False)

    with temp.open("wb") as out:

        def write(data: bytes) -> None:
            sha256.update(data)
            git.update(data)
            out.write(data)

        tarsafe.copy_exact(reader, size, write)
    blob = git.hexdigest()
    temp.replace(blobs / blob)
    return sha256.hexdigest(), blob


def read_export(path: Path, digest: str, blobs: Path) -> Snapshot:
    """Check the export at `path` against its manifest and store the contents (see the module docstring)."""
    blobs.mkdir(mode=0o700, exist_ok=True)
    limits = tarsafe.Limits(max_bytes=MAX_EXPORT_BYTES, max_entries=MAX_FILES + 1)
    expected: dict[str, tuple[int, str]] | None = None
    skipped: list[dict[str, str]] = []
    files: dict[str, SnapFile] = {}
    try:
        with path.open("rb") as source:
            for entry in tarsafe.entries(source, limits=limits, links="refuse"):
                if entry.reader is None:
                    raise SnapshotInvalid("directory member")
                if expected is None:
                    if entry.path != MANIFEST or entry.size > MAX_MANIFEST_BYTES:
                        raise SnapshotInvalid("manifest first")
                    expected, skipped = _manifest(entry.reader.read(entry.size), digest)
                    continue
                want = expected.get(entry.path)
                if want is None or entry.path in files or entry.size != want[0]:
                    raise SnapshotInvalid("member")
                sha256, blob = _store(entry.reader, entry.size, blobs)
                if sha256 != want[1]:
                    raise SnapshotInvalid("content")
                files[entry.path] = SnapFile(entry.size, blob)
    except tarsafe.UnsafeArchive:
        raise SnapshotInvalid("archive") from None
    if expected is None or set(files) != set(expected):
        raise SnapshotInvalid("incomplete")
    return Snapshot(files, skipped)


def blob_path(scratch: Path, blob: str) -> Path:
    return scratch / "blobs" / blob


def read_blob(scratch: Path, blob: str) -> bytes:
    """The content of a stored blob; a missing or altered one is `SnapshotInvalid`."""
    try:
        data = blob_path(scratch, blob).read_bytes()
    except FileNotFoundError:
        raise SnapshotInvalid("blob missing") from None
    if gitobj.blob_sha(data) != blob:
        raise SnapshotInvalid("blob altered")
    return data


GUEST_CODES = {"size_exceeded": "size_exceeded", "path_invalid": "path_invalid", "not_found": "not_found"}


def guest_code(error: GuestError) -> tuple[str, bool]:
    """The operation code of a failed control call and whether a retry may succeed: an unreachable guest is a
    retryable `upstream_error`; the guest's `size_exceeded`, `path_invalid` and `not_found` keep their code, a session
    that ended (`state_conflict`) is `not_found`; anything else is `upstream_error`."""
    if isinstance(error, GuestUnreachable):
        return "upstream_error", True
    if isinstance(error, GuestRefused):
        if error.error == "state_conflict":
            return "not_found", False
        return GUEST_CODES.get(error.error, "upstream_error"), False
    return "upstream_error", False
