"""What the orchestrator does on one device: processes, relays, egress sampling, one-shot commands, files.

A `Device` lives in the orchestrator process for the local device and in `python -m orchestrator.node` on the
runner device of T2 (reached over SSH, `node.py`). Every argument and result is JSON data, so both are called alike.
Processes run in their own session with their output in `<run_dir>/logs/<name>.log` (a restarted process starts a new
log, the earlier one is kept as `<name>.<n>.log`); `shutdown()` ends all of them
(SIGTERM, then SIGKILL after the timeout), stops the relays and the sampler.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.egress import EgressLog, read_resolvers
from orchestrator.relay import Relay

STOP_TIMEOUT_S = 10.0
POLL_S = 0.1
TAIL_LINES = 20
# Methods a remote caller may invoke (node.py); everything else is internal.
METHODS = frozenset(
    {
        "spawn",
        "wait_line",
        "wait_exit",
        "alive",
        "stop",
        "kill",
        "log_tail",
        "processes",
        "run",
        "http",
        "busy_ports",
        "relay_start",
        "relay_cut",
        "relay_restore",
        "relays",
        "egress_start",
        "egress_records",
        "read_text",
        "archive",
        "abandon_cleanup",
        "shutdown",
    }
)


def expand(values: list[str]) -> list[str]:
    """Paths sent as `~` or `~/...` by the hub device name this device's home (T2: the homes differ)."""
    return [os.path.expanduser(value) if value == "~" or value.startswith("~/") else value for value in values]


def expand_env(env: dict[str, str] | None) -> dict[str, str] | None:
    return None if env is None else dict(zip(env, expand(list(env.values())), strict=True))


class DeviceError(Exception):
    """A device operation failed; the message names the process or command, never secret values."""


TIMED_OUT = 124
NOT_FOUND = 127


