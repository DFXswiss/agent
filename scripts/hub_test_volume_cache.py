"""The cached volume data set of the web suite's volume gate (HOT-21, decisions/HOT-21.md).

Seeding the data set (`scripts/hub_test_volume.py`) takes three to five minutes; the result only changes when the
schema or the code that writes it changes. `scripts/hub_test_server.py --volume-cache DIR` therefore keeps it in
`DIR/<key>/` and reuses it:

- `key`: sha256 over the hub's migrations, the scripts of the suite's hub (`scripts/hub_test_*.py`) and every source
  file of the packages the seed's code imports (loaded with the device double) or calls over HTTP (platform log and
  device enrollment), so a change of anything that writes the data set makes a new entry;
- an entry holds the database rows (`data.dump`, `pg_dump --data-only` as the superuser, taken by the device double
  when the seed is complete and before any follow-up route changes it), what the device double knows beyond the
  database (`state.json`: the seed's answer, the founder's and the volume device's keys, the scope keys and the
  community key) and the hub's signing key (`signing.key`: the hub signs audit checkpoints with it); `pg_dump` and
  `pg_restore` of the server's major version (`tool`);
- an entry is used for `MAX_AGE` (the data set is placed relative to the time it was made: tasks 26 to 30 days back,
  charges 2 days, pull-request links 1 day, messages over 20 days, invitations open for 20 days; no hub window (the
  24 h of the situation view's done group, the billing days) has a seeded row near its edge); older ones and all
  but the `KEEP` used last are removed, never one used within `PROTECT`; a data set shrunk by `VOLUME_COUNTS` is
  never stored;
- a hub test server with a usable entry restores the rows into its freshly migrated database before the hub starts
  (`TRUNCATE` of every table, `pg_restore --data-only` as replica) and its device double takes over the state;
  without one, one lane takes the seeding lock (`claim`), its device double seeds at once and stores a new entry
  (written to a temporary directory and renamed); the other lanes wait for that entry and restore it, or take the lock
  over when its holder ended without one.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
MAX_AGE = 12 * 3600
"""Seconds an entry is used."""
KEEP = 6
"""Entries kept at most (each about 150 MB); the least recently used go first."""
STALE = 3600
"""Seconds after which a temporary directory counts as left behind by a run that ended."""
PROTECT = 1800
"""Seconds after its last use in which an entry is never removed (a lane may be restoring it right now)."""
CALLED = ("agent_hub/platform", "agent_hub/device")
"""Packages the seed reaches over the hub's HTTP API (platform log appends, device enrollment)."""
FILES = ("data.dump", "state.json", "signing.key")
LOCK_STALE = 2400
"""Seconds after which a seeding lock counts as left behind even if its process still lives."""
USED = "used"
"""Touched whenever a lane takes the entry (the age of an entry is that of its `state.json`)."""
BIN_VARIABLE = "WEB_E2E_PG_BIN"
"""Directory of `pg_dump`/`pg_restore` (else those of the server's major version where packages put them, else PATH):
a newer `pg_dump` writes settings an older server refuses on restore."""
BIN_DIRS = (
    "/opt/homebrew/opt/postgresql@{major}/bin",
    "/usr/local/opt/postgresql@{major}/bin",
    "/usr/lib/postgresql/{major}/bin",
)
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def _load_director() -> None:
    """Imports the device double (and with it the seed) so its imports are in `sys.modules`."""
    if "hub_test_director" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location("hub_test_director", ROOT / "scripts" / "hub_test_director.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hub_test_director"] = module
    spec.loader.exec_module(module)


def _package_dirs(root: Path, files: Iterable[str]) -> set[Path]:
    """For every source file below `root/src`: its package directory below the top-level package (`agent_hub/x`)."""
    src = root / "src"
    found: set[Path] = set()
    for name in files:
        path = Path(name).resolve()
        if not path.is_relative_to(src):
            continue
        parts = path.relative_to(src).parts
        found.add(src.joinpath(*parts[:2]) if len(parts) > 2 else path)
    return found


