"""File import into and export out of a session workspace (03-api.md section 7.1 `files/import`, `files/export`;
05-runner.md section 12.1 `github.checkout` and `github.commit`; ATK-34, ATK-40).

The control process runs as root and the workspace belongs to the session user, who may replace any part of it by a
symbolic link at any time. So nothing here follows a link inside the workspace: every directory is opened relative to
its parent with ``O_NOFOLLOW | O_DIRECTORY``, every file with ``O_NOFOLLOW``, and checked with ``fstat`` after the
open. Intermediate data lives in a staging directory ``<session dir>/.files-<random>`` next to ``work`` - inside the
root-owned session directory (0711), which the session cannot list or write - and is removed at the end.

Import (``import_tar``), body ``application/x-tar`` of at most ``MAX_IMPORT_BYTES``:

* ``dir`` and every member path: relative, no control character or backslash, no empty, ``.`` or ``..`` segment, no
  ``.git`` segment (``ops/rules.json#/path``), valid UTF-8 - else ``path_invalid``; one leading ``./`` is removed, a
  member ``.`` is ignored. No segment may be a protected name either (``PROTECTED``: the names Claude Code reads as
  its instructions or configuration, ``.claude``, ``.claude.json``, ``.mcp.json``, ``CLAUDE.md``, ``CLAUDE.local.md``,
  ``AGENTS.md``, and ``.gitconfig``, case-insensitively - the write policy's names, 05 section 5.3, ADR-045): a
  session never gets them through an import (prompt injection through a repository's instruction files; the
  runner's checkout already leaves them out, ATS-03); export paths follow the same rule, so an export leaves such
  files out
* only regular files and directories; links, devices, FIFOs and sparse members are ``request_invalid``, so is a path
  that appears twice or a malformed archive; more than ``MAX_ENTRIES`` members, ``MAX_IMPORT_BYTES`` of content or
  an extended header over ``MAX_HEADER`` is ``size_exceeded``
* the archive is extracted completely into the staging directory first (files ``0600``, directories ``0700``, owner
  the session user and its primary group, i.e. the session's own group); a refused archive leaves the workspace as it
  was
* then the result is moved into place with one ``rename``: the parents of ``dir`` are created where missing (``0700``,
  session user) - an existing parent that is a link or not a directory is ``path_invalid``; with ``replace=true`` an
  existing ``dir`` (whatever it is, a link included) is first moved into the staging directory and removed there;
  with ``replace=false`` ``dir`` must not exist or be an empty directory (``state_conflict`` otherwise)
* answer ``{files, bytes}``: regular files and their bytes
* ``guard(dir, tree_fd)`` (optional) is called with the extracted staging tree before it is moved and returns the
  context the move runs in: the workspace scanner surveys the tree and makes it its baseline (``scanner.py``
  ``materializing``, HOT-06), so an import is never reported as file changes of the session

Export (``export_tar``), ``{dir, paths?}`` with ``paths`` relative to ``dir``:

* ``dir`` exactly ``.`` (``WHOLE_WORKSPACE``) exports the whole workspace (the work VM's ``workvm_sync``, 13-work-vm.md
  section 3, KOR-29); every other ``dir`` follows the path rules of the import, and the import still refuses ``.``
* ``dir`` missing is ``not_found``, a link or a file ``path_invalid``; a listed path that does not exist is left out
  (for ``github.commit`` it is a deletion)
* directories are walked in name order (``.git`` directories are left out, at most ``MAX_DEPTH`` levels); regular
  files owned by the session user are exported; links are reported as skipped ``symlink``, other types as
  ``special``, files of another owner, unreadable files and too deep directories as ``unreadable``; names that are
  not valid paths (control characters, invalid UTF-8) are left out
* every file is read once, hashed and copied into the staging tar, so the manifest and the content always agree even
  while the session writes; at most ``MAX_ENTRIES`` entries of any kind (files, directories, skipped entries - so a
  session cannot make the root process build a huge manifest) and ``MAX_EXPORT_BYTES`` bytes (``size_exceeded``)
* the answer is ``application/x-tar`` whose first member is ``.agent-export-manifest.json`` = canonical JSON
  ``{files:[{path,size,sha256}], skipped:[{path,reason}]}`` (sorted by path), followed by the files (``0600``,
  owner 0, time 0); the header ``X-Manifest-Sha256`` is the manifest's SHA-256 (decisions CTR-02 no. 8)
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import pwd
import shutil
import stat
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import IO, Any, Protocol, cast

from agent_guest import policy
from agent_guest.journal import canonical
from agent_guest.state import Session

MAX_IMPORT_BYTES = 512 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_DEPTH = 128
MAX_PATH = 4096
FILE_MODE = 0o600
DIR_MODE = 0o700
MANIFEST = ".agent-export-manifest.json"
CHUNK = 1024 * 1024
SPOOL_MEMORY = 1024 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC
_LINK_ERRORS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})
WHOLE_WORKSPACE = "."
PROTECTED = policy.PROTECTED_DIRS | policy.PROTECTED_FILES
MAX_HEADER = 64 * 1024
HEADER_TYPES = frozenset(
    {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}
)


class FilesError(Exception):
    """A refused import or export; ``code`` is a guest error code (``guest/error.response.json``); ``link`` marks a
    ``path_invalid`` caused by a symbolic link (rather than a file where a directory was expected)."""

    def __init__(self, code: str, *, link: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.link = link


@dataclass(frozen=True)
class Owner:
    uid: int
    gid: int


def owner_of(session: Session) -> Owner:
    """The session user and its primary group (the session's own group, ``sessions.py``)."""
    try:
        return Owner(session.uid, pwd.getpwuid(session.uid).pw_gid)
    except KeyError:
        raise FilesError("unavailable") from None


def path_ok(path: str) -> bool:
    if not 1 <= len(path) <= MAX_PATH or path.startswith("/"):
        return False
    if any(ord(char) < 0x20 or char in "\x7f\\" for char in path):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(part not in _FORBIDDEN_SEGMENTS and part.casefold() not in PROTECTED for part in path.split("/"))


def segments(path: str) -> list[str]:
    if not path_ok(path):
        raise FilesError("path_invalid")
    return path.split("/")


# -- descriptors ---------------------------------------------------------------------------------------------------


def _own(fd: int, owner: Owner, mode: int) -> None:
    os.fchown(fd, owner.uid, owner.gid)
    os.fchmod(fd, mode)


def open_dir(parent: int, name: str) -> int:
    """``name`` below the directory ``parent`` as a directory descriptor, never through a link."""
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise FilesError("not_found") from None
    except OSError as error:
        if error.errno in _LINK_ERRORS:
            raise FilesError("path_invalid", link=_is_link(parent, name)) from None
        raise


def _is_link(parent: int, name: str) -> bool:
    """Whether ``name`` is a symbolic link (``O_NOFOLLOW | O_DIRECTORY`` on a link is ``ELOOP`` on Linux and
    ``ENOTDIR`` on macOS); only used to report a refusal."""
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
    except OSError:
        return False


def walk(root: int, parts: Sequence[str], *, create: Owner | None = None) -> int:
    """The directory ``root/parts...`` (a new descriptor); missing directories are created for ``create``."""
    current = os.dup(root)
    try:
        for part in parts:
            try:
                found = open_dir(current, part)
            except FilesError as error:
                if error.code != "not_found" or create is None:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, DIR_MODE, dir_fd=current)
                found = open_dir(current, part)
                _own(found, create, DIR_MODE)
            os.close(current)
            current = found
    except BaseException:
        os.close(current)
        raise
    return current


