"""Copied from agent-core src/agent_hub/crypto/errors.py at 3392035c31d1.

Error codes of the crypto layer (docs/platform/spec/04-crypto.md section 8, 03-api.md section 11).

Every refusal of this package is a `CryptoError` whose `code` is one of the closed lists below. The message is the
code itself and never carries key material, plaintext or input data, so it can be passed on to clients unchanged.
"""

from __future__ import annotations

from typing import NoReturn

# 04-crypto.md section 8, "Crypto/format".
CRYPTO_CODES = frozenset(
    {
        "non_ascii",
        "header_invalid",
        "signature_invalid",
        "decrypt_failed",
        "prf_length",
        "prf_missing",
        "recovery_code_length",
        "hpke_short",
        "invite_code_length",
        "invite_code_invalid",
        "event_conflict",
        "epoch_stale",
        "kind_not_allowed",
        "size_exceeded",
        "wrap_unauthorized",
        "platform_root_mismatch",
        "account_code_invalid",
    }
)

# 04-crypto.md section 8, "Log" (raised by agent_hub.log, which builds on this package).
LOG_CODES = frozenset(
    {
        "seq_conflict",
        "log_empty",
        "log_chain",
        "log_genesis",
        "log_accept",
        "log_admit",
        "log_signature",
        "log_rights",
        "log_epoch",
        "log_type",
        "log_rollback",
        "log_fork",
        "invite_not_in_log",
        "invite_consumed",
        "invite_expired",
        "invite_revoked",
        "author_not_writer",
        "last_admin",
        "rotation_required",
    }
)

CODES = CRYPTO_CODES | LOG_CODES


class CryptoError(Exception):
    """A refusal with a code from `CODES`; `str(error)` is the code."""

    def __init__(self, code: str) -> None:
        if code not in CODES:
            raise ValueError("unknown crypto error code")
        super().__init__(code)
        self.code = code


def fail(code: str) -> NoReturn:
    """Raise `CryptoError(code)`.

    Callers never call it inside an `except` block: a handled exception would become `__context__` of the refusal
    and may hold input data (a JSON decode error keeps the whole decrypted plaintext). Every module of this package
    leaves the `except` block first and then refuses; a test checks that refusals carry no context.
    """
    raise CryptoError(code)
