"""The runner's side of the browser helper in the VM (docs/platform/spec/13-work-vm.md section 13.3).

The runner runs `sudo -n /usr/local/bin/workvm-browser call|selftest|stream ...` over Lima's SSH. The VM belongs to
the session and its root may have replaced the helper, so every answer is untrusted: a `call` answer is one JSON line
of at most `ANSWER_LIMIT` bytes with exactly the keys of `ANSWER_KEYS` and their types (`parse_answer`), a screencast
frame is a header line of at most `HEADER_LIMIT` bytes with exactly `{bytes, width, height}` followed by at most
`FRAME_LIMIT` JPEG bytes (`parse_header`). Anything else raises `WorkVmError("workvm_answer_invalid")`, which the
caller treats as a failed helper (`vm_lost`). Texts are cut to the limits of the answer and the event (never
rejected for their length; the byte cap bounds them), NUL and lone surrogates become U+FFFD.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner.workvm import jobs, sync
from agent_runner.workvm.jobs import ShellResult, WorkVmError

HELPER = "/usr/local/bin/workvm-browser"
STATE_DIR = "/var/lib/workvm-browser/"
UPLOADS = STATE_DIR + "uploads"
ANSWER_LIMIT = 4 * 1024 * 1024
HEADER_LIMIT = 4096
FRAME_LIMIT = 512 * 1024
SNAPSHOT_LIMIT = 24000
RESULT_LIMIT = 24000
TRUNCATED = "[…truncated]"
URL_LIMIT = 8192
TITLE_LIMIT = 1024
ERROR_LIMIT = 2048
NOTICE_LIMIT = 1024
MAX_TABS = 64
MAX_SAFE = 2**53 - 1
STREAM_FPS = 5
STREAM_QUALITY = 60
STREAM_WIDTH = 1280
ANSWER_KEYS = frozenset(
    {"ok", "error", "notice", "url", "title", "tab", "tabs", "snapshot", "masked", "result", "image", "file"}
)


def call_argv() -> list[str]:
    return ["sudo", "-n", HELPER, "call"]


def selftest_argv() -> list[str]:
    return ["sudo", "-n", HELPER, "selftest"]


def stream_argv() -> list[str]:
    return ["sudo", "-n", HELPER, "stream", str(STREAM_FPS), str(STREAM_QUALITY), str(STREAM_WIDTH)]


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    jpeg: bytes


@dataclass(frozen=True)
class File:
    path: str
    name: str
    bytes: int


@dataclass(frozen=True)
class Answer:
    ok: bool
    error: str | None
    notice: str | None
    url: str
    title: str
    tab: int
    tabs: list[dict[str, Any]]
    snapshot: str
    masked: bool
    result: str | None
    image: Image | None
    file: File | None


@dataclass(frozen=True)
class Header:
    bytes: int
    width: int
    height: int


def _invalid() -> WorkVmError:
    return WorkVmError("workvm_answer_invalid")


def clean(value: str, limit: int) -> str:
    """`value` without NUL and lone surrogates (U+FFFD), at most `limit` characters."""
    return jobs.text(value.encode("utf-8", errors="surrogatepass"))[:limit]


def cut(value: str, limit: int) -> str:
    """`value` cleaned; longer than `limit` it ends with `TRUNCATED` within the limit."""
    text = clean(value, len(value))
    return text if len(text) <= limit else text[: limit - len(TRUNCATED)] + TRUNCATED


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid()
    return value


def _optional(value: object, limit: int) -> str | None:
    return None if value is None else clean(_string(value), limit)


def _integer(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _invalid()
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _invalid()
    return value


def _object(value: object, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid()
    return value


def _tabs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_TABS:
        raise _invalid()
    tabs: list[dict[str, Any]] = []
    for item in value:
        tab = _object(item, frozenset({"index", "url", "title", "active"}))
        tabs.append(
            {
                "index": _integer(tab["index"], 0, MAX_TABS - 1),
                "url": clean(_string(tab["url"]), URL_LIMIT),
                "title": clean(_string(tab["title"]), TITLE_LIMIT),
                "active": _boolean(tab["active"]),
            }
        )
    return tabs


def _image(value: object) -> Image | None:
    if value is None:
        return None
    image = _object(value, frozenset({"width", "height", "jpeg"}))
    try:
        jpeg = base64.b64decode(_string(image["jpeg"]), validate=True)
    except (binascii.Error, ValueError):
        raise _invalid() from None
    if not jpeg:
        raise _invalid()
    return Image(_integer(image["width"], 1, 16384), _integer(image["height"], 1, 16384), jpeg)


def _file(value: object) -> File | None:
    if value is None:
        return None
    found = _object(value, frozenset({"path", "name", "bytes"}))
    path, name = _string(found["path"]), _string(found["name"])
    below = path[len(STATE_DIR) :]
    if not path.startswith(STATE_DIR) or not sync.valid_path(below) or len(path) > 4096:
        raise _invalid()
    if "/" in name or len(name) > 255 or not sync.valid_path(name):
        raise _invalid()
    return File(path, name, _integer(found["bytes"], 0, MAX_SAFE))


def parse_answer(result: ShellResult) -> Answer:
    """The checked answer of `workvm-browser call` (module docstring)."""
    if result.returncode != 0 or result.truncated or len(result.stdout) > ANSWER_LIMIT:
        raise _invalid()
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid() from None
    answer = _object(data, ANSWER_KEYS)
    result_text = answer["result"]
    return Answer(
        ok=_boolean(answer["ok"]),
        error=_optional(answer["error"], ERROR_LIMIT),
        notice=_optional(answer["notice"], NOTICE_LIMIT),
        url=clean(_string(answer["url"]), URL_LIMIT),
        title=clean(_string(answer["title"]), TITLE_LIMIT),
        tab=_integer(answer["tab"], 0, MAX_TABS - 1),
        tabs=_tabs(answer["tabs"]),
        snapshot=cut(_string(answer["snapshot"]), SNAPSHOT_LIMIT),
        masked=_boolean(answer["masked"]),
        result=None if result_text is None else cut(_string(result_text), RESULT_LIMIT),
        image=_image(answer["image"]),
        file=_file(answer["file"]),
    )


def selftest_ok(result: ShellResult) -> bool:
    """`workvm-browser selftest` printed exactly `{"ok": true}`."""
    if result.returncode != 0 or result.truncated:
        return False
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and set(data) == {"ok"} and data["ok"] is True


def parse_header(line: bytes) -> Header:
    """The header line of one screencast frame (without its newline)."""
    if len(line) > HEADER_LIMIT:
        raise _invalid()
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid() from None
    header = _object(data, frozenset({"bytes", "width", "height"}))
    return Header(
        _integer(header["bytes"], 1, FRAME_LIMIT),
        _integer(header["width"], 0, STREAM_WIDTH),  # 0: the helper could not read the JPEG's size
        _integer(header["height"], 0, 16384),
    )