def open_workspace(session: Session) -> int:
    try:
        return os.open(session.workspace, DIR_FLAGS)
    except FileNotFoundError:
        raise FilesError("not_found") from None


def make_stage(session: Session) -> str:
    """A fresh staging directory inside the root-owned session directory."""
    path = os.path.join(os.path.dirname(session.workspace), f".files-{uuid.uuid4().hex}")
    os.mkdir(path, 0o700)
    return path


def remove_stage(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# -- import --------------------------------------------------------------------------------------------------------


@dataclass
class _Extracted:
    files: int = 0
    bytes: int = 0
    entries: int = 0
    seen: set[str] = field(default_factory=set)


def _member_path(name: str) -> str | None:
    if name in (".", "./"):
        return None
    path = name.removeprefix("./").removesuffix("/")
    if not path_ok(path):
        raise FilesError("path_invalid")
    return path


def _copy(reader: IO[bytes], size: int, fd: int) -> None:
    left = size
    while left:
        data = reader.read(min(CHUNK, left))
        if not data:
            raise FilesError("request_invalid")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        left -= len(data)


Modes = Callable[[str, tarfile.TarInfo], int]
"""The file mode an extracted regular member gets (default ``FILE_MODE``)."""
LinkCheck = Callable[[str, str], bool]
"""Whether a symbolic link member ``(path, target)`` may be created (default: none may)."""


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    tree: int,
    owner: Owner,
    path: str,
    *,
    modes: Modes | None = None,
) -> None:
    parts = path.split("/")
    parent = -1
    try:
        parent = walk(tree, parts[:-1], create=owner)
        if member.isdir():
            with contextlib.suppress(FileExistsError):
                os.mkdir(parts[-1], DIR_MODE, dir_fd=parent)
            fd = open_dir(parent, parts[-1])
            try:
                _own(fd, owner, DIR_MODE)
            finally:
                os.close(fd)
            return
        if member.issym():
            os.symlink(member.linkname, parts[-1], dir_fd=parent)
            os.chown(parts[-1], owner.uid, owner.gid, dir_fd=parent, follow_symlinks=False)
            return
        mode = FILE_MODE if modes is None else modes(path, member)
        fd = os.open(parts[-1], CREATE_FLAGS, mode, dir_fd=parent)
        try:
            _own(fd, owner, mode)
            reader = archive.extractfile(member)
            assert reader is not None
            _copy(reader, member.size, fd)
        finally:
            os.close(fd)
    except FilesError as error:
        # a member below a path that is a file (`a`, then `a/b`) or a link
        raise FilesError("request_invalid") from error
    except OSError:
        # the same file twice, or a directory over a file
        raise FilesError("request_invalid") from None
    finally:
        if parent >= 0:
            os.close(parent)


