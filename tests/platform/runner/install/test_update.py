"""U-RUN-install-update: updates of an own device with a way back (ADR-074 section 6): the feed trusted only with the
installed version's keys, unexpired and newer, the window and the grace period, the download checked by digest into
a root-only directory, the snapshot of store and plists before `installer`, the health wait, and the rollback that
restores store, plists and `current`, with a version marked bad after two failures."""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from agent_runner.install import daemons, update
from agent_runner.install.layout import MAINTENANCE_LABEL, RUNNER_LABEL
from agent_runner.install.state import Maintenance

from .conftest import (
    HUB,
    LAUNCHCTL,
    NEWER,
    NOW,
    RUNNER_PRINT,
    VERSION,
    Keys,
    Kit,
    as_owner,
    context,
    feed_files,
    make_current,
    make_version,
    new_keys,
    refused,
    runner_commands,
    runner_home,
    status_doc,
    steps,
)

OLDER = "2026.11.23.0800"
PACKAGE = b"package of the new version"
FEED_URL = HUB + "/downloads/runner/latest.json"
SIGNATURE_URL = FEED_URL + ".sig"
PACKAGE_URL = HUB + f"/downloads/runner/DFX-Agent-Runner-{NEWER}.pkg"
BEFORE = "2026-11-24T11:00:00Z"
AFTER = "2026-11-24T12:00:30Z"
RUNNING = "\tstate = running\n"
BOOTOUT_RUNNER = [LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def stamp(hours: float = 0) -> str:
    return (NOW + timedelta(hours=hours)).strftime(update.TIME_FORMAT)


def check(
    kit: Kit, keys: Keys, memory: Maintenance, *, busy: int = 0, sleeps: list[float] | None = None
) -> update.Outcome:
    used = [] if sleeps is None else sleeps
    return update.check(context(kit), memory, hub=HUB, active=VERSION, keys=[keys.public], busy=busy, sleep=used.append)


def real_file_steps(kit: Kit) -> None:
    """`cp -cpR`, `rm -rf` and `mv` of the owner act on the files."""

    def copy(argv: list[str]) -> None:
        shutil.copytree(argv[-2], argv[-1])

    kit.run.on(as_owner("/bin/cp", "-cpR"), copy)
    kit.run.on(as_owner("/bin/rm", "-rf"), lambda argv: shutil.rmtree(argv[-1], ignore_errors=True))
    kit.run.on(as_owner("/bin/mv"), lambda argv: os.rename(argv[-2], argv[-1]))


def store(kit: Kit) -> Path:
    return runner_home(kit.layout) / "pg"


def pg_stop(kit: Kit, version_dir: Path) -> list[str]:
    data = str(store(kit) / "data")
    return as_owner(str(version_dir / "postgresql/bin/pg_ctl"), "-D", data, "-m", "fast", "-w", "stop")


# --- the feed -------------------------------------------------------------------------------------------------------


def test_the_feed_url() -> None:
    assert update.feed_url("https://hub.example/", "latest.json") == FEED_URL
    assert update.feed_url("https://hub.example", "x.pkg") == HUB + "/downloads/runner/x.pkg"


def test_the_feed_is_read_and_verified(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(keys, PACKAGE))
    feed = update.read_feed(kit.system, HUB, [keys.public])
    assert (feed.version, feed.package) == (NEWER, f"DFX-Agent-Runner-{NEWER}.pkg")
    assert kit.fetch.urls == [FEED_URL, SIGNATURE_URL]
    with refused("release_signature_invalid"):
        update.read_feed(kit.system, HUB, [new_keys().public])
    with refused("release_key_missing"):
        update.read_feed(kit.system, HUB, [])
    kit.fetch.files[SIGNATURE_URL] = b"\xff" * 86
    with refused("release_signature_invalid"):
        update.read_feed(kit.system, HUB, [keys.public])
    kit.fetch.files[SIGNATURE_URL] = b"x" * 1025
    with refused("download_failed", "size"):
        update.read_feed(kit.system, HUB, [keys.public])
    kit.fetch.files[FEED_URL] = b"x" * (64 * 1024 + 1)
    with refused("download_failed", "size"):
        update.read_feed(kit.system, HUB, [keys.public])


@pytest.mark.parametrize(
    ("hour", "seen", "result"),
    [
        (2, None, True),
        (4, None, True),
        (5, None, False),
        (1, None, False),
        (12, stamp(-24 * 7), True),
        (12, stamp(-24 * 7 + 0.01), False),
        (12, stamp(-24 * 30), True),
    ],
)
def test_an_update_is_due_in_the_window_or_after_the_grace_period(
    kit: Kit, keys: Keys, hour: int, seen: str | None, result: bool
) -> None:
    kit.fetch.files.update(feed_files(keys))
    feed = update.read_feed(kit.system, HUB, [keys.public])
    kit.hour[0] = hour
    memory = Maintenance(first_seen={} if seen is None else {NEWER: seen})
    assert update.due(kit.system, memory, feed) is result
    assert memory.first_seen == {NEWER: seen or stamp()}


def test_a_version_waits_seven_days_from_when_it_was_first_seen(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(keys))
    feed = update.read_feed(kit.system, HUB, [keys.public])
    memory = Maintenance()
    assert update.due(kit.system, memory, feed) is False
    kit.clock[0] = NOW + timedelta(days=6, hours=23)
    assert update.due(kit.system, memory, feed) is False
    kit.clock[0] = NOW + timedelta(days=7)
    assert update.due(kit.system, memory, feed) is True
    assert memory.first_seen == {NEWER: stamp()}


# --- snapshot and rollback ------------------------------------------------------------------------------------------


def test_the_snapshot_stops_the_runner_then_clones_the_store_and_keeps_the_plists(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_current(layout, keys)
    (store(kit) / "data").mkdir(parents=True)
    (store(kit) / "data/table").write_text("rows", encoding="utf-8")
    daemons.write_plist(layout.runner_plist, {"Label": RUNNER_LABEL})
    daemons.write_plist(layout.maintenance_plist, {"Label": MAINTENANCE_LABEL})
    real_file_steps(kit)
    update.snapshot(context(kit))
    clone = store(kit).with_name("pg.before-update")
    assert kit.run.calls == [
        BOOTOUT_RUNNER,
        pg_stop(kit, layout.current),
        as_owner("/bin/rm", "-rf", str(clone)),
        as_owner("/bin/cp", "-cpR", str(store(kit)), str(clone)),
    ]
    assert (clone / "data/table").read_text(encoding="utf-8") == "rows"
    backup = layout.downloads / "plists"
    assert mode(backup) == 0o700
    assert (backup / layout.runner_plist.name).read_bytes() == layout.runner_plist.read_bytes()
    assert (backup / layout.maintenance_plist.name).read_bytes() == layout.maintenance_plist.read_bytes()
    update.drop_snapshot(context(kit))
    assert kit.run.calls[-1] == as_owner("/bin/rm", "-rf", str(clone))
    assert not clone.exists()


def test_a_snapshot_without_a_store_or_plists(kit: Kit) -> None:
    update.snapshot(context(kit))
    assert kit.run.called(*as_owner("/bin/cp")) == []
    assert list((kit.layout.downloads / "plists").iterdir()) == []


def test_a_failed_snapshot_is_an_error(kit: Kit) -> None:
    (store(kit) / "data").mkdir(parents=True)
    kit.run.answer(as_owner("/bin/cp"), rc=1)
    with refused("update_snapshot_failed"):
        update.snapshot(context(kit))
    kit.run.answer(BOOTOUT_RUNNER, rc=5, stderr="Input/output error")
    kit.run.calls.clear()
    with refused("service_bootout_failed", RUNNER_LABEL):
        update.snapshot(context(kit))
    assert kit.run.calls == [BOOTOUT_RUNNER]


def test_current_is_switched_by_renaming_a_new_link(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    with refused("version_missing", VERSION):
        update.switch_current(kit.system, VERSION)
    make_version(layout, keys)
    make_version(layout, keys, NEWER)
    (layout.payload / ".current.link").symlink_to("/stale")
    update.switch_current(kit.system, VERSION)
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))
    update.switch_current(kit.system, NEWER)
    assert os.readlink(layout.current) == str(layout.version_dir(NEWER))
    assert sorted(item.name for item in layout.payload.iterdir()) == ["current", "versions"]


def test_the_rollback_restores_store_plists_and_current(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_version(layout, keys)
    make_current(layout, keys, NEWER)
    clone = store(kit).with_name("pg.before-update")
    (clone / "data").mkdir(parents=True)
    (clone / "data/table").write_text("before", encoding="utf-8")
    (store(kit) / "data").mkdir(parents=True)
    (store(kit) / "data/table").write_text("after", encoding="utf-8")
    backup = layout.downloads / "plists"
    backup.mkdir(parents=True)
    old_runner = {"Label": RUNNER_LABEL, "EnvironmentVariables": {"A": "old"}}
    daemons.write_plist(backup / layout.runner_plist.name, old_runner)
    daemons.write_plist(layout.runner_plist, {"Label": RUNNER_LABEL, "EnvironmentVariables": {"A": "new"}})
    real_file_steps(kit)
    update.rollback(context(kit), VERSION)
    assert kit.run.calls == [
        BOOTOUT_RUNNER,
        pg_stop(kit, layout.version_dir(VERSION)),
        as_owner("/bin/rm", "-rf", str(store(kit))),
        as_owner("/bin/mv", str(clone), str(store(kit))),
        BOOTOUT_RUNNER,
        [LAUNCHCTL, "bootstrap", "system", str(layout.runner_plist)],
    ]
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "before"
    assert not clone.exists()
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))
    assert plistlib.loads(layout.runner_plist.read_bytes()) == old_runner
    assert mode(layout.runner_plist) == 0o644
    assert not layout.maintenance_plist.exists()


