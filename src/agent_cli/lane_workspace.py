"""Script-owned source snapshot and guarded application of text proposals."""

from __future__ import annotations

import json
import os
import stat
import uuid
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from .lane_protocol import MAX_FILE_BYTES, ProtocolError, validate_path

_PRIVATE_PARTS = {".git", ".ssh", ".config", ".coordinator-control", ".agent-coordinator"}
_PRIVATE_FILES = {".env", "ai-accounts.json", "github-accounts.json", "coordinator.json"}
_RECOVERY_INDEX = "recovery-index.json"


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
        self.recovery_dir: Path | None = None
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

    @staticmethod
    def _fsync_dir(dir_fd: int) -> None:
        """Request a directory durability barrier; surface EINVAL/EIO to callers."""
        os.fsync(dir_fd)

    @contextmanager
    def _parent(self, path: str, *, create: bool = False):
        parts = source_path(path).split("/")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    created = False
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                        created = True
                    except FileExistsError:
                        pass
                    if created:
                        # Persist the new directory entry before descending.
                        self._fsync_dir(fd)
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

    def _read_at(self, parent_fd: int, name: str) -> tuple[str, int, int]:
        """Return text, mode, and inode for a regular file opened via dir_fd."""
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                raise ProtocolError("source must be a bounded regular file without hard links")
            content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES or b"\0" in content:
                raise ProtocolError("unsupported source contents")
            return content.decode("utf-8"), stat.S_IMODE(info.st_mode), info.st_ino

    def _fsync_captured(self, recovery_fd: int, recovery_name: str) -> None:
        """Fsync captured regular-file data after namespace persistence checks."""
        fd = os.open(
            recovery_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=recovery_fd,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                raise ProtocolError("source must be a bounded regular file without hard links")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _open_recovery(self) -> tuple[Path, int]:
        """Create a private same-filesystem recovery directory outside the repository."""
        root = self.root.resolve()
        parent = root.parent
        if root == parent:
            raise ProtocolError("unsupported source publication root")
        try:
            root_stat = os.stat(root, follow_symlinks=False)
            parent_stat = os.stat(parent, follow_symlinks=False)
        except OSError as exc:
            raise ProtocolError("cannot verify source publication device") from exc
        if root_stat.st_dev != parent_stat.st_dev:
            raise ProtocolError("unsupported cross-device source publication")
        recovery = parent / f".agent-source-recovery-{uuid.uuid4().hex}"
        try:
            os.mkdir(recovery, mode=0o700)
        except OSError as exc:
            raise ProtocolError("cannot create private recovery directory") from exc
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                self._fsync_dir(parent_fd)
            except OSError as exc:
                raise ProtocolError("cannot persist private recovery directory") from exc
        finally:
            os.close(parent_fd)
        recovery_fd = os.open(recovery, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            recovery_stat = os.fstat(recovery_fd)
            if recovery_stat.st_dev != root_stat.st_dev:
                raise ProtocolError("unsupported cross-device source publication")
        except Exception:
            os.close(recovery_fd)
            raise
        self.recovery_dir = recovery
        return recovery, recovery_fd

    def _write_recovery_index(
        self,
        recovery_fd: int,
        entries: list[dict[str, object]],
    ) -> None:
        """Persist operator-only capture mapping before any source mutation."""
        payload = {
            "source_root": str(self.root.resolve()),
            "entries": entries,
        }
        raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(
            _RECOVERY_INDEX,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=recovery_fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.unlink(_RECOVERY_INDEX, dir_fd=recovery_fd)
            except OSError:
                pass
            raise
        dir_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=recovery_fd)
        try:
            self._fsync_dir(dir_fd)
        finally:
            os.close(dir_fd)

    def _publish_bytes(self, parent_fd: int, name: str, data: bytes, mode: int) -> None:
        """Write an exclusive temp inode, fsync it, then no-clobber link into place."""
        temporary = ".agent-text-" + uuid.uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        error: BaseException | None = None
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            self._fsync_dir(parent_fd)
        except BaseException as exc:
            error = exc
        unlinked = False
        try:
            os.unlink(temporary, dir_fd=parent_fd)
            unlinked = True
        except FileNotFoundError:
            pass
        if unlinked:
            try:
                self._fsync_dir(parent_fd)
            except OSError as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def _restore_captured(self, parent_fd: int, name: str, recovery_fd: int, recovery_name: str) -> str:
        """Restore via a fresh inode; retain the recovery original.

        Returns a short status for error text: ``restored``, ``retained in recovery``,
        or ``retained in recovery beside existing destination``. Never unlinks an
        existing destination and never hard-links the recovery inode into the
        worktree (restored paths stay ``nlink == 1`` for later snapshots).
        FileExistsError only means the destination path is occupied (own prior
        publication or another writer); it does not establish concurrent provenance.
        """
        try:
            fd = os.open(
                recovery_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=recovery_fd,
            )
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                    return "retained in recovery"
                data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES or b"\0" in data:
                    return "retained in recovery"
                mode = stat.S_IMODE(info.st_mode)
            self._publish_bytes(parent_fd, name, data, mode)
            return "restored"
        except FileExistsError:
            return "retained in recovery beside existing destination"
        except OSError:
            return "retained in recovery"

    def _publish_new(self, parent_fd: int, name: str, content: str, mode: int) -> None:
        """Write an exclusive temp file, fsync it, then no-clobber link into place."""
        try:
            self._publish_bytes(parent_fd, name, content.encode("utf-8"), mode)
        except FileExistsError as exc:
            raise ProtocolError("publication conflict; destination exists") from exc

    def apply(self, changes: dict[str, str | None]) -> None:
        """Check every touched path before applying; never execute the proposal.

        Publication is race-resistant and data-preserving, not filesystem CAS or a
        multi-file transaction. Existing targets are renamed into a private
        same-filesystem recovery directory outside the repository, validated,
        then replacements are published with no-clobber link. Captured
        originals remain in recovery even on success so late writers through open
        descriptors are retained rather than destroyed. Paths may be briefly
        absent during capture; noncooperating writers can still yield an
        uncertain outcome. The root/recovery device preflight only compares the
        source root and recovery parent; nested mount mismatches surface later as
        retained recovery/uncertainty rather than a portable all-files guarantee.
        Directory fsync barriers are requested after namespace mutations on
        supporting filesystems; hardware and filesystem durability guarantees
        remain outside scope. An earlier fsync of a captured inode does not
        make later writes through another open descriptor durable.
        """
        if not changes:
            return
        folded = {path_key(p): p for p in self.files}
        # Reject file/directory conversions too: no-clobber publication of each
        # replacement is atomic, but capture makes an existing path briefly
        # absent, so no proposal may depend on intermediate ordering.
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

        planned: list[tuple[str, str | None, str | None, int]] = []
        index_entries: list[dict[str, object]] = []
        for path, content in sorted(changes.items()):
            expected_text = self.files.get(path)
            expected_mode = self.modes.get(path, 0o644)
            if expected_text is not None or content is None:
                recovery_name = uuid.uuid4().hex
                action = "delete" if content is None else "replace"
                planned.append((path, content, recovery_name, expected_mode))
                index_entries.append(
                    {
                        "path": path,
                        "action": action,
                        "mode": expected_mode,
                        "basename": recovery_name,
                    }
                )
            else:
                planned.append((path, content, None, 0o644))

        recovery, recovery_fd = self._open_recovery()
        try:
            self._write_recovery_index(recovery_fd, index_entries)
            for path, content, recovery_name, expected_mode in planned:
                expected_text = self.files.get(path)
                with self._parent(path, create=content is not None) as (parent, name):
                    if recovery_name is not None:
                        # Existing snapshot path or explicit delete: capture first.
                        try:
                            os.rename(name, recovery_name, src_dir_fd=parent, dst_dir_fd=recovery_fd)
                        except FileNotFoundError as exc:
                            raise ProtocolError(
                                f"source disappeared before capture for {path}"
                            ) from exc
                        try:
                            # Persist the capture rename: recovery dir first, then source parent.
                            # The recovery index alone does not make the capture durable.
                            self._fsync_dir(recovery_fd)
                            self._fsync_dir(parent)
                        except OSError as exc:
                            detail = self._restore_captured(parent, name, recovery_fd, recovery_name)
                            raise ProtocolError(
                                f"capture namespace sync failed for {path}; "
                                f"original {detail} as {recovery_name}"
                            ) from exc
                        try:
                            captured_text, captured_mode, _ino = self._read_at(recovery_fd, recovery_name)
                        except (ProtocolError, OSError, UnicodeError) as exc:
                            self._restore_captured(parent, name, recovery_fd, recovery_name)
                            raise ProtocolError(
                                f"captured source unreadable for {path}; "
                                f"original retained as {recovery_name}"
                            ) from exc
                        if captured_text != expected_text or captured_mode != expected_mode:
                            detail = self._restore_captured(parent, name, recovery_fd, recovery_name)
                            raise ProtocolError(
                                f"worktree changed since the model snapshot for {path}; "
                                f"captured original {detail} as {recovery_name}"
                            )
                        try:
                            self._fsync_captured(recovery_fd, recovery_name)
                        except (ProtocolError, OSError) as exc:
                            detail = self._restore_captured(parent, name, recovery_fd, recovery_name)
                            raise ProtocolError(
                                f"captured source sync failed for {path}; "
                                f"original {detail} as {recovery_name}"
                            ) from exc
                        if content is None:
                            # Deletion retains the captured original in recovery.
                            # Do not unlink a concurrent recreation of the destination.
                            try:
                                os.lstat(name, dir_fd=parent)
                            except FileNotFoundError:
                                continue
                            raise ProtocolError(
                                f"publication conflict for {path}; "
                                f"original preserved beside concurrent destination "
                                f"as {recovery_name}"
                            )
                        try:
                            self._publish_new(parent, name, content, expected_mode)
                        except ProtocolError as exc:
                            detail = self._restore_captured(parent, name, recovery_fd, recovery_name)
                            raise ProtocolError(
                                f"publication conflict for {path}; "
                                f"original {detail} as {recovery_name}"
                            ) from exc
                        except OSError as exc:
                            detail = self._restore_captured(parent, name, recovery_fd, recovery_name)
                            raise ProtocolError(
                                f"publication sync failed for {path}; "
                                f"original {detail} as {recovery_name}"
                            ) from exc
                    else:
                        # New file relative to the snapshot: no-clobber publish only.
                        try:
                            self._publish_new(parent, name, content, 0o644)
                        except ProtocolError as exc:
                            raise ProtocolError(
                                f"publication conflict for {path}; "
                                f"refusing to overwrite concurrent destination"
                            ) from exc
                        except OSError as exc:
                            raise ProtocolError(
                                f"publication sync failed for {path}; "
                                f"outcome uncertain"
                            ) from exc
        finally:
            os.close(recovery_fd)
