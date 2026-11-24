"""Claude Code for the host, downloaded at installation (ADR-074 sections 1 and 3; 05-runner.md section 11.2).

The package does not redistribute Claude Code. Its signed manifest pins the npm tarball of the native macOS binary
(`fetch` entry `claude-code`); the root helper downloads it from the registry, checks the SHA-256, extracts only the
binary `package/claude` (a regular file, no links, no path outside the package) into
`host/claude-code-<sha12>/claude` (root, 0755) and points `host/bin/claude` at it. Older copies are removed.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import zlib

from agent_runner.errors import RunnerError
from agent_runner.install.release import Fetch, Manifest
from agent_runner.install.system import System

NAME = "claude-code"
MEMBER = "package/claude"
MAX_TARBALL = 512 * 1024 * 1024
PREFIX = "claude-code-"


def _item(manifest: Manifest) -> Fetch | None:
    return next((item for item in manifest.fetch if item.name == NAME), None)


def binary_from(tarball: bytes) -> bytes:
    """The bytes of `package/claude` in an npm tarball; `RunnerError` when it is missing or not a regular file."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
            member = archive.getmember(MEMBER)
            handle = archive.extractfile(member) if member.isreg() else None
            if handle is None:
                raise RunnerError("host_tools_invalid", "member")
            return handle.read()
    except (KeyError, tarfile.TarError, OSError, EOFError, zlib.error):
        raise RunnerError("host_tools_invalid", "archive") from None


def ensure(system: System, manifest: Manifest) -> str:
    """Install the pinned Claude Code for the host when it is missing; `ready`, `installed` or `not_pinned`."""
    item = _item(manifest)
    if item is None:
        return "not_pinned"
    host = system.layout.host_tools
    target = host / f"{PREFIX}{item.sha256[:12]}"
    binary = target / "claude"
    link = system.layout.claude_bin() / "claude"
    if binary.is_file() and link.is_symlink() and os.readlink(link) == str(binary):
        return "ready"
    tarball = system.fetch(item.url, MAX_TARBALL)
    if hashlib.sha256(tarball).hexdigest() != item.sha256:
        raise RunnerError("host_tools_invalid", "digest")
    content = binary_from(tarball)
    target.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = target / ".claude.tmp"
    temporary.write_bytes(content)
    temporary.chmod(0o755)
    os.replace(temporary, binary)
    link.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fresh = link.with_name(".claude.link")
    fresh.unlink(missing_ok=True)
    fresh.symlink_to(binary)
    os.replace(fresh, link)
    for old in host.iterdir():
        if old.name.startswith(PREFIX) and old != target:
            shutil.rmtree(old)
    return "installed"
