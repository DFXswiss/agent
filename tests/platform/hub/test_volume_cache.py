"""The cache of the web volume gate's data set (HOT-21): key, freshness, pruning and the round trip of the rows through
`pg_dump`/`pg_restore` on a migrated hub database (`scripts/hub_test_volume_cache.py`), the hub test server's choice
between restore, seed and fallback (`scripts/hub_test_server.py`), the device double's state export and its service of
community key requests as the volume founder (`scripts/hub_test_volume.py`)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from agent_hub.community import crypto as community_format
from agent_hub.community import distribution, store
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.stream.publish import MemoryPublisher, set_publisher
from tests.platform.hub.conftest import HubDatabase

REPO_ROOT = Path(__file__).resolve().parents[3]


def script(name: str) -> ModuleType:
    """A module of `scripts/` (no package), registered under its name (its dataclasses look themselves up there)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cache_module() -> ModuleType:
    return script("hub_test_volume_cache")


@pytest.fixture(scope="module")
def volume_module() -> ModuleType:
    return script("hub_test_volume")


def admin_url_of(database: HubDatabase) -> str:
    return make_conninfo(**{**database.admin_params, "dbname": database.name})


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
    assert "hub_test_director" in sys.modules
    assert cache_module.key(server_major=16) != cache_module.key(server_major=17)


def test_the_postgres_tools_match_the_server(
    cache_module: ModuleType, migrated_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_url = admin_url_of(migrated_db)
    major = cache_module.server_major(admin_url)
    assert major >= 16
    assert cache_module.tool("pg_dump", admin_url, {"WEB_E2E_PG_BIN": "/opt/pg"}) == "/opt/pg/pg_dump"
    monkeypatch.setattr(cache_module, "BIN_DIRS", (str(tmp_path / "missing-{major}"), str(tmp_path / "pg{major}")))
    assert cache_module.tool("pg_restore", admin_url, {}) == "pg_restore"
    (tmp_path / f"pg{major}").mkdir()
    (tmp_path / f"pg{major}" / "pg_restore").write_text("")
    assert cache_module.tool("pg_restore", admin_url, {}) == str(tmp_path / f"pg{major}" / "pg_restore")


def test_a_password_reaches_the_tools_only_through_the_environment(cache_module: ModuleType) -> None:
    url, environ = cache_module._client("host=127.0.0.1 port=5432 user=postgres password=secret dbname=t")
    assert "secret" not in url
    assert environ["PGPASSWORD"] == "secret"
    url, environ = cache_module._client("host=127.0.0.1 user=postgres dbname=t")
    assert conninfo_to_dict(url) == {"host": "127.0.0.1", "user": "postgres", "dbname": "t"}
    assert environ.get("PGPASSWORD") == os.environ.get("PGPASSWORD")


def entry(
    cache: Path, name: str, age: float = 0, files: tuple[str, ...] | None = None, used: float | None = None
) -> Path:
    """An entry `age` seconds old (its `state.json`), last used `used` seconds ago (never: None)."""
    path = cache / name
    path.mkdir(parents=True)
    for file in files if files is not None else ("data.dump", "state.json", "signing.key"):
        (path / file).write_text(file)
    if (path / "state.json").exists():
        moment = time.time() - age
        os.utime(path / "state.json", (moment, moment))
    if used is not None:
        (path / "used").write_text("")
        moment = time.time() - used
        os.utime(path / "used", (moment, moment))
    moment = time.time() - age
    os.utime(path, (moment, moment))
    return path


def test_only_complete_and_fresh_entries_are_used(cache_module: ModuleType, tmp_path: Path) -> None:
    fresh = entry(tmp_path, "fresh")
    entry(tmp_path, "old", age=cache_module.MAX_AGE + 1)
    entry(tmp_path, "half", files=("data.dump", "state.json"))
    assert cache_module.usable(tmp_path, "fresh", clock=time.time() + cache_module.MAX_AGE) is None
    assert not (fresh / "used").exists()
    assert cache_module.usable(tmp_path, "fresh") == fresh
    assert (fresh / "used").exists()
    assert cache_module.usable(tmp_path, "old") is None
    assert cache_module.usable(tmp_path, "half") is None
    assert cache_module.usable(tmp_path, "missing") is None


def test_pruning_keeps_the_entries_used_last_and_never_one_in_use(cache_module: ModuleType, tmp_path: Path) -> None:
    cache_module.prune(tmp_path / "missing")
    keep, protect = cache_module.KEEP, cache_module.PROTECT
    for index in range(keep + 1):
        entry(tmp_path, f"e{index}", age=protect + 100, used=protect + 100 - index)
    entry(tmp_path, "old", age=cache_module.MAX_AGE + 1, used=cache_module.MAX_AGE + 1)
    entry(tmp_path, "old-in-use", age=cache_module.MAX_AGE + 1, used=10)
    entry(tmp_path, "half", files=("data.dump",))
    entry(tmp_path, ".young.1", age=10, files=())
    entry(tmp_path, ".stale.2", age=cache_module.STALE + 1, files=())
    (tmp_path / "file").write_text("not an entry")
    cache_module.prune(tmp_path)
    kept = sorted(path.name for path in tmp_path.iterdir())
    assert kept == sorted([".young.1", "file", "old-in-use", *(f"e{index}" for index in range(1, keep + 1))])


def triggers(database: HubDatabase) -> list[tuple[str, str, str]]:
    with database.admin() as admin:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in admin.execute(
                "SELECT c.relname, t.tgname, t.tgenabled FROM pg_catalog.pg_trigger t"
                " JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal ORDER BY 1, 2"
            )
        ]


