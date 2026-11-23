"""Git objects as GitHub stores them: blobs, trees and commits in the git object format with real SHA-1 values.

The double computes every object id exactly like git (``sha1(b"<kind> <length>\\0" + payload)``), so a tree SHA the
runner computes locally (05-runner.md §12.1, ``tree_mismatch``) can be compared with what the double returns, and the
tests check the values against ``git hash-object`` and ``git mktree``.

Modes: ``100644`` (file), ``100755`` (executable), ``120000`` (symbolic link, the blob holds the target) and
``40000`` (tree; the REST API shows it as ``040000``). Submodules (``160000``) are not modelled.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

FILE_MODE = "100644"
EXEC_MODE = "100755"
LINK_MODE = "120000"
TREE_MODE = "40000"
API_TREE_MODE = "040000"
BLOB_MODES = frozenset({FILE_MODE, EXEC_MODE, LINK_MODE})
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class ObjectError(ValueError):
    """An object reference or entry the store cannot accept."""


def object_id(kind: str, payload: bytes) -> str:
    """The git object id of `payload` of `kind` (``blob``, ``tree`` or ``commit``)."""
    return hashlib.sha1(f"{kind} {len(payload)}\0".encode() + payload, usedforsecurity=False).hexdigest()


def api_mode(mode: str) -> str:
    """The mode as the REST API shows it (trees zero-padded to six digits)."""
    return API_TREE_MODE if mode == TREE_MODE else mode


def git_mode(mode: str) -> str:
    """The mode as git stores it (``040000`` → ``40000``)."""
    return TREE_MODE if mode == API_TREE_MODE else mode


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a tree: a name (no slash), the git mode and the object id."""

    name: str
    mode: str
    sha: str

    @property
    def is_tree(self) -> bool:
        return self.mode == TREE_MODE

    @property
    def kind(self) -> str:
        """``tree`` or ``blob``."""
        return "tree" if self.is_tree else "blob"

    def sort_key(self) -> bytes:
        """Git orders entries by name bytes, with a tree compared as if its name ended in a slash."""
        return self.name.encode() + (b"/" if self.is_tree else b"")


def tree_payload(entries: Iterable[TreeEntry]) -> bytes:
    """The payload of a tree object in git order."""
    return b"".join(
        f"{entry.mode} {entry.name}".encode() + b"\0" + bytes.fromhex(entry.sha)
        for entry in sorted(entries, key=TreeEntry.sort_key)
    )


@dataclass(frozen=True)
class Signature:
    """Author or committer of a commit; `time` in seconds since the epoch, always UTC (``+0000``) like GitHub."""

    name: str
    email: str
    time: int

    def line(self) -> str:
        return f"{self.name} <{self.email}> {self.time} +0000"


@dataclass(frozen=True)
class Commit:
    """A commit object."""

    tree: str
    parents: tuple[str, ...]
    author: Signature
    committer: Signature
    message: str

    def payload(self) -> bytes:
        lines = [f"tree {self.tree}", *(f"parent {parent}" for parent in self.parents)]
        lines += [f"author {self.author.line()}", f"committer {self.committer.line()}"]
        return ("\n".join(lines) + "\n\n" + self.message).encode()


@dataclass(frozen=True)
class FileEntry:
    """A file of a flattened tree: its full path, git mode and blob id."""

    path: str
    mode: str
    sha: str


class ObjectStore:
    """The objects of one repository, by id. Thread-safe; objects are immutable once stored."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blobs: dict[str, bytes] = {}
        self._trees: dict[str, tuple[TreeEntry, ...]] = {object_id("tree", b""): ()}
        self._commits: dict[str, Commit] = {}

    # --- writing --------------------------------------------------------------------------------

    def put_blob(self, data: bytes) -> str:
        sha = object_id("blob", data)
        with self._lock:
            self._blobs[sha] = data
        return sha

    def put_tree(self, entries: Iterable[TreeEntry]) -> str:
        """Store a tree; every entry must name a stored object of the matching kind, names must be unique."""
        listed = tuple(sorted(entries, key=TreeEntry.sort_key))
        names = [entry.name for entry in listed]
        if len(set(names)) != len(names):
            raise ObjectError("duplicate entry names in a tree")
        for entry in listed:
            if not entry.name or "/" in entry.name or entry.name in {".", ".."} or "\0" in entry.name:
                raise ObjectError(f"invalid tree entry name {entry.name!r}")
            if entry.is_tree:
                self.tree(entry.sha)
            elif entry.mode in BLOB_MODES:
                self.blob(entry.sha)
            else:
                raise ObjectError(f"unsupported mode {entry.mode}")
        sha = object_id("tree", tree_payload(listed))
        with self._lock:
            self._trees[sha] = listed
        return sha

    def put_commit(self, commit: Commit) -> str:
        """Store a commit; its tree and parents must be stored."""
        self.tree(commit.tree)
        for parent in commit.parents:
            self.commit(parent)
        sha = object_id("commit", commit.payload())
        with self._lock:
            self._commits[sha] = commit
        return sha

    # --- reading --------------------------------------------------------------------------------

    def blob(self, sha: str) -> bytes:
        with self._lock:
            if sha not in self._blobs:
                raise ObjectError(f"no blob {sha}")
            return self._blobs[sha]

    def tree(self, sha: str) -> tuple[TreeEntry, ...]:
        with self._lock:
            if sha not in self._trees:
                raise ObjectError(f"no tree {sha}")
            return self._trees[sha]

    def commit(self, sha: str) -> Commit:
        with self._lock:
            if sha not in self._commits:
                raise ObjectError(f"no commit {sha}")
            return self._commits[sha]

    def kind(self, sha: str) -> str | None:
        """``blob``, ``tree``, ``commit`` or None for an unknown id."""
        with self._lock:
            for kind, table in (("blob", self._blobs), ("tree", self._trees), ("commit", self._commits)):
                if sha in table:
                    return kind
        return None

    def commit_ids(self) -> list[str]:
        """Every stored commit id."""
        with self._lock:
            return list(self._commits)

    # --- trees as paths -------------------------------------------------------------------------

    def walk(self, sha: str, prefix: str = "") -> list[tuple[str, TreeEntry]]:
        """Every entry below the tree `sha` with its full path, depth first in git order (trees before contents)."""
        out: list[tuple[str, TreeEntry]] = []
        for entry in self.tree(sha):
            path = f"{prefix}{entry.name}"
            out.append((path, entry))
            if entry.is_tree:
                out.extend(self.walk(entry.sha, f"{path}/"))
        return out

    def files(self, sha: str) -> dict[str, FileEntry]:
        """The blobs below tree `sha` by full path."""
        return {path: FileEntry(path, entry.mode, entry.sha) for path, entry in self.walk(sha) if not entry.is_tree}

    def build(self, files: Mapping[str, tuple[str, str]]) -> str:
        """Store the trees for `files` (path → (mode, blob id)) and return the root tree id."""
        children: dict[str, dict[str, tuple[str, str]]] = {}
        entries: list[TreeEntry] = []
        for path, (mode, sha) in files.items():
            head, sep, rest = path.partition("/")
            if sep:
                children.setdefault(head, {})[rest] = (mode, sha)
            else:
                entries.append(TreeEntry(head, mode, sha))
        for name, nested in children.items():
            entries.append(TreeEntry(name, TREE_MODE, self.build(nested)))
        return self.put_tree(entries)
