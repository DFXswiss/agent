"""U-RUN-github: "Fix conflicts" through the operation pipeline against the GitHub double and the real guest file code
(ADR-084 section 4): `github.merge_base` (markers, clean merges, up to date, workspace protection, refusals), the merge
commit of `github.commit` with `merge: true` (two parents, verification against the preparation, decisions, marker
check, merged modes, protected names and links) and the fast-forward push that never merges the pull request."""

from __future__ import annotations

import base64
import copy
from pathlib import Path
from typing import Any

from agent_guest.schema import SchemaSet
from agent_runner.github import merge_prep
from agent_runner.orgs.db import session as store_session

from ..sync.fakehub import CONTRACTS
from .conftest import REPO, GitHub, seed
from .run10_kit import R10, refused, run10

SCHEMAS = SchemaSet(CONTRACTS)
APP = "src/app.py"


def world(**extra_files: Any) -> dict[str, Any]:
    """`seed()`; `feature` (pull request #1) changes `src/app.py` to `x = 2` and optionally more files."""
    data = copy.deepcopy(seed())
    feature = data["repositories"][0]["branches"][1]
    feature["commits"][0]["files"].update(extra_files)
    return data


def advance(github: GitHub, branch: str, files: dict[str, Any]) -> str:
    return str(github.double.world.add_commit({"repo": REPO, "branch": branch, "commit": {"files": files}}))


def repo(github: GitHub) -> Any:
    return github.double.world.repo("octo-org", "hello")


def files_of(github: GitHub, sha: str) -> dict[str, tuple[str, bytes]]:
    objects = repo(github).objects
    return {path: (e.mode, objects.blob(e.sha)) for path, e in objects.files(objects.commit(sha).tree).items()}


def root_of(r: R10, sid: str) -> Path:
    return r.guest.work(sid) / "repos" / "octo-org" / "hello"


async def prep_of(r: R10, sid: str) -> merge_prep.Prep | None:
    async with store_session(r.ok.env.pool) as conn:
        return await merge_prep.latest(conn, sid, REPO)


def test_conflict_fixed_with_a_merge_commit_and_a_fast_forward_push(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=world())
    head = repo(github).refs["refs/heads/feature"]
    tip = advance(github, "main", {APP: "x = 3\n", "docs/new.md": "new\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert SCHEMAS.errors("ops/github.merge_base.result.json", op.result) == []
        assert op.result["conflicts"] == [{"path": APP, "kind": "content"}]
        assert (op.result["branch"], op.result["base"], op.result["head_sha"], op.result["base_sha"]) == (
            "feature",
            "main",
            head,
            tip,
        )
        assert (op.result["up_to_date"], op.result["conflicts_total"], op.result["dir"]) == (
            False,
            1,
            "repos/octo-org/hello",
        )
        root = root_of(r, sid)
        assert (root / "docs" / "new.md").read_text() == "new\n"
        assert (root / APP).read_text() == (
            "<<<<<<< pull request (feature)\nx = 2\n||||||| common ancestor\nx = 1\n=======\nx = 3\n"
            ">>>>>>> base (main)\n"
        )
        # a normal commit while the merge is pending would carry the base's changes as the pull request's own
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "feature", "message": "Plain."})
        assert refused(op) == "merge_pending"
        # markers left: refused before the registration
        params = {"repo": REPO, "branch": "feature", "message": "Merge main into feature.", "merge": True}
        op = await r.run(sid, "github.commit", params)
        assert refused(op) == "conflict_markers"
        assert APP in op.answer["error"]["message"]  # type: ignore[index]
        (root / APP).write_text("x = 4\n")
        op = await r.run(sid, "github.commit", params)
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert SCHEMAS.errors("ops/github.commit.result.json", op.result) == []
        merge = op.result["commit_sha"]
        assert op.result["merge_parent"] == tip
        stored = repo(github).objects.commit(merge)
        assert stored.parents == (head, tip)
        assert files_of(github, merge)[APP][1] == b"x = 4\n"
        assert files_of(github, merge)["docs/new.md"][1] == b"new\n"
        found = await prep_of(r, sid)
        assert found is not None
        assert (found.state, found.commit_sha) == ("committed", merge)
        op = await r.run(sid, "github.push", {"repo": REPO, "branch": "feature", "commit_sha": merge})
        assert op.state == "succeeded", op.answer
        assert repo(github).refs["refs/heads/feature"] == merge
        assert repo(github).item(1).state == "open"  # never merged
        found = await prep_of(r, sid)
        assert found is not None
        assert found.state == "pushed"
        # pushed: a normal commit is allowed again, and the base is contained now
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert op.result["up_to_date"] is True

    run10(store_dsn, github, tmp_path, body)


