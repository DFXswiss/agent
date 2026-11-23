"""`GET /build-manifest.json` (06-web.md sections 10.3 and 10.5): the security page reads the manifest of the build the
hub delivers and computes the build hash from its exact bytes; served unchanged, revalidated, with the security
headers. Without a build the path is the JSON `404 not_found`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from agent_hub.config import load_config
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, hub_environ, start_hub

MANIFEST = b'{\n  "version": "0.1.0",\n  "commit": "0123456789abcdef0123456789abcdef01234567",\n  "files": []\n}\n'


def harness(db: HubDatabase, static_dir: Path, pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    config = load_config(hub_environ(db, AGENT_HUB_STATIC_DIR=str(static_dir)))
    yield from start_hub(config, pool, clock)


@pytest.fixture
def built(
    migrated_db: HubDatabase, tmp_path: Path, hub_pool: ConnectionPool[Any], clock: FixedClock
) -> Iterator[HubHarness]:
    (tmp_path / "index.html").write_bytes(b"<!doctype html>")
    (tmp_path / "build-manifest.json").write_bytes(MANIFEST)
    yield from harness(migrated_db, tmp_path, hub_pool, clock)


@pytest.fixture
def unbuilt(
    migrated_db: HubDatabase, tmp_path: Path, hub_pool: ConnectionPool[Any], clock: FixedClock
) -> Iterator[HubHarness]:
    yield from harness(migrated_db, tmp_path, hub_pool, clock)


def test_serves_the_manifest_bytes_unchanged(built: HubHarness) -> None:
    response = built.anon.get("/build-manifest.json")
    assert response.status_code == 200
    assert response.content == MANIFEST
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


def test_without_a_build_the_manifest_is_not_found(unbuilt: HubHarness) -> None:
    response = unbuilt.anon.get("/build-manifest.json")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}
