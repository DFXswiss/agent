"""Error shape of the hub API (03-api.md sections 1 and 11).

Every non-2xx response has the body `{"error": "<code>"}` with an optional `"detail"` of non-confidential structured
data. Never exception text, database text or a value from a request. The codes and their statuses are a copy of
`contracts/enums/error-codes.json` (hub-origin codes; the contracts directory is not installed with the package); the
test `test_error_codes_equal_the_contract` fails when the two differ.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

ERROR_STATUSES: Mapping[str, tuple[int, ...]] = {
    "request_invalid": (400,),
    "header_invalid": (400,),
    "params_invalid": (400,),
    "account_code_invalid": (400,),
    "non_ascii": (400,),
    "kind_not_allowed": (400,),
    "epoch_stale": (400,),
    "invite_code_invalid": (400,),
    "ceremony_invalid": (400,),
    "client_data_invalid": (400,),
    "challenge_mismatch": (400,),
    "origin_mismatch": (400,),
    "attestation_invalid": (400,),
    "authenticator_data_invalid": (400,),
    "rp_id_mismatch": (400,),
    "user_presence_missing": (400,),
    "user_verification_missing": (400,),
    "credential_invalid": (400,),
    "statement_invalid": (400,),
    "certificate_expiry_invalid": (400,),
    "counter_regressed": (400,),
    "size_exceeded": (400, 413),
    "signature_missing": (401,),
    "key_unknown": (401,),
    "key_revoked": (401,),
    "time_skew": (401,),
    "signature_invalid": (401,),
    "nonce_replayed": (401,),
    "session_expired": (401,),
    "unauthorized": (401,),
    "forbidden": (403,),
    "last_admin": (403,),
    "last_passkey": (403,),
    "device_not_assigned": (403,),
    "platform_invite_required": (403,),
    "platform_access_denied": (403,),
    "not_found": (404,),
    "identity_unproven": (404,),
    "invite_unknown": (404,),
    "id_conflict": (409,),
    "event_conflict": (409,),
    "seq_conflict": (409,),
    "version_conflict": (409,),
    "origin_seq_gap": (409,),
    "origin_seq_conflict": (409,),
    "no_device": (409,),
    "already_assigned": (409,),
    "rotation_required": (409,),
    "no_device_available": (409,),
    "invite_registered": (409,),
    "peer_outdated": (409,),
    "community_muted": (403,),
    "community_not_member": (409,),
    "community_epoch_exists": (409,),
    "community_no_key": (409,),
    "placement_invalid": (409,),
    "placement_forbidden": (403,),
    "invite_expired": (410,),
    "quota_exceeded": (413,),
    "rate_limited": (429,),
    "unavailable": (503,),
    "decrypt_failed": (400,),
    "prf_length": (400,),
    "recovery_code_length": (400,),
    "hpke_short": (400,),
    "invite_code_length": (400,),
    "wrap_unauthorized": (400,),
    "log_empty": (400,),
    "log_chain": (400,),
    "log_genesis": (400,),
    "log_accept": (400,),
    "log_admit": (400,),
    "log_signature": (400,),
    "log_rights": (400,),
    "log_epoch": (400,),
    "log_type": (400,),
    "invite_not_in_log": (400,),
    "invite_consumed": (400,),
    "invite_revoked": (400,),
    "author_not_writer": (400,),
    "pairing_unknown": (404,),
    "pairing_gesture_required": (403,),
    "claim_invalid": (401,),
    "handover_required": (400,),
    "handover_unexpected": (400,),
    "identity_forbidden": (403,),
}

# The detail keys of `contracts/common.json#/$defs/error` (closed).
DETAIL_KEYS = frozenset({"pointers", "expected_seq", "expected", "candidates_available", "devices", "quota"})
INTERNAL_ERROR = "unavailable"


class HubError(Exception):
    """A refusal with a code of the closed list; raised anywhere below a route, answered by the error handler.

    `status` defaults to the usual status of the code; another status must be one the list allows for the code.
    `headers` carries response headers such as `Retry-After`; never put request values into `detail`.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        detail: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        statuses = ERROR_STATUSES.get(code)
        if statuses is None:
            raise ValueError("the error code is not in the closed list")
        if status is None:
            status = statuses[0]
        elif status not in statuses:
            raise ValueError("the status is not allowed for the error code")
        if detail is not None and not set(detail) <= DETAIL_KEYS:
            raise ValueError("the error detail has a key outside the error schema")
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = None if detail is None else dict(detail)
        self.headers = dict(headers or {})


def error_body(code: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The compact error body."""
    body: dict[str, Any] = {"error": code}
    if detail is not None:
        body["detail"] = dict(detail)
    return body


def error_bytes(code: str, detail: Mapping[str, Any] | None = None) -> bytes:
    """The error body as compact JSON bytes."""
    return json.dumps(error_body(code, detail), separators=(",", ":")).encode("ascii")
