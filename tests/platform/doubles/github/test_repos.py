"""U-DBL-github: seeding, ref resolution, diffs and the tarball format of the in-memory repositories."""

from __future__ import annotations

import base64
import gzip
import io
import tarfile
from typing import Any

import pytest

from agent_doubles.github.objects import EXEC_MODE, FILE_MODE, LINK_MODE, ObjectStore
from agent_doubles.github.repos import SeedError, World, diff_trees, iso, parse_iso, tarball, unified_patch


def world_with(*repositories: dict[str, Any], users: list[dict[str, Any]] | None = None) -> World:
    world = World()
    world.seed({"users": users or [{"login": "octo-user"}], "repositories": list(repositories)})
    return world


def test_files_of_every_kind_and_the_tarball() -> None:
    png = bytes([0x89, 0x50, 0x4E, 0x47, 0, 1, 2])
    world = world_with(
        {
            "full_name": "octo-user/tools",
            "branches": [
                {
                    "name": "main",
                    "commits": [
                        {
                            "message": "Tools.",
                            "author": {"name": "Ann", "email": "a@example.invalid", "date": "2026-03-01T00:00:00Z"},
                            "files": {
                                "bin/run": {"content": "#!/bin/sh\n", "mode": EXEC_MODE},
                                "logo.png": {"base64": base64.b64encode(png).decode()},
                                "current": {"target": "bin/run"},
                                "notes.txt": {"content": "n\n"},
                            },
                        }
                    ],
                }
            ],
        }
    )
    repo = world.repo("OCTO-USER", "Tools")
    assert repo is not None
    assert repo.owner_type == "User"
    sha = repo.branch("main")
    assert sha is not None
    commit = repo.objects.commit(sha)
    assert (commit.author.name, commit.author.time, commit.message) == (
        "Ann",
        parse_iso("2026-03-01T00:00:00Z"),
        "Tools.",
    )
    modes = {path: f.mode for path, f in repo.objects.files(commit.tree).items()}
    assert modes == {"bin/run": EXEC_MODE, "current": LINK_MODE, "logo.png": FILE_MODE, "notes.txt": FILE_MODE}
    data = tarball(repo, sha)
    assert data[:2] == b"\x1f\x8b"
    assert gzip.decompress(data)
    top = f"octo-user-tools-{sha[:7]}"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = {m.name: m for m in archive.getmembers()}
        assert list(members) == [
            top,
            f"{top}/bin",
            f"{top}/bin/run",
            f"{top}/current",
            f"{top}/logo.png",
            f"{top}/notes.txt",
        ]
        assert members[top].isdir()
        assert members[top].mode == 0o775
        assert members[f"{top}/bin/run"].mode == 0o775
        assert members[f"{top}/notes.txt"].mode == 0o664
        assert members[f"{top}/current"].issym()
        assert members[f"{top}/current"].linkname == "bin/run"
        assert {m.uname for m in members.values()} == {"root"}
        assert {m.mtime for m in members.values()} == {commit.committer.time}
        logo = archive.extractfile(f"{top}/logo.png")
        assert logo is not None
        assert logo.read() == png
    assert tarball(repo, sha) == data  # deterministic


def test_patches_and_binary_changes() -> None:
    assert unified_patch(["a\n"], ["a\n"]) == (None, 0, 0)
    assert unified_patch(["Hello World!"], ["Hello World!\n"]) == (
        "@@ -1 +1 @@\n-Hello World!\n\\ No newline at end of file\n+Hello World!",
        1,
        1,
    )
    store = ObjectStore()
    old = store.build(
        {
            "same": (FILE_MODE, store.put_blob(b"s\n")),
            "text": (FILE_MODE, store.put_blob(b"1\n2\n")),
            "bin": (FILE_MODE, store.put_blob(b"\0old")),
            "mode": (FILE_MODE, store.put_blob(b"m\n")),
            "gone": (FILE_MODE, store.put_blob(b"g\n")),
            "latin": (FILE_MODE, store.put_blob(b"\xe9t\xe9")),
        }
    )
    new = store.build(
        {
            "same": (FILE_MODE, store.put_blob(b"s\n")),
            "text": (FILE_MODE, store.put_blob(b"1\nzwei\n")),
            "bin": (FILE_MODE, store.put_blob(b"\0new")),
            "mode": (EXEC_MODE, store.put_blob(b"m\n")),
            "added": (FILE_MODE, store.put_blob(b"a")),
            "latin": (FILE_MODE, store.put_blob(b"\xe9")),
        }
    )
    changes = {c.path: c for c in diff_trees(store, old, new)}
    assert sorted(changes) == ["added", "bin", "gone", "latin", "mode", "text"]
    assert (changes["text"].status, changes["text"].additions, changes["text"].deletions) == ("modified", 1, 1)
    assert changes["text"].patch == "@@ -1,2 +1,2 @@\n 1\n-2\n+zwei"
    assert (changes["bin"].patch, changes["bin"].additions) == (None, 0)
    assert (changes["latin"].patch, changes["latin"].status) == (None, "modified")
    assert (changes["mode"].status, changes["mode"].patch) == ("changed", None)
    assert changes["gone"].status == "removed"
    assert changes["gone"].sha == store.put_blob(b"g\n")
    assert changes["added"].patch == "@@ -0,0 +1 @@\n+a\n\\ No newline at end of file"


