"""U-GST-recorder: terminal recorder (05-runner.md section 7, 4.6 defects 5 and 6; ADR-016; GST-05).

The recorder reads a real FIFO written by the test (the synthetic stand-in for ``tmux pipe-pane``); tmux itself is a
recording fake. The Linux suite (``guest_linux/capture``) pipes a real tmux pane.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_guest import recorder, tmux
from agent_guest.control import ControlContext
from agent_guest.recorder import CastFile, Recorder, TerminalRecorder

SID = "01a1ee56-5e00-7003-8f1b-340691d99f03"
EURO = "€".encode()


# -- pure recorder --------------------------------------------------------------------------------------------------


def test_split_keeps_characters_whole() -> None:
    assert recorder.split("") == []
    assert recorder.split("abcdef", 4) == ["abcd", "ef"]
    assert recorder.split("a€€", 4) == ["a€", "€"]
    assert all(len(piece.encode()) <= 5 for piece in recorder.split("€" * 10 + "x", 5))


def test_multibyte_characters_across_reads_are_decoded_whole() -> None:
    rec = Recorder()
    rec.output(b"a" + EURO[:1], 10)
    rec.output(EURO[1:2], 20)
    rec.output(EURO[2:] + b"b\xff", 30)
    (body,) = rec.take()
    assert body == {"offset_ms_start": 10, "frames": [[10, "o", "a"], [30, "o", "€b�"]], "cols": 160, "rows": 48}
    assert not rec.pending


def test_an_incomplete_character_is_replaced_at_the_end() -> None:
    rec = Recorder(base_ms=500)
    rec.output(EURO[:2], 400)
    assert rec.take() == []
    (body,) = rec.take(final=True)
    assert body["frames"] == [[500, "o", "�"]]


def test_offsets_never_decrease_and_markers_are_events() -> None:
    rec = Recorder(base_ms=1000)
    rec.marker(1000)
    rec.output(b"x", 900)
    rec.output(b"y", 1500)
    rec.marker(1200, "custom")
    (body,) = rec.take()
    assert body["frames"] == [[1000, "m", "recorder restarted"], [1000, "o", "x"], [1500, "o", "y"],
                              [1500, "m", "custom"]]  # fmt: skip
    assert rec.last_ms == 1500


def test_records_hold_at_most_16_kib_of_data_and_4096_frames() -> None:
    rec = Recorder()
    rec.output(("€" * 6000).encode(), 1)  # 18000 bytes: split into two events
    assert rec.pending_bytes == 18000
    bodies = rec.take()
    assert [len(body["frames"]) for body in bodies] == [1, 1]
    assert all(sum(len(frame[2].encode()) for frame in body["frames"]) <= recorder.MAX_RECORD for body in bodies)
    for index in range(recorder.MAX_FRAMES + 2):
        rec.output(b"z", index)
    bodies = rec.take()
    assert [len(body["frames"]) for body in bodies] == [recorder.MAX_FRAMES, 2]
    assert bodies[1]["offset_ms_start"] == recorder.MAX_FRAMES


def encoded_size(body: dict[str, Any]) -> int:
    """Bytes of the body JSON the runner encrypts (04-crypto.md section 1: sorted keys, no whitespace, UTF-8)."""
    return len(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


MIXED = ('\x1b[1;31mGrüße\x1b[0m "q" \\ \r\n\t\x00\x07\x7f'.encode() + b"\xff\xfe" + "€😀".encode()) * 900


@pytest.mark.parametrize(
    ("label", "chunks"),
    [
        ("control characters", [b"\x1b" * 40000]),
        ("umlauts", ["äöü".encode() * 9000]),
        ("mixed frames", [MIXED[i : i + 777] for i in range(0, len(MIXED), 777)]),
        ("many small escaped frames", [b"\x01"] * 9000),
    ],
)
def test_every_body_fits_the_terminal_event_limit_after_json_escaping(label: str, chunks: list[bytes]) -> None:
    # E8: the sealed `session.terminal` (body JSON + 16-byte tag) is at most 64 KiB; JSON escapes a control character
    # as six bytes, so 16 KiB of raw data can become far more than 64 KiB
    rec = Recorder()
    for index, chunk in enumerate(chunks):
        rec.output(chunk, 2**40 + index)
    bodies = rec.take(final=True)
    assert all(encoded_size(body) + 16 <= 64 * 1024 for body in bodies), label
    assert all(sum(len(frame[2].encode()) for frame in body["frames"]) <= recorder.MAX_RECORD for body in bodies)
    assert all(len(body["frames"]) <= recorder.MAX_FRAMES for body in bodies)
    assert all(len(frame[2]) <= recorder.MAX_RECORD for body in bodies for frame in body["frames"])
    data = "".join(frame[2] for body in bodies for frame in body["frames"])
    assert data == b"".join(chunks).decode("utf-8", errors="replace")


def test_a_body_fills_the_limit_exactly_and_one_byte_more_starts_the_next() -> None:
    head: list[list[Any]] = [[0, "o", "\x1b" * 2730]] * 3 + [[0, "o", "\x1b" * 1620]]
    fill = recorder.MAX_BODY_JSON - encoded_size({"offset_ms_start": 0, "frames": head, "cols": 160, "rows": 48})
    for extra, expected in ((0, [4]), (1, [3, 1])):
        rec = Recorder()
        for _ in range(3):
            rec.output(b"\x1b" * 2730, 0)
        rec.output(b"\x1b" * 1620 + b"x" * (fill + extra), 0)
        bodies = rec.take()
        assert [len(body["frames"]) for body in bodies] == expected
        assert sum(len(frame[2]) for body in bodies for frame in body["frames"]) <= recorder.MAX_RECORD
        assert [encoded_size(body) <= recorder.MAX_BODY_JSON for body in bodies] == [True] * len(bodies)
    assert encoded_size(Recorder._body(Recorder(), [*head[:3], [0, "o", head[3][2] + "x" * fill]])) == (
        recorder.MAX_BODY_JSON
    )


def test_split_bounds_the_escaped_size_of_a_piece() -> None:
    assert recorder.split("\x1b" * 10, 100, 12) == ["\x1b\x1b", "\x1b\x1b", "\x1b\x1b", "\x1b\x1b", "\x1b\x1b"]
    assert recorder.split('a"b', 100, 2) == ["a", '"', "b"]
    assert recorder.split("ab" * 5, 4, 100) == ["abab", "abab", "ab"]
    assert recorder.split("😀a", 3) == ["😀", "a"]  # a character is never cut, even beyond a tiny limit


def test_bodies_match_the_journal_contract(context: ControlContext) -> None:
    rec = Recorder()
    rec.marker(0)
    rec.output(("\x1b[1m€" * 5000).encode(), 7)
    for body in rec.take():
        assert context.schemas.errors("guest/journal/session.terminal.json", body) == []


# -- cast file ----------------------------------------------------------------------------------------------------


def test_cast_file_header_events_and_restart_offset(tmp_path: Path) -> None:
    cast = CastFile(str(tmp_path / "terminal.cast"))
    assert cast.open(1_790_000_000.9) == (0, False)
    cast.append([[0, "o", "a"], [1234, "o", "€\n"]])
    lines = (tmp_path / "terminal.cast").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"version": 2, "width": 160, "height": 48, "timestamp": 1790000000}
    assert [json.loads(line) for line in lines[1:]] == [[0.0, "o", "a"], [1.234, "o", "€\n"]]
    assert oct((tmp_path / "terminal.cast").stat().st_mode & 0o777) == "0o600"
    assert cast.open(0) == (1234, True)


def test_last_offset_skips_partial_and_foreign_lines() -> None:
    assert recorder.last_offset(b'{"version": 2}\n') == 0
    assert recorder.last_offset(b'[0.5, "o", "a"]\n[2.25, "o", "b"]\n[3.0, "o", "trunc') == 2250
    assert recorder.last_offset(b'[1, "o", "x"]\n[-1, "o", "y"]\n["a"]\n[]\n') == 1000
    assert recorder.last_offset(b"") == 0


def test_a_long_cast_file_is_read_from_its_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "TAIL_BYTES", 32)
    path = tmp_path / "terminal.cast"
    path.write_text('{"version": 2}\n' + "".join(f'[{n}, "o", "x"]\n' for n in range(100)))
    assert CastFile(str(path)).open(0) == (99000, True)


# -- terminal recorder --------------------------------------------------------------------------------------------


class Ticks:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


@dataclass
class FakeTmux:
    calls: list[list[str]] = field(default_factory=list)
    code: int = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["env"] == tmux.CLIENT_ENV
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, "", "")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / SID
    path.mkdir(parents=True)
    return path


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


@pytest.fixture
def fake_tmux() -> FakeTmux:
    return FakeTmux()


@pytest.fixture
def make(context: ControlContext, base: Path, short_tmp: Path, ticks: Ticks, fake_tmux: FakeTmux) -> Iterator[Any]:
    made: list[TerminalRecorder] = []

    def create(**kwargs: Any) -> TerminalRecorder:
        created = TerminalRecorder(
            context.journal,
            SID,
            str(base),
            tmux.Tmux(socket="/run/agent-guest/tmux.sock", run=fake_tmux, scope=False),
            fake_tmux,
            fifo_dir=str(short_tmp / "capture"),
            monotonic=ticks,
            **kwargs,
        )
        made.append(created)
        return created

    yield create
    for item in made:
        if item._fd >= 0:
            item._close()


def feed(rec: TerminalRecorder, data: bytes) -> None:
    fd = os.open(rec.fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def terminal(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 1000)
    return [record["body"] for record in records if record["kind"] == "session.terminal"]


def test_pane_output_is_journaled_after_100_ms_and_copied_to_the_cast_file(
    context: ControlContext, make: Any, ticks: Ticks, fake_tmux: FakeTmux, base: Path
) -> None:
    seen: list[int] = []
    rec = make(on_output=lambda: seen.append(1))
    rec.open()
    fifo = rec.fifo
    assert fake_tmux.calls == [
        ["tmux", "-S", "/run/agent-guest/tmux.sock", "pipe-pane", "-O", "-t", f"=s-{SID}:", f"exec cat >> {fifo}"]
    ]
    assert oct(os.stat(fifo).st_mode & 0o777) == "0o600"
    assert rec.wait(0) is False
    assert rec.due() is False
    feed(rec, b"hello " + EURO[:1])
    ticks.value += 0.25
    assert rec.wait(1) is True
    assert rec.drain() == 7
    feed(rec, EURO[1:])
    ticks.value += 0.05
    rec.step()
    assert terminal(context) == []
    assert seen == [1]
    ticks.value += 0.05
    rec.step()
    (body,) = terminal(context)
    assert body == {"offset_ms_start": 250, "frames": [[250, "o", "hello "], [300, "o", "€"]], "cols": 160,
                    "rows": 48}  # fmt: skip
    feed(rec, b"more")
    rec.drain()
    assert seen == [1]
    rec.stop()
    assert not os.path.exists(fifo)
    assert fake_tmux.calls[-1] == ["tmux", "-S", "/run/agent-guest/tmux.sock", "pipe-pane", "-t", f"=s-{SID}:"]
    cast = [json.loads(line) for line in (base / "terminal.cast").read_text(encoding="utf-8").splitlines()]
    events = [[round(event[0] * 1000), event[1], event[2]] for event in cast[1:]]
    assert events == [frame for body in terminal(context) for frame in body["frames"]]
    assert events[-1] == [350, "o", "more"]


def test_reads_continue_while_whole_chunks_arrive(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "READ_CHUNK", 4)
    rec = make()
    rec.open()
    feed(rec, b"12345678")
    assert rec.drain() == 8
    feed(rec, b"123456789")
    assert rec.drain() == 9


def test_16_kib_are_flushed_at_once(context: ControlContext, make: Any) -> None:
    rec = make()
    rec.open()
    for _ in range(recorder.MAX_RECORD // 1024):  # FIFO buffers can be smaller than 16 KiB
        feed(rec, b"x" * 1024)
        rec.drain()
    assert rec.due() is True
    rec.step()
    assert len(terminal(context)) == 1


def test_a_restarted_recorder_continues_the_offsets_with_a_marker(
    context: ControlContext, make: Any, ticks: Ticks
) -> None:
    first = make()
    first.open()
    ticks.value += 2.0
    feed(first, b"before")
    first.drain()
    first.stop()
    ticks.value += 60.0  # downtime is not part of the recording
    second = make()
    second.open()
    assert second.due() is True
    ticks.value += 0.5
    feed(second, b"after")
    second.step()
    frames = [frame for body in terminal(context) for frame in body["frames"]]
    assert frames == [[2000, "o", "before"], [2000, "m", "recorder restarted"], [2500, "o", "after"]]
    second.stop()


def test_a_stale_fifo_is_replaced_and_a_failed_pipe_cleans_up(make: Any, fake_tmux: FakeTmux, short_tmp: Path) -> None:
    (short_tmp / "capture").mkdir()
    stale = short_tmp / "capture" / f"{SID}.fifo"
    stale.write_text("stale")
    fake_tmux.code = 1
    rec = make()
    with pytest.raises(tmux.TmuxError):
        rec.open()
    assert not stale.exists()
    assert rec._fd == -1


def test_the_thread_records_until_stopped_and_survives_errors(
    context: ControlContext, make: Any, ticks: Ticks, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = make()
    rec.start()
    feed(rec, b"live")
    for _ in range(3000):  # up to 30 s (a loaded device), returns as soon as reached
        if rec.recorder.pending:
            break
        rec._stop.wait(0.01)
    ticks.value += 1.0
    for _ in range(3000):  # up to 30 s (a loaded device), returns as soon as reached
        if terminal(context):
            break
        rec._stop.wait(0.01)
    assert terminal(context)[0]["frames"][0][2] == "live"
    calls: list[int] = []

    def broken() -> None:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(rec, "step", broken)
    while not calls:
        rec._stop.wait(0.01)
    monkeypatch.setattr(recorder, "FLUSH_MS", 1)
    rec.stop()
    make().stop()  # never opened: nothing to do


def test_drains_are_bounded_and_stop_reads_the_rest(
    context: ControlContext, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder, "READ_CHUNK", 4)
    monkeypatch.setattr(recorder, "MAX_DRAIN", 8)
    rec = make()
    rec.open()
    feed(rec, b"0123456789abcdefghij")
    assert rec.drain() == 8
    rec.stop()
    assert "".join(frame[2] for body in terminal(context) for frame in body["frames"]) == "0123456789abcdefghij"


def test_a_timed_out_pipe_pane_is_a_failed_pipe(make: Any, fake_tmux: FakeTmux) -> None:
    def timeout(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 10)

    rec = make()
    rec.run = timeout
    with pytest.raises(tmux.TmuxError):
        rec.open()
    assert not os.path.exists(rec.fifo)


def test_stop_tolerates_a_failing_pipe_close_and_a_stuck_thread(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    rec = make()
    rec.open()

    def broken(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 10)

    rec.run = broken
    rec.stop()
    assert rec._fd == -1

    class Stuck:
        def join(self, timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    stuck = make()
    stuck.open()
    stuck._thread = Stuck()
    stuck.stop()
    assert stuck._fd >= 0
    assert "did not stop in time" in capsys.readouterr().err


def test_the_final_drain_is_bounded_too(context: ControlContext, make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "READ_CHUNK", 4)
    monkeypatch.setattr(recorder, "MAX_DRAIN", 8)
    monkeypatch.setattr(recorder, "MAX_FINAL_DRAINS", 1)
    rec = make()
    rec.open()
    feed(rec, b"0123456789abcdefghij")
    rec.stop()
    assert "".join(frame[2] for body in terminal(context) for frame in body["frames"]) == "01234567"
