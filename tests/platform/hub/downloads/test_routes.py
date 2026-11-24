"""The download area of the hub (ADR-074 section 7, 03-api.md section 2): `GET /downloads/runner/{name}`
serves `latest.json`, `latest.json.sig` and the versioned runner package of `AGENT_HUB_DOWNLOADS_DIR` anonymously,
unchanged, with their content types and cache rules; every other name, a missing file, a symbolic link, a path that
resolves outside `runner/` and an unset setting are the JSON `404 not_found`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from psycopg_pool import ConnectionPool

from agent_hub.config import ConfigError, load_config
from agent_hub.downloads.routes import runner_file
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, hub_environ, start_hub

LATEST = b'{"version":"2026.11.24.0930","package":"DFX-Agent-Runner-2026.11.24.0930.pkg"}\n'
SIGNATURE = b"untrusted comment: signature\nRWQ0123456789abcdef==\n"
PACKAGE_NAME = "DFX-Agent-Runner-2026.11.24.0930.pkg"
PACKAGE = bytes(range(256)) * 512
NOT_FOUND = {"error": "not_found"}


@pytest.fixture
def downloads_dir(tmp_path: Path) -> Path:
    root = tmp_path / "downloads"
    (root / "runner").mkdir(parents=True)
    (root / "runner" / "latest.json").write_bytes(LATEST)
    (root / "runner" / "latest.json.sig").write_bytes(SIGNATURE)
    (root / "runner" / PACKAGE_NAME).write_bytes(PACKAGE)
    (root / "runner" / "notes.txt").write_text("not allowed\n")
    (root / "outside.pkg").write_bytes(b"outside runner/\n")
    return root


def harness(db: HubDatabase, pool: ConnectionPool[Any], clock: FixedClock, **overrides: str) -> Iterator[HubHarness]:
    config = load_config(hub_environ(db, **overrides))
    yield from start_hub(config, pool, clock)


@pytest.fixture
def hub_with_downloads(
    migrated_db: HubDatabase, downloads_dir: Path, hub_pool: ConnectionPool[Any], clock: FixedClock
) -> Iterator[HubHarness]:
    yield from harness(migrated_db, hub_pool, clock, AGENT_HUB_DOWNLOADS_DIR=str(downloads_dir))


def assert_not_found(hub: HubHarness, path: str) -> None:
    response = hub.anon.get(path)
    assert response.status_code == 404, path
    assert response.json() == NOT_FOUND, path


def test_latest_json_is_served_unchanged_and_revalidated(hub_with_downloads: HubHarness) -> None:
    response = hub_with_downloads.anon.get("/downloads/runner/latest.json")
    assert response.status_code == 200
    assert response.content == LATEST
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


def test_head_of_the_package_answers_its_headers_without_reading_it(
    hub_with_downloads: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = hub_with_downloads.anon.get(f"/downloads/runner/{PACKAGE_NAME}")
    opened: list[object] = []

    def no_read(*args: object, **kwargs: object) -> None:
        opened.append(args)
        raise AssertionError("the package was opened")

    monkeypatch.setattr(anyio, "open_file", no_read)  # what FileResponse opens its file with
    head = hub_with_downloads.anon.request("HEAD", f"/downloads/runner/{PACKAGE_NAME}")
    assert opened == []
    assert head.status_code == 200
    assert head.content == b""
    assert dict(head.headers) == dict(got.headers)
    assert head.headers["content-length"] == str(len(PACKAGE))


def test_head_of_the_feed_and_of_a_missing_file_answer_like_get(hub_with_downloads: HubHarness) -> None:
    for name in ("latest.json", "latest.json.sig", "DFX-Agent-Runner-2020.01.01.0000.pkg", "notes.txt"):
        got = hub_with_downloads.anon.get(f"/downloads/runner/{name}")
        head = hub_with_downloads.anon.request("HEAD", f"/downloads/runner/{name}")
        assert (head.status_code, dict(head.headers)) == (got.status_code, dict(got.headers)), name
        assert head.content == b"", name


def test_signature_is_ascii_text_and_revalidated(hub_with_downloads: HubHarness) -> None:
    response = hub_with_downloads.anon.get("/downloads/runner/latest.json.sig")
    assert response.status_code == 200
    assert response.content == SIGNATURE
    assert response.headers["content-type"] == "text/plain; charset=us-ascii"
    assert response.headers["cache-control"] == "no-cache"


def test_versioned_package_is_streamed_and_immutable(hub_with_downloads: HubHarness) -> None:
    response = hub_with_downloads.anon.get(f"/downloads/runner/{PACKAGE_NAME}")
    assert response.status_code == 200
    assert response.content == PACKAGE
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-length"] == str(len(PACKAGE))


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "latest.JSON",
        "DFX-Agent-Runner-2026.11.24.093.pkg",
        "DFX-Agent-Runner-2026.11.24.0930.pkg.sig",
        "DFX-Agent-Runner-latest.pkg",
        "x" + PACKAGE_NAME,
        "..outside.pkg",
    ],
)
def test_other_names_are_not_found(hub_with_downloads: HubHarness, name: str) -> None:
    assert_not_found(hub_with_downloads, f"/downloads/runner/{name}")


def test_missing_files_are_not_found(hub_with_downloads: HubHarness, downloads_dir: Path) -> None:
    (downloads_dir / "runner" / "latest.json.sig").unlink()
    assert_not_found(hub_with_downloads, "/downloads/runner/latest.json.sig")
    assert_not_found(hub_with_downloads, "/downloads/runner/DFX-Agent-Runner-2026.11.24.0931.pkg")


def test_a_directory_with_an_allowed_name_is_not_found(hub_with_downloads: HubHarness, downloads_dir: Path) -> None:
    (downloads_dir / "runner" / "DFX-Agent-Runner-2026.11.24.0931.pkg").mkdir()
    assert_not_found(hub_with_downloads, "/downloads/runner/DFX-Agent-Runner-2026.11.24.0931.pkg")


def test_symbolic_links_are_not_found(hub_with_downloads: HubHarness, downloads_dir: Path) -> None:
    runner = downloads_dir / "runner"
    (runner / "latest.json").unlink()
    (runner / "latest.json").symlink_to(runner / PACKAGE_NAME)
    (runner / "DFX-Agent-Runner-2026.11.24.0931.pkg").symlink_to(downloads_dir / "outside.pkg")
    assert_not_found(hub_with_downloads, "/downloads/runner/latest.json")
    assert_not_found(hub_with_downloads, "/downloads/runner/DFX-Agent-Runner-2026.11.24.0931.pkg")


def test_a_path_resolving_outside_runner_is_not_found(tmp_path: Path) -> None:
    # `runner/` itself a link: the file is no link, but it resolves outside `<dir>/runner/`.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "latest.json").write_bytes(LATEST)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "runner").symlink_to(elsewhere)
    assert runner_file(downloads.resolve(), "latest.json") is None
    real = tmp_path / "real"
    (real / "runner").mkdir(parents=True)
    (real / "runner" / "latest.json").write_bytes(LATEST)
    assert runner_file(real.resolve(), "latest.json") == (real / "runner" / "latest.json").resolve()


def test_without_the_setting_every_download_is_not_found(
    migrated_db: HubDatabase, hub_pool: ConnectionPool[Any], clock: FixedClock
) -> None:
    for hub in harness(migrated_db, hub_pool, clock):
        assert hub.config.downloads_dir is None
        assert_not_found(hub, "/downloads/runner/latest.json")
        assert_not_found(hub, "/downloads/runner/latest.json.sig")
        assert_not_found(hub, f"/downloads/runner/{PACKAGE_NAME}")


def test_the_download_route_is_not_in_the_schema(hub_with_downloads: HubHarness) -> None:
    assert "/downloads/runner/{name}" not in hub_with_downloads.app.openapi()["paths"]


def test_config_reads_the_downloads_dir(tmp_path: Path) -> None:
    assert load_config(hub_environ()).downloads_dir is None
    assert load_config(hub_environ(AGENT_HUB_DOWNLOADS_DIR="")).downloads_dir is None
    assert load_config(hub_environ(AGENT_HUB_DOWNLOADS_DIR=str(tmp_path))).downloads_dir == tmp_path.resolve()


@pytest.mark.parametrize("value", [".", "/nonexistent/downloads", "FILE"])
def test_config_refuses_a_downloads_dir_that_is_no_absolute_existing_directory(tmp_path: Path, value: str) -> None:
    if value == "FILE":
        value = str(tmp_path / "file")
        Path(value).write_text("")
    with pytest.raises(ConfigError) as error:
        load_config(hub_environ(AGENT_HUB_DOWNLOADS_DIR=value))
    assert str(error.value) == "AGENT_HUB_DOWNLOADS_DIR must be the absolute path of an existing directory"
