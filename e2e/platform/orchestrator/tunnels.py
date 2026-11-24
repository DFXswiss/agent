"""Doubles inside the run's VMs on loopback: one reverse SSH tunnel per running VM (ADR-034).

The guest model gateway accepts plain `http://` only to a loopback upstream (`guest/session-credential.request.json`),
and the runner hands the guest the same upstream it was configured with: the model double's relay on the runner
device's loopback (`http://127.0.0.1:<model-relay>`). This watcher makes exactly that address work inside every VM of
the run: for each running Lima instance whose name starts with the run prefix it keeps one

    ssh -F <instance dir>/ssh.config -N -o ControlMaster=no -o ControlPath=none \\
        -R 127.0.0.1:<port>:127.0.0.1:<port> lima-<name>

per forwarded port (Lima's own SSH configuration, loopback only). Inside the guest the port is bound on 127.0.0.1;
the session sandboxes cannot reach it (nftables lets session users reach only 127.0.0.1:7700), the guest API user
can. Production images and the credential contract stay unchanged; nothing in the guest knows about the test.

    python -m orchestrator.tunnels --prefix wp-x-e2e- --port 20930 [--port ...] [--interval 0.25]

A tunnel whose VM stops ends with its SSH connection; a VM that (still or again) runs gets a new one after `RETRY_S`.
Every change is one line on stdout (`<unix time> tunnel open <name>` / `<unix time> tunnel closed <name> rc=<n>`, SSH's
own messages in between); SIGTERM ends all tunnels.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

INTERVAL_S = 0.25
# A tunnel that ended (the VM still boots and its SSH port refuses) is opened again after this many seconds.
RETRY_S = 1.0
LIST_TIMEOUT_S = 30.0


class Proc(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[[list[str]], Proc]
Lister = Callable[[], str]


def ssh_argv(name: str, directory: str, ports: Sequence[int]) -> list[str]:
    argv = ["ssh", "-F", str(Path(directory) / "ssh.config"), "-N", "-o", "BatchMode=yes"]
    argv += ["-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5"]
    # Lima's ssh.config shares one master connection (`ControlMaster auto`, `ControlPersist yes`): a tunnel attached
    # to it ends at once and its forwards live and die with whatever opened the master. Each tunnel has its own.
    argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    for port in ports:
        argv += ["-R", f"127.0.0.1:{port}:127.0.0.1:{port}"]
    return [*argv, f"lima-{name}"]


def running(listing: str, prefix: str) -> dict[str, str]:
    """`{name: dir}` of the running instances named `<prefix>…` in `limactl list --json` output."""
    found = {}
    for line in listing.splitlines():
        if line.strip():
            item: dict[str, Any] = json.loads(line)
            name = str(item.get("name", ""))
            if name.startswith(prefix) and item.get("status") == "Running" and item.get("dir"):
                found[name] = str(item["dir"])
    return found


def list_instances() -> str:
    done = subprocess.run(
        ["limactl", "list", "--json"], capture_output=True, text=True, check=False, timeout=LIST_TIMEOUT_S
    )
    return done.stdout if done.returncode == 0 else ""


def spawn_ssh(argv: list[str]) -> Proc:
    # SSH's own messages go to the watcher's log (the run's artefacts), next to the open/closed lines.
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


@dataclass
class Tunnels:
    prefix: str
    ports: tuple[int, ...]
    lister: Lister = list_instances
    spawn: Spawn = spawn_ssh
    say: Callable[[str], None] = field(default=lambda line: print(f"{time.time():.3f} {line}", flush=True))
    clock: Callable[[], float] = time.monotonic
    open: dict[str, Proc] = field(default_factory=dict)
    retry_at: dict[str, float] = field(default_factory=dict)

    def step(self) -> None:
        """One round: reap ended tunnels, open one for every running VM without a tunnel (after `RETRY_S`)."""
        now = self.clock()
        for name, proc in list(self.open.items()):
            code = proc.poll()
            if code is not None:
                del self.open[name]
                self.retry_at[name] = now + RETRY_S
                self.say(f"tunnel closed {name} rc={code}")
        for name, directory in sorted(running(self.lister(), self.prefix).items()):
            if name not in self.open and self.retry_at.get(name, now) <= now:
                self.open[name] = self.spawn(ssh_argv(name, directory, self.ports))
                self.say(f"tunnel open {name}")

    def close(self) -> None:
        for proc in self.open.values():
            proc.terminate()
        for proc in self.open.values():
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        self.open.clear()


class Stop(Exception):
    pass


def main(
    argv: Sequence[str] | None = None,
    *,
    tunnels: Callable[[str, tuple[int, ...]], Tunnels] = Tunnels,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.tunnels", description=__doc__.splitlines()[0])
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--port", type=int, action="append", required=True)
    parser.add_argument("--interval", type=float, default=INTERVAL_S)
    args = parser.parse_args(argv)
    if not args.prefix.endswith("-"):
        parser.error("--prefix must end with '-'")

    def on_term(_signum: int, _frame: FrameType | None) -> None:
        raise Stop

    signal.signal(signal.SIGTERM, on_term)
    watcher = tunnels(args.prefix, tuple(args.port))
    print(f"tunnels watching {args.prefix}* ports {','.join(map(str, args.port))}", flush=True)
    try:
        while True:
            watcher.step()
            sleep(args.interval)
    except (Stop, KeyboardInterrupt):
        pass
    finally:
        watcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
