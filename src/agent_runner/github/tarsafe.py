"""Safe reading of tar archives on the runner host (05-runner.md section 12.1 `github.checkout`, 03-api.md section 9;
ATK-40). Nothing is ever extracted with `tarfile.extract*`: members are read one by one in stream mode and only
regular files and directories are passed on.

`entries(source, limits=..., links=..., compressed=...)` yields `Entry(path, kind, size, reader)` for every regular
file (`kind="file"`) and directory (`kind="dir"`), in archive order:

- the path must be relative (`ops/rules.json#/path`): no leading `/`, no control character or backslash, no empty,
  `.` or `..` segment and no `.git` segment (case-insensitive) - otherwise `UnsafeArchive("path_invalid")`; one
  leading `./` is removed and a member `.` or `./` is ignored;
- symbolic links, hard links, devices, FIFOs and other member types are `UnsafeArchive("path_invalid")` with
  `links="refuse"` (the export of a guest, which must contain regular files only), and are skipped and counted with
  `links="skip"` (a GitHub tarball: a repository may contain links; they are never created);
- sparse members are refused (`archive_invalid`), and so is the same path twice;
- at most `limits.max_entries` members and `limits.max_bytes` bytes of file content (`size_exceeded`);
- an extended header (PAX, GNU long name) larger than `MAX_HEADER` is `size_exceeded` before it is read;
- an archive that is not a tar (or gzip, with `compressed=True`) or ends early is `archive_invalid`.

`repack(source, target, strip_top=True, ...)` rewrites a GitHub tarball (`<owner>-<repo>-<sha7>/...`) as a plain tar
without its top directory: one top directory shared by every member (`path_invalid` otherwise), PAX format, files
`0600`, directories `0700`, owner `0`, modification time `0`; links are skipped, and so is every member with a
`protected` name: the names Claude Code reads as its instructions or configuration (`.claude/`, `.claude.json`,
`.mcp.json`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`) and `.gitconfig`, compared case-insensitively on every segment
(05 section 5.3; ATS-03, ADR-045). A repository may contain them, but a session must never see them as its own (prompt
injection through the instruction files); `github.commit` keeps them unchanged (`agent_runner.github.commit.plan`).
"""

from __future__ import annotations

import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, BinaryIO, Literal

CHUNK = 1024 * 1024
FILE_MODE = 0o600
DIR_MODE = 0o700
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})
PROTECTED_SEGMENTS = frozenset(
    {".claude", ".claude.json", ".mcp.json", "claude.md", "claude.local.md", "agents.md", ".gitconfig"}
)
MAX_HEADER = 64 * 1024
HEADER_TYPES = frozenset(
    {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}
)


class UnsafeArchive(Exception):
    """A refused archive: `code` is `path_invalid`, `size_exceeded` or `archive_invalid`."""

    def __init__(self, code: Literal["path_invalid", "size_exceeded", "archive_invalid"]) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    max_entries: int


@dataclass
class Stats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class Entry:
    path: str
    kind: Literal["file", "dir"]
    size: int
    reader: IO[bytes] | None


def safe_path(name: str) -> str | None:
    """The normalised relative path of a member name; None for the archive root (`.`)."""
    if name in (".", "./"):
        return None
    path = name.removeprefix("./").removesuffix("/")
    if not path or path.startswith("/") or any(ord(char) < 0x20 or char in "\x7f\\" for char in path):
        raise UnsafeArchive("path_invalid")
    for segment in path.split("/"):
        if segment in _FORBIDDEN_SEGMENTS or segment.casefold() == ".git":
            raise UnsafeArchive("path_invalid")
    return path


def protected(path: str) -> bool:
    """Whether a segment of the relative `path` is a protected name (see the module docstring)."""
    return any(segment.casefold() in PROTECTED_SEGMENTS for segment in path.split("/"))


class CappedInfo(tarfile.TarInfo):
    """Refuses an extended header (PAX, GNU long name or link) larger than `MAX_HEADER` before `tarfile` reads it into
    memory: the payload limits would only see it afterwards (a header bomb of a few KiB compressed)."""

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.type in HEADER_TYPES and self.size > MAX_HEADER:
            raise UnsafeArchive("size_exceeded")
        result: tarfile.TarInfo = super()._proc_member(archive)  # type: ignore[misc]
        return result


