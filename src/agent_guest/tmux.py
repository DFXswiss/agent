"""The root tmux server of the session terminals (05-runner.md section 7, ADR-016); standard library only.

One tmux server (root) on ``/run/agent-guest/tmux.sock`` (0600), one tmux session ``s-<session_id>`` of 160x48 per
session, ``remain-on-exit off``. Nobody attaches; the only writers to a pane are the prompt delivery and the
interrupt (GST-06), both through ``Tmux``.

The pane does not run ``systemd-run`` directly: a tmux command line travels through tmux's client/server messages
(limited to a few KiB), while the session command carries the initial prompt (up to 100 KiB). The control process
therefore writes the command as JSON into a root-only launch file and tmux runs this file as a script::

    python3 -I /opt/agent-guest/agent_guest/tmux.py exec /run/agent-guest/launch/<id>.json

``exec`` reads and deletes the launch file, runs the command on the pane's terminal with the file's ``env`` added to
its environment (the session token: ``systemd-run --setenv=NAME`` takes the value from there, so it never shows on
a command line), waits for it and writes its exit status to the ``status`` path named in the file (``<code>\\n``,
atomically), so the control process can tell a sandbox failure (126) from an agent exit.

The first ``new-session`` starts the server; it runs inside its own transient scope (``systemd-run --scope``) so a
restart of ``agent-guest-control`` does not kill the terminals of running sessions.

Interface:

* ``session_name(session_id) -> str`` - ``s-<session_id>``
* ``Tmux(socket=SOCKET, run=subprocess.run, scope=True)`` - ``argv(*args)``, ``new_session(name, command)``,
  ``has_session(name)``, ``kill_session(name)``, ``send_keys(name, *keys, literal=False)``, ``capture(name)``
* ``write_launch(path, command, status, env=None)`` - the launch file (0600)
* ``read_status(path) -> int | None`` - the exit status written by ``exec``
* ``main(argv=None, *, call=...) -> int`` - the ``exec`` entry point
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

SOCKET = "/run/agent-guest/tmux.sock"
COLS = 160
ROWS = 48
HELPER = os.path.abspath(__file__)
TIMEOUT = 10.0
EXIT_UNREADABLE = 125
# The tmux client gets a fixed, minimal environment; nothing of the control process leaks into the pane.
CLIENT_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
_NAME_RE = re.compile(r"s-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class TmuxError(RuntimeError):
    """A tmux command failed."""


def session_name(session_id: str) -> str:
    name = f"s-{session_id}"
    if not _NAME_RE.fullmatch(name):
        raise TmuxError(f"invalid session id {session_id!r}")
    return name


class Tmux:
    def __init__(
        self,
        socket: str = SOCKET,
        run: Runner = subprocess.run,
        *,
        scope: bool = True,
        binary: str = "tmux",
    ) -> None:
        self.socket = socket
        self._run = run
        self.scope = scope
        self.binary = binary

    def argv(self, *args: str) -> list[str]:
        return [self.binary, "-S", self.socket, *args]

    def _call(self, argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self._run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
            env=CLIENT_ENV,
            stdin=subprocess.DEVNULL,
        )
        if check and result.returncode != 0:
            raise TmuxError(f"tmux failed with status {result.returncode}")
        return result

    def new_session(self, name: str, command: Sequence[str]) -> None:
        """Detached session ``name`` of 160x48 running ``command`` (no shell)."""
        if not _NAME_RE.fullmatch(name) or not command:
            raise TmuxError("invalid session")
        argv = self.argv("new-session", "-d", "-s", name, "-x", str(COLS), "-y", str(ROWS), "--", *command)
        if self.scope:
            # Unique per call: the first scope keeps the tmux server for its whole life (a resume reuses ``name``).
            unit = f"agent-tmux-{name[2:]}-{os.urandom(4).hex()}"
            argv = ["systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}", "--", *argv]
        self._call(argv)
        self._call(self.argv("set-option", "-t", name, "remain-on-exit", "off"), check=False)

    def has_session(self, name: str) -> bool:
        return self._call(self.argv("has-session", "-t", f"={name}"), check=False).returncode == 0

    def kill_session(self, name: str) -> bool:
        return self._call(self.argv("kill-session", "-t", f"={name}"), check=False).returncode == 0

    def send_keys(self, name: str, *keys: str, literal: bool = False) -> None:
        """``send-keys`` to the pane of ``name``; ``literal`` types the text as is (``-l --``)."""
        flags = ["-l", "--"] if literal else ["--"]
        self._call(self.argv("send-keys", "-t", f"={name}:", *flags, *keys))

    def capture(self, name: str) -> str:
        return self._call(self.argv("capture-pane", "-p", "-t", f"={name}:")).stdout


def write_launch(path: str, command: Sequence[str], status: str, env: Mapping[str, str] | None = None) -> None:
    """The launch file of ``exec`` (root 0600, atomic); ``status`` receives the exit code; ``env`` is added to the
    command's environment (secrets that must not appear on a command line)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".launch-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"argv": list(command), "status": status, "env": dict(env or {})}, handle)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def read_status(path: str) -> int | None:
    """Exit status written by ``exec``; ``None`` while the command runs (or when the file is unreadable)."""
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if re.fullmatch(r"-?[0-9]{1,4}", text) else None


def _write_status(path: str, code: int) -> None:
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="ascii") as handle:
        handle.write(f"{code}\n")
    os.replace(temporary, path)


def _load(path: str) -> tuple[list[str], str, dict[str, str]]:
    with open(path, encoding="utf-8") as handle:
        data: Any = json.load(handle)
    os.unlink(path)
    command, status, env = data["argv"], data["status"], data["env"]
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("argv")
    if not isinstance(status, str) or not status.startswith("/"):
        raise ValueError("status")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError("env")
    return command, status, env


def _call(command: list[str], env: dict[str, str]) -> int:
    return subprocess.call(command, env=env)


def _hangup(signum: int, frame: object) -> None:
    """The pane was closed: the command gets the hangup from its terminal, ``exec`` keeps waiting for its status."""


def main(argv: Sequence[str] | None = None, *, call: Callable[[list[str], dict[str, str]], int] = _call) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "exec":
        sys.stderr.write("usage: tmux.py exec <launch file>\n")
        return 2
    try:
        command, status, env = _load(args[1])
    except (OSError, ValueError, KeyError, TypeError):
        sys.stderr.write("tmux.py: launch file unreadable\n")
        return EXIT_UNREADABLE
    signal.signal(signal.SIGHUP, _hangup)
    code = call(command, {**os.environ, **env})
    _write_status(status, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
