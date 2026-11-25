"""U-RUN-github: the edges of "Fix conflicts" (ADR-084 section 4): GitHub answers outside the expected form, refusals of
the pre-check, the export failures of the workspace protection, unsafe or oversized archives, single blob reads and
their limit, the verification rules of the merge commit on synthetic preparations, and the stored preparation."""

from __future__ import annotations

import asyncio
import gzip
import io
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.github import checkout, commit, gitobj, merge3, merge_base, merge_commit, merge_prep, snapshot
from agent_runner.github.client import GitHubOptions
from agent_runner.github.snapshot import SnapFile, Snapshot
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.operations.extensions import CheckFailed, OperationContext, OperationFailed
from agent_runner.sessions import records as session_records
from agent_runner.store.pool import open_pool

from .conftest import REPO
from .run10_kit import R10, FilesGuest, MockDouble, PublicHost, known_user, refused, run10
from .test_merge_base import APP, advance, root_of, world

F, X, L = gitobj.FILE_MODE, gitobj.EXEC_MODE, gitobj.LINK_MODE
HEAD, BASE, TIP, TREE = "a" * 40, "b" * 40, "c" * 40, "d" * 40
PULL: dict[str, Any] = {
    "state": "open",
    "merged": False,
    "head": {"ref": "feature", "sha": HEAD, "repo": {"full_name": REPO}},
    "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}},
}


def mock(**answers: Any) -> Callable[[httpx.Request], httpx.Response]:
    """Answers by path suffix (`pull`, `tip`, `compare`, `commit`, `tree`); everything else is the repository."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for key, suffix in (
            ("pull", "/pulls/1"),
            ("tip", "/git/ref/heads/main"),
            ("compare", f"/compare/{TIP}...{HEAD}"),
            ("commit", f"/git/commits/{HEAD}"),
            ("tree", f"/git/trees/{TREE}"),
        ):
            if path.endswith(suffix) and key in answers:
                status, body = answers[key]
                return httpx.Response(status, json=body)
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": TIP}})
        if path.endswith("/compare/" + f"{TIP}...{HEAD}"):
            return httpx.Response(200, json={"merge_base_commit": {"sha": BASE}})
        if path.endswith(f"/git/commits/{HEAD}"):
            return httpx.Response(200, json={"tree": {"sha": TREE}})
        if path.endswith("/pulls/1"):
            return httpx.Response(200, json=PULL)
        return httpx.Response(200, json={"default_branch": "main", "permissions": {"push": True}})

    return handle


@pytest.mark.parametrize(
    ("answers", "code"),
    [
        ({"pull": (500, {})}, "upstream_error"),
        ({"pull": (200, {**PULL, "head": {**PULL["head"], "repo": None}})}, "merge_unsupported"),
        ({"pull": (200, {**PULL, "head": {**PULL["head"], "repo": {"full_name": "fork/hello"}}})}, "merge_unsupported"),
        ({"pull": (200, {**PULL, "merged": True})}, "merge_unsupported"),
        ({"pull": (200, {**PULL, "head": {**PULL["head"], "ref": "bad..ref"}})}, "upstream_error"),
        ({"tip": (404, {})}, "not_found"),
        ({"tip": (500, {})}, "upstream_error"),
        ({"compare": (404, {})}, "merge_unsupported"),
        ({"compare": (500, {})}, "upstream_error"),
        ({"tree": (200, {"truncated": True, "tree": []})}, "size_exceeded"),
        ({"tree": (200, {"tree": [{"path": "../x", "mode": F, "type": "blob", "sha": HEAD}]})}, "upstream_error"),
        ({"tree": (200, {"tree": None})}, "upstream_error"),
    ],
)
def test_precheck_answers(store_dsn: str, tmp_path: Path, answers: dict[str, Any], code: str) -> None:
    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=known_user(mock(**answers)))
        sid = await r.session()
        workspace(r, sid)
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == code

    run10(store_dsn, MockDouble(), tmp_path, body)


def workspace(r: R10, sid: str) -> dict[str, str]:
    """A repository directory with one file (the protection then reads the head's tree)."""
    root = r.guest.work(sid) / "repos" / "octo-org" / "hello"
    root.mkdir(parents=True, exist_ok=True)
    (root / "mine.txt").write_text("mine\n")
    return {"mine.txt": gitobj.blob_sha(b"mine\n")}


@pytest.mark.parametrize(
    ("compare", "branch", "code"),
    [((500, {}), None, "upstream_error"), ((404, {}), None, "workspace_dirty"), ((404, {}), "work", "workspace_dirty")],
)
def test_a_remembered_commit_that_github_cannot_place(
    store_dsn: str, tmp_path: Path, compare: tuple[int, Any], branch: str | None, code: str
) -> None:
    commit = "e" * 40

    def handle(request: httpx.Request) -> httpx.Response:
        if "/compare/" + commit in request.url.path:
            return httpx.Response(compare[0], json=compare[1])
        if request.url.path.endswith(f"/git/trees/{TREE}"):
            return httpx.Response(200, json={"tree": []})
        return mock()(request)

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=known_user(handle))
        sid = await r.session()
        files = workspace(r, sid)
        async with r.ok.env.pool.connection() as conn:
            known = merge_prep.Known(merge_prep.digest(files.items()), commit, branch)
            await merge_prep.remember(conn, sid, "repos/octo-org/hello", known)
            await conn.commit()
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == code

    run10(store_dsn, MockDouble(), tmp_path, body)