def sequences(database: HubDatabase) -> list[tuple[str, int | None]]:
    with database.admin() as admin:
        return [
            (str(row[0]), row[1])
            for row in admin.execute("SELECT sequencename, last_value FROM pg_catalog.pg_sequences ORDER BY 1")
        ]


def test_the_rows_go_through_the_cache_into_a_migrated_database(
    cache_module: ModuleType, migrated_db: HubDatabase, tmp_path: Path
) -> None:
    admin_url = admin_url_of(migrated_db)
    modes = triggers(migrated_db)
    assert ("membership_entry", "membership_entry_no_truncate", "A") in modes
    user = uuid.uuid4()
    at = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    with migrated_db.admin() as admin:
        admin.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (user, at))
        before = admin.execute("SELECT count(*) FROM hub.principal").fetchone()
    counters = sequences(migrated_db)
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
    assert triggers(migrated_db) == modes
    assert sequences(migrated_db) == counters


def test_an_expired_entry_is_replaced_and_a_failed_restore_switches_the_triggers_back(
    cache_module: ModuleType, migrated_db: HubDatabase, tmp_path: Path
) -> None:
    admin_url = admin_url_of(migrated_db)
    entry(tmp_path, "k2", age=cache_module.MAX_AGE + 1)
    stored = cache_module.store(tmp_path, "k2", admin_url, {"state": "new"}, "ef" * 32)
    assert (stored / "signing.key").read_text() == "ef" * 32
    modes = triggers(migrated_db)
    (stored / "data.dump").write_text("not an archive")
    with pytest.raises(subprocess.CalledProcessError):
        cache_module.restore(admin_url, stored)
    assert triggers(migrated_db) == modes


def test_one_process_holds_the_seeding_lock(cache_module: ModuleType, tmp_path: Path) -> None:
    lock = cache_module.claim(tmp_path / "cache", "k")
    assert lock == tmp_path / "cache" / ".seeding-k"
    assert lock.read_text() == str(os.getpid())
    assert cache_module.claim(tmp_path / "cache", "k") == lock  # its own lock
    parent = os.getppid()
    assert cache_module.claim(tmp_path / "cache", "k", pid=parent) is None  # held by a live process
    assert cache_module.claim(tmp_path / "cache", "k", pid=parent, clock=time.time() + cache_module.LOCK_STALE) == lock
    assert lock.read_text() == str(parent)
    cache_module.release(lock)  # not this process's lock any more
    assert lock.exists()
    cache_module.release(lock, parent)
    assert not lock.exists()
    lock.write_text("999999999")  # a process that is gone
    assert cache_module.claim(tmp_path / "cache", "k") == lock
    cache_module.release(lock)
    lock.write_text("")  # not yet written by its creator
    assert cache_module.claim(tmp_path / "cache", "k") is None
    lock.unlink()
    cache_module.release(lock)  # nothing to release


