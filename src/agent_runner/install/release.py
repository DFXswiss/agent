"""Signed release manifests of the runner package (ADR-074 sections 1 and 5).

A release has two signed documents, each a canonical JSON text (sorted keys, no spaces, one final newline) with an
Ed25519 signature over its exact bytes in a `.sig` file next to it (base64url, no padding):

- `release.json` inside the payload: version, commit, the hub and platform root key the build is for, the SHA-256 of
  every payload file (a symbolic link is recorded as `link:<target>`) and the pinned downloads the installation
  fetches itself (Claude Code for the host, which is not redistributed);
- `latest.json` in the hub's download area: version, package file name and its SHA-256, the digest of the release
  manifest, the oldest macOS the package supports, whether the package is notarised, and an expiry (at most
  `FEED_MAX_DAYS` after signing) so a hub cannot freeze devices on an old release.

Only keys listed in `release_keys.py` are accepted (public values; the private key is held by the
release operator, never by the repository, the hub or a device). Versions are `YYYY.MM.DD.HHMM` of the commit time
(UTC), so two builds of one commit carry the same version and versions order by time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.errors import RunnerError
from agent_runner.install import release_keys

MANIFEST = "release.json"
FEED = "latest.json"
SIGNATURE_SUFFIX = ".sig"
KEYS_MODULE = "release_keys.py"
MAX_DOCUMENT = 64 * 1024 * 1024
CHUNK = 1024 * 1024
VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.(\d{4})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PACKAGE_RE = re.compile(r"^DFX-Agent-Runner-\d{4}\.\d{2}\.\d{2}\.\d{4}\.pkg$")
MACOS_RE = re.compile(r"^\d{2}(\.\d{1,2})?$")
FEED_MAX_DAYS = 120
LOOPBACK_OR_HTTPS = ("https://", "http://127.0.0.1:", "http://localhost:")
"""A hub origin: HTTPS, or loopback HTTP of a test hub (the runner then runs with `TEST_MODE=1`)."""
_MANIFEST_KEYS = frozenset({"v", "version", "commit", "hub", "platform_root_key", "files", "fetch"})
_FETCH_KEYS = frozenset({"name", "url", "sha256"})
_FETCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
_FEED_KEYS = frozenset(
    {"v", "version", "package", "package_sha256", "manifest_sha256", "min_macos", "notarized", "signed_at", "expires"}
)
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def version_key(version: str) -> tuple[int, ...]:
    """The sortable form of a release version; `RunnerError("release_invalid")` for anything else."""
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise RunnerError("release_invalid", "version")
    return tuple(int(part) for part in match.groups())


def canonical(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def tree_entries(tree: Path, skip: Iterable[str] = (MANIFEST, MANIFEST + SIGNATURE_SUFFIX)) -> dict[str, str]:
    """`relative posix path -> sha256 | link:<target>` of every file and link below `tree` (directories implied)."""
    skipped = frozenset(skip)
    found: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(tree):
        base = Path(directory)
        for name in sorted([*dirnames, *filenames]):
            path = base / name
            relative = path.relative_to(tree).as_posix()
            if relative in skipped:
                continue
            if path.is_symlink():
                found[relative] = "link:" + os.readlink(path)
            elif path.is_file():
                found[relative] = sha256_file(path)
            elif not path.is_dir():
                raise RunnerError("release_invalid", "file_type")
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
    return dict(sorted(found.items()))


def sign(data: bytes, private_key: bytes) -> str:
    return b64u_encode(Ed25519PrivateKey.from_private_bytes(private_key).sign(data))


def public_key(private_key: bytes) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return b64u_encode(raw)


def packaged_keys() -> list[str]:
    """The public release keys shipped with this code (`install/release_keys.py`)."""
    return keys_from({"keys": list(release_keys.KEYS)})


def keys_from(document: object) -> list[str]:
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise RunnerError("release_keys_invalid")
    for key in keys:
        _public(key)
    return list(keys)


def _public(key: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(b64u_decode(key, 32))
    except (CryptoError, ValueError):
        raise RunnerError("release_keys_invalid") from None


def verify(data: bytes, signature: str, keys: Sequence[str]) -> dict[str, Any]:
    """The JSON object `data` when one of `keys` signed it; `RunnerError` otherwise.

    No key means nothing can be trusted (`release_key_missing`).
    """
    if not keys:
        raise RunnerError("release_key_missing")
    try:
        raw = b64u_decode(signature.strip(), 64)
    except CryptoError:
        raise RunnerError("release_signature_invalid") from None
    for key in keys:
        try:
            _public(key).verify(raw, data)
        except InvalidSignature:
            continue
        try:
            document = json.loads(data)
        except ValueError:
            raise RunnerError("release_invalid", "json") from None
        if not isinstance(document, dict) or canonical(document) != data:
            raise RunnerError("release_invalid", "canonical")
        return document
    raise RunnerError("release_signature_invalid")


@dataclass(frozen=True)
class Fetch:
    """A file the installation downloads itself: `name` (also its directory), HTTPS URL and SHA-256."""

    name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Manifest:
    version: str
    commit: str
    hub: str
    platform_root_key: str
    files: dict[str, str]
    fetch: tuple[Fetch, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "v": 1,
            "version": self.version,
            "commit": self.commit,
            "hub": self.hub,
            "platform_root_key": self.platform_root_key,
            "files": dict(sorted(self.files.items())),
            "fetch": [{"name": item.name, "url": item.url, "sha256": item.sha256} for item in self.fetch],
        }


def _fetch_from(items: object) -> tuple[Fetch, ...]:
    if not isinstance(items, list):
        raise RunnerError("release_invalid", "fetch")
    found: list[Fetch] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != _FETCH_KEYS
            or not all(isinstance(item[key], str) for key in _FETCH_KEYS)
            or _FETCH_NAME_RE.fullmatch(item["name"]) is None
            or not item["url"].startswith("https://")
            or DIGEST_RE.fullmatch(item["sha256"]) is None
        ):
            raise RunnerError("release_invalid", "fetch")
        found.append(Fetch(item["name"], item["url"], item["sha256"]))
    if len({item.name for item in found}) != len(found):
        raise RunnerError("release_invalid", "fetch")
    return tuple(found)


def manifest_from(document: Mapping[str, Any]) -> Manifest:
    files = document.get("files")
    if (
        set(document) != _MANIFEST_KEYS
        or document.get("v") != 1
        or not isinstance(document.get("commit"), str)
        or COMMIT_RE.fullmatch(document["commit"]) is None
        or not isinstance(document.get("hub"), str)
        or not document["hub"].startswith(LOOPBACK_OR_HTTPS)
        or not isinstance(document.get("platform_root_key"), str)
        or not isinstance(files, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        raise RunnerError("release_invalid", "manifest")
    version_key(str(document.get("version")))
    return Manifest(
        version=str(document["version"]),
        commit=document["commit"],
        hub=document["hub"],
        platform_root_key=document["platform_root_key"],
        files=dict(files),
        fetch=_fetch_from(document.get("fetch")),
    )


def read_manifest(tree: Path, keys: Sequence[str]) -> Manifest:
    """The verified manifest of an installed version directory (signature, then every file)."""
    try:
        data = (tree / MANIFEST).read_bytes()
        signature = (tree / (MANIFEST + SIGNATURE_SUFFIX)).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise RunnerError("release_invalid", "missing") from None
    found = manifest_from(verify(data, signature, keys))
    if tree_entries(tree) != found.files:
        raise RunnerError("release_tree_mismatch")
    return found


@dataclass(frozen=True)
class Feed:
    version: str
    package: str
    package_sha256: str
    manifest_sha256: str
    min_macos: str
    notarized: bool
    signed_at: str
    expires: str

    def document(self) -> dict[str, Any]:
        return {
            "v": 1,
            "version": self.version,
            "package": self.package,
            "package_sha256": self.package_sha256,
            "manifest_sha256": self.manifest_sha256,
            "min_macos": self.min_macos,
            "notarized": self.notarized,
            "signed_at": self.signed_at,
            "expires": self.expires,
        }

    def expired(self, now: datetime) -> bool:
        return now >= _time(self.expires)


def _time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def feed_from(document: Mapping[str, Any]) -> Feed:
    names = ("package", "package_sha256", "manifest_sha256", "min_macos", "signed_at", "expires")
    values = [document.get(key) for key in names]
    if (
        set(document) != _FEED_KEYS
        or document.get("v") != 1
        or not all(isinstance(v, str) for v in values)
        or not isinstance(document.get("notarized"), bool)
    ):
        raise RunnerError("release_invalid", "feed")
    version = str(document.get("version"))
    version_key(version)
    package, package_sha, manifest_sha, min_macos, signed_at, expires = (str(v) for v in values)
    if (
        PACKAGE_RE.fullmatch(package) is None
        or package != f"DFX-Agent-Runner-{version}.pkg"
        or DIGEST_RE.fullmatch(package_sha) is None
        or DIGEST_RE.fullmatch(manifest_sha) is None
        or MACOS_RE.fullmatch(min_macos) is None
        or _TIME_RE.fullmatch(signed_at) is None
        or _TIME_RE.fullmatch(expires) is None
        or not timedelta(0) < _time(expires) - _time(signed_at) <= timedelta(days=FEED_MAX_DAYS)
    ):
        raise RunnerError("release_invalid", "feed")
    return Feed(version, package, package_sha, manifest_sha, min_macos, document["notarized"], signed_at, expires)