def key(root: Path = ROOT, modules: Iterable[str] | None = None, server_major: int = 0) -> str:
    """The cache key of the data set (module docstring); `modules`: source files (default: the loaded modules);
    `server_major`: the PostgreSQL major version (an archive is restored with the tools of the same version)."""
    if modules is None:
        _load_director()
        modules = [str(getattr(module, "__file__", "") or "") for module in list(sys.modules.values())]
    parts = _package_dirs(root, modules) | {root / "src" / name for name in CALLED}
    paths = set((root / "src" / "agent_hub" / "db" / "migrations").iterdir())
    paths |= set((root / "scripts").glob("hub_test_*.py"))
    for part in parts:
        paths |= set(part.rglob("*.py")) if part.is_dir() else {part}
    digest = hashlib.sha256(f"postgres {server_major}\0".encode())
    for path in sorted(path for path in paths if path.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:24]


def server_major(admin_url: str) -> int:
    """The PostgreSQL major version of the server at `admin_url`."""
    with psycopg.connect(admin_url) as connection:
        return int(connection.info.server_version // 10000)


def _client(admin_url: str) -> tuple[str, dict[str, str]]:
    """`admin_url` without its password and the environment of a client tool that passes it (`PGPASSWORD`): the
    command line of a process is visible to every user of the device."""
    params = conninfo_to_dict(admin_url)
    password = params.pop("password", None)
    environ = {**os.environ, **({"PGPASSWORD": str(password)} if password else {})}
    return make_conninfo(**{key: str(value) for key, value in params.items()}), environ


def tool(name: str, admin_url: str, environ: Mapping[str, str] = os.environ) -> str:
    """`pg_dump`/`pg_restore` for the server at `admin_url` (`BIN_VARIABLE`)."""
    if environ.get(BIN_VARIABLE):
        return str(Path(environ[BIN_VARIABLE]) / name)
    major = server_major(admin_url)
    for pattern in BIN_DIRS:
        path = Path(pattern.format(major=major)) / name
        if path.is_file():
            return str(path)
    return name


def _created(entry: Path) -> float:
    return (entry / "state.json").stat().st_mtime


def _last_use(entry: Path) -> float:
    used = entry / USED
    return used.stat().st_mtime if used.is_file() else _created(entry)


def usable(cache: Path, name: str, clock: float | None = None) -> Path | None:
    """The complete entry `name` if it is younger than `MAX_AGE`; marks it as used."""
    entry = cache / name
    if not all((entry / file).is_file() for file in FILES):
        return None
    if (time.time() if clock is None else clock) - _created(entry) >= MAX_AGE:
        return None
    (entry / USED).touch()
    return entry


def prune(cache: Path, clock: float | None = None) -> None:
    """Removes entries older than `MAX_AGE` and all but the `KEEP` used last, never one used within `PROTECT`;
    temporary directories once they are older than `STALE` (a lane may still be writing a younger one); incomplete
    entries."""
    if not cache.is_dir():
        return
    moment = time.time() if clock is None else clock
    directories = [path for path in cache.iterdir() if path.is_dir()]
    for path in directories:
        if path.name.startswith("."):
            if moment - path.stat().st_mtime >= STALE:
                shutil.rmtree(path, ignore_errors=True)
        elif not all((path / file).is_file() for file in FILES):
            shutil.rmtree(path, ignore_errors=True)
    entries = sorted(
        (path for path in directories if path.exists() and not path.name.startswith(".")), key=_last_use, reverse=True
    )
    kept = [path for path in entries if moment - _created(path) < MAX_AGE][:KEEP]
    for path in entries:
        if path not in kept and moment - _last_use(path) >= PROTECT:
            shutil.rmtree(path, ignore_errors=True)


def lock_path(cache: Path, name: str) -> Path:
    """The lock of the lane that seeds the entry `name` (the others wait for the entry)."""
    return cache / f".seeding-{name}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's process
        return True
    return True


def claim(cache: Path, name: str, pid: int | None = None, clock: float | None = None) -> Path | None:
    """Takes the seeding lock of `name` for the process `pid` (default: this one; a lock it holds already is its own);
    None while another live process holds it. A lock of a process that is gone, or older than `LOCK_STALE`, is taken
    over."""
    cache.mkdir(parents=True, exist_ok=True)
    path = lock_path(cache, name)
    owner = os.getpid() if pid is None else pid
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(path.read_text().strip() or "0")
                age = (time.time() if clock is None else clock) - path.stat().st_mtime
            except (OSError, ValueError):
                continue  # released meanwhile, or not yet written: try again
            if holder == owner:
                return path
            if _alive(holder) and age < LOCK_STALE:
                return None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w") as file:
            file.write(str(owner))
        return path
    return None


def release(lock: Path, pid: int | None = None) -> None:
    """Removes the seeding lock if `pid` (default: this process) holds it."""
    with contextlib.suppress(OSError, ValueError):
        if int(lock.read_text().strip()) == (os.getpid() if pid is None else pid):
            lock.unlink()


def store(cache: Path, name: str, admin_url: str, state: dict[str, Any], signing_key: str) -> Path:
    """Writes the entry `name` (rows of the database at `admin_url`, state, signing key); a usable one with the same
    name wins (another lane stored it first), an expired one is replaced."""
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / f".{name}.{os.getpid()}"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir()
    url, environ = _client(admin_url)
    try:
        dump = ["--data-only", "--format=custom", "--compress=1", f"--file={temporary / 'data.dump'}", url]
        subprocess.run([tool("pg_dump", admin_url), *dump], check=True, env=environ)
        (temporary / "signing.key").write_text(signing_key)
        (temporary / "state.json").write_text(json.dumps(state))
        target = cache / name
        if target.exists() and usable(cache, name) is None:
            shutil.rmtree(target, ignore_errors=True)
        with contextlib.suppress(OSError):  # another lane stored it first
            temporary.rename(target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    prune(cache)
    return cache / name


ENABLE = {"O": "ENABLE", "A": "ENABLE ALWAYS", "R": "ENABLE REPLICA", "D": "DISABLE"}
"""`ALTER TABLE … <mode> TRIGGER` for each `pg_trigger.tgenabled`."""


def restore(admin_url: str, entry: Path) -> None:
    """Replaces every row of the freshly migrated database at `admin_url` by the entry's rows.

    The user triggers (the append-only tables refuse `TRUNCATE` even as replica: `ENABLE ALWAYS`) are switched off by
    name and afterwards back to their exact mode; the rows are restored as replica, so the foreign keys' internal
    triggers do not fire on the way (the rows come from a consistent database of the same schema).
    """
    with psycopg.connect(admin_url, autocommit=True) as connection:
        tables = connection.execute(
            "SELECT schemaname, tablename FROM pg_catalog.pg_tables WHERE schemaname <> ALL(%s) ORDER BY 1, 2",
            (list(SYSTEM_SCHEMAS),),
        ).fetchall()
        triggers = connection.execute(
            "SELECT n.nspname, c.relname, t.tgname, t.tgenabled FROM pg_catalog.pg_trigger t"
            " JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
            " WHERE NOT t.tgisinternal AND n.nspname <> ALL(%s) ORDER BY 1, 2, 3",
            (list(SYSTEM_SCHEMAS),),
        ).fetchall()

        def switch(modes: dict[str, str]) -> None:
            with connection.transaction():
                for schema, table, name, enabled in triggers:
                    connection.execute(
                        sql.SQL("ALTER TABLE {} {} TRIGGER {}").format(
                            sql.Identifier(schema, table), sql.SQL(modes[enabled]), sql.Identifier(name)
                        )
                    )

        url, environ = _client(admin_url)
        command = [tool("pg_restore", admin_url), "--data-only", "--single-transaction", "--exit-on-error"]
        switch(dict.fromkeys(ENABLE, "DISABLE"))
        try:
            if tables:
                names = sql.SQL(", ").join(sql.Identifier(schema, table) for schema, table in tables)
                connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(names))
            subprocess.run(
                [*command, "--dbname", url, str(entry / "data.dump")],
                check=True,
                env={**environ, "PGOPTIONS": "-c session_replication_role=replica"},
            )
        finally:
            switch(ENABLE)
