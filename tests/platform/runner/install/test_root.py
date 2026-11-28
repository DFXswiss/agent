"""U-RUN-install-root: the privileged helper of the runner package (ADR-074 sections 3, 5 and 6): the root-owned
payload chain, the runner's home made and removed only by the owner of the Mac, the spool as the one channel of the
enrollment code, the code dialog of Installer.app in the owner's session, `postinstall`, the public state, the
removal with its list of leftovers (the runner's home and the owner's `~/.lima/dfx-*` included, also as the
maintenance daemon's self-removal), the golden of the active payload, the maintenance round with an interrupted
update, and the command line with its JSON output."""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_runner.install import daemons, release, root, runnerctl, update
from agent_runner.install.layout import MAINTENANCE_LABEL, PACKAGE_ID, RUNNER_LABEL
from agent_runner.install.release import Manifest
from agent_runner.install.state import Maintenance, read_json, write_state
from agent_runner.install.system import System

from .conftest import (
    COMMIT,
    HUB,
    LAUNCHCTL,
    MAINTENANCE_PRINT,
    NEWER,
    NOW,
    READ_OWNER,
    ROOT_KEY,
    RUNNER_PRINT,
    STAT_CONSOLE,
    SUDO,
    VERSION,
    Keys,
    Kit,
    as_owner,
    at_console,
    context,
    feed_files,
    known_user,
    make_current,
    make_version,
    new_keys,
    owner_of,
    refused,
    runner_commands,
    runner_doc,
    runner_home,
    safe_chain,
    status_doc,
    steps,
    unknown_user,
)

OLD = "2026.11.23.0800"
CODE = "ABCDE-FGHIJ-KLMNO"
MKDIR = "/bin/mkdir"
CHMOD = "/bin/chmod"
RM = "/bin/rm"
RMDIR = "/bin/rmdir"
TMUTIL = "/usr/bin/tmutil"
SYSCTL_MEMORY = ["/usr/sbin/sysctl", "-n", "hw.memsize"]
PRINT_DISABLED = [LAUNCHCTL, "print-disabled", "system"]
PKG_INFO = ["/usr/sbin/pkgutil", "--pkg-info", PACKAGE_ID]
PKG_FORGET = ["/usr/sbin/pkgutil", "--forget", PACKAGE_ID]
SFLTOOL = ["/usr/bin/sfltool", "dumpbtm"]
LOGGER = "/usr/bin/logger"
RUNNING = "system/swiss.dfx.agent-runner = {\n\tstate = running\n}\n"
READY = {"fact": "runner.image", "state": "ready", "claude_version": "2.1.330", "guest_version": "0.1.0"}


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def write_request(kit: Kit, code: str = CODE, permissions: int = 0o600) -> Path:
    path = kit.layout.request
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    path.chmod(permissions)
    return path


def spool_owner(kit: Kit, name: str = "alice") -> None:
    """What the administrator script of the agent way records next to the request."""
    path = kit.layout.spool / "owner"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n", encoding="ascii")
    path.chmod(0o600)


def record_owner(kit: Kit, name: str = "alice", created: tuple[str, ...] = ()) -> None:
    Maintenance(owner=name, created=list(created)).save(kit.layout.maintenance_file)


def as_user(kit: Kit) -> System:
    return dataclasses.replace(kit.system, geteuid=lambda: 501)


def command_line(kit: Kit) -> System:
    """The system of `installer` (no code dialog)."""
    return dataclasses.replace(kit.system, env={"COMMAND_LINE_INSTALL": "1"})


def owner_acts(kit: Kit) -> None:
    """`mkdir` and `rm -rf` run as the owner act on the files (root itself never does)."""
    kit.run.on(as_owner(MKDIR, "-p"), lambda argv: Path(argv[-1]).mkdir(parents=True, exist_ok=True))
    kit.run.on(as_owner(RM, "-rf"), lambda argv: shutil.rmtree(argv[-1]))


def nothing_left(kit: Kit) -> None:
    """launchd and the receipt database know nothing of the runner."""
    kit.run.answer(RUNNER_PRINT, rc=113)
    kit.run.answer(MAINTENANCE_PRINT, rc=113)
    kit.run.answer(PKG_INFO, rc=1)


def state(kit: Kit) -> dict[str, Any]:
    found = read_json(kit.layout.state_file)
    assert found is not None
    return found


def no_pkill(kit: Kit) -> None:
    assert not [call for call in kit.run.calls if "pkill" in " ".join(call)]


# --- root and the payload chain -------------------------------------------------------------------------------------


def test_only_root_runs_the_helper(kit: Kit, keys: Keys) -> None:
    root.require_root(kit.system)
    with refused("root_required"):
        root.postinstall(as_user(kit), VERSION, [keys.public])
    with refused("root_required"):
        root.uninstall(as_user(kit), [keys.public])
    with refused("root_required"):
        root.maintain(as_user(kit), [keys.public])
    assert kit.run.calls == []


def test_a_root_owned_chain_is_safe(kit: Kit) -> None:
    root.check_ancestors(kit.system)  # nothing installed yet: missing parts are skipped
    safe_chain(kit.layout)
    root.check_ancestors(kit.system)


def test_a_group_writable_directory_above_the_payload_is_unsafe(kit: Kit) -> None:
    safe_chain(kit.layout)
    (kit.layout.root / "Library").chmod(0o775)
    with refused("payload_parent_unsafe", "Library"):
        root.check_ancestors(kit.system)


def test_a_world_writable_root_is_unsafe(kit: Kit) -> None:
    safe_chain(kit.layout)
    kit.layout.root.chmod(0o757)
    with refused("payload_parent_unsafe", "root"):
        root.check_ancestors(kit.system)


