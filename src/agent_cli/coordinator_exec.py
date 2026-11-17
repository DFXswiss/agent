"""Bounded local process execution for coordinator script work."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .daemon import REAP_AFTER_SIGKILL_S, _terminate
from .runtime import Completed

# Seconds the pipe runner waits after SIGTERM before SIGKILL. A surviving lane uses it too.
_SIGTERM_GRACE_S = 2

_CLEAR_GITHUB = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR")
_CHILD_LOCK = threading.RLock()
_EXEC_UNMASKED = (
    'import os,signal,sys\n'
    'signal.pthread_sigmask(signal.SIG_SETMASK, [])\n'
    'try:\n'
    ' os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n'
    'except OSError:\n'
    ' sys.stderr.write("configured command unavailable\\n")\n'
    ' sys.exit(127)\n'
)
# Stops before exec. The recorded pid is this process, and it is still alive
# when the parent reads the start time. Continuing it execs the real command.
_SURVIVE_STARTER = (
    'import os,signal,sys\n'
    'os.kill(os.getpid(), signal.SIGSTOP)\n'
    'try:\n'
    ' os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n'
    'except OSError:\n'
    ' sys.stderr.write("configured command unavailable\\n")\n'
    ' sys.exit(127)\n'
)
# The wrapper stays alive if its parent is stopped. It starts the command in
# its own session so the recorded pid is the process-group leader, holds that
# process stopped until the parent has read the start time, then writes the
# pid and, after the command exits, its return code. SIGTERM reaps that command
# even when the pid file does not exist yet: SIGKILL would skip this handler
# and leave the stopped starter in its session.
_SURVIVE_WRAPPER = (
    'import json,os,signal,subprocess,sys\n'
    'signal.pthread_sigmask(signal.SIG_SETMASK, [])\n'
    'status_path, pid_path, starter = sys.argv[1], sys.argv[2], sys.argv[3]\n'
    'cmd = sys.argv[4:]\n'
    'child = []\n'
    'def publish(path, text):\n'
    ' tmp = path + ".tmp"\n'
    ' with open(tmp, "w", encoding="utf-8") as handle:\n'
    '  handle.write(text)\n'
    '  handle.flush()\n'
    '  os.fsync(handle.fileno())\n'
    ' os.replace(tmp, path)\n'
    'def finish(rc):\n'
    ' publish(status_path, json.dumps({"returncode": int(rc)}))\n'
    ' code = rc if 0 <= rc <= 255 else (128 + (-rc) if rc < 0 else 1)\n'
    ' sys.exit(code if 0 <= code <= 255 else 1)\n'
    'def reap(signum, _frame):\n'
    ' if child:\n'
    '  try:\n'
    '   os.killpg(child[0], signal.SIGKILL)\n'
    '  except OSError:\n'
    '   try:\n'
    '    os.kill(child[0], signal.SIGKILL)\n'
    '   except OSError:\n'
    '    pass\n'
    ' code = 128 + signum\n'
    ' sys.exit(code if 0 <= code <= 255 else 1)\n'
    'signal.signal(signal.SIGTERM, reap)\n'
    'signal.signal(signal.SIGINT, reap)\n'
    'held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})\n'
    'try:\n'
    ' try:\n'
    '  proc = subprocess.Popen([sys.executable, "-c", starter, *cmd], start_new_session=True, stdin=subprocess.PIPE)\n'
    ' except OSError:\n'
    '  sys.stderr.write("configured command unavailable\\n")\n'
    '  sys.exit(127)\n'
    ' child.append(proc.pid)\n'
    'finally:\n'
    ' signal.pthread_sigmask(signal.SIG_SETMASK, held)\n'
    'while True:\n'
    ' try:\n'
    '  _pid, status = os.waitpid(proc.pid, os.WUNTRACED)\n'
    '  break\n'
    ' except InterruptedError:\n'
    '  continue\n'
    'if not os.WIFSTOPPED(status):\n'
    ' if os.WIFEXITED(status):\n'
    '  rc = os.WEXITSTATUS(status)\n'
    ' elif os.WIFSIGNALED(status):\n'
    '  rc = -os.WTERMSIG(status)\n'
    ' else:\n'
    '  rc = 1\n'
    ' proc.returncode = rc\n'
    ' publish(pid_path, str(proc.pid))\n'
    ' finish(rc)\n'
    'publish(pid_path, str(proc.pid))\n'
    'data = sys.stdin.buffer.read()\n'
    'try:\n'
    ' os.kill(proc.pid, signal.SIGCONT)\n'
    'except OSError:\n'
    ' pass\n'
    'try:\n'
    ' if data:\n'
    '  proc.stdin.write(data)\n'
    ' proc.stdin.close()\n'
    'except BrokenPipeError:\n'
    ' try:\n'
    '  proc.stdin.close()\n'
    ' except OSError:\n'
    '  pass\n'
    'while True:\n'
    ' try:\n'
    '  _pid, status = os.waitpid(proc.pid, 0)\n'
    '  break\n'
    ' except InterruptedError:\n'
    '  continue\n'
    'if os.WIFEXITED(status):\n'
    ' rc = os.WEXITSTATUS(status)\n'
    'elif os.WIFSIGNALED(status):\n'
    ' rc = -os.WTERMSIG(status)\n'
    'else:\n'
    ' rc = 1\n'
    'proc.returncode = rc\n'
    'finish(rc)\n'
)


@dataclass
class _Tracked:
    proc: subprocess.Popen
    survive_stop: bool
    command_pid: int | None = None
    pid_path: str | None = None
    # True only after on_start has returned. Until then the capture record is
    # not durable and a stop must end the command, not leave it running.
    durable: bool = False
    doomed: bool = False


_CHILDREN: dict[int, _Tracked] = {}
# A generation, not a sticky flag: a stop the caller already caught must not
# abandon the next lane in this process. Each surviving wait snapshots the
# generation before it spawns and treats a later value as a coordinator stop.
_STOP_LOCK = threading.Lock()
_STOP_GENERATION = 0
_STOP_SIGNAL = signal.SIGTERM


def _stop_generation() -> int:
    with _STOP_LOCK:
        return _STOP_GENERATION


def _request_coordinator_stop(signum: int) -> None:
    """Record one SIGTERM or SIGINT for every surviving wait in this process."""
    global _STOP_GENERATION, _STOP_SIGNAL
    with _STOP_LOCK:
        _STOP_GENERATION += 1
        _STOP_SIGNAL = signum


def _stop_signal_since(generation: int) -> int | None:
    """The signal requested after ``generation``, or None when none was."""
    with _STOP_LOCK:
        if _STOP_GENERATION == generation:
            return None
        return _STOP_SIGNAL


def _stop_children() -> None:
    """Stop children that must not outlive this coordinator.

    Check and test commands are always killed. A model lane keeps its process
    group only after its capture record is durable. Before that, the command
    pid may exist only in the pid file, and this handler kills that group too.
    A starter that has not published its pid is reaped by the wrapper on
    SIGTERM. SIGKILL would skip that handler and leave the starter stopped.
    """
    with _CHILD_LOCK:
        ephemeral: list[_Tracked] = []
        unsettled: list[_Tracked] = []
        for child in _CHILDREN.values():
            if not child.survive_stop:
                ephemeral.append(child)
            elif not child.durable:
                child.doomed = True
                unsettled.append(child)
    for child in ephemeral:
        _kill_group(child.proc.pid, signal.SIGKILL)
        if child.command_pid is not None:
            _kill_group(child.command_pid, signal.SIGKILL)
    for child in unsettled:
        _kill_published_command(child.command_pid, child.pid_path, signal.SIGKILL)
        _stop_survive_wrapper(child.proc)
    for child in ephemeral:
        try:
            child.proc.wait(timeout=REAP_AFTER_SIGKILL_S)
        except subprocess.TimeoutExpired:
            _kill_group(child.proc.pid, signal.SIGKILL)
            try:
                child.proc.wait(timeout=REAP_AFTER_SIGKILL_S)
            except subprocess.TimeoutExpired:
                pass
    for child in (*ephemeral, *unsettled):
        with _CHILD_LOCK:
            if _CHILDREN.get(child.proc.pid) is child:
                _CHILDREN.pop(child.proc.pid, None)


@contextmanager
def process_scope():
    """The foreground static worker owns termination of its attached subprocess groups.

    SIGTERM and SIGINT stop check and test commands. A model lane marked
    ``survive_stop`` is left running once its capture record is durable, so
    the next coordinator can adopt it. The handler records a process-wide stop.
    A lane blocked in another thread polls that stop and returns without
    killing its process group. A stop before the record is durable kills the
    lane, including a command pid that is only in the pid file and a starter
    the wrapper has not published yet.
    """
    if threading.current_thread() is not threading.main_thread():
        # Embedded thread callers retain their process's signal ownership.
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    def stop(signum, _frame):
        # Workers blocked in proc.wait do not run this handler. The flag is
        # what lets them abandon a durable lane before forget() removes it.
        _request_coordinator_stop(signum)
        _stop_children()
        raise SystemExit(128 + signum)
    try:
        for sig in previous:
            signal.signal(sig, stop)
        yield
    finally:
        _stop_children()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_bounded(
    argv: list[str],
    *,
    timeout: int,
    cwd: str | None = None,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
    clear_ambient_github: bool = True,
    on_start: Callable[[int], None] | None = None,
    survive_stop: bool = False,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    status_path: str | None = None,
    pid_path: str | None = None,
) -> Completed:
    """Run argv with a hard timeout; kill the process group on expiry.

    When ``on_start`` is given, the timeout starts after it returns, so the
    child can run for the duration of that callback plus ``timeout``.

    ``survive_stop`` leaves the command running when this process receives
    SIGTERM or SIGINT. Stdout, stderr, the command pid and the return code are
    written to the given paths while the command runs. The caller supplies
    every path; a missing path is an error.

    Does not use external timeout(1). Preserves stdin and cwd. Injectable
    runners in tests should mirror Completed(returncode, stdout, stderr).
    """
    if not argv:
        return Completed(127, "", "empty argv")
    if timeout <= 0:
        return Completed(127, "", "timeout must be positive")
    if survive_stop and (
        not stdout_path or not stderr_path or not status_path or not pid_path
    ):
        return Completed(127, "", "lane capture paths are required")
    run_env = dict(os.environ)
    if env is not None:
        run_env.update(env)
    if clear_ambient_github:
        for key in _CLEAR_GITHUB:
            run_env.pop(key, None)
        if survive_stop:
            profile = Path(str(status_path) + ".gh")
            profile.mkdir(parents=True, exist_ok=True)
            run_env["GH_CONFIG_DIR"] = str(profile)
            return _run_process(
                argv, timeout, cwd, stdin_text, run_env, on_start,
                survive_stop=True,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                status_path=status_path,
                pid_path=pid_path,
            )
        with tempfile.TemporaryDirectory(prefix="agent-unconfigured-gh-") as empty_profile:
            run_env["GH_CONFIG_DIR"] = empty_profile
            return _run_process(argv, timeout, cwd, stdin_text, run_env, on_start)
    return _run_process(
        argv, timeout, cwd, stdin_text, run_env, on_start,
        survive_stop=survive_stop,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        status_path=status_path,
        pid_path=pid_path,
    )


def _run_process(
    argv,
    timeout,
    cwd,
    stdin_text,
    run_env,
    on_start: Callable[[int], None] | None = None,
    *,
    survive_stop: bool = False,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    status_path: str | None = None,
    pid_path: str | None = None,
) -> Completed:
    if survive_stop:
        return _run_surviving_process(
            argv,
            timeout,
            cwd,
            stdin_text,
            run_env,
            on_start,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            status_path=status_path,
            pid_path=pid_path,
        )
    # Close the spawn/register signal window, including parallel reviewer starts.
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        with _CHILD_LOCK:
            try:
                proc = subprocess.Popen(
                    [sys.executable, '-c', _EXEC_UNMASKED, *argv],
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, cwd=cwd, env=run_env, start_new_session=True,
                )
            except OSError:
                return Completed(127, "", "configured command unavailable")
            _CHILDREN[proc.pid] = _Tracked(proc, survive_stop=False)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    try:
        if on_start is not None:
            try:
                on_start(proc.pid)
            except Exception:  # noqa: BLE001 — bookkeeping must not break the run
                pass
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
        return Completed(int(proc.returncode or 0), stdout or "", stderr or "")
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=_SIGTERM_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        return Completed(124, stdout or "", (stderr or "") + "\ntimeout")
    except BaseException:
        _kill_group(proc.pid, signal.SIGKILL)
        proc.wait(timeout=REAP_AFTER_SIGKILL_S)
        raise
    finally:
        # A child must not leave descendants behind after returning a result.
        _kill_group(proc.pid, signal.SIGKILL)
        with _CHILD_LOCK:
            _CHILDREN.pop(proc.pid, None)


def read_capture_returncode(path: str) -> int | None:
    """Return code written by a surviving lane, or None when it is not usable.

    A missing file, unreadable JSON, or a boolean is not a return code. Callers
    must not treat that as success.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rc = payload.get("returncode")
    if isinstance(rc, bool) or not isinstance(rc, int):
        return None
    return rc


