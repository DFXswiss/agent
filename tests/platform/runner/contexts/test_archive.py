"""U-RUN-contexts: the context archive on the host - untrusted-archive checks, `context.json`, gzip pieces and their
inverse (ADR-072 sections 2 and 3; review KOR-33 nos. 5, 8, 11, 13, 20, 21)."""

from __future__ import annotations

import io
import json
import random
import tarfile
import zlib
from typing import Any

import pytest

from agent_runner.contexts import archive
from agent_runner.contexts.archive import ArchiveInvalid, Inflater, compress_chunks, inspect, limits_for, parse_context
from agent_runner.crypto.context import CHUNK_BYTES
from agent_runner.github import tarsafe

from ..prompts.kit import CLAUDE_ID
from .kit import CONVERSATION, SLUG, TURN, context_json, tar_bytes
from .kit import archive as make_archive

LIMITS = limits_for(1024 * 1024, 1024 * 1024)


def check(data: bytes, limits: Any = LIMITS) -> archive.Context:
    return inspect(io.BytesIO(data), limits)


def test_a_complete_archive_passes_and_yields_its_context() -> None:
    found = check(make_archive(work={"src/a.py": b"x", "README.md": b"y"}))
    assert found.claude_session_id == CLAUDE_ID
    assert found.workspace == "included"
    assert (found.workspace_files, found.workspace_bytes) == (1, 5)
    assert (found.workspace_omitted, found.workspace_state) == (0, "full")
    assert limits_for(1, 2).max_bytes == 3 + archive.CONTEXT_JSON_MAX


def test_the_archive_bound_follows_from_the_entry_limit() -> None:
    """Review no. 21: many small files need header room beyond their content."""
    limits = tarsafe.Limits(max_bytes=1000, max_entries=3000)
    assert archive.archive_bytes(limits) == 1000 + 3002 * archive.ENTRY_OVERHEAD_BYTES
    files = {f"d/{index:05d}-{'n' * 200}.txt": b"x" for index in range(2000)}
    data = make_archive(work=files, workspace_files=2000, workspace_bytes=2000)
    pieces: list[bytes] = []
    compress_chunks(io.BytesIO(data), lambda _i, piece: pieces.append(piece))
    out = io.BytesIO()
    inflater = Inflater(out, archive.archive_bytes(tarsafe.Limits(max_bytes=1024 * 1024, max_entries=2100)))
    for piece in pieces:
        inflater.feed(piece)
    assert inflater.finish() == len(data) > 2000 * 512


def test_workspace_states_and_omissions() -> None:
    """Review no. 5: what the export left out is counted and named as `partial`."""
    for workspace, omitted, state in (
        ("included", 2, "partial"),
        ("empty", 1, "partial"),
        ("empty", 0, "empty"),
        ("omitted_too_large", 0, "omitted"),
    ):
        found = parse_context(json.dumps(context_json(workspace=workspace, workspace_omitted=omitted)).encode())
        assert (found.workspace_omitted, found.workspace_state) == (omitted, state)
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        parse_context(json.dumps(context_json(workspace_omitted=-1)).encode())


def link_archive(path: str, target: str) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as made:
        context = json.dumps(context_json()).encode()
        info = tarfile.TarInfo("context.json")
        info.size = len(context)
        made.addfile(info, io.BytesIO(context))
        conversation = tarfile.TarInfo(CONVERSATION)
        conversation.size = len(TURN)
        made.addfile(conversation, io.BytesIO(TURN))
        link = tarfile.TarInfo(path)
        link.type = tarfile.SYMTYPE
        link.linkname = target
        made.addfile(link)
    return out.getvalue()


def test_links_inside_the_workspace_pass_and_count_in_the_content_hash() -> None:
    first = check(link_archive("work/src/link", "../a.txt"))
    second = check(link_archive("work/src/link", "b.txt"))
    assert first.content_sha256 != second.content_sha256
    assert check(link_archive("work/top", ".")).content_sha256


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("work/link", "/etc/passwd"),
        ("work/link", "../outside"),
        ("work/a/link", "../../outside"),
        ("work/link", ""),
        ("work/link", "x" * 5000),
        ("transcript/link", "a"),
    ],
)
def test_links_leaving_the_workspace_or_in_the_transcript_are_refused(path: str, target: str) -> None:
    with pytest.raises(ArchiveInvalid, match=r"path_invalid|archive_invalid"):
        check(link_archive(path, target))


