"""The failure values of PASSKEY-PROTOCOL.md that the pure functions of this package return.

Each value is defined once, with the spelling of the specification. This module imports no module
of this package, so that every other module of the package can import it.
"""

from __future__ import annotations

# Section 4.1 (R4 to R6, V2 to V4).
CLIENT_DATA_INVALID = "client_data_invalid"
CHALLENGE_MISMATCH = "challenge_mismatch"
ORIGIN_MISMATCH = "origin_mismatch"
# Section 5.5, R7.
ATTESTATION_INVALID = "attestation_invalid"
# Section 4.2 (R8 to R12, V5 to V9).
AUTHENTICATOR_DATA_INVALID = "authenticator_data_invalid"
RP_ID_MISMATCH = "rp_id_mismatch"
USER_PRESENCE_MISSING = "user_presence_missing"
USER_VERIFICATION_MISSING = "user_verification_missing"
# Section 5.5, R13.
CREDENTIAL_INVALID = "credential_invalid"
# Section 4.3 (V10).
SIGNATURE_INVALID = "signature_invalid"
# Section 6.4 (V1, V11).
STATEMENT_INVALID = "statement_invalid"
CERTIFICATE_EXPIRY_INVALID = "certificate_expiry_invalid"
# Section 4.4 (S7).
COUNTER_REGRESSED = "counter_regressed"
# Section 8.2 (C2 to C4).
COMMAND_INVALID = "command_invalid"
COMMAND_TIME_SKEW = "command_time_skew"
COMMAND_SIGNATURE_INVALID = "command_signature_invalid"


class PasskeyRefusal(Exception):
    """A refusal with one failure value of the specification; the message is that value only.

    The message never carries an input: no challenge, key, signature or client data.
    """

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error