class Readable(Protocol):
    """The request body (``routing.BodyReader``)."""

    def read(self, size: int = -1) -> bytes: ...


class CappedInfo(tarfile.TarInfo):
    """Refuses an extended header (PAX, GNU long name or link) over ``MAX_HEADER`` before ``tarfile`` reads it."""

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.type in HEADER_TYPES and self.size > MAX_HEADER:
            raise FilesError("size_exceeded")
        result: tarfile.TarInfo = super()._proc_member(archive)  # type: ignore[misc]
        return result


def _open_archive(stream: Readable) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=cast(IO[bytes], stream), mode="r|", tarinfo=CappedInfo)
    except tarfile.TarError:
        raise FilesError("request_invalid") from None


def _extract(
    stream: Readable,
    tree: int,
    owner: Owner,
    limit: int = MAX_IMPORT_BYTES,
    *,
    modes: Modes | None = None,
    link_ok: LinkCheck | None = None,
) -> _Extracted:
    """Extract every member below ``tree`` (see the module docstring); at most ``limit`` bytes of content. A context
    staging (``contexts.stage``, KOR-33) passes ``modes`` (execute bits kept) and ``link_ok`` (symbolic links it
    allows; never followed afterwards: every later member is opened through ``walk`` without following links)."""
    done = _Extracted()
    with _open_archive(stream) as archive:
        while True:
            try:
                member = archive.next()
            except tarfile.TarError:
                raise FilesError("request_invalid") from None
            if member is None:
                return done
            done.entries += 1
            if done.entries > MAX_ENTRIES:
                raise FilesError("size_exceeded")
            path = _member_path(member.name)
            link = member.issym() and link_ok is not None and path is not None and link_ok(path, member.linkname)
            if not (member.isreg() or member.isdir() or link) or member.issparse():
                raise FilesError("request_invalid")
            if path is None:
                continue
            if path in done.seen:
                raise FilesError("request_invalid")
            done.seen.add(path)
            if member.isreg():
                done.files += 1
                done.bytes += member.size
                if done.bytes > limit:
                    raise FilesError("size_exceeded")
            try:
                _extract_member(archive, member, tree, owner, path, modes=modes)
            except tarfile.TarError:
                raise FilesError("request_invalid") from None