def test_a_protected_head_branch(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        await r.ok.settings([REPO], protected=["feature"])
        sid = await r.session()
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == "branch_protected"

    run10(store_dsn, github, tmp_path, body)


def failing(error: Exception) -> Callable[[Callable[[bytes], object]], str]:
    def export(_write: Callable[[bytes], object]) -> str:
        raise error

    return export


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (GuestRefused("files_export", 400, "path_invalid"), "path_invalid"),
        (GuestUnreachable("files_export"), "upstream_error"),
        (snapshot.TooLarge(), "size_exceeded"),
        (snapshot.SnapshotInvalid("bad"), "upstream_error"),
    ],
)
def test_export_failures_of_the_workspace_protection(
    store_dsn: str, start_github: Any, tmp_path: Path, error: Exception, code: str
) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == code
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body, guest=FilesGuest(root=tmp_path / "guest", export_override=failing(error)))


def test_a_session_without_a_vm(store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: Any) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def no_guest(*_: Any) -> Any:
        raise snapshot.NoGuest

    async def body(r: R10) -> None:
        sid = await r.session()
        monkeypatch.setattr(snapshot, "guest_for", no_guest)
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == "not_found"

    run10(store_dsn, github, tmp_path, body)


class Intercept(httpx.AsyncBaseTransport):
    """The double, except for requests `answer` takes (it returns a response or None)."""

    def __init__(self, url: str, answer: Callable[[httpx.Request], httpx.Response | None]) -> None:
        self.inner = PublicHost(url)
        self.answer = answer

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        found = self.answer(request)
        return found if found is not None else await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def tarball(members: list[tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as out:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def protected_world() -> dict[str, Any]:
    data = world(**{"CLAUDE.md": "A\nb\nc\n"})
    data["repositories"][0]["branches"][0]["commits"][0]["files"]["CLAUDE.md"] = "a\nb\nc\n"
    return data


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (lambda r: httpx.Response(200, content=b"not a tar") if "/tarball/" in r.url.path else None, "upstream_error"),
        (
            lambda r: httpx.Response(200, content=tarball([("top/../x", b"x")])) if "/tarball/" in r.url.path else None,
            "path_invalid",
        ),
        (
            lambda r: (
                httpx.Response(200, json={"content": "eA", "encoding": "base64"})
                if "/git/blobs/" in r.url.path and r.method == "GET"
                else None
            ),
            "upstream_error",
        ),
        (
            lambda r: (
                httpx.Response(200, json={"content": "eA==", "encoding": "base64"})
                if "/git/blobs/" in r.url.path and r.method == "GET"
                else None
            ),
            "upstream_error",
        ),
        (
            lambda r: (
                httpx.Response(201, json={"sha": "e" * 40})
                if "/git/blobs" in r.url.path and r.method == "POST"
                else None
            ),
            "tree_mismatch",
        ),
    ],
)
def test_executor_refusals(
    store_dsn: str, start_github: Any, tmp_path: Path, answer: Callable[[httpx.Request], Any], code: str
) -> None:
    github = start_github(world=protected_world())
    # the base changes CLAUDE.md elsewhere than the head: merged at blob level and uploaded
    advance(github, "main", {APP: "x = 3\n", "CLAUDE.md": "a\nb\nC\n"})

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(wall=github.clock, transport=Intercept(github.url, answer))
        sid = await r.session()
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "failed", op.answer
        assert op.error_code == code
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


