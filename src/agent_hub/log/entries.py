"""Log entries of the membership log (04-crypto.md section 4.7) and the platform log (section 4.8): exact body
formats, signing texts, signatures and entry hashes.

Entry `{"body": {...}, "sig": b64u}` (plus `"inv_sig"` exactly on `accept`). `genesis` and `accept` may carry the
optional member `agent` `{id, pub{kem,sig}, gen, cert}` (ADR-082: the entry also adds the user's agent and is signed
by the agent's key); `agent_add` adds an agent `{subject, user, pub{kem,sig}, gen, cert}`.
`sig = Ed25519_by(P || label || canonical(body))` with label `log\\0` (membership) or `platform-log\\0` (platform).
Entry hash `b64u(SHA-256(canonical(entry)))` over the whole entry including signatures; `prev` of the next entry is
this hash.
After Spike D `keyflow.entry_hash`/`_signed_text`; the body key sets are those of `contracts/common.json`
(`log_body_*`, `platform_body_*`).

Format check order (every implementation): the entry is an object whose `body` is an object with a string `type`
(`header_invalid`); known type (`log_type`); entry key set (`header_invalid`); exact body key set of the type
(`header_invalid`); canonical JSON of the body (`non_ascii`, `header_invalid`); field formats, common fields first
in the order `org, seq, prev, type, by, ts, v`, then the type's fields in the order of the contract (`header_invalid`).
Entry signatures are not format: a malformed or wrong `sig`/`inv_sig` is a rule failure (`log_signature`, ...).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_hub.crypto import fields
from agent_hub.crypto.encoding import P, b64u_encode, canonical, require_int, require_keys
from agent_hub.crypto.errors import CryptoError, fail
from agent_hub.crypto.kdf import LABEL_LOG, LABEL_PLATFORM_LOG
from agent_hub.crypto.keys import RAW_KEY_BYTES, ed25519_sign, ed25519_verify_b64u

VERSION = 1
PLATFORM_ROOT = "platform-root"
"""The `by` of every platform-log entry."""
MAX_EPOCH = 2**31 - 1
INVITE_ID_BYTES = 16
SIGNATURE_BYTES = 64

_ENTRY_KEYS = frozenset({"body", "sig"})
_ACCEPT_ENTRY_KEYS = frozenset({"body", "sig", "inv_sig"})
_PUB_KEYS = frozenset({"kem", "sig"})
_AGENT_KEYS = frozenset({"id", "pub", "gen", "cert"})
MAX_GEN = 2**31 - 1
_CAPACITY_KEYS = frozenset(
    {"vm_memory_budget_mib", "vm_memory_used_mib", "vm_slots_total", "vm_slots_free", "pool_ready"}
)
_VERSION_TEXT_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_PLATFORM_TEXT_RE = re.compile(r"[a-z0-9_]{1,32}")
"""`os` and `arch` of the enrollment body (`contracts/api/device-enroll.request.json`)."""

Check = Callable[[object], object]


def _seq(value: object) -> object:
    return require_int(value, 0)


def _epoch(value: object) -> object:
    return require_int(value, 1, MAX_EPOCH)


def _prev(value: object) -> object:
    return value if value == "" else fields.b64u_bytes(value, 32)


def _key(value: object) -> object:
    return fields.b64u_bytes(value, RAW_KEY_BYTES)


def _sig(value: object) -> object:  # signatures inside a body (device_certify); entry signatures are rules
    return fields.b64u_bytes(value, SIGNATURE_BYTES)


def _invite_id(value: object) -> object:
    return fields.b64u_bytes(value, INVITE_ID_BYTES)


def _pub(value: object) -> object:
    pub = require_keys(value, _PUB_KEYS)
    _key(pub["kem"])
    return _key(pub["sig"])


def _gen(value: object) -> object:
    return require_int(value, 1, MAX_GEN)


def _agent(value: object) -> object:  # the optional `agent` member of `genesis` and `accept` (ADR-082)
    agent = require_keys(value, _AGENT_KEYS)
    fields.uuid(agent["id"])
    _pub(agent["pub"])
    _gen(agent["gen"])
    return _sig(agent["cert"])


def _one_of(*allowed: str) -> Check:
    def check(value: object) -> object:
        if value not in allowed:
            fail("header_invalid")
        return value

    return check


def _bool(value: object) -> object:
    if not isinstance(value, bool):
        fail("header_invalid")
    return value


def _text(pattern: re.Pattern[str]) -> Check:
    def check(value: object) -> object:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            fail("header_invalid")
        return value

    return check


def _capacity(value: object) -> object:
    capacity = require_keys(value, _CAPACITY_KEYS)
    for key in sorted(_CAPACITY_KEYS):
        require_int(capacity[key], 0)
    return capacity


def _sig_or_empty(value: object) -> object:  # identity proofs of `admit` (ADR-060): `""` when not used
    return value if value == "" else _sig(value)


def _wgs(access: Check) -> Check:
    def check(value: object) -> object:
        if not isinstance(value, dict):
            fail("header_invalid")
        for wg, level in value.items():
            fields.uuid(wg)
            access(level)
        return value

    return check


_WG_ACCESS = _one_of("read", "write")

# Membership log: common fields, then the fields of each type (contracts/common.json log_body_*).
_LOG_COMMON: dict[str, Check] = {
    "org": fields.uuid,
    "seq": _seq,
    "prev": _prev,
    "type": fields.kind,
    "by": fields.uuid,
    "ts": fields.time,
    "v": lambda value: fields.literal(value, VERSION),
}
_LOG_TYPES: dict[str, dict[str, Check]] = {
    "genesis": {"subject": fields.uuid, "pub": _pub, "default_wg": fields.uuid, "private": _bool},
    "invite": {},  # user or device form, see _invite_fields
    "invite_revoke": {"invite_id": _invite_id},
    "accept": {"invite_id": _invite_id, "subject": fields.uuid, "kind": _one_of("user", "device"), "pub": _pub},
    "role": {"subject": fields.uuid, "role": _one_of("admin", "member")},
    "wg_create": {"wg": fields.uuid, "epoch": _epoch},
    "wg_archive": {"wg": fields.uuid},
    "grant": {"subject": fields.uuid, "wg": fields.uuid, "access": _WG_ACCESS},
    "ungrant": {"subject": fields.uuid, "wg": fields.uuid},
    "device_assign": {"device": fields.uuid, "wg": fields.uuid},  # optional platform_seq, see _type_fields
    "device_unassign": {"device": fields.uuid, "wg": fields.uuid},
    "device_assign_org": {"device": fields.uuid},  # optional platform_seq, see _type_fields (ADR-075)
    "device_unassign_org": {"device": fields.uuid},
    "remove": {"subject": fields.uuid},
    "rotate": {"scope": _one_of("org", "wg"), "scope_id": fields.uuid, "epoch": _epoch},
    "admit": {
        "subject": fields.uuid,
        "pub": _pub,
        "platform_seq": _seq,
        "id_sig": _sig_or_empty,
        "by_platform_seq": _seq,
        "by_id_sig": _sig_or_empty,
        "role": _one_of("admin", "member"),
        "wgs": _wgs(_WG_ACCESS),
    },
    "agent_add": {"subject": fields.uuid, "user": fields.uuid, "pub": _pub, "gen": _gen, "cert": _sig},
    # the release of a workgroup to the support team (ADR-096)
    "support_grant": {"wg": fields.uuid, "access": _WG_ACCESS},
    "support_revoke": {"wg": fields.uuid},
}
_INVITE_COMMON: dict[str, Check] = {"invite_id": _invite_id, "pk_inv": _key, "kind": _one_of("user", "device")}
_INVITE_USER: dict[str, Check] = {
    **_INVITE_COMMON,
    "role": _one_of("admin", "member"),
    "wgs": _wgs(_WG_ACCESS),
    "expires": fields.time,
}
_INVITE_DEVICE: dict[str, Check] = {**_INVITE_COMMON, "wgs": _wgs(_one_of("device")), "expires": fields.time}

# Platform log (contracts/common.json platform_body_*).
PLATFORM_ROLES = ("inviter", "operator", "support")
"""Platform roles: `inviter` creates platform invitations (ADR-026), `operator` receives the requests of ADR-079 §4,
`support` reads and writes the workgroups released to the support team (ADR-096)."""
PLATFORM_POLICIES = ("open_platform_access", "open_platform_invites")
"""Switches of `platform_policy` (ADR-079 §2): off until an entry turns them on."""
_PLATFORM_ROLE = _one_of(*PLATFORM_ROLES)
_PLATFORM_COMMON: dict[str, Check] = {
    "seq": _seq,
    "prev": _prev,
    "type": fields.kind,
    "by": lambda value: fields.literal(value, PLATFORM_ROOT),
    "ts": fields.time,
    "v": lambda value: fields.literal(value, VERSION),
}
_PLATFORM_TYPES: dict[str, dict[str, Check]] = {
    "root": {"pub": _key},
    "device_invite": {"invite_id": _invite_id, "pk_inv": _key, "expires": fields.time},
    "device_certify": {
        "device": fields.uuid,
        "pub": _pub,
        "invite_id": _invite_id,
        "enroll_sig": _sig,
        "inv_sig": _sig,
        "agent_version": _text(_VERSION_TEXT_RE),
        "os": _text(_PLATFORM_TEXT_RE),
        "arch": _text(_PLATFORM_TEXT_RE),
        "capacity": _capacity,
    },
    "device_decertify": {"device": fields.uuid},
    "role_grant": {"subject": fields.uuid, "role": _PLATFORM_ROLE},
    "role_revoke": {"subject": fields.uuid, "role": _PLATFORM_ROLE},
    "user_invite": {"invite_id": _invite_id, "pk_inv": _key, "expires": fields.time},
    "org_platform_access": {"org": fields.uuid, "granted": _bool},
    "user_certify": {"user": fields.uuid, "pub": _pub},
    "user_decertify": {"user": fields.uuid},
    "platform_policy": {"name": _one_of(*PLATFORM_POLICIES), "on": _bool},  # ADR-079
    "org_operator_subscriptions": {"org": fields.uuid, "granted": _bool},  # ADR-079
}

LOG_TYPES = frozenset(_LOG_TYPES)
PLATFORM_TYPES = frozenset(_PLATFORM_TYPES)


def _type_fields(body: dict[str, object]) -> dict[str, Check]:
    kind = body["type"]
    if kind == "invite":
        return _INVITE_DEVICE if body.get("kind") == "device" else _INVITE_USER
    own = dict(_LOG_TYPES[cast(str, kind)])
    if kind in ("device_assign", "device_assign_org") and "platform_seq" in body:
        own["platform_seq"] = _seq
    if kind in ("genesis", "accept") and "agent" in body:
        own["agent"] = _agent
    return own


def _body_type(
    entry: object, entry_keys: Callable[[str], frozenset[str]], types: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(entry, dict) or not isinstance(entry.get("body"), dict):
        fail("header_invalid")
    body = cast(dict[str, object], entry["body"])
    if not isinstance(body.get("type"), str):
        fail("header_invalid")
    if body["type"] not in types:
        fail("log_type")
    require_keys(entry, entry_keys(cast(str, body["type"])))
    return body


def _check_fields(body: dict[str, object], checks: Mapping[str, Check]) -> dict[str, object]:
    fields.header_dict(body, frozenset(checks))
    for key, check in checks.items():
        check(body[key])
    return body


def check_log_entry(entry: object) -> dict[str, object]:
    """The body of a well-formed membership-log entry (format only; the rules are in `verify`)."""
    body = _body_type(entry, lambda kind: _ACCEPT_ENTRY_KEYS if kind == "accept" else _ENTRY_KEYS, _LOG_TYPES)
    return _check_fields(body, {**_LOG_COMMON, **_type_fields(body)})


def check_platform_entry(entry: object) -> dict[str, object]:
    """The body of a well-formed platform-log entry (format only; the rules are in `platform`)."""
    body = _body_type(entry, lambda _kind: _ENTRY_KEYS, _PLATFORM_TYPES)
    return _check_fields(body, {**_PLATFORM_COMMON, **_PLATFORM_TYPES[cast(str, body["type"])]})


def log_text(body: Mapping[str, object]) -> bytes:
    """`P || "log\\0" || canonical(body)`: signed by `by` (and by `sk_inv` on `accept`)."""
    return P + LABEL_LOG + canonical(dict(body))


def platform_text(body: Mapping[str, object]) -> bytes:
    """`P || "platform-log\\0" || canonical(body)`: signed by the platform root."""
    return P + LABEL_PLATFORM_LOG + canonical(dict(body))


def entry_hash(entry: Mapping[str, object]) -> str:
    """`b64u(SHA-256(canonical(entry)))` over the whole entry including its signatures."""
    return b64u_encode(hashlib.sha256(canonical(dict(entry))).digest())


def sign_log_entry(
    body: Mapping[str, object], author: Ed25519PrivateKey, invite: Ed25519PrivateKey | None = None
) -> dict[str, object]:
    """`{"body", "sig"}`, plus `inv_sig` by `sk_inv` when `invite` is given (the `accept` entry)."""
    text = log_text(body)
    entry: dict[str, object] = {"body": dict(body), "sig": b64u_encode(ed25519_sign(author, text))}
    if invite is not None:
        entry["inv_sig"] = b64u_encode(ed25519_sign(invite, text))
    return entry


def sign_platform_entry(body: Mapping[str, object], root: Ed25519PrivateKey) -> dict[str, object]:
    """`{"body", "sig"}` signed by the platform root."""
    return {"body": dict(body), "sig": b64u_encode(ed25519_sign(root, platform_text(body)))}


def signature_valid(public_key: bytes, signature: object, text: bytes) -> bool:
    """True if `signature` (b64u text) is a valid Ed25519 signature of `text` under `public_key`."""
    try:
        ed25519_verify_b64u(public_key, signature, text)
    except CryptoError:
        return False
    return True


@dataclass(frozen=True)
class Head:
    """`(seq, hash)` of the last entry of a log (`hash` = `entry_hash`)."""

    seq: int
    hash: str

    def to_json(self) -> dict[str, object]:
        """`{"seq", "hash"}` as in the API (`log_head`) and the invitation package."""
        return {"seq": self.seq, "hash": self.hash}


def check_extends(stored: Head | None, entries: Sequence[Mapping[str, object]]) -> None:
    """L9: a log must extend the head the client verified before (`log_rollback` otherwise).

    `entries` is the complete log from `seq` 0; it extends `stored` if it is at least as long and its entry at
    `stored.seq` has the stored hash. Without a stored head every log extends.
    """
    if stored is not None and (len(entries) <= stored.seq or entry_hash(entries[stored.seq]) != stored.hash):
        fail("log_rollback")
