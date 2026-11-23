"""Device identity files (docs/platform/spec/05-runner.md section 1.4, 02-data-model.md section 4).

`$AGENT_RUNNER_HOME/device.json` (`device_id`, `kind`, `hub_url`, `hub_key`, `enrolled_at`) and
`$AGENT_RUNNER_HOME/keys/device.key` (`{"kem","sig","v":1}`: X25519 private key and Ed25519 seed as b64u). Both
live outside the store, so a store wipe never changes the identity.

Writing follows the pattern of `agent_cli.keys.create_key` / `_write_new_file` (copied and adapted): directory
mode 0700, file mode 0600, an existing file or a symbolic link is never overwritten or followed. The content is
written to a temporary file opened with `O_EXCL` in the same directory, synced and then hard-linked to the final
name, so a reader sees either no file or the complete file, and an existing file makes the write fail.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.errors import RunnerError

DEVICE_FILE = "device.json"
KEYS_DIR = "keys"
KEY_FILE = "device.key"
KEY_VERSION = 1
DEVICE_KINDS = ("org", "platform")
_RAW_KEY_LENGTH = 32
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_RAW = serialization.Encoding.Raw
_NO_ENCRYPTION = serialization.NoEncryption()


@dataclass(frozen=True)
class DeviceKeys:
    """The device's private keys: X25519 `kem` and Ed25519 seed `sig`, 32 raw bytes each."""

    kem: bytes
    sig: bytes

    def __repr__(self) -> str:
        return "DeviceKeys(<private>)"

    @property
    def kem_public(self) -> str:
        """b64u of the X25519 public key."""
        public = X25519PrivateKey.from_private_bytes(self.kem).public_key()
        return b64u_encode(public.public_bytes(_RAW, serialization.PublicFormat.Raw))

    @property
    def sig_public(self) -> str:
        """b64u of the Ed25519 public key."""
        public = Ed25519PrivateKey.from_private_bytes(self.sig).public_key()
        return b64u_encode(public.public_bytes(_RAW, serialization.PublicFormat.Raw))

    def signing_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.sig)

    def kem_key(self) -> X25519PrivateKey:
        return X25519PrivateKey.from_private_bytes(self.kem)


@dataclass(frozen=True)
class Device:
    """Content of `device.json`."""

    device_id: str
    kind: Literal["org", "platform"]
    hub_url: str
    hub_key: str
    enrolled_at: str

    @property
    def key_id(self) -> str:
        """Key ID of signed device requests: `device.<32 hex>` (03-api.md section 1)."""
        return "device." + uuid.UUID(self.device_id).hex


@dataclass(frozen=True)
class Identity:
    device: Device
    keys: DeviceKeys


def generate_keys() -> DeviceKeys:
    """New random device keys."""
    kem = X25519PrivateKey.generate().private_bytes(_RAW, serialization.PrivateFormat.Raw, _NO_ENCRYPTION)
    sig = Ed25519PrivateKey.generate().private_bytes(_RAW, serialization.PrivateFormat.Raw, _NO_ENCRYPTION)
    return DeviceKeys(kem=kem, sig=sig)


