"""U-DBL-github (HOT-33): accounts with the role read or triage (``readonly``), forks (``fork_of``, ``allow_forking``,
``GET``/``POST …/forks``, ``POST …/merge-upstream``) and pull requests from a fork, as GitHub answers them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from agent_doubles.github.repos import SeedError, World

REPO = "octo-org/hello"
READER = "classic-reader"  # not a secret: the double only compares it
APP_READER = "app-reader"
OTHER = "classic-other"


def forked_seed(seed: Callable[[], Any], *, allow: bool = True, fork: bool = True) -> dict[str, Any]:
    """`base_seed` with `octo-user` reading `octo-org/hello` only (a classic token and an App token) and, with `fork`,
    its fork `octo-user/hello-mine` (another name than the parent's)."""
    data: dict[str, Any] = seed()
    user = data["users"][0]
    user["readonly"] = [REPO]
    user["tokens"] += [{"token": READER, "kind": "classic", "scopes": ["repo"]}, APP_READER]
    data["users"][1]["tokens"].append({"token": OTHER, "kind": "classic", "scopes": ["repo"]})
    data["repositories"][0]["allow_forking"] = allow
    if fork:
        data["repositories"].append({"full_name": "octo-user/hello-mine", "fork_of": REPO})
    data["installations"].append({"id": 32, "account": "octo-user", "repository_selection": "all"})
    return data


def test_a_reader_reads_but_never_writes_contents(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    gh = start_github(forked_seed(seed))
    found = gh.get(f"/repos/{REPO}", token=READER)
    assert found["permissions"] == {"admin": False, "maintain": False, "push": False, "triage": True, "pull": True}
    assert (found["fork"], found["allow_forking"]) == (False, True)
    main = found["default_branch"]
    sha = gh.get(f"/repos/{REPO}/git/ref/heads/{main}", token=READER)["object"]["sha"]
    body = {"ref": "refs/heads/work", "sha": sha}
    assert gh.api("POST", f"/repos/{REPO}/git/refs", body, token=READER).status == 404
    assert gh.api("POST", f"/repos/{REPO}/git/refs", body, token=APP_READER).status == 403
    # the role triage may still open issues
    assert gh.api("POST", f"/repos/{REPO}/issues", {"title": "x"}, token=READER).status == 201


def test_forks_are_listed_created_and_named_like_github(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    gh = start_github(forked_seed(seed))
    mine = gh.get("/repos/octo-user/hello-mine", token=READER)
    assert (mine["fork"], mine["parent"]["full_name"], mine["source"]["full_name"]) == (True, REPO, REPO)
    assert mine["permissions"]["push"] is True
    listed = gh.get(f"/repos/{REPO}/forks", token=READER)
    assert [item["full_name"] for item in listed] == ["octo-user/hello-mine"]
    # the existing fork is answered, whatever its name
    again = gh.api("POST", f"/repos/{REPO}/forks", {"default_branch_only": False}, token=READER)
    assert (again.status, again.json()["full_name"]) == (202, "octo-user/hello-mine")
    # another account forks: a new repository in its account, every branch, the shared objects
    made = gh.api("POST", f"/repos/{REPO}/forks", {}, token=OTHER)
    assert (made.status, made.json()["full_name"], made.json()["fork"]) == (202, "other-user/hello", True)
    world = gh.double.world
    created = world.repo("other-user", "hello")
    parent = world.repo("octo-org", "hello")
    assert created is not None
    assert parent is not None
    assert created.objects is parent.objects
    assert created.refs == {ref: sha for ref, sha in parent.refs.items() if ref.startswith("refs/heads/")}
    # a fork of a fork names the root of the network as its source
    gh.api("POST", "/repos/other-user/hello/forks", {}, token=READER)
    nested = gh.get("/repos/octo-user/hello", token=READER)
    assert (nested["parent"]["full_name"], nested["source"]["full_name"]) == ("other-user/hello", REPO)
    # a name the account has taken already gets a number
    third = {"token": "third-token", "kind": "classic", "scopes": ["repo"]}
    gh.control("POST", "seed", {"users": [{"login": "third-user", "collaborator": [REPO], "tokens": [third]}]})
    gh.control("POST", "seed", {"repositories": [{"full_name": "third-user/hello"}]})
    numbered = gh.api("POST", f"/repos/{REPO}/forks", {}, token="third-token")
    assert numbered.json()["full_name"] == "third-user/hello-1"


def test_a_repository_that_does_not_allow_forks_refuses_a_new_one(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    gh = start_github(forked_seed(seed, allow=False, fork=False))
    assert gh.get(f"/repos/{REPO}", token=READER)["allow_forking"] is False
    refused = gh.api("POST", f"/repos/{REPO}/forks", {}, token=READER)
    assert refused.status == 403
    assert gh.get(f"/repos/{REPO}/forks", token=READER) == []


def test_merge_upstream_fast_forwards_a_fork_that_is_behind(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    gh = start_github(forked_seed(seed))
    fork = "/repos/octo-user/hello-mine"
    synced = gh.api("POST", f"{fork}/merge-upstream", {"branch": "main"}, token=READER)
    assert (synced.status, synced.json()["merge_type"]) == (200, "none")
    moved = gh.control("POST", "commits", {"repo": REPO, "branch": "main", "commit": {"files": {"new.txt": "n\n"}}})
    upstream = moved.json()["sha"]
    synced = gh.api("POST", f"{fork}/merge-upstream", {"branch": "main"}, token=READER)
    assert synced.json() == {
        "message": "Successfully fetched and fast-forwarded from upstream octo-org:main.",
        "merge_type": "fast-forward",
        "base_branch": "octo-org:main",
    }
    assert gh.get(f"{fork}/git/ref/heads/main", token=READER)["object"]["sha"] == upstream
    # diverged: a commit only in the fork and one only upstream
    gh.control("POST", "commits", {"repo": "octo-user/hello-mine", "branch": "main", "commit": {"files": {"a": "a"}}})
    gh.control("POST", "commits", {"repo": REPO, "branch": "main", "commit": {"files": {"b": "b"}}})
    assert gh.api("POST", f"{fork}/merge-upstream", {"branch": "main"}, token=READER).status == 409
    # a branch missing on one side, a repository that is no fork, a body without a branch
    assert gh.api("POST", f"{fork}/merge-upstream", {"branch": "nope"}, token=READER).status == 422
    assert gh.api("POST", "/repos/octo-org/second/merge-upstream", {"branch": "main"}).status == 422
    assert gh.api("POST", f"{fork}/merge-upstream", {}, token=READER).status == 422


def test_a_pull_request_comes_from_the_fork_of_another_account(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    gh = start_github(forked_seed(seed))
    fork = "/repos/octo-user/hello-mine"
    main = gh.get(f"{fork}/git/ref/heads/main", token=READER)["object"]["sha"]
    tree = gh.get(f"{fork}/git/commits/{main}", token=READER)["tree"]["sha"]
    commit = gh.api("POST", f"{fork}/git/commits", {"message": "Fork.", "tree": tree, "parents": [main]}, token=READER)
    gh.api("POST", f"{fork}/git/refs", {"ref": "refs/heads/work", "sha": commit.json()["sha"]}, token=READER)
    body = {"title": "From the fork", "head": "octo-user:work", "base": "main"}
    created = gh.api("POST", f"/repos/{REPO}/pulls", body, token=READER)
    assert created.status == 201, created.body
    pull = created.json()
    assert (pull["head"]["label"], pull["head"]["repo"]["full_name"]) == ("octo-user:work", "octo-user/hello-mine")
    assert pull["head"]["sha"] == commit.json()["sha"]
    listed = gh.get(f"/repos/{REPO}/pulls", token=READER, headers=None)
    assert [item["number"] for item in listed] == [pull["number"]]
    found = gh.api("GET", f"/repos/{REPO}/pulls?head=octo-user:work&base=main&state=open", token=READER).json()
    assert [item["number"] for item in found] == [pull["number"]]
    assert gh.api("GET", f"/repos/{REPO}/pulls?head=octo-org:work", token=READER).json() == []
    again = gh.api("POST", f"/repos/{REPO}/pulls", body, token=READER)
    assert again.status == 422
    assert "A pull request already exists for octo-user:work." in again.body.decode()
    commits = gh.get(f"/repos/{REPO}/pulls/{pull['number']}/commits", token=READER)
    assert [item["sha"] for item in commits] == [commit.json()["sha"]]
    # an account without a fork has no head here
    unknown = gh.api("POST", f"/repos/{REPO}/pulls", {**body, "head": "nobody:work"}, token=READER)
    assert unknown.status == 422
    merged = gh.api("PUT", f"/repos/{REPO}/pulls/{pull['number']}/merge", {}, token="double-user-token")
    assert merged.status == 200, merged.body
    message = gh.double.world.repo("octo-org", "hello").objects.commit(merged.json()["sha"]).message
    assert message == f"Merge pull request #{pull['number']} from octo-user/work"


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ({"repositories": [{"full_name": "a/b", "fork_of": "a/missing"}]}, "fork_of names a repository"),
        ({"users": [{"login": "u", "readonly": ["no-slash"]}]}, "user.readonly must name repositories"),
    ],
)
def test_fork_seeds_are_checked(data: dict[str, Any], text: str) -> None:
    with pytest.raises(SeedError, match=text):
        World().seed(data)