def run_command(
    argv: Sequence[str],
    env: Mapping[str, str] | None,
    cwd: str | None,
    timeout: float,
    track: Callable[[int, bool], None] | None = None,
) -> dict[str, Any]:
    """Run a command to its end; `track(pid, running)` lets the egress sampler watch it meanwhile.

    A missing program is exit code 127, a timeout 124 (the process is killed), like the shell's `timeout`."""
    try:
        proc = subprocess.Popen(
            list(argv),
            env=dict(env) if env is not None else None,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return {"rc": NOT_FOUND, "stdout": "", "stderr": f"{argv[0]}: {type(exc).__name__}"}
    if track is not None:
        track(proc.pid, True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return {"rc": TIMED_OUT, "stdout": out, "stderr": f"{argv[0]}: TimeoutExpired"}
    finally:
        if track is not None:
            track(proc.pid, False)
    return {"rc": proc.returncode, "stdout": out, "stderr": err}


Runner = Callable[..., dict[str, Any]]


@dataclass
class _Proc:
    popen: subprocess.Popen[bytes]
    log: Path


class Device:
    def __init__(self, run_dir: Path, *, runner: Runner = run_command, clock: Callable[[], float] = time.monotonic):
        self.run_dir = run_dir.expanduser()
        self.logs = self.run_dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.clock = clock
        self._procs: dict[str, _Proc] = {}
        self._relays: dict[str, Relay] = {}
        self._running: set[int] = set()
        self._egress_started = False
        self._abandon: tuple[str, list[str]] | None = None
        self.egress = EgressLog(self.run_dir / "egress.jsonl", self._sample_command)

    def _sample_command(self, argv: list[str]) -> tuple[int, str]:
        done = self.runner(argv, None, None, 30)
        return int(done["rc"]), str(done["stdout"])

    def _track(self, pid: int, running: bool) -> None:
        if running:
            self._running.add(pid)
        else:
            self._running.discard(pid)

    # ------------------------------------------------------------------ processes

    def spawn(self, name: str, argv: list[str], env: dict[str, str], cwd: str | None = None) -> int:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            raise DeviceError(f"invalid process name {name!r}")
        if name in self._procs and self._procs[name].popen.poll() is None:
            raise DeviceError(f"process {name} is already running")
        log = self.logs / f"{name}.log"
        if name in self._procs and log.exists():
            # A restart (runner stop/start) gets a fresh log, so waiting for its ready line sees only the new
            # process; the earlier output stays as `<name>.<n>.log`.
            number = 1
            while (self.logs / f"{name}.{number}.log").exists():
                number += 1
            log.rename(self.logs / f"{name}.{number}.log")
        with log.open("ab") as out:
            try:
                popen = subprocess.Popen(
                    expand(argv),
                    env=expand_env(env),
                    cwd=os.path.expanduser(cwd) if cwd else None,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise DeviceError(f"{name}: cannot start {Path(argv[0]).name} ({type(exc).__name__})") from None
        self._procs[name] = _Proc(popen, log)
        return popen.pid

    def _proc(self, name: str) -> _Proc:
        try:
            return self._procs[name]
        except KeyError:
            raise DeviceError(f"unknown process {name}") from None

    def log_tail(self, name: str, lines: int = TAIL_LINES) -> str:
        text = self._proc(name).log.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def wait_line(self, name: str, pattern: str, timeout: float) -> str:
        """The first log line of `name` matching `pattern`; fails when the process exits or the time is over."""
        proc = self._proc(name)
        regex = re.compile(pattern)
        deadline = self.clock() + timeout
        while True:
            for line in proc.log.read_text(encoding="utf-8", errors="replace").splitlines():
                if regex.search(line):
                    return line
            if proc.popen.poll() is not None:
                raise DeviceError(f"{name} exited with {proc.popen.returncode}:\n{self.log_tail(name)}")
            if self.clock() >= deadline:
                raise DeviceError(
                    f"{name}: no line matching {pattern!r} within {timeout:.0f} s:\n{self.log_tail(name)}"
                )
            time.sleep(POLL_S)

    def wait_exit(self, name: str, timeout: float) -> int:
        """Wait for the process to end on its own; stops it after `timeout` and fails."""
        popen = self._proc(name).popen
        try:
            return int(popen.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            self.stop(name)
            raise DeviceError(f"{name} did not finish within {timeout:.0f} s") from None

    def alive(self, name: str) -> bool:
        return self._proc(name).popen.poll() is None

    def stop(self, name: str, timeout: float = STOP_TIMEOUT_S) -> int:
        """SIGTERM to the process group, SIGKILL after `timeout`; returns the exit code."""
        popen = self._proc(name).popen
        if popen.poll() is None:
            _signal_group(popen.pid, signal.SIGTERM)
            try:
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _signal_group(popen.pid, signal.SIGKILL)
                popen.wait()
        return int(popen.returncode)

    def kill(self, name: str) -> int:
        """SIGKILL to the process group at once, without a SIGTERM first: the process cannot react (a hard failure,
        KOR-58). Processes that left the group (Lima's host agents of the VMs) stay. Returns the exit code."""
        popen = self._proc(name).popen
        if popen.poll() is None:
            _signal_group(popen.pid, signal.SIGKILL)
            popen.wait()
        return int(popen.returncode)

    def processes(self) -> dict[str, dict[str, Any]]:
        return {
            name: {"pid": proc.popen.pid, "alive": proc.popen.poll() is None, "rc": proc.popen.returncode}
            for name, proc in self._procs.items()
        }

    def pids(self) -> list[int]:
        """Processes to sample: the running long-lived ones and the one-shot commands still running."""
        return [proc.popen.pid for proc in self._procs.values() if proc.popen.poll() is None] + sorted(self._running)

    # ------------------------------------------------------------------ one-shot

    def run(
        self, argv: list[str], env: dict[str, str] | None = None, cwd: str | None = None, timeout: float = 120.0
    ) -> dict[str, Any]:
        cwd_path = os.path.expanduser(cwd) if cwd else None
        return self.runner(expand(argv), expand_env(env), cwd_path, timeout, track=self._track)

    def http(self, method: str, url: str, body: Any = None, timeout: float = 10.0) -> dict[str, Any]:
        """One JSON request to a loopback service of this device; non-JSON answers come back as text."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except OSError as exc:
            raise DeviceError(f"{method} {url}: {type(exc).__name__}") from None
        text = raw.decode("utf-8", errors="replace")
        try:
            return {"status": status, "body": json.loads(text)}
        except ValueError:
            return {"status": status, "body": text}

    def busy_ports(self, ports: list[int], host: str = "127.0.0.1") -> list[int]:
        busy = []
        for number in ports:
            with socket.socket() as sock:
                # Like the servers themselves: connections of an earlier run in TIME_WAIT do not block the port.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((host, number))
                except OSError:
                    busy.append(number)
        return busy

    # ------------------------------------------------------------------ relays

    def relay_start(self, name: str, listen: list[Any], target: list[Any], tap: str | None = None) -> dict[str, Any]:
        if name in self._relays:
            raise DeviceError(f"relay {name} exists")
        try:
            listen_at, target_at = (str(listen[0]), int(listen[1])), (str(target[0]), int(target[1]))
            relay = Relay(name, listen_at, target_at, self.egress.write, os.path.expanduser(tap) if tap else None)
        except OSError as exc:
            raise DeviceError(
                f"relay {name}: cannot listen on {listen[0]}:{listen[1]} ({type(exc).__name__})"
            ) from None
        self._relays[name] = relay
        return relay.state()

    def _relay(self, name: str) -> Relay:
        try:
            return self._relays[name]
        except KeyError:
            raise DeviceError(f"unknown link {name}") from None

    def relay_cut(self, name: str) -> dict[str, Any]:
        relay = self._relay(name)
        relay.cut()
        return relay.state()

    def relay_restore(self, name: str) -> dict[str, Any]:
        relay = self._relay(name)
        relay.restore()
        return relay.state()

    def relays(self) -> dict[str, dict[str, Any]]:
        return {name: relay.state() for name, relay in self._relays.items()}

    # ------------------------------------------------------------------ egress

    def egress_start(self, interval: float, peers: list[str], match: list[str] | None = None) -> None:
        self.egress.peers = set(peers)
        self.egress.dns = read_resolvers()
        self.egress.match = list(match or [])
        self._egress_started = True
        self.egress.start(self.pids, interval)

    def egress_records(self) -> list[dict[str, Any]]:
        self.egress.sample(self.pids())
        if self._egress_started and self.egress.samples == 0:
            self.egress.error("no successful sampling round during the run")
        return self.egress.records()

    # ------------------------------------------------------------------ files

    def read_text(self, path: str) -> str:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DeviceError(f"cannot read {path} ({type(exc).__name__})") from None

    def archive(self) -> str:
        """The run directory as base64 tar.gz (artefacts of the runner device)."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            tar.add(self.run_dir, arcname=".")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def abandon_cleanup(self, vm_prefix: str, paths: list[str]) -> None:
        """What `abandon()` removes if the orchestrator disappears (T2: the SSH connection ended unexpectedly)."""
        if not vm_prefix:
            raise DeviceError("abandon cleanup needs a VM prefix")
        self._abandon = (vm_prefix, paths)

    def abandon(self) -> list[str]:
        """Delete the run's VMs and scratch paths registered with `abandon_cleanup`; returns the deleted VMs."""
        if self._abandon is None:
            return []
        prefix, paths = self._abandon
        listing = self.runner(["limactl", "list", "--quiet"], None, None, 60)
        names = [n for n in str(listing["stdout"]).split() if n.startswith(prefix)] if listing["rc"] == 0 else []
        for name in names:
            self.runner(["limactl", "delete", "-f", name], None, None, 300)
        for path in expand(paths):
            shutil.rmtree(path, ignore_errors=True)
        return names

    def shutdown(self) -> dict[str, int]:
        codes = {name: self.stop(name) for name in reversed(list(self._procs))}
        for relay in self._relays.values():
            relay.stop()
        self._relays.clear()
        self.egress.stop()
        return codes


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, sig)
