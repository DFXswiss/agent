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

from .runtime import Completed

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
# The wrapper stays alive if its parent is stopped. It starts the command in
# its own session so the recorded pid is the process-group leader, then writes
# that pid and, after the command exits, its return code.
_SURVIVE_WRAPPER = (
    'import json,os,signal,subprocess,sys\n'
    'signal.pthread_sigmask(signal.SIG_SETMASK, [])\n'
    'status_path, pid_path = sys.argv[1], sys.argv[2]\n'
    'cmd = sys.argv[3:]\n'
    'try:\n'
    ' proc = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.PIPE)\n'
    'except OSError:\n'
    ' sys.stderr.write("configured command unavailable\\n")\n'
    ' sys.exit(127)\n'
    'tmp = pid_path + ".tmp"\n'
    'with open(tmp, "w", encoding="utf-8") as handle:\n'
    ' handle.write(str(proc.pid))\n'
    ' handle.flush()\n'
    ' os.fsync(handle.fileno())\n'
    'os.replace(tmp, pid_path)\n'
    'try:\n'
    ' data = sys.stdin.buffer.read()\n'
    ' if data:\n'
    '  proc.stdin.write(data)\n'
    ' proc.stdin.close()\n'
    'except BrokenPipeError:\n'
    ' try:\n'
    '  proc.stdin.close()\n'
    ' except OSError:\n'
    '  pass\n'
    'rc = proc.wait()\n'
    'payload = json.dumps({"returncode": int(rc)})\n'
    'tmp = status_path + ".tmp"\n'
    'with open(tmp, "w", encoding="utf-8") as handle:\n'
    ' handle.write(payload)\n'
    ' handle.flush()\n'
    ' os.fsync(handle.fileno())\n'
    'os.replace(tmp, status_path)\n'
    'code = rc if 0 <= rc <= 255 else (128 + (-rc) if rc < 0 else 1)\n'
    'sys.exit(code if 0 <= code <= 255 else 1)\n'
)


@dataclass
class _Tracked:
    proc: subprocess.Popen
    survive_stop: bool
    command_pid: int | None = None


_CHILDREN: dict[int, _Tracked] = {}


def _stop_children() -> None:
    """Stop children that do not outlive this coordinator.

    A model lane started with ``survive_stop`` keeps its process group. Check
    and test commands stay in this set and are still killed.
    """
    with _CHILD_LOCK:
        children = [child for child in _CHILDREN.values() if not child.survive_stop]
    for child in children:
        _kill_group(child.proc.pid, signal.SIGKILL)
        if child.command_pid is not None:
            _kill_group(child.command_pid, signal.SIGKILL)
    for child in children:
        try:
            child.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        with _CHILD_LOCK:
            if _CHILDREN.get(child.proc.pid) is child:
                _CHILDREN.pop(child.proc.pid, None)


@contextmanager
def process_scope():
    """The foreground static worker owns termination of its attached subprocess groups.

    SIGTERM and SIGINT stop check and test commands. A model lane marked
    ``survive_stop`` is left running so the next coordinator can adopt it.
    """
    if threading.current_thread() is not threading.main_thread():
        # Embedded thread callers retain their process's signal ownership.
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    def stop(signum, _frame):
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
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        return Completed(124, stdout or "", (stderr or "") + "\ntimeout")
    except BaseException:
        _kill_group(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
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
                try:
                    pid = int(text)
                except ValueError:
                    return None
                if pid > 0:
                    return pid
                return None
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

    The recorded pid is the command, not this wrapper. Stdout, stderr, that pid
    and the return code are files the next coordinator can read. A stop signal
    leaves both processes running only after ``on_start`` has returned, which is
    when the capture record is durable. Timeout and any other failure still kill
    them. Missing capture directories are created here.
    """
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
                    [sys.executable, "-c", _SURVIVE_WRAPPER, status_path, pid_path, *argv],
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    cwd=cwd,
                    env=run_env,
                    start_new_session=True,
                )
            except OSError:
                return Completed(127, "", "configured command unavailable")
            _CHILDREN[proc.pid] = _Tracked(proc, survive_stop=True)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        stdout_handle.close()
        stderr_handle.close()
    assert proc is not None
    abandoned = False
    # True only after on_start returns. Until then a stop must not leave the
    # command running: the capture record is written inside that callback.
    recorded = False
    command_pid: int | None = None
    try:
        command_pid = _await_command_pid(pid_path, proc)
        if command_pid is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
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
            # Hold SIGTERM and SIGINT across the start record. The recorder's
            # ps is a child this handler would otherwise kill before the record
            # exists, and the command would keep running unmarked.
            held = signal.pthread_sigmask(
                signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT}
            )
            try:
                try:
                    on_start(command_pid)
                except Exception:  # noqa: BLE001 — failed bookkeeping must not leave the lane running
                    _signal_command(command_pid, proc, signal.SIGKILL)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return _completed_from_capture(
                        stdout_path, stderr_path, status_path, forced=None
                    )
                recorded = True
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, held)
        proc.communicate(
            input=None if stdin_text is None else stdin_text.encode("utf-8"),
            timeout=timeout,
        )
        return _completed_from_capture(
            stdout_path, stderr_path, status_path, forced=None
        )
    except subprocess.TimeoutExpired:
        _signal_command(command_pid, proc, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _signal_command(command_pid, proc, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return _completed_from_capture(
            stdout_path, stderr_path, status_path, forced=124
        )
    except SystemExit:
        abandoned = recorded
        raise
    except BaseException:
        _signal_command(command_pid, proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        if abandoned:
            with _CHILD_LOCK:
                _CHILDREN.pop(proc.pid, None)
        else:
            if command_pid is not None:
                _kill_group(command_pid, signal.SIGKILL)
            _kill_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with _CHILD_LOCK:
                _CHILDREN.pop(proc.pid, None)


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
