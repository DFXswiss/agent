"""Test hooks inside the run's Lima VMs (SCN-02): find a session's VM, run root commands there, read guest files.

Everything goes through `limactl shell <vm> sudo -n …` on the runner device (the run's VMs carry the run prefix,
`RunConfig.run_prefix`); nothing here is reachable from a session or the hub. Paths and names follow the guest
(`05-runner.md` sections 4.3, 7): session directory `/srv/sessions/<id>`, tmux socket `/run/agent-guest/tmux.sock`
with one tmux session `s-<id>` per session, unit `agent-session-<id>`, session user `s-<hex digits of the id without
digits 13 and 17>` (`agent_guest.sandbox.unit.user_name`), asciicast copy `/srv/sessions/<id>/terminal.cast`.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from orchestrator.context import Context
from orchestrator.vms import list_vms

TMUX_SOCKET = "/run/agent-guest/tmux.sock"
SESSIONS_ROOT = "/srv/sessions"
UNIT_PREFIX = "agent-session-"
SHELL_TIMEOUT_S = 60.0
READY_TIMEOUT_S = 120.0
POLL_S = 0.5
SSH_NOT_READY = 255
# `sudo -n` while the guest's boot hardening runs (images/.../harden.sh first removes every sudo grant of the Lima
# default user, then writes the TEST_MODE grant again): the VM answers, but its root hooks are not ready yet.
SUDO_NOT_READY = "a password is required"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class GuestVmError(Exception):
    """No VM of the run hosts the session, or a command in the VM failed."""


def session_uuid(text: str) -> str:
    if not _UUID_RE.fullmatch(text):
        raise GuestVmError(f"not a session ID: {text!r}")
    return text


def user_name(session_id: str) -> str:
    """The session user of `agent_guest.sandbox.unit.user_name`."""
    digits = uuid.UUID(session_uuid(session_id)).hex
    return "s-" + digits[:12] + digits[13:16] + digits[17:]


def running_vms(ctx: Context) -> list[str]:
    """Names of the run's running instances (pool and workgroup VMs of every runner)."""
    found = list_vms(ctx.runner_node) or []
    return sorted(vm.name for vm in found if vm.name.startswith(ctx.config.run_prefix) and vm.status == "Running")


def shell(ctx: Context, vm: str, argv: list[str]) -> dict[str, Any]:
    """`argv` as root in `vm` (Lima quotes every argument); returns the node's `{rc, stdout, stderr}`."""
    if not vm.startswith(ctx.config.run_prefix):
        raise GuestVmError(f"{vm} is not a VM of this run")
    command = ["limactl", "shell", "--workdir", "/", vm, "sudo", "-n", *argv]
    return ctx.runner_node.run(command, timeout=SHELL_TIMEOUT_S)


def shell_when_up(
    ctx: Context,
    vm: str,
    argv: list[str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """`shell` for a VM that may still be booting (a pool VM being filled: SSH answers 255 until it is up, then `sudo`
    refuses until the boot hardening has written the TEST_MODE grant): retried until `READY_TIMEOUT_S`; None when a
    command failed because the VM disappeared meanwhile (deleted by its runner)."""
    deadline = clock() + READY_TIMEOUT_S
    while True:
        done = shell(ctx, vm, argv)
        if done["rc"] == 0:
            return done
        if vm not in running_vms(ctx):
            return None
        booting = done["rc"] == SSH_NOT_READY or SUDO_NOT_READY in done["stderr"]
        if not booting or clock() >= deadline:
            return done
        sleep(POLL_S)


def checked(ctx: Context, vm: str, argv: list[str]) -> str:
    done = shell(ctx, vm, argv)
    if done["rc"] != 0:
        raise GuestVmError(f"{' '.join(argv[:3])} in {vm} exited with {done['rc']}: {done['stderr'].strip()[-300:]}")
    return str(done["stdout"])


def session_vm(ctx: Context, session_id: str) -> str:
    """The VM whose tmux server has the session's tmux session."""
    name = f"s-{session_uuid(session_id)}"
    tried = []
    for vm in running_vms(ctx):
        done = shell(ctx, vm, ["tmux", "-S", TMUX_SOCKET, "has-session", "-t", f"={name}"])
        if done["rc"] == 0:
            return vm
        tried.append(f"{vm}: {done['rc']} {done['stderr'].strip()[-200:]}")
    raise GuestVmError(f"no VM of the run hosts session {session_id} ({'; '.join(tried) or 'no running VM'})")
