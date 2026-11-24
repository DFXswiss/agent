"""U-RUN-github: `github.checkout` (05-runner.md section 12.1, 03-api.md section 9) through the operation pipeline
against the GitHub double and the real guest file code: the exact REST sequence, the resolved ref as dedupe field, the
workspace content (regular files only, links skipped, modes 0600/0700), refs by branch, tag and short SHA, refusals of
the pre-check, size limits, unsafe archives, guest failures and the scratch directory."""

from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.crypto.ids import uuid7
from agent_runner.github import checkout, snapshot
from agent_runner.github.client import GitHubOptions
from agent_runner.guestlink.errors import GuestProtocolError, GuestRefused, GuestUnreachable
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.sessions import records as session_records

from ..sync.fakehub import CONTRACTS
from .conftest import REPO, GitHub
from .run10_kit import BINARY, R10, MockDouble, refused, run10, world

SCHEMAS = SchemaSet(CONTRACTS)


def head(github: GitHub, branch: str = "main") -> str:
    found = github.double.world.repo("octo-org", "hello")
    assert found is not None
    sha: str = found.refs[f"refs/heads/{branch}"]
    return sha


def test_checkout_of_the_default_branch_fills_the_workspace(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    sha = head(github)

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.checkout", {"repo": REPO})
        assert op.state == "succeeded", op.answer
        assert op.result == {
            "repo": REPO,
            "commit_sha": sha,
            "dir": "repos/octo-org/hello",
            "files": 4,
            "bytes": len(b"hello\nworld\n") + len(b"x = 1\n") + len(b"echo run\n") + len(BINARY),
        }
        assert SCHEMAS.errors("ops/github.checkout.result.json", op.result) == []
        assert r.journal() == [
            f"GET /repos/{REPO}",
            f"GET /repos/{REPO}/commits/main",
            f"GET /repos/{REPO}/tarball/{sha}",
            f"GET /codeload/{REPO}/legacy.tar.gz/{sha}",
        ]
        assert r.ok.hub.requests[0]["dedupe"] is not None
        assert op.checked is not None
        assert op.checked["data"] == {"commit_sha": sha}
        root = r.guest.work(sid) / "repos" / "octo-org" / "hello"
        assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == [
            "README.md",
            "logo.bin",
            "run.sh",
            "src",
            "src/app.py",
        ]
        assert (root / "logo.bin").read_bytes() == BINARY
        assert stat.S_IMODE((root / "run.sh").stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "src").stat().st_mode) == 0o700
        assert r.guest.imports == [(sid, "repos/octo-org/hello", True)]
        assert r.scratch() == []
        assert f"runner.github checkout operation_id={op.id} files=4 skipped=1" in r.ok.lines

    run10(store_dsn, github, tmp_path, body)


