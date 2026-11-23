"""U-RUN-github: safe tar handling on the runner host (05-runner.md section 12.1 `github.checkout`; ATK-40 at unit
level): malicious archives with symbolic and hard links, `..`, absolute paths, devices, FIFOs, `.git`, control
characters, duplicates, sparse members, too many members and too much content, broken and truncated archives, and
the re-packing of a GitHub tarball without its top directory."""

from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import Callable

import pytest

from agent_runner.github import tarsafe
from agent_runner.github.tarsafe import Limits, UnsafeArchive

LIMITS = Limits(max_bytes=1024, max_entries=20)
Build = Callable[[tarfile.TarFile], None]


def member(
    name: str, kind: bytes = tarfile.REGTYPE, data: bytes = b"", **extra: object
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.mode = 0o755
    info.mtime = 1_700_000_000
    for key, value in extra.items():
        setattr(info, key, value)
    return info, data


def archive(*members: tuple[tarfile.TarInfo, bytes], compressed: bool = False, fmt: int = tarfile.PAX_FORMAT) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt, pax_headers={"comment": "c" * 40}) as out:
        for info, data in members:
            out.addfile(info, io.BytesIO(data) if info.isreg() else None)
    raw = buffer.getvalue()
    return gzip.compress(raw, mtime=0) if compressed else raw


def listing(raw: bytes, **kw: object) -> list[tuple[str, str, int, bytes]]:
    found: list[tuple[str, str, int, bytes]] = []
    for entry in tarsafe.entries(io.BytesIO(raw), limits=kw.pop("limits", LIMITS), **kw):  # type: ignore[arg-type]
        data = io.BytesIO()
        if entry.reader is not None:
            tarsafe.copy_exact(entry.reader, entry.size, data.write)
        found.append((entry.path, entry.kind, entry.size, data.getvalue()))
    return found


def refused(raw: bytes, **kw: object) -> str:
    with pytest.raises(UnsafeArchive) as caught:
        listing(raw, **kw)
    return caught.value.code


def test_regular_files_and_directories_pass() -> None:
    raw = archive(member("./", tarfile.DIRTYPE), member("./d", tarfile.DIRTYPE), member("./d/a.txt", data=b"abc"))
    assert listing(raw) == [("d", "dir", 0, b""), ("d/a.txt", "file", 3, b"abc")]


@pytest.mark.parametrize(
    "bad",
    [
        member("link", tarfile.SYMTYPE, linkname="/etc/passwd"),
        member("hard", tarfile.LNKTYPE, linkname="a"),
        member("dev", tarfile.CHRTYPE, devmajor=1, devminor=3),
        member("blk", tarfile.BLKTYPE, devmajor=8, devminor=0),
        member("fifo", tarfile.FIFOTYPE),
    ],
)
def test_links_devices_and_fifos_are_refused_or_skipped(bad: tuple[tarfile.TarInfo, bytes]) -> None:
    raw = archive(member("a", data=b"x"), bad)
    assert refused(raw) == "path_invalid"
    stats = tarsafe.Stats()
    assert listing(raw, links="skip", stats=stats) == [("a", "file", 1, b"x")]
    assert stats.skipped == 1


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../escape",
        "a/../../b",
        "a//b",
        "a/./b",
        ".git/config",
        "x/.GIT/hooks/pre-commit",
        "a\x01b",
        "a\x7f",
    ],
)
def test_unsafe_paths_are_refused(name: str) -> None:
    assert refused(archive(member(name, data=b"x"))) == "path_invalid"
    # a link with an unsafe name is refused before it could be skipped
    assert refused(archive(member(name, tarfile.SYMTYPE, linkname="x")), links="skip") == "path_invalid"


def test_gnu_long_names_are_checked_after_decoding() -> None:
    name = "d/" + "a" * 150 + "/../../../etc/x"
    assert refused(archive(member(name, data=b"x"), fmt=tarfile.GNU_FORMAT)) == "path_invalid"
    good = "d/" + "a" * 150
    assert listing(archive(member(good, data=b"x"), fmt=tarfile.GNU_FORMAT))[0][0] == good