def test_the_hub_test_server_restores_seeds_waits_or_falls_back(
    cache_module: ModuleType, migrated_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = script("hub_test_server")
    monkeypatch.setattr(server, "WAIT_SECONDS", 0.05)
    admin_url = admin_url_of(migrated_db)
    renewed: list[bool] = []
    name = cache_module.key(server_major=cache_module.server_major(admin_url))
    lock = tmp_path / f".seeding-{name}"
    key, environ = server.volume_data(tmp_path, admin_url, "11" * 32, lambda: renewed.append(True))
    assert key == "11" * 32
    assert environ == {
        "WEB_E2E_VOLUME_SNAPSHOT": str(tmp_path / name),
        "WEB_E2E_VOLUME_LOCK": str(lock),
        "WEB_E2E_VOLUME_ADMIN_URL": admin_url,
        "WEB_E2E_HUB_SIGNING_KEY": "11" * 32,
    }
    assert lock.read_text() == str(os.getpid())
    # another lane seeds: this one waits for its entry and restores it
    lock.write_text(str(os.getppid()))
    found: list[tuple[str, dict[str, str]]] = []
    waiter = threading.Thread(
        target=lambda: found.append(server.volume_data(tmp_path, admin_url, "11" * 32, lambda: renewed.append(True)))
    )
    waiter.start()
    time.sleep(0.3)
    assert not found
    cache_module.store(tmp_path, name, admin_url, {"state": {"state": "done"}}, "22" * 32)
    waiter.join(timeout=60)
    assert found == [("22" * 32, {"WEB_E2E_VOLUME_STATE": json.dumps({"state": {"state": "done"}})})]
    assert not renewed
    lock.unlink()
    (tmp_path / name / "data.dump").write_text("damaged")
    key, environ = server.volume_data(tmp_path, admin_url, "11" * 32, lambda: renewed.append(True))
    assert renewed == [True]
    assert key == "11" * 32
    assert environ["WEB_E2E_VOLUME_LOCK"] == str(lock)
    assert not (tmp_path / name).exists()
    cache_module.release(lock)


class Director:
    """What the seeder uses of the device double in these tests."""

    def __init__(self, pool: object = None) -> None:
        self.pool = pool
        self.identities: dict[str, object] = {}


def test_the_device_double_hands_its_state_to_a_restored_lane(volume_module: ModuleType) -> None:
    identity = volume_module.Identity
    founder = volume_module.Actor(str(uuid.uuid4()), "user", identity.generate())
    seeder = volume_module.Seeder(Director())
    seeder.org = volume_module.Org(str(uuid.uuid4()), str(uuid.uuid4()), founder)
    seeder.org.keys = {("wg", seeder.org.wg, 1): b"k" * 32, ("org", seeder.org.id, 1): b"o" * 32}
    seeder.device = volume_module.Actor(str(uuid.uuid4()), "device", identity.generate())
    seeder.live = [("t1", "s1"), ("t2", "s2")]
    seeder.workvm = False
    seeder.unseedable = {"chat:x": "refused"}
    seeder.root_key = b"r" * 32
    seeder.community_key = (1, b"c" * 32)
    saved = json.loads(json.dumps(seeder.export({"state": "done", "data": {"org": seeder.org.id, "seconds": 1}})))
    director = Director()
    restored = volume_module.Seeder(director)
    restored.restore(saved, serve=False)
    assert restored.status() == {"state": "done", "data": {"org": seeder.org.id, "seconds": 1, "cached": True}}
    assert restored.org is not None
    assert (restored.org.id, restored.org.wg, restored.org.keys) == (seeder.org.id, seeder.org.wg, seeder.org.keys)
    assert restored.org.founder.id == founder.id
    assert restored.org.founder.identity.public() == founder.identity.public()
    assert restored.device is not None
    assert restored.device.identity.public() == seeder.device.identity.public()
    assert director.identities[seeder.device.id] is restored.device.identity
    assert (restored.live, restored.workvm, restored.unseedable) == (seeder.live, False, {"chat:x": "refused"})
    assert (restored.root_key, restored.community_key) == (b"r" * 32, (1, b"c" * 32))


def test_the_volume_founder_serves_the_community_key(volume_module: ModuleType, migrated_db: HubDatabase) -> None:
    with ConnectionPool(migrated_db.writer_url, min_size=1, max_size=2, open=True) as pool:
        seeder = volume_module.Seeder(Director(pool))
        identity = volume_module.Identity
        founder = volume_module.Actor(str(volume_module.uuid7()), "user", identity.generate())
        other = volume_module.Actor(str(volume_module.uuid7()), "user", identity.generate())
        seeder.org = volume_module.Org(str(volume_module.uuid7()), str(volume_module.uuid7()), founder)
        assert seeder.serve_community() == 0  # no key yet
        at = volume_module.now()
        with pool.connection() as connection, connection.transaction():
            volume_module.insert_user(connection, founder, at)
            volume_module.insert_user(connection, other, at)
        seeder.community(founder, 3)
        assert seeder.community_key is not None
        epoch, key = seeder.community_key
        ephemeral = X25519PrivateKey.generate()
        request = str(volume_module.uuid7())
        kem_text = b64u_encode(ephemeral.public_key().public_bytes_raw())
        signature = community_format.sign_request(other.identity.sig, id=request, kem_pub=kem_text, user=other.id)
        with pool.connection() as connection, connection.transaction():
            store.join(connection, uuid.UUID(other.id), at)
            distribution.put_request(
                connection, uuid.UUID(other.id), request_id=uuid.UUID(request), kem_text=kem_text, signature=signature,
                now=at,
            )  # fmt: skip
            assert store.head_seq(connection) == 3
        publisher = MemoryPublisher()
        previous = set_publisher(publisher)
        try:
            assert seeder.serve_community() == 1
            assert seeder.serve_community() == 0
        finally:
            set_publisher(previous)
        with pool.connection() as connection:
            row = connection.execute(
                "SELECT epoch, wrap FROM hub.community_request WHERE id = %s", (uuid.UUID(request),)
            ).fetchone()
        assert row is not None
        opened = community_format.unwrap_community_key(
            ephemeral, epoch=int(row[0]), recipient=request, wrap_text=b64u_encode(bytes(row[1]))
        )
        assert (int(row[0]), opened) == (epoch, key)
        frames = [(item.audience.id, item.frame, dict(item.data)) for item in publisher.items()]
        assert frames == [(uuid.UUID(other.id), "community", {"change": "key", "seq": 3})]