def test_ref_resolution_and_history() -> None:
    world = world_with(
        {
            "full_name": "octo-org/r",
            "default_branch": "trunk",
            "branches": [
                {"name": "trunk", "commits": [{"files": {"a": "1"}}, {"files": {"a": "2"}}]},
                {"name": "side", "from": "trunk", "commits": [{"files": {"b": "x"}}]},
                {"name": "copy", "from": "side"},
                {"name": "heads/odd", "commits": [{"files": {"o": "o"}}]},
            ],
            "tags": {"v1": "trunk"},
        }
    )
    repo = world.repo("octo-org", "r")
    assert repo is not None
    trunk, side = repo.branch("trunk"), repo.branch("side")
    assert trunk is not None
    assert side is not None
    assert repo.branch("copy") == side
    assert repo.resolve("v1") == repo.resolve("refs/tags/v1") == trunk
    assert repo.resolve("heads/odd") == repo.branch("heads/odd")
    assert repo.resolve(side[:10]) == side
    assert repo.resolve("zzzzzzz") is None
    assert repo.merge_base(trunk, side) == trunk
    assert len(repo.history(side, set())) == 3
    assert repo.history(side, repo.ancestors(trunk)) == [side]
    assert repo.merge_base(trunk, repo.branch("heads/odd") or "") is None
    assert iso(0) == "1970-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ([], "seed must be an object"),
        ({"other": []}, "unknown keys"),
        ({"users": {}}, "users must be a list"),
        ({"users": [1]}, "user must be an object"),
        ({"users": [{"login": "-bad"}]}, "invalid login"),
        ({"users": [{"login": "u"}, {"login": "U"}]}, "already exists"),
        ({"users": [{"login": "u", "id": -1}]}, "user.id must be a non-negative integer"),
        ({"users": [{"login": "u", "name": 1}]}, "user.name must be a string or null"),
        ({"users": [{"login": "u", "tokens": [""]}]}, "tokens must be non-empty strings"),
        ({"users": [{"login": 5}]}, "user.login must be a string"),
        ({"repositories": [{"full_name": "bad"}]}, "invalid repository name"),
        ({"repositories": [{"full_name": "o/r", "private": "yes"}]}, "repository.private must be a boolean"),
        ({"repositories": [{"full_name": "o/r"}, {"full_name": "O/R"}]}, "already exists"),
        ({"repositories": [{"full_name": "o/r", "branches": [{"name": "m"}]}]}, "has no commit"),
        ({"repositories": [{"full_name": "o/r", "branches": [{"name": ""}]}]}, "empty or already exists"),
        ({"repositories": [{"full_name": "o/r", "branches": [{"name": "m", "from": "x"}]}]}, "unknown start"),
        (
            {"repositories": [{"full_name": "o/r", "branches": [{"name": "m", "commits": [{"files": {"a": None}}]}]}]},
            "cannot delete missing path",
        ),
        (
            {
                "repositories": [
                    {"full_name": "o/r", "branches": [{"name": "m", "commits": [{"files": {"a/../b": "x"}}]}]}
                ]
            },
            "invalid path",
        ),
        (
            {"repositories": [{"full_name": "o/r", "branches": [{"name": "m", "commits": [{"files": {"a": 1}}]}]}]},
            "must be an object",
        ),
        (
            {
                "repositories": [
                    {
                        "full_name": "o/r",
                        "branches": [{"name": "m", "commits": [{"files": {"a": {"mode": "120000", "content": ""}}}]}],
                    }
                ]
            },
            "mode must be",
        ),
        (
            {
                "repositories": [
                    {"full_name": "o/r", "branches": [{"name": "m", "commits": [{"files": {"a": {"base64": "%%"}}}]}]}
                ]
            },
            "invalid base64",
        ),
        (
            {
                "repositories": [
                    {"full_name": "o/r", "branches": [{"name": "m", "commits": [{"files": {"a": "x", "a/b": "y"}}]}]}
                ]
            },
            "duplicate entry names",
        ),
        (
            {
                "repositories": [
                    {
                        "full_name": "o/r",
                        "branches": [{"name": "m", "commits": [{"author": {"name": "n", "email": "e", "date": "x"}}]}],
                    }
                ]
            },
            "invalid timestamp",
        ),
        (
            {
                "repositories": [
                    {
                        "full_name": "o/r",
                        "branches": [
                            {
                                "name": "m",
                                "commits": [{"author": {"name": "n", "email": "e", "date": "2026-01-01T00:00:00"}}],
                            }
                        ],
                    }
                ]
            },
            "without time zone",
        ),
        (
            {
                "repositories": [
                    {"full_name": "o/r", "branches": [{"name": "m", "commits": [{"author": {"name": "n"}}]}]}
                ]
            },
            "author.email must be a string",
        ),
        ({"repositories": [{"full_name": "o/r", "tags": {"t": "none"}}]}, "unknown ref"),
        ({"repositories": [{"full_name": "o/r", "tags": {"t": 1}}]}, "unknown ref"),
        (
            {"repositories": [{"full_name": "o/r", "pulls": [{"title": "t", "head": "x", "user": "u"}]}]},
            "unknown branch",
        ),
        (
            {"repositories": [{"full_name": "o/r", "comments": [{"number": 1, "body": "b", "user": "u"}]}]},
            "unknown issue or pull request",
        ),
        ({"users": [{"login": "a", "tokens": ["t"]}, {"login": "b", "tokens": ["t"]}]}, "one user only"),
        ({"users": [{"login": "a", "tokens": ["t"], "personal_tokens": ["t"]}]}, "one user only"),
        ({"users": [{"login": "a", "personal_tokens": [""]}]}, "personal_tokens must be non-empty strings"),
        ({"users": [{"login": "a", "collaborator": ["hello"]}]}, "collaborator must name repositories"),
        ({"users": [{"login": "a", "collaborator": [1]}]}, "collaborator must name repositories"),
        (
            {"users": [{"login": "a", "personal_tokens": ["p"]}], "installations": [{"account": "o", "users": ["x"]}]},
            "is not seeded",
        ),
        ({"repositories": [{"full_name": "o/r", "issues": [{"title": "t", "user": "u", "x": 1}]}]}, "unknown keys"),
        ({"installations": [{"account": "o", "account_type": "Team"}]}, "account_type must be one of"),
        ({"installations": [{"account": "o", "repository_selection": "some"}]}, "repository_selection must be one of"),
        ({"installations": [{"account": "o", "repositories": ["o/none"]}]}, "is not seeded"),
        ({"installations": [{"account": "o", "users": ["nobody"]}]}, "is not seeded"),
        ({"installations": [{"account": "o", "permissions": {"contents": "admin"}}]}, "must be read or write"),
        ({"installations": [{"id": 1, "account": "o"}, {"id": 1, "account": "o"}]}, "installation 1 already exists"),
    ],
)
def test_seed_refusals_leave_nothing_behind(seed: Any, message: str) -> None:
    world = World()
    with pytest.raises(SeedError, match=message):
        world.seed(seed)
    assert (world.users, world.tokens, world.repos, world.installations) == ({}, {}, {}, {})
    assert world.personal == set()