def test_the_content_hash_ignores_context_json_but_sees_content_and_execute_bits() -> None:
    """Review no. 11: two exports of an unchanged conversation differ only in `created_at`."""
    base = check(make_archive())
    again = check(make_archive(created_at="2026-11-22T00:00:00.000Z"))
    assert base.content_sha256 == again.content_sha256
    assert check(make_archive(work={"notes.txt": b"other"})).content_sha256 != base.content_sha256
    executable = tar_bytes(
        [("context.json", json.dumps(context_json()).encode()), (CONVERSATION, TURN), ("work/notes.txt", b"hello")]
    )
    plain = check(executable).content_sha256
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as made:
        for name, data, mode in (
            ("context.json", json.dumps(context_json()).encode(), 0o644),
            (CONVERSATION, TURN, 0o644),
            ("work/notes.txt", b"hello", 0o755),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            made.addfile(info, io.BytesIO(data))
    assert check(out.getvalue()).content_sha256 != plain


@pytest.mark.parametrize(
    ("members", "code"),
    [
        ([], "context_invalid"),
        ([("work/a", b"x")], "context_invalid"),  # context.json is not first
        ([("context.json", None)], "context_invalid"),  # a directory
        ([("context.json", b"x" * (archive.CONTEXT_JSON_MAX + 1))], "context_invalid"),
        ([("context.json", json.dumps(context_json()).encode()), ("other/a", b"x")], "context_invalid"),
        ([("context.json", json.dumps(context_json()).encode()), ("work", b"x")], "context_invalid"),
        ([("context.json", json.dumps(context_json()).encode()), ("work/CLAUDE.md", b"x")], "path_invalid"),
        ([("context.json", json.dumps(context_json()).encode()), ("work/.git/config", b"x")], "path_invalid"),
        ([("context.json", json.dumps(context_json()).encode()), ("work/../x", b"x")], "path_invalid"),
    ],
)
def test_refused_archives(members: list[tuple[str, bytes | None]], code: str) -> None:
    with pytest.raises(ArchiveInvalid) as refused:
        check(tar_bytes(members))
    assert refused.value.code == code


def test_garbage_hard_links_duplicates_and_limits_are_refused() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as made:
        context = json.dumps(context_json()).encode()
        info = tarfile.TarInfo("context.json")
        info.size = len(context)
        made.addfile(info, io.BytesIO(context))
        hard = tarfile.TarInfo("work/hard")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "work/a"
        made.addfile(hard)
    with pytest.raises(ArchiveInvalid, match="path_invalid"):
        check(out.getvalue())
    twice = io.BytesIO()
    with tarfile.open(fileobj=twice, mode="w", format=tarfile.PAX_FORMAT) as made:
        for _ in range(2):
            link = tarfile.TarInfo("work/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "a"
            made.addfile(link)
    with pytest.raises(tarsafe.UnsafeArchive, match="archive_invalid"):
        list(tarsafe.entries(io.BytesIO(twice.getvalue()), limits=LIMITS, links="symlinks"))
    with pytest.raises(ArchiveInvalid, match="archive_invalid"):
        check(b"not a tar at all" * 100)
    with pytest.raises(ArchiveInvalid, match="size_exceeded"):
        check(make_archive(work={"big": b"x" * 100}), tarsafe.Limits(max_bytes=50, max_entries=100))


@pytest.mark.parametrize(
    "change",
    [
        {"v": 2},
        {"v": True},
        {"extra": 1},
        {"transcript_files": "x"},
        {"transcript_files": [1]},
        {"workspace": "partial"},
        {"workspace_omitted": "3"},
        {"agent_kind": ""},
        {"agent_version": "bad version"},
        {"claude_session_id": "ABC"},
        {"session_id": 5},
        {"created_at": "yesterday"},
        {"workspace_files": -1},
        {"workspace_bytes": True},
        {"cwd": "x" * 5000},
    ],
)
def test_context_json_is_checked(change: dict[str, Any]) -> None:
    value = {**context_json(), **change}
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        parse_context(json.dumps(value).encode())


def test_context_json_must_be_an_object_of_utf8_json() -> None:
    for raw in (b"\xff", b"[1]", b"{", b"[" * 100_000):
        with pytest.raises(ArchiveInvalid, match="context_invalid"):
            parse_context(raw)
    missing = context_json()
    del missing["cwd"]
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        parse_context(json.dumps(missing).encode())


def roundtrip(data: bytes, max_bytes: int | None = None) -> tuple[int, bytes]:
    pieces: list[bytes] = []

    def emit(index: int, piece: bytes) -> None:
        assert index == len(pieces)
        pieces.append(piece)

    count = compress_chunks(io.BytesIO(data), emit)
    assert count == len(pieces)
    assert all(len(piece) == CHUNK_BYTES for piece in pieces[:-1])
    assert 0 < len(pieces[-1]) <= CHUNK_BYTES
    out = io.BytesIO()
    inflater = Inflater(out, max_bytes if max_bytes is not None else len(data))
    for piece in pieces:
        inflater.feed(piece)
    assert inflater.finish() == len(data)
    return count, out.getvalue()


def test_small_and_large_archives_are_chunked_and_restored() -> None:
    assert roundtrip(b"") == (1, b"")
    assert roundtrip(b"abc" * 1000)[1] == b"abc" * 1000
    noise = random.Random(7).randbytes(5 * CHUNK_BYTES + 12345)  # incompressible: six pieces
    count, back = roundtrip(noise)
    assert count == 6
    assert back == noise


def test_data_just_over_one_piece_gives_two_pieces() -> None:
    noise = random.Random(3).randbytes(CHUNK_BYTES)
    count, back = roundtrip(noise)
    assert back == noise
    assert count == 2


def test_the_inflater_refuses_bombs_truncation_trailing_data_and_garbage() -> None:
    data = b"\0" * (4 * 1024 * 1024)
    pieces: list[bytes] = []
    compress_chunks(io.BytesIO(data), lambda _i, piece: pieces.append(piece))
    with pytest.raises(ArchiveInvalid, match="size_exceeded"):
        Inflater(io.BytesIO(), 1024).feed(pieces[0])
    truncated = Inflater(io.BytesIO(), len(data))
    truncated.feed(pieces[0][:-8])
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        truncated.finish()
    trailing = Inflater(io.BytesIO(), len(data))
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        trailing.feed(pieces[0] + b"junk")
    after_end = Inflater(io.BytesIO(), len(data))
    after_end.feed(pieces[0])
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        after_end.feed(b"more")
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        Inflater(io.BytesIO(), 10).feed(b"definitely not gzip")
    plain = zlib.compress(b"x")  # zlib, not gzip
    with pytest.raises(ArchiveInvalid, match="context_invalid"):
        Inflater(io.BytesIO(), 10).feed(plain)


def test_transcript_lines_are_not_refused_and_their_highest_version_is_kept() -> None:
    """Review no. 13: a damaged line never blocks a checkpoint or a restore; review no. 20: absurd versions and
    nesting are ignored."""
    lines = b'{"version":"2.1.300"}\n[1]\n{"version":"2.1.331","x":1}\n{"version":5}\n{"version":"dev"}\n'
    assert check(make_archive(transcript=TURN + lines)).max_version == (2, 1, 331)
    assert check(make_archive()).max_version is None
    for damaged in (b'{"version":"2.1.300"}\nnot json\n', b'{"version":"2.1.300"}', b"[" * 100_000 + b"\n"):
        assert check(make_archive(transcript=TURN + damaged)).max_version in ((2, 1, 300), None)
    huge = b'{"version":"' + b"9" * 5000 + b'.1"}\n'
    assert check(make_archive(transcript=TURN + huge)).max_version is None
    long_line = b'{"version":"2.1.400","pad":"' + b"x" * (3 * 1024 * 1024) + b'"}\n'
    assert check(make_archive(transcript=TURN + long_line), limits_for(8 << 20, 1 << 20)).max_version == (2, 1, 400)
    assert archive.version_key("2.0.76 (Claude Code)") == (2, 0, 76)
    assert archive.version_key(None) is None
    assert archive.version_key("1234567890.1") is None


@pytest.mark.parametrize(
    "transcript",
    [
        b"",
        b'{"type":"user","message":{"role":"user","content":"hello"}}\n',
        b'{"type":"assistant","message":{"role":"assistant","content":[]}}\n',
        b'{"type":"summary"}\n[1]\n{"type":["user"]}\nnot json\n',
    ],
)
def test_a_conversation_without_a_complete_turn_is_incomplete(transcript: bytes) -> None:
    """KOR-33 E2E: an idle checkpoint taken before Claude Code wrote the turn held 545-623 bytes and failed the
    restored start; such an archive is never sealed or restored."""
    with pytest.raises(ArchiveInvalid, match="context_incomplete"):
        check(make_archive(transcript=transcript))


def test_only_the_conversation_file_itself_counts_for_the_turn() -> None:
    """A turn in a subagent file or in another conversation's file does not make the archive complete."""
    sub = (f"transcript/projects/{SLUG}/{CLAUDE_ID}/subagents/agent-1.jsonl", TURN)
    with pytest.raises(ArchiveInvalid, match="context_incomplete"):
        check(make_archive(transcript=b'{"type":"user"}\n', extra=[sub]))
    assert check(make_archive(extra=[sub])).claude_session_id == CLAUDE_ID
    assert archive.cwd_key("/srv/sessions/a.b/work") == "-srv-sessions-a-b-work"
