"""Running the runner's own commands as the owner of the Mac (ADR-074 sections 3 and 8).

Root never imports or executes anything below the owner's home: it starts the payload's interpreter (`python -I -B`,
no user site, no `PYTHON*` variables, no byte code written) through `sudo -u <owner>` with an empty environment plus
the runner's settings, and reads only the JSON document of `--json` as data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.install.layout import VM_PREFIX, Layout
from agent_runner.install.owner import Owner
from agent_runner.install.release import Manifest
from agent_runner.install.system import System

SUDO = "/usr/bin/sudo"
ENV = "/usr/bin/env"
PREFIX = "AGENT_RUNNER_"
BUSY_STATES = frozenset({"starting", "agent_starting", "working", "prompt_delivering", "stopping"})


def runner_env(system: System, owner: Owner, manifest: Manifest, reserve_mib: int) -> dict[str, str]:
    """The settings of an own device's runner (written into its plist and used for every command)."""
    layout = system.layout
    env = {
        PREFIX + "HOME": str(Layout.runner_home(owner.home)),
        PREFIX + "HUB_URL": manifest.hub,
        PREFIX + "PLATFORM_ROOT_KEY": manifest.platform_root_key,
        PREFIX + "POOL_SIZE": "0",
        PREFIX + "MEMORY_RESERVE_MIB": str(reserve_mib),
        PREFIX + "GOLDEN_TEMPLATE": str(layout.golden_template()),
        PREFIX + "LOGIN_AUTOMATION": "off",
        PREFIX + "VM_PREFIX": VM_PREFIX,
    }
    if manifest.hub.startswith("http://"):
        env[PREFIX + "TEST_MODE"] = "1"
    return env


@dataclass(frozen=True)
class Context:
    """The installation's system, the owner the runner runs as, and the runner's settings."""

    system: System
    owner: Owner
    env: dict[str, str]

    @property
    def home(self) -> Path:
        return Layout.runner_home(self.owner.home)

    def as_owner(self, argv: Sequence[str]) -> list[str]:
        """`argv` (absolute program path) run as the owner through `sudo`, with the owner's home."""
        return [SUDO, "-u", self.owner.name, "-H", "--", *argv]

    def runner(self, args: Sequence[str]) -> list[str]:
        """Argv that runs `agent-runner <args>` of the active payload as the owner."""
        layout = self.system.layout
        base = {"HOME": str(self.owner.home), "PATH": layout.runner_path(), **self.env}
        assignments = [f"{key}={value}" for key, value in sorted(base.items())]
        program = [str(layout.python()), "-I", "-B", "-m", "agent_runner"]
        return self.as_owner([ENV, "-i", *assignments, *program, *args])


def call(ctx: Context, args: Sequence[str]) -> dict[str, Any]:
    """Run a runner command with `--json` as the owner and return its document (`ok`, `facts`, `error`)."""
    done = ctx.system.run(ctx.runner([*args, "--json"]))
    return parse(done.stdout, args[0])


def parse(stdout: str, name: str) -> dict[str, Any]:
    """The JSON document on the last line of a runner command's output; `runner_command_failed` otherwise."""
    try:
        document = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    except ValueError:
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("facts"), list):
        raise RunnerError("runner_command_failed", name)
    return document


def facts(document: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    return [fact for fact in document["facts"] if isinstance(fact, dict) and fact.get("fact") == f"runner.{name}"]


@dataclass(frozen=True)
class RunnerStatus:
    enrolled: bool = False
    device_id: str = ""
    revoked: str = "no"
    removed: str = "no"
    last_connect: str = "never"
    busy_sessions: int = 0


def status(ctx: Context) -> RunnerStatus:
    """Identity, revocation, hub connection and working sessions from `agent-runner status`."""
    document = call(ctx, ["status"])
    if document.get("ok") is not True:
        raise RunnerError("runner_command_failed", "status")
    identity = next(iter(facts(document, "status.identity")), {})
    hub = next(iter(facts(document, "status.hub")), {})
    sessions = facts(document, "status.session")
    return RunnerStatus(
        enrolled=identity.get("enrolled") is True,
        device_id=str(identity.get("device_id", "")),
        revoked=str(hub.get("revoked", "no")),
        removed=str(hub.get("removed", "no")),
        last_connect=str(hub.get("last_connect", "never")),
        busy_sessions=sum(1 for item in sessions if item.get("state") in BUSY_STATES),
    )


def goldens(ctx: Context) -> list[dict[str, Any]]:
    document = call(ctx, ["image", "list"])
    if document.get("ok") is not True:
        raise RunnerError("runner_command_failed", "image")
    return facts(document, "image")


def enroll(ctx: Context, code: str) -> dict[str, Any]:
    """Enroll with `code` on the standard input of `agent-runner enroll --code-file -` (never in an argv or a file:
    root writes nothing below the owner's home)."""
    args = ["enroll", "--hub", ctx.env[PREFIX + "HUB_URL"], "--code-file", "-", "--json"]
    done = ctx.system.run_input(ctx.runner(args), code + "\n")
    document = parse(done.stdout, "enroll")
    if document.get("ok") is not True:
        error = document.get("error")
        code_name = error.get("code") if isinstance(error, dict) else None
        raise RunnerError("enroll_failed", code_name if isinstance(code_name, str) else None)
    found = next(iter(facts(document, "enroll")), {})
    return found


def uninstall(ctx: Context) -> None:
    document = call(ctx, ["uninstall"])
    if document.get("ok") is not True:
        raise RunnerError("uninstall_failed", "runner")
