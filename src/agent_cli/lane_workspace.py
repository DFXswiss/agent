"""Script-owned source snapshot and guarded application of text proposals."""

from __future__ import annotations

import os
import stat
import uuid
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from .lane_protocol import MAX_FILE_BYTES, ProtocolError, validate_path

_PRIVATE_PARTS = {".git", ".ssh", ".config", ".coordinator-control", ".agent-coordinator"}
_PRIVATE_FILES = {".env", "ai-accounts.json", "github-accounts.json", "coordinator.json"}


def path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def local_manifest(cwd: str) -> list[str]:
    """Static local Git read without credentials or global Git configuration."""
    import shutil
    import tempfile
    from .coordinator_exec import run_bounded
    binary = shutil.which("git", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if binary is None:
        raise ProtocolError("script Git executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="agent-source-inventory-") as home:
        result = run_bounded([binary, "-C", cwd, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             timeout=30, inherit_env=False, clear_ambient_github=False,
                             env={"HOME": home, "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
                                  "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0:
        raise ProtocolError("script could not read source inventory")
    return [p for p in result.stdout.split("\0") if p]


def source_path(value: str) -> str:
    path = validate_path(value)
    if any(p.casefold() in _PRIVATE_PARTS for p in path.split("/")) or path.split("/")[-1].casefold() in _PRIVATE_FILES:
        raise ProtocolError("control or credential path is not model source")
    return path


class Workspace:
    """A static caller provides Git's source manifest; models never see Git."""

    def __init__(self, root: Path, paths: list[str], *, max_bytes: int = 50_000_000):
        self.root = root
        if root.is_symlink() or not root.is_dir():
            raise ProtocolError("source root must be a real directory")
        if len(paths) > 20_000 or len(paths) != len({path_key(p) for p in paths}):
            raise ProtocolError("invalid or oversized source inventory")
        self.files: dict[str, str] = {}
        self.modes: dict[str, int] = {}
        self.unavailable: list[str] = []
        self.total = 0
        self.max_bytes = max_bytes
        for path in sorted(paths):
            try:
                source_path(path)
                text, mode = self._read(path)
            except (ProtocolError, OSError, UnicodeError):
                self.unavailable.append(path)
                continue
            self.total += len(path.encode()) + len(text.encode())
            if self.total > max_bytes:
                raise ProtocolError("source snapshot exceeds byte limit")
            self.files[path], self.modes[path] = text, mode

    @contextmanager
    def _parent(self, path: str, *, create: bool = False):
        parts = source_path(path).split("/")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def _read(self, path: str) -> tuple[str, int]:
        with self._parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                    raise ProtocolError("source must be a bounded regular file without hard links")
                content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES or b"\0" in content:
                    raise ProtocolError("unsupported source contents")
                return content.decode("utf-8"), stat.S_IMODE(info.st_mode)

    def apply(self, changes: dict[str, str | None]) -> None:
        """Check every touched path before applying; never execute the proposal.

        Individual writes are atomic. The coordinator owns the worktree lock
        and treats an interrupted multi-file application as uncertain.
        """
        folded = {path_key(p): p for p in self.files}
        # Reject file/directory conversions too: application is deliberately
        # per-file atomic, so no proposal may depend on intermediate ordering.
        all_paths = {path_key(p) for p in [*self.files, *self.unavailable, *changes]}
        for path in all_paths:
            parts = path.split("/")
            if any("/".join(parts[:i]) in all_paths for i in range(1, len(parts))):
                raise ProtocolError("source file and directory paths collide")
        for path, content in changes.items():
            source_path(path)
            if path in self.unavailable:
                raise ProtocolError("proposal targets unavailable source")
            if path_key(path) in folded and folded[path_key(path)] != path:
                raise ProtocolError("ambiguous case-insensitive source path")
            folded[path_key(path)] = path
            if content is not None and (not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES or "\0" in content):
                raise ProtocolError("invalid proposed source content")
            try:
                current, mode = self._read(path)
            except FileNotFoundError:
                current, mode = None, None
            if current != self.files.get(path) or (path in self.modes and mode != self.modes[path]):
                raise ProtocolError("worktree changed since the model snapshot")
            if content is None and current is None:
                raise ProtocolError("cannot delete absent source")
        for path, content in sorted(changes.items()):
            with self._parent(path, create=content is not None) as (parent, name):
                if content is None:
                    os.unlink(name, dir_fd=parent)
                    continue
                temporary = ".agent-text-" + uuid.uuid4().hex
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             self.modes.get(path, 0o644), dir_fd=parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