def _install(session: Session, parts: Sequence[str], stage: int, owner: Owner, *, replace: bool) -> None:
    workspace = open_workspace(session)
    try:
        parent = walk(workspace, parts[:-1], create=owner)
    finally:
        os.close(workspace)
    name = parts[-1]
    try:
        for attempt in range(3):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if replace:
                    os.rename(name, f"old-{attempt}", src_dir_fd=parent, dst_dir_fd=stage)
                else:
                    try:
                        os.rmdir(name, dir_fd=parent)
                    except OSError:
                        raise FilesError("state_conflict") from None
            try:
                os.rename("tree", name, src_dir_fd=stage, dst_dir_fd=parent)
            except OSError:
                # the session created `dir` again in between
                continue
            return
        raise FilesError("state_conflict")
    finally:
        os.close(parent)


Guard = Callable[[str, int], AbstractContextManager[object]]


def import_tar(
    session: Session, directory: str, stream: Readable, *, replace: bool, guard: Guard | None = None
) -> dict[str, int]:
    """Import the tar ``stream`` into ``directory`` of the session's workspace (see the module docstring)."""
    parts = segments(directory)
    owner = owner_of(session)
    path = make_stage(session)
    try:
        stage = os.open(path, DIR_FLAGS)
        try:
            os.mkdir("tree", DIR_MODE, dir_fd=stage)
            tree = open_dir(stage, "tree")
            try:
                _own(tree, owner, DIR_MODE)
                done = _extract(stream, tree, owner, MAX_IMPORT_BYTES)
                moving = contextlib.nullcontext() if guard is None else guard("/".join(parts), tree)
            finally:
                os.close(tree)
            with moving:
                _install(session, parts, stage, owner, replace=replace)
        finally:
            os.close(stage)
    finally:
        remove_stage(path)
    return {"files": done.files, "bytes": done.bytes}


# -- export --------------------------------------------------------------------------------------------------------


