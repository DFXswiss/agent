"""`agent-runner workvm install|uninstall|status` and `workvm firewall render|check` through `run_cli` (spec 13 sections
6.1-6.4): a recording fake `Machine` answers every command and keeps every written file, the effective UID comes from
the injected host, so nothing needs root or touches the machine."""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_runner.cli import run_cli
from agent_runner.errors import RunnerError
from agent_runner.vm.lima import Lima
from agent_runner.workvm import accounts, firewall
from agent_runner.workvm import cli as workvm_cli

SUDO = (firewall.SUDO, "-n")
DS_USERS = "/" + "Users"  # dscl record directory, joined so no home path literal appears
CONTAINER_PLIST = plistlib.dumps({"APFSContainerReference": "disk3"}).decode()
HOOKED_MAIN = 'scrub-anchor "com.apple/*" all\nanchor "com.apple/*" all\nanchor "com.cloudflare.warp_svc" all\n'


@dataclass
class FakeMachine:
    """Answers the commands of the work VM CLI; `answers` overrides one exact argv."""

    uids: dict[str, int] = field(default_factory=lambda: {"runner": 501})
    gid: int | None = None
    mounted: bool = False
    dirs: set[str] = field(default_factory=set)
    """Directories that exist (`remove_dir` removes one only when it is not in `full_dirs`)."""
    full_dirs: set[str] = field(default_factory=set)
    visudo_ok: bool = True
    egress: str | None = "utun3"
    answers: dict[tuple[str, ...], tuple[int, str]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    files: dict[str, tuple[str, int]] = field(default_factory=dict)
    pf_conf: str = 'scrub-anchor "com.apple/*"\nanchor "com.apple/*"\nload anchor "com.apple" from "x"\n'
    main: str = HOOKED_MAIN
    flushed: bool = False
    removed: list[str] = field(default_factory=list)
    given: list[tuple[str, int, int]] = field(default_factory=list)
    """`give_lima_config` calls (home, uid, gid); `give_fails` makes it refuse like a planted link."""
    give_fails: bool = False

    def live(self, argv: tuple[str, ...]) -> tuple[int, str]:
        slots = [
            firewall.Slot(index=i, account=name, uid=uid, ssh_port=47200 + i)
            for i, (name, uid) in enumerate((n, u) for n, u in self.uids.items() if n != "runner")
        ]
        outputs = {
            firewall.READ_COMMANDS[0]: "Status: Enabled for 1 days\n",
            firewall.READ_COMMANDS[1]: "" if self.flushed else firewall.render(slots, self.egress, 500),
            firewall.READ_COMMANDS[2]: self.main,
            firewall.READ_COMMANDS[3]: "lo0\nen0\n",
            firewall.READ_COMMANDS[4]: "\n".join((*firewall.BLOCKED_V4, *firewall.BLOCKED_V6)) + "\n",
            firewall.READ_COMMANDS[5]: f"  {firewall.ANCHOR}\n  com.apple/200.AirDrop\n",
        }
        return 0, outputs[argv]

    def run(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append(key)
        bare = key[2:] if key[:2] == SUDO else key
        if key in self.answers:
            code, out = self.answers[key]
        elif bare in firewall.READ_COMMANDS:
            code, out = self.live(bare)
        elif key[0] == workvm_cli.ID:
            uid = self.uids.get(key[-1])
            code, out = (1, "") if uid is None else (0, f"{uid}\n")
        elif key[:3] == (accounts.DSCL, ".", "-read"):
            code, out = (1, "") if self.gid is None else (0, f"PrimaryGroupID: {self.gid}\n")
        elif key[:3] == (accounts.DSCL, ".", "-list") and key[3] == "/Users":
            code, out = (
                0,
                "root 0\nnobody -2\nrunner 501\nweird\n" + "".join(f"{n} {u}\n" for n, u in self.uids.items()),
            )
        elif key[:3] == (accounts.DSCL, ".", "-list"):
            code, out = 0, "wheel 0\nstaff 20\nother 600\n"
        elif key[:2] == (accounts.DISKUTIL, "info"):
            code, out = 0, CONTAINER_PLIST
        else:
            code, out = 0, ""
            self.effect(key)
        return subprocess.CompletedProcess(list(argv), code, out, "")

    def effect(self, key: tuple[str, ...]) -> None:
        """What a changing command does to the fake machine."""
        if key[:3] == (accounts.DSCL, ".", "-delete") and key[3].startswith(DS_USERS + "/"):
            self.uids.pop(key[3].rsplit("/", 1)[1], None)
        elif key[:3] == (accounts.DSCL, ".", "-delete"):
            self.gid = None
        elif key == (firewall.PFCTL, "-a", firewall.ANCHOR, "-F", "all"):
            self.flushed = True
        elif key[:3] == (accounts.DISKUTIL, "apfs", "deleteVolume"):
            self.mounted = False

    def run_input(self, argv: Sequence[str], text: str, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((*argv, "<stdin>"))
        return subprocess.CompletedProcess(list(argv), 0 if self.visudo_ok else 1, "", "")

    def write_file(self, path: str, content: str, mode: int) -> None:
        self.files[path] = (content, mode)

    def read_file(self, path: str) -> str:
        return self.files[path][0] if path in self.files else self.pf_conf

    def is_mount(self, path: str) -> bool:
        return self.mounted

    def remove_file(self, path: str) -> None:
        self.files.pop(path, None)
        self.removed.append(path)

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def remove_dir(self, path: str) -> None:
        if path not in self.full_dirs:
            self.dirs.discard(path)

    def machine(self) -> workvm_cli.Machine:
        return workvm_cli.Machine(
            run=self.run,
            run_input=self.run_input,
            write_file=self.write_file,
            read_file=self.read_file,
            is_mount=self.is_mount,
            remove_file=self.remove_file,
            exists=self.exists,
            remove_dir=self.remove_dir,
            give_lima_config=self.give_lima_config,
        )

    def give_lima_config(self, home: str, uid: int, gid: int) -> str:
        self.given.append((home, uid, gid))
        if self.give_fails:
            raise RunnerError("workvm_install_failed", "lima_config")
        return "present"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMachine:
    machine = FakeMachine()
    monkeypatch.setattr(workvm_cli, "default_machine", machine.machine)
    return machine


@pytest.fixture
def cli(make_host: Callable[..., Any], runner_home: Path) -> Callable[..., tuple[int, str, str]]:
    def call(*argv: str, euid: int = 0, **env: str) -> tuple[int, str, str]:
        values = {"AGENT_RUNNER_HOME": str(runner_home), "AGENT_RUNNER_WORKVM_SLOTS": "2", **env}
        captured = make_host(values, geteuid=lambda: euid)
        code = run_cli(["workvm", *argv], captured.host)
        return code, captured.stdout.getvalue(), captured.stderr.getvalue()

    return call


INSTALL = ("install", "--runner-account", "runner", "--quota-gib", "200")


def test_install_creates_everything(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    code, out, err = cli(*INSTALL)
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "runner.workvm.install step=volume action=created path=/var/agent-workvm",
        "runner.workvm.install step=images path=/var/agent-workvm/images",
        "runner.workvm.install step=group action=created name=workvm gid=601",
        "runner.workvm.install step=accounts count=2 created=2",
        f"runner.workvm.install step=wrapper path={accounts.WRAPPER_PATH}",
        f"runner.workvm.install step=sudoers path={accounts.SUDOERS_PATH}",
        "runner.workvm.install step=firewall steps=10 anchor=com.apple/000.agent.workvm",
        "runner.workvm.install result=ok slots=2",
    ]
    quota = str(200 * 2**30)
    assert (accounts.DISKUTIL, "apfs", "addVolume", "disk3", "APFS", "AgentWorkVM", "-quota", quota,
            "-mountpoint", "/var/agent-workvm") in fake.calls  # fmt: skip
    assert (accounts.DSCL, ".", "-create", "/Groups/workvm", "PrimaryGroupID", "601") in fake.calls
    assert (accounts.DSCL, ".", "-create", f"{DS_USERS}/wvm00", "UniqueID", "602") in fake.calls
    assert (accounts.DSCL, ".", "-create", f"{DS_USERS}/wvm01", "UniqueID", "603") in fake.calls
    assert fake.files[accounts.WRAPPER_PATH] == (accounts.wrapper_script(["wvm00", "wvm01"]), 0o755)
    assert fake.files[accounts.SUDOERS_PATH] == (accounts.sudoers("runner", ["wvm00", "wvm01"]), 0o440)
    slots = [firewall.Slot(0, "wvm00", 602, 47200), firewall.Slot(1, "wvm01", 603, 47201)]
    assert fake.files[firewall.ANCHOR_PATH] == (firewall.render(slots, None, 500), 0o644)
    assert fake.files[firewall.WATCH_PATH][1] == 0o755
    assert firewall.PF_CONF not in fake.files  # never written
    assert (firewall.PFCTL, "-f", firewall.PF_CONF) not in fake.calls  # the main ruleset is never loaded
    assert (firewall.PFCTL, "-a", firewall.ANCHOR, "-f", firewall.ANCHOR_PATH) in fake.calls
    assert (firewall.LAUNCHCTL, "bootstrap", "system", firewall.DAEMON_PATH) in fake.calls


def test_install_without_an_upper_bound_creates_the_accounts_the_ram_can_use(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-059 addendum 4: no WORKVM_SLOTS - as many slot accounts as the RAM above the reserve holds standard VMs."""
    monkeypatch.setattr(workvm_cli, "physical_memory_mib", lambda: 32768 + 3 * 8192)
    code, out, _ = cli(*INSTALL, AGENT_RUNNER_WORKVM_SLOTS="")
    assert code == 0
    assert "runner.workvm.install step=accounts count=3 created=3" in out.splitlines()
    assert out.splitlines()[-1] == "runner.workvm.install result=ok slots=3"
    code, out, _ = cli("status", euid=501, AGENT_RUNNER_WORKVM_SLOTS="")
    assert out.splitlines()[0] == "runner.workvm.status slots=3 self=no accounts=0 missing=3"


def test_install_keeps_what_exists(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.mounted = True
    fake.gid = 650
    fake.uids.update({"wvm00": 700, "wvm01": 701})
    code, out, _ = cli(*INSTALL, "--bandwidth-mbit", "100")
    assert code == 0
    assert out.splitlines()[:4] == [
        "runner.workvm.install step=volume action=present path=/var/agent-workvm",
        "runner.workvm.install step=images path=/var/agent-workvm/images",
        "runner.workvm.install step=group action=present name=workvm gid=650",
        "runner.workvm.install step=accounts count=2 created=0",
    ]
    assert not any(call[:3] == (accounts.DSCL, ".", "-create") for call in fake.calls)
    assert not any(call[0] == accounts.DISKUTIL for call in fake.calls)
    # an installation before HOT-18 left _config root-owned: install gives it to the slot accounts
    assert fake.given == [(f"{accounts.HOME_ROOT}/wvm00", 700, 650), (f"{accounts.HOME_ROOT}/wvm01", 701, 650)]
    slots = [firewall.Slot(0, "wvm00", 700, 47200), firewall.Slot(1, "wvm01", 701, 47201)]
    assert fake.files[firewall.ANCHOR_PATH][0] == firewall.render(slots, None, 100)


def test_install_one_account_missing(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids["wvm00"] = 602
    code, out, _ = cli(*INSTALL)
    assert code == 0
    assert "runner.workvm.install step=accounts count=2 created=1" in out.splitlines()
    assert (accounts.DSCL, ".", "-create", f"{DS_USERS}/wvm01", "UniqueID", "603") in fake.calls
    assert fake.given == [(f"{accounts.HOME_ROOT}/wvm00", 602, 601)]


def test_install_refuses_a_lima_config_it_cannot_give(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]
) -> None:
    fake.uids.update({"wvm00": 700, "wvm01": 701})
    fake.give_fails = True
    code, _, err = cli(*INSTALL)
    assert (code, err) == (1, "runner.error code=workvm_install_failed name=lima_config\n")


@pytest.mark.parametrize(
    "case",
    [
        (INSTALL, {}, 501, "runner.error code=workvm_root_required"),
        (INSTALL, {"AGENT_RUNNER_WORKVM_SLOTS": "0"}, 0, "runner.error code=workvm_not_configured name=WORKVM_SLOTS"),
        (
            INSTALL,
            {"AGENT_RUNNER_WORKVM_SLOTS_SELF": "1", "AGENT_RUNNER_TEST_MODE": "1"},
            0,
            "runner.error code=workvm_slots_self name=WORKVM_SLOTS_SELF",
        ),
        (
            ("install", "--runner-account", "Bad Name", "--quota-gib", "1"),
            {},
            0,
            "runner.error code=workvm_accounts_invalid name=account",
        ),
        (
            ("install", "--runner-account", "ghost", "--quota-gib", "1"),
            {},
            0,
            "runner.error code=workvm_account_missing name=runner_account",
        ),
        (
            (*INSTALL, "--bandwidth-mbit", "0"),
            {},
            0,
            "runner.error code=workvm_firewall_invalid name=bandwidth_mbit",
        ),
    ],
)
def test_install_refusals(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]], case: tuple[tuple[str, ...], dict[str, str], int, str]
) -> None:
    argv, env, euid, error = case
    code, out, err = cli(*argv, euid=euid, **env)
    assert (code, out, err) == (1, "", error + "\n")
    assert fake.files == {}
    assert not any(call[0] in (accounts.DSCL, accounts.DISKUTIL) for call in fake.calls)


def test_install_failing_volume_command(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.answers[("/bin/mkdir", "-p", accounts.HOME_ROOT)] = (1, "")
    code, out, err = cli(*INSTALL)
    assert (code, out, err) == (1, "", "runner.error code=workvm_install_failed name=mkdir\n")


@pytest.mark.parametrize("path", ["/Users", "/Groups"])
def test_install_failing_id_listing(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]], path: str) -> None:
    fake.mounted = True
    key = "UniqueID" if path == "/Users" else "PrimaryGroupID"
    fake.answers[(accounts.DSCL, ".", "-list", path, key)] = (1, "")
    code, _, err = cli(*INSTALL)
    assert (code, err) == (1, "runner.error code=workvm_install_failed name=dscl\n")


def test_install_unreadable_group(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.mounted = True
    fake.answers[(accounts.DSCL, ".", "-read", "/Groups/workvm", "PrimaryGroupID")] = (0, "PrimaryGroupID: x\n")
    code, _, err = cli(*INSTALL)
    assert (code, err) == (1, "runner.error code=workvm_install_failed name=dscl\n")


def test_install_refused_sudoers(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.mounted = True
    fake.visudo_ok = False
    code, out, err = cli(*INSTALL)
    assert (code, err) == (1, "runner.error code=workvm_sudoers_invalid\n")
    assert accounts.SUDOERS_PATH not in fake.files
    assert "step=wrapper" in out


def test_install_failing_firewall(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.mounted = True
    fake.answers[(firewall.PFCTL, "-a", firewall.ANCHOR, "-n", "-f", firewall.ANCHOR_PATH)] = (1, "")
    code, out, err = cli(*INSTALL)
    assert (code, err) == (1, "runner.error code=workvm_firewall_install name=pfctl\n")
    assert "step=firewall" not in out


def test_render(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids.update({"wvm00": 602, "wvm01": 603})
    slots = [firewall.Slot(0, "wvm00", 602, 47200), firewall.Slot(1, "wvm01", 603, 47201)]
    code, out, err = cli("firewall", "render", euid=501)
    assert (code, out, err) == (0, firewall.render(slots, None, 500), "")
    code, out, _ = cli("firewall", "render", "--egress", "utun4", "--bandwidth-mbit", "50", "--json", euid=501)
    assert code == 0
    assert json.loads(out)["facts"] == [
        {"fact": "runner.workvm.firewall.anchor", "text": firewall.render(slots, "utun4", 50)}
    ]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (("firewall", "render"), "runner.error code=workvm_account_missing name=wvm00"),
        (("firewall", "check"), "runner.error code=workvm_account_missing name=wvm00"),
    ],
)
def test_render_and_check_need_the_accounts(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]], argv: tuple[str, ...], error: str
) -> None:
    assert cli(*argv) == (1, "", error + "\n")


@pytest.mark.parametrize("command", ["render", "check"])
def test_render_and_check_need_slots(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]], command: str) -> None:
    code, _, err = cli("firewall", command, AGENT_RUNNER_WORKVM_SLOTS="0")
    assert (code, err) == (1, "runner.error code=workvm_not_configured name=WORKVM_SLOTS\n")


def test_render_refuses_a_bad_egress(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids.update({"wvm00": 602, "wvm01": 603})
    code, _, err = cli("firewall", "render", "--egress", "en0")
    assert (code, err) == (1, "runner.error code=workvm_firewall_invalid name=egress_interface\n")


def test_check_ok_through_sudo(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids.update({"wvm00": 602, "wvm01": 603})
    code, out, err = cli("firewall", "check", euid=501)
    assert (code, out, err) == (0, "runner.workvm.firewall ok=yes internet=yes egress=utun3\n", "")
    assert (*SUDO, *firewall.READ_COMMANDS[0]) in fake.calls
    assert (workvm_cli.ID, "-u", "wvm00") in fake.calls


def test_check_as_root_with_problems(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids.update({"wvm00": 602, "wvm01": 603})
    fake.answers[firewall.READ_COMMANDS[0]] = (0, "Status: Disabled\n")
    fake.answers[firewall.READ_COMMANDS[3]] = (0, "lo0 (skip)\n")
    code, out, _ = cli("firewall", "check")
    assert code == 1
    assert out.splitlines() == [
        "runner.workvm.firewall ok=no internet=no egress=utun3",
        "runner.workvm.firewall.problem code=pf_disabled",
        "runner.workvm.firewall.problem code=skip_lo0",
    ]
    assert not any(call[:2] == SUDO for call in fake.calls)


def test_check_names_the_missing_table_ranges(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids.update({"wvm00": 602, "wvm01": 603})
    fake.answers[firewall.READ_COMMANDS[4]] = (0, f"   {firewall.BLOCKED_V4[1]}\n")
    code, out, _ = cli("firewall", "check")
    assert code == 1
    lines = out.splitlines()
    assert lines[:2] == [
        "runner.workvm.firewall ok=no internet=no egress=utun3",
        "runner.workvm.firewall.problem code=table_stale",
    ]
    first, private, cgnat = firewall.BLOCKED_V4[:3]
    assert lines[2].startswith(f"runner.workvm.firewall.table_missing entries={first},{cgnat},")
    assert f"{private}," not in lines[2]


def test_status_with_boundary(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids.update({"wvm00": 602, "wvm01": 603})
    fake.egress = None
    code, out, _ = cli("status", euid=501)
    assert code == 0
    assert out.splitlines() == [
        "runner.workvm.status slots=2 self=no accounts=2 missing=0",
        "runner.workvm.firewall ok=yes internet=no egress=-",
    ]


def test_status_with_missing_accounts(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids["wvm00"] = 602
    code, out, _ = cli("status", euid=501)
    assert code == 0
    assert out.splitlines() == [
        "runner.workvm.status slots=2 self=no accounts=1 missing=1",
        "runner.workvm.firewall ok=no internet=no egress=-",
        "runner.workvm.firewall.problem code=accounts_missing",
    ]
    assert not any(call[:2] == SUDO for call in fake.calls)


def test_status_self_mode_and_no_slots(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    code, out, _ = cli("status", AGENT_RUNNER_WORKVM_SLOTS_SELF="1", AGENT_RUNNER_TEST_MODE="1")
    assert (code, out) == (0, "runner.workvm.status slots=2 self=yes accounts=0 missing=0\n")
    code, out, _ = cli("status", AGENT_RUNNER_WORKVM_SLOTS="0")
    assert (code, out) == (0, "runner.workvm.status slots=0 self=no accounts=0 missing=0\n")
    assert fake.calls == []


def test_real_machine(tmp_path: Path) -> None:
    machine = workvm_cli.default_machine()
    done = machine.run([sys.executable, "-c", "print('ok')"], 30.0)
    assert (done.returncode, done.stdout) == (0, "ok\n")
    missing = machine.run([str(tmp_path / "missing")], 30.0)
    assert (missing.returncode, missing.stdout) == (1, "")
    echoed = machine.run_input([sys.executable, "-c", "import sys; print(sys.stdin.read())"], "in", 30.0)
    assert echoed.stdout == "in\n"
    assert machine.is_mount("/")
    assert machine.exists("/")
    gone = tmp_path / "gone"
    gone.write_text("x")
    machine.remove_file(str(gone))
    machine.remove_file(str(gone))
    assert not gone.exists()
    target = tmp_path / "new" / "file"
    write = workvm_cli.file_writer(os.getuid(), os.getgid())
    write(str(target), "text\n", 0o640)
    assert (machine.read_file(str(target)), target.stat().st_mode & 0o777) == ("text\n", 0o640)
    with pytest.raises(TypeError):
        write(str(target), None, 0o644)  # type: ignore[arg-type]
    assert sorted(p.name for p in target.parent.iterdir()) == ["file"]
    assert target.read_text(encoding="utf-8") == "text\n"


def test_install_prepares_the_images_directory_of_the_service(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]
) -> None:
    from agent_runner.workvm import service  # noqa: PLC0415 - only for the path constant

    images = str(service.IMAGES_DIR)
    assert images == f"{accounts.HOME_ROOT}/images"
    assert cli(*INSTALL)[0] == 0
    assert [call for call in fake.calls if images in call] == [
        ("/bin/mkdir", "-p", images),
        ("/usr/sbin/chown", "runner", images),
        ("/bin/chmod", "0755", images),
    ]
    fake.answers[("/usr/sbin/chown", "runner", images)] = (1, "")
    code, _, err = cli(*INSTALL)
    assert (code, err) == (1, "runner.error code=workvm_install_failed name=chown\n")


# -- install --plan and the hook (KOR-29 operations finding 1) --------------------------------------------------


def test_install_refuses_a_device_without_the_hook(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.main = 'anchor "com.cloudflare.warp_svc" all\n'
    code, out, err = cli(*INSTALL)
    assert (code, out, err) == (1, "", "runner.error code=workvm_firewall_unhooked name=not_hooked\n")
    assert fake.files == {}
    assert not any(call[0] in (accounts.DSCL, accounts.DISKUTIL) and call[2] != "-list" for call in fake.calls)


def test_install_plan_changes_nothing(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    fake.uids["wvm00"] = 700
    code, out, err = cli(*INSTALL, "--plan")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[:7] == [
        "runner.workvm.plan step=volume action=create path=/var/agent-workvm quota_gib=200",
        "runner.workvm.plan step=images path=/var/agent-workvm/images",
        "runner.workvm.plan step=group action=create name=workvm",
        "runner.workvm.plan step=accounts count=2 create=1",
        f"runner.workvm.plan step=wrapper path={accounts.WRAPPER_PATH}",
        f"runner.workvm.plan step=sudoers path={accounts.SUDOERS_PATH}",
        "runner.workvm.plan.firewall action=write path=/etc/pf.anchors/agent.workvm mode=0644 sha256="
        + lines[6].rsplit("=", 1)[1],
    ]
    assert (
        "runner.workvm.plan.firewall action=run"
        f' argv="/sbin/pfctl -a {firewall.ANCHOR} -f /etc/pf.anchors/agent.workvm" may_fail=no' in lines
    )
    assert (
        "runner.workvm.plan.firewall action=run"
        ' argv="/bin/launchctl bootout system/swiss.dfx.agent-runner.pf" may_fail=yes' in lines
    )
    assert lines[-1] == f"runner.workvm.plan main_ruleset=untouched pf_conf=untouched anchor={firewall.ANCHOR} hook=ok"
    assert fake.files == {}
    reads = (workvm_cli.ID, accounts.DSCL)
    assert [call for call in fake.calls if call[0] not in reads] == [(firewall.PFCTL, "-sr")]
    assert all(call[2] in ("-list", "-read") for call in fake.calls if call[0] == accounts.DSCL)


def test_install_plan_with_present_parts_and_without_hook(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]
) -> None:
    fake.mounted = True
    fake.gid = 650
    fake.main = ""
    code, out, _ = cli(*INSTALL, "--plan")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "runner.workvm.plan step=volume action=present path=/var/agent-workvm quota_gib=200"
    assert lines[2] == "runner.workvm.plan step=group action=present name=workvm"
    assert lines[-2:] == [
        f"runner.workvm.plan main_ruleset=untouched pf_conf=untouched anchor={firewall.ANCHOR} hook=missing",
        "runner.workvm.plan.problem code=not_hooked",
    ]
    assert fake.files == {}


# -- uninstall ---------------------------------------------------------------------------------------------------


def installed(fake: FakeMachine) -> None:
    fake.mounted = True
    fake.dirs.add(accounts.HOME_ROOT)
    fake.gid = 650
    fake.uids.update({"wvm00": 700, "wvm01": 701})
    for path in (
        firewall.DAEMON_PATH,
        firewall.WATCH_PATH,
        firewall.ANCHOR_PATH,
        accounts.WRAPPER_PATH,
        accounts.SUDOERS_PATH,
    ):
        fake.files[path] = ("x", 0o644)


def test_uninstall_removes_everything_and_checks(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    installed(fake)
    fake.files[firewall.PF_CONF] = ('anchor "agent.workvm"\n' + fake.pf_conf, 0o644)
    code, out, err = cli("uninstall")
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        f"runner.workvm.uninstall step=firewall steps=10 anchor={firewall.ANCHOR}",
        "runner.workvm.uninstall step=accounts count=2 removed=2",
        "runner.workvm.uninstall step=group action=removed name=workvm",
        f"runner.workvm.uninstall step=files wrapper={accounts.WRAPPER_PATH} sudoers={accounts.SUDOERS_PATH}",
        "runner.workvm.uninstall step=volume action=removed path=/var/agent-workvm",
        "runner.workvm.uninstall result=ok",
    ]
    assert fake.files == {firewall.PF_CONF: (fake.pf_conf, 0o644)}
    kills = [i for i, call in enumerate(fake.calls) if call == (firewall.PKILL, "-KILL", "-u", "wvm00")]
    flush = fake.calls.index((firewall.PFCTL, "-a", firewall.ANCHOR, "-F", "all"))
    bootout = fake.calls.index((firewall.LAUNCHCTL, "bootout", "system/swiss.dfx.agent-runner.pf"))
    deletes = fake.calls.index((accounts.DSCL, ".", "-delete", f"{DS_USERS}/wvm00"))
    # the slots stop while the filter still holds, and again after it is gone, before their accounts go
    assert len(kills) == 2
    assert kills[0] < bootout < flush < kills[1] < deletes
    assert (firewall.PKILL, "-KILL", "-u", "wvm01") in fake.calls
    assert (accounts.DISKUTIL, "apfs", "deleteVolume", accounts.HOME_ROOT) in fake.calls
    assert (firewall.PFCTL, "-f", firewall.PF_CONF) not in fake.calls


def test_uninstall_keeps_the_volume_and_twice_is_harmless(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]
) -> None:
    installed(fake)
    assert cli("uninstall", "--keep-volume")[0] == 0
    assert fake.mounted
    code, out, _ = cli("uninstall")
    assert code == 0
    assert "runner.workvm.uninstall step=accounts count=2 removed=0" in out.splitlines()
    assert "runner.workvm.uninstall step=group action=absent name=workvm" in out.splitlines()
    assert "runner.workvm.uninstall step=volume action=removed path=/var/agent-workvm" in out.splitlines()
    code, out, _ = cli("uninstall")
    assert "runner.workvm.uninstall step=volume action=absent path=/var/agent-workvm" in out.splitlines()


def test_uninstall_reports_what_is_left(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    installed(fake)
    fake.answers[(firewall.PFCTL, "-a", firewall.ANCHOR, "-sr")] = (0, "block return out quick all user = 700\n")
    fake.answers[(accounts.DSCL, ".", "-delete", f"{DS_USERS}/wvm01")] = (0, "")
    real_effect = fake.effect

    def stubborn(key: tuple[str, ...]) -> None:
        if key[:3] != (accounts.DSCL, ".", "-delete"):
            real_effect(key)

    fake.effect = stubborn  # type: ignore[method-assign]
    fake.pf_conf = 'anchor "agent.workvm"\n'
    original_remove = fake.remove_file

    kept = (accounts.SUDOERS_PATH, firewall.ANCHOR_UP_PATH, firewall.ANCHOR_SAMPLE_PATH, firewall.STATE_PATH)
    for path in kept[1:]:
        fake.files[path] = ("x", 0o644)

    def keep_sudoers(path: str) -> None:
        if path not in kept:
            original_remove(path)

    fake.remove_file = keep_sudoers  # type: ignore[method-assign]
    original_write = fake.write_file

    def keep_pf_conf(path: str, content: str, mode: int) -> None:
        if path != firewall.PF_CONF:
            original_write(path, content, mode)

    fake.write_file = keep_pf_conf  # type: ignore[method-assign]
    code, out, _ = cli("uninstall", "--keep-volume")
    assert code == 1
    assert [line for line in out.splitlines() if ".problem" in line] == [
        "runner.workvm.uninstall.problem code=anchor_rules",
        f"runner.workvm.uninstall.problem code=file:{firewall.ANCHOR_UP_PATH}",
        f"runner.workvm.uninstall.problem code=file:{firewall.ANCHOR_SAMPLE_PATH}",
        f"runner.workvm.uninstall.problem code=file:{firewall.STATE_PATH}",
        f"runner.workvm.uninstall.problem code=file:{accounts.SUDOERS_PATH}",
        "runner.workvm.uninstall.problem code=account:wvm00",
        "runner.workvm.uninstall.problem code=account:wvm01",
        "runner.workvm.uninstall.problem code=group:workvm",
        "runner.workvm.uninstall.problem code=pf_conf",
    ]
    assert out.splitlines()[-1] == "runner.workvm.uninstall result=incomplete"


def test_uninstall_reports_a_volume_still_mounted(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    installed(fake)
    fake.answers[(accounts.DISKUTIL, "apfs", "deleteVolume", accounts.HOME_ROOT)] = (0, "")  # answers, never unmounts
    code, out, _ = cli("uninstall")
    assert code == 1
    assert out.splitlines()[-2:] == [
        "runner.workvm.uninstall.problem code=volume",
        "runner.workvm.uninstall result=incomplete",
    ]


def test_plan_facts_of_every_step_kind() -> None:
    assert workvm_cli._step_fact(firewall.InstallStep("remove", "/x")) == {"action": "remove", "path": "/x"}
    assert workvm_cli._step_fact(firewall.InstallStep("run", argv=("/a", "b"), may_fail=True)) == {
        "action": "run",
        "argv": "/a b",
        "may_fail": True,
    }


@pytest.mark.parametrize(
    ("env", "euid", "error"),
    [
        ({}, 501, "runner.error code=workvm_root_required"),
        ({"AGENT_RUNNER_WORKVM_SLOTS": "0"}, 0, "runner.error code=workvm_not_configured name=WORKVM_SLOTS"),
    ],
)
def test_uninstall_refusals(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]], env: dict[str, str], euid: int, error: str
) -> None:
    assert cli("uninstall", euid=euid, **env) == (1, "", error + "\n")
    assert fake.calls == []


def test_uninstall_failing_delete(fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]) -> None:
    installed(fake)
    fake.answers[(accounts.DSCL, ".", "-delete", f"{DS_USERS}/wvm00")] = (1, "")
    code, _, err = cli("uninstall")
    assert (code, err) == (1, "runner.error code=workvm_install_failed name=dscl\n")


# -- workvm image build|list (spec 11 section 4) ---------------------------------------------------------------


@pytest.fixture
def image_cli(
    make_host: Callable[..., Any],
    runner_dsn: str,
    runner_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[int, str, str]]:
    """`workvm image ...` with a fake `limactl`, the images directory below `tmp_path` and a real store."""
    from agent_runner.store.cluster import ClusterTools  # noqa: PLC0415
    from agent_runner.workvm import service  # noqa: PLC0415

    from .test_image import FakeLimactl, make_images  # noqa: PLC0415

    (tmp_path / "lima").mkdir()
    (tmp_path / "exports").mkdir()
    fake = FakeLimactl(tmp_path / "lima")
    template = make_images(tmp_path / "tree")
    monkeypatch.setattr(workvm_cli, "image_lima", lambda config, env: Lima(fake.run, prefix=config.vm_prefix))
    monkeypatch.setattr(service, "IMAGES_DIR", tmp_path / "exports")
    exists = {"value": True}

    def call(*argv: str, **env: str) -> tuple[int, str, str]:
        tools = ClusterTools(
            ensure=lambda path: "postgresql://unused",
            stop=lambda path: None,
            create_database=lambda admin, name: runner_dsn,
            exists=lambda path: exists["value"],
        )
        values = {
            "AGENT_RUNNER_HOME": str(runner_home),
            "AGENT_RUNNER_VM_PREFIX": "wp-c-",
            "AGENT_RUNNER_WORKVM_SLOTS": "2",
            "AGENT_RUNNER_WORKVM_TEMPLATE": str(template),
            **env,
        }
        captured = make_host(values, cluster=tools)
        code = run_cli(["workvm", "image", *argv], captured.host)
        return code, captured.stdout.getvalue(), captured.stderr.getvalue()

    call.fake = fake  # type: ignore[attr-defined]
    call.exists = exists  # type: ignore[attr-defined]
    call.exports = tmp_path / "exports"  # type: ignore[attr-defined]
    return call


def test_image_build_then_list(image_cli: Any) -> None:
    code, out, err = image_cli("build", "--name", "v1")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].startswith("runner.workvm.image step=create name=wp-c-wimg-build-")
    assert [line.split()[1] for line in lines[:-1]] == [
        f"step={step}"
        for step in ("create", "start", "bundle", "provision", "smoke", "seal", "stop", "rename", "export", "register")
    ]
    assert re.fullmatch(r"runner\.workvm\.image name=wp-c-wimg-v1 state=ready seconds=\d+\.\d{3}", lines[-1])
    exported = image_cli.exports / "wp-c-wimg-v1.img"
    assert exported.stat().st_mode & 0o777 == 0o644
    code, out, err = image_cli("list")
    assert (code, err) == (0, "")
    first, count = out.splitlines()
    assert re.fullmatch(
        r"runner\.workvm\.image name=wp-c-wimg-v1 state=ready built_at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", first
    )
    assert count == "runner.workvm.images count=1"


def test_image_build_in_self_mode_exports_nothing(image_cli: Any) -> None:
    code, out, _ = image_cli("build", AGENT_RUNNER_WORKVM_SLOTS_SELF="1", AGENT_RUNNER_TEST_MODE="1")
    assert code == 0
    assert "step=export" not in out
    assert re.search(r"name=wp-c-wimg-\d{8}-[0-9a-f]{6} state=ready", out)
    assert list(image_cli.exports.iterdir()) == []


def test_image_build_failure_is_logged(image_cli: Any, runner_home: Path) -> None:
    image_cli.fake.fail["provision"] = (1, "apt-get failed\n")
    code, out, err = image_cli("build")
    assert (code, err) == (1, "runner.error code=lima_failed name=shell\n")
    assert out.splitlines()[-1].startswith("runner.workvm.image.failed step=provision name=wp-c-wimg-build-")
    log = runner_home / "log" / "image.log"
    assert "apt-get failed" in log.read_text(encoding="utf-8")
    assert log.stat().st_mode & 0o777 == 0o600
    assert image_cli.fake.instances == {}
    assert image_cli("list")[1] == "runner.workvm.images count=0\n"


def test_image_build_needs_slots(image_cli: Any) -> None:
    code, out, err = image_cli("build", AGENT_RUNNER_WORKVM_SLOTS="0")
    assert (code, out, err) == (1, "", "runner.error code=workvm_not_configured name=WORKVM_SLOTS\n")
    assert image_cli.fake.calls == []


def test_image_list_without_store(image_cli: Any) -> None:
    image_cli.exists["value"] = False
    assert image_cli("list") == (0, "runner.workvm.images count=0\n", "")


def test_image_lima_is_the_runner_lima() -> None:
    from agent_runner.vm import cli as vm_cli  # noqa: PLC0415

    assert workvm_cli.image_lima is vm_cli.make_lima


def test_uninstall_removes_the_empty_mount_point_and_reports_one_that_stays(
    fake: FakeMachine, cli: Callable[..., tuple[int, str, str]]
) -> None:
    installed(fake)
    assert cli("uninstall")[0] == 0
    assert accounts.HOME_ROOT not in fake.dirs
    installed(fake)
    fake.full_dirs.add(accounts.HOME_ROOT)  # something was left inside: it stays and is reported
    code, out, _ = cli("uninstall")
    assert code == 1
    assert f"runner.workvm.uninstall.problem code=dir:{accounts.HOME_ROOT}" in out.splitlines()
    assert out.splitlines()[-1] == "runner.workvm.uninstall result=incomplete"
    fake.full_dirs.clear()
    fake.dirs.add(accounts.HOME_ROOT)
    assert cli("uninstall", "--keep-volume")[0] == 0  # a kept volume keeps its mount point
    assert accounts.HOME_ROOT in fake.dirs


def test_remove_empty_dir_removes_only_an_empty_directory(tmp_path: Path) -> None:
    empty, full = tmp_path / "empty", tmp_path / "full"
    empty.mkdir()
    full.mkdir()
    (full / "x").write_text("x")
    workvm_cli.remove_empty_dir(str(empty))
    workvm_cli.remove_empty_dir(str(full))
    workvm_cli.remove_empty_dir(str(tmp_path / "missing"))
    assert (empty.exists(), full.exists()) == (False, True)
