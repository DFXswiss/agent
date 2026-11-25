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
  community key) and the hub's signing key (`signing.key`: the hub signs audit checkpoints with it);
- an entry is used for `MAX_AGE` (the data set is placed relative to the time it was made: messages of the last 20
  days, open invitations); older ones and all but the newest `KEEP` are removed;
- a hub test server with a usable entry restores the rows into its freshly migrated database before the hub starts
  (`TRUNCATE` of every table, `pg_restore --data-only --disable-triggers`) and its device double takes over the state;
  without one, the device double seeds and stores a new entry (written to a temporary directory and renamed, so two
  lanes seeding at once never leave a half entry; the first rename wins).
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
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
MAX_AGE = 12 * 3600
"""Seconds an entry is used."""
KEEP = 3
"""Entries kept at most (each is a few hundred MB)."""
STALE = 3600
"""Seconds after which a temporary directory counts as left behind by a run that ended."""
CALLED = ("agent_hub/platform", "agent_hub/device")
"""Packages the seed reaches over the hub's HTTP API (platform log appends, device enrollment)."""
FILES = ("data.dump", "state.json", "signing.key")
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


def key(root: Path = ROOT, modules: Iterable[str] | None = None) -> str:
    """The cache key of the data set (module docstring); `modules`: source files (default: the loaded modules)."""
    if modules is None:
        _load_director()
        modules = [str(getattr(module, "__file__", "") or "") for module in list(sys.modules.values())]
    parts = _package_dirs(root, modules) | {root / "src" / name for name in CALLED}
    paths = set((root / "src" / "agent_hub" / "db" / "migrations").iterdir())
    paths |= set((root / "scripts").glob("hub_test_*.py"))
    for part in parts:
        paths |= set(part.rglob("*.py")) if part.is_dir() else {part}
    digest = hashlib.sha256()
    for path in sorted(path for path in paths if path.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:24]


def usable(cache: Path, name: str, clock: float | None = None) -> Path | None:
    """The complete entry `name` if it is younger than `MAX_AGE`."""
    entry = cache / name
    if not all((entry / file).is_file() for file in FILES):
        return None
    age = (time.time() if clock is None else clock) - entry.stat().st_mtime
    return entry if age < MAX_AGE else None


def prune(cache: Path, clock: float | None = None) -> None:
    """Removes entries older than `MAX_AGE` and all but the newest `KEEP`; temporary directories once they are older
    than `STALE` (a lane may still be writing a younger one)."""
    if not cache.is_dir():
        return
    moment = time.time() if clock is None else clock
    entries = sorted((path for path in cache.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime)
    for path in [path for path in entries if path.name.startswith(".")]:
        if moment - path.stat().st_mtime >= STALE:
            shutil.rmtree(path, ignore_errors=True)
    done = [path for path in entries if not path.name.startswith(".")]
    fresh = [path for path in done if moment - path.stat().st_mtime < MAX_AGE][-KEEP:]
    for path in done:
        if path not in fresh:
            shutil.rmtree(path, ignore_errors=True)


def store(cache: Path, name: str, admin_url: str, state: dict[str, Any], signing_key: str) -> Path:
    """Writes the entry `name` (rows of the database at `admin_url`, state, signing key); the existing one wins."""
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / f".{name}.{os.getpid()}"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir()
    try:
        subprocess.run(
            [
                "pg_dump",
                "--data-only",
                "--format=custom",
                "--compress=1",
                f"--file={temporary / 'data.dump'}",
                admin_url,
            ],
            check=True,
        )
        (temporary / "state.json").write_text(json.dumps(state))
        (temporary / "signing.key").write_text(signing_key)
        with contextlib.suppress(OSError):  # another lane stored it first
            temporary.rename(cache / name)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    prune(cache)
    return cache / name


def restore(admin_url: str, entry: Path) -> None:
    """Replaces every row of the freshly migrated database at `admin_url` by the entry's rows."""
    with psycopg.connect(admin_url, autocommit=True) as connection:
        tables = connection.execute(
            "SELECT schemaname, tablename FROM pg_catalog.pg_tables WHERE schemaname <> ALL(%s) ORDER BY 1, 2",
            (list(SYSTEM_SCHEMAS),),
        ).fetchall()
        if tables:
            names = sql.SQL(", ").join(sql.Identifier(schema, table) for schema, table in tables)
            connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(names))
    subprocess.run(
        [
            "pg_restore",
            "--data-only",
            "--disable-triggers",
            "--single-transaction",
            "--exit-on-error",
            f"--dbname={admin_url}",
            str(entry / "data.dump"),
        ],
        check=True,
    )