def _parse_positive_pid(text: str) -> int | None:
    """A positive integer pid, or None when the text is not one."""
    try:
        pid = int(text)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return pid


def _read_published_pid(pid_path: str | None) -> int | None:
    """Command pid the survive wrapper has published, or None when it has not.

    A missing, empty, or unusable file is None. ``_await_command_pid`` still
    tells a missing file from a present unusable one and stops waiting only
    for the latter.
    """
    if pid_path is None or pid_path == "":
        return None
    try:
        text = Path(pid_path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not text:
        return None
    return _parse_positive_pid(text)


def _kill_published_command(
    command_pid: int | None,
    pid_path: str | None,
    sig: signal.Signals,
) -> None:
    """Kill the command group. The pid file counts when the local pid is empty."""
    published = command_pid
    if published is None:
        published = _read_published_pid(pid_path)
    if published is not None:
        _kill_group(published, sig)


def _await_command_pid(pid_path: str, proc: subprocess.Popen) -> int | None:
    """Command pid published by the wrapper, or None when the wrapper ends first."""
    path = Path(pid_path)
    while True:
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                text = ""
            if text:
                return _parse_positive_pid(text)
        if proc.poll() is not None:
            return None
        time.sleep(0.05)


def _captured_streams(stdout_path: str, stderr_path: str) -> tuple[str, str, str | None]:
    """Durable lane output, or ``(empty, empty, diagnostic)`` when it cannot be read."""
    from .lane import output_read_failure

    try:
        stdout = Path(stdout_path).read_text(encoding="utf-8")
        stderr = Path(stderr_path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return "", "", output_read_failure(exc)
    return stdout, stderr, None


def _completed_from_capture(
    stdout_path: str,
    stderr_path: str,
    status_path: str,
    *,
    forced: int | None,
) -> Completed:
    stdout, stderr, error = _captured_streams(stdout_path, stderr_path)
    if error is not None:
        stdout, stderr = "", error
    if forced == 124:
        return Completed(124, stdout, stderr + "\ntimeout")
    if error is not None:
        rc = read_capture_returncode(status_path)
        if rc is None:
            return Completed(127, "", error)
        return Completed(rc, "", error)
    rc = read_capture_returncode(status_path)
    if rc is None:
        return Completed(127, stdout, stderr + "\nlane status is missing")
    return Completed(rc, stdout, stderr)


def _signal_command(command_pid: int | None, proc: subprocess.Popen, sig: signal.Signals) -> None:
    if command_pid is not None:
        _kill_group(command_pid, sig)
        return
    _kill_group(proc.pid, sig)


def _release_survive_stdin(proc: subprocess.Popen, stdin_text: str | None) -> None:
    """Close the wrapper stdin so it continues the stopped command.

    The wrapper reads this only after the command has stopped. Closing it is
    the signal that ``on_start`` has finished reading the start time.
    """
    if proc.stdin is None:
        return
    payload = None if stdin_text is None else stdin_text.encode("utf-8")
    try:
        if payload:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except BrokenPipeError:
        pass
    try:
        proc.stdin.close()
    except BrokenPipeError:
        pass


def _run_surviving_process(
    argv,
    timeout,
    cwd,
    stdin_text,
    run_env,
    on_start: Callable[[int], None] | None,
    *,
    stdout_path: str | None,
    stderr_path: str | None,
    status_path: str | None,
    pid_path: str | None,
) -> Completed:
    """Run a model lane whose process group outlives SIGTERM and SIGINT.

    The recorded pid is the command, not this wrapper. The command stops before
    exec, so ``on_start`` can read its start time before the command runs or is
    reaped. Stdout, stderr, that pid and the return code are files the next
    coordinator can read. A stop signal leaves both processes running only after
    ``on_start`` has returned, which is when the capture record is durable.
    The wait polls the process-wide stop flag because a gate or PR-review lane
    runs on a worker thread, where the signal handler never raises. After a
    durable record that stop returns ``SystemExit`` and does not kill the
    group, so the caller must not forget the capture. Until the record is
    durable the stop path kills the command, including a pid that is only in
    the pid file and a starter the wrapper has not published yet.
    Timeout and any other failure still kill them. Missing capture directories
    are created here.
    """
    stop_generation = _stop_generation()
    if not stdout_path or not stderr_path or not status_path or not pid_path:
        return Completed(127, "", "lane capture paths are required")
    for capture_path in (stdout_path, stderr_path, status_path, pid_path):
        Path(capture_path).parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = open(stdout_path, "wb")
    stderr_handle = open(stderr_path, "wb")
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    proc: subprocess.Popen | None = None
    try:
        with _CHILD_LOCK:
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _SURVIVE_WRAPPER,
                        status_path,
                        pid_path,
                        _SURVIVE_STARTER,
                        *argv,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    cwd=cwd,
                    env=run_env,
                    start_new_session=True,
                )
            except OSError:
                return Completed(127, "", "configured command unavailable")
            _CHILDREN[proc.pid] = _Tracked(
                proc, survive_stop=True, pid_path=pid_path
            )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        stdout_handle.close()
        stderr_handle.close()
    assert proc is not None
    abandoned = False
    # True only after on_start returns and no stop has claimed the lane.
    # Until then a stop must not leave the command running.
    recorded = False
    command_pid: int | None = None
    try:
        command_pid = _await_command_pid(pid_path, proc)
        if command_pid is None:
            try:
                proc.wait(timeout=REAP_AFTER_SIGKILL_S)
            except subprocess.TimeoutExpired:
                _stop_survive_wrapper(proc)
            stdout, stderr, error = _captured_streams(stdout_path, stderr_path)
            if error is not None:
                return Completed(127, "", error)
            detail = stderr.strip() or "configured command unavailable"
            return Completed(127, stdout, detail)
        with _CHILD_LOCK:
            tracked = _CHILDREN.get(proc.pid)
            if tracked is not None:
                tracked.command_pid = command_pid
        if on_start is not None:
            # Block SIGTERM and SIGINT on this thread while the start record is
            # written. The main thread's handler still runs and kills this lane
            # until it is marked durable below.
            held = signal.pthread_sigmask(
                signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT}
            )
            try:
                try:
                    on_start(command_pid)
                except Exception:  # noqa: BLE001 — failed bookkeeping must not leave the lane running
                    _signal_command(command_pid, proc, signal.SIGKILL)
                    try:
                        proc.wait(timeout=REAP_AFTER_SIGKILL_S)
                    except subprocess.TimeoutExpired:
                        pass
                    return _completed_from_capture(
                        stdout_path, stderr_path, status_path, forced=None
                    )
                with _CHILD_LOCK:
                    tracked = _CHILDREN.get(proc.pid)
                    # A stop that already claimed this lane must not be undone
                    # by marking the record durable after the kill. A stop that
                    # arrived before this child was registered is the same claim.
                    if (
                        tracked is not None
                        and not tracked.doomed
                        and _stop_signal_since(stop_generation) is None
                    ):
                        tracked.durable = True
                        recorded = True
                    elif tracked is not None:
                        tracked.doomed = True
                if recorded:
                    # Unblock the wrapper only after the start time is durable,
                    # and before pending stop signals are unmasked.
                    _release_survive_stdin(proc, stdin_text)
                else:
                    return _completed_from_capture(
                        stdout_path, stderr_path, status_path, forced=None
                    )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, held)
        else:
            _release_survive_stdin(proc, stdin_text)
        # proc.wait(timeout=timeout) would hide a coordinator stop from this
        # thread until the lane exited, and forget() would then drop the capture.
        deadline = time.monotonic() + timeout
        while True:
            signum = _stop_signal_since(stop_generation)
            if signum is not None and recorded:
                raise SystemExit(128 + signum)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            try:
                proc.wait(timeout=0.05 if remaining > 0.05 else remaining)
            except subprocess.TimeoutExpired:
                continue
            signum = _stop_signal_since(stop_generation)
            if signum is not None and recorded:
                raise SystemExit(128 + signum)
            break
        return _completed_from_capture(
            stdout_path, stderr_path, status_path, forced=None
        )
    except subprocess.TimeoutExpired:
        _signal_command(command_pid, proc, signal.SIGTERM)
        try:
            proc.wait(timeout=_SIGTERM_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal_command(command_pid, proc, signal.SIGKILL)
            try:
                proc.wait(timeout=REAP_AFTER_SIGKILL_S)
            except subprocess.TimeoutExpired:
                pass
        return _completed_from_capture(
            stdout_path, stderr_path, status_path, forced=124
        )
    except SystemExit:
        abandoned = recorded
        raise
    except BaseException:
        _kill_published_command(command_pid, pid_path, signal.SIGKILL)
        _stop_survive_wrapper(proc)
        raise
    finally:
        if abandoned:
            with _CHILD_LOCK:
                _CHILDREN.pop(proc.pid, None)
        else:
            _kill_published_command(command_pid, pid_path, signal.SIGKILL)
            _stop_survive_wrapper(proc)
            with _CHILD_LOCK:
                _CHILDREN.pop(proc.pid, None)


def _stop_survive_wrapper(proc: subprocess.Popen) -> None:
    """SIGTERM the survive wrapper so it reaps an unpublished starter.

    The starter is in its own session. SIGKILL of the wrapper does not run the
    wrapper's handler and does not reach that session. SIGKILL follows only
    when the wrapper ignores SIGTERM. That grace is `_terminate`.
    """
    _terminate(proc)


def _kill_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def kill_process_group(pid: int) -> None:
    """Kill the process group led by ``pid``; a missing group is not an error."""
    _kill_group(pid, signal.SIGKILL)
