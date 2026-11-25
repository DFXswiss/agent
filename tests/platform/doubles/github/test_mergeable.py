"""U-DBL-github (KOR-48, ADR-084 sections 2 and 4): the GitHub double computes ``mergeable`` asynchronously like
GitHub, detects conflicts between the base tip and the head, advances a branch through ``POST /_double/commits``, and
serves merge commits (two parents) to compare, trees, blobs, tarballs and the fast-forward check of ref updates. The
merge of a pull request (KOR-51) is `test_reviews.py`.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest

from agent_doubles.github.objects import EXEC_MODE
from agent_doubles.github.repos import SeedError, World

REPO = "/repos/octo-org/hello"
PULL_ROUTE = "GET /repos/{owner}/{repo}/pulls/{number}"


def seed(*, draft: bool = False, state: str = "open") -> dict[str, Any]:
    """`octo-org/hello`: `main` (README.md, src/app.py), `feature` from main changing src/app.py, pull request #1
    feature -> main."""
    return {
        "users": [{"login": "octo-user", "id": 101, "tokens": ["double-user-token"]}],
        "repositories": [
            {
                "full_name": "octo-org/hello",
                "branches": [
                    {"name": "main", "commits": [{"files": {"README.md": "hello\n", "src/app.py": "x = 1\n"}}]},
                    {"name": "feature", "from": "main", "commits": [{"files": {"src/app.py": "x = 2\n"}}]},
                    {"name": "topic", "from": "main", "commits": [{"files": {"docs/a.md": "a\n"}}]},
                ],
                "pulls": [
                    {"title": "Feature", "head": "feature", "base": "main", "draft": draft, "user": "octo-user"},
                    {"title": "Closed", "head": "feature", "base": "main", "state": "closed", "user": "octo-user"},
                ][: 1 if state == "open" else 2],
            }
        ],
        "installations": [{"account": "octo-org", "repository_selection": "all", "users": ["octo-user"]}],
    }


def mergeable(gh: Any, number: int = 1) -> tuple[Any, Any]:
    pull = gh.get(f"{REPO}/pulls/{number}")
    return pull["mergeable"], pull["mergeable_state"]


def advance(gh: Any, branch: str, files: dict[str, Any]) -> str:
    answer = gh.control("POST", "commits", {"repo": "octo-org/hello", "branch": branch, "commit": {"files": files}})
    assert answer.status == 200, answer.body
    sha: str = answer.json()["sha"]
    return sha


def head(gh: Any, branch: str) -> str:
    sha: str = gh.get(f"{REPO}/git/ref/heads/{branch}")["object"]["sha"]
    return sha


def test_the_first_read_after_a_change_is_unknown_and_starts_the_computation(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (True, "clean")
    assert gh.get(f"{REPO}/pulls/1")["rebaseable"] is True
    assert mergeable(gh) == (True, "clean")
    # the base moves without touching the head's file: unknown once, then clean again
    advance(gh, "main", {"README.md": "hello\nthere\n"})
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (True, "clean")
    # the base moves with a conflicting change of the head's file
    advance(gh, "main", {"src/app.py": "x = 3\n"})
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (False, "dirty")
    assert gh.get(f"{REPO}/pulls/1")["rebaseable"] is False
    # the head moves (the conflict is resolved in the head by taking the base's text): unknown, then clean
    advance(gh, "feature", {"src/app.py": "x = 3\n"})
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (True, "clean")
    # the list omits the fields; nothing here merged the pull request
    listed = gh.get(f"{REPO}/pulls")
    assert "mergeable" not in listed[0]
    pull = gh.get(f"{REPO}/pulls/1")
    assert (pull["state"], pull["merged"], pull["merged_at"], pull["merge_commit_sha"]) == ("open", False, None, None)
    assert gh.state()["ok"] is True


def test_a_draft_says_draft_with_and_without_a_conflict(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed(draft=True))
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (True, "draft")
    advance(gh, "main", {"src/app.py": "x = 3\n"})
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (False, "draft")


def test_a_closed_pull_request_has_no_mergeability(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed(state="closed"))
    for _ in range(2):
        assert mergeable(gh, 2) == (None, "unknown")
        assert gh.get(f"{REPO}/pulls/2")["rebaseable"] is None


def test_a_created_pull_request_answers_unknown(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    created = gh.api("POST", f"{REPO}/pulls", {"title": "Topic", "head": "topic", "base": "main"}).json()
    assert (created["mergeable"], created["mergeable_state"]) == (None, "unknown")
    assert mergeable(gh, created["number"]) == (None, "unknown")
    assert mergeable(gh, created["number"]) == (True, "clean")


def test_the_fault_keeps_reads_unknown_for_n_reads(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    armed = gh.control("POST", "faults", {"faults": [{"route": PULL_ROUTE, "kind": "mergeable_unknown", "times": 3}]})
    assert armed.status == 200
    assert gh.state()["done"] is False
    assert [mergeable(gh) for _ in range(4)] == [(None, "unknown")] * 3 + [(True, "clean")]
    assert gh.state()["done"] is True


def test_the_commit_control_route_refuses_what_it_cannot_apply(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    for body, error in (
        ([], "commit request must be an object"),
        ({"repo": "octo-org/hello", "branch": "main", "extra": 1}, "commit request: unknown keys ['extra']"),
        ({"repo": "octo-org/none", "branch": "main"}, "repository 'octo-org/none' is not seeded"),
        ({"repo": "octo-org/hello", "branch": "none"}, "branch 'none' does not exist"),
        ({"repo": "octo-org/hello", "branch": "main", "commit": {"files": {"gone": None}}}, "cannot delete missing"),
    ):
        refused = gh.control("POST", "commits", body)
        assert (refused.status, refused.json()["error"][: len(error)]) == (400, error)
    assert gh.control("GET", "commits").status == 405
    before = head(gh, "main")
    sha = gh.control("POST", "commits", {"repo": "OCTO-ORG/Hello", "branch": "main"}).json()["sha"]
    commit = gh.get(f"{REPO}/git/commits/{sha}")
    assert [p["sha"] for p in commit["parents"]] == [before]
    assert commit["message"] == "Seed commit."
    assert head(gh, "main") == sha


def test_merge_commits_serve_compare_trees_blobs_tarballs_and_fast_forward(start_github: Callable[..., Any]) -> None:
    """The flow of `github.merge_base`, `github.commit` with `merge: true` and `github.push` against the double."""
    gh = start_github(seed())
    advance(gh, "main", {"src/app.py": "x = 3\n", "docs/new.md": "new\n"})
    base_tip, pr_head = head(gh, "main"), head(gh, "feature")
    first = gh.get(f"{REPO}/compare/{base_tip}...{pr_head}")
    assert first["status"] == "diverged"
    merge_base = first["merge_base_commit"]["sha"]
    assert merge_base == gh.get(f"{REPO}/git/commits/{base_tip}")["parents"][0]["sha"]
    for sha in (merge_base, pr_head, base_tip):
        listed = gh.get(f"{REPO}/git/trees/{sha}?recursive=1")
        assert listed["truncated"] is False
    base_tree = gh.get(f"{REPO}/git/trees/{base_tip}?recursive=1")["tree"]
    blob = next(entry["sha"] for entry in base_tree if entry["path"] == "docs/new.md")
    assert gh.get(f"{REPO}/git/blobs/{blob}")["content"] == "bmV3Cg==\n"
    # the resolved merge: the base's tree with the head's resolution of src/app.py, parents [head, base tip]
    resolved = gh.api(
        "POST",
        f"{REPO}/git/trees",
        {
            "base_tree": gh.get(f"{REPO}/git/commits/{base_tip}")["tree"]["sha"],
            "tree": [{"path": "src/app.py", "mode": "100644", "type": "blob", "content": "x = 4\n"}],
        },
    ).json()["sha"]
    made = gh.api(
        "POST", f"{REPO}/git/commits", {"message": "Merge main.", "tree": resolved, "parents": [pr_head, base_tip]}
    )
    merge = made.json()
    assert [p["sha"] for p in merge["parents"]] == [pr_head, base_tip]
    sha = merge["sha"]
    # compare sees the merge commit containing both sides; its merge base with the base tip is the base tip
    after = gh.get(f"{REPO}/compare/{base_tip}...{sha}")
    assert (after["status"], after["ahead_by"], after["behind_by"]) == ("ahead", 2, 0)
    assert after["merge_base_commit"]["sha"] == base_tip
    back = gh.get(f"{REPO}/compare/{sha}...{base_tip}")
    assert (back["status"], back["behind_by"]) == ("behind", 2)
    identical = gh.get(f"{REPO}/compare/{sha}...{sha}")
    assert identical["status"] == "identical"
    # tree, tarball and blob of the merge commit
    paths = {entry["path"] for entry in gh.get(f"{REPO}/git/trees/{sha}?recursive=1")["tree"]}
    assert {"README.md", "src/app.py", "docs/new.md"} <= paths
    location = urlsplit(gh.api("GET", f"{REPO}/tarball/{sha}").headers["location"])
    archive = gh.client.request("GET", f"{location.path}?{location.query}")
    with tarfile.open(fileobj=io.BytesIO(archive.body)) as tar:
        member = tar.extractfile(f"octo-org-hello-{sha[:7]}/src/app.py")
        assert member is not None
        assert member.read() == b"x = 4\n"
    # the push: a fast-forward of the head branch (its old head is the first parent); a stale merge is refused
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (False, "dirty")
    assert gh.api("PATCH", f"{REPO}/git/refs/heads/feature", {"sha": sha, "force": False}).status == 200
    assert mergeable(gh) == (None, "unknown")
    assert mergeable(gh) == (True, "clean")
    stale = gh.api("POST", f"{REPO}/git/commits", {"message": "Old.", "tree": resolved, "parents": [pr_head, base_tip]})
    refused = gh.api("PATCH", f"{REPO}/git/refs/heads/feature", {"sha": stale.json()["sha"]})
    assert (refused.status, refused.json()["message"]) == (422, "Update is not a fast forward")
    assert gh.get(f"{REPO}/pulls/1")["merged"] is False
    assert gh.state()["ok"] is True


def world_of(*branches: dict[str, Any]) -> World:
    world = World()
    world.seed(
        {
            "users": [{"login": "octo-user"}],
            "repositories": [{"full_name": "octo-org/hello", "branches": list(branches)}],
        }
    )
    return world


BASE = {"name": "main", "commits": [{"files": {"a.txt": "a\n", "d/x.txt": "x\n", "f": "f\n"}}]}


@pytest.mark.parametrize(
    ("ours", "theirs", "clean"),
    [
        ({"a.txt": "ours\n"}, {"d/x.txt": "theirs\n"}, True),  # different paths
        ({"a.txt": "same\n"}, {"a.txt": "same\n"}, True),  # the same change on both sides
        ({"a.txt": "ours\n"}, {"a.txt": "theirs\n"}, False),  # changed differently
        ({"a.txt": "ours\n"}, {"a.txt": None}, False),  # modified against deleted
        ({"a.txt": {"content": "a\n", "mode": EXEC_MODE}}, {"a.txt": "theirs\n"}, False),  # mode against content
        ({"new.txt": "1\n"}, {"new.txt": "2\n"}, False),  # added differently on both sides
        ({"n": "file\n"}, {"n/z.txt": "z\n"}, False),  # a file where the other side added below
        ({"d/y.txt": "y\n"}, {"d/x.txt": None, "d": "file\n"}, False),  # the same the other way round
        ({"f": None, "f/z.txt": "z\n"}, {"README": "r\n"}, True),  # a file became a directory on one side only
        ({"d/y.txt": "y\n"}, {"f": "theirs\n"}, True),  # a file the other side did not touch is no block
    ],
)
def test_conflicts_between_the_base_tip_and_the_head(ours: dict[str, Any], theirs: dict[str, Any], clean: bool) -> None:
    world = world_of(
        BASE,
        {"name": "head", "from": "main", "commits": [{"files": ours}]},
        {"name": "base", "from": "main", "commits": [{"files": theirs}]},
    )
    repo = world.repo("octo-org", "hello")
    assert repo is not None
    base, pr_head = repo.branch("base"), repo.branch("head")
    assert base is not None
    assert pr_head is not None
    assert repo.merges_cleanly(base, pr_head) is clean
    assert repo.merges_cleanly(pr_head, base) is clean


def test_unrelated_histories_merge_against_the_empty_tree() -> None:
    world = world_of(
        {"name": "main", "commits": [{"files": {"a.txt": "a\n"}}]},
        {"name": "other", "commits": [{"files": {"b.txt": "b\n"}}]},
        {"name": "clash", "commits": [{"files": {"a.txt": "else\n"}}]},
    )
    repo = world.repo("octo-org", "hello")
    assert repo is not None
    main, other, clash = (repo.branch(name) or "" for name in ("main", "other", "clash"))
    assert repo.merge_base(main, other) is None
    assert repo.merges_cleanly(main, other) is True
    assert repo.merges_cleanly(main, clash) is False


def test_add_commit_refuses_a_body_that_is_no_object() -> None:
    with pytest.raises(SeedError, match="commit must be an object"):
        world_of(BASE).add_commit({"repo": "octo-org/hello", "branch": "main", "commit": []})
