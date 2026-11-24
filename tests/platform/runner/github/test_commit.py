"""U-RUN-github: `github.commit` (05-runner.md sections 12 step 3 and 12.1, 03-api.md section 9) through the operation
pipeline against the GitHub double and the real guest file code: one snapshot before the registration, parent from the
branch or the checkout, the exact Git Data API sequence, GitHub's tree equal to the local one, no ref update,
executable bits and links kept, `paths`, the dedupe fields, `tree_mismatch`, `secret_detected` for every pattern,
size limits, invalid exports and the scratch directory."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.crypto.ids import uuid7
from agent_runner.github import commit, gitobj, snapshot
from agent_runner.github.client import GitHubOptions
from agent_runner.github.snapshot import SnapFile, Snapshot, SnapshotInvalid
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.operations.extensions import CheckFailed, OperationContext, OperationFailed
from agent_runner.operations.pipeline import ATTRIBUTE as PIPELINE_ATTRIBUTE
from agent_runner.sessions import records as session_records

from ..sync.fakehub import CONTRACTS
from .conftest import REPO, GitHub
from .run10_kit import R10, MockDouble, refused, run10, world

SCHEMAS = SchemaSet(CONTRACTS)
README = "README.md"


def repo(github: GitHub) -> Any:
    return github.double.world.repo("octo-org", "hello")


def files_of(github: GitHub, commit_sha: str) -> dict[str, tuple[str, bytes]]:
    """`path -> (mode, content)` of a commit in the double."""
    objects = repo(github).objects
    tree = objects.commit(commit_sha).tree
    return {path: (entry.mode, objects.blob(entry.sha)) for path, entry in objects.files(tree).items()}


async def checked_out(r: R10, sid: str) -> Path:
    op = await r.run(sid, "github.checkout", {"repo": REPO})
    assert op.state == "succeeded", op.answer
    return r.guest.work(sid) / "repos" / "octo-org" / "hello"


def test_commit_of_a_new_branch_then_push_then_a_second_commit(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=world())
    main = repo(github).refs["refs/heads/main"]
    before = files_of(github, main)

    async def body(r: R10) -> None:
        sid = await r.session()
        root = await checked_out(r, sid)
        (root / README).write_text("changed\n")
        (root / "new.txt").write_text("new\n")
        (root / "src" / "app.py").unlink()
        start = len(r.journal())
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "Change files."})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert SCHEMAS.errors("ops/github.commit.result.json", op.result) == []
        assert (op.result["parent_sha"], op.result["files_changed"]) == (main, 3)
        parent_tree = repo(github).objects.commit(main).tree
        assert r.journal()[start:] == [
            f"GET /repos/{REPO}",
            f"GET /repos/{REPO}/git/ref/heads/work",
            f"GET /repos/{REPO}/git/commits/{main}",
            f"GET /repos/{REPO}/git/trees/{parent_tree}",
            f"POST /repos/{REPO}/git/blobs",
            f"POST /repos/{REPO}/git/blobs",
            f"POST /repos/{REPO}/git/trees",
            f"POST /repos/{REPO}/git/commits",
        ]
        after = files_of(github, op.result["commit_sha"])
        assert after == {
            **{path: value for path, value in before.items() if path != "src/app.py"},
            README: (gitobj.FILE_MODE, b"changed\n"),
            "new.txt": (gitobj.FILE_MODE, b"new\n"),
        }
        assert after["run.sh"][0] == gitobj.EXEC_MODE
        assert after["link"][0] == gitobj.LINK_MODE
        assert repo(github).objects.commit(op.result["commit_sha"]).message == "Change files."
        assert "refs/heads/work" not in repo(github).refs  # no ref update
        assert r.ok.hub.requests[-1]["dedupe"] is not None
        assert r.guest.exports[-1] == (sid, "repos/octo-org/hello", None)
        assert r.scratch() == []
        pushed = await r.run(
            sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": op.result["commit_sha"]}
        )
        assert pushed.state == "succeeded", pushed.answer
        assert repo(github).refs["refs/heads/work"] == op.result["commit_sha"]
        (root / "new.txt").write_text("newer\n")
        second = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "More."})
        assert second.result is not None
        assert (second.result["parent_sha"], second.result["files_changed"]) == (op.result["commit_sha"], 1)

    run10(store_dsn, github, tmp_path, body)


def test_commit_with_paths_compares_only_below_them(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        root = await checked_out(r, sid)
        (root / README).write_text("only this\n")
        (root / "src" / "app.py").write_text("not this\n")
        (root / "run.sh").unlink()
        (root / "docs").mkdir()
        (root / "docs" / "a.md").write_text("a\n")
        params = {"repo": REPO, "branch": "work", "message": "Some.", "paths": [README, "docs", "run.sh"]}
        op = await r.run(sid, "github.commit", params)
        assert op.result is not None, op.answer
        after = files_of(github, op.result["commit_sha"])
        assert after[README] == (gitobj.FILE_MODE, b"only this\n")
        assert after["src/app.py"] == (gitobj.FILE_MODE, b"x = 1\n")
        assert after["docs/a.md"] == (gitobj.FILE_MODE, b"a\n")
        assert "run.sh" not in after
        assert op.result["files_changed"] == 3
        assert r.guest.exports[-1][2] == [README, "docs", "run.sh"]

    run10(store_dsn, github, tmp_path, body)


def test_commit_on_an_existing_branch_takes_its_head_as_parent(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=world())
    topic = repo(github).refs["refs/heads/topic"]

    async def body(r: R10) -> None:
        sid = await r.session()
        await checked_out(r, sid)
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "refs/heads/topic", "message": "M."})
        assert op.result is not None, op.answer
        assert op.result["parent_sha"] == topic
        after = files_of(github, op.result["commit_sha"])
        assert "docs/a.md" not in after  # not in the workspace: deleted
        assert after == files_of(github, repo(github).refs["refs/heads/main"])
        assert repo(github).refs["refs/heads/topic"] == topic

    run10(store_dsn, github, tmp_path, body)


def test_dedupe_fields_follow_the_snapshot(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        root = await checked_out(r, sid)
        (root / README).write_text("one\n")
        params = {"repo": REPO, "branch": "work", "message": "M."}
        first = await r.run(sid, "github.commit", params)
        again = await r.run(sid, "github.commit", params)
        (root / README).write_text("two\n")
        other = await r.run(sid, "github.commit", params)
        tags = [request["dedupe"]["tag"] for request in r.ok.hub.requests[-3:]]
        assert tags[0] == tags[1] != tags[2]
        assert first.result is not None
        assert again.result is not None
        assert other.result is not None
        assert first.result["tree_sha"] == again.result["tree_sha"] != other.result["tree_sha"]

    run10(store_dsn, github, tmp_path, body)


def test_tree_mismatch_and_a_retried_5xx(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        root = await checked_out(r, sid)
        (root / README).write_text("x\n")
        params = {"repo": REPO, "branch": "work", "message": "M."}
        github.faults({"route": "POST /repos/{owner}/{repo}/git/trees", "kind": "tree_mismatch", "times": 1})
        mismatch = await r.run(sid, "github.commit", params)
        assert (mismatch.state, mismatch.error_code, mismatch.retryable) == ("failed", "tree_mismatch", False)
        assert r.scratch() == []
        github.faults({"route": "POST /repos/{owner}/{repo}/git/commits", "kind": "error", "times": 1})
        retried = await r.run(sid, "github.commit", params)
        assert (retried.state, retried.attempt) == ("succeeded", 2)
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "-----BEGIN " + "PRIVATE KEY-----",
        "ghp_" + "a" * 36,
        "gho_" + "B" * 20,
        "ghu_" + "1" * 20,
        "github_pat_" + "x" * 30,
        "sk-ant-" + "api03-" + "k" * 20,
        "sk-or-" + "v1-" + "k" * 20,
        "AKIA" + "ABCDEFGHIJKLMNOP",
    ],
)
def test_scan_finds_every_secret_pattern(tmp_path: Path, secret: str) -> None:
    content = f"prefix {secret} suffix\n".encode()
    blob = gitobj.blob_sha(content)
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / blob).write_bytes(content)
    with pytest.raises(CheckFailed) as caught:
        commit.scan(tmp_path, [{"path": "a", "sha": None}, {"path": "b", "sha": blob}])
    assert caught.value.code == "secret_detected"


def test_a_secret_refuses_the_commit_before_registration(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        root = await checked_out(r, sid)
        (root / "config.env").write_text("KEY=AKIA" + "ABCDEFGHIJKLMNOP\n")
        start = len(r.journal())
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "M."})
        assert refused(op) == "secret_detected"
        assert not any(line.startswith("POST") for line in r.journal()[start:])
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


def test_a_commit_without_changes_is_refused_before_registration(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    """KOR-10 (AUD-02 NB-03): a snapshot equal to the parent is the formal refusal `no_changes` at the check; nothing
    is registered and nothing goes to GitHub (before: an empty tree, GitHub `422`, `upstream_error`)."""
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        root = await checked_out(r, sid)
        start = len(r.journal())
        params = {"repo": REPO, "branch": "work", "message": "M."}
        assert refused(await r.run(sid, "github.commit", params)) == "no_changes"
        (root / "notes.txt").write_text("new\n")
        assert refused(await r.run(sid, "github.commit", {**params, "paths": [README]})) == "no_changes"
        assert not any(line.startswith("POST") for line in r.journal()[start:])
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


def test_every_spelling_of_a_repository_shares_one_directory(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    """KOR-10 (AUD-02 NB-04): GitHub names are case-insensitive, so are the allow-list and the dedupe; the checkout
    goes to `repos/<owner>/<name>` in lower case, names it in `dir`, and a commit in another spelling exports it."""
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.checkout", {"repo": "Octo-Org/Hello"})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert op.result["dir"] == "repos/octo-org/hello"
        (r.guest.work(sid) / op.result["dir"] / "notes.txt").write_text("new\n")
        done = await r.run(sid, "github.commit", {"repo": "octo-org/HELLO", "branch": "work", "message": "M."})
        assert done.state == "succeeded", done.answer
        assert done.result is not None
        assert done.result["files_changed"] == 1

    run10(store_dsn, github, tmp_path, body)


def test_precheck_refusals(store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        params = {"repo": REPO, "branch": "work", "message": "M."}
        assert refused(await r.run(sid, "github.commit", params)) == "not_found"  # no checkout, no branch
        on_topic = {**params, "branch": "topic"}
        assert refused(await r.run(sid, "github.commit", on_topic)) == "not_found"  # no workspace directory
        await checked_out(r, sid)
        github.faults({"route": "GET /repos/{owner}/{repo}/git/trees/{tree}", "kind": "truncated", "limit": 1})
        assert refused(await r.run(sid, "github.commit", params)) == "size_exceeded"
        monkeypatch.setattr(snapshot, "MAX_EXPORT_BYTES", 100)
        assert refused(await r.run(sid, "github.commit", params)) == "size_exceeded"
        monkeypatch.undo()
        r.guest.fail["export_files"] = GuestUnreachable("files_export")
        assert refused(await r.run(sid, "github.commit", params)) == "upstream_error"
        del r.guest.fail["export_files"]
        r.guest.export_override = lambda write: "0" * 64
        assert refused(await r.run(sid, "github.commit", params)) == "upstream_error"
        r.guest.export_override = None
        github.faults({"route": "GET /repos/{owner}/{repo}", "kind": "error", "status": 502})
        assert refused(await r.run(sid, "github.commit", params)) == "upstream_error"
        github.faults({"route": "GET /repos/{owner}/{repo}/git/ref/{ref}", "kind": "error", "status": 502})
        assert refused(await r.run(sid, "github.commit", params)) == "upstream_error"
        assert refused(await r.run(sid, "github.commit", {**params, "repo": "octo-org/hidden"})) == (
            "repo_not_accessible"
        )
        delattr(r.ok.env.context, PIPELINE_ATTRIBUTE)
        assert refused(await r.run(sid, "github.commit", params)) == "not_found"
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body, allowed=(REPO, "octo-org/hidden"))


def test_a_revoked_token_and_an_unusable_credential(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        github.double.world.tokens.clear()
        params = {"repo": REPO, "branch": "work", "message": "M."}
        # held for the owner's connection (KOR-25), not registered
        commit = await r.run(sid, "github.commit", params)
        assert (commit.state, commit.held, commit.registration) == ("received", True, None)
        async with r.ok.env.pool.connection() as conn:
            await conn.execute('UPDATE runner.credential_cache SET record = record || \'{"ct": "AAAA"}\'::jsonb')
        checkout = await r.run(sid, "github.checkout", {"repo": REPO})
        assert (checkout.state, checkout.held, checkout.registration) == ("received", True, None)

    run10(store_dsn, github, tmp_path, body)


def test_parent_answers_outside_the_git_format(store_dsn: str, tmp_path: Path) -> None:
    parent, tree = "a" * 40, "b" * 40

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/git/ref/heads/work"):
            return httpx.Response(200, json={"object": {"sha": parent}})
        if path.endswith(f"/git/commits/{parent}"):
            return httpx.Response(200, json={"tree": {"sha": tree}})
        if path.endswith(f"/git/trees/{tree}"):
            return httpx.Response(200, json={"tree": [{"path": "../x", "mode": "100644", "type": "blob"}]})
        return httpx.Response(200, json={"default_branch": "main"})

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
        sid = await r.session()
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "M."})
        assert refused(op) == "upstream_error"

    run10(store_dsn, MockDouble(), tmp_path, body)


# -- the executor directly -------------------------------------------------------------------------------------


def executor_ctx(r: R10, found: Any, data: dict[str, Any], op_id: str | None = None) -> OperationContext:
    return OperationContext(
        context=r.ok.env.context,
        operation=SimpleNamespace(id=op_id or uuid7()),  # type: ignore[arg-type]
        session=found,
        params={"repo": REPO, "branch": "work", "message": "M."},
        data=data,
        attempt=1,
        env=r.ok.kit.session_env,
    )


def test_executor_without_or_with_an_altered_snapshot(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    main = repo(github).refs["refs/heads/main"]

    async def body(r: R10) -> None:
        sid = await r.session()
        async with r.ok.env.pool.connection() as conn:
            found = await session_records.get(conn, sid)
        blob = gitobj.blob_sha(b"content\n")
        data = {
            "parent_sha": main,
            "parent_tree": repo(github).objects.commit(main).tree,
            "tree_sha": "c" * 40,
            "entries": [{"path": "f", "mode": "100644", "type": "blob", "sha": blob}],
            "files_changed": 1,
        }
        with pytest.raises(OperationFailed) as failed:
            await commit.execute(executor_ctx(r, found, data))
        assert failed.value.code == "not_found"
        op_id = uuid7()
        scratch = snapshot.fresh(r.ok.env.context, op_id)
        (scratch / "blobs").mkdir()
        (scratch / "blobs" / blob).write_bytes(b"altered\n")
        with pytest.raises(OperationFailed) as failed:
            await commit.execute(executor_ctx(r, found, data, op_id))
        assert failed.value.code == "not_found"
        assert r.scratch() == []

    run10(store_dsn, github, tmp_path, body)


def test_executor_refuses_a_blob_stored_under_another_sha(store_dsn: str, tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sha": "d" * 40})

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
        sid = await r.session()
        async with r.ok.env.pool.connection() as conn:
            found = await session_records.get(conn, sid)
        blob = gitobj.blob_sha(b"content\n")
        op_id = uuid7()
        scratch = snapshot.fresh(r.ok.env.context, op_id)
        (scratch / "blobs").mkdir()
        (scratch / "blobs" / blob).write_bytes(b"content\n")
        data = {
            "parent_sha": "a" * 40,
            "parent_tree": "b" * 40,
            "tree_sha": "c" * 40,
            "entries": [{"path": "old", "mode": "100644", "type": "blob", "sha": None}, {"path": "f", "sha": blob}],
            "files_changed": 2,
        }
        assert found is not None
        with pytest.raises(OperationFailed) as failed:
            await commit.execute(executor_ctx(r, replace(found), data, op_id))
        assert failed.value.code == "tree_mismatch"
        assert r.scratch() == []

    run10(store_dsn, MockDouble(), tmp_path, body)


# -- plan ------------------------------------------------------------------------------------------------------


def snap(**files: bytes) -> Snapshot:
    return Snapshot(
        {path.replace("__", "/"): SnapFile(len(data), gitobj.blob_sha(data)) for path, data in files.items()}, []
    )


BASE = {
    "keep.txt": (gitobj.FILE_MODE, gitobj.blob_sha(b"keep")),
    "tool": (gitobj.EXEC_MODE, gitobj.blob_sha(b"tool")),
    "link": (gitobj.LINK_MODE, gitobj.blob_sha(b"keep.txt")),
    "vendor/lib": (gitobj.SUBMODULE_MODE, "1" * 40),
    "dir/a": (gitobj.FILE_MODE, gitobj.blob_sha(b"a")),
    "dir/ln": (gitobj.LINK_MODE, gitobj.blob_sha(b"a")),
}


def test_plan_keeps_modes_links_and_submodules_and_resolves_conflicts() -> None:
    found = commit.plan(
        BASE, snap(**{"keep.txt": b"keep", "tool": b"tool2", "dir": b"now a file", "link": b"real"}), None
    )
    entries = {entry["path"]: entry for entry in found.entries}
    assert entries["dir/a"] == {"path": "dir/a", "mode": gitobj.FILE_MODE, "type": "blob", "sha": None}
    assert entries["dir/ln"]["sha"] is None
    assert entries["tool"]["mode"] == gitobj.EXEC_MODE
    assert entries["link"] == {
        "path": "link",
        "mode": gitobj.FILE_MODE,
        "type": "blob",
        "sha": gitobj.blob_sha(b"real"),
    }
    assert entries["dir"]["sha"] == gitobj.blob_sha(b"now a file")
    assert "vendor/lib" not in entries
    assert "keep.txt" not in entries
    assert found.files_changed == 5
    expected = {
        "keep.txt": BASE["keep.txt"],
        "tool": (gitobj.EXEC_MODE, gitobj.blob_sha(b"tool2")),
        "link": (gitobj.FILE_MODE, gitobj.blob_sha(b"real")),
        "vendor/lib": BASE["vendor/lib"],
        "dir": (gitobj.FILE_MODE, gitobj.blob_sha(b"now a file")),
    }
    assert found.tree_sha == gitobj.build_tree(expected)
    deeper = commit.plan(BASE, snap(**{"keep.txt__x": b"x", "vendor__lib__y": b"y"}), ["keep.txt", "vendor"])
    entries = {entry["path"]: entry for entry in deeper.entries}
    assert entries["keep.txt"]["sha"] is None
    assert entries["vendor/lib"] == {"path": "vendor/lib", "mode": "160000", "type": "commit", "sha": None}
    assert set(entries) == {"keep.txt", "vendor/lib", "keep.txt/x", "vendor/lib/y"}


def test_plan_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    def code(**files: bytes) -> str:
        with pytest.raises(CheckFailed) as caught:
            commit.plan({}, snap(**files), None)
        return caught.value.code

    monkeypatch.setattr(commit, "MAX_CHANGES", 2)
    assert code(a=b"1", b=b"2", c=b"3") == "size_exceeded"
    monkeypatch.setattr(commit, "MAX_CHANGES", 500)
    monkeypatch.setattr(commit, "MAX_FILE", 3)
    assert code(a=b"1234") == "size_exceeded"
    monkeypatch.setattr(commit, "MAX_FILE", 10)
    monkeypatch.setattr(commit, "MAX_TOTAL", 5)
    assert code(a=b"123", b=b"456") == "size_exceeded"
    assert code(**{"a__..__b": b"x"}) == "path_invalid"
    assert commit.plan({}, snap(a=b"12"), None).files_changed == 1
    odd = {"a\\b": (gitobj.FILE_MODE, gitobj.blob_sha(b"x"))}
    assert commit.plan(odd, snap(**{"a\\b": b"x"}), None).files_changed == 0  # unchanged: no refusal
    with pytest.raises(CheckFailed):
        commit.plan(odd, snap(**{"a\\b": b"y"}), None)


def test_plan_keeps_what_the_export_skipped() -> None:
    skipped = Snapshot({}, [{"path": "keep.txt", "reason": "unreadable"}, {"path": "dir", "reason": "symlink"}])
    found = commit.plan(BASE, skipped, None)
    assert {entry["path"] for entry in found.entries} == {"tool"}  # only the executable file is deleted
    assert found.entries[0]["sha"] is None


def test_plan_never_touches_protected_names() -> None:
    """ATS-03 (B-09): the checkout never imports `.claude/`, `.mcp.json`, `CLAUDE.md` …, so their absence in the
    workspace is no deletion, and a protected file in a snapshot is never committed."""
    base = {
        **BASE,
        "CLAUDE.md": (gitobj.FILE_MODE, gitobj.blob_sha(b"rules")),
        ".claude/settings.json": (gitobj.FILE_MODE, gitobj.blob_sha(b"{}")),
        "sub/.mcp.json": (gitobj.FILE_MODE, gitobj.blob_sha(b"{}")),
    }
    unchanged = {"keep.txt": b"keep", "tool": b"tool", "dir__a": b"a"}
    found = commit.plan(
        base, snap(**unchanged, **{".claude__x": b"x", "CLAUDE.md": b"new", "x__.MCP.json": b"{}"}), None
    )
    assert found.entries == []
    assert found.tree_sha == gitobj.build_tree(base)
    scoped = commit.plan(base, snap(**unchanged), ["CLAUDE.md", "sub"])
    assert scoped.entries == []


def test_trim_keeps_only_the_blobs_to_upload(tmp_path: Path) -> None:
    (tmp_path / "blobs").mkdir()
    for name in ("a" * 40, "b" * 40):
        (tmp_path / "blobs" / name).write_bytes(b"x")
    snapshot.trim(tmp_path, {"a" * 40, None})
    assert [p.name for p in (tmp_path / "blobs").iterdir()] == ["a" * 40]


# -- read_export -----------------------------------------------------------------------------------------------


def export(
    manifest: Any, *members: tuple[str, bytes], kind: bytes = tarfile.REGTYPE, raw: bytes | None = None
) -> tuple[bytes, str]:
    data = raw if raw is not None else json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for name, content in ((snapshot.MANIFEST, data), *members):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.type = kind if name != snapshot.MANIFEST else tarfile.REGTYPE
            out.addfile(info, io.BytesIO(content) if info.isreg() else None)
    return buffer.getvalue(), hashlib.sha256(data).hexdigest()


def entry(path: str, content: bytes) -> dict[str, Any]:
    return {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def read(tmp_path: Path, raw: bytes, digest: str) -> Snapshot:
    target = tmp_path / "export.tar"
    target.write_bytes(raw)
    return snapshot.read_export(target, digest, tmp_path / "blobs")


GOOD = {"files": [entry("a", b"A")], "skipped": [{"path": "l", "reason": "symlink"}]}


def test_read_export_stores_blobs(tmp_path: Path) -> None:
    raw, digest = export(GOOD, ("a", b"A"))
    found = read(tmp_path, raw, digest)
    assert found.files == {"a": SnapFile(1, gitobj.blob_sha(b"A"))}
    assert found.skipped == GOOD["skipped"]
    assert snapshot.read_blob(tmp_path, gitobj.blob_sha(b"A")) == b"A"


Case = Callable[[], tuple[bytes, str]]
BAD: list[Case] = [
    lambda: (export(GOOD, ("a", b"A"))[0], "0" * 64),
    lambda: export(None, raw=b"{not json"),
    lambda: export([]),
    lambda: export({"files": {}, "skipped": []}),
    lambda: export({"files": ["x"], "skipped": []}),
    lambda: export({"files": [{**entry("a", b"A"), "size": -1}], "skipped": []}),
    lambda: export({"files": [{**entry("a", b"A"), "sha256": "x"}], "skipped": []}),
    lambda: export({"files": [entry("a", b"A"), entry("a", b"A")], "skipped": []}),
    lambda: export({"files": [], "skipped": [{"path": "x", "reason": "other"}]}),
    lambda: export({"files": [], "skipped": [{"path": 5, "reason": "symlink"}]}),
    lambda: export(GOOD),
    lambda: export(GOOD, ("a", b"B")),
    lambda: export(GOOD, ("a", b"AA")),
    lambda: export(GOOD, ("b", b"A")),
    lambda: export(GOOD, ("a", b"A"), ("a", b"A")),
    lambda: export(GOOD, ("d", b""), kind=tarfile.DIRTYPE),
    lambda: export(GOOD, ("l", b""), kind=tarfile.SYMTYPE),
]


@pytest.mark.parametrize("case", BAD)
def test_read_export_refuses_exports_that_do_not_match(tmp_path: Path, case: Case) -> None:
    raw, digest = case()
    with pytest.raises(SnapshotInvalid):
        read(tmp_path, raw, digest)


def test_read_export_needs_the_manifest_first(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as out:
        info = tarfile.TarInfo("a")
        out.addfile(info, io.BytesIO(b""))
    with pytest.raises(SnapshotInvalid):
        read(tmp_path, buffer.getvalue(), "0" * 64)
    with pytest.raises(SnapshotInvalid):
        read(tmp_path, b"", "0" * 64)
    with pytest.raises(SnapshotInvalid):
        snapshot.read_blob(tmp_path, "e" * 40)
