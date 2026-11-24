"""U-RUN-install-owner: the account the runner of an own device runs as (ADR-074 section 3): only a regular account
(UID >= 501, a plain name, an absolute home) can be the owner, its home mapped below the layout's root; the user at
the screen; the owner the administrator script spooled (read without following a link, always removed); and the
order recorded, spooled, at the screen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_runner.install import owner
from agent_runner.install.owner import Owner

from .conftest import (
    STAT_CONSOLE,
    Kit,
    at_console,
    known_user,
    read_user,
    refused,
    unknown_user,
)


def spool_owner(kit: Kit, text: str) -> Path:
    path = kit.layout.spool / "owner"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("name", ["alice", "a", "_admin", "bob.smith", "j-doe_2", "x" * 32])
def test_plain_names_are_valid(name: str) -> None:
    assert owner.valid_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["", "root", "loginwindow", "_mbsetupuser", "Alice", "1alice", "-alice", "al ice", "al/ice", "x" * 33, "a\n"],
)
def test_other_names_are_not(name: str) -> None:
    assert owner.valid_name(name) is False


def test_a_regular_account_is_found_with_its_home_below_the_layout_root(kit: Kit) -> None:
    kit.run.answer(
        read_user("alice"),
        stdout="dsAttrTypeNative:x: 1\n  NFSHomeDirectory: /Volumes/P/alice  \nPrimaryGroupID: 20\nUniqueID: 501\n",
    )
    assert owner.lookup(kit.system, "alice") == Owner("alice", 501, 20, kit.layout.root / "Volumes/P/alice")
    assert kit.run.calls == [read_user("alice")]


def test_an_invalid_name_is_never_looked_up(kit: Kit) -> None:
    assert owner.lookup(kit.system, "root") is None
    assert owner.lookup(kit.system, "a b") is None
    assert kit.run.calls == []


def test_an_unknown_account_is_none(kit: Kit) -> None:
    unknown_user(kit.run, "bob")
    assert owner.lookup(kit.system, "bob") is None


@pytest.mark.parametrize(
    ("uid", "gid", "home"),
    [
        ("500", "20", "/Volumes/P/bob"),  # below the regular range (a service account)
        ("248", "248", "/var/empty"),
        ("-2", "20", "/Volumes/P/bob"),
        ("x", "20", "/Volumes/P/bob"),
        ("", "20", "/Volumes/P/bob"),
        ("501", "staff", "/Volumes/P/bob"),
        ("501", "", "/Volumes/P/bob"),
        ("501", "20", "Volumes/P/bob"),
        ("501", "20", "/"),
        ("501", "20", ""),
    ],
)
def test_only_a_regular_account_with_a_home_can_be_the_owner(kit: Kit, uid: str, gid: str, home: str) -> None:
    lines = [f"UniqueID: {uid}" if uid else "UniqueID:", f"PrimaryGroupID: {gid}", f"NFSHomeDirectory: {home}"]
    kit.run.answer(read_user("bob"), stdout="\n".join(lines) + "\n")
    assert owner.lookup(kit.system, "bob") is None


def test_the_lowest_regular_uid_is_accepted(kit: Kit) -> None:
    known_user(kit.run, "bob", uid=501, gid=20, home="/Volumes/Data/bob")
    assert owner.lookup(kit.system, "bob") == Owner("bob", 501, 20, kit.layout.root / "Volumes/Data/bob")


def test_the_user_at_the_screen(kit: Kit) -> None:
    at_console(kit.run, "alice")
    assert owner.console(kit.system) == "alice"
    assert kit.run.calls == [STAT_CONSOLE]


@pytest.mark.parametrize(("rc", "stdout"), [(1, "alice\n"), (0, "\n"), (0, "root\n"), (0, "loginwindow\n")])
def test_no_user_at_the_screen(kit: Kit, rc: int, stdout: str) -> None:
    kit.run.answer(STAT_CONSOLE, rc=rc, stdout=stdout)
    assert owner.console(kit.system) is None


def test_the_spooled_owner_is_read_once(kit: Kit) -> None:
    assert owner.take_spooled(kit.system) is None
    path = spool_owner(kit, "alice\n")
    assert owner.take_spooled(kit.system) == "alice"
    assert not path.exists()
    assert owner.take_spooled(kit.system) is None


@pytest.mark.parametrize("text", ["root\n", "", "Alice", "alice bob", "a" * 70])
def test_a_spooled_name_that_is_no_regular_name_is_ignored_and_removed(kit: Kit, text: str) -> None:
    path = spool_owner(kit, text)
    assert owner.take_spooled(kit.system) is None
    assert not path.exists()


def test_a_linked_spool_entry_is_removed_without_following_it(kit: Kit, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("mallory\n", encoding="ascii")
    kit.layout.spool.mkdir(parents=True)
    (kit.layout.spool / "owner").symlink_to(target)
    assert owner.take_spooled(kit.system) is None
    assert not (kit.layout.spool / "owner").is_symlink()
    assert target.read_text(encoding="ascii") == "mallory\n"


def test_a_spool_entry_that_is_no_regular_file_is_ignored_and_removed(kit: Kit) -> None:
    path = kit.layout.spool / "owner"
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    assert owner.take_spooled(kit.system) is None  # O_NONBLOCK: no writer needed, root never waits
    assert not path.exists()


def test_a_directory_in_the_spool_is_ignored_and_left(kit: Kit) -> None:
    path = kit.layout.spool / "owner"
    path.mkdir(parents=True)
    assert owner.take_spooled(kit.system) is None
    assert path.is_dir()


def test_the_recorded_owner_comes_first_and_the_spool_is_emptied(kit: Kit) -> None:
    path = spool_owner(kit, "bob\n")
    known_user(kit.run, "alice")
    known_user(kit.run, "bob", uid=502)
    found = owner.resolve(kit.system, "alice")
    assert found == Owner("alice", 501, 20, kit.layout.root / "Users/alice")
    assert not path.exists()
    assert kit.run.calls[0] == STAT_CONSOLE  # the screen is read before the first lookup
    assert kit.run.called(*read_user("bob")) == []


def test_the_spooled_owner_replaces_a_recorded_one_that_is_gone(kit: Kit) -> None:
    spool_owner(kit, "bob\n")
    unknown_user(kit.run, "alice")
    known_user(kit.run, "bob", uid=502)
    assert owner.resolve(kit.system, "alice").name == "bob"


def test_installer_app_takes_the_user_at_the_screen(kit: Kit) -> None:
    at_console(kit.run, "carol")
    known_user(kit.run, "carol", uid=503)
    found = owner.resolve(kit.system, None)
    assert (found.name, found.uid) == ("carol", 503)
    assert kit.run.calls == [STAT_CONSOLE, read_user("carol")]


def test_without_any_owner_the_installation_is_refused(kit: Kit) -> None:
    kit.run.answer(STAT_CONSOLE, stdout="loginwindow\n")
    with refused("owner_unknown"):
        owner.resolve(kit.system, None)
    at_console(kit.run, "root")
    spool_owner(kit, "_mbsetupuser\n")
    with refused("owner_unknown"):
        owner.resolve(kit.system, "root")
    assert [call for call in kit.run.calls if call != STAT_CONSOLE] == []
    known_user(kit.run, "dave", uid=499)
    at_console(kit.run, "dave")
    with refused("owner_unknown"):
        owner.resolve(kit.system, "dave")