def canonical(value: object) -> bytes:
    """Canonical JSON: sorted keys, no insignificant white space, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def keys_text(keys: DeviceKeys) -> bytes:
    return canonical({"kem": b64u_encode(keys.kem), "sig": b64u_encode(keys.sig), "v": KEY_VERSION})


def device_text(device: Device) -> bytes:
    return canonical(
        {
            "device_id": device.device_id,
            "enrolled_at": device.enrolled_at,
            "hub_key": device.hub_key,
            "hub_url": device.hub_url,
            "kind": device.kind,
        }
    )


def key_path(home: Path) -> Path:
    return home / KEYS_DIR / KEY_FILE


def device_path(home: Path) -> Path:
    return home / DEVICE_FILE


def ensure_private_dir(path: Path) -> None:
    """Create `path` (and parents) with mode 0700; a symbolic link or a non-directory is refused."""
    if path.is_symlink():
        raise RunnerError("identity_symlink", path.name)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        raise RunnerError("identity_not_directory", path.name) from None
    os.chmod(path, 0o700)


def write_new_file(path: Path, data: bytes) -> None:
    """Create `path` atomically with mode 0600; refuse an existing file or symbolic link (`identity_exists`).

    `link(2)` fails with EEXIST for any existing directory entry, a dangling symbolic link included.
    """
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise RunnerError("identity_exists", path.name) from None
    finally:
        temporary.unlink()


def write_keys(home: Path, keys: DeviceKeys) -> None:
    """Write `keys/device.key`; the keys directory gets mode 0700."""
    ensure_private_dir(home)
    ensure_private_dir(home / KEYS_DIR)
    write_new_file(key_path(home), keys_text(keys))


def write_device(home: Path, device: Device) -> None:
    """Write `device.json` (enrollment writes it last, after the keys)."""
    _check_device(device)
    ensure_private_dir(home)
    write_new_file(device_path(home), device_text(device))


def _read_private(path: Path) -> bytes | None:
    """Content of an owner-only regular file; None when it does not exist. Symlinks and wide modes are refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        # O_NOFOLLOW on a symbolic link fails with ELOOP; every other open error is refused the same way.
        raise RunnerError("identity_unreadable", path.name) from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        os.close(fd)
        raise RunnerError("identity_mode" if stat.S_ISREG(info.st_mode) else "identity_unreadable", path.name)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _parse_object(data: bytes, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(data)
    except ValueError:
        raise RunnerError("identity_invalid", name) from None
    if not isinstance(parsed, dict):
        raise RunnerError("identity_invalid", name)
    return cast(dict[str, Any], parsed)


def _raw_key(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise RunnerError("identity_invalid", name)
    try:
        raw = b64u_decode(value)
    except ValueError:
        raise RunnerError("identity_invalid", name) from None
    if len(raw) != _RAW_KEY_LENGTH:
        raise RunnerError("identity_invalid", name)
    return raw


def parse_keys(data: bytes) -> DeviceKeys:
    parsed = _parse_object(data, KEY_FILE)
    if set(parsed) != {"kem", "sig", "v"} or parsed["v"] != KEY_VERSION or isinstance(parsed["v"], bool):
        raise RunnerError("identity_invalid", KEY_FILE)
    return DeviceKeys(kem=_raw_key(parsed["kem"], KEY_FILE), sig=_raw_key(parsed["sig"], KEY_FILE))


def _check_device(device: Device) -> None:
    try:
        canonical_id = str(uuid.UUID(device.device_id))
    except ValueError:
        raise RunnerError("identity_invalid", DEVICE_FILE) from None
    if canonical_id != device.device_id or device.kind not in DEVICE_KINDS:
        raise RunnerError("identity_invalid", DEVICE_FILE)
    if not device.hub_url or not _TIME_RE.fullmatch(device.enrolled_at):
        raise RunnerError("identity_invalid", DEVICE_FILE)
    _raw_key(device.hub_key, DEVICE_FILE)


def parse_device(data: bytes) -> Device:
    parsed = _parse_object(data, DEVICE_FILE)
    fields = ("device_id", "enrolled_at", "hub_key", "hub_url", "kind")
    if set(parsed) != set(fields) or not all(isinstance(parsed[field], str) for field in fields):
        raise RunnerError("identity_invalid", DEVICE_FILE)
    device = Device(
        device_id=parsed["device_id"],
        kind=parsed["kind"],
        hub_url=parsed["hub_url"],
        hub_key=parsed["hub_key"],
        enrolled_at=parsed["enrolled_at"],
    )
    _check_device(device)
    return device


def load_keys(home: Path) -> DeviceKeys | None:
    if (home / KEYS_DIR).is_symlink():
        # O_NOFOLLOW covers only the last path component; the keys directory itself must not be a link either.
        raise RunnerError("identity_unreadable", KEYS_DIR)
    data = _read_private(key_path(home))
    return None if data is None else parse_keys(data)


def load_device(home: Path) -> Device | None:
    data = _read_private(device_path(home))
    return None if data is None else parse_device(data)


def load_identity(home: Path) -> Identity | None:
    """The complete identity, None when the device is not enrolled; one file without the other is refused."""
    keys = load_keys(home)
    device = load_device(home)
    if keys is None and device is None:
        return None
    if keys is None or device is None:
        raise RunnerError("identity_incomplete")
    return Identity(device=device, keys=keys)


def remove_identity(home: Path) -> None:
    """Delete `device.json`, `keys/device.key` and the keys directory when empty (rollback, uninstall)."""
    keys_dir = home / KEYS_DIR
    if keys_dir.is_symlink():
        # Never reach through a symbolic link: remove the link itself, the target stays.
        keys_dir.unlink()
    elif key_path(home).is_symlink() or key_path(home).exists():
        key_path(home).unlink()
    path = device_path(home)
    if path.is_symlink() or path.exists():
        path.unlink()
    if keys_dir.is_dir() and not any(keys_dir.iterdir()):
        keys_dir.rmdir()