def test_a_rollback_without_a_clone_or_plists_only_switches_back(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_version(layout, keys)
    make_current(layout, keys, NEWER)
    update.rollback(context(kit), VERSION)
    assert kit.run.calls == [BOOTOUT_RUNNER, pg_stop(kit, layout.version_dir(VERSION))]
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))


def test_a_failed_store_restore_is_an_error(kit: Kit, keys: Keys) -> None:
    make_version(kit.layout, keys)
    store(kit).with_name("pg.before-update").mkdir(parents=True)
    kit.run.answer(as_owner("/bin/mv"), rc=1)
    with refused("update_rollback_failed", "store"):
        update.rollback(context(kit), VERSION)
    kit.run.answer(as_owner("/bin/rm", "-rf", str(store(kit))), rc=1)
    with refused("update_rollback_failed", "store"):
        update.rollback(context(kit), VERSION)


def test_a_kept_plist_that_is_no_dictionary_is_an_error(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    make_version(layout, keys)
    backup = layout.downloads / "plists"
    backup.mkdir(parents=True)
    (backup / layout.maintenance_plist.name).write_bytes(plistlib.dumps(["not", "a", "dict"]))
    with refused("update_rollback_failed", "plist"):
        update.rollback(context(kit), VERSION)


# --- health ---------------------------------------------------------------------------------------------------------


def test_healthy_needs_a_running_daemon_with_a_new_connection(kit: Kit) -> None:
    seen = runner_commands(kit.run, {"status": status_doc(last_connect=AFTER)})
    assert update.healthy(context(kit), BEFORE) is False  # launchd reports no state
    assert seen == []
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    assert update.healthy(context(kit), BEFORE) is True
    assert update.healthy(context(kit), AFTER) is False
    runner_commands(kit.run, {"status": status_doc(last_connect="never")})
    assert update.healthy(context(kit), "never") is False
    runner_commands(kit.run, {})
    assert update.healthy(context(kit), BEFORE) is False


def test_the_wait_polls_every_30_seconds_for_10_minutes(kit: Kit) -> None:
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    seen = runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    sleeps: list[float] = []
    assert update.wait_healthy(context(kit), BEFORE, sleeps.append) is False
    assert sleeps == [30.0] * 20
    assert len(seen) == 21


def test_the_wait_ends_with_the_first_connection(kit: Kit) -> None:
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    runner_commands(kit.run, {"status": [status_doc(last_connect=BEFORE)] * 3 + [status_doc(last_connect=AFTER)]})
    sleeps: list[float] = []
    assert update.wait_healthy(context(kit), BEFORE, sleeps.append) is True
    assert sleeps == [30.0] * 3


# --- download -------------------------------------------------------------------------------------------------------


def test_the_package_is_downloaded_root_only_and_checked(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(keys, PACKAGE))
    feed = update.read_feed(kit.system, HUB, [keys.public])
    kit.layout.downloads.mkdir(parents=True, mode=0o755)
    kit.layout.downloads.chmod(0o755)
    path = update.download(kit.system, HUB, feed)
    assert path == kit.layout.downloads / f"DFX-Agent-Runner-{NEWER}.pkg"
    assert path.read_bytes() == PACKAGE
    assert (mode(path), mode(kit.layout.downloads)) == (0o600, 0o700)
    assert kit.fetch.urls[-1] == PACKAGE_URL


def test_a_package_with_another_digest_is_not_written(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(keys, PACKAGE))
    feed = update.read_feed(kit.system, HUB, [keys.public])
    kit.fetch.files[PACKAGE_URL] = PACKAGE + b"!"
    with refused("package_digest_mismatch"):
        update.download(kit.system, HUB, feed)
    assert not kit.layout.downloads.exists()


# --- one round ------------------------------------------------------------------------------------------------------


def test_the_feed_is_read_at_most_once_an_hour(kit: Kit, keys: Keys) -> None:
    memory = Maintenance(last_check=stamp(-0.5))
    assert check(kit, keys, memory) == update.Outcome("not_due")
    assert kit.fetch.urls == []
    memory.last_check = stamp(-1)
    assert check(kit, keys, memory) == update.Outcome("download_failed")
    assert (memory.last_check, memory.feed) == (stamp(), "download_failed")
    assert kit.fetch.urls == [FEED_URL]


def test_a_feed_signed_by_another_key_is_refused(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(new_keys(), PACKAGE))
    memory = Maintenance()
    assert check(kit, keys, memory) == update.Outcome("release_signature_invalid")
    assert memory.feed == "release_signature_invalid"


def test_an_expired_feed_stops_updates(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(keys, signed_at="2026-08-01T12:00:00Z", expires="2026-11-24T12:00:00Z"))
    memory = Maintenance()
    assert check(kit, keys, memory) == update.Outcome("feed_expired", NEWER)
    assert memory.feed == "feed_expired"
    assert kit.fetch.urls == [FEED_URL, SIGNATURE_URL]


@pytest.mark.parametrize(("version", "bad"), [(VERSION, []), (OLDER, []), (NEWER, [NEWER])])
def test_no_update_to_the_same_an_older_or_a_bad_version(kit: Kit, keys: Keys, version: str, bad: list[str]) -> None:
    kit.fetch.files.update(feed_files(keys, version=version))
    memory = Maintenance(bad=list(bad))
    assert check(kit, keys, memory) == update.Outcome("current", VERSION)
    assert memory.feed == "ok"
    assert memory.first_seen == {}


def test_a_working_session_or_the_time_of_day_makes_it_wait(kit: Kit, keys: Keys) -> None:
    kit.fetch.files.update(feed_files(keys, PACKAGE))
    kit.hour[0] = 3
    assert check(kit, keys, Maintenance(), busy=1) == update.Outcome("waiting", NEWER)
    kit.hour[0] = 12
    memory = Maintenance()
    assert check(kit, keys, memory) == update.Outcome("waiting", NEWER)
    assert memory.first_seen == {NEWER: stamp()}
    assert PACKAGE_URL not in kit.fetch.urls
    assert kit.run.calls == []


@pytest.mark.parametrize(
    ("package", "result"), [(PACKAGE + b"!", "package_digest_mismatch"), (None, "download_failed")]
)
def test_a_bad_package_ends_the_round_before_anything_stops(
    kit: Kit, keys: Keys, package: bytes | None, result: str
) -> None:
    kit.fetch.files.update(feed_files(keys, PACKAGE))
    if package is None:
        del kit.fetch.files[PACKAGE_URL]
    else:
        kit.fetch.files[PACKAGE_URL] = package
    kit.hour[0] = 3
    memory = Maintenance()
    assert check(kit, keys, memory) == update.Outcome(result, NEWER)
    assert kit.run.calls == []
    assert memory.pending is None


def prepared(kit: Kit, keys: Keys) -> None:
    """VERSION active with a store and a runner plist; NEWER in the feed, inside the window."""
    layout = kit.layout
    make_current(layout, keys)
    (store(kit) / "data").mkdir(parents=True)
    (store(kit) / "data/table").write_text("before", encoding="utf-8")
    daemons.write_plist(layout.runner_plist, {"Label": RUNNER_LABEL, "EnvironmentVariables": {"V": VERSION}})
    kit.fetch.files.update(feed_files(keys, PACKAGE))
    kit.hour[0] = 3
    real_file_steps(kit)


def new_version_installer(kit: Kit, keys: Keys, rc: int = 0) -> list[bytes]:
    """`installer` acting like the new version's postinstall: it changes the store, the plist and `current`."""
    installed: list[bytes] = []
    layout = kit.layout

    def install(argv: list[str]) -> None:
        installed.append(Path(argv[2]).read_bytes())
        saved = Maintenance.load(layout.maintenance_file)
        assert saved.pending is not None
        assert saved.pending["version"] == NEWER
        if not layout.version_dir(NEWER).exists():
            make_version(layout, keys, NEWER)
        update.switch_current(kit.system, NEWER)
        (store(kit) / "data/table").write_text("migrated", encoding="utf-8")
        daemons.write_plist(layout.runner_plist, {"Label": RUNNER_LABEL, "EnvironmentVariables": {"V": NEWER}})

    kit.run.on([update.INSTALLER], install)
    kit.run.answer([update.INSTALLER], rc=rc)
    return installed


def test_an_update_is_kept_when_the_runner_connects(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    installed = new_version_installer(kit, keys)
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    runner_commands(kit.run, {"status": [status_doc(last_connect=BEFORE), status_doc(last_connect=AFTER)]})
    memory = Maintenance(attempts={NEWER: 1, OLDER: 2})
    sleeps: list[float] = []
    assert check(kit, keys, memory, sleeps=sleeps) == update.Outcome("updated", NEWER)
    package = layout.downloads / f"DFX-Agent-Runner-{NEWER}.pkg"
    clone = store(kit).with_name("pg.before-update")
    assert steps(kit.run) == [
        "runner status",
        BOOTOUT_RUNNER,
        pg_stop(kit, layout.current),
        as_owner("/bin/rm", "-rf", str(clone)),
        as_owner("/bin/cp", "-cpR", str(store(kit)), str(clone)),
        [update.INSTALLER, "-pkg", str(package), "-target", str(layout.root)],
        RUNNER_PRINT,
        "runner status",
        as_owner("/bin/rm", "-rf", str(clone)),
    ]
    assert installed == [PACKAGE]
    assert not package.exists()
    assert not clone.exists()
    assert (memory.previous, memory.pending, memory.attempts, memory.bad) == (VERSION, None, {OLDER: 2}, [])
    assert os.readlink(layout.current) == str(layout.version_dir(NEWER))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "migrated"
    assert sleeps == []


def test_a_failed_installer_rolls_back_and_counts_the_attempt(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    new_version_installer(kit, keys, rc=1)
    runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    memory = Maintenance()
    assert check(kit, keys, memory) == update.Outcome("rolled_back", NEWER)
    assert (memory.pending, memory.attempts, memory.bad, memory.previous) == (None, {NEWER: 1}, [], None)
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "before"
    assert not store(kit).with_name("pg.before-update").exists()
    runner = plistlib.loads(layout.runner_plist.read_bytes())
    assert runner["EnvironmentVariables"] == {"V": VERSION}
    assert kit.run.calls[-1] == [LAUNCHCTL, "bootstrap", "system", str(layout.runner_plist)]
    assert kit.run.called(LAUNCHCTL, "print") == []  # no health wait after a failed installer
    # the second failure marks the version bad; it is never tried again
    memory.last_check = None
    assert check(kit, keys, memory) == update.Outcome("rolled_back", NEWER)
    assert (memory.attempts, memory.bad) == ({NEWER: 2}, [NEWER])
    memory.last_check = None
    kit.run.calls.clear()
    assert check(kit, keys, memory) == update.Outcome("current", VERSION)
    assert kit.run.calls == []


def test_a_runner_that_never_connects_is_rolled_back_after_ten_minutes(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    new_version_installer(kit, keys)
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    memory = Maintenance()
    sleeps: list[float] = []
    assert check(kit, keys, memory, sleeps=sleeps) == update.Outcome("rolled_back", NEWER)
    assert sum(sleeps) == 600.0
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "before"
    assert memory.attempts == {NEWER: 1}


def not_started(kit: Kit, keys: Keys, memory: Maintenance) -> None:
    """The round ended before or inside the snapshot: no installer, no attempt, no way back, the package removed and
    the runner started again from its unchanged plist."""
    layout = kit.layout
    installed = new_version_installer(kit, keys)
    assert check(kit, keys, memory) == update.Outcome("update_not_started", NEWER)
    assert installed == []
    assert kit.run.called(update.INSTALLER) == []
    assert (memory.pending, memory.attempts, memory.bad, memory.previous) == (None, {OLDER: 1}, [], None)
    assert not (layout.downloads / f"DFX-Agent-Runner-{NEWER}.pkg").exists()
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "before"
    assert not Maintenance.load(layout.maintenance_file).pending
    assert kit.run.called(*as_owner("/bin/mv")) == []  # the store was not restored
    assert kit.run.calls[-1] == [LAUNCHCTL, "bootstrap", "system", str(layout.runner_plist)]


def test_a_failing_status_before_the_snapshot_starts_no_update(kit: Kit, keys: Keys) -> None:
    prepared(kit, keys)
    seen = runner_commands(kit.run, {"status": "crash\n"})
    not_started(kit, keys, Maintenance(attempts={OLDER: 1}))
    assert seen == ["status"]
    bootstrap = [LAUNCHCTL, "bootstrap", "system", str(kit.layout.runner_plist)]
    assert steps(kit.run) == ["runner status", BOOTOUT_RUNNER, bootstrap]  # only the start again


def test_a_failing_snapshot_starts_no_update(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    kit.run.answer(as_owner("/bin/cp"), rc=1)
    not_started(kit, keys, Maintenance(attempts={OLDER: 1}))
    assert BOOTOUT_RUNNER in kit.run.calls
    assert list(layout.downloads.iterdir()) == []  # neither the package nor a copy of the plists


def test_a_stale_clone_that_cannot_be_removed_starts_no_update(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    clone = store(kit).with_name("pg.before-update")
    kit.run.answer(as_owner("/bin/rm", "-rf", str(clone)), rc=1, stderr="Operation not permitted")
    not_started(kit, keys, Maintenance(attempts={OLDER: 1}))
    assert kit.run.called(*as_owner("/bin/cp")) == []
    assert list(layout.downloads.iterdir()) == []


def test_an_update_not_started_without_a_runner_plist_starts_nothing(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    layout.runner_plist.unlink()
    runner_commands(kit.run, {"status": "crash\n"})
    memory = Maintenance()
    assert check(kit, keys, memory) == update.Outcome("update_not_started", NEWER)
    assert (memory.pending, memory.attempts) == (None, {})
    assert kit.run.called(LAUNCHCTL) == []
    assert list(layout.downloads.iterdir()) == []


def test_the_pending_update_saves_the_connection_before_it(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    prepared(kit, keys)
    saved: list[dict[str, str] | None] = []
    kit.run.on([update.INSTALLER], lambda argv: saved.append(Maintenance.load(layout.maintenance_file).pending))
    kit.run.answer([update.INSTALLER], rc=1)
    runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    assert check(kit, keys, Maintenance()) == update.Outcome("rolled_back", NEWER)
    assert saved == [{"version": NEWER, "previous": VERSION, "started": stamp(), "before": BEFORE}]


def test_the_attempt_is_counted_before_the_way_back_runs(kit: Kit, keys: Keys) -> None:
    """A way back that fails itself still ends the retries: the attempt was counted first."""
    layout = kit.layout
    prepared(kit, keys)
    new_version_installer(kit, keys, rc=1)
    runner_commands(kit.run, {"status": status_doc(last_connect=BEFORE)})
    kit.run.answer(as_owner("/bin/mv"), rc=1)
    memory = Maintenance(attempts={NEWER: 1})
    with refused("update_rollback_failed", "store"):
        check(kit, keys, memory)
    assert (memory.attempts, memory.bad, memory.pending) == ({NEWER: 2}, [NEWER], None)
    assert not (layout.downloads / f"DFX-Agent-Runner-{NEWER}.pkg").exists()


# --- an interrupted update ------------------------------------------------------------------------------------------


def interrupted(kit: Kit, keys: Keys) -> Path:
    """NEWER was installed (active) over VERSION; the clone of the store is still there."""
    layout = kit.layout
    make_version(layout, keys)
    make_current(layout, keys, NEWER)
    clone = store(kit).with_name("pg.before-update")
    (clone / "data").mkdir(parents=True)
    (clone / "data/table").write_text("before", encoding="utf-8")
    (store(kit) / "data").mkdir(parents=True)
    (store(kit) / "data/table").write_text("migrated", encoding="utf-8")
    real_file_steps(kit)
    return clone


def test_an_interrupted_update_is_kept_when_the_runner_is_connected(kit: Kit, keys: Keys) -> None:
    layout = kit.layout
    clone = interrupted(kit, keys)
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    seen = runner_commands(kit.run, {"status": status_doc(last_connect=AFTER)})
    memory = Maintenance(previous=OLDER, pending={"version": NEWER, "previous": VERSION, "started": stamp()})
    assert update.resume(context(kit), memory) == update.Outcome("updated", NEWER)
    assert seen == ["status"]
    assert (memory.pending, memory.previous, memory.attempts) == (None, VERSION, {})
    assert kit.run.calls[-1] == as_owner("/bin/rm", "-rf", str(clone))
    assert not clone.exists()
    assert os.readlink(layout.current) == str(layout.version_dir(NEWER))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "migrated"


@pytest.mark.parametrize(("connect", "result"), [(BEFORE, "rolled_back"), (AFTER, "updated")])
def test_a_resumed_update_needs_a_connection_newer_than_the_saved_one(
    kit: Kit, keys: Keys, connect: str, result: str
) -> None:
    """The connection saved before the update is not proof of the new version: only a newer one keeps it."""
    layout = kit.layout
    interrupted(kit, keys)
    kit.run.answer(RUNNER_PRINT, stdout=RUNNING)
    runner_commands(kit.run, {"status": status_doc(last_connect=connect)})
    pending = {"version": NEWER, "previous": VERSION, "started": stamp(), "before": BEFORE}
    memory = Maintenance(pending=pending)
    assert update.resume(context(kit), memory) == update.Outcome(result, NEWER)
    kept = result == "updated"
    assert os.readlink(layout.current) == str(layout.version_dir(NEWER if kept else VERSION))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == ("migrated" if kept else "before")
    assert memory.attempts == ({} if kept else {NEWER: 1})


@pytest.mark.parametrize("pending", [{"version": NEWER}, {"previous": VERSION}, {}])
def test_a_pending_update_without_both_versions_is_dropped(kit: Kit, keys: Keys, pending: dict[str, str]) -> None:
    clone = interrupted(kit, keys)
    memory = Maintenance(previous=OLDER, pending=pending)
    assert update.resume(context(kit), memory) == update.Outcome("pending_invalid")
    assert (memory.pending, memory.previous, memory.attempts) == (None, OLDER, {})
    assert kit.run.calls == []
    assert clone.exists()


@pytest.mark.parametrize(
    ("daemon", "reply"),
    [
        (RUNNING, status_doc(last_connect="never")),  # running, never connected
        (RUNNING, ""),  # running, no answer
        ("\tstate = waiting\n", status_doc(last_connect=AFTER)),  # not running
    ],
)
def test_an_interrupted_update_without_a_connection_takes_the_way_back(
    kit: Kit, keys: Keys, daemon: str, reply: str
) -> None:
    layout = kit.layout
    clone = interrupted(kit, keys)
    kit.run.answer(RUNNER_PRINT, stdout=daemon)
    runner_commands(kit.run, {"status": reply})
    memory = Maintenance(pending={"version": NEWER, "previous": VERSION}, attempts={NEWER: 1})
    assert update.resume(context(kit), memory) == update.Outcome("rolled_back", NEWER)
    assert (memory.pending, memory.attempts, memory.bad, memory.previous) == (None, {NEWER: 2}, [NEWER], None)
    assert os.readlink(layout.current) == str(layout.version_dir(VERSION))
    assert (store(kit) / "data/table").read_text(encoding="utf-8") == "before"
    assert not clone.exists()
    assert BOOTOUT_RUNNER in kit.run.calls
    assert pg_stop(kit, layout.version_dir(VERSION)) in kit.run.calls


def test_a_first_failed_resume_counts_one_attempt(kit: Kit, keys: Keys) -> None:
    interrupted(kit, keys)
    memory = Maintenance(pending={"version": NEWER, "previous": VERSION})
    assert update.resume(context(kit), memory) == update.Outcome("rolled_back", NEWER)
    assert (memory.attempts, memory.bad) == ({NEWER: 1}, [])


def test_the_attempt_of_a_resume_is_counted_before_its_way_back(kit: Kit, keys: Keys) -> None:
    interrupted(kit, keys)
    kit.run.answer(as_owner("/bin/mv"), rc=1)
    memory = Maintenance(pending={"version": NEWER, "previous": VERSION})
    with refused("update_rollback_failed", "store"):
        update.resume(context(kit), memory)
    assert (memory.pending, memory.attempts) == (None, {NEWER: 1})
