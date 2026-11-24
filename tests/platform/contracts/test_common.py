"""Named formats of `contracts/common.json` (03-api.md section 1, 04-crypto.md sections 1 and 3)."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from jsonschema import ValidationError

from tests.platform.contracts.harness import ID_BASE, Contracts, StrictValidator

MAX_SAFE = 2**53 - 1


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def check(contracts: Contracts, name: str, value: Any) -> bool:
    validator = StrictValidator({"$ref": f"{ID_BASE}common.json#/$defs/{name}"}, registry=contracts.registry)
    try:
        validator.validate(value)
    except ValidationError:
        return False
    return True


CASES: list[tuple[str, object, bool]] = [
    # UUIDv7, lower-case text with dashes
    ("uuid", "01936f0e-8a2b-7c3d-9e4f-0a1b2c3d4e5f", True),
    ("uuid", "01936f0e-8a2b-7c3d-af4f-0a1b2c3d4e5f", True),
    ("uuid", "01936f0e-8a2b-4c3d-9e4f-0a1b2c3d4e5f", False),
    ("uuid", "01936f0e-8a2b-7c3d-ce4f-0a1b2c3d4e5f", False),
    ("uuid", "01936F0E-8A2B-7C3D-9E4F-0A1B2C3D4E5F", False),
    ("uuid", "01936f0e8a2b7c3d9e4f0a1b2c3d4e5f", False),
    ("uuid", "", False),
    ("uuid_or_empty", "", True),
    ("uuid_or_empty", "01936f0e-8a2b-7c3d-9e4f-0a1b2c3d4e5f", True),
    ("uuid_or_empty", " ", False),
    # path parameter text of a context chunk index (KOR-33)
    ("q_index", "0", True),
    ("q_index", "999999", True),
    ("q_index", "1000000", False),
    ("q_index", "01", False),
    ("q_index", "-1", False),
    ("q_index", "", False),
    # times: UTC with milliseconds
    ("time", "2026-11-21T12:00:00.000Z", True),
    ("time", "2026-12-31T23:59:59.999Z", True),
    ("time", "2026-11-21T12:00:00Z", False),
    ("time", "2026-11-21T12:00:00.000000Z", False),
    ("time", "2026-11-21T12:00:00.000+00:00", False),
    ("time", "2026-11-21 12:00:00.000Z", False),
    ("time", "2026-13-01T00:00:00.000Z", False),
    ("time", "2026-11-21T24:00:00.000Z", False),
    ("time", "2026-11-21T12:00:00.000z", False),
    # canonical b64u without padding
    ("b64u", "", True),
    ("b64u", b64u(b"\xff"), True),
    ("b64u", b64u(b"\xff\xfe"), True),
    ("b64u", b64u(b"\xff\xfe\xfd"), True),
    ("b64u", b64u(bytes(range(256))), True),
    ("b64u", "_w", True),
    ("b64u", "_x", False),
    ("b64u", "AAB", False),
    ("b64u", "AAE", True),
    ("b64u", "A", False),
    ("b64u", "AA==", False),
    ("b64u", "AA+/", False),
    ("b64u", "AA A", False),
    ("b64u_12", b64u(bytes(12)), True),
    ("b64u_12", b64u(bytes(11)), False),
    ("b64u_16", b64u(bytes(16)), True),
    ("b64u_16", b64u(b"\xff" * 16), True),
    ("b64u_16", b64u(bytes(15)), False),
    ("b64u_16", b64u(bytes(17)), False),
    ("b64u_16", b64u(bytes(16))[:-1] + "B", False),
    ("b64u_32", b64u(b"\xff" * 32), True),
    ("b64u_32", b64u(bytes(32))[:-1] + "B", False),
    ("b64u_32", b64u(bytes(33)), False),
    ("b64u_64", b64u(b"\xab" * 64), True),
    ("b64u_64", b64u(bytes(63)), False),
    ("b64u_65", b64u(b"\x04" + bytes(64)), True),
    ("b64u_65", b64u(bytes(64)), False),
    ("b64u_80", b64u(b"\x99" * 80), True),
    ("b64u_80", b64u(bytes(79)), False),
    ("invite_id", b64u(bytes(16)), True),
    ("invite_id", b64u(bytes(16)) + "=", False),
    ("recovery_id", b64u(b"\x01" * 16), True),
    ("credential_id", b64u(bytes(1)), True),
    ("credential_id", b64u(bytes(1023)), True),
    ("credential_id", b64u(bytes(1024)), False),
    ("credential_id", "", False),
    ("sha256_hex", "ab" * 32, True),
    ("sha256_hex", "AB" * 32, False),
    ("sha256_hex", "ab" * 31, False),
    # key IDs (03-api.md section 1)
    ("session_key_id", "session." + "0123456789abcdef" * 2, True),
    ("session_key_id", "session." + "0123456789ABCDEF" * 2, False),
    ("session_key_id", "device." + "0" * 32, False),
    ("device_key_id", "device." + "0" * 32, True),
    ("device_key_id", "device." + "0" * 31, False),
    ("hub_key_id", "hub-1", True),
    ("hub_key_id", "a" * 64, True),
    ("hub_key_id", "a" * 65, False),
    ("hub_key_id", "Hub_1", False),
    ("bundle_slot", "recovery", True),
    ("bundle_slot", "passkey:" + b64u(bytes(32)), True),
    ("bundle_slot", "passkey:", False),
    ("bundle_slot", "passkey:AB", False),
    ("bundle_slot", "passkey:AQ", True),
    ("bundle_slot", "passkey:AAAAA", False),
    ("bundle_slot", "passkey:" + b64u(bytes(1023)), True),
    ("bundle_slot", "passkey:" + b64u(bytes(1024)), False),
    ("bundle_slot", "other", False),
    ("vm_id", "vm-0a1b2c3d", True),
    ("vm_id", "vm-0A1B2C3D", False),
    ("error_code", "not_found", True),
    ("error_code", "Not_found", False),
    ("error_code", "", False),
    ("model_id", "deepseek/deepseek-v4.1-flash-1017", True),
    ("model_id", "claude-sonnet-4-5", True),
    ("model_id", "a b", False),
    ("version_text", "2.1.330", True),
    ("version_text", "", False),
    # integers
    ("seq", 0, True),
    ("seq", MAX_SAFE, True),
    ("seq", MAX_SAFE + 1, False),
    ("seq", -1, False),
    ("seq", True, False),
    ("seq", 1.0, False),
    ("positive", 0, False),
    ("positive", 1, True),
    ("epoch", 1, True),
    ("epoch", 0, False),
    # query texts
    ("q_seq", "0", True),
    ("q_seq", "17", True),
    ("q_seq", "017", False),
    ("q_seq", "-1", False),
    ("q_seq", "", False),
    ("q_limit_log", "1000", True),
    ("q_limit_log", "1", True),
    ("q_limit_log", "0", False),
    ("q_limit_log", "1001", False),
    ("q_limit_list", "200", True),
    ("q_limit_list", "99", True),
    ("q_limit_list", "201", False),
    ("q_before", "MDE5MzAwMDM", True),
    ("q_before", "a/b", False),
    ("q_epoch", "1", True),
    ("q_epoch", "0", False),
    ("next_before", None, True),
    ("next_after", None, True),
    ("next_after", 3, True),
    ("count", 0, True),
    ("count", -1, False),
    ("label_text", "macOS 27.2", True),
    ("label_text", "", False),
    ("label_text", "caf\u00e9", False),
    ("operation_type", "github.create_pull_request", True),
    ("operation_type", "task.set_title", True),
    ("operation_type", "github", False),
    ("operation_type", "GitHub.comment", False),
    ("bundle_ct", b64u(bytes(133)), True),
    ("bundle_ct", b64u(bytes(27)), False),
    ("bundle_ct", b64u(bytes(1025)), False),
    ("package_ct", b64u(bytes(400)), True),
    ("package_ct", b64u(bytes(64 * 1024 + 1)), False),
    ("empty", {}, True),
    ("empty", {"a": 1}, False),
]


@pytest.mark.parametrize(("name", "value", "valid"), CASES)
def test_format(contracts: Contracts, name: str, value: object, valid: bool) -> None:
    assert check(contracts, name, value) is valid


def test_every_named_format_is_exercised(contracts: Contracts) -> None:
    tested = {name for name, _, _ in CASES}
    simple = {
        name
        for name, schema in contracts.schemas["common.json"]["$defs"].items()
        if schema.get("type") in ("string", "integer") and "enum" not in schema and "const" not in schema
    }
    assert simple <= tested, sorted(simple - tested)


def test_size_limits(contracts: Contracts) -> None:
    """Event ciphertext ≤ 256 KiB decoded (04-crypto.md E8), 1 MiB request bodies hold at most that."""
    defs = contracts.schemas["common.json"]["$defs"]
    other, terminal = defs["event"]["anyOf"]
    assert other["properties"]["ct"]["maxLength"] == len(b64u(bytes(256 * 1024)))
    assert terminal["properties"]["ct"]["maxLength"] == len(b64u(bytes(64 * 1024)))
    assert other["properties"]["ct"]["minLength"] == len(b64u(bytes(16)))
    assert defs["event_header_terminal"]["properties"]["kind"] == {"type": "string", "const": "session.terminal"}
    assert "session.terminal" not in defs["event_header_other"]["properties"]["kind"]["enum"]
    for name in ("event_header_terminal", "event_header_other", "event_header_prompt"):
        assert defs[name]["required"] == defs["event_header"]["required"]
    assert defs["prf_input"]["const"] == b64u(hashlib.sha256(b"agent.dfx.swiss/prf/v1").digest())
    assert defs["event_header"]["required"] == [
        "v",
        "alg",
        "id",
        "org",
        "scope",
        "scope_id",
        "epoch",
        "wg",
        "task",
        "session",
        "author",
        "kind",
        "ref",
        "ts",
        "lh",
        "salt",
        "nonce",
    ]
    assert defs["credential_header"]["required"] == [
        "v",
        "alg",
        "id",
        "owner",
        "kind",
        "version",
        "epoch",
        "author",
        "ts",
        "salt",
        "nonce",
    ]
    assert set(defs["device_request_header"]["required"]) == {
        "v",
        "id",
        "user",
        "device",
        "kind",
        "follows",
        "ts",
        "enc_alg",
    }
    assert set(defs["device_response_header"]["required"]) == {
        "v",
        "id",
        "user",
        "device",
        "kind",
        "in_reply_to",
        "seq",
        "final",
        "ts",
        "enc_alg",
    }