def test_seeded_pull_needs_known_user_and_state() -> None:
    base = {
        "full_name": "o/r",
        "branches": [{"name": "main", "commits": [{}]}, {"name": "f", "from": "main", "commits": [{}]}],
    }
    for pull, message in (
        ({"title": "t", "head": "f", "user": "nobody"}, "is not seeded"),
        ({"title": "t", "head": "f", "user": "octo-user", "state": "merged"}, "pull.state must be open or closed"),
    ):
        world = World()
        with pytest.raises(SeedError, match=message):
            world.seed({"users": [{"login": "octo-user"}], "repositories": [{**base, "pulls": [pull]}]})


def test_installation_defaults_and_account_ids() -> None:
    world = World()
    world.seed(
        {
            "users": [{"login": "octo-user", "id": 5}],
            "repositories": [{"full_name": "octo-user/r", "branches": [{"name": "main", "commits": [{}]}]}],
            "installations": [{"account": "octo-user", "repositories": ["octo-user/r"]}, {"account": "acme"}],
        }
    )
    first, second = sorted(world.installations.values(), key=lambda i: i.id)
    assert (first.account_type, first.account_id, first.selection) == ("User", 5, "selected")
    assert (second.account_type, second.account_id) == ("Organization", world.account_id("acme"))
    user = world.users["octo-user"]
    assert world.visible_installations(user) == [first]
    repo = world.repo("octo-user", "r")
    assert repo is not None
    assert world.installation_for(user, repo) is first
    assert world.user_for_token("none") is None
