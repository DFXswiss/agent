"""Load the keys file (KEY-PROTOCOL.md section 3). Fail closed on a broken file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .signing import (
    ED25519_KEY_BYTES,
    P256_KEY_BYTES,
    b64url_decode,
    ed25519_public_key_valid,
    p256_public_key_valid,
    parse_time,
    valid_key_id,
)
from .teams import Roster, _UniqueKeyLoader

PRINCIPALS = ("staff", "agent", "service")
KIND_OF_PRINCIPAL = {"staff": "webauthn", "agent": "ed25519", "service": "ed25519"}
SCOPE_HOLDERS: dict[str, frozenset[str]] = {
    "record:read": frozenset({"staff", "agent", "service"}),
    "record:write": frozenset({"staff", "agent"}),
    "release:query": frozenset({"service"}),
    "keys:invite": frozenset({"staff"}),
    "device:executor": frozenset({"agent"}),
    "device:controller": frozenset({"agent"}),
}
DEVICE_SCOPES = frozenset({"device:executor", "device:controller"})
INVITE_SCOPE = "keys:invite"
FIELDS = frozenset(
    {
        "id",
        "kind",
        "principal",
        "handle",
        "team",
        "public_key",
        "credential_id",
        "device",
        "program",
        "scopes",
        "subjects",
        "expires",
        "revoked",
    }
)
_COMMON = frozenset({"id", "kind", "principal", "handle", "team", "public_key", "scopes"})
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "staff": _COMMON | {"credential_id"},
    "agent": _COMMON | {"device"},
    "service": _COMMON | {"device", "subjects", "expires"},
}
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "staff": REQUIRED_FIELDS["staff"] | {"revoked"},
    "agent": REQUIRED_FIELDS["agent"] | {"program", "revoked"},
    "service": REQUIRED_FIELDS["service"] | {"program", "revoked"},
}
CREDENTIAL_ID_MIN_BYTES = 16
CREDENTIAL_ID_MAX_BYTES = 1023
HANDLE_MAX = 39
REPOSITORY_NAME_MAX = 100

_HANDLE_CHARS = re.compile(r"[a-z0-9-]+")
_REPOSITORY_NAME_CHARS = re.compile(r"[a-z0-9._-]+")


class KeysError(ValueError):
    pass


@dataclass(frozen=True)
class KeyEntry:
    id: str
    kind: str
    principal: str
    handle: str
    team: str
    public_key: bytes
    scopes: tuple[str, ...]
    credential_id: bytes | None
    device: str | None
    program: str | None
    subjects: tuple[str, ...] | None
    expires: int | None
    revoked: int | None

    def revoked_at(self, now: int) -> bool:
        return self.revoked is not None and self.revoked <= now

    def expired_at(self, now: int) -> bool:
        return self.expires is not None and self.expires <= now


@dataclass(frozen=True)
class KeyRing:
    entries: dict[str, KeyEntry]

    def get(self, key_id: str) -> KeyEntry | None:
        return self.entries.get(key_id)


def valid_handle(value: object) -> bool:
    """1 to 39 of a-z, 0-9 and `-`, starting and ending alphanumeric, no `--`."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= HANDLE_MAX
        and _HANDLE_CHARS.fullmatch(value) is not None
        and not value.startswith("-")
        and not value.endswith("-")
        and "--" not in value
    )


def valid_subject(value: object) -> bool:
    """`owner/name`: owner in the handle form, name 1 to 100 of a-z, 0-9, `.`, `_`, `-`."""
    if not isinstance(value, str):
        return False
    owner, slash, name = value.partition("/")
    if slash != "/" or not valid_handle(owner):
        return False
    if not 1 <= len(name) <= REPOSITORY_NAME_MAX or name in (".", ".."):
        return False
    return _REPOSITORY_NAME_CHARS.fullmatch(name) is not None


