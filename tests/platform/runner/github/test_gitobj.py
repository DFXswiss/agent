"""U-RUN-github: git object SHA-1s computed without git (03-api.md section 9 `github.commit`, ADR-015) against values
the git binary produced for the same content: empty file, text, binary data, an executable, nested trees, the git
sort order of trees (`a-b` before `a/`), deletions down to the empty tree, and listings of the Git Data API."""

from __future__ import annotations

import io

import pytest

from agent_runner.github import gitobj

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
HELLO = "ce013625030ba8dba906f756967f9e9ca394464a"
BINARY = b"\x00\x01\x02\xff\xfe binary\x00"
BINARY_SHA = "0c2906cbe11feab1d5f6984f521044058d0c9871"
FILES = {
    "empty": (gitobj.FILE_MODE, EMPTY_BLOB),
    "hello": (gitobj.FILE_MODE, HELLO),
    "bin.dat": (gitobj.FILE_MODE, BINARY_SHA),
    "run.sh": (gitobj.EXEC_MODE, "f5bdd214e01603ecd6c83be9f66d88579c588ec6"),
    "a/b/c.txt": (gitobj.FILE_MODE, "4cdb2265d30204be5463b38174b2e8e717982405"),
    "a/z.txt": (gitobj.FILE_MODE, "587be6b4c3f93f93c489c0111bba5596147a26cb"),
    "a-b/q": (gitobj.FILE_MODE, "bca70f35318f31dd1d1d1d2d2e64c19b880899ff"),
}
ROOT_TREE = "8a365431b0ba2e441b44f4883549a70aa376be07"


def test_blob_shas_match_git() -> None:
    assert gitobj.blob_sha(b"") == EMPTY_BLOB
    assert gitobj.blob_sha(b"hello\n") == HELLO
    assert gitobj.blob_sha(BINARY) == BINARY_SHA
    assert gitobj.blob_sha(b"deep\n") == FILES["a/b/c.txt"][1]
    assert gitobj.blob_sha(b"run\n") == FILES["run.sh"][1]


def test_blob_sha_of_a_stream_reads_in_chunks() -> None:
    data = bytes(range(256)) * 1000
    assert gitobj.blob_sha_stream(io.BytesIO(data), len(data), chunk=4096) == gitobj.blob_sha(data)
    with pytest.raises(ValueError, match="size"):
        gitobj.blob_sha_stream(io.BytesIO(b"short"), 9)


def test_tree_shas_match_git_including_nested_trees_and_the_sort_order() -> None:
    assert gitobj.tree_sha([]) == gitobj.EMPTY_TREE == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    assert gitobj.build_tree(FILES) == ROOT_TREE
    trees = gitobj.trees(FILES)
    assert trees["a"] == "870941e54c71027eaf467f8379f98f33f0239a3e"
    assert trees["a/b"] == "7b0c5d2afa30e0b524990e5c6f6a5bc4dd63a09a"
    assert trees["a-b"] == "7cafae9f1b71ea42ad1c9db8ef5969f9b4a9f11a"
    assert trees[""] == ROOT_TREE


def test_deletions_drop_empty_trees() -> None:
    files = dict(FILES)
    del files["a/z.txt"]
    assert gitobj.build_tree(files) == "ae234fa5c3171d5445d82b695c0ec10142e5f948"
    del files["a/b/c.txt"]
    assert gitobj.build_tree(files) == "d19285ed0579ff5f27b93d06a779a2142f39a9f5"
    assert gitobj.build_tree({}) == gitobj.EMPTY_TREE


def test_symlinks_and_submodules_are_leaf_entries() -> None:
    files = {
        "link": (gitobj.LINK_MODE, gitobj.blob_sha(b"hello")),
        "sub": (gitobj.SUBMODULE_MODE, "1" * 40),
    }
    raw = b"120000 link\x00" + bytes.fromhex(gitobj.blob_sha(b"hello")) + b"160000 sub\x00" + bytes.fromhex("1" * 40)
    assert gitobj.build_tree(files) == gitobj.object_sha("tree", raw)


def test_build_refuses_a_path_that_is_file_and_directory() -> None:
    with pytest.raises(ValueError, match="conflict"):
        gitobj.build_tree({"a": (gitobj.FILE_MODE, HELLO), "a/b": (gitobj.FILE_MODE, HELLO)})
    with pytest.raises(ValueError, match="conflict"):
        gitobj.build_tree({"a/b": (gitobj.FILE_MODE, HELLO), "a": (gitobj.FILE_MODE, HELLO)})
    with pytest.raises(ValueError, match="mode"):
        gitobj.build_tree({"a": ("100600", HELLO)})


def test_flatten_reads_a_recursive_tree_listing() -> None:
    listing = [
        {"path": "a", "mode": "040000", "type": "tree", "sha": "870941e54c71027eaf467f8379f98f33f0239a3e"},
        {"path": "a/z.txt", "mode": "100644", "type": "blob", "sha": FILES["a/z.txt"][1], "size": 2},
        {"path": "run.sh", "mode": "100755", "type": "blob", "sha": FILES["run.sh"][1]},
        {"path": "link", "mode": "120000", "type": "blob", "sha": HELLO},
        {"path": "sub", "mode": "160000", "type": "commit", "sha": "1" * 40},
    ]
    assert gitobj.flatten(listing) == {
        "a/z.txt": (gitobj.FILE_MODE, FILES["a/z.txt"][1]),
        "run.sh": (gitobj.EXEC_MODE, FILES["run.sh"][1]),
        "link": (gitobj.LINK_MODE, HELLO),
        "sub": (gitobj.SUBMODULE_MODE, "1" * 40),
    }


@pytest.mark.parametrize(
    "item",
    [
        "x",
        {"path": "a", "mode": "100644", "type": "blob", "sha": "XYZ"},
        {"path": "a", "mode": "100600", "type": "blob", "sha": HELLO},
        {"path": "a", "mode": "100644", "type": "tag", "sha": HELLO},
        {"path": "a", "mode": "160000", "type": "blob", "sha": HELLO},
        {"path": "a", "mode": "100644", "type": "commit", "sha": HELLO},
        {"path": "/abs", "mode": "100644", "type": "blob", "sha": HELLO},
        {"path": "a/../b", "mode": "100644", "type": "blob", "sha": HELLO},
        {"path": 5, "mode": "100644", "type": "blob", "sha": HELLO},
    ],
)
def test_flatten_refuses_entries_outside_the_git_format(item: object) -> None:
    with pytest.raises(ValueError, match="tree"):
        gitobj.flatten([item])


def test_flatten_refuses_duplicate_paths() -> None:
    entry = {"path": "a", "mode": "100644", "type": "blob", "sha": HELLO}
    with pytest.raises(ValueError, match="duplicate"):
        gitobj.flatten([entry, entry])
