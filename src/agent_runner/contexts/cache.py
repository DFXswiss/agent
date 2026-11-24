"""The local encrypted checkpoint cache `<home>/contexts/` (ADR-072 section 4).

`<home>/contexts/<object_id>/<index>` holds the sealed chunks exactly as they are uploaded (nonce and ciphertext; the
manifest and signature are in `runner.context_object`). Directories are 0700, files 0600 and written atomically
(temporary name, then `rename`), so a crash never leaves a short chunk under its final name. Plaintext is never written
here.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from agent_runner.identity import ensure_private_dir

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Cache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def directory(self, object_id: str) -> Path:
        if not _UUID_RE.fullmatch(object_id):
            raise ValueError("object id")
        return self.root / object_id

    def write_chunk(self, object_id: str, index: int, data: bytes) -> None:
        directory = self.directory(object_id)
        ensure_private_dir(directory)
        final = directory / str(index)
        partial = directory / f".{index}.part"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(partial, final)

    def read_chunk(self, object_id: str, index: int) -> bytes | None:
        """The stored chunk, None when it is missing (removed, never written)."""
        try:
            fd = os.open(self.directory(object_id) / str(index), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as handle:
            return handle.read()

    def remove(self, object_id: str) -> None:
        shutil.rmtree(self.directory(object_id), ignore_errors=True)
