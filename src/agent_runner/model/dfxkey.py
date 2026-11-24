"""DFX model access on own hardware: the envelope of the limited per-device OpenRouter key and the org device's side
(ADR-081 section 7, idea 46 case 3; ADR-062 section 1; ADR-026).

**Envelope** (built by the issuing platform device with `seal_envelope`, treated by the hub as an opaque string):
`base64url(JSON {"v": 1, "header": H, "ct": b64u(enc || ct), "sig": b64u(sig)})` with the header
`H = {request_id, org_id, device_id, device_kem, key_ref, issued_at, issuer_device_id}`, `ct` = HPKE single shot
(`crypto.hpke`, RFC 9180 base mode) of the key to `device_kem` with `info = canonical(H)` and empty AAD, and
`sig = Ed25519_issuer(canonical(H) || ct)` by the issuer's device signing key. The whole text is at most
`MAX_ENVELOPE` characters.

**Org device** (`accept`, reconcile step `dfx_keys`): the device state lists `dfx_key_held: [{org_id, key_ref,
envelope, issuer_device_id}]`, one per organization of the device. An envelope is accepted only when

1. it is well formed (exact key sets, formats, version, size);
2. its header names **this device** and **its own KEM key** (a key sealed to another key is useless here; a key
   sealed for another device is not this device's key);
3. the organization of the header is the listed one and one this device serves (`OrgBook.acting`): a hub-made key
   could otherwise route an organization's prompts to someone else's provider account;
4. the key reference and the issuer of the header are the listed ones;
5. the issuer is a platform device **certified now** in the verified platform log and the signature verifies under
   the signing key of that certification (an unsigned, a hub-signed or an org-device-signed envelope is refused);
6. it opens with the device's KEM key and holds one printable token.

An accepted envelope is stored **sealed** in `runner.dfx_key` (one row per organization) and mirrored in memory; an
organization that is no longer listed loses its row (revoked, or the device left it). A refused envelope never
replaces a stored one; the refusal is logged once with a closed reason (`REASONS`), never with the envelope.

**Use** (`org_key(context, org_id)`): checks 2-6 again against the current logs (an issuer decertified meanwhile, an
organization the device left: None) and returns the plaintext key for one provisioning. The plaintext is never
stored, logged or put into an error; it lives in memory for the provisioning only (accepted risk of ADR-081 section 7:
it is in the guest's gateway during a session, bounded by the key's `limit`).

`platform_key(context, org_id)` is the `SessionEnv.platform_key` of the daemon: the platform key file on a platform
device (`provision.read_platform_key`), the opened DFX key of the session's organization on an org device, None
otherwise. The management key and the platform key never reach an org device: the first stays on the issuer, the
second is refused on org devices by the configuration (`OPENROUTER_KEY_FILE`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import AsyncConnection

from agent_runner.crypto import hpke
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, canonical, is_b64u, parse_json, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.keys import ED25519_SIGNATURE_BYTES, RAW_KEY_BYTES, ed25519_sign, ed25519_verify
from agent_runner.crypto.log.platform import CERTIFIED
from agent_runner.identity import DeviceKeys
from agent_runner.model.provision import read_platform_key
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.orgs.db import session

VERSION = 1
MAX_ENVELOPE = 8192
MAX_KEY_BYTES = 4096
HEADER_KEYS = frozenset({"request_id", "org_id", "device_id", "device_kem", "key_ref", "issued_at", "issuer_device_id"})
ENVELOPE_KEYS = frozenset({"v", "header", "ct", "sig"})
HELD_KEYS = frozenset({"org_id", "key_ref", "envelope", "issuer_device_id"})
KEY_REF_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")
_UUID_FIELDS = ("request_id", "org_id", "device_id", "issuer_device_id")
REASONS = (
    "malformed",
    "other_device",
    "other_kem",
    "other_org",
    "org_not_served",
    "key_ref_mismatch",
    "issuer_mismatch",
    "issuer_not_platform",
    "signature",
    "decrypt",
    "key_invalid",
)
"""The closed refusal reasons of an envelope (logged, never the envelope)."""
ATTRIBUTE = "dfx_keys_held"


class EnvelopeRefused(Exception):
    """An envelope this device does not accept; `reason` is one of `REASONS` (the only text of the error)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- the envelope ----------------------------------------------------------------------------------------------------


