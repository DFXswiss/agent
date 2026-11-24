"""Fixtures of the installation helper's suite (ADR-074): a `System` whose layout lives in a temporary directory,
whose programs are a scripted fake that records every argv, and whose downloads come from a dictionary.

`fake.answer(prefix, rc, stdout)` scripts the answer to every argv that starts with `prefix` (the newest matching
script wins; a list of answers is consumed in order, the last one repeats); unscripted programs exit 0 with no
output. `fake.calls` lists the argv of every call; a call with standard input (`System.run_input`) is answered and
recorded the same way and its `(argv, text)` is also listed in `fake.inputs`. Release keys are generated per test
(`release_keys`).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from agent_runner.errors import RunnerError
from agent_runner.install import release, runnerctl
from agent_runner.install.layout import Layout
from agent_runner.install.owner import Owner
from agent_runner.install.system import System

COMMIT = "0123456789abcdef0123456789abcdef01234567"
HUB = "https://hub.example"
ROOT_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
VERSION = "2026.11.24.0800"
NEWER = "2026.11.25.0800"
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

Answer = tuple[int, str, str]


@dataclass
class FakeRun:
    scripts: list[tuple[tuple[str, ...], list[Answer]]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    hooks: list[tuple[tuple[str, ...], Callable[[list[str]], None]]] = field(default_factory=list)
    inputs: list[tuple[list[str], str]] = field(default_factory=list)

    def answer(self, prefix: Sequence[str], rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.scripts.insert(0, (tuple(prefix), [(rc, stdout, stderr)]))

    def answers(self, prefix: Sequence[str], answers: list[Answer]) -> None:
        self.scripts.insert(0, (tuple(prefix), list(answers)))

    def on(self, prefix: Sequence[str], hook: Callable[[list[str]], None]) -> None:
        """Call `hook(argv)` for every call whose argv starts with `prefix` (before answering)."""
        self.hooks.append((tuple(prefix), hook))

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(argv)
        self.calls.append(call)
        for prefix, hook in self.hooks:
            if tuple(call[: len(prefix)]) == prefix:
                hook(call)
        for prefix, answers in self.scripts:
            if tuple(call[: len(prefix)]) == prefix:
                rc, out, err = answers.pop(0) if len(answers) > 1 else answers[0]
                return subprocess.CompletedProcess(call, rc, out, err)
        return subprocess.CompletedProcess(call, 0, "", "")

    def with_input(self, argv: Sequence[str], text: str) -> subprocess.CompletedProcess[str]:
        """`System.run_input`: records `(argv, text)`, then answers like a call without input."""
        self.inputs.append((list(argv), text))
        return self(argv)

    def called(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if tuple(call[: len(prefix)]) == prefix]


@dataclass
class FakeFetch:
    files: dict[str, bytes] = field(default_factory=dict)
    urls: list[str] = field(default_factory=list)

    def __call__(self, url: str, limit: int) -> bytes:
        self.urls.append(url)
        if url not in self.files:
            raise RunnerError("download_failed")
        data = self.files[url]
        if len(data) > limit:
            raise RunnerError("download_failed", "size")
        return data


@dataclass
class Kit:
    system: System
    run: FakeRun
    fetch: FakeFetch
    out: io.StringIO
    hour: list[int]
    clock: list[datetime]

    @property
    def layout(self) -> Layout:
        return self.system.layout


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    root = tmp_path / "root"
    root.mkdir()
    run = FakeRun()
    fetch = FakeFetch()
    out = io.StringIO()
    hour = [12]
    clock = [NOW]
    system = System(
        layout=Layout(root),
        run=run,
        run_input=run.with_input,
        fetch=fetch,
        env={},
        stdout=out,
        platform="darwin",
        machine=lambda: "arm64",
        geteuid=lambda: 0,
        disk_free=lambda path: 500 * 1024**3,
        now=lambda: clock[0],
        local_hour=lambda: hour[0],
        chown=lambda path, uid, gid: None,
        root_uid=os.getuid(),
    )
    return Kit(system, run, fetch, out, hour, clock)


@dataclass(frozen=True)
class Keys:
    private: bytes
    public: str

    def sign(self, data: bytes) -> str:
        return release.sign(data, self.private)


def new_keys() -> Keys:
    raw = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return Keys(raw, release.public_key(raw))


@pytest.fixture
def keys() -> Keys:
    return new_keys()


def manifest_doc(files: dict[str, str], version: str = VERSION, **extra: Any) -> dict[str, Any]:
    document = {
        "v": 1,
        "version": version,
        "commit": COMMIT,
        "hub": HUB,
        "platform_root_key": ROOT_KEY,
        "files": files,
        "fetch": [],
    }
    document.update(extra)
    return document


def make_version(layout: Layout, keys: Keys, version: str = VERSION, **extra: Any) -> Path:
    """A version directory with a launcher, an interpreter stub, golden sources and a signed manifest."""
    folder = layout.version_dir(version)
    (folder / "python/bin").mkdir(parents=True)
    (folder / "python/bin/python3").write_text("#!/bin/sh\n", encoding="utf-8")
    (folder / "libexec").mkdir()
    (folder / "libexec/dfx-agent-runner").write_text("#!/bin/sh\n", encoding="utf-8")
    write_golden_source(folder / "golden-src")
    document = manifest_doc(release.tree_entries(folder), version, **extra)
    data = release.canonical(document)
    (folder / release.MANIFEST).write_bytes(data)
    (folder / (release.MANIFEST + release.SIGNATURE_SUFFIX)).write_text(keys.sign(data), encoding="ascii")
    return folder


def write_golden_source(tree: Path, claude: str = "2.1.330", guest: str = "0.1.0") -> None:
    (tree / "images/provision").mkdir(parents=True)
    (tree / "images/golden.yaml").write_text("vmType: vz\n", encoding="utf-8")
    (tree / "images/provision/install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tree / "images/versions.json").write_text(
        json.dumps({"claude_code": claude, "ubuntu_image_digest": "sha256:" + "0" * 64}), encoding="utf-8"
    )
    (tree / "src/agent_guest").mkdir(parents=True)
    (tree / "src/agent_guest/__init__.py").write_text(f'__version__ = "{guest}"\n', encoding="utf-8")
    (tree / "contracts").mkdir()


def runner_doc(*facts: dict[str, Any], ok: bool = True, error: dict[str, Any] | None = None) -> str:
    document: dict[str, Any] = {"ok": ok, "facts": list(facts)}
    if error is not None:
        document["error"] = error
    return json.dumps(document) + "\n"


def status_doc(
    *,
    enrolled: bool = True,
    revoked: str = "no",
    removed: str = "no",
    last_connect: str = "2026-11-24T11:00:00Z",
    sessions: Sequence[str] = (),
) -> str:
    facts: list[dict[str, Any]] = [
        {"fact": "runner.status.identity", "enrolled": enrolled, **({"device_id": "dev-1"} if enrolled else {})},
        {"fact": "runner.status.hub", "last_connect": last_connect, "revoked": revoked, "removed": removed},
    ]
    facts += [{"fact": "runner.status.session", "state": state} for state in sessions]
    return runner_doc(*facts)


def refused(code: str, name: str | None = None) -> AbstractContextManager[Any]:
    """`pytest.raises` for a `RunnerError` with exactly this code and item name."""
    text = code if name is None else f"{code} {name}"
    return pytest.raises(RunnerError, match=f"^{re.escape(text)}$")


# --- helpers of the root helper's, the update's and the build's suites ----------------------------------------------

SUDO = "/usr/bin/sudo"
DSCL = "/usr/bin/dscl"
LAUNCHCTL = "/bin/launchctl"
STAT_CONSOLE = ["/usr/bin/stat", "-f", "%Su", "/dev/console"]
RUNNER_PRINT = [LAUNCHCTL, "print", "system/swiss.dfx.agent-runner"]
MAINTENANCE_PRINT = [LAUNCHCTL, "print", "system/swiss.dfx.agent-runner.maintenance"]
OWNER = "alice"
OWNER_UID = 501
OWNER_GID = 20


def read_user(name: str = OWNER) -> list[str]:
    """The directory-service query of `owner.lookup` for `name`."""
    return [DSCL, ".", "-read", f"/Users/{name}", "UniqueID", "PrimaryGroupID", "NFSHomeDirectory"]


READ_OWNER = read_user()


def known_user(
    run: FakeRun, name: str = OWNER, uid: int = OWNER_UID, gid: int = OWNER_GID, home: str | None = None
) -> None:
    """The directory service knows the account `name` with these IDs and its home `/Users/<name>`."""
    folder = f"/Users/{name}" if home is None else home
    run.answer(read_user(name), stdout=f"UniqueID: {uid}\nPrimaryGroupID: {gid}\nNFSHomeDirectory: {folder}\n")


def unknown_user(run: FakeRun, name: str = OWNER) -> None:
    run.answer(read_user(name), rc=56, stderr="eDSRecordNotFound")


def at_console(run: FakeRun, name: str = OWNER) -> None:
    run.answer(STAT_CONSOLE, stdout=name + "\n")


def owner_of(layout: Layout, name: str = OWNER) -> Owner:
    """The owner as `owner.lookup` finds it after `known_user(run, name)`."""
    return Owner(name, OWNER_UID, OWNER_GID, layout.root / "Users" / name)


def context(kit: Kit, env: dict[str, str] | None = None) -> runnerctl.Context:
    return runnerctl.Context(kit.system, owner_of(kit.layout), {} if env is None else env)


def runner_home(layout: Layout) -> Path:
    """The runner's home below the owner's home."""
    return Layout.runner_home(owner_of(layout).home)


def as_owner(*argv: str) -> list[str]:
    """Argv of a program run by root as the owner (not the runner)."""
    return [SUDO, "-u", OWNER, "-H", "--", *argv]


Reply = str | list[str]


def runner_commands(run: FakeRun, replies: dict[str, Reply]) -> list[str]:
    """Answer `agent-runner <command> … --json` run as the owner by its command (`status`, `image list`,
    `image build`, `enroll`, `uninstall`): a list of replies is consumed in order, the last one repeats; a command
    without a reply prints nothing. Returns the commands in call order (other `sudo` calls are not answered here)."""
    seen: list[str] = []

    def hook(argv: list[str]) -> None:
        if "agent_runner" not in argv:
            return
        args = argv[argv.index("agent_runner") + 1 :]
        assert args[-1] == "--json"
        key = " ".join(args[:2]) if args[0] == "image" else args[0]
        seen.append(key)
        reply = replies.get(key, "")
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        run.answer(argv, stdout=reply)

    run.on(as_owner("/usr/bin/env", "-i"), hook)
    return seen


def make_current(layout: Layout, keys: Keys, version: str = VERSION, **extra: Any) -> Path:
    """`make_version` with `current` pointing at it."""
    folder = make_version(layout, keys, version, **extra)
    layout.current.symlink_to(folder)
    return folder


def safe_chain(layout: Layout) -> None:
    """The payload and every directory above it up to the layout's root: 0755 (independent of the umask)."""
    layout.payload.mkdir(parents=True, exist_ok=True)
    for part in [layout.payload, *layout.payload.parents]:
        part.chmod(0o755)
        if part == layout.root:
            break


