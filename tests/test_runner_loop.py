"""Self-contained tests of the scheduled runner loop."""

from __future__ import annotations

import signal

import pytest

from agent_cli import runner_loop

pytestmark = pytest.mark.no_pg


def test_step_argvs() -> None:
    prefix = ["python", "-m", "agent_cli"]
    assert runner_loop.step_argvs(prefix, "s") == [
        ("ingest", [*prefix, "runner", "ingest", "--session", "s"]),
        ("dispatch", [*prefix, "runner", "dispatch"]),
        ("supervise", [*prefix, "runner", "supervise"]),
        ("prune", [*prefix, "runner", "prune"]),
    ]
    assert prefix == ["python", "-m", "agent_cli"]


@pytest.mark.parametrize("failure", [7, OSError(2, "missing"), OSError()])
def test_failure_continues(
    failure: int | OSError, capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    waits: list[float] = []

    def run(argv: list[str]) -> int:
        calls.append(argv)
        if argv == ["first"]:
            if isinstance(failure, OSError):
                raise failure
            return failure
        return 0

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return True

    assert runner_loop.run_loop(
        steps=[("first", ["first"]), ("last", ["last"])], interval=5,
        run=run, wait=wait, install_signals=False,
    ) == 0
    assert calls == [["first"], ["last"]]
    assert waits == [5]
    expected = (f"exited {failure}" if isinstance(failure, int) else
                f"could not start: {failure.strerror or 'OSError'}")
    assert capsys.readouterr().err == f"runner loop first {expected}\n"


def test_passes_repeat_silently(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[list[str]] = []
    waits = iter([False, True])

    def run(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    assert runner_loop.run_loop(
        steps=[("one", ["one"])], interval=1, run=run,
        wait=lambda _: next(waits), install_signals=False,
    ) == 0
    assert calls == [["one"], ["one"]]
    assert capsys.readouterr() == ("", "")


def test_stop_during_step() -> None:
    stop = runner_loop._Stop()
    finished: list[str] = []

    def run(argv: list[str]) -> int:
        stop.requested = True
        finished.append(argv[0])
        return 0

    assert runner_loop.run_loop(
        steps=[("one", ["one"]), ("two", ["two"])], interval=1,
        run=run, wait=lambda _: pytest.fail("must not wait"),
        stop=stop, install_signals=False,
    ) == 0
    assert finished == ["one"]


def test_stop_during_wait() -> None:
    stop = runner_loop._Stop()

    def wait(_: float) -> bool:
        stop.requested = True
        return False

    assert runner_loop.run_loop(
        steps=[], interval=1, run=lambda _: 0, wait=wait,
        stop=stop, install_signals=False,
    ) == 0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("raises", [False, True])
def test_handlers_restored(signum: int, raises: bool) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    stop = runner_loop._Stop()

    def run(_: list[str]) -> int:
        for s in before:
            assert signal.getsignal(s) != before[s]
        handler = signal.getsignal(signum)
        assert callable(handler)
        assert vars(stop) == {}
        handler(signum, None)
        assert vars(stop) == {"requested": True}
        if raises:
            raise RuntimeError("broken")
        return 0

    try:
        if raises:
            with pytest.raises(RuntimeError, match="broken"):
                runner_loop.run_loop(
                    steps=[("one", [])], interval=0, run=run, stop=stop,
                )
        else:
            assert runner_loop.run_loop(
                steps=[("one", []), ("two", [])], interval=0, run=run,
                stop=stop,
            ) == 0
    finally:
        assert {s: signal.getsignal(s) for s in before} == before


def test_default_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        def __init__(self, argv: list[str], *, start_new_session: bool) -> None:
            assert argv == ["step"]
            assert start_new_session is True

        def wait(self) -> int:
            return 19

    monkeypatch.setattr(runner_loop.subprocess, "Popen", Proc)
    assert runner_loop._run(["step"]) == 19
    assert runner_loop.run_loop(
        steps=[("step", ["step"])], interval=0,
        wait=lambda _: True, install_signals=False,
    ) == 0


@pytest.mark.parametrize("stop_after_slice", [False, True])
def test_wait_slices(stop_after_slice: bool) -> None:
    stop = runner_loop._Stop()
    now = 10.0
    slices: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        slices.append(seconds)
        now += seconds
        if stop_after_slice:
            stop.requested = True

    assert runner_loop._wait(
        2.5, stop, sleep=sleep, monotonic=lambda: now,
    ) is stop_after_slice
    assert slices == ([1.0] if stop_after_slice else [1.0, 1.0, 0.5])
    assert now == (11.0 if stop_after_slice else 12.5)


def test_wait_already_stopped() -> None:
    stop = runner_loop._Stop()
    stop.requested = True
    assert runner_loop._wait(
        5, stop, sleep=lambda _: pytest.fail("must not sleep"),
        monotonic=lambda: 10.0,
    ) is True


def test_default_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = runner_loop._Stop()
    waits: list[float] = []

    def monotonic() -> float:
        # Let the first zero-length wait time out, then stop before another pass.
        waits.append(0.0)
        if len(waits) == 2:
            stop.requested = True
        return 10.0

    original_wait = runner_loop._wait

    def wait(seconds: float, stopping: runner_loop._Stop) -> bool:
        assert stopping is stop
        return original_wait(
            seconds, stopping, monotonic=monotonic,
            sleep=lambda _: pytest.fail("must not sleep"),
        )

    monkeypatch.setattr(runner_loop, "_wait", wait)
    assert runner_loop.run_loop(
        steps=[], interval=0, stop=stop, install_signals=False,
    ) == 0
    assert waits == [0.0, 0.0]
    assert runner_loop.run_loop(
        steps=[], interval=0, stop=stop, install_signals=False,
    ) == 0
    assert waits == [0.0, 0.0, 0.0]
