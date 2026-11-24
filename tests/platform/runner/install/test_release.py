"""U-RUN-install-release: signed release manifests and feeds (ADR-074 sections 1 and 5): versions, canonical JSON,
payload trees, release keys, signature verification and the validation of `release.json` and `latest.json`."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_runner.crypto.encoding import b64u_encode
from agent_runner.install import release, release_keys

from .conftest import COMMIT, HUB, ROOT_KEY, VERSION, Keys, make_version, manifest_doc, new_keys, refused
from .conftest import Kit as InstallKit

DIGEST = "ab" * 32


# --- versions, canonical JSON, digests ------------------------------------------------------------------------------


def test_versions_order_by_time() -> None:
    assert release.version_key("2026.11.24.0800") == (2026, 11, 24, 800)
    assert release.version_key("2026.11.24.0800") < release.version_key("2026.11.25.0001")


@pytest.mark.parametrize("text", ["2026.11.24", "2026.11.24.800", "v2026.11.24.0800", "2026.11.24.0800\n", ""])
def test_other_versions_are_invalid(text: str) -> None:
    with refused("release_invalid", "version"):
        release.version_key(text)


def test_canonical_json_is_sorted_compact_ascii_with_a_newline() -> None:
    assert release.canonical({"b": 1, "a": ["ä", {"z": None, "y": True}]}) == (
        b'{"a":["\\u00e4",{"y":true,"z":null}],"b":1}\n'
    )


def test_the_file_digest_is_sha256(tmp_path: Path) -> None:
    path = tmp_path / "f"
    data = os.urandom(release.CHUNK + 17)
    path.write_bytes(data)
    assert release.sha256_file(path) == hashlib.sha256(data).hexdigest()


# --- payload trees --------------------------------------------------------------------------------------------------


def test_the_tree_lists_files_and_links_but_not_the_manifest(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "lib/sub").mkdir(parents=True)
    (tree / "lib/sub/a.txt").write_bytes(b"a")
    (tree / "b.txt").write_bytes(b"b")
    (tree / "link").symlink_to("b.txt")
    (tree / "dirlink").symlink_to("lib")
    (tree / "empty").mkdir()
    (tree / release.MANIFEST).write_bytes(b"{}")
    (tree / (release.MANIFEST + release.SIGNATURE_SUFFIX)).write_bytes(b"sig")
    entries = release.tree_entries(tree)
    assert entries == {
        "b.txt": hashlib.sha256(b"b").hexdigest(),
        "dirlink": "link:lib",
        "lib/sub/a.txt": hashlib.sha256(b"a").hexdigest(),
        "link": "link:b.txt",
    }
    # sorted, and a directory link is recorded, not descended into
    assert list(entries) == sorted(entries)
    assert release.tree_entries(tree, skip=())[release.MANIFEST] == hashlib.sha256(b"{}").hexdigest()


def test_a_special_file_in_the_tree_is_refused(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    os.mkfifo(tree / "pipe")
    with refused("release_invalid", "file_type"):
        release.tree_entries(tree)


# --- keys and signatures --------------------------------------------------------------------------------------------


def test_a_signature_verifies_with_the_public_key(keys: Keys) -> None:
    data = release.canonical({"a": 1})
    signature = keys.sign(data)
    assert "=" not in signature
    assert release.verify(data, signature, [keys.public]) == {"a": 1}
    # surrounding whitespace of a `.sig` file is ignored
    assert release.verify(data, f" {signature}\n", [keys.public]) == {"a": 1}


def test_any_listed_key_may_have_signed(keys: Keys) -> None:
    other = new_keys()
    data = release.canonical({"a": 1})
    assert release.verify(data, keys.sign(data), [other.public, keys.public]) == {"a": 1}


def test_no_key_trusts_nothing(keys: Keys) -> None:
    data = release.canonical({"a": 1})
    with refused("release_key_missing"):
        release.verify(data, keys.sign(data), [])


def test_a_foreign_or_broken_signature_is_refused(keys: Keys) -> None:
    data = release.canonical({"a": 1})
    with refused("release_signature_invalid"):
        release.verify(data, new_keys().sign(data), [keys.public])
    with refused("release_signature_invalid"):
        release.verify(data + b" ", keys.sign(data), [keys.public])
    with refused("release_signature_invalid"):
        release.verify(data, "not base64!", [keys.public])
    with refused("release_signature_invalid"):
        release.verify(data, b64u_encode(bytes(63)), [keys.public])


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"not json\n", "json"),
        (b'{"b":1, "a":2}\n', "canonical"),
        (b'{"a":1}', "canonical"),
        (b"[1]\n", "canonical"),
    ],
)
def test_signed_data_must_be_one_canonical_object(keys: Keys, data: bytes, name: str) -> None:
    with refused("release_invalid", name):
        release.verify(data, keys.sign(data), [keys.public])


def test_the_packaged_key_list_is_valid() -> None:
    assert release.packaged_keys() == list(release_keys.KEYS)


def test_a_key_list_is_an_object_of_valid_public_keys(keys: Keys) -> None:
    assert release.keys_from({"keys": [keys.public]}) == [keys.public]
    assert release.keys_from({"keys": []}) == []


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"keys": "abc"},
        {"keys": [1]},
        {"keys": ["not base64!"]},
        {"keys": [b64u_encode(bytes(31))]},
    ],
)
def test_other_key_lists_are_refused(document: object) -> None:
    with refused("release_keys_invalid"):
        release.keys_from(document)


def test_the_public_key_belongs_to_the_private_key(keys: Keys) -> None:
    assert release.public_key(keys.private) == keys.public
    assert release.public_key(new_keys().private) != keys.public


# --- release.json ---------------------------------------------------------------------------------------------------


def test_a_manifest_round_trips() -> None:
    fetch = {"name": "claude-code", "url": "https://registry.example/c.tgz", "sha256": DIGEST}
    document = manifest_doc({"b": "x", "a": "y"}, fetch=[fetch])
    found = release.manifest_from(document)
    assert found == release.Manifest(
        version=VERSION,
        commit=COMMIT,
        hub=HUB,
        platform_root_key=ROOT_KEY,
        files={"a": "y", "b": "x"},
        fetch=(release.Fetch("claude-code", "https://registry.example/c.tgz", DIGEST),),
    )
    assert found.document() == document
    assert list(found.document()["files"]) == ["a", "b"]
    assert release.manifest_from(manifest_doc({}, hub="http://127.0.0.1:8080")).hub == "http://127.0.0.1:8080"
    assert release.manifest_from(manifest_doc({}, hub="http://localhost:8080")).hub == "http://localhost:8080"


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"v": 2},
        {"commit": 1},
        {"commit": "abc"},
        {"hub": 1},
        {"hub": "http://hub.example"},
        {"hub": "http://localhost.example/"},
        {"platform_root_key": None},
        {"files": []},
        {"files": {"a": 1}},
    ],
)
def test_other_manifests_are_refused(change: dict[str, Any]) -> None:
    with refused("release_invalid", "manifest"):
        release.manifest_from({**manifest_doc({}), **change})


def test_a_manifest_without_keys_is_refused() -> None:
    document = manifest_doc({})
    del document["files"]
    with refused("release_invalid", "manifest"):
        release.manifest_from(document)


@pytest.mark.parametrize("version", [None, 2026, "1.0"])
def test_a_manifest_version_must_be_a_release_version(version: object) -> None:
    with refused("release_invalid", "version"):
        release.manifest_from({**manifest_doc({}), "version": version})


GOOD_FETCH = {"name": "claude-code", "url": "https://registry.example/c.tgz", "sha256": DIGEST}


@pytest.mark.parametrize(
    "fetch",
    [
        {},
        [1],
        [{"name": "claude-code", "url": "https://x"}],
        [{**GOOD_FETCH, "extra": "x"}],
        [{**GOOD_FETCH, "name": 1}],
        [{**GOOD_FETCH, "name": "Claude"}],
        [{**GOOD_FETCH, "name": "-x"}],
        [{**GOOD_FETCH, "url": "http://registry.example/c.tgz"}],
        [{**GOOD_FETCH, "sha256": "AB" * 32}],
        [GOOD_FETCH, GOOD_FETCH],
    ],
)
def test_other_fetch_lists_are_refused(fetch: object) -> None:
    with refused("release_invalid", "fetch"):
        release.manifest_from({**manifest_doc({}), "fetch": fetch})


def test_an_installed_version_is_verified_file_by_file(kit: InstallKit, keys: Keys) -> None:
    folder = make_version(kit.layout, keys)
    found = release.read_manifest(folder, [keys.public])
    assert found.version == VERSION
    assert found.files == release.tree_entries(folder)
    assert "libexec/dfx-agent-runner" in found.files
    (folder / "libexec/dfx-agent-runner").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with refused("release_tree_mismatch"):
        release.read_manifest(folder, [keys.public])


def test_an_added_file_is_a_tree_mismatch(kit: InstallKit, keys: Keys) -> None:
    folder = make_version(kit.layout, keys)
    (folder / "libexec/extra").write_text("x", encoding="utf-8")
    with refused("release_tree_mismatch"):
        release.read_manifest(folder, [keys.public])


def test_an_installed_version_needs_its_manifest_and_signature(kit: InstallKit, keys: Keys) -> None:
    folder = make_version(kit.layout, keys)
    signature = folder / (release.MANIFEST + release.SIGNATURE_SUFFIX)
    signature.write_bytes("ä".encode())
    with refused("release_invalid", "missing"):
        release.read_manifest(folder, [keys.public])
    signature.unlink()
    with refused("release_invalid", "missing"):
        release.read_manifest(folder, [keys.public])
    with refused("release_invalid", "missing"):
        release.read_manifest(kit.layout.version_dir("absent"), [keys.public])


def test_an_installed_version_signed_by_another_key_is_refused(kit: InstallKit, keys: Keys) -> None:
    folder = make_version(kit.layout, keys)
    with refused("release_signature_invalid"):
        release.read_manifest(folder, [new_keys().public])


# --- latest.json ----------------------------------------------------------------------------------------------------


def feed_doc(**change: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "v": 1,
        "version": VERSION,
        "package": f"DFX-Agent-Runner-{VERSION}.pkg",
        "package_sha256": DIGEST,
        "manifest_sha256": "cd" * 32,
        "min_macos": "14.0",
        "notarized": False,
        "signed_at": "2026-11-24T08:00:00Z",
        "expires": "2027-03-24T08:00:00Z",
    }
    document.update(change)
    return document


def test_a_feed_round_trips_and_expires() -> None:
    feed = release.feed_from(feed_doc())
    assert feed == release.Feed(
        VERSION,
        f"DFX-Agent-Runner-{VERSION}.pkg",
        DIGEST,
        "cd" * 32,
        "14.0",
        False,
        "2026-11-24T08:00:00Z",
        "2027-03-24T08:00:00Z",
    )
    assert feed.document() == feed_doc()
    assert not feed.expired(datetime(2027, 3, 24, 7, 59, 59, tzinfo=UTC))
    assert feed.expired(datetime(2027, 3, 24, 8, 0, 0, tzinfo=UTC))
    assert release.feed_from(feed_doc(min_macos="15", notarized=True)).notarized is True


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"v": 2},
        {"package": 1},
        {"package_sha256": None},
        {"expires": 0},
        {"notarized": "yes"},
        {"package": "DFX-Agent-Runner-2026.11.24.0800.dmg"},
        {"package": "DFX-Agent-Runner-2026.11.25.0800.pkg"},
        {"package_sha256": "x"},
        {"manifest_sha256": "AB" * 32},
        {"min_macos": "14.0.1"},
        {"min_macos": "macOS 14"},
        {"signed_at": "2026-11-24 08:00:00"},
        {"expires": "2027-03-24T08:00:00+00:00"},
        {"expires": "2026-11-24T08:00:00Z"},
        {"expires": "2026-11-23T08:00:00Z"},
        {"expires": "2027-03-24T08:00:01Z"},
    ],
)
def test_other_feeds_are_refused(change: dict[str, Any]) -> None:
    with refused("release_invalid", "feed"):
        release.feed_from(feed_doc(**change))


def test_a_feed_lasts_at_most_the_maximum() -> None:
    assert release.FEED_MAX_DAYS == 120
    assert release.feed_from(feed_doc(expires="2027-03-24T08:00:00Z")).expires == "2027-03-24T08:00:00Z"


def test_a_feed_version_must_be_a_release_version() -> None:
    with refused("release_invalid", "version"):
        release.feed_from(feed_doc(version="1.0"))
