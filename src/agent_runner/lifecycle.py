"""Daemon start and runner removal (docs/platform/spec/05-runner.md sections 1.1, 1.2, 1.4).

`run_daemon`: report each retired setting that is still set (`runner.config_ignored name=POOL_SIZE pool_size=1`, once
per start), prepare the home (0700; `log/`; `tmp/` wiped), start the own PostgreSQL cluster, apply the store
migrations, open the pool, run the supervisor with the discovered tasks and shutdown hooks, then close the pool and
stop the cluster.

`uninstall_runner`: unload the service, call the registered cleaners (`<area>/uninstall.py:CLEANERS`, for example
the VM deletion) while the store still exists, stop the cluster and delete the store and runner data; the
identity files are deleted too unless `keep_identity` is set. A failing cleaner stops the removal before anything
is deleted, so it can be repeated.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from agent_runner import registry, service
from agent_runner.config import POOL_SIZE, Config
from agent_runner.errors import RunnerError
from agent_runner.host import Host
from agent_runner.identity import Identity, ensure_private_dir, remove_identity
from agent_runner.store.cluster import start_store, stop_store, store_exists
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.supervisor import Supervisor, SupervisorPolicy

# Runner data below the home that `uninstall` deletes (the identity files are handled separately).
DATA_ENTRIES = ("pg", "golden", "log", "tmp", "vm", service.PLIST_NAME)
# 05-runner.md section 1.1: exit within 10 s of SIGTERM. The supervisor gets 8 s; closing the pool (at most 1 s) and
# the fast cluster stop use the rest.
SUPERVISOR_STOP_S = 8.0
POOL_CLOSE_S = 1.0


@dataclass
class DaemonContext:
    """Passed to every daemon task and shutdown hook."""

    config: Config
    identity: Identity | None
    host: Host
    dsn: str
    pool: Any
    log: TextIO
    stopping: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class UninstallContext:
    """Passed to every cleaner; `dsn` is None when no store exists."""

    config: Config
    host: Host
    dsn: str | None


def prepare_home(config: Config) -> None:
    """Create the home, `log/` and an empty `tmp/` (per-operation scratch, wiped at start), all 0700."""
    ensure_private_dir(config.home)
    ensure_private_dir(config.log_dir)
    _remove(config.tmp_dir)
    ensure_private_dir(config.tmp_dir)


def _remove(path: Path) -> None:
    """Delete a file, a symbolic link (never its target) or a directory tree; missing paths are fine."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def run_daemon(
    config: Config,
    identity: Identity | None,
    host: Host,
    *,
    announce: Callable[[str, dict[str, str | int]], None],
) -> int:
    """Run the daemon in the foreground; returns the exit code (0 after SIGTERM, 1 after a restart limit).

    The daemon runs as the unprivileged runner account (05 section 1.1); root is refused (`run_as_root`).
    """
    if host.geteuid() == 0:
        raise RunnerError("run_as_root")
    for name in config.ignored:
        # A retired setting from an old runner.env or plist: not read, the fixed value applies (ADR-009 addendum).
        announce("config_ignored", {"name": name, "pool_size": POOL_SIZE})
    prepare_home(config)
    dsn = start_store(config.pg_dir, host.cluster)
    try:
        result = migrate(dsn)
        announce("migrate", {"result": result.line})
        if result.status == "failed":
            raise RunnerError("migration_failed", result.step)
        specs = registry.tasks(host.package)
        hooks = registry.shutdown_hooks(host.package)
        announce("run", {"tasks": len(specs), "enrolled": "yes" if identity is not None else "no"})
        return asyncio.run(_serve(config, identity=identity, host=host, dsn=dsn, specs=specs, hooks=hooks))
    finally:
        stop_store(config.pg_dir, host.cluster)


async def _serve(
    config: Config,
    *,
    identity: Identity | None,
    host: Host,
    dsn: str,
    specs: list[registry.TaskSpec],
    hooks: list[registry.ShutdownHook],
) -> int:
    pool = await open_pool(dsn)
    try:
        context = DaemonContext(config=config, identity=identity, host=host, dsn=dsn, pool=pool, log=host.stderr)
        supervisor = Supervisor(
            specs,
            hooks=hooks,
            clock=host.clock,
            sleep=host.sleep,
            policy=SupervisorPolicy(stop_timeout=SUPERVISOR_STOP_S),
            log=host.stderr,
            install_signals=host.install_signals,
        )
        return await supervisor.run(context)
    finally:
        await pool.close(timeout=POOL_CLOSE_S)


def uninstall_runner(config: Config, host: Host, *, keep_identity: bool) -> dict[str, str | int]:
    """Remove the runner from this device; returns the facts to report.

    The LaunchDaemon needs root, the store and the VMs belong to the runner account (PostgreSQL and Lima refuse or
    misbehave as root). As root: unload and remove the service, then run `uninstall` again as the account named in
    the plist (`sudo -u`), with the plist's environment. As the runner account: refuse while a system service is
    still installed (`service_installed`: run with sudo), otherwise remove everything else.
    """
    if host.geteuid() == 0:
        return _uninstall_as_root(config, host, keep_identity=keep_identity)
    if host.under_pytest:
        removed_plist = service.uninstall(config, host)
    elif service.plist_path(config, host).exists():
        raise RunnerError("service_installed")
    else:
        removed_plist = False
    cleaners = registry.cleaners(host.package)
    dsn = start_store(config.pg_dir, host.cluster) if store_exists(config.pg_dir, host.cluster) else None
    for cleaner in cleaners:
        try:
            cleaner.run(UninstallContext(config=config, host=host, dsn=dsn))
        except Exception:
            raise RunnerError("uninstall_failed", cleaner.name) from None
    stop_store(config.pg_dir, host.cluster)
    for entry in DATA_ENTRIES:
        _remove(config.home / entry)
    if not keep_identity:
        remove_identity(config.home)
        if config.home.is_dir() and not any(config.home.iterdir()):
            config.home.rmdir()
    return {
        "service": "removed" if removed_plist else "absent",
        "cleaners": len(cleaners),
        "store": "deleted",
        "identity": "kept" if keep_identity else "deleted",
    }


def _uninstall_as_root(config: Config, host: Host, *, keep_identity: bool) -> dict[str, str | int]:
    data = service.read_plist(service.plist_path(config, host))
    user = data.get("UserName") if data is not None else None
    env = data.get("EnvironmentVariables") if data is not None else None
    if (
        not isinstance(user, str)
        or not isinstance(env, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    ):
        # Without an installed service the runner account is unknown: run `uninstall` as that account instead.
        raise RunnerError("uninstall_as_root")
    account = service.runner_user(host, user)
    service.uninstall(config, host)
    argv = ["-u", account, "-H", "--", "/usr/bin/env", "-i", *(f"{k}={v}" for k, v in sorted(env.items()))]
    argv += [*host.cli(), "uninstall", *(["--keep-identity"] if keep_identity else [])]
    done = host.sudo(argv)
    for line in (done.stdout + done.stderr).splitlines():
        host.stderr.write(f"runner.uninstall_step {line}\n")
    if done.returncode != 0:
        raise RunnerError("uninstall_failed", "runner_account")
    return {"service": "removed", "user": account, "identity": "kept" if keep_identity else "deleted"}