def test_limits_of_the_executor(store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: Any) -> None:
    github = start_github(world=protected_world())
    advance(github, "main", {APP: "x = 3\n", "CLAUDE.md": "B\nb\nc\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        monkeypatch.setattr(merge_base, "MAX_BLOB_READS", 0)
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert (op.state, op.error_code) == ("failed", "size_exceeded")
        monkeypatch.setattr(merge_base, "MAX_BLOB_READS", 1000)
        monkeypatch.setattr(checkout, "ARCHIVE_LIMIT", 10)
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert (op.state, op.error_code) == ("failed", "size_exceeded")
        monkeypatch.setattr(checkout, "ARCHIVE_LIMIT", 512 * 1024 * 1024)
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert op.result["conflicts"] == [
            {"path": "CLAUDE.md", "kind": "protected"},
            {"path": APP, "kind": "content"},
        ]

    run10(store_dsn, github, tmp_path, body)


def test_a_base_that_only_deletes_needs_no_base_archive(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {"README.md": None})

    async def body(r: R10) -> None:
        sid = await r.session()
        start = len(r.journal())
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        assert len([line for line in r.journal()[start:] if "/tarball/" in line]) == 1
        assert not (root_of(r, sid) / "README.md").exists()
        assert (root_of(r, sid) / APP).read_text() == "x = 2\n"

    run10(store_dsn, github, tmp_path, body)


# -- units ---------------------------------------------------------------------------------------------------------


def test_store_writes_each_blob_once(tmp_path: Path) -> None:
    store = merge_base.Store(tmp_path / "blobs")
    sha = store.put(b"x\n")
    (tmp_path / "blobs" / sha).write_bytes(b"changed")
    assert store.put(b"x\n") == sha
    assert store.read(sha) == b"changed"  # not written again


def test_needed_blobs_of_an_add_add_merge() -> None:
    plan = merge3.merge_entries({}, {"n": (F, HEAD)}, {"n": (F, BASE)})
    assert merge_base._needed(plan, {}, {"n": (F, HEAD)}, {"n": (F, BASE)}) == {HEAD, BASE}


def prep(**fields: Any) -> merge_prep.Prep:
    values: dict[str, Any] = {
        "id": str(uuid7()),
        "session_id": str(uuid7()),
        "repo": REPO,
        "number": 1,
        "branch": "feature",
        "head_sha": HEAD,
        "base_sha": BASE,
        "merge_base_sha": TIP,
        "changed": {},
        "conflicts": {},
        "sides": {},
        "markers": {},
        "state": "pending",
        "commit_sha": None,
    }
    values.update(fields)
    return merge_prep.Prep(**values)


def snap(scratch: Path, **files: bytes) -> Snapshot:
    (scratch / "blobs").mkdir(parents=True, exist_ok=True)
    found = {}
    for path, data in files.items():
        sha = gitobj.blob_sha(data)
        (scratch / "blobs" / sha).write_bytes(data)
        found[path.replace("__", "/")] = SnapFile(len(data), sha)
    return Snapshot(found, [])


def sha(data: bytes) -> str:
    return gitobj.blob_sha(data)


def test_protected_conflict_kept_from_the_base_and_decided(tmp_path: Path) -> None:
    head = {"a.txt": (F, sha(b"a"))}
    theirs = {"a.txt": (F, sha(b"a")), "AGENTS.md": (F, sha(b"base"))}
    found = prep(
        changed={"AGENTS.md": [F, sha(b"base")]},
        conflicts={"AGENTS.md": "protected"},
        sides={"AGENTS.md": {"head": None, "base": [F, sha(b"base")]}},
    )
    s = snap(tmp_path, **{"a.txt": b"a"})
    with pytest.raises(CheckFailed) as failed:
        merge_commit.plan(head, theirs, s, found, {})
    assert failed.value.code == "conflicts_unresolved"
    assert "AGENTS.md" not in merge_commit.plan(head, theirs, s, found, {"AGENTS.md": "head"}).new
    assert merge_commit.plan(head, theirs, s, found, {"AGENTS.md": "base"}).new["AGENTS.md"] == (F, sha(b"base"))
    with pytest.raises(CheckFailed):
        merge_commit.plan(head, theirs, s, found, {"AGENTS.md": "edited"})


def test_hidden_entries_follow_the_merge(tmp_path: Path) -> None:
    head = {"a.txt": (F, sha(b"a")), "old": (L, sha(b"t"))}
    theirs = {"a.txt": (F, sha(b"a")), "new": (L, sha(b"u"))}
    found = prep(changed={"old": None, "new": [L, sha(b"u")]})
    plan = merge_commit.plan(head, theirs, snap(tmp_path, **{"a.txt": b"a"}), found, {})
    assert plan.new == {"a.txt": (F, sha(b"a")), "new": (L, sha(b"u"))}
    assert plan.counted == frozenset()
    # a decision for a path the merge did not change
    with pytest.raises(CheckFailed) as failed:
        merge_commit.plan(head, theirs, snap(tmp_path, **{"a.txt": b"a"}), found, {"a.txt": "head"})
    assert "a.txt" in (failed.value.message or "")


def test_a_file_and_a_directory_at_one_path(tmp_path: Path) -> None:
    head = {"x": (F, sha(b"x"))}
    found = prep(changed={"x": None, "x/y": [L, sha(b"t")]})
    with pytest.raises(CheckFailed) as failed:
        merge_commit.plan(head, {}, snap(tmp_path, x=b"x"), found, {"x": "edited"})
    assert failed.value.code == "conflicts_unresolved"
    with pytest.raises(CheckFailed) as failed:
        merge_commit.plan(head, {}, snap(tmp_path, x=b"x"), prep(changed={"x/y": [L, sha(b"t")]}), {})
    assert "file and a directory" in (failed.value.message or "")


def test_marker_check_units(tmp_path: Path) -> None:
    head: dict[str, tuple[str, str]] = {"c": (F, sha(b"c"))}
    theirs: dict[str, tuple[str, str]] = {}
    s = snap(tmp_path, c=b"clean\n", m=b"<<<<<<< a\nx\n>>>>>>> b\n", p=b"plain\n")
    found = prep(changed={"c": [F, sha(b"merged")]}, conflicts={"c": "content"}, markers={"c": []})
    merged = merge_commit.plan(head, theirs, s, found, {"c": "edited"})
    bad, suspects = merge_commit.marked(tmp_path, merged, found)
    assert (bad, suspects) == ([], ["m"])
    assert merge_commit.marked(tmp_path, merged, found, {"m": frozenset()}) == (["m"], [])
    known = frozenset({b"<<<<<<< a", b">>>>>>> b"})
    assert merge_commit.marked(tmp_path, merged, found, {"m": known}) == ([], [])
    merge_commit.refuse_marked([])
    with pytest.raises(CheckFailed) as failed:
        merge_commit.refuse_marked([f"f{index}" for index in range(25)])
    assert failed.value.code == "conflict_markers"
    assert "and 5 more" in (failed.value.message or "")


def test_marker_helpers() -> None:
    assert merge3.marker_lines(b"a\r\n=======\r\n<<<<<<< x\n") == {b"=======", b"<<<<<<< x"}
    assert not merge3.has_markers(b"=======\n", strict=True, known=frozenset({b"======="}))
    assert merge3.has_markers(b"<<<<<<< x\n>>>>>>> y\n", strict=False)
    assert not merge3.has_markers(b"<<<<<<< x\n>>>>>>> y\n", strict=False, known=frozenset({b">>>>>>> y"}))


def test_commit_executor_without_entries_keeps_the_head_tree(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        async with r.ok.env.pool.connection() as conn:
            found = await session_records.get(conn, sid)
        assert found is not None
        ctx = OperationContext(
            context=r.ok.env.context,
            operation=SimpleNamespace(id=uuid7(), type="github.commit", actor_id=found.owner_id),  # type: ignore[arg-type]
            session=found,
            params={"repo": REPO, "branch": "feature", "message": "M."},
            data={
                "parent_sha": HEAD,
                "parent_tree": TREE,
                "tree_sha": TIP,
                "entries": [],
                "files_changed": 0,
                "merge_parent": BASE,
                "prep_id": str(uuid7()),
                "upload": [],
            },
            attempt=1,
            env=r.ok.kit.session_env,
        )
        snapshot.fresh(r.ok.env.context, ctx.operation.id)
        with pytest.raises(OperationFailed) as failed:
            await commit.execute(ctx)
        assert failed.value.code == "tree_mismatch"

    run10(store_dsn, github, tmp_path, body)


def test_a_merge_commit_without_change_keeps_the_head_tree(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    hello = github.double.world.repo("octo-org", "hello")
    head, tip = hello.refs["refs/heads/feature"], hello.refs["refs/heads/main"]
    tree = hello.objects.commit(head).tree

    async def body(r: R10) -> None:
        sid = await r.session()
        async with r.ok.env.pool.connection() as conn:
            found = await session_records.get(conn, sid)
        assert found is not None
        identity = {"name": "octo-user", "email": "101+octo-user@users.noreply.github.com"}
        ctx = OperationContext(
            context=r.ok.env.context,
            operation=SimpleNamespace(id=uuid7(), type="github.commit", actor_id=found.owner_id),  # type: ignore[arg-type]
            session=found,
            params={"repo": REPO, "branch": "feature", "message": "Merge."},
            data={
                "parent_sha": head,
                "parent_tree": tree,
                "tree_sha": tree,
                "entries": [],
                "files_changed": 0,
                "merge_parent": tip,
                "prep_id": str(uuid7()),
                "upload": [],
                "commit": {"author": identity, "committer": identity, "co_authors": [], "signing": "none"},
            },
            attempt=1,
            env=r.ok.kit.session_env,
        )
        snapshot.fresh(r.ok.env.context, ctx.operation.id)
        result = await commit.execute(ctx)
        assert result["merge_parent"] == tip
        stored = hello.objects.commit(result["commit_sha"])
        assert (stored.tree, stored.parents) == (tree, (head, tip))

    run10(store_dsn, github, tmp_path, body)


def test_prune_removes_old_rows(store_dsn: str) -> None:
    async def main() -> list[int]:
        pool = await open_pool(store_dsn)
        try:
            session_id = str(uuid7())
            async with pool.connection() as conn:
                await merge_prep.save(conn, prep(session_id=session_id))
                await merge_prep.remember(conn, session_id, "repos/octo-org/hello", merge_prep.Known("f" * 64))
                assert await merge_prep.remembered(conn, session_id, "repos/octo-org/hello") == merge_prep.Known(
                    "f" * 64
                )
                await conn.execute("UPDATE runner.merge_prep SET created_at = now() - interval '8 days'")
                await conn.execute("UPDATE runner.workspace_state SET updated_at = now() - interval '8 days'")
                await conn.commit()
            counts = []
            for _ in range(2):
                async with pool.connection() as conn, conn.transaction():
                    counts.append(await merge_prep.prune(conn))
            return counts
        finally:
            await pool.close()

    assert asyncio.run(main()) == [2, 0]
