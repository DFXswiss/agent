"""Typed wrapper of `limactl` (docs/platform/spec/05-runner.md sections 3.1-3.5, ADR-009, ADR-027 section 7).

Every Lima operation the runner needs is one method with its own time limit; nothing else runs `limactl`. The
command itself is an injected callable `run(argv, timeout) -> CompletedProcess` (legacy injection style,
07-testing.md section 1): production uses `run_limactl` (the binary from `PATH`), tests a fake that answers with
recorded `limactl` output (`tests/platform/runner/vm/fixtures/`).

Instance names always start with the runner's `AGENT_RUNNER_VM_PREFIX`: `name(suffix)` builds them, every method
refuses a name outside the prefix (`lima_foreign_instance`) and `list()` reports only own instances, so the runner
never touches a VM it did not create (other runners, other work packages, a developer's VMs).

| Method | `limactl` |
|---|---|
| `version()` | `--version`; `check_version()` refuses Lima < 2.3.0 (`lima_version`) |
| `create(name, template, cpus=, memory_mib=, disk_gib=)` | `create -y --name=NAME [--cpus --memory --disk] TEMPLATE` |
| `start(name, params=)` | `start -y [--set '.param.KEY="VALUE"' ...] NAME` |
| `stop(name)` / `delete(name)` | `stop NAME` / `delete -f NAME` (plus the leftover of an interrupted clone, below) |
| `clone(source, name, sets=)` | `clone --tty=false [--set EXPR ...] SOURCE NAME` (`-y` is deprecated for clone) |
| `rename(old, new)` | `rename OLD NEW` (stopped instances only) |
| `list()` / `get(name)` | `list --json` / `list --json NAME` (one JSON object per line) |
| `shell(name, argv, timeout=)` | `shell --workdir / NAME ARGV...` |
| `copy(source, target)` | `copy SOURCE TARGET` (`NAME:/path` names the guest side) |

A `limactl clone` killed early (a stop signal to the runner's process group) can leave an instance directory without
`lima.yaml`: `list` reports it `Broken` and `delete -f` refuses it "in case it is currently being cloned into"
(measured with Lima 2.3.0, decisions KOR-03). The runner deletes only instances whose `limactl` calls have returned
(provisioning serialises the deletion behind them; the startup reconciliation runs before any other VM work), so
`delete` then removes that directory itself - only an own instance (prefix), reported `Broken`, whose directory has
the instance's name, lies directly in Lima's home (`LimaHome` of the listing) and holds no `lima.yaml`. The refusal is
recognised by its own line ("it was NOT deleted"), with or without Lima's warning before it.

Failures raise `LimaError` subclasses with a closed code and the operation as name (`RunnerError` contract: never
a value). The tail of `limactl`'s stderr is kept in `detail` for logs written by the caller; it is never part of
the message, because it may repeat argument values.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_runner.errors import RunnerError

LimactlRun = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]

MIN_VERSION = (2, 3, 0)
EXIT_NOT_FOUND = 127
EXIT_TIMEOUT = 124
DETAIL_LIMIT = 2000
# Time limits in seconds per operation (05 section 3: clone 0.04 s, start 4-6 s, stop ~1.2 s, delete ~0.2 s on the
# runner device; the build start includes the slim provisioning, ~16 s). Generous, but a hung `limactl` never
# blocks the runner for ever.
DEFAULT_TIMEOUTS: Mapping[str, float] = MappingProxyType(
    {
        "version": 15.0,
        "create": 120.0,
        "start": 600.0,
        "stop": 180.0,
        "delete": 120.0,
        "clone": 120.0,
        "rename": 60.0,
        "list": 30.0,
        "shell": 120.0,
        "copy": 300.0,
    }
)
# Lima instance names (Lima's identifier rule; dots allowed for versioned golden names). The guest hostname is
# `lima-<name>` (dots become dashes), a DNS label of at most 63 characters.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_MAX_NAME = 58
# Shared golden caches every work package may clone (ADR-027 section 7).
SHARED_PREFIX = "cache-golden-"
_PARAM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PARAM_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{0,512}$")
_VERSION_RE = re.compile(r"limactl version v?(\d+)\.(\d+)\.(\d+)")
_MISSING_RE = re.compile(r"instance `[^`]*` (?:does not exist|not found)")
_IGNORED_MISSING = "Ignoring non-existent instance"
_NOT_DELETED = "it was NOT deleted"
_NOT_INSTALLED = "limactl: not found"
_TIMED_OUT = "limactl: timed out"
_GUEST_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{0,255}$")


class LimaError(RunnerError):
    """A failed `limactl` call; `detail` holds the stderr tail (for logs only, may repeat argument values)."""

    def __init__(self, code: str, operation: str, *, returncode: int | None = None, detail: str = "") -> None:
        super().__init__(code, operation)
        self.operation = operation
        self.returncode = returncode
        self.detail = detail[-DETAIL_LIMIT:]


class LimaTimeout(LimaError):
    def __init__(self, operation: str, *, detail: str = "") -> None:
        super().__init__("lima_timeout", operation, returncode=EXIT_TIMEOUT, detail=detail)


class LimaNotInstalled(LimaError):
    def __init__(self, operation: str) -> None:
        super().__init__("lima_missing", operation, returncode=EXIT_NOT_FOUND)


class LimaNotFound(LimaError):
    """The named instance does not exist."""

    def __init__(self, operation: str, *, detail: str = "") -> None:
        super().__init__("lima_instance_missing", operation, returncode=1, detail=detail)


class LimaVersionError(LimaError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("lima_version", "version", detail=detail)


def run_limactl(
    argv: Sequence[str], timeout: float, *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `limactl ARGV` (list form, never a shell) with `timeout`; never raises for exit codes.

    `env` is the process environment (default: this process's); `limactl` is looked up on its `PATH`. A missing
    binary answers 127, a timeout 124 (the child is killed), like `agent_runner.host.run_command`.
    """
    environment = dict(os.environ if env is None else env)
    binary = shutil.which("limactl", path=environment.get("PATH", os.defpath))
    if binary is None:
        return subprocess.CompletedProcess(["limactl", *argv], EXIT_NOT_FOUND, "", _NOT_INSTALLED)
    try:
        return subprocess.run(
            [binary, *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([binary, *argv], EXIT_TIMEOUT, "", _TIMED_OUT)


def parse_version(text: str) -> tuple[int, int, int]:
    """`(major, minor, patch)` from `limactl --version` output; `LimaVersionError` when it is not recognised."""
    match = _VERSION_RE.search(text)
    if match is None:
        raise LimaVersionError(text)
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def memory_gib(memory_mib: int) -> str:
    """`--memory` value (GiB, Lima accepts fractions) of a size in MiB."""
    return f"{memory_mib / 1024:g}"


@dataclass(frozen=True)
class Instance:
    """One entry of `limactl list --json` (the fields the runner uses)."""

    name: str
    status: str
    dir: str
    cpus: int
    memory_bytes: int
    disk_bytes: int
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def running(self) -> bool:
        return self.status == "Running"

    @property
    def control_socket(self) -> str:
        """Host end of the forwarded guest control socket (`images/golden.yaml`)."""
        return os.path.join(self.dir, "sock", "agent-guest.sock")


def _instance(item: object) -> Instance:
    if not isinstance(item, dict):
        raise LimaError("lima_output_invalid", "list")
    try:
        name, status, directory = item["name"], item["status"], item["dir"]
        cpus, memory, disk = item.get("cpus", 0), item.get("memory", 0), item.get("disk", 0)
    except KeyError:
        raise LimaError("lima_output_invalid", "list") from None
    if not all(isinstance(value, str) for value in (name, status, directory)) or not all(
        type(value) is int for value in (cpus, memory, disk)
    ):
        raise LimaError("lima_output_invalid", "list")
    return Instance(name=name, status=status, dir=directory, cpus=cpus, memory_bytes=memory, disk_bytes=disk, raw=item)


def parse_list(text: str) -> list[Instance]:
    """Instances of `limactl list --json` output (one JSON document per line; empty output = none)."""
    instances = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise LimaError("lima_output_invalid", "list") from None
        instances.append(_instance(item))
    return instances


class Lima:
    """`limactl` operations on the instances of one runner (prefix)."""

    def __init__(
        self,
        run: LimactlRun,
        *,
        prefix: str = "",
        timeouts: Mapping[str, float] | None = None,
    ) -> None:
        self._run = run
        self.prefix = prefix
        self.timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}

    # -- names -----------------------------------------------------------------------------------------------

    def name(self, suffix: str) -> str:
        """Instance name `<prefix><suffix>` (validated)."""
        return self._own(self.prefix + suffix, "name")

    def owns(self, name: str) -> bool:
        return name.startswith(self.prefix) and bool(_NAME_RE.fullmatch(name)) and len(name) <= _MAX_NAME

    def _own(self, name: str, operation: str) -> str:
        if not self.owns(name):
            raise LimaError("lima_foreign_instance", operation)
        return name

    # -- execution -------------------------------------------------------------------------------------------

    def _call(
        self, operation: str, argv: Sequence[str], *, timeout: float | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run one `limactl` command; Lima's own failures always raise, a plain non-zero exit only with `check`.

        `limactl shell` passes the guest command's exit code through, so 127 and 124 count as "limactl missing" and
        "timed out" only together with the messages of `run_limactl`.
        """
        limit = self.timeouts[operation] if timeout is None else timeout
        result = self._run(list(argv), limit)
        if result.returncode == 0:
            return result
        stderr = result.stderr or ""
        if result.returncode == EXIT_NOT_FOUND and _NOT_INSTALLED in stderr:
            raise LimaNotInstalled(operation)
        if result.returncode == EXIT_TIMEOUT and _TIMED_OUT in stderr:
            raise LimaTimeout(operation, detail=stderr)
        if _missing(stderr):
            raise LimaNotFound(operation, detail=stderr)
        if not check:
            return result
        raise LimaError("lima_failed", operation, returncode=result.returncode, detail=stderr)

    # -- operations ------------------------------------------------------------------------------------------

    def version(self) -> tuple[int, int, int]:
        result = self._call("version", ["--version"])
        return parse_version(result.stdout)

    def check_version(self) -> tuple[int, int, int]:
        """The installed version; `LimaVersionError` below 2.3.0 (05 section 1.2 doctor, section 3.1)."""
        found = self.version()
        if found < MIN_VERSION:
            raise LimaVersionError(".".join(map(str, found)))
        return found

    def create(
        self,
        name: str,
        template: str,
        *,
        cpus: int | None = None,
        memory_mib: int | None = None,
        disk_gib: int | None = None,
    ) -> None:
        argv = ["create", "-y", f"--name={self._own(name, 'create')}"]
        if cpus is not None:
            argv += ["--cpus", str(cpus)]
        if memory_mib is not None:
            argv += ["--memory", memory_gib(memory_mib)]
        if disk_gib is not None:
            argv += ["--disk", str(disk_gib)]
        self._call("create", [*argv, template])

    def start(self, name: str, *, params: Mapping[str, str] | None = None, timeout: float | None = None) -> None:
        """Start (and wait until ready); `params` become `--set '.param.KEY="VALUE"'` before boot (05 section 3.2)."""
        # `limactl start NAME` of a missing instance would create one from template:default: refuse it first.
        self._require(name, "start")
        argv = ["start", "-y"]
        for key, value in sorted((params or {}).items()):
            if not _PARAM_RE.fullmatch(key) or not _PARAM_VALUE_RE.fullmatch(value):
                raise LimaError("lima_param_invalid", "start")
            argv += ["--set", f'.param.{key}="{value}"']
        self._call("start", [*argv, self._own(name, "start")], timeout=timeout)

    def stop(self, name: str) -> None:
        self._call("stop", ["stop", self._own(name, "stop")])

    def delete(self, name: str, *, missing_ok: bool = True) -> bool:
        """`delete -f`; returns False when the instance did not exist (`LimaNotFound` unless `missing_ok`).

        The leftover of an interrupted clone that Lima refuses to delete is removed here (module docstring).
        """
        try:
            result = self._call("delete", ["delete", "-f", self._own(name, "delete")])
        except LimaError as exc:
            if _NOT_DELETED not in exc.detail:
                raise
            if self._remove_partial(name):
                return True
            if not missing_ok:
                raise LimaNotFound("delete", detail=exc.detail) from None
            return False
        if _IGNORED_MISSING in (result.stderr or ""):
            if not missing_ok:
                raise LimaNotFound("delete", detail=result.stderr)
            return False
        return True

    def clone(self, source: str, name: str, *, sets: Sequence[str] = ()) -> None:
        """APFS clone of a stopped instance (05 section 3.2); `sets` are `--set` yq expressions for the copy.

        The source may be any own instance; a shared golden cache (`cache-golden-...`) is the one exception to
        the prefix rule, because every work package may clone it (ADR-027 section 7).
        """
        if not (self.owns(source) or (source.startswith(SHARED_PREFIX) and _NAME_RE.fullmatch(source))):
            raise LimaError("lima_foreign_instance", "clone")
        argv = ["clone", "--tty=false"]
        for expression in sets:
            argv += ["--set", expression]
        self._call("clone", [*argv, source, self._own(name, "clone")])

    def rename(self, old: str, new: str) -> None:
        self._call("rename", ["rename", self._own(old, "rename"), self._own(new, "rename")])

    def list(self) -> list[Instance]:
        """Own instances (prefix), in `limactl` order."""
        result = self._call("list", ["list", "--json"])
        return [instance for instance in parse_list(result.stdout) if self.owns(instance.name)]

    def get(self, name: str) -> Instance | None:
        """The own instance `name`, or None; `list --json NAME` inspects that instance only (KOR-15).

        A full listing inspects every instance of the device (0.17-0.20 s with 67 instances, 0.03 s by name with Lima
        2.3.0) and ran twice on every clone and restart (`start` checks the instance, then the socket path is looked
        up). Lima ends the call for a missing name with "No instance matching ..." and exit 1 (`LimaNotFound`).
        """
        try:
            result = self._call("list", ["list", "--json", self._own(name, "list")])
        except LimaNotFound:
            return None
        return next((instance for instance in parse_list(result.stdout) if instance.name == name), None)

    def shared(self, name: str) -> Instance | None:
        """A shared golden cache (`cache-golden-...`, the one foreign source `clone` accepts) by name."""
        if not (name.startswith(SHARED_PREFIX) and _NAME_RE.fullmatch(name)):
            raise LimaError("lima_foreign_instance", "list")
        result = self._call("list", ["list", "--json"])
        return next((instance for instance in parse_list(result.stdout) if instance.name == name), None)

    def _remove_partial(self, name: str) -> bool:
        """Remove the directory of an interrupted clone (`Broken`, no `lima.yaml`, directly below Lima's home);
        False when the instance is gone meanwhile; anything else stays refused (`lima_failed`)."""
        instance = self.get(name)
        if instance is None:
            return False
        directory = Path(instance.dir)
        home = instance.raw.get("LimaHome")
        if (
            instance.status != "Broken"
            or not isinstance(home, str)
            or not directory.is_absolute()
            or directory.parent != Path(home)
            or directory.name != name
            or (directory / "lima.yaml").exists()
        ):
            raise LimaError("lima_failed", "delete", returncode=1)
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            raise LimaError("lima_failed", "delete", returncode=1)
        return True

    def _require(self, name: str, operation: str) -> Instance:
        instance = self.get(self._own(name, operation))
        if instance is None:
            raise LimaNotFound(operation)
        return instance

    def shell(
        self, name: str, argv: Sequence[str], *, timeout: float | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` in the guest as the Lima default user (`--workdir /`: no host directory is mounted).

        With `check=False` a non-zero exit of the guest command is returned instead of raised (Lima's own
        failures - missing instance, missing binary, timeout - still raise).
        """
        if not argv:
            raise LimaError("lima_argv_invalid", "shell")
        full = ["shell", "--workdir", "/", self._own(name, "shell"), *argv]
        return self._call("shell", full, timeout=timeout, check=check)

    def copy_to(self, source: str, name: str, guest_path: str) -> None:
        """Copy the host file `source` to `guest_path` (owned by the Lima default user)."""
        if not _GUEST_PATH_RE.fullmatch(guest_path):
            raise LimaError("lima_argv_invalid", "copy")
        self._call("copy", ["copy", source, f"{self._own(name, 'copy')}:{guest_path}"])


def _missing(stderr: str) -> bool:
    """True when `limactl` reports an instance that does not exist."""
    return (
        "No instance matching" in stderr
        or "lima.yaml: no such file or directory" in stderr
        or bool(_MISSING_RE.search(stderr))
    )