def _open(source: BinaryIO, compressed: bool) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=source, mode="r|gz" if compressed else "r|", tarinfo=CappedInfo)
    except (tarfile.TarError, OSError, EOFError):
        raise UnsafeArchive("archive_invalid") from None


def entries(
    source: BinaryIO,
    *,
    limits: Limits,
    links: Literal["refuse", "skip"] = "refuse",
    compressed: bool = False,
    stats: Stats | None = None,
) -> Iterator[Entry]:
    """The regular files and directories of the archive (see the module docstring)."""
    stats = stats if stats is not None else Stats()
    seen: set[str] = set()
    archive = _open(source, compressed)
    count = 0
    try:
        while True:
            try:
                member = archive.next()
            except (tarfile.TarError, OSError, EOFError):
                raise UnsafeArchive("archive_invalid") from None
            if member is None:
                return
            count += 1
            if count > limits.max_entries:
                raise UnsafeArchive("size_exceeded")
            path = safe_path(member.name)
            if not (member.isreg() or member.isdir()):
                if links == "refuse":
                    raise UnsafeArchive("path_invalid")
                stats.skipped += 1
                continue
            if member.issparse():
                raise UnsafeArchive("archive_invalid")
            if path is None:
                continue
            if path in seen:
                raise UnsafeArchive("archive_invalid")
            seen.add(path)
            if member.isdir():
                stats.dirs += 1
                yield Entry(path, "dir", 0, None)
                continue
            stats.files += 1
            stats.bytes += member.size
            if stats.bytes > limits.max_bytes:
                raise UnsafeArchive("size_exceeded")
            yield Entry(path, "file", member.size, archive.extractfile(member))
    finally:
        archive.close()


def copy_exact(reader: IO[bytes], size: int, write: Callable[[bytes], object]) -> None:
    """Copy exactly `size` bytes to `write` (a member that ends early is `archive_invalid`)."""
    left = size
    while left:
        try:
            data = reader.read(min(CHUNK, left))
        except (tarfile.TarError, OSError, EOFError):
            raise UnsafeArchive("archive_invalid") from None
        if not data:
            raise UnsafeArchive("archive_invalid")
        write(data)
        left -= len(data)


def _info(path: str, kind: Literal["file", "dir"], size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.type = tarfile.REGTYPE if kind == "file" else tarfile.DIRTYPE
    info.mode = FILE_MODE if kind == "file" else DIR_MODE
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def repack(
    source: BinaryIO, target: BinaryIO, *, limits: Limits, compressed: bool = True, strip_top: bool = True
) -> Stats:
    """Rewrite `source` into a plain tar on `target` (see the module docstring); the counts of the result."""
    stats = Stats()
    top: str | None = None
    with tarfile.open(fileobj=target, mode="w|", format=tarfile.PAX_FORMAT) as out:
        for entry in entries(source, limits=limits, links="skip", compressed=compressed, stats=stats):
            path = entry.path
            if strip_top:
                first, _, rest = path.partition("/")
                if top is None:
                    top = first
                if first != top:
                    raise UnsafeArchive("path_invalid")
                if not rest:
                    if entry.kind == "dir":
                        stats.dirs -= 1
                        continue
                    raise UnsafeArchive("path_invalid")
                path = rest
            if protected(path):
                stats.skipped += 1
                if entry.kind == "dir":
                    stats.dirs -= 1
                else:
                    stats.files -= 1
                    stats.bytes -= entry.size
                continue
            info = _info(path, entry.kind, entry.size)
            if entry.reader is None:
                out.addfile(info)
                continue
            out.addfile(info, _Exact(entry.reader, entry.size))
    return stats


class _Exact:
    """A reader that yields exactly `size` bytes of `reader` and refuses a member that ends early."""

    def __init__(self, reader: IO[bytes], size: int) -> None:
        self._reader = reader
        self._left = size

    def read(self, size: int = -1) -> bytes:
        want = self._left if size < 0 else min(size, self._left)
        if not want:
            return b""
        try:
            data = self._reader.read(want)
        except (tarfile.TarError, OSError, EOFError):
            raise UnsafeArchive("archive_invalid") from None
        if not data:
            raise UnsafeArchive("archive_invalid")
        self._left -= len(data)
        return data
