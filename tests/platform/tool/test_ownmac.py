"""The agent's own Mac as an execution device (ADR-074 sections 3 to 5): `device add` and `device remove
--this-computer` with a `Machine` whose programs, downloads, clock and layout root are fakes (the administrator step
is a scripted program that writes the installation's public state), against the hub fake."""

from __future__ import annotations

import dataclasses
import getpass
import hashlib
import json
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from agent_platform import errors, ownmac
from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto import invite as invite_format
from agent_runner.errors import RunnerError
from agent_runner.install import admin, release, update
from agent_runner.install.layout import Layout
from agent_runner.install.state import write_state
from agent_runner.install.system import System, fetch_url, run_argv

from .conftest import Tool
from .hubfake import HubFake, User
from .teams import assign, grant_platform_access, private_org, team, wg_create

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)
STARTED = NOW.strftime(update.TIME_FORMAT)
GIB = 1024**3
SEAM = "/seam/admin"
VERSION = "2026.11.24.0800"
PACKAGE = b"package of the runner"
SW_VERS = ["/usr/bin/sw_vers", "-productVersion"]
HV = ["/usr/sbin/sysctl", "-n", "kern.hv_support"]
MEMSIZE = ["/usr/sbin/sysctl", "-n", "hw.memsize"]
USER = "alice"
UNKNOWN_DEVICE = "01a1ee56-5e00-7242-9e24-f87629ad2977"


# --- fakes of the Mac -------------------------------------------------------------------------------------------------