def test_up_to_date_touches_nothing(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert (op.result["up_to_date"], op.result["conflicts"], op.result["files"]) == (True, [], 0)
        assert not root_of(r, sid).exists()
        assert await prep_of(r, sid) is None
        params = {"repo": REPO, "branch": "feature", "message": "Merge.", "merge": True}
        assert refused(await r.run(sid, "github.commit", params)) == "merge_outdated"

    run10(store_dsn, github, tmp_path, body)


def test_workspace_protection(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.checkout", {"repo": REPO, "ref": "feature"})
        assert op.state == "succeeded", op.answer
        root = root_of(r, sid)
        # equal to what the checkout put there: clean
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        # the preparation's own import is remembered as well; an uncommitted edit is not
        (root / "README.md").write_text("mine\n")
        (root / "extra.txt").write_text("extra\n")
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert refused(op) == "workspace_dirty"
        message = op.answer["error"]["message"]  # type: ignore[index]
        assert "README.md" in message
        assert "extra.txt" in message
        assert "discard: true" in message
        assert (root / "README.md").read_text() == "mine\n"  # nothing replaced
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1, "discard": True})
        assert op.state == "succeeded", op.answer
        assert (root / "README.md").read_text() == "hello\nworld\n"
        # a checkout ends the preparation: a normal commit is no longer `merge_pending`
        op = await r.run(sid, "github.checkout", {"repo": REPO, "ref": "feature"})
        assert op.state == "succeeded", op.answer
        assert await prep_of(r, sid) is None

    run10(store_dsn, github, tmp_path, body)


def test_a_workspace_equal_to_the_head_is_clean(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        root = root_of(r, sid)
        (root / "src").mkdir(parents=True)
        (root / "README.md").write_text("hello\nworld\n")
        (root / APP).write_text("x = 2\n")
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        other = await r.session(2)
        (r.guest.work(other) / "repos" / "octo-org" / "hello").mkdir(parents=True)
        # an empty directory has nothing to lose
        assert (await r.run(other, "github.merge_base", {"repo": REPO, "number": 1})).state == "succeeded"

    run10(store_dsn, github, tmp_path, body)


def test_merge_commit_must_match_the_preparation(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n", "docs/new.md": "new\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})).state == "succeeded"
        root = root_of(r, sid)
        (root / APP).write_text("x = 4\n")
        (root / "docs" / "new.md").unlink()  # drops the base's clean change
        params: dict[str, Any] = {"repo": REPO, "branch": "feature", "message": "Merge.", "merge": True}
        op = await r.run(sid, "github.commit", params)
        assert refused(op) == "conflicts_unresolved"
        assert "docs/new.md" in op.answer["error"]["message"]  # type: ignore[index]
        params["decisions"] = [{"path": "docs/new.md", "choice": "edited"}]  # absent is no edit
        assert refused(await r.run(sid, "github.commit", params)) == "conflicts_unresolved"
        params["decisions"] = [{"path": "docs/new.md", "choice": "deleted"}]
        op = await r.run(sid, "github.commit", params)
        assert op.state == "succeeded", op.answer
        assert "docs/new.md" not in files_of(github, op.result["commit_sha"])  # type: ignore[index]

    run10(store_dsn, github, tmp_path, body)


def test_merge_commit_needs_a_current_preparation(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        params: dict[str, Any] = {"repo": REPO, "branch": "feature", "message": "Merge.", "merge": True}
        assert refused(await r.run(sid, "github.commit", params)) == "merge_outdated"  # nothing prepared
        assert (await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})).state == "succeeded"
        (root_of(r, sid) / APP).write_text("x = 4\n")
        other = {**params, "branch": "topic"}
        assert refused(await r.run(sid, "github.commit", other)) == "merge_outdated"  # another branch
        assert refused(await r.run(await r.session(2), "github.commit", params)) == "merge_outdated"  # other session
        advance(github, "feature", {"late.txt": "late\n"})
        assert refused(await r.run(sid, "github.commit", params)) == "merge_outdated"  # the branch moved

    run10(store_dsn, github, tmp_path, body)


