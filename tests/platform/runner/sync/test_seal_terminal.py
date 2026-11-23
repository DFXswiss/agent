"""U-RUN-outbox: terminal bodies beyond the E8 limit are split, never dropped (04-crypto.md section 6.4 E8, 05-runner.md
section 7; AUD-01 A-03, decisions/KOR-02.md)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.crypto.encoding import body_json
from agent_runner.sync.seal import MAX_TERMINAL_BODY, MAX_TERMINAL_DATA, MAX_TERMINAL_FRAMES, split_terminal


def body(*frames: Any, **extra: Any) -> dict[str, Any]:
    return {"offset_ms_start": 0, "frames": list(frames), "cols": 160, "rows": 48, **extra}


def test_a_body_within_the_limit_is_kept() -> None:
    small = body([0, "o", "\x1b" * 1000])
    assert split_terminal(small) == [small]
    assert split_terminal(small)[0] is small


ESC = "\x1b" * 16384  # 16 KiB of data, 96 KiB of body JSON


@pytest.mark.parametrize(
    "value",
    [
        body([0, "o", ESC], extra=1),
        {"offset_ms_start": 0, "frames": ESC, "cols": 1, "rows": 1},
        body(),
        body([0, "o"], [0, "o", ESC]),
        body(ESC),
        body([True, "o", ESC]),
        body([-1, "o", ESC]),
        body([2**53, "o", ESC]),
        body([0, 1, ESC]),
        body([0, "x", ESC]),
        body([0, "o", 70000]),
        body([0, "o", ESC], [0, "o", None]),
        body([0, "o", ESC], [0, "o", "x"]),  # more than 16 KiB of data
        body([0, "o", "x" * 70000]),  # far more than the contract allows a guest to write
        body(*([[0, "o", "\x1b" * 4]] * (MAX_TERMINAL_FRAMES + 1))),
        {**body([0, "o", ESC]), "offset_ms_start": -1},
        {**body([0, "o", ESC]), "cols": 0},
        {**body([0, "o", ESC]), "rows": 1001},
        {**body([0, "o", ESC]), "rows": "48"},
    ],
)
def test_malformed_bodies_are_left_to_the_seal_checks(value: dict[str, Any]) -> None:
    assert split_terminal(value) == [value]


def test_an_oversized_body_becomes_consecutive_bodies_within_the_limit() -> None:
    frames = [
        [1, "o", ""],
        [2, "o", "\x1b" * 9000],
        [3, "o", "😀" * 500],
        [4, "m", "recorder restarted"],
        [5, "o", ESC[:5000]],
    ]
    parts = split_terminal(body(*frames))
    assert len(parts) > 1
    for part in parts:
        assert len(body_json(part)) <= MAX_TERMINAL_BODY
        assert part["offset_ms_start"] == part["frames"][0][0]
        assert all(len(frame[2]) <= MAX_TERMINAL_DATA for frame in part["frames"])
        assert sum(len(frame[2].encode()) for frame in part["frames"]) <= MAX_TERMINAL_DATA
        assert len(part["frames"]) <= MAX_TERMINAL_FRAMES
        assert (part["cols"], part["rows"]) == (160, 48)
    flat = [frame for part in parts for frame in part["frames"]]
    assert flat[0] == [1, "o", ""]
    assert [4, "m", "recorder restarted"] in flat
    assert "".join(frame[2] for frame in flat if frame[0] == 2) == "\x1b" * 9000
    assert "".join(frame[2] for frame in flat if frame[0] == 3) == "😀" * 500
    assert "".join(frame[2] for frame in flat if frame[0] == 5) == ESC[:5000]


def test_a_part_never_holds_more_frames_than_the_contract_allows() -> None:
    # 4095 empty frames and one frame of escaped data: the pieces would fit one body, but not one frame list
    frames = [[0, "o", ""]] * 4095 + [[0, "o", "\x1b" * 3399]]
    value = {"offset_ms_start": 2**53 - 1, "frames": frames, "cols": 1000, "rows": 1000}
    assert len(body_json(value)) > MAX_TERMINAL_BODY
    parts = split_terminal(value)
    assert [len(part["frames"]) for part in parts] == [MAX_TERMINAL_FRAMES, 1]
    assert len(body_json({**parts[0], "frames": [*parts[0]["frames"], parts[1]["frames"][0]]})) <= MAX_TERMINAL_BODY
