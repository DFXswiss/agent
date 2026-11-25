"""The runner's side of `workvm-browser` (docs/platform/spec/13-work-vm.md section 13.3): argv, the closed schema of a
`call` answer and of a screencast header, the selftest; every answer is untrusted."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agent_runner.workvm import browser_helper as helper
from agent_runner.workvm.jobs import ShellResult, WorkVmError

from .kit import JPEG, browser_answer


def result(value: Any, *, code: int = 0, truncated: bool = False) -> ShellResult:
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    return ShellResult(code, data + b"\n", truncated)


def test_argv() -> None:
    assert helper.call_argv() == ["sudo", "-n", "/usr/local/bin/workvm-browser", "call"]
    assert helper.selftest_argv() == ["sudo", "-n", "/usr/local/bin/workvm-browser", "selftest"]
    assert helper.stream_argv() == ["sudo", "-n", "/usr/local/bin/workvm-browser", "stream", "5", "60", "1280"]


def test_a_valid_answer_is_parsed_and_cut() -> None:
    answer = helper.parse_answer(
        result(
            browser_answer(
                error="e" * 3000,
                notice="n\x00\ud800",
                title="t" * 2000,
                snapshot="s" * 30000,
                result="r" * 30000,
                masked=True,
                file={"path": "/var/lib/workvm-browser/downloads/a b.pdf", "name": "a b.pdf", "bytes": 3},
            )
        )
    )
    assert answer.ok is True
    assert answer.error == "e" * 2048
    assert answer.notice == "n" + "\ufffd" * 4  # NUL, and the three bytes of a lone surrogate
    assert answer.title == "t" * 1024
    assert len(answer.snapshot) == helper.SNAPSHOT_LIMIT
    assert answer.snapshot.endswith(helper.TRUNCATED)
    assert answer.result is not None
    assert answer.result.endswith(helper.TRUNCATED)
    assert answer.image == helper.Image(960, 600, JPEG)
    assert answer.file == helper.File("/var/lib/workvm-browser/downloads/a b.pdf", "a b.pdf", 3)
    assert answer.tabs == [{"index": 0, "url": "https://example.test/", "title": "Example", "active": True}]
    short = helper.parse_answer(result(browser_answer(image=None, error=None, result="1")))
    assert (short.image, short.error, short.result, short.snapshot) == (None, None, "1", '- heading "Example" [ref=e1]')


GOOD_IMAGE = {"width": 1, "height": 1, "jpeg": base64.b64encode(b"j").decode()}
REFUSED: list[Any] = [
    browser_answer(extra=1),
    {**browser_answer(), "ok": None},
    browser_answer(ok="yes"),
    browser_answer(error=1),
    browser_answer(url=None),
    browser_answer(tab=64),
    browser_answer(tab=True),
    browser_answer(tabs={}),
    browser_answer(tabs=[{"index": 0}]),
    browser_answer(tabs=[{"index": 0, "url": "u", "title": "t", "active": 1}]),
    browser_answer(tabs=[{"index": 0, "url": "u", "title": "t", "active": True}] * 65),
    browser_answer(masked=None),
    browser_answer(result=7),
    browser_answer(image={"width": 1, "height": 1}),
    browser_answer(image={**GOOD_IMAGE, "jpeg": "!!"}),
    browser_answer(image={**GOOD_IMAGE, "jpeg": ""}),
    browser_answer(image={**GOOD_IMAGE, "width": 0}),
    browser_answer(file={"path": "/etc/passwd", "name": "passwd", "bytes": 1}),
    browser_answer(file={"path": "/var/lib/workvm-browser/../x", "name": "x", "bytes": 1}),
    browser_answer(file={"path": "/var/lib/workvm-browser/" + "a" * 4096, "name": "x", "bytes": 1}),
    browser_answer(file={"path": "/var/lib/workvm-browser/x", "name": "a/b", "bytes": 1}),
    browser_answer(file={"path": "/var/lib/workvm-browser/x", "name": "..", "bytes": 1}),
    browser_answer(file={"path": "/var/lib/workvm-browser/x", "name": "x" * 256, "bytes": 1}),
    browser_answer(file={"path": "/var/lib/workvm-browser/x", "name": "x", "bytes": -1}),
    [],
]


@pytest.mark.parametrize("value", REFUSED)
def test_an_answer_outside_the_schema_is_invalid(value: Any) -> None:
    with pytest.raises(WorkVmError) as caught:
        helper.parse_answer(result(value))
    assert caught.value.code == "workvm_answer_invalid"


@pytest.mark.parametrize(
    "shell",
    [
        result(browser_answer(), code=1),
        result(browser_answer(), truncated=True),
        ShellResult(0, b"x" * (helper.ANSWER_LIMIT + 1), False),
        ShellResult(0, b"\xff", False),
        ShellResult(0, b"{", False),
        ShellResult(0, b"[" * 100000 + b"]" * 100000, False),  # nested deeper than the parser goes
    ],
)
def test_a_broken_answer_is_invalid(shell: ShellResult) -> None:
    with pytest.raises(WorkVmError):
        helper.parse_answer(shell)


@pytest.mark.parametrize(
    ("shell", "ok"),
    [
        (result({"ok": True}), True),
        (result({"ok": 1}), False),
        (result({"ok": True, "x": 1}), False),
        (result([]), False),
        (result({"ok": True}, code=1), False),
        (result({"ok": True}, truncated=True), False),
        (ShellResult(0, b"\xff", False), False),
        (ShellResult(0, b"[" * 100000, False), False),
    ],
)
def test_selftest(shell: ShellResult, ok: bool) -> None:
    assert helper.selftest_ok(shell) is ok


def test_frame_headers() -> None:
    assert helper.parse_header(b'{"bytes": 5, "width": 1280, "height": 800}') == helper.Header(5, 1280, 800)
    assert helper.parse_header(b'{"bytes": 5, "width": 0, "height": 0}') == helper.Header(5, 0, 0)
    for line in (
        b'{"bytes": 0, "width": 1, "height": 1}',
        b'{"bytes": 524289, "width": 1, "height": 1}',
        b'{"bytes": 1, "width": 1281, "height": 1}',
        b'{"bytes": 1, "width": 1}',
        b"[]",
        b"\xff",
        b"[" * 4000,
        b" " * 4097,
    ):
        with pytest.raises(WorkVmError):
            helper.parse_header(line)
