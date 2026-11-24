"""Updates of an own device with a way back (ADR-074 section 6).

Run by the maintenance daemon (root). The feed `latest.json` of the hub's download area is trusted only when a
release key of the **installed** version signed it, it has not expired, and its version is newer than the active one
and not marked bad. It is installed when no session is working and either the local hour is inside the window or
the version has waited `GRACE` since it was first seen. Steps:

1. download the package into the root-only `downloads/` and check its SHA-256;
2. stop the runner, keep its store as an APFS clone (`cp -c`, as the owner) and keep both plists;
3. `installer -pkg` - the new version's `postinstall` switches `current` and starts the runner;
4. wait up to `HEALTH_TIMEOUT` for a running daemon that reports a new hub connection: success drops the clone;
   failure stops the runner, restores the store and the plists, switches `current` back and starts the runner again.
   Nothing was pushed to the hub without a connection, so the restored store is consistent with the hub. A version
   that failed `MAX_ATTEMPTS` times is bad and never tried again.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_runner.errors import RunnerError
from agent_runner.install import daemons, release, runnerctl
from agent_runner.install.layout import RUNNER_LABEL
from agent_runner.install.release import Feed
from agent_runner.install.state import Maintenance
from agent_runner.install.system import System

FEED_PATH = "/downloads/runner/"
MAX_FEED = 64 * 1024
MAX_PACKAGE = 2 * 1024**3
CHECK_EVERY = timedelta(hours=1)
GRACE = timedelta(days=7)
WINDOW = (2, 5)
HEALTH_TIMEOUT_S = 600.0
HEALTH_POLL_S = 30.0
MAX_ATTEMPTS = 2
INSTALLER = "/usr/sbin/installer"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Outcome:
    result: str
    version: str = ""


def feed_url(hub: str, name: str) -> str:
    return hub.rstrip("/") + FEED_PATH + name


def read_feed(system: System, hub: str, keys: list[str]) -> Feed:
    data = system.fetch(feed_url(hub, release.FEED), MAX_FEED)
    signature = system.fetch(feed_url(hub, release.FEED + release.SIGNATURE_SUFFIX), 1024)
    try:
        text = signature.decode("ascii")
    except UnicodeDecodeError:
        raise RunnerError("release_signature_invalid") from None
    return release.feed_from(release.verify(data, text, keys))


def _stamp(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def due(system: System, memory: Maintenance, feed: Feed) -> bool:
    """Inside the window, or the version waited `GRACE` since it was first seen."""
    first = memory.first_seen.setdefault(feed.version, _stamp(system.now()))
    if WINDOW[0] <= system.local_hour() < WINDOW[1]:
        return True
    return system.now() - _parse(first) >= GRACE


def store_dir(ctx: runnerctl.Context) -> Path:
    return ctx.home / "pg"


def _backup(system: System) -> Path:
    return system.layout.downloads / "plists"


def snapshot(ctx: runnerctl.Context) -> None:
    """Stop the runner and its store; clone the store (APFS, as the owner) and copy both plists."""
    system = ctx.system
    layout = system.layout
    daemons.bootout(system, RUNNER_LABEL)
    store = store_dir(ctx)
    pg_ctl = layout.current / "postgresql/bin/pg_ctl"
    system.run(ctx.as_owner([str(pg_ctl), "-D", str(store / "data"), "-m", "fast", "-w", "stop"]))
    clone = store.with_name("pg.before-update")
    system.must(ctx.as_owner(["/bin/rm", "-rf", str(clone)]), "update_snapshot_failed", "stale_clone")
    if store.is_dir():
        system.must(ctx.as_owner(["/bin/cp", "-cpR", str(store), str(clone)]), "update_snapshot_failed")
    backup = _backup(system)
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    for plist in (layout.runner_plist, layout.maintenance_plist):
        if plist.is_file():
            (backup / plist.name).write_bytes(plist.read_bytes())


def drop_snapshot(ctx: runnerctl.Context) -> None:
    clone = store_dir(ctx).with_name("pg.before-update")
    ctx.system.run(ctx.as_owner(["/bin/rm", "-rf", str(clone)]))


def switch_current(system: System, version: str) -> None:
    layout = system.layout
    target = layout.version_dir(version)
    if not target.is_dir():
        raise RunnerError("version_missing", version)
    fresh = layout.current.with_name(".current.link")
    fresh.unlink(missing_ok=True)
    fresh.symlink_to(target)
    os.replace(fresh, layout.current)


def rollback(ctx: runnerctl.Context, previous: str) -> None:
    system = ctx.system
    layout = system.layout
    daemons.bootout(system, RUNNER_LABEL)
    store = store_dir(ctx)
    clone = store.with_name("pg.before-update")
    pg_ctl = layout.version_dir(previous) / "postgresql/bin/pg_ctl"
    system.run(ctx.as_owner([str(pg_ctl), "-D", str(store / "data"), "-m", "fast", "-w", "stop"]))
    if clone.is_dir():
        system.must(ctx.as_owner(["/bin/rm", "-rf", str(store)]), "update_rollback_failed", "store")
        system.must(ctx.as_owner(["/bin/mv", str(clone), str(store)]), "update_rollback_failed", "store")
    switch_current(system, previous)
    backup = _backup(system)
    for plist in (layout.runner_plist, layout.maintenance_plist):
        kept = backup / plist.name
        if kept.is_file():
            daemons.write_plist(plist, _plist_data(kept))
    if layout.runner_plist.is_file():
        daemons.bootstrap(system, layout.runner_plist, RUNNER_LABEL)


def _plist_data(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        data = plistlib.load(handle)
    if not isinstance(data, dict):
        raise RunnerError("update_rollback_failed", "plist")
    return data


def healthy(ctx: runnerctl.Context, before: str) -> bool:
    """The runner runs and reports a hub connection newer than `before`."""
    if daemons.state(ctx.system, RUNNER_LABEL) != "running":
        return False
    try:
        found = runnerctl.status(ctx)
    except RunnerError:
        return False
    return found.last_connect not in ("never", before)


Sleep = Callable[[float], None]


def wait_healthy(ctx: runnerctl.Context, before: str, sleep: Sleep) -> bool:
    waited = 0.0
    while True:
        if healthy(ctx, before):
            return True
        if waited >= HEALTH_TIMEOUT_S:
            return False
        sleep(HEALTH_POLL_S)
        waited += HEALTH_POLL_S


def resume(ctx: runnerctl.Context, memory: Maintenance) -> Outcome:
    """Finish an update whose maintenance run was ended while it waited: keep it when the runner is connected, else
    take the way back."""
    pending = memory.pending or {}
    version, previous = str(pending.get("version", "")), str(pending.get("previous", ""))
    memory.pending = None
    if not previous or not version:
        return Outcome("pending_invalid")
    if healthy(ctx, str(pending.get("before", "never"))):
        drop_snapshot(ctx)
        memory.previous = previous or memory.previous
        return Outcome("updated", version)
    memory.attempts[version] = memory.attempts.get(version, 0) + 1
    if memory.attempts[version] >= MAX_ATTEMPTS:
        memory.bad.append(version)
    rollback(ctx, previous)
    return Outcome("rolled_back", version)


def download(system: System, hub: str, feed: Feed) -> Path:
    data = system.fetch(feed_url(hub, feed.package), MAX_PACKAGE)
    if hashlib.sha256(data).hexdigest() != feed.package_sha256:
        raise RunnerError("package_digest_mismatch")
    folder = system.layout.downloads
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / feed.package
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _feed(system: System, memory: Maintenance, hub: str, active: str, keys: list[str]) -> Feed | Outcome:
    """The feed when it names a version to install, else the outcome that ends the round."""
    now = system.now()
    if memory.last_check is not None and now - _parse(memory.last_check) < CHECK_EVERY:
        return Outcome("not_due")
    memory.last_check = _stamp(now)
    try:
        feed = read_feed(system, hub, keys)
    except RunnerError as error:
        memory.feed = error.code
        return Outcome(error.code)
    if feed.expired(now):
        memory.feed = "feed_expired"
        return Outcome("feed_expired", feed.version)
    memory.feed = "ok"
    if release.version_key(feed.version) <= release.version_key(active) or feed.version in memory.bad:
        return Outcome("current", active)
    return feed


def check(
    ctx: runnerctl.Context,
    memory: Maintenance,
    *,
    hub: str,
    active: str,
    keys: list[str],
    busy: int,
    sleep: Sleep,
) -> Outcome:
    """One update round; never raises for a feed or package problem (the outcome names it)."""
    system = ctx.system
    feed = _feed(system, memory, hub, active, keys)
    if isinstance(feed, Outcome):
        return feed
    if busy or not due(system, memory, feed):
        return Outcome("waiting", feed.version)
    try:
        package = download(system, hub, feed)
    except RunnerError as error:
        return Outcome(error.code, feed.version)
    snapped = False
    ok = False
    try:
        before = runnerctl.status(ctx).last_connect
        snapshot(ctx)
        snapped = True
        started = _stamp(system.now())
        memory.pending = {"version": feed.version, "previous": active, "started": started, "before": before}
        # Saved before `installer`: a maintenance run that ends while it waits leaves the update to `resume`.
        memory.save(system.layout.maintenance_file)
        done = system.run([INSTALLER, "-pkg", str(package), "-target", str(system.layout.root)])
        ok = done.returncode == 0 and wait_healthy(ctx, before, sleep)
    except RunnerError:
        ok = False
    finally:
        package.unlink(missing_ok=True)
    memory.pending = None
    if ok:
        drop_snapshot(ctx)
        memory.previous = active
        memory.attempts.pop(feed.version, None)
        return Outcome("updated", feed.version)
    if not snapped:
        # Nothing was installed and no store was kept: only the runner is started again, no attempt is counted.
        if system.layout.runner_plist.is_file():
            daemons.bootstrap(system, system.layout.runner_plist, RUNNER_LABEL)
        return Outcome("update_not_started", feed.version)
    # Counted before the way back runs, so a failing way back still ends the retries (the caller saves the memory).
    memory.attempts[feed.version] = memory.attempts.get(feed.version, 0) + 1
    if memory.attempts[feed.version] >= MAX_ATTEMPTS:
        memory.bad.append(feed.version)
    rollback(ctx, active)
    return Outcome("rolled_back", feed.version)
