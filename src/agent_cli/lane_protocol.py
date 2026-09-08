"""Bounded model requests over data; only the calling script owns side effects.

This module deliberately has no filesystem, process, network or vendor client.
The script supplies a source snapshot and receives proposed text changes. A
model cannot select an executable, account, endpoint, lane, check or monitor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MAX_FILE_BYTES = 1_000_000
MAX_REQUEST_BYTES = 1_100_000
MAX_RESULT_BYTES = 100_000
MAX_PATH_BYTES = 1000


class ProtocolError(ValueError):
    """An invalid request is a blocked lane, never an executable fallback."""


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise ProtocolError("path must be a bounded relative string")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.casefold() == ".git" for p in parts):
        raise ProtocolError("invalid source path")
    if "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProtocolError("invalid source path")
    return value


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError("expected text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ProtocolError("text is not valid UTF-8") from exc
    if size > maximum or "\x00" in value:
        raise ProtocolError("text exceeds its limit or contains NUL")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def parse_request(raw: str) -> dict:
    _text(raw, MAX_REQUEST_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("invalid JSON constant")))
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("expected one strict JSON object") from exc
    if not isinstance(value, dict):
        raise ProtocolError("expected one JSON object")
    return value


@dataclass(frozen=True)
class Finished:
    text: str


class SourceSession:
    """In-memory source view and proposals, bounded by the static caller.

    A request is never interpreted as Python, shell, a regex or a path on the
    host. Writes require the digest of the current text. They do not mutate the
    caller's original snapshot, and a read-only role cannot propose changes.
    """

    def __init__(self, files: dict[str, str], *, write: bool, max_requests: int,
                 max_total_bytes: int):
        if type(write) is not bool or type(max_requests) is not int or max_requests < 1:
            raise ProtocolError("explicit valid role and request limit required")
        if type(max_total_bytes) is not int or max_total_bytes < 1:
            raise ProtocolError("explicit positive source byte limit required")
        self.original = {validate_path(p): _text(t, MAX_FILE_BYTES) for p, t in files.items()}
        self.files = dict(self.original)
        self.write = write
        self.remaining = max_requests
        self.max_total_bytes = max_total_bytes
        self.finished = False
        self._check_size(self.files)

    def _check_size(self, files: dict[str, str]) -> None:
        if sum(len(p.encode()) + len(t.encode()) for p, t in files.items()) > self.max_total_bytes:
            raise ProtocolError("source session byte limit exceeded")

    def request(self, raw: str) -> dict | Finished:
        if self.finished or self.remaining < 1:
            raise ProtocolError("source session finished or request limit exhausted")
        self.remaining -= 1
        request = parse_request(raw)
        action = request.get("action")
        fields = {
            "list": {"action", "prefix", "offset"},
            "read": {"action", "path", "offset", "limit"},
            "write": {"action", "path", "expected_sha256", "content"},
            "delete": {"action", "path", "expected_sha256"},
            "finish": {"action", "text"},
        }
        if not isinstance(action, str) or action not in fields or set(request) != fields[action]:
            raise ProtocolError("unknown action or unexpected fields")
        if action == "finish":
            text = _text(request["text"], MAX_RESULT_BYTES)
            if not text.strip():
                raise ProtocolError("empty final result")
            self.finished = True
            return Finished(text)
        if action == "list":
            prefix = _text(request["prefix"], MAX_PATH_BYTES)
            offset = self._integer(request["offset"], 0, 1_000_000)
            paths = sorted(p for p in self.files if p.startswith(prefix))
            page = paths[offset:offset + 50]
            return {"paths": page, "next_offset": offset + len(page) if offset + len(page) < len(paths) else None}
        path = validate_path(request["path"])
        if action == "read":
            if path not in self.files:
                raise ProtocolError("source file is not available")
            offset = self._integer(request["offset"], 0, 1_000_000)
            limit = self._integer(request["limit"], 1, 200)
            text = self.files[path]
            lines = text.splitlines(keepends=True)
            content = "".join(lines[offset:offset + limit])
            if len(content.encode()) > MAX_RESULT_BYTES:
                raise ProtocolError("read result exceeds byte limit")
            return {"path": path, "sha256": digest(text), "offset": offset,
                    "total_lines": len(lines), "content": content}
        if not self.write:
            raise ProtocolError("read-only role cannot change source")
        old = self.files.get(path)
        expected = request["expected_sha256"]
        if expected != (digest(old) if old is not None else None):
            raise ProtocolError("source digest does not match")
        proposed = dict(self.files)
        if action == "delete":
            if old is None:
                raise ProtocolError("cannot delete an absent file")
            del proposed[path]
        else:
            proposed[path] = _text(request["content"], MAX_FILE_BYTES)
        self._check_size(proposed)
        self.files = proposed
        return {"path": path, "sha256": digest(proposed[path]) if path in proposed else None}

    @staticmethod
    def _integer(value: object, minimum: int, maximum: int) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ProtocolError("integer outside allowed range")
        return value

    def changes(self) -> dict[str, str | None]:
        if not self.finished:
            raise ProtocolError("unfinished lane has no applicable changes")
        return {p: self.files.get(p) for p in self.original.keys() | self.files.keys()
                if self.original.get(p) != self.files.get(p)}