def feed_files(
    keys: Keys,
    package: bytes = b"package of the new version",
    *,
    version: str = NEWER,
    signed_at: str = "2026-11-24T08:00:00Z",
    expires: str = "2027-01-24T08:00:00Z",
    hub: str = HUB,
) -> dict[str, bytes]:
    """The download area of `hub` with a signed `latest.json` naming `package` (for `FakeFetch.files`)."""
    name = f"DFX-Agent-Runner-{version}.pkg"
    feed = release.Feed(
        version=version,
        package=name,
        package_sha256=hashlib.sha256(package).hexdigest(),
        manifest_sha256="cd" * 32,
        min_macos="14.0",
        notarized=False,
        signed_at=signed_at,
        expires=expires,
    )
    data = release.canonical(feed.document())
    base = hub + "/downloads/runner/"
    return {
        base + "latest.json": data,
        base + "latest.json.sig": keys.sign(data).encode("ascii"),
        base + name: package,
    }


def steps(run: FakeRun) -> list[str | list[str]]:
    """The calls in order, each `agent-runner` command as `runner <command>` and every other call as its argv."""
    found: list[str | list[str]] = []
    for call in run.calls:
        if "agent_runner" in call:
            found.append("runner " + call[call.index("agent_runner") + 1])
        else:
            found.append(call)
    return found
