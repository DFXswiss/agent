"""Copied from src/agent_hub/crypto/agent.py.

Delegated agent access (11-agent-access.md section 3, ADR-056).

- `fingerprint(kem_pub, sig_pub)`: `b64u(SHA-256(P || "agent-key\\0" || kem_pub || sig_pub)[:16])`, carried in the
  fragment of the pairing link so that the browser can check the keys the hub returns.
- `pop_text`/`sign_pop`/`verify_pop`: the agent's proof of possession of its Ed25519 key,
  `P || "agent-pair\\0" || canonical({"kem", "pairing_id", "sig"})`.
- The handover secret `r` (32 random bytes, drawn by the confirming browser, given to the agent only over the
  loopback) yields the bundle key `k = HKDF(r, P || "agent-bundle-key\\0" || canonical({"agent", "user"}))` and the
  claim `HKDF(r, P || "agent-claim\\0" || canonical({"agent", "user"}))`; the hub stores only
  `claim_hash = SHA-256(P || "agent-claim-hash\\0" || claim)`.
- `seal_identity`/`open_identity`: the user's identity bundle plaintext (04-crypto.md section 4.1), first encrypted
  under `k` (`nonce || AES-GCM(k, plaintext, AAD = info)`), then sealed with HPKE to the agent's X25519 key,
  `info = P || "agent-bundle\\0" || canonical({"agent", "user"})`. Opening needs the agent's private key **and** `r`:
  a hub that swapped the agent's key, or that put its own key into a link, still lacks `r`; and a hub cannot make the
  agent accept an identity of its own choosing.
- `seal_label`/`open_label`: the label `{"host", "name"}` (body JSON) sealed with HPKE to the user's identity X25519
  key, `info = P || "agent-label\\0" || canonical({"agent", "user"})`.
- `check_message`: the personal message of a request for access (rule E-28, KOR-64): 1-500 characters under the
  character rules of the label; it travels where the request's other texts travel (the pairing link, the sealed
  invitation package) and is shown only as plain text.

Pairing from another computer (ADR-067, 11-agent-access.md sections 3 and 5.4):

- `agent_code`: the code the agent shows and the user types on the page,
  `crockford32(SHA-256(P || "agent-code\\0" || canonical({"fp", "mode": "remote", "nonce", "origin",
  "pairing_id"})))[:10]` with the hub's 16-byte nonce of the pairing (b64u in the input). It binds the confirmed
  pairing to the one the user's own agent shows; it is not a secret.
- `seal_handover`/`open_handover`: `r` and the platform root key `k` of the web build, sealed by the confirming
  browser to the agent's X25519 key, `info = P || "agent-handover\\0" || canonical({"agent", "user"})`, plaintext
  `canonical({"k", "r"})`; the hub only relays it.
- `return_code`: `crockford32(HKDF(r, P || "agent-return\\0" || canonical({"agent", "user"})))[:8]`, shown by the
  page after the confirmation and given by the user to the agent, which accepts the handover only when it matches:
  HPKE base mode lets anyone seal to the agent, and only the browser knows `r`.
- `parse_code`: the typed form of either code (lower case, spaces and dashes allowed, `I`/`L` read as `1`, `O` as
  `0`); `format_code` shows it in two groups (`XXXXX-XXXXX`, `XXXX-XXXX`).

An account of an agent alone (rule E-27, KOR-62, 11-agent-access.md sections 3 and 5.5): the agent tool creates the
user's identity itself, with no passkey, and proves possession with signatures instead of WebAuthn. The proof names
its method (`method`, today only `PROOF_METHODS` = `agent-signature-v1`):

- `account_text`: `P || "agent-account\0" || canonical({"agent", "agent_pub": {kem, sig}, "invite_id", "method",
  "pub": {kem, sig}, "user"})`, signed by the invitation key `sk_inv` (the holder of the code), the new identity and
  the new agent key (`sign_text`, `verify_text`); the hub checks all three before it creates the account.
- `recover_text`: `P || "agent-recover\0" || canonical({"agent", "agent_pub": {kem, sig}, "challenge", "method",
  "user"})` with the 32-byte challenge of `recovery/begin`, signed by the identity opened with the recovery code and
  by the new agent key; it binds the new main orchestrator to that recovery.

The agent's own identity (ADR-082 sections 1, 2 and 9.1): the agent is a sub-principal of its user.

- `cert_text`/`sign_cert`/`verify_cert`: the delegation certificate, the user's Ed25519 signature over
  `P || "agent-cert\0" || canonical({"agent", "gen", "pub": {kem, sig}, "user", "v": 1})` with the generation
  `gen` >= 1 the hub counts per user. Every verifier checks it under the user's signing key from its verified log.
- `seal_user`/`open_user`: the agent's anchor, the user's **public** identity keys and the certificate sealed like the
  identity (`k` from `r`, AES-GCM inner with AAD = info, HPKE outer), `info = P || "agent-user\0" ||
  canonical({"agent", "user"})`, plaintext exactly `canonical({"cert", "kem", "sig"})` (`header_invalid` otherwise).

The hub uses only `verify_pop`, `verify_text`, `verify_cert`, `claim_hash` and the format checks; sealing and opening
belong to the browser and the agent tool (this module is the reference for the vectors).
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_runner.crypto import aead, fields, hpke
from agent_runner.crypto.bundle import bundle_plaintext, parse_bundle_plaintext
from agent_runner.crypto.encoding import (
    P,
    b64u_decode,
    b64u_encode,
    body_json,
    canonical,
    parse_json,
    require_bytes,
    require_int,
    require_keys,
)
from agent_runner.crypto.errors import CryptoError, fail
from agent_runner.crypto.kdf import hkdf
from agent_runner.crypto.keys import RAW_KEY_BYTES, Identity, ed25519_sign, ed25519_verify_b64u

LABEL_AGENT_KEY = b"agent-key\x00"
LABEL_AGENT_PAIR = b"agent-pair\x00"
LABEL_AGENT_BUNDLE = b"agent-bundle\x00"
LABEL_AGENT_BUNDLE_KEY = b"agent-bundle-key\x00"
LABEL_AGENT_LABEL = b"agent-label\x00"
LABEL_AGENT_CLAIM = b"agent-claim\x00"
LABEL_AGENT_CLAIM_HASH = b"agent-claim-hash\x00"
LABEL_AGENT_CODE = b"agent-code\x00"
LABEL_AGENT_HANDOVER = b"agent-handover\x00"
LABEL_AGENT_RETURN = b"agent-return\x00"
LABEL_AGENT_ACCOUNT = b"agent-account\x00"
LABEL_AGENT_RECOVER = b"agent-recover\x00"
LABEL_AGENT_CERT = b"agent-cert\x00"
LABEL_AGENT_USER = b"agent-user\x00"
CERT_VERSION = 1
MAX_GEN = 2**31 - 1
SIGNATURE_BYTES = 64
_PUB_KEYS = frozenset({"kem", "sig"})
_USER_KEYS = frozenset({"cert", "kem", "sig"})
METHOD_AGENT_SIGNATURE = "agent-signature-v1"
PROOF_METHODS = (METHOD_AGENT_SIGNATURE,)
CHALLENGE_BYTES = 32
MODE_REMOTE = "remote"
NONCE_BYTES = 16
AGENT_CODE_LENGTH = 10
RETURN_CODE_LENGTH = 8
CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_ALIASES = str.maketrans({"I": "1", "L": "1", "O": "0"})
_HANDOVER_KEYS = frozenset({"k", "r"})
FINGERPRINT_BYTES = 16
SECRET_BYTES = 32
LABEL_TEXT_MAX = 64
MESSAGE_TEXT_MAX = 500
_LABEL_KEYS = frozenset({"host", "name"})
# Unicode categories a label must not contain: controls, format characters (bidi overrides, zero-width), line and
# paragraph separators, surrogates, private use and unassigned code points.
_LABEL_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def fingerprint(kem_pub: bytes, sig_pub: bytes) -> str:
    """The 22-character fingerprint of an agent's public keys."""
    require_bytes(kem_pub, RAW_KEY_BYTES)
    require_bytes(sig_pub, RAW_KEY_BYTES)
    digest = hashlib.sha256(P + LABEL_AGENT_KEY + kem_pub + sig_pub).digest()
    return b64u_encode(digest[:FINGERPRINT_BYTES])


