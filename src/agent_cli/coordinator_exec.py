"""Bounded local process execution for coordinator script work."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import tempfile
from contextlib import contextmanager
from typing import Mapping

from .runtime import Completed

_CLEAR_GITHUB = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR")
_CHILDREN: dict[int, subprocess.Popen] = {}
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


def _stop_children() -> None:
    with _CHILD_LOCK:
        children = list(_CHILDREN.values())
    for child in children:
        _kill_group(child.pid, signal.SIGKILL)
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        with _CHILD_LOCK:
            if _CHILDREN.get(child.pid) is child:
                _CHILDREN.pop(child.pid, None)


@contextmanager
def process_scope():
    """The foreground static worker owns termination of its subprocess groups."""
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
    inherit_env: bool = True,
) -> Completed:
    """Run argv with a hard timeout; kill the process group on expiry.

    Does not use external timeout(1). Preserves stdin and cwd. Injectable
    runners in tests should mirror Completed(returncode, stdout, stderr).
    """
    if not argv:
        return Completed(127, "", "empty argv")
    if timeout <= 0:
        return Completed(127, "", "timeout must be positive")
    run_env = dict(os.environ) if inherit_env else {}
    if env is not None:
        run_env.update(env)
    if clear_ambient_github:
        for key in _CLEAR_GITHUB:
            run_env.pop(key, None)
        with tempfile.TemporaryDirectory(prefix="agent-unconfigured-gh-") as empty_profile:
            run_env["GH_CONFIG_DIR"] = empty_profile
            return _run_process(argv, timeout, cwd, stdin_text, run_env)
    return _run_process(argv, timeout, cwd, stdin_text, run_env)


def _run_process(argv, timeout, cwd, stdin_text, run_env) -> Completed:
    # Close the spawn/register signal window, including parallel reviewer starts.
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        with _CHILD_LOCK:
            try:
                proc = subprocess.Popen(
                    [sys.executable, '-I', '-S', '-c', _EXEC_UNMASKED, *argv],
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, cwd=cwd, env=run_env, start_new_session=True,
                )
            except OSError:
                return Completed(127, "", "configured command unavailable")
            _CHILDREN[proc.pid] = proc
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    try:
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
