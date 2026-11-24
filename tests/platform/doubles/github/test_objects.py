"""U-DBL-github: git object ids of the double against the real ``git`` (only used here, to compare)."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_doubles.github.objects import (
    EMPTY_TREE,
    EXEC_MODE,
    FILE_MODE,
    LINK_MODE,
    TREE_MODE,
    Commit,
    ObjectError,
    ObjectStore,
    Signature,
    TreeEntry,
    api_mode,
    git_mode,
    object_id,
)


@pytest.fixture
def git(tmp_path: Path) -> Callable[..., str]:
    """Run ``git`` in an empty repository, isolated from every user and system configuration."""
    binary = shutil.which("git")
    if binary is None:
        pytest.fail("git is required to check the object ids")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "a@example.invalid",
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": "a@example.invalid",
    }
    repo = tmp_path / "repo"
    subprocess.run([binary, "init", "-q", str(repo)], check=True, env=env)

    def run(*args: str, data: bytes = b"", extra: dict[str, str] | None = None) -> str:
        done = subprocess.run(
            [binary, *args], input=data, cwd=repo, env={**env, **(extra or {})}, capture_output=True, check=True
        )
        return done.stdout.decode().strip()

    return run


def test_blob_ids_match_git_hash_object(git: Callable[..., str]) -> None:
    run = git
    store = ObjectStore()
    for data in (b"", b"hello\n", bytes(range(256)) * 3, "grüezi\r\nno newline".encode()):
        assert store.put_blob(data) == run("hash-object", "-w", "--stdin", data=data)
        assert store.blob(object_id("blob", data)) == data


def test_tree_ids_match_git_mktree(git: Callable[..., str]) -> None:
    run = git
    store = ObjectStore()
    files = {
        "README.md": (FILE_MODE, b"hello\n"),
        "a-b": (FILE_MODE, b"dash sorts before slash\n"),
        "a/inner.txt": (FILE_MODE, b"inner\n"),
        "a/deep/x.bin": (FILE_MODE, b"\x00\x01"),
        "a.txt": (FILE_MODE, b"dot sorts before slash\n"),
        "run.sh": (EXEC_MODE, b"#!/bin/sh\n"),
        "link": (LINK_MODE, b"README.md"),
        "empty": (FILE_MODE, b""),
    }
    flat = {}
    for path, (mode, data) in files.items():
        sha = store.put_blob(data)
        assert run("hash-object", "-w", "--stdin", data=data) == sha
        flat[path] = (mode, sha)
    root = store.build(flat)
    # Build the same trees with git mktree, bottom-up.
    deep = run("mktree", data=f"100644 blob {flat['a/deep/x.bin'][1]}\tx.bin\n".encode())
    a_tree = run(
        "mktree",
        data=f"040000 tree {deep}\tdeep\n100644 blob {flat['a/inner.txt'][1]}\tinner.txt\n".encode(),
    )
    lines = [f"040000 tree {a_tree}\ta"]
    for path in ("README.md", "a-b", "a.txt", "run.sh", "link", "empty"):
        mode, sha = flat[path]
        lines.append(f"{mode} blob {sha}\t{path}")
    assert root == run("mktree", data=("\n".join(lines) + "\n").encode())
    assert run("ls-tree", "-r", "--name-only", root).split("\n") == [p for p, _ in store.walk(root) if p in files]
    assert sorted(store.files(root)) == sorted(files)
    assert store.files(root)["a/deep/x.bin"].sha == flat["a/deep/x.bin"][1]
    assert store.put_tree([]) == EMPTY_TREE == run("mktree", data=b"")


def test_commit_ids_match_git_commit_tree(git: Callable[..., str]) -> None:
    run = git
    store = ObjectStore()
    tree = store.build({"f": (FILE_MODE, store.put_blob(b"x\n"))})
    run("hash-object", "-w", "--stdin", data=b"x\n")
    assert run("mktree", data=f"100644 blob {object_id('blob', b'x\n')}\tf\n".encode()) == tree
    when = {"GIT_AUTHOR_DATE": "1767225601 +0000", "GIT_COMMITTER_DATE": "1767225601 +0000"}
    first = store.put_commit(
        Commit(
            tree,
            (),
            Signature("A", "a@example.invalid", 1767225601),
            Signature("A", "a@example.invalid", 1767225601),
            "First.\n",
        )
    )
    assert first == run("commit-tree", tree, "-m", "First.", extra=when)
    second = store.put_commit(
        Commit(
            tree,
            (first,),
            Signature("A", "a@example.invalid", 1767225601),
            Signature("A", "a@example.invalid", 1767225601),
            "Second.\n\nBody.\n",
        )
    )
    assert second == run("commit-tree", tree, "-p", first, "-m", "Second.", "-m", "Body.", extra=when)
    # A message without a final newline (as the REST API stores it) is hashed byte for byte.
    raw = Commit(tree, (), Signature("A", "a@example.invalid", 5), Signature("B", "b@example.invalid", 6), "no newline")
    assert store.put_commit(raw) == run("hash-object", "-t", "commit", "-w", "--stdin", data=raw.payload())
    assert store.commit(second).parents == (first,)
    assert set(store.commit_ids()) == {first, second, object_id("commit", raw.payload())}


def test_signed_commit_ids_match_git_hash_object(git: Callable[..., str]) -> None:
    """The signature goes into the ``gpgsig`` header (further lines indented by one space, a final newline of the
    signature dropped); the payload without it is what the signature covers."""
    store = ObjectStore()
    sig = Signature("A", "a@example.invalid", 5)
    plain = Commit(EMPTY_TREE, (), sig, sig, "Signed.\n")
    signed = Commit(EMPTY_TREE, (), sig, sig, "Signed.\n", "-----BEGIN X-----\nabc\n-----END X-----\n")
    assert signed.unsigned_payload() == plain.payload() == plain.unsigned_payload()
    assert signed.payload() == (
        b"tree " + EMPTY_TREE.encode() + b"\nauthor A <a@example.invalid> 5 +0000\n"
        b"committer A <a@example.invalid> 5 +0000\ngpgsig -----BEGIN X-----\n abc\n -----END X-----\n\nSigned.\n"
    )
    assert store.put_commit(signed) == git("hash-object", "-t", "commit", "--stdin", data=signed.payload())
    assert store.commit(object_id("commit", signed.payload())).signature == signed.signature


def test_store_refuses_invalid_objects() -> None:
    store = ObjectStore()
    blob = store.put_blob(b"x")
    with pytest.raises(ObjectError, match="duplicate"):
        store.put_tree([TreeEntry("a", FILE_MODE, blob), TreeEntry("a", EXEC_MODE, blob)])
    for name in ("", "a/b", ".", "..", "nul\0"):
        with pytest.raises(ObjectError, match="invalid tree entry name"):
            store.put_tree([TreeEntry(name, FILE_MODE, blob)])
    with pytest.raises(ObjectError, match="unsupported mode"):
        store.put_tree([TreeEntry("m", "160000", blob)])
    with pytest.raises(ObjectError, match="no tree"):
        store.put_tree([TreeEntry("t", TREE_MODE, blob)])
    with pytest.raises(ObjectError, match="no blob"):
        store.put_tree([TreeEntry("b", FILE_MODE, "0" * 40)])
    with pytest.raises(ObjectError, match="no commit"):
        store.commit(blob)
    with pytest.raises(ObjectError, match="duplicate"):
        store.build({"a": (FILE_MODE, blob), "a/b": (FILE_MODE, blob)})
    sig = Signature("n", "e", 0)
    with pytest.raises(ObjectError, match="no tree"):
        store.put_commit(Commit(blob, (), sig, sig, "m"))
    with pytest.raises(ObjectError, match="no commit"):
        store.put_commit(Commit(EMPTY_TREE, (blob,), sig, sig, "m"))
    assert store.kind(blob) == "blob"
    assert store.kind(EMPTY_TREE) == "tree"
    assert store.kind("f" * 40) is None


def test_modes_and_entries() -> None:
    assert api_mode(TREE_MODE) == "040000"
    assert api_mode(FILE_MODE) == FILE_MODE
    assert git_mode("040000") == TREE_MODE
    assert git_mode(EXEC_MODE) == EXEC_MODE
    entry = TreeEntry("d", TREE_MODE, EMPTY_TREE)
    assert (entry.is_tree, entry.kind, entry.sort_key()) == (True, "tree", b"d/")
    assert TreeEntry("f", LINK_MODE, EMPTY_TREE).kind == "blob"
    assert Signature("N", "e@x", 7).line() == "N <e@x> 7 +0000"