def pop_text(pairing_id: str, kem_pub: bytes, sig_pub: bytes) -> bytes:
    """The text of the proof of possession."""
    require_bytes(kem_pub, RAW_KEY_BYTES)
    require_bytes(sig_pub, RAW_KEY_BYTES)
    fields = {"kem": b64u_encode(kem_pub), "pairing_id": pairing_id, "sig": b64u_encode(sig_pub)}
    return P + LABEL_AGENT_PAIR + canonical(fields)


def sign_pop(agent: Identity, pairing_id: str) -> str:
    """The agent's b64u Ed25519 signature over `pop_text`."""
    return b64u_encode(ed25519_sign(agent.sig, pop_text(pairing_id, agent.kem_public(), agent.sig_public())))


def verify_pop(pairing_id: str, kem_pub: bytes, sig_pub: bytes, pop: object) -> None:
    """`signature_invalid` unless `pop` is the signature of `sig_pub` over `pop_text`."""
    ed25519_verify_b64u(sig_pub, pop, pop_text(pairing_id, kem_pub, sig_pub))


def _ids(agent_id: str, user_id: str) -> bytes:
    return canonical({"agent": agent_id, "user": user_id})


def identity_info(agent_id: str, user_id: str) -> bytes:
    """The HPKE `info` (and inner AAD) of the identity sealed to the agent."""
    return P + LABEL_AGENT_BUNDLE + _ids(agent_id, user_id)


