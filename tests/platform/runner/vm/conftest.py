"""Fixtures of the VM area tests (ADR-027 section 6): a stateful `limactl` fake built from recorded output.

`FakeLimactl` answers `limactl` argv lists like Lima 2.3.0 on the runner device did (recordings in `fixtures/`):
instances live in a dict, `list --json` renders them from the recorded JSON line, failures use the recorded stderr.
Guest commands of the golden build (`limactl shell`) are recognised by their content and answered from
`shell_answers` (step key -> `(returncode, stdout, stderr)`), so a test changes one step's answer to drive a
failure path.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_runner.store.migrate import migrate
from agent_runner.vm import golden

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE = "2.1.330"
GUEST = "0.1.0"
TOKEN = "t" * 43
EXEC_SHA = "66a6c4ac95894cf4fb078a9fc7f2964f6fec21cf18b8448a494967a6276b16a2"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def recorded(name: str) -> tuple[int, str, str]:
    """A recorded failure: stderr lines followed by the exit code line."""
    lines = fixture_text(name).splitlines()
    return int(lines[-1]), "", "\n".join(lines[:-1]) + "\n"


def instance_template() -> dict[str, Any]:
    first = fixture_text("list.jsonl").splitlines()[1]
    value: dict[str, Any] = json.loads(first)
    return value


SELFTEST_OK = json.dumps(
    {"ok": True, "checks": [{"check": "layer2", "ok": True}], "kernel": "6.8.0", "landlock_abi": 4}, indent=1
)


def default_shell_answers() -> dict[str, tuple[int, str, str]]:
    return {
        "extract": (0, "", ""),
        "install": (0, "agent-golden: done\n", ""),
        "contracts": (0, "", ""),
        "inspect": (
            0,
            json.dumps(
                {"exec_paths": ["/usr/lib/libc.so.6"], "exec_paths_sha256": EXEC_SHA, "landlock_abi": 4, "kernel": "6"}
            )
            + "\n",
            "",
        ),
        "build_file": (0, "", ""),
        "token": (0, TOKEN + "\n", ""),
        "api": (0, json.dumps({"state": "ok", "guest_version": GUEST}), ""),
        "claude": (0, f"{CLAUDE} (Claude Code)\n", ""),
        "selftest": (0, SELFTEST_OK + "\n", ""),
        "tui": (0, json.dumps({"ok": True, "tui_start_ms": 180, "screen": "Welcome to Claude Code"}) + "\n", ""),
        "seal": (0, "", ""),
        "other": (0, "", ""),
    }


def shell_step(argv: Sequence[str]) -> str:
    """The build step a `limactl shell` command belongs to (by its content)."""
    text = "\0".join(argv)
    if golden.INSPECT_SCRIPT in argv:
        return "inspect"
    if golden.TUI_SCRIPT in argv:
        return "tui"
    for marker, step in (
        ("install.sh", "install"),
        ("agent_guest.sandbox.selftest", "selftest"),
        ("tar -xf", "extract"),
        ("cp -R", "contracts"),
        ("printf", "build_file"),
        ("token_urlsafe", "token"),
        ("systemctl stop", "seal"),
        ("curl", "api"),
        ("--version", "claude"),
    ):
        if marker in text:
            return step
    return "other"


@dataclass
class FakeLimactl:
    """Stateful `limactl` (see the module docstring). `fail[op]` answers the next call of `op` once."""

    lima_dir: Path
    version: str = "limactl version 2.3.0\n"
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[list[str], float]] = field(default_factory=list)
    fail: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    shell_answers: dict[str, tuple[int, str, str]] = field(default_factory=default_shell_answers)
    shell_steps: list[str] = field(default_factory=list)
    hook: Callable[[list[str]], None] | None = None
    # Report this directory for every instance (one control-socket server serves all of them).
    fixed_dir: Path | None = None

    def instance_dir(self, name: str) -> str:
        return str(self.fixed_dir or self.lima_dir / name)

    def add(self, name: str, status: str = "Stopped") -> None:
        item = copy.deepcopy(instance_template())
        item.update(name=name, status=status, dir=self.instance_dir(name))
        self.instances[name] = item

    def add_broken(self, name: str) -> Path:
        """The leftover of a `limactl clone` killed early: an empty instance directory Lima lists as `Broken`."""
        directory = Path(self.instance_dir(name))
        directory.mkdir(parents=True, exist_ok=True)
        item = json.loads(fixture_text("list-broken.jsonl"))
        template_dir, template_home = item["dir"], item["LimaHome"]
        text = json.dumps(item).replace(template_dir, str(directory)).replace(template_home, str(directory.parent))
        text = text.replace("wp-kor03-ck-3", name)
        self.instances[name] = json.loads(text)
        return directory

    def save(self, path: Path) -> None:
        state = {
            "lima_dir": str(self.lima_dir),
            "fixed_dir": None if self.fixed_dir is None else str(self.fixed_dir),
            "instances": self.instances,
            "calls": self.calls,
            "fail": self.fail,
            "shell_answers": self.shell_answers,
            "shell_steps": self.shell_steps,
        }
        path.write_text(json.dumps(state), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> FakeLimactl:
        state = json.loads(path.read_text(encoding="utf-8"))
        fake = cls(lima_dir=Path(state["lima_dir"]))
        fake.fixed_dir = None if state["fixed_dir"] is None else Path(state["fixed_dir"])
        fake.instances = state["instances"]
        fake.calls = [(list(argv), timeout) for argv, timeout in state["calls"]]
        fake.fail = {key: tuple(value) for key, value in state["fail"].items()}
        fake.shell_answers = {key: tuple(value) for key, value in state["shell_answers"].items()}
        fake.shell_steps = state["shell_steps"]
        return fake

    def ops(self) -> list[str]:
        return [argv[0] for argv, _ in self.calls]

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append((args, timeout))
        if self.hook is not None:
            self.hook(args)
        code, out, err = self._answer(args)
        return subprocess.CompletedProcess(["limactl", *args], code, out, err)

    def _answer(self, args: list[str]) -> tuple[int, str, str]:
        op = args[0]
        if op in self.fail:
            return self.fail.pop(op)
        handler = {
            "--version": self._version,
            "list": self._list,
            "create": self._create,
            "shell": self._shell,
            "start": self._start_stop,
            "stop": self._start_stop,
            "delete": self._delete,
            "rename": self._rename,
            "clone": self._clone,
        }.get(op, lambda _args: (0, "", ""))  # copy
        return handler(args)

    def _version(self, args: list[str]) -> tuple[int, str, str]:
        return 0, self.version, ""

    def _create(self, args: list[str]) -> tuple[int, str, str]:
        self.add(args[2].removeprefix("--name="))
        return 0, "", ""

    def _shell(self, args: list[str]) -> tuple[int, str, str]:
        step = shell_step(args[4:])
        self.shell_steps.append(step)
        return self.shell_answers[step]

    def _start_stop(self, args: list[str]) -> tuple[int, str, str]:
        if args[-1] not in self.instances:
            return recorded("stop-missing.txt")
        item = self.instances[args[-1]]
        item["status"] = "Running" if args[0] == "start" else "Stopped"
        if args[0] == "start" and self.fixed_dir is None:
            # Like Lima: the host end of the socket forward lives in <dir>/sock, left behind with mode 000.
            sock = Path(item["dir"]) / "sock"
            sock.mkdir(parents=True, exist_ok=True)
            sock.chmod(0o000)
        return 0, "", ""

    def _delete(self, args: list[str]) -> tuple[int, str, str]:
        item = self.instances.get(args[-1])
        if item is not None and item["status"] == "Broken":
            code, out, err = recorded("delete-broken.txt")
            return code, out, err.replace("wp-kor03-ck-3", args[-1])
        if self.instances.pop(args[-1], None) is None:
            return recorded("delete-missing.txt")
        return 0, "", ""

    def _list(self, args: list[str]) -> tuple[int, str, str]:
        # A Broken instance is listed while its directory exists (Lima scans its home).
        for name, item in list(self.instances.items()):
            if item["status"] == "Broken" and not Path(item["dir"]).exists():
                del self.instances[name]
        # `list --json NAME...` (Lima 2.3.0): only the named instances; one unmatched name fails the whole call.
        names = [arg for arg in args[1:] if not arg.startswith("--")]
        if any(name not in self.instances for name in names):
            return recorded("list-missing.txt")
        items = [item for name, item in self.instances.items() if not names or name in names]
        return 0, "".join(json.dumps(item) + "\n" for item in items), ""

    def _rename(self, args: list[str]) -> tuple[int, str, str]:
        old, new = args[1], args[2]
        if new in self.instances:
            return recorded("rename-exists.txt")
        item = self.instances.pop(old)
        item.update(name=new, dir=self.instance_dir(new))
        self.instances[new] = item
        return 0, "", ""

    def _clone(self, args: list[str]) -> tuple[int, str, str]:
        if args[-2] not in self.instances:
            return recorded("clone-missing.txt")
        self.add(args[-1])
        return 0, "", ""


STATE_ENV = "FAKE_LIMACTL_STATE"
SCRIPT = """#!{python}
import os, sys
sys.path.insert(0, {tests!r})
from conftest import run_script
sys.exit(run_script(sys.argv[1:]))
"""


def run_script(argv: list[str]) -> int:
    """Entry of the `limactl` stand-in binary: one call against the state file of `$FAKE_LIMACTL_STATE`."""
    path = Path(os.environ[STATE_ENV])
    fake = FakeLimactl.load(path)
    result = fake(argv, 0.0)
    fake.save(path)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def install_binary(bindir: Path) -> Path:
    """Write the `limactl` stand-in into `bindir`."""
    bindir.mkdir(parents=True, exist_ok=True)
    binary = bindir / "limactl"
    binary.write_text(SCRIPT.format(python=sys.executable, tests=str(Path(__file__).parent)), encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def lima_dir(tmp_path: Path) -> Path:
    path = tmp_path / "lima"
    path.mkdir()
    return path


@pytest.fixture
def fake_limactl(lima_dir: Path) -> FakeLimactl:
    return FakeLimactl(lima_dir=lima_dir)


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    """A migrated runner store."""
    result = migrate(runner_dsn)
    assert result.status == "applied"
    return runner_dsn


# -- VM-02: pool, lifecycle and tasks against fake Lima VMs with real guest control processes ---------------------


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory with a short path under /tmp (Unix socket paths are limited to 104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="vm2", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def harness(short_dir: Path, store_dsn: str, repo_root: Path, tmp_path: Path, fake_clock: Any) -> Iterator[Any]:
    """`Harness` of `guests.py`: fake Lima with real guests, migrated store, fake clock; guests stopped afterwards."""
    # guests.py builds on this module's FakeLimactl, so it can only be imported once this module exists.
    from .guests import GuestFarm, Harness, LimaWithGuests  # noqa: PLC0415

    lima_dir = short_dir / "lima"
    lima_dir.mkdir()
    fake = LimaWithGuests(lima_dir=lima_dir, farm=GuestFarm(contracts=repo_root / "contracts"))
    built = Harness(fake=fake, dsn=store_dsn, tokens_dir=tmp_path / "rh" / "vm", clock=fake_clock)
    try:
        yield built
    finally:
        assert fake.farm is not None
        fake.farm.close()
