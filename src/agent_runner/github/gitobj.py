"""Git object SHA-1s without the `git` binary (03-api.md section 9 `github.commit`, 05-runner.md section 12.1, ADR-015).

`github.commit` computes the blob and tree SHAs of the snapshot locally and compares GitHub's tree SHA with them
(`tree_mismatch`). The formats are git's:

- object ID = SHA-1 of `<type> <size>\\0<content>`;
- blob content = the file's bytes;
- tree content = the entries sorted by name, where a subtree sorts as `<name>/` (so `a-b` comes before the tree `a`),
  each `<mode> <name>\\0<20-byte binary SHA>`; modes `100644` (file), `100755` (executable), `120000` (symbolic link),
  `160000` (submodule commit), `40000` (tree, without a leading zero); an empty directory is no tree entry.

`build_tree(files)` builds the root tree SHA of a flat mapping `path -> (mode, sha)` of leaf entries (the form of a
recursive tree listing of the Git Data API, `flatten(listing)`), `trees(files)` every tree SHA by directory path.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import BinaryIO

FILE_MODE = "100644"
EXEC_MODE = "100755"
LINK_MODE = "120000"
SUBMODULE_MODE = "160000"
TREE_MODE = "40000"
REGULAR_MODES = frozenset({FILE_MODE, EXEC_MODE})
LEAF_MODES = frozenset({FILE_MODE, EXEC_MODE, LINK_MODE, SUBMODULE_MODE})
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CHUNK = 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LISTING_TYPES = {FILE_MODE: "blob", EXEC_MODE: "blob", LINK_MODE: "blob", SUBMODULE_MODE: "commit"}

Files = Mapping[str, tuple[str, str]]
"""Leaf entries by repository path: `(mode, sha)`."""


def object_sha(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode("ascii") + content, usedforsecurity=False).hexdigest()


def blob_sha(data: bytes) -> str:
    return object_sha("blob", data)


def blob_sha_stream(stream: BinaryIO, size: int, *, chunk: int = CHUNK) -> str:
    """The blob SHA of exactly `size` bytes read from `stream` (a shorter stream is a `ValueError`)."""
    digest = hashlib.sha1(f"blob {size}\0".encode("ascii"), usedforsecurity=False)
    left = size
    while left:
        data = stream.read(min(chunk, left))
        if not data:
            raise ValueError("stream shorter than its size")
        digest.update(data)
        left -= len(data)
    return digest.hexdigest()


def _sort_key(entry: tuple[str, str, str]) -> bytes:
    mode, name, _sha = entry
    return name.encode("utf-8") + (b"/" if mode == TREE_MODE else b"")


def tree_sha(entries: Iterable[tuple[str, str, str]]) -> str:
    """The SHA of one tree from its direct entries `(mode, name, sha)`."""
    content = b"".join(
        f"{mode} {name}\0".encode() + bytes.fromhex(sha) for mode, name, sha in sorted(entries, key=_sort_key)
    )
    return object_sha("tree", content)


def trees(files: Files) -> dict[str, str]:
    """The SHA of every non-empty tree of `files` by directory path (`""` is the root)."""
    children: dict[str, dict[str, tuple[str, str]]] = {"": {}}
    for path, (mode, sha) in files.items():
        if mode not in LEAF_MODES:
            raise ValueError(f"mode {mode!r} of {path!r}")
        parts = path.split("/")
        for depth in range(len(parts)):
            directory = "/".join(parts[:depth])
            entries = children.setdefault(directory, {})
            name = parts[depth]
            leaf = depth == len(parts) - 1
            found = entries.get(name)
            if found is not None and (leaf or found[0] != TREE_MODE):
                raise ValueError(f"path conflict at {path!r}")
            entries[name] = (mode, sha) if leaf else (TREE_MODE, "")
    done: dict[str, str] = {}
    for directory in sorted(children, key=lambda d: d.count("/") + (1 if d else 0), reverse=True):
        entries = children[directory]
        prefix = f"{directory}/" if directory else ""
        done[directory] = tree_sha(
            (mode, name, done[prefix + name] if mode == TREE_MODE else sha) for name, (mode, sha) in entries.items()
        )
    return done


def build_tree(files: Files) -> str:
    """The root tree SHA of `files` (`EMPTY_TREE` when empty)."""
    return trees(files)[""]


def _listing_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("tree path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("tree path")
    return value


def flatten(listing: Iterable[object]) -> dict[str, tuple[str, str]]:
    """The leaf entries of a recursive tree listing (`GET /repos/{r}/git/trees/{sha}?recursive=1`, member `tree`);
    subtrees are implied by the paths. Anything outside the git format is a `ValueError`."""
    found: dict[str, tuple[str, str]] = {}
    for item in listing:
        if not isinstance(item, dict):
            raise ValueError("tree entry")
        mode, kind, sha = item.get("mode"), item.get("type"), item.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise ValueError("tree sha")
        if kind == "tree" and mode in (TREE_MODE, "040000"):
            _listing_path(item.get("path"))
            continue
        if not isinstance(mode, str) or _LISTING_TYPES.get(mode) != kind:
            raise ValueError("tree mode")
        path = _listing_path(item.get("path"))
        if path in found:
            raise ValueError("duplicate tree path")
        found[path] = (mode, sha)
    return found