@dataclass
class FakeRun:
    """Scripted programs: the newest answer whose prefix matches wins; hooks run before answering."""

    scripts: list[tuple[tuple[str, ...], tuple[int, str, str]]] = field(default_factory=list)
    hooks: list[tuple[tuple[str, ...], Callable[[list[str]], None]]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def answer(self, prefix: Sequence[str], rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.scripts.insert(0, (tuple(prefix), (rc, stdout, stderr)))

    def on(self, prefix: Sequence[str], hook: Callable[[list[str]], None]) -> None:
        self.hooks.append((tuple(prefix), hook))

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(argv)
        self.calls.append(call)
        for prefix, hook in self.hooks:
            if tuple(call[: len(prefix)]) == prefix:
                hook(call)
        for prefix, (rc, out, err) in self.scripts:
            if tuple(call[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(call, rc, out, err)
        return subprocess.CompletedProcess(call, 0, "", "")

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
        return self.files[url]


@dataclass(frozen=True)
class Keys:
    private: bytes
    public: str

    def sign(self, data: bytes) -> str:
        return release.sign(data, self.private)


def new_keys() -> Keys:
    raw = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return Keys(raw, release.public_key(raw))


def feed_files(
    keys: Keys,
    origin: str,
    package: bytes = PACKAGE,
    *,
    min_macos: str = "14.0",
    signer: Keys | None = None,
) -> dict[str, bytes]:
    name = f"DFX-Agent-Runner-{VERSION}.pkg"
    feed = release.Feed(
        version=VERSION,
        package=name,
        package_sha256=hashlib.sha256(package).hexdigest(),
        manifest_sha256="cd" * 32,
        min_macos=min_macos,
        notarized=False,
        signed_at="2026-11-24T08:00:00Z",
        expires="2027-01-24T08:00:00Z",
    )
    data = release.canonical(feed.document())
    base = origin + "/downloads/runner/"
    return {
        base + "latest.json": data,
        base + "latest.json.sig": (signer or keys).sign(data).encode("ascii"),
        base + name: package,
    }


@dataclass
class Mac:
    machine: ownmac.Machine
    run: FakeRun
    fetch: FakeFetch
    keys: Keys
    slept: list[float]
    folders: list[Path]
    """Every private directory `temp` created."""

    @property
    def layout(self) -> Layout:
        return self.machine.layout

    def system(self, **changes: Any) -> None:
        self.machine.system = dataclasses.replace(self.machine.system, **changes)

    def state(self, **fields: Any) -> None:
        write_state(self.layout.state_file, fields)


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    root = tmp_path / "root"
    root.mkdir()
    spare = tmp_path / "tmp"
    spare.mkdir()
    run = FakeRun()
    run.answer(SW_VERS, stdout="15.1\n")
    run.answer(HV, stdout="1\n")
    run.answer(MEMSIZE, stdout=f"{32 * GIB}\n")
    fetch = FakeFetch()
    keys = new_keys()
    slept: list[float] = []
    folders: list[Path] = []
    ticks = [0.0]

    def clock() -> float:
        ticks[0] += 1.0
        return ticks[0]

    def temp() -> str:
        folder = spare / f"private-{len(folders)}"
        folder.mkdir(mode=0o755)
        folders.append(folder)
        return str(folder)

    system = System(
        layout=Layout(root),
        run=run,
        fetch=fetch,
        env={},
        platform="darwin",
        machine=lambda: "arm64",
        disk_free=lambda path: 500 * GIB,
        now=lambda: NOW,
    )
    machine = ownmac.Machine(
        system,
        admin=[SEAM],
        sleep=slept.append,
        clock=clock,
        keys=lambda: [keys.public],
        user=lambda: USER,
        temp=temp,
    )
    return Mac(machine, run, fetch, keys, slept, folders)


def refused(code: str, **fields: Any) -> Any:
    """Check a raised `PlatformError`'s code and fields."""

    class Expect:
        def __enter__(self) -> None:
            return None

        def __exit__(self, kind: Any, error: Any, tb: Any) -> bool:
            assert isinstance(error, PlatformError), error
            assert error.code == code, error.fields
            for name, value in fields.items():
                assert error.fields.get(name) == value, error.fields
            return True

    return Expect()


@pytest.fixture
def opened(tool: Tool, hub: HubFake, account: User) -> Iterator[Account]:
    found = Account.open(Home.from_env(tool.env))
    try:
        yield found
    finally:
        found.close()


# --- machine_for ------------------------------------------------------------------------------------------------------


def test_machine_for_the_real_mac() -> None:
    machine = ownmac.machine_for({})
    assert machine.admin is None
    assert machine.layout == Layout()
    assert (machine.system.run, machine.system.fetch) == (run_argv, fetch_url)
    assert (machine.keys, machine.clock, machine.user) == (release.packaged_keys, time.monotonic, getpass.getuser)
    folder = Path(machine.temp())
    try:
        assert folder.name.startswith("dfx-agent-runner-")
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    finally:
        folder.rmdir()


def test_machine_for_the_seam(tmp_path: Path) -> None:
    environ = {ownmac.ADMIN_ENV: "/bin/program", ownmac.ROOT_ENV: str(tmp_path)}
    machine = ownmac.machine_for(environ)
    assert machine.admin == ["/bin/program"]
    assert machine.layout == Layout(tmp_path)
    assert machine.system.env == environ
    assert machine.keys() == release.packaged_keys()
    test_key = new_keys().public
    with_keys = ownmac.machine_for({**environ, ownmac.KEYS_ENV: f"{test_key},,"})
    assert with_keys.keys() == [*release.packaged_keys(), test_key]
    assert ownmac.machine_for({ownmac.KEYS_ENV: test_key}).keys is release.packaged_keys


@pytest.mark.parametrize("name", [ownmac.ADMIN_ENV, ownmac.ROOT_ENV])
def test_machine_for_needs_both_seams(name: str) -> None:
    with refused("invalid_argument", reason="device_seam"):
        ownmac.machine_for({name: "/x"})


# --- check_mac --------------------------------------------------------------------------------------------------------


def test_check_mac_passes(mac: Mac) -> None:
    checks = ownmac.check_mac(mac.machine)
    assert [c["name"] for c in checks] == ["os", "macos", "arch", "virtualization", "memory", "disk"]
    assert all(c["ok"] is True and "code" not in c for c in checks)
    assert mac.run.called(ownmac.OPEN) == []


@pytest.mark.parametrize(
    ("change", "code", "target"),
    [
        (lambda m: m.system(platform="linux"), "unsupported_os", None),
        (lambda m: m.run.answer(SW_VERS, stdout="13.6\n"), "macos_too_old", ownmac.OPENERS["macos_too_old"]),
        (lambda m: m.system(machine=lambda: "x86_64"), "not_apple_silicon", None),
        (lambda m: m.run.answer(HV, stdout="0\n"), "virtualization_unavailable", None),
        (lambda m: m.run.answer(MEMSIZE, stdout=f"{8 * GIB}\n"), "memory_too_small", None),
        (lambda m: m.system(disk_free=lambda path: 10 * GIB), "disk_too_full", ownmac.OPENERS["disk_too_full"]),
    ],
)
def test_check_mac_failures(mac: Mac, change: Callable[[Mac], None], code: str, target: str | None) -> None:
    change(mac)
    with refused("mac_unsupported", reason=code, opened=target is not None):
        ownmac.check_mac(mac.machine)
    assert mac.run.called(ownmac.OPEN) == ([] if target is None else [[ownmac.OPEN, target]])


# --- fetch_release ----------------------------------------------------------------------------------------------------

ORIGIN = "https://hub.example"


def test_fetch_release_verifies_feed_and_package(mac: Mac) -> None:
    mac.fetch.files = feed_files(mac.keys, ORIGIN)
    feed, package = ownmac.fetch_release(mac.machine, ORIGIN + "/")
    assert (feed.version, feed.package, package) == (VERSION, f"DFX-Agent-Runner-{VERSION}.pkg", PACKAGE)
    assert feed.package_sha256 == hashlib.sha256(PACKAGE).hexdigest()
    assert mac.fetch.urls[-1] == f"{ORIGIN}/downloads/runner/{feed.package}"


def test_fetch_release_without_a_known_macos_version(mac: Mac) -> None:
    mac.fetch.files = feed_files(mac.keys, ORIGIN, min_macos="99.0")
    mac.run.answer(SW_VERS, rc=1)
    assert ownmac.fetch_release(mac.machine, ORIGIN)[1] == PACKAGE


def test_fetch_release_expired(mac: Mac) -> None:
    mac.fetch.files = feed_files(mac.keys, ORIGIN)
    mac.system(now=lambda: datetime(2027, 2, 1, tzinfo=UTC))
    with refused("release_unavailable", reason="feed_expired"):
        ownmac.fetch_release(mac.machine, ORIGIN)


@pytest.mark.parametrize("minimum", ["15.2", "16"])
def test_fetch_release_needs_a_newer_macos(mac: Mac, minimum: str) -> None:
    mac.fetch.files = feed_files(mac.keys, ORIGIN, min_macos=minimum)
    with refused("mac_unsupported", reason="macos_too_old", opened=False):
        ownmac.fetch_release(mac.machine, ORIGIN)
    assert mac.run.called(ownmac.OPEN) == []
    assert not any(url.endswith(".pkg") for url in mac.fetch.urls)


def test_fetch_release_refusals(mac: Mac) -> None:
    mac.fetch.files = feed_files(mac.keys, ORIGIN, signer=new_keys())
    with refused("release_unavailable", reason="release_signature_invalid"):
        ownmac.fetch_release(mac.machine, ORIGIN)
    mac.fetch.files = feed_files(mac.keys, ORIGIN)
    mac.machine.keys = lambda: []
    with refused("release_unavailable", reason="release_key_missing"):
        ownmac.fetch_release(mac.machine, ORIGIN)
    mac.machine.keys = lambda: [mac.keys.public]
    del mac.fetch.files[f"{ORIGIN}/downloads/runner/DFX-Agent-Runner-{VERSION}.pkg"]
    with refused("release_unavailable", reason="download_failed"):
        ownmac.fetch_release(mac.machine, ORIGIN)
    mac.fetch.files = feed_files(mac.keys, ORIGIN)
    mac.fetch.files[f"{ORIGIN}/downloads/runner/DFX-Agent-Runner-{VERSION}.pkg"] = b"another package"
    with refused("release_unavailable", reason="package_digest_mismatch"):
        ownmac.fetch_release(mac.machine, ORIGIN)


# --- run_admin and wait_for -------------------------------------------------------------------------------------------

ARGS = ("/tmp/p/DFX.pkg", "ab" * 32, "/tmp/p/enroll.request")


def test_run_admin_through_osascript(mac: Mac) -> None:
    mac.machine.admin = None
    mac.run.answer([admin.OSASCRIPT], stdout="installed\n")
    assert ownmac.run_admin(mac.machine, True, ARGS) == "installed\n"
    argv = mac.run.calls[-1]
    assert argv == admin.install_argv(*ARGS, layout=mac.layout)
    assert argv[0] == admin.OSASCRIPT
    assert argv[-5:] == [admin.install_script(mac.layout), *ARGS, admin.INSTALL_PROMPT]
    assert str(mac.layout.spool) in argv[-5]
    ownmac.run_admin(mac.machine, False, ("", "", ""))
    argv = mac.run.calls[-1]
    assert argv == admin.uninstall_argv(mac.layout)
    assert argv[-5:] == [admin.uninstall_script(mac.layout), "", "", "", admin.UNINSTALL_PROMPT]


def test_run_admin_through_the_seam(mac: Mac) -> None:
    ownmac.run_admin(mac.machine, True, ARGS)
    ownmac.run_admin(mac.machine, False, ("", "", ""))
    assert mac.run.calls == [[SEAM, "install", *ARGS], [SEAM, "uninstall", "", "", ""]]


@pytest.mark.parametrize("stderr", ["execution error: User canceled. (-128)", "0:1: (-128)", "User canceled"])
def test_run_admin_cancelled(mac: Mac, stderr: str) -> None:
    mac.run.answer([SEAM], rc=1, stderr=stderr)
    with refused("admin_cancelled"):
        ownmac.run_admin(mac.machine, True, ARGS)


@pytest.mark.parametrize(("install", "reason"), [(True, "admin_step"), (False, "removal_step")])
def test_run_admin_failed(mac: Mac, install: bool, reason: str) -> None:
    mac.run.answer([SEAM], rc=3, stderr="installer: failed")
    with refused("install_failed", reason=reason):
        ownmac.run_admin(mac.machine, install, ARGS)


SUDO_TRUE = [admin.SUDO, "-n", "true"]


def real_step(mac: Mac, *, euid: int = 501, sudo: bool) -> None:
    """The machine without the seam: the caller's user ID and whether `sudo -n true` succeeds."""
    mac.machine.admin = None
    mac.machine.system.geteuid = lambda: euid
    mac.run.answer(SUDO_TRUE, rc=0 if sudo else 1, stderr="" if sudo else "sudo: a password is required")


@pytest.mark.parametrize(("euid", "sudo", "way"), [(0, False, "root"), (501, True, "sudo"), (501, False, "dialog")])
def test_admin_way(mac: Mac, euid: int, sudo: bool, way: str) -> None:
    real_step(mac, euid=euid, sudo=sudo)
    assert ownmac.admin_way(mac.machine) == way
    # root never asks sudo; anyone else asks without ever prompting (-n)
    assert mac.run.called(*SUDO_TRUE) == ([] if euid == 0 else [SUDO_TRUE])


def test_admin_way_of_the_seam_is_the_dialog_it_stands_in_for(mac: Mac) -> None:
    mac.machine.system.geteuid = lambda: 0
    assert ownmac.admin_way(mac.machine) == "dialog"
    assert mac.run.calls == []


@pytest.mark.parametrize(("way", "prefix"), [("sudo", [admin.SUDO, "-n"]), ("root", [])])
def test_run_admin_without_a_dialog(mac: Mac, way: str, prefix: list[str]) -> None:
    real_step(mac, sudo=True)
    ownmac.run_admin(mac.machine, True, ARGS, way)
    script = admin.install_script(mac.layout)
    assert mac.run.calls[-1] == [*prefix, admin.SHELL, "-c", script, "dfx-agent-runner", *ARGS]
    ownmac.run_admin(mac.machine, False, ("", "", ""), way)
    script = admin.uninstall_script(mac.layout)
    assert mac.run.calls[-1] == [*prefix, admin.SHELL, "-c", script, "dfx-agent-runner", "", "", ""]
    assert mac.run.called(admin.OSASCRIPT) == []


def test_run_admin_without_a_dialog_failed(mac: Mac) -> None:
    real_step(mac, sudo=True)
    mac.run.answer([admin.SUDO, "-n", admin.SHELL], rc=1, stderr="sudo: a password is required")
    with refused("install_failed", reason="admin_step"):
        ownmac.run_admin(mac.machine, True, ARGS, "sudo")


def test_wait_for_a_newer_accepted_state(mac: Mac) -> None:
    mac.state(updated_at="2026-11-24T11:59:59Z", enrollment="enrolled")
    steps = [
        lambda: mac.state(updated_at=STARTED, enrollment="waiting"),
        lambda: mac.state(updated_at="2026-11-24T12:00:05Z", enrollment="enrolled", device_id="d"),
    ]

    def sleep(seconds: float) -> None:
        mac.slept.append(seconds)
        steps.pop(0)()

    mac.machine.sleep = sleep
    found = ownmac.wait_for(mac.machine, lambda state: state.get("enrollment") == "enrolled", STARTED)
    assert (found["device_id"], found["v"]) == ("d", 1)
    assert mac.slept == [ownmac.POLL_S, ownmac.POLL_S]


def test_wait_for_times_out(mac: Mac) -> None:
    ticks = iter([0.0, 10.0, ownmac.WAIT_S - 1, ownmac.WAIT_S])
    mac.machine.clock = lambda: next(ticks)
    with refused("install_failed", reason="timeout"):
        ownmac.wait_for(mac.machine, lambda state: True, STARTED)
    assert mac.slept == [ownmac.POLL_S, ownmac.POLL_S]


# --- device add -------------------------------------------------------------------------------------------------------


@dataclass
class Seen:
    argv: list[str] = field(default_factory=list)
    package: bytes = b""
    code: str = ""
    request_mode: int = 0
    folder_mode: int = 0


def installs(mac: Mac, **final: Any) -> Seen:
    """The administrator step writes the installation's public state (`final`, else enrolled as `dev-1`)."""
    seen = Seen()

    def hook(argv: list[str]) -> None:
        package, digest, request = argv[2:]
        seen.argv = argv
        seen.package = Path(package).read_bytes()
        assert hashlib.sha256(seen.package).hexdigest() == digest
        seen.code = Path(request).read_text(encoding="utf-8")
        seen.request_mode = stat.S_IMODE(Path(request).stat().st_mode)
        seen.folder_mode = stat.S_IMODE(Path(request).parent.stat().st_mode)
        fields = final or {"enrolled": True, "enrollment": "enrolled", "device_id": "dev-1"}
        mac.state(updated_at=STARTED, **fields)

    mac.run.on([SEAM, "install"], hook)
    return seen


def invite_of(hub: HubFake, org: str, code: str) -> Any:
    invite_id = invite_format.invite_secrets(invite_format.parse_code(code.strip())).invite_id
    return hub.orgs[org].log.state.invites[invite_id]


def test_device_add_to_the_default_workgroup(mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    org = private_org(hub, account)
    default = hub.orgs[org].log.state.default_wg
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    seen = installs(mac, enrolled=True, enrollment="enrolled", device_id="dev-1", version=VERSION, daemon="running")
    result = ownmac.device_add(opened, mac.machine, None, [])
    folder = mac.folders[0]
    assert seen.argv == [
        SEAM,
        "install",
        str(folder / f"DFX-Agent-Runner-{VERSION}.pkg"),
        hashlib.sha256(PACKAGE).hexdigest(),
        str(folder / "enroll.request"),
    ]
    assert seen.package == PACKAGE
    assert (seen.request_mode, seen.folder_mode) == (0o600, 0o700)
    assert seen.code.endswith("\n")
    assert seen.code.count("\n") == 1
    found = invite_of(hub, org, seen.code)
    assert (found.kind, dict(found.wgs)) == ("device", {default: "device"})
    assert not folder.exists()
    assert result == {
        "device_id": "dev-1",
        "org_id": org,
        "workgroups": [default],
        "version": VERSION,
        "daemon": "running",
        "golden": "unknown",
        "owner": "",
        "checks": result["checks"],
        "admin": "dialog",
        "dialog": admin.INSTALL_SENTENCE,
        "notice": ownmac.NOTICE,
    }
    assert next(check["name"] for check in result["checks"]) == "os"
    assert "every task" in result["notice"]
    assert seen.code.strip() not in json.dumps(result)
    assert mac.run.called(ownmac.OPEN) == []


def test_device_add_without_a_dialog_through_sudo(mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    org = private_org(hub, account)
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    real_step(mac, sudo=True)
    seen = Seen()

    def hook(argv: list[str]) -> None:
        seen.argv = argv
        seen.code = Path(argv[-1]).read_text(encoding="utf-8")
        mac.state(updated_at=STARTED, enrolled=True, enrollment="enrolled", device_id="dev-3", owner=USER)

    mac.run.on([admin.SUDO, "-n", admin.SHELL], hook)
    result = ownmac.device_add(opened, mac.machine, None, [])
    folder = mac.folders[0]
    package = str(folder / f"DFX-Agent-Runner-{VERSION}.pkg")
    script = admin.install_script(mac.layout)
    digest = hashlib.sha256(PACKAGE).hexdigest()
    request = str(folder / "enroll.request")
    assert seen.argv == [admin.SUDO, "-n", admin.SHELL, "-c", script, "dfx-agent-runner", package, digest, request]
    assert invite_of(hub, org, seen.code).kind == "device"
    assert (result["device_id"], result["owner"], result["admin"]) == ("dev-3", USER, "sudo")
    assert result["dialog"] == admin.DIRECT_INSTALL_SENTENCE
    assert mac.run.called(admin.OSASCRIPT) == []


@pytest.mark.parametrize(("euid", "reason"), [(0, "owner_root"), (500, "owner_not_regular")])
def test_device_add_by_an_account_that_cannot_own_the_device_is_refused_first(  # noqa: PLR0917 - fixtures
    mac: Mac, hub: HubFake, account: User, opened: Account, euid: int, reason: str
) -> None:
    private_org(hub, account)
    real_step(mac, euid=euid, sudo=True)
    calls = len(mac.run.calls)
    with refused("invalid_argument", reason=reason):
        ownmac.device_add(opened, mac.machine, None, [])
    assert len(mac.run.calls) == calls  # not even the checks of the Mac or sudo
    assert mac.fetch.urls == []
    assert mac.folders == []
    assert "Run `device add` as" in str(PlatformError("invalid_argument", reason=reason).to_json()["next"])


def test_device_add_to_named_workgroups(tool: Tool, mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    seen = installs(mac, enrolled=True, enrollment="enrolled", device_id="dev-2", disabled_in_login_items=True)
    # two orgs: without --org or --wg the org is ambiguous
    with refused("invalid_argument", reason="org_ambiguous"):
        ownmac.device_add(opened, mac.machine, None, [])
    result = ownmac.device_add(opened, mac.machine, None, [wg])
    assert (result["org_id"], result["workgroups"], result["device_id"]) == (org, [wg], "dev-2")
    assert result["version"] == VERSION
    assert dict(invite_of(hub, org, seen.code).wgs) == {wg: "device"}
    assert mac.run.called(ownmac.OPEN) == [[ownmac.OPEN, ownmac.LOGIN_ITEMS]]
    # with --org given, the default workgroup of that org
    mac.layout.state_file.unlink()
    default = hub.orgs[org].log.state.default_wg
    assert ownmac.device_add(opened, mac.machine, org, [])["workgroups"] == [default]
    assert dict(invite_of(hub, org, seen.code).wgs) == {default: "device"}
    mac.layout.state_file.unlink()
    with refused("invalid_argument"):
        ownmac.device_add(opened, mac.machine, None, ["not-a-uuid"])


def test_device_add_without_an_org(mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    def no_orgs(body: Any, request: Any) -> tuple[int, Any]:
        status, document = hub.r_me(request, body)
        return status, {**document, "orgs": []}

    hub.hooks[("GET", "/v1/me")] = no_orgs
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    with refused("no_org"):
        ownmac.device_add(opened, mac.machine, None, [])
    assert mac.folders == []


def test_device_add_refusals_before_the_dialog(mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    mac.run.answer(HV, stdout="0\n")
    with refused("mac_unsupported", reason="virtualization_unavailable"):
        ownmac.device_add(opened, mac.machine, None, [])
    mac.run.answer(HV, stdout="1\n")
    mac.state(enrolled=True, device_id="dev-9")
    with refused("device_installed", device_id="dev-9"):
        ownmac.device_add(opened, mac.machine, None, [])
    # an installation that runs as another account is never enrolled with this user's org
    mac.state(enrolled=False, enrollment="failed", owner="bob")
    with refused("device_installed", reason="other_owner"):
        ownmac.device_add(opened, mac.machine, None, [])
    assert mac.fetch.urls == []
    mac.state(enrolled=False, enrollment="failed", owner=USER)
    with refused("release_unavailable", reason="download_failed"):
        ownmac.device_add(opened, mac.machine, None, [])
    mac.state(enrolled=False, enrollment="failed")
    with refused("release_unavailable", reason="download_failed"):
        ownmac.device_add(opened, mac.machine, None, [])
    assert mac.folders == []
    assert mac.run.called(SEAM) == []


@pytest.mark.parametrize(
    ("stderr", "code"), [("installer: failed", "install_failed"), ("User canceled (-128)", "admin_cancelled")]
)
def test_device_add_removes_the_private_directory_when_the_dialog_fails(  # noqa: PLR0917 - fixtures and parameters
    mac: Mac, hub: HubFake, account: User, opened: Account, stderr: str, code: str
) -> None:
    org = private_org(hub, account)
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    mac.run.answer([SEAM, "install"], rc=1, stderr=stderr)
    with refused(code):
        ownmac.device_add(opened, mac.machine, None, [])
    assert len(mac.folders) == 1
    assert not mac.folders[0].exists()
    # the invitation of the failed dialog is revoked at once (nothing open is left for its hour)
    (invite,) = hub.orgs[org].log.state.invites.values()
    assert invite.kind == "device"
    assert invite.revoked_seq is not None


@pytest.mark.parametrize(
    ("final", "reason"),
    [
        ({"enrolled": False, "enrollment": "failed", "enrollment_error": "invite_expired"}, "invite_expired"),
        ({"enrolled": False, "enrollment": "revoked"}, "revoked"),
        ({"enrolled": False}, "None"),
    ],
)
def test_device_add_enrollment_failed(
    mac: Mac, hub: HubFake, opened: Account, final: dict[str, Any], reason: str
) -> None:
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    installs(mac, **final)
    with refused("install_failed", reason=reason):
        ownmac.device_add(opened, mac.machine, None, [])


def test_device_add_waits_while_the_enrollment_waits(mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    installs(mac, enrolled=False, enrollment="waiting")

    def later(seconds: float) -> None:
        mac.slept.append(seconds)
        mac.state(updated_at=STARTED, enrolled=True, enrollment="enrolled", device_id="dev-3", golden="ready")

    mac.machine.sleep = later
    result = ownmac.device_add(opened, mac.machine, None, [])
    assert (result["device_id"], result["golden"], mac.slept) == ("dev-3", "ready", [ownmac.POLL_S])


def test_device_add_times_out(mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    mac.fetch.files = feed_files(mac.keys, hub.origin)
    mac.run.on([SEAM, "install"], lambda argv: None)
    ticks = iter([0.0, 600.0, 1200.0])
    mac.machine.clock = lambda: next(ticks)
    with refused("install_failed", reason="timeout"):
        ownmac.device_add(opened, mac.machine, None, [])
    assert mac.slept == [ownmac.POLL_S]
    assert not mac.folders[0].exists()


# --- device remove ----------------------------------------------------------------------------------------------------

REMOVED = '{"ok": true, "removed": ["/Library/Application Support/DFXAgentRunner"], "leftovers": ["/Volumes/P/a/x"]}'


def test_device_remove_not_installed(mac: Mac, opened: Account) -> None:
    with refused("not_installed"):
        ownmac.device_remove(opened, mac.machine)
    assert mac.run.calls == []


def test_device_remove_unpairs_and_removes(tool: Tool, mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, _ = assign(tool, hub, wg)
    assert hub.orgs[org].log.state.member(device) is not None
    mac.state(enrolled=True, device_id=device)
    mac.run.answer([SEAM, "uninstall"], stdout="stopping the runner\n{not the result\n" + REMOVED + "\n")
    result = ownmac.device_remove(opened, mac.machine)
    assert hub.orgs[org].log.state.member(device) is None
    assert opened.org_log(org).state.member(device) is None
    assert (result["device_id"], result["user_id"], result["unpaired"]) == (device, device, True)
    assert result["changed"] is True
    assert result["removed"] == ["/Library/Application Support/DFXAgentRunner"]
    assert result["leftovers"] == ["/Volumes/P/a/x"]
    assert (result["admin"], result["dialog"]) == ("dialog", admin.UNINSTALL_SENTENCE)
    assert mac.run.calls[-1] == [SEAM, "uninstall", "", "", ""]


@pytest.mark.parametrize(("euid", "way"), [(0, "root"), (501, "sudo")])
def test_device_remove_without_a_dialog(mac: Mac, opened: Account, euid: int, way: str) -> None:
    mac.state(enrolled=False, device_id="d")
    real_step(mac, euid=euid, sudo=True)
    mac.run.answer([admin.SUDO, "-n", admin.SHELL] if euid else [admin.SHELL], stdout=REMOVED + "\n")
    result = ownmac.device_remove(opened, mac.machine)
    assert (result["admin"], result["dialog"]) == (way, admin.DIRECT_UNINSTALL_SENTENCE)
    assert result["leftovers"] == ["/Volumes/P/a/x"]
    assert mac.run.called(admin.OSASCRIPT) == []


@pytest.mark.parametrize(
    "fields", [{"enrolled": True, "device_id": UNKNOWN_DEVICE}, {"enrolled": False, "device_id": "d"}, {}]
)
def test_device_remove_without_a_device_to_unpair(
    mac: Mac, hub: HubFake, account: User, opened: Account, fields: dict[str, Any]
) -> None:
    mac.state(**fields)
    mac.run.answer([SEAM, "uninstall"], stdout=REMOVED + "\n")
    seq = {org: item.log.head.seq for org, item in hub.orgs.items()}
    result = ownmac.device_remove(opened, mac.machine)
    assert result == {
        "device_id": fields.get("device_id", ""),
        "unpaired": False,
        "removed": ["/Library/Application Support/DFXAgentRunner"],
        "leftovers": ["/Volumes/P/a/x"],
        "admin": "dialog",
        "dialog": admin.UNINSTALL_SENTENCE,
    }
    assert {org: item.log.head.seq for org, item in hub.orgs.items()} == seq


def test_device_remove_with_the_device_in_two_orgs(
    tool: Tool, mac: Mac, hub: HubFake, account: User, opened: Account
) -> None:
    device, seq = assign(tool, hub, hub.orgs[private_org(hub, account)].log.state.default_wg)
    shared = team(tool)
    grant_platform_access(hub, shared)
    assign(tool, hub, hub.orgs[shared].log.state.default_wg, device, seq)
    mac.state(enrolled=True, device_id=device)
    mac.run.answer([SEAM, "uninstall"], stdout=REMOVED + "\n")
    with refused("invalid_argument", reason="org_ambiguous"):
        ownmac.device_remove(opened, mac.machine)
    # the unpairing comes first: an ambiguous one is left to the user and no dialog is shown
    assert mac.run.calls == []


@pytest.mark.parametrize(
    "stdout", ["", "removed everything\n", "{broken\n", '{"ok": false, "removed": []}\n', '{"removed": []}\n']
)
def test_device_remove_without_a_result(mac: Mac, opened: Account, stdout: str) -> None:
    mac.state(enrolled=False)
    mac.run.answer([SEAM, "uninstall"], stdout=stdout)
    with refused("install_failed", reason="removal_step"):
        ownmac.device_remove(opened, mac.machine)


def test_device_remove_cancelled(tool: Tool, mac: Mac, hub: HubFake, account: User, opened: Account) -> None:
    org = private_org(hub, account)
    device, _ = assign(tool, hub, hub.orgs[org].log.state.default_wg)
    mac.state(enrolled=True, device_id=device)
    mac.run.answer([SEAM, "uninstall"], rc=1, stderr="User canceled")
    with refused("admin_cancelled"):
        ownmac.device_remove(opened, mac.machine)
    # the device was unpaired before the dialog; a cancelled dialog does not pair it again
    assert hub.orgs[org].log.state.member(device) is None
    assert mac.run.calls == [[SEAM, "uninstall", "", "", ""]]


# --- command line -----------------------------------------------------------------------------------------------------

ADMIN_SCRIPT = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "admin.log"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print("removing")
print(json.dumps({{"ok": True, "removed": ["payload"], "leftovers": []}}))
"""


def test_commands_are_registered(tool: Tool) -> None:
    code, document = tool.run("device", "add", "--help")
    assert code == 0
    assert "--this-computer" in document["help"]
    assert "--wg" in document["help"]
    assert "--org" in document["help"]
    assert "--this-computer" in tool.run("device", "remove", "--help")[1]["help"]
    assert tool.run("device", "remove")[1]["error"] == "invalid_argument"


def test_cli_remove_through_the_seam(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    root = tmp_path / "layout"
    program = tmp_path / "seam" / "admin"
    program.parent.mkdir()
    program.write_text(ADMIN_SCRIPT.format(python=sys.executable))
    program.chmod(0o700)
    env = {ownmac.ADMIN_ENV: str(program), ownmac.ROOT_ENV: str(root)}
    code, document = tool.run("device", "remove", "--this-computer", env=env)
    assert (code, document["error"]) == (1, "not_installed")
    assert document["next"] == errors.NEXT["not_installed"]
    write_state(Layout(root).state_file, {"enrolled": False})
    code, document = tool.run("device", "remove", "--this-computer", env=env)
    assert code == 0, document
    assert (document["ok"], document["unpaired"], document["removed"]) == (True, False, ["payload"])
    assert (program.parent / "admin.log").read_text().splitlines() == [json.dumps(["uninstall", "", "", ""])]


def test_cli_add_needs_both_seams(tool: Tool, hub: HubFake, account: User) -> None:
    code, document = tool.run("device", "add", "--this-computer", env={ownmac.ADMIN_ENV: "/x"})
    assert (code, document["error"], document["reason"]) == (1, "invalid_argument", "device_seam")


def test_new_error_codes_have_their_next_step() -> None:
    for code in (
        "mac_unsupported",
        "release_unavailable",
        "admin_cancelled",
        "install_failed",
        "device_installed",
        "not_installed",
    ):
        assert code in errors.CODES
        assert errors.NEXT[code]
        assert PlatformError(code, reason="x").to_json()["error"] == code
    with pytest.raises(ValueError, match="unknown error code"):
        PlatformError("mac_too_old")


class _DescribingTree:
    """A tree whose commands take search words and an example (as after KOR-63)."""

    def command(
        self, group: object, name: str, work: object, help_text: str, *, writes: bool = False, example: str, words: str
    ) -> None:
        raise AssertionError("not called")


def test_lookup_fields_go_only_to_a_tree_that_takes_them(tool: Tool) -> None:
    plain = ownmac.lookup(cast(Any, object.__new__(_PlainTree)), ownmac.ADD_LOOKUP)
    described = ownmac.lookup(cast(Any, _DescribingTree()), ownmac.ADD_LOOKUP)
    assert plain == {}
    assert described == ownmac.ADD_LOOKUP
    assert set(ownmac.REMOVE_LOOKUP) == {"example", "words"}


class _PlainTree:
    def command(self, group: object, name: str, work: object, help_text: str, *, writes: bool = False) -> None:
        raise AssertionError("not called")