def test_conflicts_without_markers_need_decisions(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    binary_head, binary_base = bytes(range(256)) * 2, bytes(range(255, -1, -1)) * 2
    data = world(
        **{
            "logo.bin": {"base64": base64.b64encode(binary_head).decode()},
            "gone.txt": None,
            "CLAUDE.md": "head rules\n",
            "AGENTS.md": "one\ntwo\nthree\nfour\nfive changed by head\n",
            "link": {"target": "README.md"},
        }
    )
    main = data["repositories"][0]["branches"][0]["commits"][0]["files"]
    main.update(
        {
            "logo.bin": {"base64": base64.b64encode(bytes(256)).decode()},
            "gone.txt": "old\n",
            "CLAUDE.md": "rules\n",
            "AGENTS.md": "one\ntwo\nthree\nfour\nfive\n",
            "run.sh": "echo run\n",
            "link": {"target": "src/app.py"},
        }
    )
    github = start_github(world=data)
    tip = advance(
        github,
        "main",
        {
            "logo.bin": {"base64": base64.b64encode(binary_base).decode()},
            "gone.txt": "modified by base\n",
            "CLAUDE.md": "base rules\n",
            "AGENTS.md": "one changed by base\ntwo\nthree\nfour\nfive\n",
            "run.sh": {"content": "echo run\n", "mode": "100755"},
            "link": {"target": "docs/a.md"},
        },
    )

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        kinds = {item["path"]: item["kind"] for item in op.result["conflicts"]}
        assert kinds == {
            "CLAUDE.md": "protected",
            "gone.txt": "modify_delete",
            "link": "protected",
            "logo.bin": "binary",
        }
        root = root_of(r, sid)
        assert (root / "logo.bin").read_bytes() == binary_head  # the head's version is kept
        assert (root / "gone.txt").read_text() == "modified by base\n"  # the modified version is kept
        assert not (root / "CLAUDE.md").exists()  # protected names never reach the workspace
        params: dict[str, Any] = {"repo": REPO, "branch": "feature", "message": "Merge.", "merge": True}
        op = await r.run(sid, "github.commit", params)
        assert refused(op) == "conflicts_unresolved"
        message = op.answer["error"]["message"]  # type: ignore[index]
        assert all(path in message for path in kinds)
        (root / "logo.bin").write_bytes(binary_base)
        params["decisions"] = [
            {"path": "logo.bin", "choice": "base"},
            {"path": "gone.txt", "choice": "edited"},
            {"path": "CLAUDE.md", "choice": "head"},
            {"path": "link", "choice": "base"},
        ]
        op = await r.run(sid, "github.commit", params)
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        merged = files_of(github, op.result["commit_sha"])
        base_files = files_of(github, tip)
        assert merged["logo.bin"][1] == binary_base
        assert merged["gone.txt"][1] == b"modified by base\n"
        assert merged["CLAUDE.md"][1] == b"head rules\n"
        assert merged["link"] == base_files["link"]
        assert merged["AGENTS.md"][1] == b"one changed by base\ntwo\nthree\nfour\nfive changed by head\n"
        assert merged["run.sh"][0] == "100755"  # the base's mode change survives

    run10(store_dsn, github, tmp_path, body)


def test_marker_lines_the_sides_already_contain_are_no_markers(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    fixture = "<<<<<<< ours\nkept\n>>>>>>> theirs\n"
    data = world(**{"doc.md": "Title head\n=======\n", "fixture.txt": fixture + "head\n"})
    data["repositories"][0]["branches"][0]["commits"][0]["files"].update(
        {"doc.md": "Title\n=======\n", "fixture.txt": fixture}
    )
    github = start_github(world=data)
    advance(github, "main", {"doc.md": "Title base\n=======\n", APP: "x = 3\n", "fixture.txt": "base\n" + fixture})

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer
        root = root_of(r, sid)
        (root / "doc.md").write_text("Title both\n=======\n")
        (root / APP).write_text("x = 4\n")
        (root / "fixture.txt").write_text("base\n" + fixture + "head\nmine\n")
        params: dict[str, Any] = {
            "repo": REPO,
            "branch": "feature",
            "message": "Merge.",
            "merge": True,
            "decisions": [{"path": "fixture.txt", "choice": "edited"}],
        }
        op = await r.run(sid, "github.commit", params)
        assert op.state == "succeeded", op.answer

    run10(store_dsn, github, tmp_path, body)


def test_new_markers_in_other_files_refuse_the_merge_commit(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})).state == "succeeded"
        root = root_of(r, sid)
        (root / APP).write_text("x = 4\n")
        (root / "notes.txt").write_text("<<<<<<< mine\na\n=======\nb\n>>>>>>> yours\n")
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "feature", "message": "M.", "merge": True})
        assert refused(op) == "conflict_markers"
        assert "notes.txt" in op.answer["error"]["message"]  # type: ignore[index]

    run10(store_dsn, github, tmp_path, body)


