"""The cache of the web volume gate's data set (`scripts/hub_test_volume_cache.py`, HOT-21): key, freshness, pruning
and the round trip of the rows through `pg_dump`/`pg_restore` on a migrated hub database."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from psycopg.conninfo import make_conninfo

from tests.platform.hub.conftest import HubDatabase

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def cache_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hub_test_volume_cache", REPO_ROOT / "scripts" / "hub_test_volume_cache.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(root: Path) -> Path:
    """A minimal repository: one migration, one suite script, one package with a module and a called package."""
    for path, text in {
        "src/agent_hub/db/migrations/0001_a.sql": "CREATE TABLE a ();",
        "scripts/hub_test_server.py": "# server",
        "scripts/other.py": "# not part of the key",
        "src/agent_hub/orgs/append.py": "# append",
        "src/agent_hub/orgs/sub/deep.py": "# deep",
        "src/agent_hub/platform/log.py": "# platform",
        "src/agent_hub/device/enroll.py": "# device",
        "src/agent_hub/unused/x.py": "# not imported",
        "src/agent_hub/top.py": "# a top-level module",
    }.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def test_the_key_follows_migrations_suite_scripts_and_the_packages_of_the_seed(
    cache_module: ModuleType, tmp_path: Path
) -> None:
    root = tree(tmp_path)
    modules = [str(root / "src/agent_hub/orgs/append.py"), str(root / "src/agent_hub/top.py"), "", "/elsewhere/x.py"]
    first = cache_module.key(root, modules)
    assert len(first) == 24
    assert cache_module.key(root, modules) == first
    (root / "scripts/other.py").write_text("# changed")
    (root / "src/agent_hub/unused/x.py").write_text("# changed")
    assert cache_module.key(root, modules) == first
    for changed in (
        "src/agent_hub/db/migrations/0001_a.sql",
        "scripts/hub_test_server.py",
        "src/agent_hub/orgs/sub/deep.py",
        "src/agent_hub/platform/log.py",
        "src/agent_hub/device/enroll.py",
        "src/agent_hub/top.py",
    ):
        before = cache_module.key(root, modules)
        (root / changed).write_text(f"# changed {changed}")
        assert cache_module.key(root, modules) != before, changed


def test_the_key_of_the_repository_covers_the_seed_and_the_device_double(cache_module: ModuleType) -> None:
    assert cache_module.key() == cache_module.key()
    assert "hub_test_volume" in sys.modules


def entry(cache: Path, name: str, age: float = 0, files: tuple[str, ...] | None = None) -> Path:
    path = cache / name
    path.mkdir(parents=True)
    for file in files if files is not None else ("data.dump", "state.json", "signing.key"):
        (path / file).write_text(file)
    moment = time.time() - age
    os.utime(path, (moment, moment))
    return path


def test_only_complete_and_fresh_entries_are_used(cache_module: ModuleType, tmp_path: Path) -> None:
    fresh = entry(tmp_path, "fresh")
    entry(tmp_path, "old", age=cache_module.MAX_AGE + 1)
    entry(tmp_path, "half", files=("data.dump", "state.json"))
    assert cache_module.usable(tmp_path, "fresh") == fresh
    assert cache_module.usable(tmp_path, "old") is None
    assert cache_module.usable(tmp_path, "half") is None
    assert cache_module.usable(tmp_path, "missing") is None
    assert cache_module.usable(tmp_path, "fresh", clock=time.time() + cache_module.MAX_AGE) is None


def test_pruning_keeps_the_newest_fresh_entries_and_young_temporaries(cache_module: ModuleType, tmp_path: Path) -> None:
    cache_module.prune(tmp_path / "missing")
    for index in range(cache_module.KEEP + 1):
        entry(tmp_path, f"e{index}", age=100 - index)
    entry(tmp_path, "old", age=cache_module.MAX_AGE + 1)
    entry(tmp_path, ".young.1", age=10)
    entry(tmp_path, ".stale.2", age=cache_module.STALE + 1)
    (tmp_path / "file").write_text("not an entry")
    cache_module.prune(tmp_path)
    kept = sorted(path.name for path in tmp_path.iterdir())
    assert kept == [".young.1", "e1", "e2", "e3", "file"]


def test_the_rows_go_through_the_cache_into_a_migrated_database(
    cache_module: ModuleType, migrated_db: HubDatabase, tmp_path: Path
) -> None:
    admin_url = make_conninfo(**{**migrated_db.admin_params, "dbname": migrated_db.name})
    user = uuid.uuid4()
    at = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    with migrated_db.admin() as admin:
        admin.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (user, at))
        before = admin.execute("SELECT count(*) FROM hub.principal").fetchone()
    stored = cache_module.store(tmp_path, "k1", admin_url, {"state": "done"}, "ab" * 32)
    assert stored == tmp_path / "k1"
    assert cache_module.usable(tmp_path, "k1") == stored
    assert (stored / "signing.key").read_text() == "ab" * 32
    # a second lane storing the same entry leaves the first one in place
    cache_module.store(tmp_path, "k1", admin_url, {"state": "other"}, "cd" * 32)
    assert (stored / "signing.key").read_text() == "ab" * 32
    assert [path.name for path in tmp_path.iterdir()] == ["k1"]
    later = uuid.uuid4()
    with migrated_db.admin() as admin:
        admin.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (later, at))
    cache_module.restore(admin_url, stored)
    with migrated_db.admin() as admin:
        assert admin.execute("SELECT count(*) FROM hub.principal").fetchone() == before
        assert admin.execute("SELECT kind FROM hub.principal WHERE id = %s", (user,)).fetchone() == ("user",)
        assert admin.execute("SELECT 1 FROM hub.principal WHERE id = %s", (later,)).fetchone() is None