def test_duplicates_and_sparse_members_are_refused() -> None:
    assert refused(archive(member("a", data=b"x"), member("a", data=b"y"))) == "archive_invalid"
    assert refused(archive(member("s", tarfile.GNUTYPE_SPARSE), fmt=tarfile.GNU_FORMAT)) == "archive_invalid"


def test_limits_on_members_and_bytes() -> None:
    many = archive(*(member(f"f{i}") for i in range(21)))
    assert refused(many) == "size_exceeded"
    big = archive(member("a", data=b"x" * 600), member("b", data=b"y" * 600))
    assert refused(big) == "size_exceeded"
    assert len(listing(big, limits=Limits(max_bytes=1200, max_entries=2))) == 2


def test_broken_and_truncated_archives() -> None:
    assert refused(b"no tar at all" * 100) == "archive_invalid"
    assert refused(b"not gzip", compressed=True) == "archive_invalid"
    raw = archive(member("a", data=b"x" * 1000), member("b", data=b"y" * 1000))
    assert refused(raw[:1700]) == "archive_invalid"
    corrupt = bytearray(raw)
    corrupt[1024 + 148 : 1024 + 156] = b"garbage!"  # header checksum of the first member (after the pax header)
    assert refused(bytes(corrupt)) == "archive_invalid"
    second = archive(member("a", data=b"x"), member("b" * 120, data=b"y"))
    broken = bytearray(second)
    start = second.index(b"path=" + b"b" * 120) // 512 * 512 + 512  # the header after the pax header of `bbb...`
    broken[start + 148 : start + 156] = b"garbage!"
    assert refused(bytes(broken)) == "archive_invalid"
    packed = bytearray(archive(*(member(f"m{i}", data=bytes(range(256)) * 3) for i in range(8)), compressed=True))
    packed[len(packed) // 2 : len(packed) // 2 + 64] = b"\xff" * 64  # the deflate stream breaks after some members
    assert refused(bytes(packed), compressed=True, limits=Limits(10_000, 20)) == "archive_invalid"


def test_copy_exact_copies_or_refuses_a_short_member() -> None:
    out = io.BytesIO()
    tarsafe.copy_exact(io.BytesIO(b"abcdef"), 4, out.write)
    assert out.getvalue() == b"abcd"
    with pytest.raises(UnsafeArchive):
        tarsafe.copy_exact(io.BytesIO(b"ab"), 4, io.BytesIO().write)

    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("gone")

    with pytest.raises(UnsafeArchive):
        tarsafe.copy_exact(Broken(), 1, io.BytesIO().write)


def github_tarball(*members: tuple[tarfile.TarInfo, bytes]) -> bytes:
    return archive(member("o-r-abc1234", tarfile.DIRTYPE), *members, compressed=True)


def test_repack_strips_the_top_directory_and_normalises_members() -> None:
    raw = github_tarball(
        member("o-r-abc1234/src", tarfile.DIRTYPE),
        member("o-r-abc1234/src/app.py", data=b"x = 1\n", uid=1000, uname="dev"),
        member("o-r-abc1234/run.sh", data=b"run\n", mode=0o775),
        member("o-r-abc1234/link", tarfile.SYMTYPE, linkname="/etc/passwd"),
    )
    out = io.BytesIO()
    stats = tarsafe.repack(io.BytesIO(raw), out, limits=LIMITS)
    assert (stats.files, stats.dirs, stats.bytes, stats.skipped) == (2, 1, 10, 1)
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as result:
        members = result.getmembers()
        assert [(m.name, m.type, m.mode, m.uid, m.mtime) for m in members] == [
            ("src", tarfile.DIRTYPE, 0o700, 0, 0),
            ("src/app.py", tarfile.REGTYPE, 0o600, 0, 0),
            ("run.sh", tarfile.REGTYPE, 0o600, 0, 0),
        ]
        reader = result.extractfile("src/app.py")
        assert reader is not None
        assert reader.read() == b"x = 1\n"


def test_repack_skips_the_names_claude_code_reads_as_instructions() -> None:
    """ATS-03 (B-09): a repository's own `.claude/`, `.mcp.json`, `CLAUDE.md` … never reach the workspace."""
    raw = github_tarball(
        member("o-r-abc1234/.claude", tarfile.DIRTYPE),
        member("o-r-abc1234/.claude/settings.json", data=b"{}"),
        member("o-r-abc1234/sub", tarfile.DIRTYPE),
        member("o-r-abc1234/sub/CLAUDE.md", data=b"obey"),
        member("o-r-abc1234/sub/claude.local.md", data=b"obey"),
        member("o-r-abc1234/AGENTS.md", data=b"obey"),
        member("o-r-abc1234/.mcp.json", data=b"{}"),
        member("o-r-abc1234/.Claude.json", data=b"{}"),
        member("o-r-abc1234/.gitconfig", data=b"[x]"),
        member("o-r-abc1234/sub/CLAUDE.md.txt", data=b"ok"),
    )
    out = io.BytesIO()
    stats = tarsafe.repack(io.BytesIO(raw), out, limits=LIMITS)
    assert (stats.files, stats.dirs, stats.skipped) == (1, 1, 8)
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as result:
        assert result.getnames() == ["sub", "sub/CLAUDE.md.txt"]
    assert tarsafe.protected("a/.CLAUDE/b")
    assert tarsafe.protected("CLAUDE.md")
    assert not tarsafe.protected("claude/md")
    assert not tarsafe.protected("a/b")


def test_repack_refuses_members_outside_the_top_directory() -> None:
    raw = github_tarball(member("other/x", data=b"x"))
    with pytest.raises(UnsafeArchive) as caught:
        tarsafe.repack(io.BytesIO(raw), io.BytesIO(), limits=LIMITS)
    assert caught.value.code == "path_invalid"
    top_file = archive(member("README", data=b"x"), compressed=True)
    with pytest.raises(UnsafeArchive) as caught:
        tarsafe.repack(io.BytesIO(top_file), io.BytesIO(), limits=LIMITS)
    assert caught.value.code == "path_invalid"


def test_repack_without_stripping_and_of_a_truncated_member() -> None:
    raw = archive(member("a", data=b"abc"))
    out = io.BytesIO()
    stats = tarsafe.repack(io.BytesIO(raw), out, limits=LIMITS, compressed=False, strip_top=False)
    assert stats.files == 1
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as result:
        assert result.getnames() == ["a"]
    long = archive(member("a", data=b"x" * 5000))
    with pytest.raises(UnsafeArchive) as caught:
        tarsafe.repack(
            io.BytesIO(long[:3000]), io.BytesIO(), limits=Limits(10_000, 5), compressed=False, strip_top=False
        )
    assert caught.value.code == "archive_invalid"


def test_exact_reader_stops_at_its_size_and_maps_read_errors() -> None:
    exact = tarsafe._Exact(io.BytesIO(b"abcdef"), 4)
    assert exact.read() == b"abcd"
    assert exact.read(10) == b""
    short = tarsafe._Exact(io.BytesIO(b"ab"), 4)
    assert short.read() == b"ab"
    with pytest.raises(UnsafeArchive):
        short.read()

    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise tarfile.ReadError("broken")

    with pytest.raises(UnsafeArchive):
        tarsafe._Exact(Broken(), 1).read(1)


def test_extended_header_bombs_are_refused_before_reading() -> None:
    bomb = archive(member("d/" + "a" * (tarsafe.MAX_HEADER + 10), data=b"x"))
    assert refused(bomb) == "size_exceeded"
    gnu = archive(member("d/" + "a" * (tarsafe.MAX_HEADER + 10), data=b"x"), fmt=tarfile.GNU_FORMAT)
    assert refused(gnu) == "size_exceeded"
    first_ok = archive(member("a", data=b"x"), member("d/" + "b" * (tarsafe.MAX_HEADER + 10), data=b"x"))
    assert refused(first_ok) == "size_exceeded"
    assert refused(archive(member("a\\b", data=b"x"))) == "path_invalid"