def check_header(value: object) -> dict[str, str]:
    """The header with the exact key set and formats, else `EnvelopeRefused("malformed")`."""
    if not isinstance(value, dict) or set(value) != HEADER_KEYS or not all(isinstance(v, str) for v in value.values()):
        raise EnvelopeRefused("malformed")
    header = cast(dict[str, str], value)
    try:
        b64u_decode(header["device_kem"], RAW_KEY_BYTES)
        parse_time(header["issued_at"])
    except CryptoError:
        raise EnvelopeRefused("malformed") from None
    if not all(UUID_RE.fullmatch(header[name]) for name in _UUID_FIELDS) or not KEY_REF_RE.fullmatch(header["key_ref"]):
        raise EnvelopeRefused("malformed")
    return header


def signature_text(header: Mapping[str, str], ct: bytes) -> bytes:
    """`canonical(header) || ct`, the text the issuer signs."""
    return canonical(dict(header)) + ct


def seal_envelope(header: Mapping[str, str], key: str, signer: Ed25519PrivateKey) -> str:
    """The envelope of `key` for the device and KEM key named in `header`, signed by `signer` (the issuer)."""
    checked = check_header(dict(header))
    if not _TOKEN_RE.fullmatch(key):
        raise EnvelopeRefused("key_invalid")
    info = canonical(checked)
    ct = hpke.seal(b64u_decode(checked["device_kem"], RAW_KEY_BYTES), info, key.encode("ascii"))
    document = {
        "v": VERSION,
        "header": checked,
        "ct": b64u_encode(ct),
        "sig": b64u_encode(ed25519_sign(signer, info + ct)),
    }
    return b64u_encode(canonical(document))


@dataclass(frozen=True)
class Parsed:
    """A well-formed envelope: header, ciphertext and signature bytes."""

    header: dict[str, str]
    ct: bytes
    sig: bytes


def parse_envelope(text: object) -> Parsed:
    """Structure, version, formats and size (check 1), else `EnvelopeRefused("malformed")`."""
    if not isinstance(text, str) or not 0 < len(text) <= MAX_ENVELOPE or not is_b64u(text):
        raise EnvelopeRefused("malformed")
    try:
        document = parse_json(b64u_decode(text))
    except CryptoError:
        raise EnvelopeRefused("malformed") from None
    if not isinstance(document, dict) or set(document) != ENVELOPE_KEYS:
        raise EnvelopeRefused("malformed")
    if type(document["v"]) is not int or document["v"] != VERSION:
        raise EnvelopeRefused("malformed")
    header = check_header(document["header"])
    try:
        ct = b64u_decode(document["ct"])
        sig = b64u_decode(document["sig"], ED25519_SIGNATURE_BYTES)
    except CryptoError:
        raise EnvelopeRefused("malformed") from None
    return Parsed(header, ct, sig)


@dataclass(frozen=True)
class Holder:
    """What the receiving org device checks an envelope against."""

    book: OrgBook
    device_id: str
    keys: DeviceKeys


def verify_envelope(holder: Holder, text: object, *, org_id: str, key_ref: str, issuer: str) -> Parsed:
    """Checks 1-5 of the module docstring for the listed `(org_id, key_ref, issuer)`; returns the parsed envelope."""
    parsed = parse_envelope(text)
    header = parsed.header
    if header["device_id"] != holder.device_id:
        raise EnvelopeRefused("other_device")
    if header["device_kem"] != holder.keys.kem_public:
        raise EnvelopeRefused("other_kem")
    if header["org_id"] != org_id:
        raise EnvelopeRefused("other_org")
    if not holder.book.acting(org_id):
        raise EnvelopeRefused("org_not_served")
    if header["key_ref"] != key_ref:
        raise EnvelopeRefused("key_ref_mismatch")
    if header["issuer_device_id"] != issuer:
        raise EnvelopeRefused("issuer_mismatch")
    state = holder.book.platform_state
    if state is None or state.status(issuer) != CERTIFIED:
        raise EnvelopeRefused("issuer_not_platform")
    certification = state.certifications[state.devices[issuer]]
    try:
        ed25519_verify(b64u_decode(certification.sig), parsed.sig, signature_text(header, parsed.ct))
    except CryptoError:
        raise EnvelopeRefused("signature") from None
    return parsed


def open_envelope(holder: Holder, text: object, *, org_id: str, key_ref: str, issuer: str) -> str:
    """`verify_envelope`, then the key opened with the device's KEM key (check 6)."""
    parsed = verify_envelope(holder, text, org_id=org_id, key_ref=key_ref, issuer=issuer)
    try:
        plain = hpke.open_(holder.keys.kem_key(), canonical(parsed.header), parsed.ct)
    except CryptoError:
        raise EnvelopeRefused("decrypt") from None
    key = plain.decode("ascii", errors="replace")
    if len(plain) > MAX_KEY_BYTES or not _TOKEN_RE.fullmatch(key):
        raise EnvelopeRefused("key_invalid")
    return key