def test_refusals_of_the_preparation(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    data = world()
    hello = data["repositories"][0]
    hello["branches"].append({"name": "done", "from": "main", "commits": [{"files": {"d.txt": "d\n"}}]})
    hello["pulls"].append({"title": "Done", "head": "done", "base": "main", "draft": False, "user": "octo-user"})
    github = start_github(world=data)
    repo(github).item(2).state = "closed"

    async def body(r: R10) -> None:
        sid = await r.session()
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 2})) == "merge_unsupported"
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 99})) == "not_found"
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 0})) == "params_invalid"
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1, "discard": "yes"})
        assert refused(op) == "params_invalid"

    run10(store_dsn, github, tmp_path, body)


def test_refused_and_unpushed_commits_never_make_a_workspace_clean(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    """Code review H1/H2: only a succeeded commit's snapshot is remembered, and only while its commit is on the pull
    request's head or on the branch it was committed to."""
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.checkout", {"repo": REPO, "ref": "feature"})).state == "succeeded"
        root = root_of(r, sid)
        (root / "README.md").write_text("token ghp_" + "a" * 36 + "\n")
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "feature", "message": "Secret."})
        assert refused(op) == "secret_detected"
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == "workspace_dirty"
        (root / "README.md").write_text("mine\n")
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "feature", "message": "Mine."})
        assert op.state == "succeeded", op.answer
        mine = op.result["commit_sha"]  # type: ignore[index]
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert refused(op) == "workspace_dirty"
        assert "not pushed" in op.answer["error"]["message"]  # type: ignore[index]
        op = await r.run(sid, "github.push", {"repo": REPO, "branch": "feature", "commit_sha": mine})
        assert op.state == "succeeded", op.answer
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer

    run10(store_dsn, github, tmp_path, body)


def test_a_commit_pushed_to_another_branch_is_safe_to_replace(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.checkout", {"repo": REPO, "ref": "topic"})).state == "succeeded"
        (root_of(r, sid) / "docs" / "a.md").write_text("b\n")
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "topic", "message": "Topic."})
        assert op.state == "succeeded", op.answer
        sha = op.result["commit_sha"]  # type: ignore[index]
        assert refused(await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})) == "workspace_dirty"
        assert (await r.run(sid, "github.push", {"repo": REPO, "branch": "topic", "commit_sha": sha})).state
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer

    run10(store_dsn, github, tmp_path, body)


def test_skipped_entries_and_deleted_head_files(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.checkout", {"repo": REPO, "ref": "feature"})).state == "succeeded"
        root = root_of(r, sid)
        (root / "mine.lnk").symlink_to("README.md")
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert refused(op) == "workspace_dirty"
        assert "mine.lnk" in op.answer["error"]["message"]  # type: ignore[index]
        (root / "mine.lnk").unlink()
        assert (await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})).state == "succeeded"
        (root / APP).write_text("x = 4\n")
        (root / "README.md").unlink()  # a head file the merge did not touch
        params: dict[str, Any] = {"repo": REPO, "branch": "feature", "message": "Merge.", "merge": True}
        op = await r.run(sid, "github.commit", params)
        assert refused(op) == "conflicts_unresolved"
        assert "README.md" in op.answer["error"]["message"]  # type: ignore[index]
        params["decisions"] = [{"path": "README.md", "choice": "deleted"}]
        op = await r.run(sid, "github.commit", params)
        assert op.state == "succeeded", op.answer
        merge = op.result["commit_sha"]  # type: ignore[index]
        # a push of the merge commit to another branch does not finish the preparation
        assert (await r.run(sid, "github.push", {"repo": REPO, "branch": "elsewhere", "commit_sha": merge})).state
        found = await prep_of(r, sid)
        assert found is not None
        assert found.state == "committed"
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "feature", "message": "Plain."})
        assert refused(op) == "merge_pending"

    run10(store_dsn, github, tmp_path, body)


def test_a_pushed_commit_the_head_moved_past_is_safe_to_replace(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=world())
    advance(github, "main", {APP: "x = 3\n"})

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.checkout", {"repo": REPO, "ref": "feature"})).state == "succeeded"
        (root_of(r, sid) / "README.md").write_text("mine\n")
        op = await r.run(sid, "github.commit", {"repo": REPO, "branch": "feature", "message": "Mine."})
        assert op.state == "succeeded", op.answer
        sha = op.result["commit_sha"]  # type: ignore[index]
        assert (await r.run(sid, "github.push", {"repo": REPO, "branch": "feature", "commit_sha": sha})).state
        advance(github, "feature", {"other.txt": "someone else\n"})  # the head moves past the pushed commit
        op = await r.run(sid, "github.merge_base", {"repo": REPO, "number": 1})
        assert op.state == "succeeded", op.answer

    run10(store_dsn, github, tmp_path, body)