def test_a_symbolic_link_in_the_chain_is_unsafe(kit: Kit, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (kit.layout.root / "Library/Application Support").mkdir(parents=True)
    kit.layout.payload.symlink_to(elsewhere)
    safe_chain(kit.layout)
    with refused("payload_parent_unsafe", "DFXAgentRunner"):
        root.check_ancestors(kit.system)


def test_a_payload_owned_by_another_user_is_unsafe(kit: Kit) -> None:
    safe_chain(kit.layout)
    system = dataclasses.replace(kit.system, root_uid=os.getuid() + 1)
    with refused("payload_parent_unsafe", "DFXAgentRunner"):
        root.check_ancestors(system)


# --- versions -------------------------------------------------------------------------------------------------------


def test_the_manifest_of_a_running_version_is_checked_by_signature_only(kit: Kit, keys: Keys) -> None:
    folder = make_version(kit.layout, keys)
    (folder / "added").write_text("x", encoding="utf-8")
    found = root._manifest(kit.system, VERSION, [keys.public], verify_tree=False)
    assert (found.version, found.hub, found.commit) == (VERSION, HUB, COMMIT)
    with refused("release_tree_mismatch"):
        root._manifest(kit.system, VERSION, [keys.public], verify_tree=True)
    with refused("release_signature_invalid"):
        root._manifest(kit.system, VERSION, [new_keys().public], verify_tree=False)
    (folder / "release.json.sig").write_bytes(b"\xff\xfe")
    with refused("release_invalid", "missing"):
        root._manifest(kit.system, VERSION, [keys.public], verify_tree=False)
    (folder / "release.json.sig").unlink()
    with refused("release_invalid", "missing"):
        root._manifest(kit.system, VERSION, [keys.public], verify_tree=False)


def test_the_active_version_is_the_target_of_current(kit: Kit, keys: Keys) -> None:
    assert root.active_version(kit.system) is None
    make_current(kit.layout, keys)
    assert root.active_version(kit.system) == VERSION


def test_older_versions_are_pruned(kit: Kit, keys: Keys) -> None:
    assert root.prune_versions(kit.system, {VERSION}) == []
    for version in (OLD, VERSION, NEWER, "2026.11.22.0800"):
        make_version(kit.layout, keys, version)
    versions = kit.layout.versions
    (versions / "notes").write_text("x", encoding="utf-8")
    (versions / "link").symlink_to(versions / OLD)
    assert root.prune_versions(kit.system, {VERSION, NEWER}) == ["2026.11.22.0800", OLD]
    assert sorted(item.name for item in versions.iterdir()) == [VERSION, NEWER, "link", "notes"]


def test_the_runner_home_is_made_by_the_owner(kit: Kit) -> None:
    ctx = context(kit)
    home = ctx.home
    memory = Maintenance()
    root.runner_dirs(ctx, memory)
    assert kit.run.calls == [
        as_owner(MKDIR, "-p", "-m", "0700", str(home)),
        as_owner(CHMOD, "0700", str(home)),
        as_owner(MKDIR, "-p", "-m", "0700", str(home / "log")),
    ]
    assert not ctx.owner.home.exists()  # root itself created nothing below the owner's home
    # outermost first, the runner home itself not; Lima's directories it will make (KOR-67)
    assert memory.created == [".local", ".local/share", ".lima", "Library/Caches/lima"]
    root.runner_dirs(ctx, memory)
    assert memory.created == [".local", ".local/share", ".lima", "Library/Caches/lima"]  # recorded once
    kit.run.answer([SUDO], rc=1)
    with refused("runner_home_failed"):
        root.runner_dirs(ctx, memory)


def test_only_parents_the_installation_had_to_create_are_recorded(kit: Kit, tmp_path: Path) -> None:
    ctx = context(kit)
    (ctx.owner.home / ".local").mkdir(parents=True)
    memory = Maintenance()
    root.runner_dirs(ctx, memory)
    assert memory.created == [".local/share", ".lima", "Library/Caches/lima"]
    (ctx.owner.home / ".local/share").symlink_to(tmp_path / "missing")  # the owner's own (dangling) link
    (ctx.owner.home / ".lima").mkdir()  # the owner's own Lima
    (ctx.owner.home / "Library/Caches/lima").mkdir(parents=True)
    memory = Maintenance()
    root.runner_dirs(ctx, memory)
    assert memory.created == []


# --- the spool and the code dialog ----------------------------------------------------------------------------------


def test_a_request_is_read_once(kit: Kit) -> None:
    assert root.read_request(kit.layout.request) is None
    path = write_request(kit, "  " + CODE + "\n")
    assert root.read_request(path) == CODE
    assert not path.exists()
    write_request(kit, " \n")
    assert root.read_request(path) is None
    assert not path.exists()


@pytest.mark.parametrize(("code", "permissions"), [(CODE, 0o644), (CODE, 0o640), ("x" * 4097, 0o600)])
def test_an_open_or_oversized_request_is_refused_and_removed(kit: Kit, code: str, permissions: int) -> None:
    path = write_request(kit, code, permissions)
    with refused("request_invalid"):
        root.read_request(path)
    assert not path.exists()


def test_a_request_of_exactly_the_maximum_is_read(kit: Kit) -> None:
    path = write_request(kit, "x" * 4096)
    assert root.read_request(path) == "x" * 4096


def test_a_linked_request_is_refused_and_removed_without_following_it(kit: Kit, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text(CODE, encoding="utf-8")
    target.chmod(0o600)
    kit.layout.spool.mkdir(parents=True)
    kit.layout.request.symlink_to(target)
    with refused("request_invalid"):
        root.read_request(kit.layout.request)
    assert not kit.layout.request.is_symlink()
    assert target.read_text(encoding="utf-8") == CODE


def dialog_argv() -> list[str]:
    return [
        "/bin/launchctl",
        "asuser",
        "501",
        SUDO,
        "-u",
        "alice",
        "-H",
        "--",
        "/usr/bin/osascript",
        "-e",
        root.ASK_SCRIPT,
        root.CODE_PROMPT,
    ]


def test_installer_app_asks_the_owner_for_the_code_in_the_owners_session(kit: Kit) -> None:
    ctx = context(kit)
    at_console(kit.run)
    kit.run.answer(["/bin/launchctl", "asuser"], stdout=f" {CODE} \n")
    assert root.ask_code(ctx) == CODE
    assert kit.run.calls == [STAT_CONSOLE, dialog_argv()]
    # the script is a constant: the prompt is its argument, the answer is its output
    assert root.ASK_SCRIPT.startswith("on run argv\n")
    kit.run.answer(["/bin/launchctl", "asuser"], rc=1, stdout="", stderr="User canceled. (-128)")
    assert root.ask_code(ctx) is None
    kit.run.answer(["/bin/launchctl", "asuser"], stdout="\n")
    assert root.ask_code(ctx) is None


@pytest.mark.parametrize("screen", ["loginwindow\n", "bob\n", "\n"])
def test_no_dialog_unless_the_owner_is_at_the_screen(kit: Kit, screen: str) -> None:
    kit.run.answer(STAT_CONSOLE, stdout=screen)
    assert root.ask_code(context(kit)) is None
    assert kit.run.calls == [STAT_CONSOLE]


def test_no_dialog_on_the_command_line(kit: Kit) -> None:
    at_console(kit.run)
    ctx = runnerctl.Context(command_line(kit), owner_of(kit.layout), {})
    assert root.ask_code(ctx) is None
    assert kit.run.calls == []


# --- enrollment -----------------------------------------------------------------------------------------------------


ENV = {"AGENT_RUNNER_HUB_URL": HUB}


def test_an_enrolled_device_ignores_and_removes_a_code(kit: Kit) -> None:
    ctx = context(kit)
    assert root.enroll_pending(ctx, True) == {"enrollment": "already"}
    path = write_request(kit)
    assert root.enroll_pending(ctx, True) == {"enrollment": "already_enrolled"}
    assert not path.exists()
    assert kit.run.calls == []


def test_without_a_code_the_device_waits(kit: Kit) -> None:
    ctx = runnerctl.Context(command_line(kit), owner_of(kit.layout), ENV)
    assert root.enroll_pending(ctx, False) == {"enrollment": "waiting"}
    assert kit.run.calls == []


def test_the_code_reaches_the_runner_on_standard_input(kit: Kit) -> None:
    ctx = context(kit, ENV)
    ctx.home.mkdir(parents=True)
    write_request(kit)
    seen = runner_commands(kit.run, {"enroll": runner_doc({"fact": "runner.enroll", "device_id": "dev-7"})})
    result = root.enroll_pending(ctx, False)
    assert result == {"enrollment": "enrolled", "device_id": "dev-7"}
    assert seen == ["enroll"]
    [(argv, text)] = kit.run.inputs
    assert text == CODE + "\n"
    assert argv[-6:] == ["enroll", "--hub", HUB, "--code-file", "-", "--json"]
    assert not any(CODE in " ".join(call) for call in kit.run.calls)
    assert list(ctx.home.iterdir()) == []
    assert not kit.layout.request.exists()


def test_the_dialog_supplies_the_code_in_installer_app(kit: Kit) -> None:
    ctx = context(kit, ENV)
    ctx.home.mkdir(parents=True)
    at_console(kit.run)
    kit.run.answer(["/bin/launchctl", "asuser"], stdout=CODE + "\n")
    runner_commands(kit.run, {"enroll": runner_doc({"fact": "runner.enroll"})})
    assert root.enroll_pending(ctx, False) == {"enrollment": "enrolled", "device_id": ""}
    assert steps(kit.run) == [STAT_CONSOLE, dialog_argv(), "runner enroll"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (runner_doc(ok=False, error={"code": "code_expired"}), "code_expired"),
        (runner_doc(ok=False), "enroll_failed"),
        ("", "enroll"),
    ],
)
def test_a_failed_enrollment_keeps_the_installation_and_names_the_reason(kit: Kit, reply: str, reason: str) -> None:
    ctx = context(kit, ENV)
    ctx.home.mkdir(parents=True)
    write_request(kit)
    runner_commands(kit.run, {"enroll": reply})
    result = root.enroll_pending(ctx, False)
    assert result == {"enrollment": "failed", "enrollment_error": reason}
    assert list(ctx.home.iterdir()) == []


def test_a_linked_runner_home_gets_nothing_written(kit: Kit, tmp_path: Path) -> None:
    ctx = context(kit, ENV)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ctx.home.parent.mkdir(parents=True)
    ctx.home.symlink_to(elsewhere)
    write_request(kit)
    runner_commands(kit.run, {"enroll": runner_doc({"fact": "runner.enroll", "device_id": "dev-7"})})
    assert root.enroll_pending(ctx, False) == {"enrollment": "enrolled", "device_id": "dev-7"}
    assert list(elsewhere.iterdir()) == []
    assert [text for _, text in kit.run.inputs] == [CODE + "\n"]
    assert not kit.layout.request.exists()


# --- the context ----------------------------------------------------------------------------------------------------


MANIFEST = Manifest(VERSION, COMMIT, HUB, ROOT_KEY, {})


def test_the_context_resolves_the_owner_and_the_runner_settings(kit: Kit) -> None:
    known_user(kit.run)
    ctx = root.context(kit.system, MANIFEST, "alice")
    assert ctx.owner == owner_of(kit.layout)
    assert ctx.env == runnerctl.runner_env(kit.system, ctx.owner, MANIFEST, 4096)
    assert ctx.env["AGENT_RUNNER_HOME"] == str(runner_home(kit.layout))
    assert kit.run.calls == [STAT_CONSOLE, READ_OWNER, SYSCTL_MEMORY]


def test_a_service_account_is_never_the_owner(kit: Kit) -> None:
    known_user(kit.run, "_dfxagentrunner", uid=300, gid=300, home="/private/var/dfx-agent-runner")
    at_console(kit.run, "_dfxagentrunner")
    with refused("owner_unknown"):
        root.context(kit.system, MANIFEST, "_dfxagentrunner")


# --- postinstall ----------------------------------------------------------------------------------------------------


def test_postinstall_installs_enrolls_and_publishes(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    folder = make_version(layout, keys)
    safe_chain(layout)
    layout.spool.mkdir(mode=0o755)
    layout.spool.chmod(0o755)
    write_request(kit)
    spool_owner(kit)
    known_user(kit.run)
    owner_acts(kit)
    kit.run.answer(MAINTENANCE_PRINT, rc=113)
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    seen = runner_commands(
        kit.run,
        {
            "status": [status_doc(enrolled=False), status_doc()],
            "enroll": runner_doc({"fact": "runner.enroll", "device_id": "dev-1"}),
        },
    )
    result = root.postinstall(kit.system, VERSION, [keys.public])
    assert result == {
        "version": VERSION,
        "previous": None,
        "pruned": [],
        "host_tools": "not_pinned",
        "owner": "alice",
        "enrollment": "enrolled",
        "device_id": "dev-1",
    }
    assert seen == ["status", "enroll", "status"]
    home = runner_home(layout)
    assert steps(kit.run) == [
        MAINTENANCE_PRINT,
        [LAUNCHCTL, "bootout", f"system/{MAINTENANCE_LABEL}"],
        [LAUNCHCTL, "bootstrap", "system", str(layout.maintenance_plist)],
        STAT_CONSOLE,
        READ_OWNER,
        SYSCTL_MEMORY,
        as_owner(MKDIR, "-p", "-m", "0700", str(home)),
        as_owner(CHMOD, "0700", str(home)),
        as_owner(MKDIR, "-p", "-m", "0700", str(home / "log")),
        as_owner(TMUTIL, "addexclusion", str(home)),
        "runner status",
        "runner enroll",
        [LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"],
        [LAUNCHCTL, "bootstrap", "system", str(layout.runner_plist)],
        [LAUNCHCTL, "kickstart", f"system/{MAINTENANCE_LABEL}"],
        "runner status",
        RUNNER_PRINT,
        PRINT_DISABLED,
    ]
    assert not any(CODE in " ".join(call) for call in kit.run.calls)
    assert os.readlink(layout.current) == str(folder)
    assert mode(layout.spool) == 0o700
    assert not layout.request.exists()
    assert not (layout.spool / "owner").exists()
    assert layout.log_dir.is_dir()
    assert list(home.iterdir()) == [home / "log"]  # no code file was written
    assert [text for _, text in kit.run.inputs] == [CODE + "\n"]
    runner = plistlib.loads(layout.runner_plist.read_bytes())
    assert runner["UserName"] == "alice"
    assert runner["EnvironmentVariables"]["AGENT_RUNNER_HOME"] == str(home)
    assert "AGENT_RUNNER_POOL_SIZE" not in runner["EnvironmentVariables"]
    assert runner["EnvironmentVariables"]["AGENT_RUNNER_MEMORY_RESERVE_MIB"] == "4096"
    assert plistlib.loads(layout.maintenance_plist.read_bytes()) == daemons.maintenance_plist(kit.system)
    assert mode(layout.maintenance_file) == 0o600
    created = [".local", ".local/share", ".lima", "Library/Caches/lima"]
    assert Maintenance.load(layout.maintenance_file) == Maintenance(owner="alice", created=created)
    assert mode(layout.state_file) == 0o644
    assert state(kit) == {
        "v": 1,
        "version": VERSION,
        "hub": HUB,
        "owner": "alice",
        "enrolled": True,
        "device_id": "dev-1",
        "status": "read",
        "connected": True,
        "revoked": "no",
        "removed": "no",
        "daemon": "running",
        "disabled_in_login_items": False,
        "update": {"feed": "unchecked", "pending": False, "bad": []},
        "updated_at": "2026-11-24T12:00:00Z",
        "golden": "unknown",
        "host_tools": "not_pinned",
        "enrollment": "enrolled",
    }
    no_pkill(kit)


def test_a_new_version_keeps_the_owner_and_the_previous_version_and_prunes_older_ones(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_version(layout, keys, OLD)
    make_current(layout, keys, VERSION)
    make_version(layout, keys, NEWER)
    safe_chain(layout)
    at_console(kit.run)
    known_user(kit.run)
    seen = runner_commands(kit.run, {"status": status_doc()})
    result = root.postinstall(kit.system, NEWER, [keys.public])
    assert (result["previous"], result["pruned"], result["enrollment"]) == (VERSION, [OLD], "already")
    assert result["owner"] == "alice"
    assert seen == ["status", "status"]
    assert os.readlink(layout.current) == str(layout.version_dir(NEWER))
    assert sorted(item.name for item in layout.versions.iterdir()) == [VERSION, NEWER]
    created = [".local", ".local/share", ".lima", "Library/Caches/lima"]
    assert Maintenance.load(layout.maintenance_file) == Maintenance(previous=VERSION, owner="alice", created=created)
    assert layout.runner_plist.is_file()
    # the same version again with someone else at the screen: the recorded owner and the way back stay
    at_console(kit.run, "bob")
    known_user(kit.run, "bob", uid=502)
    result = root.postinstall(kit.system, NEWER, [keys.public])
    assert (result["previous"], result["pruned"], result["owner"]) == (VERSION, [], "alice")
    assert sorted(item.name for item in layout.versions.iterdir()) == [VERSION, NEWER]
    assert plistlib.loads(layout.runner_plist.read_bytes())["UserName"] == "alice"
    assert Maintenance.load(layout.maintenance_file).created == created  # recorded once


def test_postinstall_without_a_code_waits_and_starts_no_runner(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    claude = {"name": "claude-code", "url": "https://registry.example/claude.tgz", "sha256": "ab" * 32}
    make_version(layout, keys, fetch=[claude])
    safe_chain(layout)
    spool_owner(kit)
    known_user(kit.run)
    result = root.postinstall(command_line(kit), VERSION, [keys.public])
    assert result == {
        "version": VERSION,
        "previous": None,
        "pruned": [],
        "host_tools": "download_failed",
        "owner": "alice",
        "enrollment": "waiting",
    }
    assert kit.fetch.urls == ["https://registry.example/claude.tgz"]
    assert not layout.runner_plist.exists()
    assert kit.run.called(LAUNCHCTL, "bootstrap", "system", str(layout.runner_plist)) == []
    assert kit.run.called(LAUNCHCTL, "asuser") == []
    assert not owner_of(layout).home.exists()  # the home was asked of the owner, root made nothing
    found = state(kit)
    assert (found["enrolled"], found["revoked"], found["removed"], found["connected"]) == (
        False,
        "unknown",
        "unknown",
        False,
    )
    assert (found["host_tools"], found["enrollment"], found["device_id"]) == ("download_failed", "waiting", "")


def test_postinstall_without_an_owner_is_refused(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_version(layout, keys)
    safe_chain(layout)
    kit.run.answer(STAT_CONSOLE, stdout="loginwindow\n")
    with refused("owner_unknown"):
        root.postinstall(kit.system, VERSION, [keys.public])
    assert kit.run.called(SUDO) == []
    assert not layout.runner_plist.exists()
    assert Maintenance.load(layout.maintenance_file).owner is None


def test_postinstall_records_a_failed_enrollment(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_version(layout, keys)
    safe_chain(layout)
    write_request(kit)
    spool_owner(kit)
    known_user(kit.run)
    owner_acts(kit)
    runner_commands(
        kit.run,
        {"status": status_doc(enrolled=False), "enroll": runner_doc(ok=False, error={"code": "code_used"})},
    )
    result = root.postinstall(kit.system, VERSION, [keys.public])
    assert (result["enrollment"], result["enrollment_error"]) == ("failed", "code_used")
    assert not layout.runner_plist.exists()
    found = state(kit)
    assert (found["enrollment"], found["enrollment_error"], found["enrolled"]) == ("failed", "code_used", False)


def test_postinstall_refuses_a_manifest_of_another_version(kit: Kit, keys: Keys) -> None:
    make_version(kit.layout, keys, NEWER).rename(kit.layout.version_dir(VERSION))
    safe_chain(kit.layout)
    with refused("release_invalid", "version"):
        root.postinstall(kit.system, VERSION, [keys.public])
    assert kit.run.calls == []
    assert not kit.layout.current.exists()


def test_postinstall_refuses_a_changed_payload(kit: Kit, keys: Keys) -> None:
    folder = make_version(kit.layout, keys)
    (folder / "libexec/dfx-agent-runner").write_text("#!/bin/sh\nevil\n", encoding="utf-8")
    safe_chain(kit.layout)
    with refused("release_tree_mismatch"):
        root.postinstall(kit.system, VERSION, [keys.public])
    assert kit.run.calls == []


def test_postinstall_refuses_an_unsafe_chain_first(kit: Kit, keys: Keys) -> None:
    make_version(kit.layout, keys)
    safe_chain(kit.layout)
    kit.layout.payload.chmod(0o777)
    with refused("payload_parent_unsafe", "DFXAgentRunner"):
        root.postinstall(kit.system, VERSION, [keys.public])
    assert kit.run.calls == []


# --- the public state -----------------------------------------------------------------------------------------------


def test_publish_keeps_what_this_round_did_not_learn(kit: Kit) -> None:
    write_state(
        kit.layout.state_file,
        {"golden": "ready", "host_tools": "ready", "enrollment": "failed", "enrollment_error": "code_used", "x": 1},
    )
    kit.run.answer(PRINT_DISABLED, stdout=f'disabled services = {{\n\t"{RUNNER_LABEL}" => disabled\n}}\n')
    kit.run.answer(RUNNER_PRINT, rc=113)
    runner_commands(kit.run, {"status": status_doc(last_connect="never", revoked="key_revoked")})
    memory = Maintenance(feed="ok", pending={"version": NEWER}, bad=[OLD])
    fields = root.publish(context(kit), MANIFEST, memory)
    assert fields == {
        "version": VERSION,
        "hub": HUB,
        "owner": "alice",
        "enrolled": True,
        "device_id": "dev-1",
        "status": "read",
        "connected": False,
        "revoked": "key_revoked",
        "removed": "no",
        "daemon": "absent",
        "disabled_in_login_items": True,
        "update": {"feed": "ok", "pending": True, "bad": [OLD]},
        "updated_at": "2026-11-24T12:00:00Z",
        "golden": "ready",
        "host_tools": "ready",
        "enrollment": "failed",
        "enrollment_error": "code_used",
    }
    assert state(kit) == {"v": 1, **fields}
    # what this round learned replaces the old values; an enrollment without an error drops the old error
    kit.clock[0] = NOW.replace(hour=13)
    fields = root.publish(
        context(kit), MANIFEST, memory, host_tools="installed", golden="built", enrollment={"enrollment": "already"}
    )
    assert (fields["golden"], fields["host_tools"], fields["enrollment"]) == ("built", "installed", "already")
    assert "enrollment_error" not in fields
    assert fields["updated_at"] == "2026-11-24T13:00:00Z"


def test_publish_without_a_state_or_a_runner(kit: Kit) -> None:
    fields = root.publish(context(kit), MANIFEST, Maintenance())
    assert {key: fields[key] for key in ("enrolled", "device_id", "connected", "revoked", "removed")} == {
        "enrolled": False,
        "device_id": "",
        "connected": False,
        "revoked": "unknown",
        "removed": "unknown",
    }
    assert (fields["golden"], fields["host_tools"], fields["enrollment"]) == ("unknown", "unknown", "unknown")
    assert fields["daemon"] == "unknown"
    assert "enrollment_error" not in fields
    # a runner that is not enrolled has no device ID even when it names one
    runner_commands(kit.run, {"status": runner_doc({"fact": "runner.status.identity", "device_id": "x"})})
    fields = root.publish(context(kit), MANIFEST, Maintenance())
    assert (fields["enrolled"], fields["device_id"], fields["connected"], fields["revoked"]) == (
        False,
        "",
        False,
        "no",
    )


def test_publish_keeps_the_device_while_the_status_cannot_be_read(kit: Kit) -> None:
    """Right after an enrollment and at boot the runner's store is still starting and `status` fails: that is no
    removal (HOT-19, measured in a macOS guest). The device stays as last known; an enrollment of this run names it."""
    write_state(kit.layout.state_file, {"enrolled": True, "device_id": "dev-9", "enrollment": "enrolled"})
    runner_commands(kit.run, {"status": "not json"})
    fields = root.publish(context(kit), MANIFEST, Maintenance())
    assert (fields["enrolled"], fields["device_id"], fields["revoked"]) == (True, "dev-9", "unknown")
    assert fields["status"] == "unreadable"
    write_state(kit.layout.state_file, {"enrolled": False, "device_id": "stale"})
    fields = root.publish(context(kit), MANIFEST, Maintenance())
    assert (fields["enrolled"], fields["device_id"]) == (False, "")
    enrolled = {"enrollment": "enrolled", "device_id": "dev-10"}
    fields = root.publish(context(kit), MANIFEST, Maintenance(), enrollment=enrolled)
    assert (fields["enrolled"], fields["device_id"], fields["enrollment"]) == (True, "dev-10", "enrolled")
    # an enrollment without a device ID changes nothing (the status is still unreadable: the device is kept)
    fields = root.publish(context(kit), MANIFEST, Maintenance(), enrollment={"enrollment": "enrolled"})
    assert (fields["enrolled"], fields["device_id"]) == (True, "dev-10")
    # a readable status decides
    runner_commands(kit.run, {"status": status_doc()})
    fields = root.publish(context(kit), MANIFEST, Maintenance(), enrollment={"enrollment": "failed"})
    assert (fields["enrolled"], fields["device_id"], fields["status"]) == (True, "dev-1", "read")
    runner_commands(kit.run, {"status": status_doc(enrolled=False)})
    fields = root.publish(context(kit), MANIFEST, Maintenance())
    assert (fields["enrolled"], fields["device_id"], fields["status"]) == (False, "", "read")


# --- removal --------------------------------------------------------------------------------------------------------


def lima(kit: Kit, *names: str) -> list[Path]:
    """Lima instances in the owner's `~/.lima`."""
    folders = [owner_of(kit.layout).home / ".lima" / name for name in names]
    for folder in folders:
        folder.mkdir(parents=True)
    return folders


def test_the_vms_of_the_runner_are_the_owners_dfx_instances(kit: Kit) -> None:
    found = owner_of(kit.layout)
    assert root.vm_dirs(found) == []
    vms = lima(kit, "dfx-b", "default", "dfx-a", "other-dfx")
    assert root.vm_dirs(found) == [vms[2], vms[0]]


def test_a_linked_lima_folder_is_never_followed(kit: Kit, tmp_path: Path) -> None:
    found = owner_of(kit.layout)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "dfx-a").mkdir(parents=True)
    found.home.mkdir(parents=True)
    (found.home / ".lima").symlink_to(elsewhere)
    assert root.vm_dirs(found) == []
    (found.home / ".lima").unlink()
    (found.home / ".lima").write_text("not a folder", encoding="utf-8")
    assert root.vm_dirs(found) == []


def test_uninstall_refuses_an_unsafe_chain_before_it_changes_anything(kit: Kit, keys: Keys) -> None:
    safe_chain(kit.layout)
    (kit.layout.root / "Library").chmod(0o777)
    with refused("payload_parent_unsafe", "Library"):
        root.uninstall(kit.system, [keys.public])
    assert kit.run.calls == []
    assert kit.layout.payload.is_dir()


def test_the_vm_disks_are_kept_out_of_time_machine(kit: Kit) -> None:
    root.exclude_vms(context(kit))
    assert kit.run.calls == []
    first, second, _ = lima(kit, "dfx-1", "dfx-2", "mine")
    root.exclude_vms(context(kit))
    assert kit.run.calls == [
        as_owner(TMUTIL, "addexclusion", str(first)),
        as_owner(TMUTIL, "addexclusion", str(second)),
    ]


def test_nothing_is_left_of_no_installation(kit: Kit) -> None:
    nothing_left(kit)
    assert root.leftovers(kit.system, None) == []
    assert kit.run.calls == [RUNNER_PRINT, MAINTENANCE_PRINT, PKG_INFO]
    assert root.leftovers(kit.system, owner_of(kit.layout)) == []


def test_every_leftover_is_listed(kit: Kit, tmp_path: Path) -> None:
    layout = kit.layout
    layout.payload.mkdir(parents=True)
    daemons.write_plist(layout.runner_plist, {"Label": RUNNER_LABEL})
    layout.maintenance_plist.symlink_to(tmp_path / "missing")
    home = runner_home(layout)
    home.mkdir(parents=True)
    vms = lima(kit, "dfx-1", "colima")
    assert root.leftovers(kit.system, owner_of(layout)) == [
        f"path:{layout.payload}",
        f"path:{layout.runner_plist}",
        f"path:{layout.maintenance_plist}",
        f"path:{home}",
        f"path:{vms[0]}",
        f"service:{RUNNER_LABEL}",
        f"service:{MAINTENANCE_LABEL}",
        f"receipt:{PACKAGE_ID}",
    ]
    # a linked runner home is a leftover too; without an owner the owner's files are not looked at
    shutil.rmtree(home)
    home.symlink_to(tmp_path / "missing")
    nothing_left(kit)
    assert f"path:{home}" in root.leftovers(kit.system, owner_of(layout))
    assert root.leftovers(kit.system, None) == [
        f"path:{layout.payload}",
        f"path:{layout.runner_plist}",
        f"path:{layout.maintenance_plist}",
    ]


def test_background_items_naming_the_runner_are_counted(kit: Kit) -> None:
    assert root.background_items(kit.system) == 0
    kit.run.answer(
        SFLTOOL,
        stdout=f"Identifier: {RUNNER_LABEL}\nIdentifier: {MAINTENANCE_LABEL}\nIdentifier: com.other\n",
    )
    assert root.background_items(kit.system) == 2
    assert kit.run.calls == [SFLTOOL, SFLTOOL]


def test_the_recorded_owner(kit: Kit) -> None:
    assert root.recorded_owner(kit.system) is None
    assert kit.run.calls == []
    record_owner(kit)
    known_user(kit.run)
    assert root.recorded_owner(kit.system) == owner_of(kit.layout)
    unknown_user(kit.run)
    assert root.recorded_owner(kit.system) is None
    assert kit.run.calls == [READ_OWNER, READ_OWNER]


def installed_for_removal(kit: Kit, keys: Keys, created: tuple[str, ...] = ()) -> Path:
    """A complete installation of the owner alice: payload, both plists, the runner's home holding data."""
    layout = kit.layout
    folder = make_current(layout, keys)
    record_owner(kit, created=created)
    known_user(kit.run)
    daemons.write_plist(layout.runner_plist, {"Label": RUNNER_LABEL})
    daemons.write_plist(layout.maintenance_plist, {"Label": MAINTENANCE_LABEL})
    (runner_home(layout) / "pg").mkdir(parents=True)
    (runner_home(layout) / "pg/data").write_text("store", encoding="utf-8")
    owner_acts(kit)
    return folder


def test_uninstall_removes_everything_in_order(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    folder = installed_for_removal(kit, keys, created=(".local", ".local/share"))
    (owner_of(layout).home / "Documents").mkdir()
    kit.run.on(as_owner(RMDIR), lambda argv: Path(argv[-1]).rmdir())
    manifest = release.read_manifest(folder, [keys.public])
    nothing_left(kit)
    kit.run.answer(SFLTOOL, stdout=f"  {RUNNER_LABEL}\n")
    seen = runner_commands(kit.run, {"uninstall": runner_doc()})
    result = root.uninstall(kit.system, [keys.public])
    assert result == {
        "removed": [
            "runner_daemon",
            "runner_data",
            "runner_home",
            "created:.local/share",
            "created:.local",
            "maintenance_daemon",
            "receipt",
            "payload",
        ],
        "leftovers": [],
        "notes": [],
        "background_items": 1,
    }
    assert seen == ["uninstall"]
    found = owner_of(layout)
    ctx = runnerctl.Context(kit.system, found, runnerctl.runner_env(kit.system, found, manifest, 4096))
    home = str(runner_home(layout))
    assert kit.run.calls == [
        [LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"],
        READ_OWNER,
        SYSCTL_MEMORY,
        ctx.runner(["uninstall", "--json"]),
        as_owner(RM, "-rf", home),
        as_owner(RMDIR, str(found.home / ".local/share")),
        as_owner(RMDIR, str(found.home / ".local")),
        [LAUNCHCTL, "bootout", f"system/{MAINTENANCE_LABEL}"],
        PKG_FORGET,
        RUNNER_PRINT,
        MAINTENANCE_PRINT,
        PKG_INFO,
        SFLTOOL,
    ]
    for path in (layout.payload, found.home / ".local", layout.runner_plist, layout.maintenance_plist):
        assert not path.exists()
    assert (found.home / "Documents").is_dir()  # the rest of the owner's home stays
    assert layout.root.is_dir()
    no_pkill(kit)


def test_a_created_parent_the_owner_still_uses_stays(kit: Kit, keys: Keys) -> None:
    installed_for_removal(kit, keys, created=(".local", ".local/share"))
    kept = owner_of(kit.layout).home / ".local/bin"
    kept.mkdir()
    kit.run.answer(as_owner(RMDIR, str(kept.parent)), rc=1, stderr="Directory not empty")
    kit.run.on(as_owner(RMDIR, str(kept.parent / "share")), lambda argv: Path(argv[-1]).rmdir())
    nothing_left(kit)
    runner_commands(kit.run, {"uninstall": runner_doc()})
    result = root.uninstall(kit.system, [keys.public])
    assert "created:.local/share" in result["removed"]
    assert "created:.local" not in result["removed"]
    assert kept.is_dir()
    assert result["leftovers"] == []


def test_lima_directories_the_installation_made_are_removed_and_reported(kit: Kit, keys: Keys) -> None:
    """KOR-67: `~/.lima` and Lima's cache did not exist before the installation: Lima's configuration and the cache go,
    `~/.lima` too when nothing else is in it; what cannot go is a leftover."""
    installed_for_removal(kit, keys, created=(".lima", "Library/Caches/lima"))
    home = owner_of(kit.layout).home
    for item in (".lima/_config/networks.yaml", ".lima/_config/user", "Library/Caches/lima/download/x"):
        (home / item).parent.mkdir(parents=True, exist_ok=True)
        (home / item).write_text("x")
    kit.run.on(as_owner(RMDIR), lambda argv: Path(argv[-1]).rmdir())
    nothing_left(kit)
    runner_commands(kit.run, {"uninstall": runner_doc()})
    result = root.uninstall(kit.system, [keys.public])
    assert {"created:.lima", "created:Library/Caches/lima"} <= set(result["removed"])
    assert result["leftovers"] == []
    assert not (home / ".lima").exists()
    assert not (home / "Library/Caches/lima").exists()


def test_an_own_lima_instance_keeps_lima_and_is_no_leftover_of_ours(kit: Kit, keys: Keys) -> None:
    installed_for_removal(kit, keys, created=(".lima", "Library/Caches/lima"))  # Lima never ran for the runner
    home = owner_of(kit.layout).home
    (home / ".lima/mine").mkdir(parents=True)  # an instance the owner made with Lima, and Lima's configuration
    (home / ".lima/_config").mkdir()
    kit.run.answer(as_owner(RMDIR, str(home / ".lima")), rc=1, stderr="Directory not empty")
    nothing_left(kit)
    runner_commands(kit.run, {"uninstall": runner_doc()})
    result = root.uninstall(kit.system, [keys.public])
    assert not {"created:.lima", "created:Library/Caches/lima"} & set(result["removed"])
    assert (home / ".lima/mine").is_dir()
    assert (home / ".lima/_config").is_dir()
    assert result["leftovers"] == [f"path:{home / '.lima'}"]


def test_the_removal_waits_until_a_booted_out_service_has_ended(
    kit: Kit, keys: Keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A booted-out runner stays loaded until its process (with its database) has ended - a few seconds, measured in
    a macOS guest (HOT-19): what is left is asked again meanwhile, and a service that never ends stays a leftover."""
    installed_for_removal(kit, keys)
    nothing_left(kit)
    runner_commands(kit.run, {"uninstall": runner_doc()})
    kit.run.answers(RUNNER_PRINT, [(0, "", ""), (0, "", ""), (113, "", "")])
    slept: list[float] = []
    monkeypatch.setattr(root, "UNLOAD_POLLS", 5)
    monkeypatch.setattr(time, "sleep", slept.append)
    assert root.uninstall(kit.system, [keys.public])["leftovers"] == []
    assert slept == [root.UNLOAD_POLL_S, root.UNLOAD_POLL_S]
    kit.run.answer(RUNNER_PRINT, rc=0)
    slept.clear()
    assert root.uninstall(kit.system, [keys.public])["leftovers"] == [f"service:{RUNNER_LABEL}"]
    assert slept == [root.UNLOAD_POLL_S] * 5


def test_a_vm_the_runner_left_is_a_leftover_of_the_removal(kit: Kit, keys: Keys) -> None:
    installed_for_removal(kit, keys)
    [vm] = lima(kit, "dfx-task-1")
    nothing_left(kit)
    runner_commands(kit.run, {"uninstall": runner_doc()})
    result = root.uninstall(kit.system, [keys.public])
    assert result["leftovers"] == [f"path:{vm}"]
    assert vm.is_dir()  # root removes nothing in the owner's home itself


@pytest.mark.parametrize(
    ("reply", "damage", "note"),
    [
        (runner_doc(ok=False), False, "runner_uninstall:uninstall_failed"),
        ("", False, "runner_uninstall:runner_command_failed"),
        (runner_doc(), True, "runner_uninstall:release_invalid"),
    ],
)
def test_a_failed_runner_uninstall_is_noted_and_the_removal_goes_on(
    kit: Kit, keys: Keys, reply: str, damage: bool, note: str
) -> None:
    folder = installed_for_removal(kit, keys)
    if damage:
        (folder / "release.json.sig").unlink()
    runner_commands(kit.run, {"uninstall": reply})
    result = root.uninstall(kit.system, [keys.public])
    assert result["notes"] == [note]
    assert "runner_data" not in result["removed"]
    assert "runner_home" in result["removed"]
    assert "payload" in result["removed"]
    no_pkill(kit)


def test_without_an_active_version_the_runner_is_not_asked(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    record_owner(kit)
    known_user(kit.run)
    runner_commands(kit.run, {})
    result = root.uninstall(kit.system, [keys.public])
    assert "runner_data" not in result["removed"]
    assert not [call for call in kit.run.calls if "agent_runner" in call]
    assert kit.run.called(SUDO) == []  # no runner home: nothing to remove
    assert kit.run.called(TMUTIL) == []
    # a current version without its interpreter: the runner cannot run either
    folder = make_current(layout, keys)
    record_owner(kit)
    (folder / "python/bin/python3").unlink()
    root.uninstall(kit.system, [keys.public])
    assert not [call for call in kit.run.calls if "agent_runner" in call]


def test_without_a_recorded_owner_nothing_below_a_home_is_touched(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_current(layout, keys)
    home = runner_home(layout)
    home.mkdir(parents=True)
    at_console(kit.run)
    known_user(kit.run)
    result = root.uninstall(kit.system, [keys.public])
    assert result["removed"] == ["receipt", "payload"]
    assert kit.run.called(SUDO) == []
    assert kit.run.called(STAT_CONSOLE[0]) == []  # the user at the screen is not asked
    assert home.is_dir()


def test_an_owner_whose_account_is_gone_is_not_asked(kit: Kit) -> None:
    record_owner(kit)
    unknown_user(kit.run)
    runner_home(kit.layout).mkdir(parents=True)
    result = root.uninstall(kit.system, [])
    assert result["removed"] == ["receipt", "payload"]
    assert kit.run.called(SUDO) == []


def test_a_removal_without_a_receipt_names_none(kit: Kit) -> None:
    nothing_left(kit)
    kit.run.answer(PKG_FORGET, rc=1, stderr="No receipt for 'swiss.dfx.agent-runner' found at '/'.")
    result = root.uninstall(kit.system, [])
    assert result == {"removed": [], "leftovers": [], "notes": [], "background_items": 0}
    assert PKG_FORGET in kit.run.calls


def test_a_linked_runner_home_is_removed_by_the_owner_as_a_link(kit: Kit, tmp_path: Path) -> None:
    record_owner(kit)
    known_user(kit.run)
    target = tmp_path / "target"
    target.mkdir()
    home = runner_home(kit.layout)
    home.parent.mkdir(parents=True)
    home.symlink_to(target)
    result = root.uninstall(kit.system, [])
    assert result["removed"][0] == "runner_home"
    assert as_owner(RM, "-rf", str(home)) in kit.run.calls
    assert target.is_dir()


def test_a_home_that_cannot_be_removed_fails_the_removal(kit: Kit) -> None:
    record_owner(kit)
    known_user(kit.run)
    runner_home(kit.layout).mkdir(parents=True)
    kit.run.answer(as_owner(RM, "-rf"), rc=1)
    with refused("uninstall_failed", "runner_home"):
        root.uninstall(kit.system, [])


def test_the_self_removal_leaves_its_own_daemon_loaded_and_logs_the_result(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_current(layout, keys)
    daemons.write_plist(layout.maintenance_plist, {"Label": MAINTENANCE_LABEL})
    nothing_left(kit)
    kit.run.answer(MAINTENANCE_PRINT, rc=0)
    result = root.uninstall(kit.system, [keys.public], self_removal=True)
    assert result == {
        "removed": ["maintenance_daemon", "receipt", "payload"],
        "leftovers": [],
        "notes": [],
        "background_items": 0,
    }
    assert kit.run.called(LAUNCHCTL, "bootout", f"system/{MAINTENANCE_LABEL}") == []
    assert kit.run.calls[-1] == [LOGGER, "-t", "dfx-agent-runner", json.dumps(result, sort_keys=True)]
    # without the self-removal the same loaded daemon is a leftover and nothing is logged
    kit.run.calls.clear()
    assert root.uninstall(kit.system, [])["leftovers"] == [f"service:{MAINTENANCE_LABEL}"]
    assert kit.run.called(LOGGER) == []


# --- the golden of the active payload -------------------------------------------------------------------------------


def test_no_golden_without_its_sources(kit: Kit) -> None:
    assert root.ensure_golden(context(kit)) == "source_missing"
    assert kit.run.calls == []


def test_a_ready_golden_with_the_payloads_pins_is_kept(kit: Kit, keys: Keys) -> None:
    make_current(kit.layout, keys)
    building = {**READY, "state": "building"}
    seen = runner_commands(kit.run, {"image list": runner_doc(building, READY)})
    assert root.ensure_golden(context(kit)) == "ready"
    assert seen == ["image list"]


@pytest.mark.parametrize(("reply", "result"), [(runner_doc(), "built"), (runner_doc(ok=False), "build_failed")])
def test_a_golden_with_other_pins_is_built_as_the_owner(kit: Kit, keys: Keys, reply: str, result: str) -> None:
    make_current(kit.layout, keys)
    other_claude = {**READY, "claude_version": "2.1.329"}
    other_guest = {**READY, "guest_version": "0.0.9"}
    seen = runner_commands(kit.run, {"image list": runner_doc(other_claude, other_guest), "image build": reply})
    assert root.ensure_golden(context(kit)) == result
    assert seen == ["image list", "image build"]
    assert all(call[:3] == [SUDO, "-u", "alice"] for call in kit.run.calls)


def test_a_failed_golden_list_is_an_error(kit: Kit, keys: Keys) -> None:
    make_current(kit.layout, keys)
    runner_commands(kit.run, {"image list": runner_doc(ok=False)})
    with refused("runner_command_failed", "image"):
        root.ensure_golden(context(kit))


# --- the maintenance round ------------------------------------------------------------------------------------------


def test_detach_bootout_starts_launchctl_in_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[list[str], dict[str, Any]]] = []

    def popen(argv: list[str], **options: Any) -> None:
        started.append((argv, options))

    monkeypatch.setattr(subprocess, "Popen", popen)
    root.detach_bootout(MAINTENANCE_LABEL)
    assert started == [
        (
            ["/bin/launchctl", "bootout", f"system/{MAINTENANCE_LABEL}"],
            {
                "start_new_session": True,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def detach(self, label: str) -> None:
        self.events.append(f"detach {label}")


def maintain(kit: Kit, keys: Keys, recorder: Recorder) -> dict[str, Any]:
    return root.maintain(kit.system, [keys.public], sleep=recorder.sleep, detach=recorder.detach)


def installed(kit: Kit, keys: Keys, **extra: Any) -> None:
    """VERSION active in a safe chain, run by the recorded owner alice."""
    make_current(kit.layout, keys, **extra)
    safe_chain(kit.layout)
    record_owner(kit)
    known_user(kit.run)


def test_maintain_without_an_installation(kit: Kit, keys: Keys) -> None:
    safe_chain(kit.layout)
    recorder = Recorder()
    assert maintain(kit, keys, recorder) == {"result": "not_installed"}
    assert kit.run.calls == []
    kit.layout.payload.chmod(0o775)
    with refused("payload_parent_unsafe", "DFXAgentRunner"):
        maintain(kit, keys, recorder)


def test_maintain_without_an_owner_is_refused(kit: Kit, keys: Keys) -> None:
    make_current(kit.layout, keys)
    safe_chain(kit.layout)
    with refused("owner_unknown"):
        maintain(kit, keys, Recorder())
    assert kit.run.called(SUDO) == []


def test_maintain_completes_a_verified_removal_and_unloads_itself_last(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    installed(kit, keys)
    owner_acts(kit)
    runner_home(layout).mkdir(parents=True)
    nothing_left(kit)
    kit.run.answer(MAINTENANCE_PRINT, rc=0)
    runner_commands(kit.run, {"status": status_doc(removed="org-1"), "uninstall": runner_doc()})
    recorder = Recorder()
    kit.run.on([LOGGER], lambda argv: recorder.events.append("logger"))
    result = maintain(kit, keys, recorder)
    assert result == {
        "result": "removed",
        "removed": ["runner_data", "runner_home", "receipt", "payload"],
        "leftovers": [],
        "notes": [],
        "background_items": 0,
    }
    assert recorder.events == ["logger", f"detach {MAINTENANCE_LABEL}"]
    assert kit.run.called(LAUNCHCTL, "bootout", f"system/{MAINTENANCE_LABEL}") == []
    assert not layout.payload.exists()
    assert not runner_home(layout).exists()
    no_pkill(kit)


def test_a_revoked_key_alone_removes_nothing(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    installed(kit, keys)
    seen = runner_commands(kit.run, {"status": status_doc(revoked="key_revoked")})
    recorder = Recorder()
    result = maintain(kit, keys, recorder)
    assert result == {
        "result": "maintained",
        "golden": "not_enrolled",
        "host_tools": "not_pinned",
        "update": "download_failed",
    }
    assert seen == ["status", "status"]
    assert recorder.events == []
    assert layout.payload.is_dir()
    assert state(kit)["revoked"] == "key_revoked"


def test_maintain_ensures_the_golden_checks_for_updates_and_publishes(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    installed(kit, keys)
    [vm] = lima(kit, "dfx-golden")
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    seen = runner_commands(kit.run, {"status": status_doc(), "image list": runner_doc(READY)})
    recorder = Recorder()
    result = maintain(kit, keys, recorder)
    assert result == {
        "result": "maintained",
        "golden": "ready",
        "host_tools": "not_pinned",
        "update": "download_failed",
    }
    assert seen == ["status", "image list", "status"]
    assert as_owner(TMUTIL, "addexclusion", str(vm)) in kit.run.calls
    assert kit.fetch.urls == [HUB + "/downloads/runner/latest.json"]
    memory = Maintenance.load(layout.maintenance_file)
    assert (memory.owner, memory.last_check, memory.feed) == ("alice", "2026-11-24T12:00:00Z", "download_failed")
    found = state(kit)
    assert (found["owner"], found["golden"], found["host_tools"], found["daemon"], found["update"]) == (
        "alice",
        "ready",
        "not_pinned",
        "running",
        {"feed": "download_failed", "pending": False, "bad": []},
    )
    assert recorder.sleeps == []
    # within the hour the feed is not read again
    assert maintain(kit, keys, recorder)["update"] == "not_due"
    assert kit.fetch.urls == [HUB + "/downloads/runner/latest.json"]


def test_right_after_the_enrollment_maintain_waits_for_the_runner_and_builds_the_golden(kit: Kit, keys: Keys) -> None:
    """KOR-67: the run `postinstall` starts finds the runner loaded but its store still starting; it asks again and
    builds the golden now instead of reporting `not_enrolled` until the next run 15 minutes later."""
    installed(kit, keys)
    lima(kit, "dfx-golden")
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    seen = runner_commands(
        kit.run,
        {"status": ["", "", status_doc()], "image list": runner_doc(), "image build": runner_doc()},
    )
    recorder = Recorder()
    assert maintain(kit, keys, recorder)["golden"] == "built"
    assert seen[:4] == ["status", "status", "status", "image list"]
    assert recorder.sleeps == [root.STATUS_POLL_S, root.STATUS_POLL_S]


def test_a_runner_that_never_answers_is_asked_a_bounded_number_of_times(kit: Kit, keys: Keys) -> None:
    installed(kit, keys)
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    runner_commands(kit.run, {})
    recorder = Recorder()
    assert maintain(kit, keys, recorder)["golden"] == "not_enrolled"
    assert recorder.sleeps == [root.STATUS_POLL_S] * root.STATUS_POLLS


def test_a_failed_golden_list_is_reported_as_the_golden_state(kit: Kit, keys: Keys) -> None:
    installed(kit, keys)
    runner_commands(kit.run, {"status": status_doc(), "image list": runner_doc(ok=False)})
    result = maintain(kit, keys, Recorder())
    assert result["golden"] == "runner_command_failed"
    assert state(kit)["golden"] == "runner_command_failed"


def test_maintain_without_a_runner_answer(kit: Kit, keys: Keys) -> None:
    claude = {"name": "claude-code", "url": "https://registry.example/claude.tgz", "sha256": "ab" * 32}
    installed(kit, keys, fetch=[claude])
    result = maintain(kit, keys, Recorder())
    assert result == {
        "result": "maintained",
        "golden": "not_enrolled",
        "host_tools": "download_failed",
        "update": "not_enrolled",
    }
    assert kit.fetch.urls == ["https://registry.example/claude.tgz"]
    found = state(kit)
    assert (found["enrolled"], found["host_tools"], found["golden"]) == (False, "download_failed", "not_enrolled")


def test_maintain_publishes_the_version_an_update_installed(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    installed(kit, keys)
    kit.fetch.files.update(feed_files(keys, b"new package"))
    kit.hour[0] = 3
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    before = status_doc(last_connect="2026-11-24T11:00:00Z")
    after = status_doc(last_connect="2026-11-24T12:00:05Z")
    runner_commands(kit.run, {"status": [before, before, after], "image list": runner_doc(READY)})

    def install(argv: list[str]) -> None:
        make_version(layout, keys, NEWER)
        update.switch_current(kit.system, NEWER)

    kit.run.on([update.INSTALLER], install)
    result = maintain(kit, keys, Recorder())
    assert result["update"] == "updated"
    assert state(kit)["version"] == NEWER
    memory = Maintenance.load(layout.maintenance_file)
    assert (memory.previous, memory.pending, memory.feed, memory.owner) == (VERSION, None, "ok", "alice")


@pytest.mark.parametrize(
    ("daemon", "result", "active"), [(RUNNING, "updated", VERSION), ("\tstate = waiting\n", "rolled_back", OLD)]
)
def test_maintain_finishes_an_interrupted_update_first(
    kit: Kit, keys: Keys, daemon: str, result: str, active: str
) -> None:
    """VERSION was installed over OLD when the last round ended: kept when the runner is connected, else undone."""
    layout = kit.layout
    installed(kit, keys)
    make_version(layout, keys, OLD)
    Maintenance(owner="alice", pending={"version": VERSION, "previous": OLD}).save(layout.maintenance_file)
    kit.run.answer(RUNNER_PRINT, stdout=daemon)
    kit.fetch.files.update(feed_files(keys))
    seen = runner_commands(kit.run, {"status": status_doc(), "image list": runner_doc(READY)})
    found = maintain(kit, keys, Recorder())
    assert found["update"] == result
    assert kit.fetch.urls == []  # no feed is read while an update is unfinished
    assert seen[:2] == ["status", "image list"]
    assert os.readlink(layout.current) == str(layout.version_dir(active))
    memory = Maintenance.load(layout.maintenance_file)
    assert memory.pending is None
    assert memory.previous == (OLD if result == "updated" else None)
    assert state(kit)["version"] == active
    assert state(kit)["update"]["pending"] is False


def test_an_interrupted_update_is_finished_without_an_enrolled_runner(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    installed(kit, keys)
    make_version(layout, keys, OLD)
    Maintenance(owner="alice", pending={"version": VERSION, "previous": OLD}).save(layout.maintenance_file)
    result = maintain(kit, keys, Recorder())
    assert (result["update"], result["golden"]) == ("rolled_back", "not_enrolled")
    assert os.readlink(layout.current) == str(layout.version_dir(OLD))
    assert state(kit)["version"] == OLD
    memory = Maintenance.load(layout.maintenance_file)
    assert (memory.pending, memory.attempts) == (None, {VERSION: 1})


# --- the command line -----------------------------------------------------------------------------------------------


def output(kit: Kit) -> dict[str, Any]:
    document = json.loads(kit.out.getvalue().splitlines()[-1])
    assert isinstance(document, dict)
    return document


def test_run_prints_one_document(kit: Kit, keys: Keys) -> None:
    nothing_left(kit)
    assert root.run(["leftovers"], kit.system, lambda: [keys.public]) == 0
    assert output(kit) == {"ok": True, "leftovers": []}
    assert kit.out.getvalue().count("\n") == 1
    assert root.run(["uninstall"], kit.system, lambda: [keys.public]) == 0
    assert output(kit) == {"ok": True, "removed": ["receipt"], "leftovers": [], "notes": [], "background_items": 0}
    safe_chain(kit.layout)
    assert root.run(["maintain"], kit.system, lambda: [keys.public]) == 0
    assert output(kit) == {"ok": True, "result": "not_installed"}


def test_leftovers_of_a_named_or_the_recorded_owner(kit: Kit) -> None:
    home = runner_home(kit.layout)
    home.mkdir(parents=True)
    nothing_left(kit)
    known_user(kit.run)
    assert root.run(["leftovers", "--owner", "alice"], kit.system, lambda: []) == 0
    assert output(kit) == {"ok": True, "leftovers": [f"path:{home}"]}
    assert READ_OWNER in kit.run.calls
    kit.layout.payload.mkdir(parents=True, exist_ok=True)
    record_owner(kit)
    assert root.run(["leftovers"], kit.system, lambda: []) == 0
    assert output(kit)["leftovers"] == [f"path:{kit.layout.payload}", f"path:{home}"]
    # a name that is no regular account is not looked up
    calls = len(kit.run.calls)
    assert root.run(["leftovers", "--owner", "root"], kit.system, lambda: []) == 0
    assert output(kit)["leftovers"] == [f"path:{kit.layout.payload}"]
    assert READ_OWNER not in kit.run.calls[calls:]


def test_run_reports_errors_as_codes(kit: Kit, keys: Keys) -> None:
    safe_chain(kit.layout)
    assert root.run(["postinstall", "--version", VERSION], kit.system, lambda: [keys.public]) == 1
    assert output(kit) == {"ok": False, "error": {"code": "release_invalid", "name": "missing"}}
    assert root.run(["leftovers"], as_user(kit), lambda: [keys.public]) == 1
    assert output(kit) == {"ok": False, "error": {"code": "root_required", "name": None}}


@pytest.mark.parametrize("argv", [[], ["postinstall"], ["remove"], ["leftovers", "--all"], ["--help"]])
def test_other_command_lines_are_usage_errors(kit: Kit, argv: list[str]) -> None:
    assert root.run(argv, kit.system, lambda: []) == 1
    assert output(kit) == {"ok": False, "error": {"code": "usage", "name": None}}
    assert kit.run.calls == []


def test_postinstall_from_the_command_line(kit: Kit, keys: Keys) -> None:
    make_version(kit.layout, keys)
    safe_chain(kit.layout)
    spool_owner(kit)
    known_user(kit.run)
    assert root.run(["postinstall", "--version", VERSION], command_line(kit), lambda: [keys.public]) == 0
    document = output(kit)
    assert (document["ok"], document["version"], document["enrollment"], document["owner"]) == (
        True,
        VERSION,
        "waiting",
        "alice",
    )


def test_main_refuses_a_user_that_is_not_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.geteuid() == 0:  # the suite never runs as root; this guards the real system all the same
        raise AssertionError("the suite must not run as root")
    monkeypatch.setattr(sys, "argv", ["agent-runner-root", "leftovers"])
    assert root.main() == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": {"code": "root_required", "name": None}}