# --- the org device: store and use ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """One accepted envelope (a row of `runner.dfx_key`)."""

    org_id: str
    key_ref: str
    issuer_device_id: str
    envelope: str

    def __repr__(self) -> str:
        return f"Held(org_id={self.org_id!r}, key_ref={self.key_ref!r}, envelope=<sealed>)"


@dataclass
class HeldKeys:
    """The in-memory mirror of `runner.dfx_key` (loaded once per daemon, kept in step with every commit)."""

    rows: dict[str, Held] = field(default_factory=dict)
    loaded: bool = False


def held_keys(context: Any) -> HeldKeys:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, HeldKeys):
        return found
    created = HeldKeys()
    setattr(context, ATTRIBUTE, created)
    return created


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.dfxkey {text}\n")
    context.log.flush()


def holder_of(context: Any) -> Holder:
    identity = context.identity
    return Holder(book=book_for(context), device_id=identity.device.device_id, keys=identity.keys)


async def _load(conn: AsyncConnection[Any], held: HeldKeys) -> None:
    if held.loaded:
        return
    cursor = await conn.execute(
        "SELECT org_id::text, key_ref, issuer_device_id::text, envelope FROM runner.dfx_key ORDER BY org_id"
    )
    held.rows = {
        str(row[0]): Held(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in await cursor.fetchall()
    }
    held.loaded = True


def _listed(desired: Mapping[str, Any]) -> list[Held]:
    """The well-formed entries of `dfx_key_held` (an older hub sends none: nothing is held)."""
    found = desired.get("dfx_key_held")
    entries = found if isinstance(found, list) else []
    out: list[Held] = []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and set(entry) == HELD_KEYS
            and all(isinstance(entry[name], str) for name in HELD_KEYS)
            and UUID_RE.fullmatch(entry["org_id"])
        ):
            out.append(Held(entry["org_id"], entry["key_ref"], entry["issuer_device_id"], entry["envelope"]))
    return out


async def accept(context: Any, desired: Mapping[str, Any]) -> None:
    """Verify and store the envelopes of `dfx_key_held` (org devices; module docstring)."""
    held = held_keys(context)
    holder = holder_of(context)
    listed = {entry.org_id: entry for entry in _listed(desired)}
    async with session(context.pool) as conn:
        await _load(conn, held)
        for org_id in sorted(set(held.rows) - set(listed)):
            async with conn.transaction():
                await conn.execute("DELETE FROM runner.dfx_key WHERE org_id = %s", (org_id,))
            del held.rows[org_id]
            _line(context, f"dropped org_id={org_id}")
        for org_id, entry in sorted(listed.items()):
            if held.rows.get(org_id) == entry:
                continue
            try:
                open_envelope(
                    holder, entry.envelope, org_id=org_id, key_ref=entry.key_ref, issuer=entry.issuer_device_id
                )
            except EnvelopeRefused as exc:
                if holder.book.remember_refusal(("dfx_key", org_id, entry.envelope)):
                    _line(context, f"refused org_id={org_id} reason={exc.reason}")
                continue
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO runner.dfx_key (org_id, key_ref, issuer_device_id, envelope) VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (org_id) DO UPDATE SET key_ref = EXCLUDED.key_ref,"
                    " issuer_device_id = EXCLUDED.issuer_device_id, envelope = EXCLUDED.envelope, accepted_at = now()",
                    (org_id, entry.key_ref, entry.issuer_device_id, entry.envelope),
                )
            held.rows[org_id] = entry
            _line(context, f"accepted org_id={org_id} key_ref={entry.key_ref}")


def org_key(context: Any, org_id: str) -> str | None:
    """The plaintext DFX key of `org_id` for one provisioning, None without a usable envelope (module docstring)."""
    entry = held_keys(context).rows.get(org_id)
    if entry is None:
        return None
    try:
        return open_envelope(
            holder_of(context), entry.envelope, org_id=org_id, key_ref=entry.key_ref, issuer=entry.issuer_device_id
        )
    except EnvelopeRefused as exc:
        _line(context, f"unusable org_id={org_id} reason={exc.reason}")
        return None


def platform_key(context: Any, org_id: str | None = None) -> str | None:
    """The key of the `platform` access for a session of `org_id` on this device (module docstring)."""
    if context.identity.device.kind == "platform":
        return read_platform_key(context.config.openrouter_key_file)
    return None if org_id is None else org_key(context, org_id)