def test_checkout_by_branch_tag_and_short_sha_and_again_replaces(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    data = world()
    data["repositories"][0]["tags"] = {"v1.0": "feature"}
    github = start_github(world=data)
    feature = head(github, "feature")

    async def body(r: R10) -> None:
        sid = await r.session()
        by_branch = await r.run(sid, "github.checkout", {"repo": "Octo-Org/Hello", "ref": "refs/heads/feature"})
        assert by_branch.state == "succeeded", by_branch.answer
        assert by_branch.result is not None
        assert (by_branch.result["commit_sha"], by_branch.result["dir"]) == (feature, "repos/octo-org/hello")
        root = r.guest.work(sid) / "repos" / "octo-org" / "hello"
        assert (root / "src" / "app.py").read_text() == "x = 2\n"
        (root / "extra.txt").write_text("gone after the next checkout")
        by_tag = await r.run(sid, "github.checkout", {"repo": REPO, "ref": "v1.0"})
        assert by_tag.result is not None
        assert by_tag.result["commit_sha"] == feature
        assert not (root / "extra.txt").exists()
        by_sha = await r.run(sid, "github.checkout", {"repo": REPO, "ref": head(github)[:10]})
        assert by_sha.result is not None
        assert by_sha.result["commit_sha"] == head(github)
        assert (root / "src" / "app.py").read_text() == "x = 1\n"
        tags = [request["dedupe"]["tag"] for request in r.ok.hub.requests]
        assert tags[0] == tags[1] != tags[2]  # same session, repository and resolved commit

    run10(store_dsn, github, tmp_path, body)


def test_checkout_precheck_refusals(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        unknown = await r.run(sid, "github.checkout", {"repo": REPO, "ref": "nope"})
        assert refused(unknown) == "not_found"
        hidden = await r.run(sid, "github.checkout", {"repo": "octo-org/hidden"})
        assert refused(hidden) == "repo_not_accessible"
        github.faults({"route": "GET /repos/{owner}/{repo}/commits/{ref}", "kind": "error", "status": 502})
        failing = await r.run(sid, "github.checkout", {"repo": REPO})
        assert refused(failing) == "upstream_error"
        assert r.guest.imports == []

    run10(store_dsn, github, tmp_path, body, allowed=(REPO, "octo-org/hidden"))


def test_checkout_of_a_repository_without_a_valid_default_branch(store_dsn: str, tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"default_branch": "-bad"})

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
        sid = await r.session()
        op = await r.run(sid, "github.checkout", {"repo": REPO})
        assert refused(op) == "upstream_error"

    run10(store_dsn, MockDouble(), tmp_path, body)


def test_checkout_streams_the_archive_to_the_guest_in_pieces(
    store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The packed archive goes from its scratch file to the guest piece by piece: never read whole (AUD-01 A-27).
    github = start_github(world=world())

    def whole(_path: Path) -> bytes:
        raise AssertionError("the archive was read into memory")

    monkeypatch.setattr(checkout, "CHUNK", 512)
    monkeypatch.setattr(Path, "read_bytes", whole)

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.checkout", {"repo": REPO})
        assert op.state == "succeeded", op.answer
        (pieces,) = r.guest.import_chunks
        assert len(pieces) > 1
        assert max(pieces) <= 512
        with (r.guest.work(sid) / "repos/octo-org/hello/README.md").open("rb") as readme:
            assert readme.read() == b"hello\nworld\n"

    run10(store_dsn, github, tmp_path, body)


def test_checkout_size_limit_and_retryable_guest(
    store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        monkeypatch.setattr(checkout, "ARCHIVE_LIMIT", 100)
        small = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (small.state, small.error_code) == ("failed", "size_exceeded")
        monkeypatch.undo()
        r.guest.fail["import_files"] = GuestUnreachable("files_import")
        unreachable = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (unreachable.state, unreachable.error_code, unreachable.attempt) == ("failed", "upstream_error", 4)
        assert unreachable.retryable is True
        r.guest.fail["import_files"] = GuestRefused("files_import", 409, "state_conflict")
        ended = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (ended.state, ended.error_code, ended.attempt) == ("failed", "not_found", 1)
        del r.guest.fail["import_files"]
        r.guest.import_answer = {"files": -1, "bytes": 0}
        odd = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (odd.state, odd.error_code) == ("failed", "upstream_error")
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


def test_a_packed_archive_over_the_limit_is_size_exceeded(
    store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        monkeypatch.setattr(checkout, "ARCHIVE_LIMIT", 2000)  # the gzip archive fits, the plain tar does not
        op = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (op.state, op.error_code) == ("failed", "size_exceeded")

    run10(store_dsn, github, tmp_path, body)


def tarball(*members: tuple[str, bytes, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as out:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.type, info.size = kind, len(data)
            out.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return gzip.compress(buffer.getvalue())


@pytest.mark.parametrize(
    ("archive", "code"),
    [
        (tarball(("top", tarfile.DIRTYPE, b""), ("top/../../etc/x", tarfile.REGTYPE, b"x")), "path_invalid"),
        (tarball(("top", tarfile.DIRTYPE, b""), ("/abs", tarfile.REGTYPE, b"x")), "path_invalid"),
        (b"not an archive", "upstream_error"),
    ],
)
def test_unsafe_archives_are_refused(store_dsn: str, tmp_path: Path, archive: bytes, code: str) -> None:
    sha = "a" * 40

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/tarball/{sha}"):
            return httpx.Response(200, content=archive)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": sha})
        return httpx.Response(200, json={"default_branch": "main"})

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
        sid = await r.session()
        op = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (op.state, op.error_code) == ("failed", code)
        assert r.guest.imports == []
        assert r.scratch() == []

    run10(store_dsn, MockDouble(), tmp_path, body)


def test_a_session_without_a_vm_is_not_found(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        async with r.ok.env.pool.connection() as conn:
            found = await session_records.get(conn, sid)
        assert found is not None
        ctx = OperationContext(
            context=r.ok.env.context,
            operation=SimpleNamespace(id=uuid7(), type="github.checkout", actor_id=found.owner_id),  # type: ignore[arg-type]
            session=replace(found, vm_id=None),
            params={"repo": REPO},
            data={"commit_sha": head(github)},
            attempt=1,
            env=r.ok.kit.session_env,
        )
        with pytest.raises(OperationFailed) as failed:
            await checkout.execute(ctx)
        assert failed.value.code == "not_found"
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


def test_sweep_removes_only_old_scratch_directories(tmp_path: Path) -> None:
    class Context:
        class config:
            tmp_dir = tmp_path / "tmp"

    assert snapshot.sweep(Context) == 0
    old = snapshot.fresh(Context, "old")
    young = snapshot.fresh(Context, "young")
    (tmp_path / "tmp" / "other").mkdir()
    os.utime(old, (0, 0))
    assert snapshot.sweep(Context, now=lambda: snapshot.SCRATCH_MAX_AGE_S + 10) == 1
    assert not old.exists()
    assert young.exists()


def test_sweep_tolerates_a_directory_removed_meanwhile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Context:
        class config:
            tmp_dir = tmp_path / "tmp"

    snapshot.fresh(Context, "racing")

    def gone(self: Path) -> os.stat_result:
        raise FileNotFoundError(str(self))

    monkeypatch.setattr(Path, "lstat", gone)
    assert snapshot.sweep(Context) == 0


def test_guest_codes() -> None:
    assert snapshot.guest_code(GuestUnreachable("files_import")) == ("upstream_error", True)
    assert snapshot.guest_code(GuestRefused("files_import", 413, "size_exceeded")) == ("size_exceeded", False)
    assert snapshot.guest_code(GuestRefused("files_import", 400, "path_invalid")) == ("path_invalid", False)
    assert snapshot.guest_code(GuestRefused("files_import", 500, "sandbox_failed")) == ("upstream_error", False)
    assert snapshot.guest_code(GuestProtocolError("files_export")) == ("upstream_error", False)
