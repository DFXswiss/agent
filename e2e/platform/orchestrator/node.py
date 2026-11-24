"""Access to a device: `LocalNode` calls a `Device` in-process, `RemoteNode` drives one over SSH.

The remote side is `python -m orchestrator.node --run-dir <dir>` on the runner device (T2): it reads one JSON
request per line from stdin (`{"method", "args"}`), runs the `Device` method (only those in `device.METHODS`) and
writes one JSON answer per line (`{"ok": true, "result"}` or `{"ok": false, "error"}`). When stdin ends - the
orchestrator finished or the SSH connection broke - it shuts the device down, so no process of the run survives, and
removes the VMs and scratch paths registered with `abandon_cleanup` (the orchestrator removes them itself on a normal
end, so then nothing is left to do). The run directory is emptied at start.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from orchestrator.device import METHODS, Device, DeviceError

CLOSE_TIMEOUT_S = 60.0


class Node:
    """The device API; subclasses implement `call`."""

    name: str
    remote: bool = False

    def call(self, op: str, /, **args: Any) -> Any:
        raise NotImplementedError

    def spawn(self, name: str, argv: list[str], env: dict[str, str], cwd: str | None = None) -> int:
        return int(self.call("spawn", name=name, argv=argv, env=env, cwd=cwd))

    def wait_line(self, name: str, pattern: str, timeout: float) -> str:
        return str(self.call("wait_line", name=name, pattern=pattern, timeout=timeout))

    def wait_exit(self, name: str, timeout: float) -> int:
        return int(self.call("wait_exit", name=name, timeout=timeout))

    def alive(self, name: str) -> bool:
        return bool(self.call("alive", name=name))

    def stop(self, name: str, timeout: float = 10.0) -> int:
        return int(self.call("stop", name=name, timeout=timeout))

    def log_tail(self, name: str, lines: int = 20) -> str:
        return str(self.call("log_tail", name=name, lines=lines))

    def processes(self) -> dict[str, Any]:
        return dict(self.call("processes"))

    def run(
        self, argv: list[str], env: dict[str, str] | None = None, cwd: str | None = None, timeout: float = 120.0
    ) -> dict[str, Any]:
        return dict(self.call("run", argv=argv, env=env, cwd=cwd, timeout=timeout))

    def http(self, method: str, url: str, body: Any = None, timeout: float = 10.0) -> dict[str, Any]:
        return dict(self.call("http", method=method, url=url, body=body, timeout=timeout))

    def busy_ports(self, ports: list[int], host: str = "127.0.0.1") -> list[int]:
        return [int(p) for p in self.call("busy_ports", ports=ports, host=host)]

    def relay_start(
        self, name: str, listen: tuple[str, int], target: tuple[str, int], tap: str | None = None
    ) -> dict[str, Any]:
        """`tap`: directory for a copy of the bytes the target sends back (relay.py); only when given."""
        extra = {"tap": tap} if tap else {}
        return dict(self.call("relay_start", name=name, listen=list(listen), target=list(target), **extra))

    def relay_cut(self, name: str) -> dict[str, Any]:
        return dict(self.call("relay_cut", name=name))

    def relay_restore(self, name: str) -> dict[str, Any]:
        return dict(self.call("relay_restore", name=name))

    def relays(self) -> dict[str, Any]:
        return dict(self.call("relays"))

    def egress_start(self, interval: float, peers: list[str], match: list[str] | None = None) -> None:
        self.call("egress_start", interval=interval, peers=peers, match=match)

    def abandon_cleanup(self, vm_prefix: str, paths: list[str]) -> None:
        self.call("abandon_cleanup", vm_prefix=vm_prefix, paths=paths)

    def egress_records(self) -> list[dict[str, Any]]:
        return list(self.call("egress_records"))

    def read_text(self, path: str) -> str:
        return str(self.call("read_text", path=path))

    def archive(self) -> str:
        return str(self.call("archive"))

    def shutdown(self) -> dict[str, int]:
        return dict(self.call("shutdown"))


class LocalNode(Node):
    def __init__(self, name: str, device: Device) -> None:
        self.name = name
        self.device = device

    def call(self, op: str, /, **args: Any) -> Any:
        try:
            return getattr(self.device, op)(**args)
        except DeviceError as exc:
            raise NodeError(f"{self.name}: {exc}") from None


class NodeError(Exception):
    """A device call failed (locally or on the remote device)."""


class RemoteNode(Node):
    """A device on another machine, driven over one SSH connection (JSON lines on stdin/stdout)."""

    remote = True

    def __init__(self, name: str, argv: Sequence[str], stderr: IO[bytes] | None = None) -> None:
        self.name = name
        self.argv = list(argv)
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr or subprocess.DEVNULL
        )

    def call(self, op: str, /, **args: Any) -> Any:
        stdin, stdout = self._proc.stdin, self._proc.stdout
        assert stdin is not None
        assert stdout is not None
        with self._lock:
            try:
                stdin.write((json.dumps({"method": op, "args": args}) + "\n").encode())
                stdin.flush()
                line = stdout.readline()
            except (OSError, ValueError):
                line = b""
        if not line:
            raise NodeError(f"{self.name}: connection to the device ended (exit {self._proc.poll()})")
        answer = json.loads(line)
        if not answer["ok"]:
            raise NodeError(f"{self.name}: {answer['error']}")
        return answer["result"]

    def close(self) -> int:
        """End the connection (the remote side shuts its device down) and return the exit code of SSH."""
        if self._proc.stdin is not None and not self._proc.stdin.closed:
            self._proc.stdin.close()
        try:
            return self._proc.wait(timeout=CLOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            return self._proc.wait()


def ssh_argv(target: str, remote_command: str, known_hosts: str, path_prefix: str) -> list[str]:
    """SSH command line for the runner device: batch mode, LAN target, own known-hosts file."""
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ControlPath=none",
        target,
        f"export PATH={shlex.quote(path_prefix)}:$PATH; {remote_command}",
    ]


def serve(device: Device, stdin: IO[str], stdout: IO[str]) -> int:
    """Answer requests until stdin ends, then shut the device down."""
    try:
        for line in stdin:
            try:
                request = json.loads(line)
                method = request["method"]
                if method not in METHODS:
                    raise DeviceError(f"unknown method {method!r}")
                answer = {"ok": True, "result": getattr(device, method)(**request.get("args", {}))}
            except (DeviceError, ValueError, KeyError, TypeError) as exc:
                answer = {"ok": False, "error": str(exc) or type(exc).__name__}
            stdout.write(json.dumps(answer) + "\n")
            stdout.flush()
    finally:
        device.shutdown()
        device.abandon()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.node", description="device side of the orchestrator")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser()
    shutil.rmtree(run_dir, ignore_errors=True)  # never mix records of an earlier run into this one
    return serve(Device(run_dir), sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