def label_info(agent_id: str, user_id: str) -> bytes:
    """The HPKE `info` of the label sealed to the user."""
    return P + LABEL_AGENT_LABEL + _ids(agent_id, user_id)


def bundle_key(secret: bytes, agent_id: str, user_id: str) -> bytes:
    """`k = HKDF(r, P || "agent-bundle-key\\0" || canonical({"agent", "user"}))`."""
    require_bytes(secret, SECRET_BYTES)
    return hkdf(secret, P + LABEL_AGENT_BUNDLE_KEY + _ids(agent_id, user_id))


def claim(secret: bytes, agent_id: str, user_id: str) -> bytes:
    """The claim the agent sends: `HKDF(r, P || "agent-claim\\0" || canonical({"agent", "user"}))`."""
    require_bytes(secret, SECRET_BYTES)
    return hkdf(secret, P + LABEL_AGENT_CLAIM + _ids(agent_id, user_id))


def claim_hash(claim_raw: bytes) -> bytes:
    """`SHA-256(P || "agent-claim-hash\\0" || claim)` of a 32-byte claim (what the hub stores)."""
    require_bytes(claim_raw, SECRET_BYTES)
    return hashlib.sha256(P + LABEL_AGENT_CLAIM_HASH + claim_raw).digest()


def seal_identity(
    agent_kem_pub: bytes,
    secret: bytes,
    agent_id: str,
    user_id: str,
    identity: Identity,
    *,
    nonce: bytes | None = None,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """The user's identity under `k` and sealed to the agent (`nonce`, `ephemeral` only for test vectors)."""
    info = identity_info(agent_id, user_id)
    inner = aead.seal_blob(bundle_key(secret, agent_id, user_id), bundle_plaintext(identity), info, nonce)
    return b64u_encode(hpke.seal(agent_kem_pub, info, inner, ephemeral))


def open_identity(agent_kem: X25519PrivateKey, secret: bytes, agent_id: str, user_id: str, sealed: str) -> Identity:
    """The inverse of `seal_identity` (`hpke_short`, `decrypt_failed`, `header_invalid`)."""
    info = identity_info(agent_id, user_id)
    inner = hpke.open_(agent_kem, info, b64u_decode(sealed))
    return parse_bundle_plaintext(aead.open_blob(bundle_key(secret, agent_id, user_id), inner, info))


def _label_text_valid(text: object, maximum: int = LABEL_TEXT_MAX) -> bool:
    return (
        isinstance(text, str)
        and 1 <= len(text) <= maximum
        and text == text.strip()
        and not any(unicodedata.category(char) in _LABEL_REFUSED_CATEGORIES for char in text)
    )


def check_label(value: object) -> dict[str, str]:
    """Exactly `{"host", "name"}`, each 1-64 characters, no leading or trailing space, no control, format, separator,
    private-use or unassigned characters (`header_invalid`)."""
    label = require_keys(value, _LABEL_KEYS)
    if not all(_label_text_valid(label[key]) for key in _LABEL_KEYS):
        fail("header_invalid")
    return {"host": str(label["host"]), "name": str(label["name"])}


def check_message(value: object) -> str:
    """The personal message of a request: 1-500 characters under the label rules (`header_invalid`)."""
    if not _label_text_valid(value, MESSAGE_TEXT_MAX):
        fail("header_invalid")
    return str(value)


def seal_label(
    user_kem_pub: bytes,
    agent_id: str,
    user_id: str,
    label: object,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """The label sealed to the user's identity (`ephemeral` only for test vectors)."""
    info = label_info(agent_id, user_id)
    return b64u_encode(hpke.seal(user_kem_pub, info, body_json(check_label(label)), ephemeral))


def open_label(user_kem: X25519PrivateKey, agent_id: str, user_id: str, sealed: str) -> dict[str, str]:
    """The inverse of `seal_label`."""
    plaintext = hpke.open_(user_kem, label_info(agent_id, user_id), b64u_decode(sealed))
    label = check_label(parse_json(plaintext))
    if body_json(label) != plaintext:
        fail("header_invalid")
    return label


def crockford32(raw: bytes) -> str:
    """Crockford base32 of `raw` (big-endian bit string, 5 bits per character, the last one padded with zeros)."""
    bits = "".join(f"{byte:08b}" for byte in raw)
    return "".join(CROCKFORD32[int(bits[at : at + 5].ljust(5, "0"), 2)] for at in range(0, len(bits), 5))


def code_input(pairing_id: str, fp: str, nonce: bytes, origin: str) -> bytes:
    """The SHA-256 input of the agent code."""
    require_bytes(nonce, NONCE_BYTES)
    fields = {"fp": fp, "mode": MODE_REMOTE, "nonce": b64u_encode(nonce), "origin": origin, "pairing_id": pairing_id}
    return P + LABEL_AGENT_CODE + canonical(fields)


def agent_code(pairing_id: str, fp: str, nonce: bytes, origin: str) -> str:
    """The 10-character agent code of a remote pairing (the user types it on the page)."""
    return crockford32(hashlib.sha256(code_input(pairing_id, fp, nonce, origin)).digest())[:AGENT_CODE_LENGTH]


def return_info(agent_id: str, user_id: str) -> bytes:
    """The HKDF `info` of the return code."""
    return P + LABEL_AGENT_RETURN + _ids(agent_id, user_id)


def return_code(secret: bytes, agent_id: str, user_id: str) -> str:
    """The 8-character return code the page shows after the confirmation (the user gives it to the agent)."""
    require_bytes(secret, SECRET_BYTES)
    return crockford32(hkdf(secret, return_info(agent_id, user_id)))[:RETURN_CODE_LENGTH]


def parse_code(text: object, length: int) -> str:
    """The canonical code of a typed text: upper case, spaces and dashes removed, `I`/`L` → `1`, `O` → `0`; exactly
    `length` characters of the Crockford alphabet, else `header_invalid`."""
    if not isinstance(text, str):
        fail("header_invalid")
    code = text.upper().replace(" ", "").replace("-", "").translate(_CODE_ALIASES)
    if len(code) != length or any(char not in CROCKFORD32 for char in code):
        fail("header_invalid")
    return code


def format_code(code: str) -> str:
    """Two groups separated by a dash (`XXXXX-XXXXX`, `XXXX-XXXX`)."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


def return_code_matches(secret: bytes, agent_id: str, user_id: str, typed: object) -> bool:
    """Whether the code the user typed is the return code of `r` (constant-time comparison)."""
    try:
        code = parse_code(typed, RETURN_CODE_LENGTH)
    except CryptoError:
        return False
    return hmac.compare_digest(code.encode(), return_code(secret, agent_id, user_id).encode())


def handover_info(agent_id: str, user_id: str) -> bytes:
    """The HPKE `info` of the handover sealed to the agent."""
    return P + LABEL_AGENT_HANDOVER + _ids(agent_id, user_id)


def handover_plaintext(secret: bytes, root_key: str) -> bytes:
    """`canonical({"k", "r"})`: the plaintext of `handover_ct`."""
    require_bytes(secret, SECRET_BYTES)
    return canonical({"k": b64u_encode(b64u_decode(root_key, RAW_KEY_BYTES)), "r": b64u_encode(secret)})


def seal_handover(
    agent_kem_pub: bytes,
    agent_id: str,
    user_id: str,
    secret: bytes,
    root_key: str,
    *,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """`handover_ct`: `r` and the platform root key sealed to the agent (`ephemeral` only for test vectors)."""
    plaintext = handover_plaintext(secret, root_key)
    return b64u_encode(hpke.seal(agent_kem_pub, handover_info(agent_id, user_id), plaintext, ephemeral))


def open_handover(agent_kem: X25519PrivateKey, agent_id: str, user_id: str, sealed: str) -> tuple[bytes, str]:
    """The inverse of `seal_handover`: `(r, platform root key)`; the plaintext must be exactly canonical."""
    plaintext = hpke.open_(agent_kem, handover_info(agent_id, user_id), b64u_decode(sealed))
    value = require_keys(parse_json(plaintext), _HANDOVER_KEYS)
    secret = b64u_decode(value["r"], SECRET_BYTES)
    root_key = str(value["k"])
    if handover_plaintext(secret, root_key) != plaintext:
        fail("header_invalid")
    return secret, root_key


def new_agent() -> Identity:
    """Fresh agent key pairs (X25519 and Ed25519)."""
    return Identity(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())


def _public(kem_pub: bytes, sig_pub: bytes) -> dict[str, str]:
    require_bytes(kem_pub, RAW_KEY_BYTES)
    require_bytes(sig_pub, RAW_KEY_BYTES)
    return {"kem": b64u_encode(kem_pub), "sig": b64u_encode(sig_pub)}


def check_method(method: object) -> str:
    """A proof method of `PROOF_METHODS` (`header_invalid` otherwise)."""
    if method not in PROOF_METHODS:
        fail("header_invalid")
    return str(method)


def account_text(
    *,
    agent_id: str,
    agent_kem: bytes,
    agent_sig: bytes,
    invite_id: str,
    method: str,
    user_id: str,
    user_kem: bytes,
    user_sig: bytes,
) -> bytes:
    """The text the invitation key, the new identity and the new agent key sign for an account of an agent alone."""
    fields = {
        "agent": agent_id,
        "agent_pub": _public(agent_kem, agent_sig),
        "invite_id": invite_id,
        "method": check_method(method),
        "pub": _public(user_kem, user_sig),
        "user": user_id,
    }
    return P + LABEL_AGENT_ACCOUNT + canonical(fields)


def recover_text(
    *, agent_id: str, agent_kem: bytes, agent_sig: bytes, challenge: bytes, method: str, user_id: str
) -> bytes:
    """The text the recovered identity and the new agent key sign to make the new agent the main orchestrator."""
    require_bytes(challenge, CHALLENGE_BYTES)
    fields = {
        "agent": agent_id,
        "agent_pub": _public(agent_kem, agent_sig),
        "challenge": b64u_encode(challenge),
        "method": check_method(method),
        "user": user_id,
    }
    return P + LABEL_AGENT_RECOVER + canonical(fields)


def sign_text(key: Ed25519PrivateKey, text: bytes) -> str:
    """The b64u Ed25519 signature of `key` over `text`."""
    return b64u_encode(ed25519_sign(key, text))


def verify_text(sig_pub: bytes, signature: object, text: bytes) -> None:
    """`signature_invalid` unless `signature` is the signature of `sig_pub` over `text`."""
    ed25519_verify_b64u(sig_pub, signature, text)


def public_keys(pub: object) -> dict[str, str]:
    """Exactly `{"kem", "sig"}`, each the b64u text of a 32-byte key (`header_invalid` otherwise)."""
    value = require_keys(pub, _PUB_KEYS)
    return _public(b64u_decode(value["kem"], RAW_KEY_BYTES), b64u_decode(value["sig"], RAW_KEY_BYTES))


def cert_text(agent_id: str, pub: object, user_id: str, gen: object) -> bytes:
    """The text of the delegation certificate (`header_invalid` for malformed IDs, keys or generation)."""
    certified = {
        "agent": fields.uuid(agent_id),
        "gen": require_int(gen, 1, MAX_GEN),
        "pub": public_keys(pub),
        "user": fields.uuid(user_id),
        "v": CERT_VERSION,
    }
    return P + LABEL_AGENT_CERT + canonical(certified)


def sign_cert(user_sig: Ed25519PrivateKey, agent_id: str, pub: object, user_id: str, gen: object) -> str:
    """The user's b64u signature over `cert_text` (made by the user's client at the confirmation)."""
    return b64u_encode(ed25519_sign(user_sig, cert_text(agent_id, pub, user_id, gen)))


def verify_cert(user_sig_pub: bytes, cert: object, *, agent_id: str, pub: object, user_id: str, gen: object) -> None:
    """`signature_invalid` unless `cert` is the user's signature over `cert_text`."""
    ed25519_verify_b64u(user_sig_pub, cert, cert_text(agent_id, pub, user_id, gen))


def user_info(agent_id: str, user_id: str) -> bytes:
    """The HPKE `info` (and inner AAD) of the user's anchor sealed to the agent."""
    return P + LABEL_AGENT_USER + _ids(agent_id, user_id)


def user_plaintext(user_pub: object, cert: object) -> bytes:
    """`canonical({"cert", "kem", "sig"})`: the plaintext of `user_ct`."""
    return canonical({"cert": b64u_encode(b64u_decode(cert, SIGNATURE_BYTES)), **public_keys(user_pub)})


def seal_user(
    agent_kem_pub: bytes,
    secret: bytes,
    agent_id: str,
    user_id: str,
    *,
    user_pub: object,
    cert: object,
    nonce: bytes | None = None,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """`user_ct`: the user's public keys and the certificate under `k` and sealed to the agent (`nonce`, `ephemeral`
    only for test vectors)."""
    info = user_info(agent_id, user_id)
    inner = aead.seal_blob(bundle_key(secret, agent_id, user_id), user_plaintext(user_pub, cert), info, nonce)
    return b64u_encode(hpke.seal(agent_kem_pub, info, inner, ephemeral))


def open_user(agent_kem: X25519PrivateKey, secret: bytes, agent_id: str, user_id: str, sealed: str) -> dict[str, str]:
    """The inverse of `seal_user`: `{"cert", "kem", "sig"}` (`hpke_short`, `decrypt_failed`, `header_invalid`)."""
    info = user_info(agent_id, user_id)
    inner = hpke.open_(agent_kem, info, b64u_decode(sealed))
    plaintext = aead.open_blob(bundle_key(secret, agent_id, user_id), inner, info)
    value = require_keys(parse_json(plaintext), _USER_KEYS)
    if user_plaintext({"kem": value["kem"], "sig": value["sig"]}, value["cert"]) != plaintext:
        fail("header_invalid")
    return {"cert": str(value["cert"]), "kem": str(value["kem"]), "sig": str(value["sig"])}
