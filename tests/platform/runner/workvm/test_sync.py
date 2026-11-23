"""U-RUN-workvm sync: archives of `workvm_sync` (docs/platform/spec/13-work-vm.md section 3; threat model
`docs/platform/security/work-vm-threat-model.md`, crafted archive from the VM). Real tar files in `tmp_path`: guest
exports, push tars and adversarial pack tars (links, devices, `..`, absolute and `.git` paths, protected names,
duplicates, too many members, oversize claims, bad extended headers)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_guest import policy
from agent_runner.errors import RunnerError
from agent_runner.github import tarsafe
from agent_runner.workvm import sync

NOW = 1_800_000_000.0
Extra = Callable[[tarfile.TarFile], None]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def add_file(archive: tarfile.TarFile, name: str, data: bytes, **attrs: Any) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    for key, value in attrs.items():
        setattr(info, key, value)
    archive.addfile(info, io.BytesIO(data))


def add_special(archive: tarfile.TarFile, name: str, kind: bytes, linkname: str = "") -> None:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    archive.addfile(info)


def add_dir(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    archive.addfile(info)


def manifest_of(files: Mapping[str, bytes], skipped: Sequence[dict[str, str]] = ()) -> dict[str, Any]:
    return {
        "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in sorted(files.items())],
        "skipped": list(skipped),
    }


def write_export(
    path: Path,
    files: Mapping[str, bytes],
    *,
    manifest: object = None,
    members: Mapping[str, bytes] | None = None,
    skipped: Sequence[dict[str, str]] = (),
    extra: Extra | None = None,
    fmt: int = tarfile.PAX_FORMAT,
) -> Path:
    """A guest export: the manifest first, then the files (`members` overrides what is actually stored)."""
    data = json.dumps(manifest if manifest is not None else manifest_of(files, skipped)).encode()
    with tarfile.open(path, mode="w", format=fmt) as archive:
        add_file(archive, sync.EXPORT_MANIFEST, data)
        for name, content in sorted((members if members is not None else files).items()):
            add_file(archive, name, content)
        if extra is not None:
            extra(archive)
    return path


def write_pack(
    path: Path,
    files: Mapping[str, bytes],
    excluded: Sequence[str] = (),
    *,
    listing: object = None,
    extra: Extra | None = None,
    first: Extra | None = None,
) -> Path:
    """A pack tar of the VM helper: `.workvm-pack.json` first, then the files; `extra` appends crafted members."""
    data = json.dumps(listing if listing is not None else {"files": sorted(files), "excluded": list(excluded)})
    with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        if first is not None:
            first(archive)
        add_file(archive, sync.PACK_MANIFEST, data.encode())
        for name, content in files.items():
            add_file(archive, name, content)
        if extra is not None:
            extra(archive)
    return path


def read_tar(path: Path) -> list[tuple[str, str, int, int, bytes]]:
    """(name, kind, mode, mtime, data) of every member, in order."""
    result = []
    with tarfile.open(path) as archive:
        for member in archive:
            handle = archive.extractfile(member) if member.isreg() else None
            data = handle.read() if handle is not None else b""
            result.append((member.name, "file" if member.isreg() else "dir", member.mode, int(member.mtime), data))
    return result


def refused(call: Callable[[], object]) -> tuple[str, str | None]:
    with pytest.raises(RunnerError) as caught:
        call()
    assert caught.value.code in sync.ERROR_CODES
    return caught.value.code, caught.value.name


INVALID = sync.INVALID
TOO_LARGE = sync.TOO_LARGE


# -- rules ---------------------------------------------------------------------------------------------------------


def test_protected_names_match_the_guest_policy() -> None:
    assert (policy.PROTECTED_DIRS | policy.PROTECTED_FILES) - {".git"} == tarsafe.PROTECTED_SEGMENTS


@pytest.mark.parametrize(
    ("path", "valid"),
    [
        ("a/b.txt", True),
        ("ü/ß", True),
        ("", False),
        ("x" * 4097, False),
        ("/abs", False),
        ("a\x01b", False),
        ("a\x7fb", False),
        ("a\\b", False),
        ("bad\udcff", False),
        ("a//b", False),
        ("a/./b", False),
        ("../x", False),
    ],
)
def test_valid_path(path: str, valid: bool) -> None:
    assert sync.valid_path(path) is valid


# -- read_export -----------------------------------------------------------------------------------------------------


def test_read_export_returns_the_manifest(tmp_path: Path) -> None:
    files = {"a.txt": b"alpha", "dir/b.bin": b"\x00\x01", "empty": b""}
    skipped = [{"path": "link", "reason": "symlink"}]
    export = sync.read_export(write_export(tmp_path / "e.tar", files, skipped=skipped))
    assert export.manifest == {path: sync.FileEntry(len(data), sha(data)) for path, data in files.items()}
    assert export.skipped == skipped


def test_read_export_accepts_an_empty_export(tmp_path: Path) -> None:
    export = sync.read_export(write_export(tmp_path / "e.tar", {}))
    assert export == sync.Export({}, [])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: [],
        lambda m: {"files": []},
        lambda m: {"files": {}, "skipped": []},
        lambda m: {"files": [], "skipped": {}},
        lambda m: {"files": ["a"], "skipped": []},
        lambda m: {"files": [{"path": "a", "size": 1}], "skipped": []},
        lambda m: {"files": [{"path": 1, "size": 1, "sha256": "0" * 64}], "skipped": []},
        lambda m: {"files": [{"path": "../a", "size": 1, "sha256": "0" * 64}], "skipped": []},
        lambda m: {"files": [{"path": "x/.git/a", "size": 1, "sha256": "0" * 64}], "skipped": []},
        lambda m: {"files": [{"path": "a", "size": 1, "sha256": "0" * 64}] * 2, "skipped": []},
        lambda m: {"files": [{"path": "a", "size": True, "sha256": "0" * 64}], "skipped": []},
        lambda m: {"files": [{"path": "a", "size": -1, "sha256": "0" * 64}], "skipped": []},
        lambda m: {"files": [{"path": "a", "size": sync.MAX_BYTES + 1, "sha256": "0" * 64}], "skipped": []},
        lambda m: {"files": [{"path": "a", "size": 1, "sha256": 7}], "skipped": []},
        lambda m: {"files": [{"path": "a", "size": 1, "sha256": "Z" * 64}], "skipped": []},
        lambda m: {"files": [], "skipped": ["x"]},
        lambda m: {"files": [], "skipped": [{"path": "x"}]},
        lambda m: {"files": [], "skipped": [{"path": "x", "reason": "other"}]},
        lambda m: {"files": [], "skipped": [{"path": 1, "reason": "special"}]},
    ],
)
def test_read_export_refuses_a_malformed_manifest(tmp_path: Path, mutate: Callable[[object], object]) -> None:
    tar = write_export(tmp_path / "e.tar", {}, manifest=mutate(None))
    assert refused(lambda: sync.read_export(tar)) == (INVALID, "manifest_invalid")


def test_read_export_bounds_the_manifest_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "MAX_ENTRIES", 1)
    many = {"a": b"1", "b": b"2"}
    tar = write_export(tmp_path / "e.tar", {}, manifest=manifest_of(many), members={})
    assert refused(lambda: sync.read_export(tar)) == (INVALID, "manifest_invalid")
    skips = [{"path": "x", "reason": "special"}, {"path": "y", "reason": "special"}]
    tar = write_export(tmp_path / "s.tar", {}, skipped=skips)
    assert refused(lambda: sync.read_export(tar)) == (INVALID, "manifest_invalid")


def test_read_export_refuses_a_bad_first_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "e.tar"
    with tarfile.open(path, mode="w") as archive:
        add_file(archive, "other.json", b"{}")
    assert refused(lambda: sync.read_export(path)) == (INVALID, "manifest_invalid")
    with tarfile.open(path, mode="w") as archive:
        add_file(archive, sync.EXPORT_MANIFEST, b"{not json")
    assert refused(lambda: sync.read_export(path)) == (INVALID, "manifest_invalid")
    with tarfile.open(path, mode="w") as archive:
        add_file(archive, sync.EXPORT_MANIFEST, b"\xff\xfe")
    assert refused(lambda: sync.read_export(path)) == (INVALID, "manifest_invalid")
    monkeypatch.setattr(sync, "MAX_MANIFEST_BYTES", 10)
    write_export(path, {"a": b"x"})
    assert refused(lambda: sync.read_export(path)) == (INVALID, "manifest_invalid")


@pytest.mark.parametrize(
    ("members", "extra"),
    [
        ({"a": b"x", "b": b"y"}, None),
        ({"a": b"xx"}, None),
        ({"a": b"z"}, None),
        ({}, None),
    ],
)
def test_read_export_refuses_members_that_do_not_match(
    tmp_path: Path, members: dict[str, bytes], extra: Extra | None
) -> None:
    tar = write_export(tmp_path / "e.tar", {"a": b"x"}, members=members, extra=extra)
    assert refused(lambda: sync.read_export(tar)) == (INVALID, "content_mismatch")


@pytest.mark.parametrize(
    "extra",
    [
        lambda a: add_dir(a, "d"),
        lambda a: add_special(a, "l", tarfile.SYMTYPE, "a"),
    ],
)
def test_read_export_refuses_anything_but_files(tmp_path: Path, extra: Extra) -> None:
    tar = write_export(tmp_path / "e.tar", {"a": b"x"}, extra=extra)
    assert refused(lambda: sync.read_export(tar)) == (INVALID, "archive_invalid")


def test_read_export_refuses_what_is_not_an_archive(tmp_path: Path) -> None:
    empty = tmp_path / "empty.tar"
    empty.write_bytes(b"")
    assert refused(lambda: sync.read_export(empty)) == (INVALID, "archive_invalid")
    garbage = tmp_path / "garbage.tar"
    garbage.write_bytes(b"x" * 1024 + b"\x01" * 512)
    assert refused(lambda: sync.read_export(garbage)) == (INVALID, "archive_invalid")
    no_members = tmp_path / "none.tar"
    no_members.write_bytes(b"\0" * 1024)
    assert refused(lambda: sync.read_export(no_members)) == (INVALID, "content_mismatch")


def test_read_export_refuses_a_truncated_archive(tmp_path: Path) -> None:
    tar = write_export(tmp_path / "e.tar", {"a": b"x" * 5000, "b": b"y"})
    raw = tar.read_bytes()
    cut = tmp_path / "cut.tar"
    cut.write_bytes(raw[: raw.index(b"x" * 100) + 1000])
    assert refused(lambda: sync.read_export(cut)) == (INVALID, "archive_invalid")
    second = tmp_path / "second.tar"
    second.write_bytes(raw[: raw.index(b"x" * 100) + 5000 + 100])
    assert refused(lambda: sync.read_export(second)) == (INVALID, "archive_invalid")


# -- plan_push and write_push_tar ------------------------------------------------------------------------------------


def test_plan_push_against_nothing_and_against_a_mirror() -> None:
    export = sync.Export({"a": sync.FileEntry(1, sha(b"a")), "b": sync.FileEntry(1, sha(b"b"))}, [])
    first = sync.plan_push(None, export)
    assert first == sync.PushPlan(["a", "b"], [], {"a": sha(b"a"), "b": sha(b"b")})
    later = sync.plan_push({"a": sha(b"a"), "b": sha(b"old"), "gone": sha(b"g")}, export)
    assert later == sync.PushPlan(["b"], ["gone"], first.manifest)


def test_write_push_tar_writes_deletions_directories_and_changed_files(tmp_path: Path) -> None:
    files = {"top.txt": b"top", "src/lib/m.c": b"int m;", "same": b"s"}
    tar = write_export(tmp_path / "e.tar", files)
    plan = sync.plan_push({"same": sha(b"s"), "old/x": sha(b"x")}, sync.read_export(tar))
    out = tmp_path / "push.tar"
    assert sync.write_push_tar(tar, plan, out, "", now=lambda: NOW) == (2, 9)
    members = read_tar(out)
    assert members[0] == (sync.DELETIONS, "file", 0o644, int(NOW), b'["old/x"]')
    assert members[1:] == [
        ("src", "dir", 0o755, int(NOW), b""),
        ("src/lib", "dir", 0o755, int(NOW), b""),
        ("src/lib/m.c", "file", 0o644, int(NOW), b"int m;"),
        ("top.txt", "file", 0o644, int(NOW), b"top"),
    ]


def test_write_push_tar_places_everything_under_the_prefix(tmp_path: Path) -> None:
    tar = write_export(tmp_path / "e.tar", {"a": b"1", sync.DELETIONS: b"[]"})
    plan = sync.plan_push({"b": sha(b"2")}, sync.read_export(tar))
    out = tmp_path / "push.tar"
    assert sync.write_push_tar(tar, plan, out, "repos/x") == (2, 3)
    names = [(name, kind, data) for name, kind, _mode, _mtime, data in read_tar(out)]
    assert names == [
        (sync.DELETIONS, "file", b'["repos/x/b"]'),
        ("repos", "dir", b""),
        ("repos/x", "dir", b""),
        ("repos/x/.workvm-deletions.json", "file", b"[]"),
        ("repos/x/a", "file", b"1"),
    ]


@pytest.mark.parametrize("prefix", ["../x", "/abs", "a/.git", ".claude", "x/CLAUDE.md"])
def test_write_push_tar_refuses_a_bad_prefix(tmp_path: Path, prefix: str) -> None:
    tar = write_export(tmp_path / "e.tar", {"a": b"1"})
    plan = sync.plan_push(None, sync.read_export(tar))
    assert refused(lambda: sync.write_push_tar(tar, plan, tmp_path / "o.tar", prefix)) == (INVALID, "prefix_invalid")


@pytest.mark.parametrize(
    "plan",
    [
        sync.PushPlan(["missing"], [], {}),
        sync.PushPlan([], ["../escape"], {}),
        sync.PushPlan([], [".git/config"], {}),
    ],
)
def test_write_push_tar_refuses_a_plan_that_does_not_fit(tmp_path: Path, plan: sync.PushPlan) -> None:
    tar = write_export(tmp_path / "e.tar", {"a": b"1"})
    assert refused(lambda: sync.write_push_tar(tar, plan, tmp_path / "o.tar", "")) == (INVALID, "content_mismatch")


def test_copy_member_refuses_changed_or_short_content() -> None:
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as archive:
        info = tarfile.TarInfo("a")
        info.size = 3
        with pytest.raises(RunnerError) as caught:
            sync._copy_member(archive, io.BytesIO(b"abc"), info, 0, sha(b"xyz"))
        assert caught.value.name == "content_mismatch"
        with pytest.raises(RunnerError) as caught:
            sync._copy_member(archive, io.BytesIO(b"ab"), info, 0, sha(b"abc"))
        assert caught.value.name == "archive_invalid"


def test_hashing_reader_reads_everything_at_once() -> None:
    reader = sync._Hashing(io.BytesIO(b"abcdef"), 4)
    assert reader.read() == b"abcd"
    assert reader.read(10) == b""
    assert reader.hexdigest() == sha(b"abcd")


def test_read_exact_refuses_short_and_failing_readers() -> None:
    class Failing(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise tarfile.ReadError("broken")

    with pytest.raises(RunnerError) as caught:
        sync._read_exact(io.BytesIO(b"ab"), 3, sync._discard)
    assert caught.value.name == "archive_invalid"
    with pytest.raises(RunnerError) as caught:
        sync._read_exact(Failing(), 3, sync._discard)
    assert caught.value.name == "archive_invalid"


# -- pack tars (untrusted) -------------------------------------------------------------------------------------------


def merge(
    tmp_path: Path,
    workspace: Mapping[str, bytes],
    pack: Path,
    pushed: Mapping[str, str] | None = None,
) -> tuple[sync.MergeResult, list[tuple[str, str, int, int, bytes]]]:
    export = write_export(tmp_path / "ws.tar", workspace)
    out = tmp_path / "merged.tar"
    result = sync.merge_from_vm(export, pack, pushed, out, now=lambda: NOW)
    return result, read_tar(out)


def contents(members: list[tuple[str, str, int, int, bytes]]) -> dict[str, bytes | None]:
    return {name: (data if kind == "file" else None) for name, kind, _mode, _mtime, data in members}


@pytest.mark.parametrize(
    "extra",
    [
        lambda a: add_file(a, "../x", b"1"),
        lambda a: add_file(a, "/etc/passwd", b"1"),
        lambda a: add_file(a, ".git/config", b"1"),
        lambda a: add_file(a, "sub/.GIT/hooks/post-commit", b"1"),
        lambda a: add_special(a, "../link", tarfile.SYMTYPE, "/etc"),
        lambda a: add_file(a, "a\\b", b"1"),
        lambda a: add_file(a, "a\nb", b"1"),
        lambda a: add_file(a, "a//b", b"1"),
        lambda a: add_file(a, "x" * 4097, b"1"),
        lambda a: add_file(a, "ok", b"1", pax_headers={"path": "bad\udcff"}),
    ],
)
def test_pack_with_an_invalid_name_is_refused(tmp_path: Path, extra: Extra) -> None:
    pack = write_pack(tmp_path / "p.tar", {"ok.txt": b"1"}, extra=extra)
    assert refused(lambda: merge(tmp_path, {}, pack)) == (INVALID, "path_invalid")


def test_pack_with_a_duplicate_is_refused(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p.tar", {"a": b"1"}, extra=lambda a: add_file(a, "./a", b"2"))
    assert refused(lambda: merge(tmp_path, {}, pack)) == (INVALID, "archive_invalid")
    pack = write_pack(tmp_path / "q.tar", {"a": b"1"}, extra=lambda a: add_dir(a, "a"))
    assert refused(lambda: merge(tmp_path, {}, pack)) == (INVALID, "archive_invalid")


def test_pack_with_a_sparse_member_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p.tar"
    data = json.dumps({"files": ["s"], "excluded": []}).encode()
    with tarfile.open(path, mode="w", format=tarfile.GNU_FORMAT) as archive:
        add_file(archive, sync.PACK_MANIFEST, data)
        add_special(archive, "s", tarfile.GNUTYPE_SPARSE)
    assert refused(lambda: merge(tmp_path, {}, path)) == (INVALID, "archive_invalid")


@pytest.mark.parametrize(
    "extra",
    [
        lambda a: add_dir(a, "f/sub"),
        lambda a: add_file(a, "f/x", b"1"),
    ],
)
def test_pack_with_entries_below_a_file_is_refused(tmp_path: Path, extra: Extra) -> None:
    listing = {"files": ["f", "f/x"], "excluded": []}
    pack = write_pack(tmp_path / "p.tar", {"f": b"1"}, listing=listing, extra=extra)
    assert refused(lambda: merge(tmp_path, {}, pack)) == (INVALID, "path_invalid")


def test_pack_with_too_many_members_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "MAX_ENTRIES", 2)
    listing = {"files": ["a"], "excluded": []}
    pack = write_pack(tmp_path / "p.tar", {"a": b"1", "b": b"2", "c": b"3"}, listing=listing)
    assert refused(lambda: merge(tmp_path, {}, pack)) == (TOO_LARGE, "size_exceeded")


def test_pack_with_too_much_content_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "MAX_BYTES", 8)
    pack = write_pack(tmp_path / "p.tar", {"a": b"12345", "b": b"6789"})
    assert refused(lambda: merge(tmp_path, {}, pack)) == (TOO_LARGE, "size_exceeded")
    monkeypatch.setattr(sync, "MAX_MANIFEST_BYTES", 40)
    pack = write_pack(tmp_path / "q.tar", {"a": b"x" * 30, "b": b"y" * 30})
    assert refused(lambda: merge(tmp_path, {}, pack)) == (TOO_LARGE, "size_exceeded")


def test_pack_claiming_a_huge_member_is_refused(tmp_path: Path) -> None:
    listing = json.dumps({"files": ["big"], "excluded": []}).encode()
    head = tarfile.TarInfo(sync.PACK_MANIFEST)
    head.size = len(listing)
    huge = tarfile.TarInfo("big")
    huge.size = sync.MAX_BYTES + sync.MAX_MANIFEST_BYTES + 1
    path = tmp_path / "huge.tar"
    padding = b"\0" * (-len(listing) % tarfile.BLOCKSIZE)
    path.write_bytes(head.tobuf(tarfile.PAX_FORMAT) + listing + padding + huge.tobuf(tarfile.PAX_FORMAT))
    assert refused(lambda: merge(tmp_path, {}, path)) == (TOO_LARGE, "size_exceeded")


def test_pack_with_an_oversize_extended_header_is_refused(tmp_path: Path) -> None:
    big = {"comment": "c" * (tarsafe.MAX_HEADER + 1)}
    pack = write_pack(tmp_path / "p.tar", {"a": b"1"}, extra=lambda a: add_file(a, "b", b"2", pax_headers=big))
    assert refused(lambda: merge(tmp_path, {}, pack)) == (TOO_LARGE, "size_exceeded")
    first = write_pack(tmp_path / "f.tar", {}, first=lambda a: add_file(a, "b", b"2", pax_headers=big))
    assert refused(lambda: merge(tmp_path, {}, first)) == (TOO_LARGE, "size_exceeded")


def test_pack_with_a_broken_extended_header(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p.tar", {"a": b"1"}, first=lambda a: add_file(a, "c" * 120, b"2"), extra=None)
    raw = bytearray(pack.read_bytes())
    start = raw.index(b"path=" + b"c" * 120)
    raw[start - 4 : start] = b"zzz "  # the record length of the first pax header
    broken = tmp_path / "broken.tar"
    broken.write_bytes(bytes(raw))
    assert refused(lambda: merge(tmp_path, {}, broken)) == (INVALID, "archive_invalid")
    # A broken header after the first member ends the archive there (tarfile): the listed files that follow count
    # as skipped and their workspace copies are kept, never deleted.
    pack = write_pack(tmp_path / "q.tar", {"a": b"1", "b" * 120: b"2"})
    raw = bytearray(pack.read_bytes())
    start = raw.index(b"path=" + b"b" * 120)
    raw[start - 4 : start] = b"zzz "
    broken.write_bytes(bytes(raw))
    workspace = {"b" * 120: b"2"}
    result, members = merge(tmp_path, workspace, broken, {"b" * 120: sha(b"2")})
    assert result == sync.MergeResult(files=1, bytes=1, deleted=0, skipped=1)
    assert contents(members) == {"a": b"1", "b" * 120: b"2"}


@pytest.mark.parametrize(
    ("listing", "code"),
    [
        ([], (INVALID, "manifest_invalid")),
        ({"files": []}, (INVALID, "manifest_invalid")),
        ({"files": {}, "excluded": []}, (INVALID, "manifest_invalid")),
        ({"files": [], "excluded": {}}, (INVALID, "manifest_invalid")),
        ({"files": [1], "excluded": []}, (INVALID, "manifest_invalid")),
        ({"files": ["../a"], "excluded": []}, (INVALID, "manifest_invalid")),
        ({"files": [], "excluded": [None]}, (INVALID, "manifest_invalid")),
        ({"files": [], "excluded": ["/abs/"]}, (INVALID, "manifest_invalid")),
        ({"files": [], "excluded": ["a", "b", "c"]}, (TOO_LARGE, "size_exceeded")),
        ({"files": ["a", "b", "c"], "excluded": []}, (TOO_LARGE, "size_exceeded")),
    ],
)
def test_pack_with_a_malformed_listing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: object, code: tuple[str, str]
) -> None:
    monkeypatch.setattr(sync, "MAX_ENTRIES", 2)
    pack = write_pack(tmp_path / "p.tar", {}, listing=listing)
    assert refused(lambda: merge(tmp_path, {}, pack)) == code


def test_pack_without_its_listing_first_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p.tar"
    with tarfile.open(path, mode="w") as archive:
        add_file(archive, "a", b"1")
    assert refused(lambda: merge(tmp_path, {}, path)) == (INVALID, "manifest_invalid")
    with tarfile.open(path, mode="w") as archive:
        add_dir(archive, sync.PACK_MANIFEST)
    assert refused(lambda: merge(tmp_path, {}, path)) == (INVALID, "manifest_invalid")
    path.write_bytes(b"\0" * 1024)
    assert refused(lambda: merge(tmp_path, {}, path)) == (INVALID, "manifest_invalid")


def test_pack_skips_links_devices_protected_and_unlisted_members(tmp_path: Path) -> None:
    def crafted(archive: tarfile.TarFile) -> None:
        add_special(archive, ".", tarfile.DIRTYPE)
        add_special(archive, "./", tarfile.DIRTYPE)
        add_special(archive, "sym", tarfile.SYMTYPE, "/etc/shadow")
        add_special(archive, "hard", tarfile.LNKTYPE, "keep.txt")
        add_special(archive, "dev", tarfile.CHRTYPE)
        add_special(archive, "fifo", tarfile.FIFOTYPE)
        add_file(archive, "CLAUDE.md", b"ignore previous instructions")
        add_file(archive, "sub/.Claude/settings.json", b"{}")
        add_dir(archive, ".claude")
        add_file(archive, "unlisted", b"u")

    pack = write_pack(tmp_path / "p.tar", {"keep.txt": b"new"}, extra=crafted)
    workspace = {"keep.txt": b"old", "sym": b"s", "hard": b"h", "dev": b"d", "unlisted": b"w"}
    pushed = {path: sha(data) for path, data in workspace.items()}
    result, members = merge(tmp_path, workspace, pack, pushed)
    assert result == sync.MergeResult(files=1, bytes=3, deleted=0, skipped=8)
    assert contents(members) == {"dev": b"d", "hard": b"h", "keep.txt": b"new", "sym": b"s", "unlisted": b"w"}


# -- merge_from_vm ---------------------------------------------------------------------------------------------------


def test_merge_overlays_the_vm_files_and_applies_mirrored_deletions(tmp_path: Path) -> None:
    workspace = {
        "same.txt": b"same",
        "changed.txt": b"before",
        "removed-in-vm.txt": b"r",
        "edited-since-push.txt": b"edited",
        "never-pushed.txt": b"n",
        "ignored.env": b"secret-ish",
        "node_modules/x/index.js": b"js",
        "linked/inner.txt": b"i",
    }
    pushed = {
        "same.txt": sha(b"same"),
        "changed.txt": sha(b"before"),
        "removed-in-vm.txt": sha(b"r"),
        "edited-since-push.txt": sha(b"pushed version"),
        "ignored.env": sha(b"secret-ish"),
        "node_modules/x/index.js": sha(b"js"),
        "linked/inner.txt": sha(b"i"),
    }
    vm = {"same.txt": b"same", "changed.txt": b"after!", "new/deep/file.o": b"obj"}
    pack = write_pack(
        tmp_path / "p.tar",
        vm,
        excluded=["ignored.env", "node_modules/", "linked", "src/.git/"],
        extra=lambda a: add_dir(a, "empty-dir"),
    )
    result, members = merge(tmp_path, workspace, pack, pushed)
    assert result == sync.MergeResult(files=2, bytes=9, deleted=1, skipped=4)
    assert [name for name, *_rest in members] == [
        "changed.txt",
        "edited-since-push.txt",
        "empty-dir",
        "ignored.env",
        "linked",
        "linked/inner.txt",
        "never-pushed.txt",
        "new",
        "new/deep",
        "new/deep/file.o",
        "node_modules",
        "node_modules/x",
        "node_modules/x/index.js",
        "same.txt",
    ]
    assert all(mtime == int(NOW) for _name, _kind, _mode, mtime, _data in members)
    assert {(kind, mode) for _name, kind, mode, _mtime, _data in members} == {("file", 0o600), ("dir", 0o700)}
    merged = contents(members)
    assert merged["changed.txt"] == b"after!"
    assert merged["edited-since-push.txt"] == b"edited"
    assert merged["ignored.env"] == b"secret-ish"


def test_merge_without_a_mirror_keeps_every_workspace_file(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p.tar", {"b": b"vm"})
    result, members = merge(tmp_path, {"a": b"ws"}, pack, None)
    assert result == sync.MergeResult(files=1, bytes=2, deleted=0, skipped=0)
    assert contents(members) == {"a": b"ws", "b": b"vm"}


def test_merge_lets_the_vm_win_where_types_collide(tmp_path: Path) -> None:
    workspace = {"was-file": b"f", "was-dir/inner": b"i", "empty": b"e"}
    vm = {"was-file/now-inside": b"x", "was-dir": b"now a file"}
    pack = write_pack(tmp_path / "p.tar", vm, extra=lambda a: add_dir(a, "empty"))
    result, members = merge(tmp_path, workspace, pack, {})
    assert result == sync.MergeResult(files=2, bytes=11, deleted=3, skipped=0)
    assert contents(members) == {
        "empty": None,
        "was-dir": b"now a file",
        "was-file": None,
        "was-file/now-inside": b"x",
    }


def test_merge_counts_listed_files_missing_from_the_pack_as_skipped(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p.tar", {}, listing={"files": ["vanished"], "excluded": []})
    result, members = merge(tmp_path, {"vanished": b"v"}, pack, {"vanished": sha(b"v")})
    assert result == sync.MergeResult(files=0, bytes=0, deleted=0, skipped=1)
    assert contents(members) == {"vanished": b"v"}


def test_merge_respects_the_guest_import_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "MAX_BYTES", 15)
    pack = write_pack(tmp_path / "p.tar", {"vm": b"0123456789"})
    assert refused(lambda: merge(tmp_path, {"ws": b"0123456789"}, pack)) == (TOO_LARGE, "size_exceeded")
    monkeypatch.setattr(sync, "MAX_BYTES", 512)
    monkeypatch.setattr(sync, "MAX_ENTRIES", 3)
    pack = write_pack(tmp_path / "q.tar", {"d/vm": b"1", "e": b"2"})
    assert refused(lambda: merge(tmp_path, {"ws": b"3"}, pack)) == (TOO_LARGE, "size_exceeded")


def test_merge_refuses_a_workspace_export_that_does_not_match(tmp_path: Path) -> None:
    export = write_export(tmp_path / "ws.tar", {"a": b"1"}, members={"a": b"2"})
    pack = write_pack(tmp_path / "p.tar", {})
    with pytest.raises(RunnerError) as caught:
        sync.merge_from_vm(export, pack, None, tmp_path / "o.tar")
    assert caught.value.name == "content_mismatch"


def test_covered_by_exclusions() -> None:
    pack = sync._Pack(excluded={"link", "file"}, excluded_dirs={"ignored"})
    assert sync._covered("file", pack)
    assert sync._covered("ignored", pack)
    assert sync._covered("ignored/a/b", pack)
    assert sync._covered("link/x", pack)
    assert not sync._covered("other/x", pack)
