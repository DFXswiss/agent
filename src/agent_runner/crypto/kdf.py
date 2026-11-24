"""Copied from agent-core src/agent_hub/crypto/kdf.py at 3d02f01a237c.

Derivations and labels of 04-crypto.md section 3 (HKDF-SHA256, 32-byte output, empty salt unless stated).

Labels and derivations follow Spike D `e2ee.py` (`kek_from_prf`, `kek_from_recovery_code`, `invite_secrets`);
`recovery_id`, `dedupe_key`, `credential_key` and `storage_key` are new in the specification.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent_runner.crypto.encoding import P, require_bytes

PRF_INPUT_LABEL = b"agent.dfx.swiss/prf/v1"

# Info labels of the derivations (section 3), without the prefix P.
KEK = b"kek\x00"
RECOVERY_KEK = b"recovery-kek"
RECOVERY_ID = b"recovery-id"
INVITE_ID = b"invite-id"
INVITE_PACKAGE = b"invite-package"
INVITE_SIGN = b"invite-sign"
EVENT = b"event"
DEDUPE = b"dedupe"
CREDENTIAL = b"credential"
RUNNER_STORAGE = b"runner-storage"

# Signature/AAD labels (section 3), without the prefix P.
LABEL_BUNDLE = b"bundle\x00"
LABEL_WRAP = b"wrap\x00"
LABEL_EVENT_SIG = b"event-sig\x00"
LABEL_LOG = b"log\x00"
LABEL_PLATFORM_LOG = b"platform-log\x00"
LABEL_INVITE_PACKAGE = b"invite-package\x00"
LABEL_CREDENTIAL_SIG = b"credential-sig\x00"
LABEL_REQUEST_WRAP = b"request-wrap\x00"
LABEL_REQUEST_SIG = b"request-sig\x00"
LABEL_DEDUPE = b"dedupe\x00"
LABEL_RECOVERY_PROOF = b"recovery-proof\x00"
LABEL_ENROLL = b"enroll\x00"
LABEL_INVITE_REGISTER = b"invite-register\x00"
LABEL_INVITE_IDENTITY = b"invite-identity\x00"
LABEL_ACCOUNT_CODE = b"account-code\x00"
LABEL_USER_ID = b"user-id\x00"

KEY_BYTES = 32
CODE_BYTES = 16
SALT_BYTES = 32


def hkdf(ikm: bytes, info: bytes, salt: bytes = b"", length: int = KEY_BYTES) -> bytes:
    """HKDF-SHA256 (RFC 5869); an empty salt is the all-zero salt of the RFC."""
    return HKDF(hashes.SHA256(), length, salt or None, info).derive(ikm)


def prf_input() -> bytes:
    """`prf.eval.first` of every `create()` and `get()`: SHA-256 of the PRF label."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(PRF_INPUT_LABEL)
    return digest.finalize()


def kek_from_prf(prf_output: bytes, credential_id: bytes) -> bytes:
    """`KEK_i` of a passkey slot; the PRF output must be 32 bytes (`prf_length`)."""
    require_bytes(prf_output, KEY_BYTES, "prf_length")
    return hkdf(prf_output, P + KEK + credential_id)


def _code(code: bytes, error: str) -> bytes:
    return require_bytes(code, CODE_BYTES, error)


def kek_from_recovery_code(code: bytes) -> bytes:
    """`KEK_rec`; the recovery code must be 16 bytes (`recovery_code_length`)."""
    return hkdf(_code(code, "recovery_code_length"), P + RECOVERY_KEK)


def recovery_id_raw(code: bytes) -> bytes:
    """The 16 raw bytes behind `recovery_id` (section 3)."""
    return hkdf(_code(code, "recovery_code_length"), P + RECOVERY_ID)[:16]


def invite_id_raw(code: bytes) -> bytes:
    """The 16 raw bytes behind `invite_id`; the code must be 16 bytes (`invite_code_length`)."""
    return hkdf(_code(code, "invite_code_length"), P + INVITE_ID)[:16]


def package_key(code: bytes) -> bytes:
    """The AES-256-GCM key of the invitation package."""
    return hkdf(_code(code, "invite_code_length"), P + INVITE_PACKAGE)


def invite_sign_seed(code: bytes) -> bytes:
    """The Ed25519 seed of `sk_inv`."""
    return hkdf(_code(code, "invite_code_length"), P + INVITE_SIGN)


def event_key(scope_key: bytes, salt: bytes) -> bytes:
    """`k_event = HKDF(scope_key, salt=header.salt, info=P||"event")`; salt 32 bytes, key 32 bytes."""
    require_bytes(scope_key, KEY_BYTES)
    return hkdf(scope_key, P + EVENT, salt=require_bytes(salt, SALT_BYTES))


def dedupe_key(workgroup_key: bytes) -> bytes:
    """`dk_e = HKDF(WK_e, info=P||"dedupe")`."""
    return hkdf(require_bytes(workgroup_key, KEY_BYTES), P + DEDUPE)


def credential_key(credential_scope_key: bytes, salt: bytes) -> bytes:
    """`k_cred = HKDF(CK_e, salt=header.salt, info=P||"credential")`."""
    require_bytes(credential_scope_key, KEY_BYTES)
    return hkdf(credential_scope_key, P + CREDENTIAL, salt=require_bytes(salt, SALT_BYTES))


def storage_key(device_sig_seed: bytes) -> bytes:
    """The runner storage key `HKDF(device_sig_seed, info=P||"runner-storage")`."""
    return hkdf(require_bytes(device_sig_seed, KEY_BYTES), P + RUNNER_STORAGE)