@dataclass
class _Export:
    """One walk into ``archive``: member names are ``prefix`` + path, files get ``mode``, at most ``limit`` bytes."""

    session: Session
    stage: str
    archive: tarfile.TarFile
    prefix: str = ""
    limit: int = MAX_EXPORT_BYTES
    mode: int = FILE_MODE
    files: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    added: set[str] = field(default_factory=set)
    bytes: int = 0
    entries: int = 0

    def count(self, more: int = 1) -> None:
        self.entries += more
        if self.entries > MAX_ENTRIES:
            raise FilesError("size_exceeded")

    def skip(self, path: str, reason: str) -> None:
        self.added.add(path)
        self.skipped.append({"path": path, "reason": reason})

    def add_file(self, fd: int, path: str) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            self.skip(path, "special")
            return
        if info.st_uid != self.session.uid:
            self.skip(path, "unreadable")
            return
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, dir=self.stage) as spool:
            while True:
                try:
                    data = os.read(fd, CHUNK)
                except OSError:
                    self.skip(path, "unreadable")
                    return
                if not data:
                    break
                size += len(data)
                if self.bytes + size > self.limit:
                    raise FilesError("size_exceeded")
                digest.update(data)
                spool.write(data)
            spool.seek(0)
            self.archive.addfile(member_info(self.prefix + path, size, self.mode), spool)
        self.bytes += size
        self.added.add(path)
        self.files.append({"path": path, "size": size, "sha256": digest.hexdigest()})

    def entry(self, parent: int, name: str, path: str, depth: int) -> None:
        """One directory entry ``name`` of ``parent`` (``path`` relative to the exported directory)."""
        if not path_ok(path) or path in self.added:
            return
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            self.skip(path, "symlink")
        elif stat.S_ISDIR(info.st_mode):
            self.directory(parent, name, path, depth + 1)
        elif stat.S_ISREG(info.st_mode):
            self.regular(parent, name, path)
        else:
            self.skip(path, "special")

    def regular(self, parent: int, name: str, path: str) -> None:
        try:
            fd = os.open(name, READ_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError as error:
            self.skip(path, "symlink" if error.errno == errno.ELOOP else "unreadable")
            return
        try:
            self.add_file(fd, path)
        finally:
            os.close(fd)

    def directory(self, parent: int, name: str, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.skip(path, "unreadable")
            return
        try:
            fd = open_dir(parent, name)
        except FilesError as error:
            if error.link:
                self.skip(path, "symlink")
            return
        try:
            self.children(fd, path, depth)
        finally:
            os.close(fd)

    def children(self, fd: int, prefix: str, depth: int) -> None:
        names: list[str] = []
        with os.scandir(fd) as found:
            for item in found:
                self.count()
                names.append(item.name)
        for name in sorted(names):
            self.entry(fd, name, f"{prefix}/{name}" if prefix else name, depth)

    def listed(self, root: int, path: str) -> None:
        """A path of ``paths``: its parents are walked without links (a missing one leaves the path out)."""
        parts = segments(path)
        try:
            parent = walk(root, parts[:-1])
        except FilesError as error:
            if error.link:
                self.skip(path, "symlink")
            return
        try:
            self.entry(parent, parts[-1], path, len(parts) - 1)
        finally:
            os.close(parent)


def member_info(path: str, size: int, mode: int = FILE_MODE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.size = size
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def member_bytes(path: str, data: bytes, mode: int = FILE_MODE) -> bytes:
    """A complete tar member (PAX header and padded content) of a small file built in memory."""
    head = member_info(path, len(data), mode).tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")
    return head + data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)


class Stream:
    """The answer body: ``head`` (the first member), then the staged parts - each a file of which the first ``length``
    bytes are sent (``None``: all of it) -, then ``tail``. The staging directory is removed when the stream ends, is
    closed, or is collected without ever being read (the client went away before the body was sent)."""

    def __init__(self, head: bytes, parts: Sequence[tuple[str, int | None]], stage: str, tail: bytes = b"") -> None:
        self._head, self._parts, self._stage, self._tail = head, list(parts), stage, tail

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield self._head
            for path, length in self._parts:
                left = length
                with open(path, "rb") as source:
                    while left is None or left > 0:
                        chunk = source.read(CHUNK if left is None else min(CHUNK, left))
                        if not chunk:
                            break
                        if left is not None:
                            left -= len(chunk)
                        yield chunk
            if self._tail:
                yield self._tail
        finally:
            self.close()

    def close(self) -> None:
        remove_stage(self._stage)

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True)
class Exported:
    """The manifest digest, the manifest and the tar stream."""

    digest: str
    manifest: dict[str, Any]
    stream: Stream


def export_tar(session: Session, directory: str, paths: Sequence[str] | None = None) -> Exported:
    """Export ``directory`` of the session's workspace (``.``: the whole workspace), or the listed ``paths`` below it
    (see the module docstring)."""
    parts = [] if directory == WHOLE_WORKSPACE else segments(directory)
    for path in paths or ():
        segments(path)
    workspace = open_workspace(session)
    try:
        root = walk(workspace, parts)
    finally:
        os.close(workspace)
    stage = make_stage(session)
    try:
        body = os.path.join(stage, "body.tar")
        try:
            with tarfile.open(body, mode="w", format=tarfile.PAX_FORMAT) as archive:
                export = _Export(session, stage, archive, limit=MAX_EXPORT_BYTES)
                if paths is None:
                    export.children(root, "", 0)
                else:
                    for path in sorted(set(paths)):
                        export.listed(root, path)
        finally:
            os.close(root)
        manifest = {
            "files": sorted(export.files, key=lambda item: item["path"]),
            "skipped": sorted(export.skipped, key=lambda item: item["path"]),
        }
        data = canonical(manifest).encode("utf-8")
        head = member_bytes(MANIFEST, data)
    except BaseException:
        remove_stage(stage)
        raise
    return Exported(hashlib.sha256(data).hexdigest(), manifest, Stream(head, [(body, None)], stage))
