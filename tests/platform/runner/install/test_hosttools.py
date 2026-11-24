"""U-RUN-install-hosttools: Claude Code for the host, downloaded at installation (ADR-074 sections 1 and 3): only the
pinned tarball with the right SHA-256, only the regular file `package/claude`, installed 0755 under
`host/claude-code-<sha12>/` with `host/bin/claude` pointing at it, older copies removed, idempotent."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile

import pytest

from agent_runner.install import hosttools
from agent_runner.install.release import Fetch, Manifest

from .conftest import COMMIT, HUB, ROOT_KEY, VERSION, Kit, refused

URL = "https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.330.tgz"
BINARY = b"\xcf\xfa\xed\xfe claude binary"


def tarball(members: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                archive.addfile(member)
            else:
                info = tarfile.TarInfo(member[0])
                info.size = len(member[1])
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(member[1]))
    return buffer.getvalue()


GOOD = tarball([("package/package.json", b"{}"), ("package/claude", BINARY)])


def manifest(data: bytes = GOOD, *extra: Fetch, name: str = "claude-code") -> Manifest:
    item = Fetch(name, URL, hashlib.sha256(data).hexdigest())
    return Manifest(VERSION, COMMIT, HUB, ROOT_KEY, {}, (*extra, item))


def test_the_binary_is_read_from_the_tarball() -> None:
    assert hosttools.binary_from(GOOD) == BINARY


def link_member() -> tarfile.TarInfo:
    info = tarfile.TarInfo("package/claude")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    return info


def dir_member() -> tarfile.TarInfo:
    info = tarfile.TarInfo("package/claude")
    info.type = tarfile.DIRTYPE
    return info


@pytest.mark.parametrize("members", [[link_member()], [dir_member()]])
def test_a_member_that_is_no_regular_file_is_refused(members: list[tarfile.TarInfo | tuple[str, bytes]]) -> None:
    with refused("host_tools_invalid", "member"):
        hosttools.binary_from(tarball(members))


@pytest.mark.parametrize(
    "data",
    [
        tarball([("package/other", b"x")]),
        tarball([("claude", BINARY)]),
        b"not a tarball",
        GOOD[: len(GOOD) // 2],
    ],
    ids=["other_member", "outside_package", "no_tarball", "truncated"],
)
def test_a_tarball_without_the_binary_is_refused(data: bytes) -> None:
    with refused("host_tools_invalid", "archive"):
        hosttools.binary_from(data)


def test_nothing_happens_when_claude_code_is_not_pinned(kit: Kit) -> None:
    assert hosttools.ensure(kit.system, Manifest(VERSION, COMMIT, HUB, ROOT_KEY, {})) == "not_pinned"
    assert hosttools.ensure(kit.system, manifest(name="other-tool")) == "not_pinned"
    assert kit.fetch.urls == []
    assert not kit.layout.host_tools.exists()


def test_the_pinned_binary_is_installed_and_linked(kit: Kit) -> None:
    kit.fetch.files[URL] = GOOD
    other = Fetch("other-tool", "https://example.com/x", "00" * 32)
    pinned = manifest(GOOD, other)
    assert hosttools.ensure(kit.system, pinned) == "installed"
    target = kit.layout.host_tools / f"claude-code-{hashlib.sha256(GOOD).hexdigest()[:12]}"
    binary = target / "claude"
    link = kit.layout.host_tools / "bin/claude"
    assert binary.read_bytes() == BINARY
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    assert link.is_symlink()
    assert os.readlink(link) == str(binary)
    assert sorted(item.name for item in target.iterdir()) == ["claude"]
    assert sorted(item.name for item in link.parent.iterdir()) == ["claude"]
    assert kit.fetch.urls == [URL]
    # a second run finds it ready and downloads nothing
    assert hosttools.ensure(kit.system, pinned) == "ready"
    assert kit.fetch.urls == [URL]


def test_a_new_pin_replaces_the_old_copy(kit: Kit) -> None:
    kit.fetch.files[URL] = GOOD
    assert hosttools.ensure(kit.system, manifest(GOOD)) == "installed"
    old = kit.layout.host_tools / f"claude-code-{hashlib.sha256(GOOD).hexdigest()[:12]}"
    keep = kit.layout.host_tools / "notes"
    keep.mkdir()
    (kit.layout.host_tools / "bin/.claude.link").symlink_to("/stale")
    newer = tarball([("package/claude", b"new binary")])
    kit.fetch.files[URL] = newer
    assert hosttools.ensure(kit.system, manifest(newer)) == "installed"
    target = kit.layout.host_tools / f"claude-code-{hashlib.sha256(newer).hexdigest()[:12]}"
    assert os.readlink(kit.layout.host_tools / "bin/claude") == str(target / "claude")
    assert (target / "claude").read_bytes() == b"new binary"
    assert not old.exists()
    assert keep.is_dir()
    assert sorted(item.name for item in kit.layout.host_tools.iterdir()) == ["bin", target.name, "notes"]
    assert sorted(item.name for item in (kit.layout.host_tools / "bin").iterdir()) == ["claude"]


def test_a_broken_installation_is_repaired(kit: Kit) -> None:
    kit.fetch.files[URL] = GOOD
    pinned = manifest()
    assert hosttools.ensure(kit.system, pinned) == "installed"
    link = kit.layout.host_tools / "bin/claude"
    link.unlink()
    assert hosttools.ensure(kit.system, pinned) == "installed"
    link.unlink()
    link.symlink_to("/usr/bin/false")
    assert hosttools.ensure(kit.system, pinned) == "installed"
    assert hosttools.ensure(kit.system, pinned) == "ready"
    binary = kit.layout.host_tools / f"claude-code-{hashlib.sha256(GOOD).hexdigest()[:12]}/claude"
    binary.unlink()
    assert hosttools.ensure(kit.system, pinned) == "installed"
    assert binary.read_bytes() == BINARY
    assert len(kit.fetch.urls) == 4


def test_a_download_with_another_digest_is_refused(kit: Kit) -> None:
    kit.fetch.files[URL] = GOOD + b"\0"
    with refused("host_tools_invalid", "digest"):
        hosttools.ensure(kit.system, manifest(GOOD))
    assert not kit.layout.host_tools.exists()


def test_a_pinned_tarball_without_the_binary_installs_nothing(kit: Kit) -> None:
    empty = tarball([("package/package.json", b"{}")])
    kit.fetch.files[URL] = empty
    with refused("host_tools_invalid", "archive"):
        hosttools.ensure(kit.system, manifest(empty))
    assert not kit.layout.host_tools.exists()


def test_the_download_is_limited(kit: Kit) -> None:
    sizes: list[int] = []

    def fetch(url: str, limit: int) -> bytes:
        sizes.append(limit)
        return GOOD

    kit.system.fetch = fetch
    assert hosttools.ensure(kit.system, manifest()) == "installed"
    assert sizes == [512 * 1024 * 1024]
