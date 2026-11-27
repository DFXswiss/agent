"""Archives of `workvm_sync` (docs/platform/spec/13-work-vm.md section 3, ADR-059; threat model
`docs/platform/security/work-vm-threat-model.md`, "crafted archive from the VM").

Pure functions over tar files on the runner's disk: no network, no VM, no guest call. Archives are read member by
member in stream mode (`tarfile` `r|`, extended headers capped by `tarsafe.CappedInfo`), file contents are copied in
`CHUNK` pieces, so memory stays bounded whatever the archive holds; nothing is ever extracted and no link is created.

`to_vm` (the runner exported the workspace's `path` with `files/export`):

* `known_keys(previous)` are the keys of the last mirror's files (`known_key(path, sha256)`, the guest's
  `files.known_key`): the runner passes them to `files/export` as `known`, so the guest leaves out the content of every
  file the VM already holds unchanged and only the changed files cross the guest link (FIX-02). None when nothing was
  mirrored yet (a fresh VM needs every file).
* `read_export(tar)` checks a guest export: the first member is `.agent-export-manifest.json` (canonical JSON
  `{files:[{path,size,sha256}], skipped:[{path,reason}]}`, plus `omitted:[path]` of an export with `known`), every
  other member a regular file of the manifest with its size and SHA-256, each exactly once, and every manifest file
  present except the omitted ones (an omitted path must be a manifest file and has no member).
* `plan_push(previous, export)` compares it with the manifest of the last mirror (`runner.workvm_mirror.manifest`,
  `{path: sha256}`): `changed` = new or different, `deleted` = mirrored before and gone now, `manifest` = the new
  mirror manifest. A changed path the export omitted (`PushPlan.incomplete`) cannot be pushed from this export; the
  caller exports again without `known`.
* `write_push_tar(export_tar, plan, out, prefix)` writes the **push tar** the VM helper applies
  (`workvm-job apply ROOT`): its first member is always `.workvm-deletions.json`, a regular file holding the canonical
  JSON list of the paths to delete (sorted); then the directories (`0755`) and changed files (`0644`), owner 0,
  modification time = now (so make and ninja rebuild what changed). Every name, including the deletions, is
  `prefix/<path>` (just `<path>` for prefix `""`), relative to the helper's ROOT. A workspace file named like the
  deletions member is an ordinary later member: only the first member is the list.

`from_vm` (the runner exported the workspace's `path` and ran `workvm-job pack ROOT` in the VM):

* the **pack tar** is untrusted (the VM's root is the session). Its first member must be `.workvm-pack.json`, a
  regular file with the JSON object `{files:[path], excluded:[path]}`: the files the helper packed and the entries it
  left out (`.git` directories, git-ignored files, links, special files, protected names); an excluded path ending in
  `/` stands for the whole directory below it. Every member name must be a valid relative path (no leading `/`, no
  empty, `.` or `..` segment, no control character or backslash, valid UTF-8, at most `MAX_PATH` characters, no
  `.git` segment) or the archive is refused; at most `MAX_ENTRIES` members, `MAX_BYTES` of content, extended headers
  of at most `tarsafe.MAX_HEADER`; a path twice, a sparse member or a file below a file refuses it too. Links, hard
  links, devices, FIFOs, members with a protected name (`tarsafe.PROTECTED_SEGMENTS`, the guest's
  `policy.PROTECTED_DIRS | PROTECTED_FILES` without `.git`) and files the manifest does not list are skipped; their
  paths count as excluded.
* `merge_from_vm(workspace_tar, pack_tar, pushed, out)` (a workspace export without `known`) writes one canonical tar
  (PAX, sorted by path segments, regular files `0600` and directories `0700`, owner 0, modification time now) for the
  guest import with
  `replace=true`: the workspace files overlaid by the VM's files. A workspace file the VM does not have is dropped
  only when the last mirror pushed it with the same SHA-256 (`pushed`) and the VM side neither excluded nor skipped
  it; every other workspace file is kept. Where a kept workspace file and a VM entry collide (a file where the other
  side has a directory) the VM wins. Counts: `files`/`bytes` = files added or changed against the workspace,
  `deleted` = workspace files dropped, `skipped` = refused VM members + excluded entries + listed files missing from
  the archive. The result obeys the guest import's limits (`MAX_ENTRIES` members, `MAX_BYTES`).

Errors are `RunnerError` with the closed codes of `ERROR_CODES`: `workvm_sync_invalid` (name `archive_invalid`,
`path_invalid`, `manifest_invalid`, `content_mismatch`, `prefix_invalid`) and `workvm_sync_too_large` (name
`size_exceeded`).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, BinaryIO

from agent_runner.errors import RunnerError
from agent_runner.github import tarsafe

MAX_ENTRIES = 100_000
MAX_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_PATH = 4096
KNOWN_KEY_LENGTH = 32
CHUNK = 1024 * 1024
EXPORT_MANIFEST = ".agent-export-manifest.json"
PACK_MANIFEST = ".workvm-pack.json"
DELETIONS = ".workvm-deletions.json"
PUSH_FILE_MODE = 0o644
PUSH_DIR_MODE = 0o755
MERGE_FILE_MODE = 0o600
MERGE_DIR_MODE = 0o700
INVALID = "workvm_sync_invalid"
TOO_LARGE = "workvm_sync_too_large"
ERROR_CODES = (INVALID, TOO_LARGE)
SKIP_REASONS = frozenset({"symlink", "special", "unreadable"})
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _invalid(reason: str) -> RunnerError:
    return RunnerError(INVALID, reason)


def _too_large() -> RunnerError:
    return RunnerError(TOO_LARGE, "size_exceeded")


@dataclass(frozen=True)
class FileEntry:
    size: int
    sha256: str


@dataclass(frozen=True)
class Export:
    """A checked guest export: its files, the entries the guest skipped and the files whose content it left out."""

    manifest: dict[str, FileEntry]
    skipped: list[dict[str, str]]
    omitted: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PushPlan:
    changed: list[str]
    deleted: list[str]
    manifest: dict[str, str]
    incomplete: list[str] = field(default_factory=list)
    """Changed paths whose content the export left out: this export cannot be pushed."""


@dataclass(frozen=True)
class MergeResult:
    files: int
    bytes: int
    deleted: int
    skipped: int


# -- paths ---------------------------------------------------------------------------------------------------------


def valid_path(path: str) -> bool:
    """A relative path the guest accepts syntactically (`.git` and protected names are checked separately)."""
    if not 1 <= len(path) <= MAX_PATH or path.startswith("/"):
        return False
    if any(ord(char) < 0x20 or char in "\x7f\\" for char in path):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(segment not in _FORBIDDEN_SEGMENTS for segment in path.split("/"))


def _git(path: str) -> bool:
    return any(segment.casefold() == ".git" for segment in path.split("/"))


def _member_path(name: str) -> str | None:
    """The normalised path of a member name (one leading `./` and a trailing `/` removed); None for the root."""
    if name in (".", "./"):
        return None
    path = name.removeprefix("./")
    path = path.removesuffix("/")
    if not valid_path(path) or _git(path):
        raise _invalid("path_invalid")
    return path


def _ancestors(path: str) -> Iterator[str]:
    parts = path.split("/")
    for index in range(1, len(parts)):
        yield "/".join(parts[:index])


def _segments(path: str) -> list[str]:
    return path.split("/")


# -- reading -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Member:
    path: str | None
    kind: str  # "file", "dir" or "other"
    size: int
    offset: int
    reader: IO[bytes] | None


def _guarded[T](call: Callable[[], T]) -> T:
    """`call()` with the errors of `tarfile` mapped to closed codes (an oversize extended header is too large)."""
    try:
        return call()
    except tarsafe.UnsafeArchive:
        raise _too_large() from None
    except (tarfile.TarError, OSError, EOFError):
        raise _invalid("archive_invalid") from None


def _kind(member: tarfile.TarInfo) -> str:
    return "file" if member.isreg() else "dir" if member.isdir() else "other"


def _members(source: BinaryIO, max_entries: int) -> Iterator[_Member]:
    """Every member of an uncompressed tar in archive order, counted and bounded (see the module docstring)."""
    count = 0
    content = 0
    seen: set[str | None] = set()
    with _guarded(lambda: tarfile.open(fileobj=source, mode="r|", tarinfo=tarsafe.CappedInfo)) as archive:
        while (member := _guarded(archive.next)) is not None:
            count += 1
            if count > max_entries:
                raise _too_large()
            path = _member_path(member.name)
            if member.issparse() or path in seen:
                raise _invalid("archive_invalid")
            if path is not None:
                seen.add(path)
            kind = _kind(member)
            if kind != "file":
                yield _Member(path, kind, 0, 0, None)
                continue
            content += member.size
            if content > MAX_BYTES + MAX_MANIFEST_BYTES:
                raise _too_large()
            yield _Member(path, "file", member.size, member.offset_data, archive.extractfile(member))


def _read_exact(reader: IO[bytes], size: int, write: Callable[[bytes], object]) -> str:
    """Copy exactly `size` bytes to `write`; their SHA-256 (a member that ends early is `archive_invalid`)."""
    digest = hashlib.sha256()
    left = size
    while left:
        try:
            data = reader.read(min(CHUNK, left))
        except (tarfile.TarError, OSError, EOFError):
            raise _invalid("archive_invalid") from None
        if not data:
            raise _invalid("archive_invalid")
        digest.update(data)
        write(data)
        left -= len(data)
    return digest.hexdigest()


def _discard(_data: bytes) -> None:
    return None


def _json_member(member: _Member, name: str) -> Any:
    if member.path != name or member.reader is None or member.size > MAX_MANIFEST_BYTES:
        raise _invalid("manifest_invalid")
    raw = io.BytesIO()
    _read_exact(member.reader, member.size, raw.write)
    try:
        return json.loads(raw.getvalue().decode("utf-8"))
    except ValueError:
        raise _invalid("manifest_invalid") from None


def _omitted(data: dict[str, Any], manifest: Mapping[str, FileEntry]) -> frozenset[str]:
    """The `omitted` list of an export with `known`: manifest files, each once (empty without the key)."""
    listed = data.get("omitted", [])
    if not isinstance(listed, list) or len(listed) > MAX_ENTRIES:
        raise _invalid("manifest_invalid")
    omitted = frozenset(item for item in listed if isinstance(item, str) and item in manifest)
    if len(omitted) != len(listed):
        raise _invalid("manifest_invalid")
    return omitted


def _export_manifest(data: Any) -> tuple[dict[str, FileEntry], list[dict[str, str]], frozenset[str]]:
    if not isinstance(data, dict) or set(data) - {"omitted"} != {"files", "skipped"}:
        raise _invalid("manifest_invalid")
    files, skipped = data["files"], data["skipped"]
    if not isinstance(files, list) or not isinstance(skipped, list) or len(files) > MAX_ENTRIES:
        raise _invalid("manifest_invalid")
    manifest: dict[str, FileEntry] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise _invalid("manifest_invalid")
        path, size, sha = item["path"], item["size"], item["sha256"]
        if not isinstance(path, str) or not valid_path(path) or _git(path) or path in manifest:
            raise _invalid("manifest_invalid")
        if type(size) is not int or not 0 <= size <= MAX_BYTES or not isinstance(sha, str):
            raise _invalid("manifest_invalid")
        if not _SHA256_RE.fullmatch(sha):
            raise _invalid("manifest_invalid")
        manifest[path] = FileEntry(size, sha)
    if len(skipped) > MAX_ENTRIES:
        raise _invalid("manifest_invalid")
    for item in skipped:
        if not isinstance(item, dict) or set(item) != {"path", "reason"} or item["reason"] not in SKIP_REASONS:
            raise _invalid("manifest_invalid")
        if not isinstance(item["path"], str):
            raise _invalid("manifest_invalid")
    kept = [{"path": item["path"], "reason": item["reason"]} for item in skipped]
    return manifest, kept, _omitted(data, manifest)


def _scan_export(path: Path | str) -> tuple[Export, dict[str, int]]:
    """The checked export and the data offset of every file in the archive."""
    manifest: dict[str, FileEntry] | None = None
    skipped: list[dict[str, str]] = []
    omitted: frozenset[str] = frozenset()
    offsets: dict[str, int] = {}
    with open(path, "rb") as source:
        for member in _members(source, MAX_ENTRIES + 1):
            if member.kind != "file" or member.reader is None:
                raise _invalid("archive_invalid")
            if manifest is None:
                manifest, skipped, omitted = _export_manifest(_json_member(member, EXPORT_MANIFEST))
                continue
            entry = manifest.get(str(member.path))
            if entry is None or member.path in omitted or member.size != entry.size:
                raise _invalid("content_mismatch")
            if _read_exact(member.reader, member.size, _discard) != entry.sha256:
                raise _invalid("content_mismatch")
            offsets[str(member.path)] = member.offset
    if manifest is None or set(offsets) != set(manifest) - omitted:
        raise _invalid("content_mismatch")
    return Export(manifest, skipped, omitted), offsets


def read_export(tar_path: Path | str) -> Export:
    """Check a guest export (see the module docstring)."""
    export, _offsets = _scan_export(tar_path)
    return export


# -- to_vm ---------------------------------------------------------------------------------------------------------


def known_key(path: str, sha256: str) -> str:
    """The key of a mirrored file for `files/export` `known` (the guest's `files.known_key`)."""
    return hashlib.sha256(f"{path}\0{sha256}".encode()).hexdigest()[:KNOWN_KEY_LENGTH]


def known_keys(previous: Mapping[str, str] | None) -> list[str] | None:
    """The sorted keys of the last mirror's files; None when nothing was mirrored yet."""
    if previous is None:
        return None
    return sorted(known_key(path, sha) for path, sha in previous.items())[:MAX_ENTRIES]


def plan_push(previous: Mapping[str, str] | None, export: Export) -> PushPlan:
    """The files to push and to delete against the manifest of the last mirror (None: nothing mirrored yet)."""
    before = previous or {}
    manifest = {path: entry.sha256 for path, entry in export.manifest.items()}
    changed = sorted(path for path, sha in manifest.items() if before.get(path) != sha)
    deleted = sorted(path for path in before if path not in manifest)
    return PushPlan(changed, deleted, manifest, [path for path in changed if path in export.omitted])


def _check_prefix(prefix: str) -> str:
    if prefix == "":
        return ""
    if not valid_path(prefix) or _git(prefix) or tarsafe.protected(prefix):
        raise _invalid("prefix_invalid")
    return prefix + "/"


def _info(path: str, *, file: bool, size: int, mode: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.type = tarfile.REGTYPE if file else tarfile.DIRTYPE
    info.mode = mode
    info.size = size
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _copy_member(archive: tarfile.TarFile, source: BinaryIO, info: tarfile.TarInfo, offset: int, sha256: str) -> None:
    """Append `info` with the bytes at `offset` of `source`, which must still have the SHA-256 `sha256`."""
    source.seek(offset)
    reader = _Hashing(source, info.size)
    archive.addfile(info, reader)
    if reader.hexdigest() != sha256:
        raise _invalid("content_mismatch")


class _Hashing:
    """Exactly `size` bytes of `reader`, hashed on the way (a source that ends early is `archive_invalid`)."""

    def __init__(self, reader: BinaryIO, size: int) -> None:
        self._reader = reader
        self._left = size
        self._digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        want = self._left if size < 0 else min(size, self._left)
        if not want:
            return b""
        data = b""
        while len(data) < want:
            piece = self._reader.read(want - len(data))
            if not piece:
                raise _invalid("archive_invalid")
            data += piece
        self._digest.update(data)
        self._left -= want
        return data

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def write_push_tar(
    export_tar_path: Path | str,
    plan: PushPlan,
    out_path: Path | str,
    prefix: str,
    *,
    now: Callable[[], float] = time.time,
) -> tuple[int, int]:
    """Write the push tar of `plan` (see the module docstring); the number of files and their bytes."""
    head = _check_prefix(prefix)
    export, offsets = _scan_export(export_tar_path)
    missing = [path for path in plan.changed if path not in export.manifest or path in export.omitted]
    if missing or any(not valid_path(path) or _git(path) for path in plan.deleted):
        raise _invalid("content_mismatch")
    mtime = int(now())
    deletions = _json_bytes(sorted(head + path for path in plan.deleted))
    directories: set[str] = set()
    for path in plan.changed:
        directories.update(_ancestors(head + path))
    if head:
        directories.add(prefix)
    size = 0
    with (
        open(export_tar_path, "rb") as source,
        tarfile.open(out_path, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        archive.addfile(
            _info(DELETIONS, file=True, size=len(deletions), mode=PUSH_FILE_MODE, mtime=mtime), io.BytesIO(deletions)
        )
        for directory in sorted(directories, key=_segments):
            archive.addfile(_info(directory, file=False, size=0, mode=PUSH_DIR_MODE, mtime=mtime))
        for path in plan.changed:
            entry = export.manifest[path]
            info = _info(head + path, file=True, size=entry.size, mode=PUSH_FILE_MODE, mtime=mtime)
            _copy_member(archive, source, info, offsets[path], entry.sha256)
            size += entry.size
    return len(plan.changed), size


# -- from_vm -------------------------------------------------------------------------------------------------------


@dataclass
class _Pack:
    files: dict[str, tuple[int, str, int]] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    excluded: set[str] = field(default_factory=set)
    excluded_dirs: set[str] = field(default_factory=set)
    skipped: int = 0


def _pack_manifest(data: Any) -> tuple[set[str], set[str], set[str]]:
    """The listed files, excluded paths and excluded directories of `.workvm-pack.json`."""
    if not isinstance(data, dict) or set(data) != {"files", "excluded"}:
        raise _invalid("manifest_invalid")
    files, excluded = data["files"], data["excluded"]
    if not isinstance(files, list) or not isinstance(excluded, list):
        raise _invalid("manifest_invalid")
    if len(files) > MAX_ENTRIES or len(excluded) > MAX_ENTRIES:
        raise _too_large()
    listed: set[str] = set()
    for item in files:
        if not isinstance(item, str) or not valid_path(item):
            raise _invalid("manifest_invalid")
        listed.add(item)
    paths: set[str] = set()
    directories: set[str] = set()
    for item in excluded:
        if not isinstance(item, str):
            raise _invalid("manifest_invalid")
        path = item.removesuffix("/")
        if not valid_path(path):
            raise _invalid("manifest_invalid")
        (directories if item.endswith("/") else paths).add(path)
    return listed, paths, directories


def _scan_pack(path: Path | str) -> _Pack:
    pack = _Pack()
    listed: set[str] | None = None
    listed_excluded: set[str] = set()
    with open(path, "rb") as source:
        for member in _members(source, MAX_ENTRIES + 1):
            if listed is None:
                listed, listed_excluded, pack.excluded_dirs = _pack_manifest(_json_member(member, PACK_MANIFEST))
                pack.excluded = set(listed_excluded)
                continue
            if member.path is None:
                continue
            refused = member.kind == "other" or tarsafe.protected(member.path)
            if refused or (member.kind == "file" and member.path not in listed):
                pack.skipped += 1
                pack.excluded.add(member.path)
                continue
            if member.reader is None:
                pack.dirs.add(member.path)
                continue
            sha = _read_exact(member.reader, member.size, _discard)
            pack.files[member.path] = (member.size, sha, member.offset)
    if listed is None:
        raise _invalid("manifest_invalid")
    if sum(size for size, _sha, _offset in pack.files.values()) > MAX_BYTES:
        raise _too_large()
    for item in pack.files:
        if any(parent in pack.files for parent in _ancestors(item)):
            raise _invalid("path_invalid")
    for item in pack.dirs:
        if item in pack.files or any(parent in pack.files for parent in _ancestors(item)):
            raise _invalid("path_invalid")
    missing = listed - set(pack.files)
    pack.skipped += len(listed_excluded) + len(pack.excluded_dirs) + len(missing)
    pack.excluded |= missing
    return pack


def _covered(path: str, pack: _Pack) -> bool:
    """Whether the VM side excluded or skipped `path` (itself or a directory above it)."""
    if path in pack.excluded or path in pack.excluded_dirs:
        return True
    return any(parent in pack.excluded_dirs or parent in pack.excluded for parent in _ancestors(path))


def merge_from_vm(
    workspace_tar_path: Path | str,
    vm_tar_path: Path | str,
    pushed: Mapping[str, str] | None,
    out_path: Path | str,
    *,
    now: Callable[[], float] = time.time,
) -> MergeResult:
    """Merge the VM's pack into the workspace export as one canonical tar (see the module docstring)."""
    workspace, offsets = _scan_export(workspace_tar_path)
    if workspace.omitted:
        raise _invalid("content_mismatch")
    pack = _scan_pack(vm_tar_path)
    mirrored = pushed or {}
    vm_dirs = set(pack.dirs)
    for item in pack.files:
        vm_dirs.update(_ancestors(item))
    deleted = 0
    kept: dict[str, FileEntry] = {}
    for item, entry in workspace.manifest.items():
        if item in pack.files:
            continue
        if mirrored.get(item) == entry.sha256 and not _covered(item, pack):
            deleted += 1
            continue
        collides = item in vm_dirs or any(parent in pack.files for parent in _ancestors(item))
        if collides:
            deleted += 1
            continue
        kept[item] = entry
    directories = set(vm_dirs)
    for item in kept:
        directories.update(_ancestors(item))
    total = sum(entry.size for entry in kept.values()) + sum(size for size, _sha, _offset in pack.files.values())
    if total > MAX_BYTES or len(kept) + len(pack.files) + len(directories) > MAX_ENTRIES:
        raise _too_large()
    changed = [
        item
        for item, (_size, sha, _offset) in pack.files.items()
        if item not in workspace.manifest or workspace.manifest[item].sha256 != sha
    ]
    mtime = int(now())
    order = sorted([*directories, *kept, *pack.files], key=_segments)
    with (
        open(workspace_tar_path, "rb") as workspace_source,
        open(vm_tar_path, "rb") as vm_source,
        tarfile.open(out_path, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for item in order:
            if item in pack.files:
                size, sha, offset = pack.files[item]
                info = _info(item, file=True, size=size, mode=MERGE_FILE_MODE, mtime=mtime)
                _copy_member(archive, vm_source, info, offset, sha)
            elif item in kept:
                entry = kept[item]
                info = _info(item, file=True, size=entry.size, mode=MERGE_FILE_MODE, mtime=mtime)
                _copy_member(archive, workspace_source, info, offsets[item], entry.sha256)
            else:
                archive.addfile(_info(item, file=False, size=0, mode=MERGE_DIR_MODE, mtime=mtime))
    return MergeResult(
        files=len(changed),
        bytes=sum(pack.files[item][0] for item in changed),
        deleted=deleted,
        skipped=pack.skipped,
    )