def load_keys(path: Path, roster: Roster, now: int) -> KeyRing:
    """Read and check the keys file. `now` decides which entries count as revoked at start."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KeysError(f"keys file is not readable: {path}") from exc
    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except (yaml.YAMLError, TypeError) as exc:
        # TypeError: a mapping key YAML cannot hash, such as a list.
        raise KeysError(f"keys file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict) or list(data.keys()) != ["keys"]:
        raise KeysError(f"keys file must be a mapping with exactly one key, 'keys': {path}")
    items = data["keys"]
    if not isinstance(items, list):
        raise KeysError(f"'keys' must be a list: {path}")
    entries = [_entry(index, item, roster) for index, item in enumerate(items)]
    _check_unique(entries)
    _check_invite(entries, now)
    _check_devices(entries, now)
    return KeyRing({entry.id: entry for entry in entries})


def _entry(index: int, item: Any, roster: Roster) -> KeyEntry:
    where = f"keys entry {index}"
    if not isinstance(item, dict):
        raise KeysError(f"{where} must be a mapping")
    for field in item:
        if not isinstance(field, str) or field not in FIELDS:
            raise KeysError(f"{where} has an unknown field {field!r}")
    if "principal" not in item:
        raise KeysError(f"{where} is missing the field principal")
    principal = item["principal"]
    if not isinstance(principal, str) or principal not in PRINCIPALS:
        raise KeysError(f"{where}: principal must be one of {', '.join(PRINCIPALS)}")
    missing = sorted(REQUIRED_FIELDS[principal] - item.keys())
    if missing:
        raise KeysError(f"{where} is missing the field {missing[0]}")
    refused = sorted(item.keys() - ALLOWED_FIELDS[principal])
    if refused:
        raise KeysError(f"{where}: the field {refused[0]} is refused for principal {principal}")

    key_id = _text(item, "id", where)
    if not valid_key_id(key_id):
        raise KeysError(f"{where}: id breaks the id rule")
    where = f"keys entry {key_id!r}"

    kind = _text(item, "kind", where)
    if kind != KIND_OF_PRINCIPAL[principal]:
        raise KeysError(f"{where}: kind must be {KIND_OF_PRINCIPAL[principal]} for {principal}")

    handle = _text(item, "handle", where)
    if not valid_handle(handle):
        raise KeysError(f"{where}: handle breaks the handle rule")
    team = _text(item, "team", where)
    if team not in roster.teams:
        raise KeysError(f"{where}: team {team!r} is not in the teams file")
    if principal in ("staff", "agent"):
        if handle not in roster.teams[team]:
            raise KeysError(f"{where}: handle {handle!r} is not a member of team {team!r}")
        if roster.is_agent(handle):
            raise KeysError(f"{where}: handle {handle!r} is a login in the agents map")

    public_key = _binary(item, "public_key", where)
    if kind == "ed25519":
        if len(public_key) != ED25519_KEY_BYTES:
            raise KeysError(f"{where}: public_key must be {ED25519_KEY_BYTES} bytes")
        if not ed25519_public_key_valid(public_key):
            raise KeysError(f"{where}: public_key is not a valid Ed25519 point")
    else:
        if len(public_key) != P256_KEY_BYTES:
            raise KeysError(f"{where}: public_key must be {P256_KEY_BYTES} bytes")
        if not p256_public_key_valid(public_key):
            raise KeysError(f"{where}: public_key is not a valid P-256 point")

    credential_id: bytes | None = None
    if "credential_id" in item:
        credential_id = _binary(item, "credential_id", where)
        if not CREDENTIAL_ID_MIN_BYTES <= len(credential_id) <= CREDENTIAL_ID_MAX_BYTES:
            raise KeysError(
                f"{where}: credential_id must be {CREDENTIAL_ID_MIN_BYTES} to "
                f"{CREDENTIAL_ID_MAX_BYTES} bytes"
            )

    device = _label(item, "device", where)
    program = _label(item, "program", where)

    scopes = _unique_texts(item, "scopes", where)
    for scope in scopes:
        holders = SCOPE_HOLDERS.get(scope)
        if holders is None:
            raise KeysError(f"{where}: scope {scope!r} is unknown")
        if principal not in holders:
            raise KeysError(f"{where}: scope {scope!r} is not allowed for principal {principal}")
    if principal == "agent" and len(DEVICE_SCOPES.intersection(scopes)) != 1:
        raise KeysError(f"{where}: an agent key holds exactly one of {', '.join(sorted(DEVICE_SCOPES))}")

    subjects: tuple[str, ...] | None = None
    if "subjects" in item:
        subjects = _unique_texts(item, "subjects", where)
        for subject in subjects:
            if not valid_subject(subject):
                raise KeysError(f"{where}: subject {subject!r} is not owner/name")

    return KeyEntry(
        id=key_id,
        kind=kind,
        principal=principal,
        handle=handle,
        team=team,
        public_key=public_key,
        scopes=scopes,
        credential_id=credential_id,
        device=device,
        program=program,
        subjects=subjects,
        expires=_time(item, "expires", where),
        revoked=_time(item, "revoked", where),
    )


def _text(item: dict[str, Any], field: str, where: str) -> str:
    value = item[field]
    if not isinstance(value, str):
        raise KeysError(f"{where}: {field} must be a string")
    return value


def _binary(item: dict[str, Any], field: str, where: str) -> bytes:
    decoded = b64url_decode(_text(item, field, where))
    if decoded is None:
        raise KeysError(f"{where}: {field} is not canonical base64url without padding")
    return decoded


def _label(item: dict[str, Any], field: str, where: str) -> str | None:
    if field not in item:
        return None
    value = _text(item, field, where)
    if not valid_key_id(value):
        raise KeysError(f"{where}: {field} breaks the id rule")
    return value


def _unique_texts(item: dict[str, Any], field: str, where: str) -> tuple[str, ...]:
    value = item[field]
    if not isinstance(value, list) or not value:
        raise KeysError(f"{where}: {field} must be a non-empty list")
    seen: set[str] = set()
    for element in value:
        if not isinstance(element, str):
            raise KeysError(f"{where}: every entry of {field} must be a string")
        if element in seen:
            raise KeysError(f"{where}: {field} lists {element!r} twice")
        seen.add(element)
    return tuple(value)


def _time(item: dict[str, Any], field: str, where: str) -> int | None:
    if field not in item:
        return None
    seconds = parse_time(_text(item, field, where))
    if seconds is None:
        raise KeysError(f"{where}: {field} must be a quoted time YYYY-MM-DDTHH:MM:SSZ")
    return seconds


def _check_unique(entries: list[KeyEntry]) -> None:
    ids: set[str] = set()
    public_keys: set[bytes] = set()
    credential_ids: set[bytes] = set()
    for entry in entries:
        if entry.id in ids:
            raise KeysError(f"keys entry id {entry.id!r} appears twice")
        ids.add(entry.id)
        if entry.public_key in public_keys:
            raise KeysError(f"keys entry {entry.id!r}: public_key appears twice")
        public_keys.add(entry.public_key)
        if entry.credential_id is not None:
            if entry.credential_id in credential_ids:
                raise KeysError(f"keys entry {entry.id!r}: credential_id appears twice")
            credential_ids.add(entry.credential_id)


def _check_invite(entries: list[KeyEntry], now: int) -> None:
    holders = {
        entry.handle
        for entry in entries
        if INVITE_SCOPE in entry.scopes and not entry.revoked_at(now)
    }
    if len(holders) > 1:
        raise KeysError(f"more than one handle holds {INVITE_SCOPE}: {', '.join(sorted(holders))}")


def _check_devices(entries: list[KeyEntry], now: int) -> None:
    groups: dict[tuple[str, str], list[KeyEntry]] = {}
    for entry in entries:
        if entry.principal not in ("agent", "service") or entry.revoked_at(now):
            continue
        if entry.device is None:
            raise KeysError(f"keys entry {entry.id!r} has no device")
        groups.setdefault((entry.handle, entry.device), []).append(entry)
    for (handle, device), group in groups.items():
        if len(group) < 2:
            continue
        programs = [entry.program for entry in group]
        if None in programs:
            raise KeysError(
                f"handle {handle!r} has several keys on device {device!r}; each needs a program"
            )
        if len(set(programs)) != len(programs):
            raise KeysError(
                f"handle {handle!r} has the pair of device {device!r} and one program twice"
            )
