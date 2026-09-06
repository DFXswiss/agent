"""Immutability adapter: allow only configured whole-line comment edits."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from .common import (
    JobError,
    JobRuntime,
    CommonConfig,
    loads_strict_json,
    parse_common_config,
    reject_unknown_keys,
    require_mapping,
    require_rel_path,
    require_str,
    require_str_list,
    run_lifecycle,
)

IMMUTABLE_KEYS = frozenset({"path", "exclude", "comment_prefix", "unset", "unset_prefixes", "env", "lock", "npm", "postgres"})
# Common keys are accepted but unused beyond scoping for symmetry with other adapters.


def parse_immutable_config(text: str) -> tuple[CommonConfig, dict[str, Any]]:
    raw = loads_strict_json(text)
    obj = require_mapping(raw, "config")
    reject_unknown_keys(obj, IMMUTABLE_KEYS, "immutable config")
    if "path" not in obj:
        raise JobError("immutable config requires path")
    path = require_rel_path(obj["path"], "path")
    exclude: list[str] = []
    if "exclude" in obj:
        exclude = [require_rel_path(item, "exclude[]") for item in require_str_list(obj["exclude"], "exclude")]
    comment_prefix = None
    if "comment_prefix" in obj:
        comment_prefix = require_str(obj["comment_prefix"], "comment_prefix")
        if any(ch in comment_prefix for ch in ("\n", "\r")):
            raise JobError("comment_prefix must not contain newlines")
    common = parse_common_config(obj)
    return common, {"path": path, "exclude": exclude, "comment_prefix": comment_prefix}


def _git_bytes(
    runtime: JobRuntime,
    args: list[str],
) -> subprocess.CompletedProcess[bytes]:
    return runtime.run_argv(
        ["git", *args],
        cwd=runtime.root,
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _parse_name_status_z(payload: bytes) -> list[tuple[str, str, str | None]]:
    """Parse ``git diff -z --name-status`` records.

    Regular entries: STATUS\\0PATH\\0
    Renames/copies: STATUS\\0OLD\\0NEW\\0
    """
    if not payload:
        return []
    parts = payload.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    records: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(parts):
        status_b = parts[index]
        index += 1
        try:
            status = status_b.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JobError("git name-status status is not valid UTF-8") from exc
        if not status:
            raise JobError("git name-status produced an empty status")
        code = status[0]
        if code in {"R", "C"}:
            if index + 1 >= len(parts):
                raise JobError("git name-status rename/copy record is truncated")
            old = parts[index]
            new = parts[index + 1]
            index += 2
            records.append((status, _decode_path(old), _decode_path(new)))
        else:
            if index >= len(parts):
                raise JobError("git name-status record is truncated")
            path = parts[index]
            index += 1
            records.append((status, _decode_path(path), None))
    return records


def _decode_path(raw: bytes) -> str:
    # Preserve unusual bytes via surrogateescape so NUL-free odd names compare safely.
    if b"\x00" in raw:
        raise JobError("git path unexpectedly contains NUL inside a -z field")
    return raw.decode("utf-8", "surrogateescape")


def _read_blob(runtime: JobRuntime, commit: str, path: str) -> bytes:
    # Resolve the exact literal tree path to an object id, avoiding revision/path
    # ambiguity for names containing colons, tabs, newlines, or pathspec metacharacters.
    literal = f":(top,literal){path}"
    listed = _git_bytes(
        runtime,
        ["ls-tree", "-z", "--full-tree", commit, "--", literal],
    )
    if listed.returncode != 0:
        detail = listed.stderr.decode("utf-8", "replace").strip()
        raise JobError(f"git ls-tree failed for selected path: {detail or listed.returncode}")
    records = [item for item in listed.stdout.split(b"\0") if item]
    expected_path = path.encode("utf-8", "surrogateescape")
    matches: list[bytes] = []
    for record in records:
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise JobError("git ls-tree produced malformed output")
        if raw_path == expected_path:
            matches.append(metadata)
    if len(matches) != 1:
        raise JobError(f"selected Git blob is missing at {commit}")
    fields = matches[0].split()
    if len(fields) != 3 or fields[1] != b"blob":
        raise JobError(f"selected Git path is not a blob at {commit}")
    try:
        object_id = fields[2].decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise JobError("git ls-tree produced an invalid object id") from exc
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", object_id) is None:
        raise JobError("git ls-tree produced an invalid object id")
    blob = _git_bytes(runtime, ["cat-file", "blob", object_id])
    if blob.returncode != 0:
        detail = blob.stderr.decode("utf-8", "replace").strip()
        raise JobError(f"git cat-file failed for selected blob: {detail or blob.returncode}")
    return blob.stdout


def _strip_comment_lines(data: bytes, prefix: str) -> bytes:
    prefix_b = prefix.encode("utf-8")
    out_lines: list[bytes] = []
    # Split on lines but preserve whether the file ended with a newline by
    # comparing filtered line lists; missing blobs never become empty equality.
    parts = data.split(b"\n")
    # If data ends with newline, last part is empty; keep that structure after filter.
    trailing_empty = bool(parts) and parts[-1] == b"" and data.endswith(b"\n")
    body = parts[:-1] if trailing_empty else parts
    for line in body:
        stripped = line.lstrip(b" \t")
        if stripped.startswith(prefix_b):
            continue
        out_lines.append(line)
    if trailing_empty:
        return b"\n".join(out_lines) + (b"\n" if out_lines or data.endswith(b"\n") else b"")
    return b"\n".join(out_lines)


def _comment_only_change(runtime: JobRuntime, path: str, prefix: str) -> bool:
    base_blob = _read_blob(runtime, runtime.base, path)
    head_blob = _read_blob(runtime, runtime.head, path)
    return _strip_comment_lines(base_blob, prefix) == _strip_comment_lines(head_blob, prefix)


def _body(runtime: JobRuntime, cfg: Mapping[str, Any]) -> int:
    path = cfg["path"]
    exclude = cfg["exclude"]
    diff_args = [
        "diff",
        "-z",
        "--name-status",
        f"{runtime.base}...{runtime.head}",
        "--",
        f":(top,literal){path}",
    ]
    for item in exclude:
        diff_args.append(f":(top,exclude,literal){item}")
    completed = _git_bytes(runtime, diff_args)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise JobError(f"git diff failed: {detail or completed.returncode}")

    records = _parse_name_status_z(completed.stdout)
    blocked: list[str] = []
    for status, path_a, path_b in records:
        code = status[0]
        if code in {"A"}:
            # New files under the watched path are allowed (follow-up additions).
            continue
        if code == "M":
            if cfg["comment_prefix"] is None:
                blocked.append(f"{status}\t{path_a}")
                continue
            try:
                if _comment_only_change(runtime, path_a, cfg["comment_prefix"]):
                    # Display path carefully without assuming printable ASCII.
                    display = path_a.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
                    print(f"a38: allowing comment-only change to {display}", flush=True)
                    continue
            except JobError as exc:
                print(f"a38: {exc}", file=sys.stderr)
                blocked.append(f"{status}\t{path_a}")
                continue
            blocked.append(f"{status}\t{path_a}")
            continue
        # D / R / T / C and anything else block.
        if path_b is not None:
            blocked.append(f"{status}\t{path_a}\t{path_b}")
        else:
            blocked.append(f"{status}\t{path_a}")

    if blocked:
        print(
            "a38: existing paths must not be modified, renamed, type-changed or deleted.",
            file=sys.stderr,
        )
        for line in blocked:
            print(line, file=sys.stderr)
        print(
            "Only whole-line comment changes are allowed when comment_prefix is configured; "
            "add a follow-up file instead.",
            file=sys.stderr,
        )
        return 1
    print(f"a38: no blocking changes vs {runtime.base}", flush=True)
    return 0


def run_immutable(
    config_text: str,
    *,
    cwd: Path | None = None,
    lock_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    common, parsed = parse_immutable_config(config_text)

    def body(runtime: JobRuntime) -> int:
        return _body(runtime, parsed)

    return run_lifecycle(
        adapter="immutable",
        common=common,
        body=body,
        cwd=cwd,
        lock_root=lock_root,
        environ=environ,
    )
