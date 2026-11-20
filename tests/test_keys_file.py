"""Keys file loader (KEY-PROTOCOL.md section 3): every failure stops the start."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_core.keys import KeyRing, KeysError, device_key, load_keys
from agent_core.signing import (
    ED25519_FIELD_PRIME,
    ED25519_ORDER8_Y,
    ED25519_SMALL_ORDER_Y,
    ed25519_public_key_valid,
)
from agent_core.teams import Roster, load_roster

NOW = int(datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp())
PAST = "2027-01-15T07:59:59Z"
AT_NOW = "2027-01-15T08:00:00Z"
FUTURE = "2027-01-15T08:00:01Z"
DROP = object()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def ed_raw(n: int) -> bytes:
    private = Ed25519PrivateKey.from_private_bytes(bytes([n]) * 32)
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def ed_key(n: int) -> str:
    return b64(ed_raw(n))


def p256_raw(n: int) -> bytes:
    private = ec.derive_private_key(n, ec.SECP256R1())
    return private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def p256_key(n: int) -> str:
    return b64(p256_raw(n))


def changed(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = dict(base)
    for field, value in changes.items():
        if value is DROP:
            out.pop(field)
        else:
            out[field] = value
    return out


def staff(**changes: Any) -> dict[str, Any]:
    return changed(
        {
            "id": "staff-a-pass-1",
            "kind": "webauthn",
            "principal": "staff",
            "handle": "staff-a",
            "team": "example",
            "credential_id": b64(b"credential-0001!"),
            "public_key": p256_key(1001),
            "scopes": ["record:read", "record:write"],
        },
        **changes,
    )


def agent(**changes: Any) -> dict[str, Any]:
    return changed(
        {
            "id": "staff-a-laptop-1",
            "kind": "ed25519",
            "principal": "agent",
            "handle": "staff-a",
            "team": "example",
            "device": "laptop-1",
            "public_key": ed_key(1),
            "scopes": ["record:read", "record:write", "device:controller"],
        },
        **changes,
    )


def service(**changes: Any) -> dict[str, Any]:
    return changed(
        {
            "id": "example-guard-1",
            "kind": "ed25519",
            "principal": "service",
            "handle": "example-guard",
            "team": "example",
            "device": "runner-1",
            "public_key": ed_key(2),
            "scopes": ["release:query"],
            "subjects": ["example-org/example-repo"],
            "expires": "2027-06-30T00:00:00Z",
        },
        **changes,
    )


BUILDERS = {"staff": staff, "agent": agent, "service": service}


@pytest.fixture
def roster(tmp_path: Path) -> Roster:
    path = tmp_path / "teams.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "teams": {
                    "example": {"members": ["staff-a", "staff-b", "helperbot"]},
                    "other": {"members": ["staff-c"]},
                },
                "agents": {"helperbot": {"owners": ["staff-a"]}},
            }
        ),
        encoding="utf-8",
    )
    return load_roster(path)


def write(tmp_path: Path, entries: list[Any]) -> Path:
    path = tmp_path / "keys.yaml"
    path.write_text(yaml.safe_dump({"keys": entries}, sort_keys=False), encoding="utf-8")
    return path


def load(tmp_path: Path, roster: Roster, entries: list[Any]) -> KeyRing:
    return load_keys(write(tmp_path, entries), roster, NOW)


def refused(tmp_path: Path, roster: Roster, entries: list[Any], match: str) -> None:
    with pytest.raises(KeysError, match=match):
        load(tmp_path, roster, entries)


def refused_text(tmp_path: Path, roster: Roster, text: str, match: str) -> None:
    path = tmp_path / "keys.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(KeysError, match=match):
        load_keys(path, roster, NOW)


# Valid files


def test_empty_list_is_the_valid_starting_state(tmp_path: Path, roster: Roster) -> None:
    assert load(tmp_path, roster, []).entries == {}


def test_the_three_example_entries_load(tmp_path: Path, roster: Roster) -> None:
    ring = load(tmp_path, roster, [staff(), agent(), service(program="guard-cli")])
    passkey = ring.get("staff-a-pass-1")
    laptop = ring.get("staff-a-laptop-1")
    guard = ring.get("example-guard-1")
    assert passkey is not None and laptop is not None and guard is not None
    assert passkey.kind == "webauthn" and passkey.credential_id == b"credential-0001!"
    assert passkey.public_key == p256_raw(1001)
    assert laptop.public_key == ed_raw(1)
    assert laptop.scopes == ("record:read", "record:write", "device:controller")
    assert laptop.device == "laptop-1" and laptop.program is None
    assert guard.subjects == ("example-org/example-repo",)
    assert guard.expires == int(datetime(2027, 6, 30, tzinfo=timezone.utc).timestamp())
    assert guard.program == "guard-cli"
    assert guard.revoked is None
    assert ring.get("unknown-key") is None


def test_service_handle_need_not_be_in_teams(tmp_path: Path, roster: Roster) -> None:
    assert load(tmp_path, roster, [service(handle="not-on-any-team")]).get("example-guard-1")


def test_revoked_entry_keeps_its_revocation_time(tmp_path: Path, roster: Roster) -> None:
    ring = load(tmp_path, roster, [agent(revoked=PAST)])
    entry = ring.get("staff-a-laptop-1")
    assert entry is not None and entry.revoked == NOW - 1
    assert entry.revoked_at(NOW) and not entry.revoked_at(NOW - 2)


# The file itself


def test_missing_file_is_refused(tmp_path: Path, roster: Roster) -> None:
    with pytest.raises(KeysError, match="not readable"):
        load_keys(tmp_path / "absent.yaml", roster, NOW)


def test_unreadable_file_is_refused(tmp_path: Path, roster: Roster) -> None:
    folder = tmp_path / "keys-folder"
    folder.mkdir()
    with pytest.raises(KeysError, match="not readable"):
        load_keys(folder, roster, NOW)


def test_file_that_is_not_utf8_is_refused(tmp_path: Path, roster: Roster) -> None:
    path = tmp_path / "keys.yaml"
    path.write_bytes(b"keys: [\xff]\n")
    with pytest.raises(KeysError, match="not readable"):
        load_keys(path, roster, NOW)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("keys: [\n", "not valid YAML"),
        ("keys: []\nkeys: []\n", "not valid YAML"),
        (
            "keys:\n  - id: staff-a-laptop-1\n    id: staff-a-laptop-2\n",
            "not valid YAML",
        ),
        ("", "exactly one key"),
        ("- keys\n", "exactly one key"),
        ("keys: []\nextra: 1\n", "exactly one key"),
        ("other: []\n", "exactly one key"),
        ("keys: {}\n", "must be a list"),
        ("keys:\n", "must be a list"),
        ("keys: [plain-text]\n", "must be a mapping"),
        ("keys:\n  - [1, 2]\n", "must be a mapping"),
    ],
)
def test_file_shape_failures(tmp_path: Path, roster: Roster, text: str, match: str) -> None:
    refused_text(tmp_path, roster, text, match)


# Fields: unknown, missing, wrong type, refused for the principal, not in form


def test_unknown_field_is_refused(tmp_path: Path, roster: Roster) -> None:
    refused(tmp_path, roster, [agent(note="laptop at home")], "unknown field 'note'")


def test_missing_principal_is_refused(tmp_path: Path, roster: Roster) -> None:
    refused(tmp_path, roster, [agent(principal=DROP)], "missing the field principal")


@pytest.mark.parametrize("principal", ["admin", "Agent", ["agent"], 1, None])
def test_unknown_principal_is_refused(tmp_path: Path, roster: Roster, principal: Any) -> None:
    refused(tmp_path, roster, [agent(principal=principal)], "principal must be one of")


@pytest.mark.parametrize(
    ("principal", "field"),
    [
        (principal, field)
        for principal, fields in {
            "staff": ["id", "kind", "handle", "team", "public_key", "scopes", "credential_id"],
            "agent": ["id", "kind", "handle", "team", "public_key", "scopes", "device"],
            "service": [
                "id",
                "kind",
                "handle",
                "team",
                "public_key",
                "scopes",
                "device",
                "subjects",
                "expires",
            ],
        }.items()
        for field in fields
    ],
)
def test_missing_required_field_is_refused(
    tmp_path: Path, roster: Roster, principal: str, field: str
) -> None:
    entry = BUILDERS[principal](**{field: DROP})
    refused(tmp_path, roster, [entry], f"missing the field {field}")


@pytest.mark.parametrize(
    ("principal", "field", "value"),
    [
        ("staff", "device", "laptop-1"),
        ("staff", "program", "agent-cli"),
        ("staff", "subjects", ["example-org/example-repo"]),
        ("staff", "expires", "2027-06-30T00:00:00Z"),
        ("agent", "credential_id", b64(b"credential-0001!")),
        ("agent", "subjects", ["example-org/example-repo"]),
        ("agent", "expires", "2027-06-30T00:00:00Z"),
        ("service", "credential_id", b64(b"credential-0001!")),
    ],
)
def test_field_refused_for_the_principal(
    tmp_path: Path, roster: Roster, principal: str, field: str, value: Any
) -> None:
    entry = BUILDERS[principal](**{field: value})
    refused(tmp_path, roster, [entry], f"field {field} is refused for principal {principal}")


@pytest.mark.parametrize(
    "entry",
    [
        agent(kind="webauthn"),
        agent(kind="p256"),
        service(kind="webauthn"),
        staff(kind="ed25519"),
    ],
)
def test_kind_must_match_the_principal(tmp_path: Path, roster: Roster, entry: dict[str, Any]) -> None:
    refused(tmp_path, roster, [entry], "kind must be")


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (agent(id=123), "id must be a string"),
        (agent(kind=True), "kind must be a string"),
        (agent(handle=True), "handle must be a string"),
        (agent(team=["example"]), "team must be a string"),
        (agent(public_key=5), "public_key must be a string"),
        (agent(device=7), "device must be a string"),
        (agent(program=["cli"]), "program must be a string"),
        (agent(scopes="record:read"), "scopes must be a non-empty list"),
        (agent(scopes=[1]), "every entry of scopes must be a string"),
        (service(subjects="example-org/example-repo"), "subjects must be a non-empty list"),
        (service(subjects=[None]), "every entry of subjects must be a string"),
        (service(expires=1800000000), "expires must be a string"),
        (agent(revoked=0), "revoked must be a string"),
        (staff(credential_id=12), "credential_id must be a string"),
    ],
)
def test_field_of_the_wrong_type_is_refused(
    tmp_path: Path, roster: Roster, entry: dict[str, Any], match: str
) -> None:
    refused(tmp_path, roster, [entry], match)


def test_unquoted_time_is_refused(tmp_path: Path, roster: Roster) -> None:
    entry = yaml.safe_dump({"keys": [service(expires=DROP)]}, sort_keys=False)
    text = entry.rstrip("\n") + "\n  expires: 2027-06-30T00:00:00Z\n"
    refused_text(tmp_path, roster, text, "expires must be a string")


def test_unquoted_yes_no_value_is_refused(tmp_path: Path, roster: Roster) -> None:
    entry = yaml.safe_dump({"keys": [service(handle=DROP)]}, sort_keys=False)
    text = entry.rstrip("\n") + "\n  handle: no\n"
    refused_text(tmp_path, roster, text, "handle must be a string")


@pytest.mark.parametrize(
    "key_id",
    ["Staff-A-Laptop", "ab", "a" * 64, "-laptop-1", "laptop-1-", "lap_top", "lap top", "läptop"],
)
def test_id_outside_the_rule_is_refused(tmp_path: Path, roster: Roster, key_id: str) -> None:
    refused(tmp_path, roster, [agent(id=key_id)], "id breaks the id rule")


@pytest.mark.parametrize(
    "handle",
    ["Example-Guard", "example--guard", "-guard", "guard-", "a" * 40, "", "guard_1", "guard.1"],
)
def test_handle_outside_the_rule_is_refused(tmp_path: Path, roster: Roster, handle: str) -> None:
    refused(tmp_path, roster, [service(handle=handle)], "handle breaks the handle rule")


def test_handle_of_39_characters_is_allowed(tmp_path: Path, roster: Roster) -> None:
    assert load(tmp_path, roster, [service(handle="a" * 39)]).get("example-guard-1")


@pytest.mark.parametrize(("field", "value"), [("device", "Laptop 1"), ("program", "-cli"), ("device", "ab")])
def test_device_or_program_outside_the_id_rule_is_refused(
    tmp_path: Path, roster: Roster, field: str, value: str
) -> None:
    refused(tmp_path, roster, [agent(**{field: value})], f"{field} breaks the id rule")


@pytest.mark.parametrize(
    "subject",
    [
        "example-org",
        "example-org/",
        "/example-repo",
        "Example-Org/example-repo",
        "example-org/Example-Repo",
        "example-org/.",
        "example-org/..",
        "example-org/" + "a" * 101,
        "example-org/a/b",
        "example--org/example-repo",
        "example-org/example repo",
    ],
)
def test_subject_outside_the_form_is_refused(tmp_path: Path, roster: Roster, subject: str) -> None:
    refused(tmp_path, roster, [service(subjects=[subject])], "is not owner/name")


def test_subject_forms_that_are_allowed(tmp_path: Path, roster: Roster) -> None:
    subjects = ["example-org/" + "a" * 100, "example-org/.github", "example-org/a_b.c-d", "x/..."]
    assert load(tmp_path, roster, [service(subjects=subjects)]).get("example-guard-1")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("scopes", [], "scopes must be a non-empty list"),
        ("scopes", ["record:read", "record:read", "device:executor"], "lists 'record:read' twice"),
    ],
)
def test_scopes_list_rules(
    tmp_path: Path, roster: Roster, field: str, value: Any, match: str
) -> None:
    refused(tmp_path, roster, [agent(**{field: value})], match)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ([], "subjects must be a non-empty list"),
        (["example-org/example-repo", "example-org/example-repo"], "twice"),
    ],
)
def test_subjects_list_rules(tmp_path: Path, roster: Roster, value: Any, match: str) -> None:
    refused(tmp_path, roster, [service(subjects=value)], match)


@pytest.mark.parametrize(
    "value",
    [
        "2027-06-30",
        "2027-06-30T00:00:00.5Z",
        "2027-06-30T00:00:00+00:00",
        "2027-06-30T00:00:00",
        "2027-02-30T00:00:00Z",
        "2027-06-30T24:00:00Z",
        "2027-06-30 00:00:00Z",
        "2027-6-30T00:00:00Z",
    ],
)
@pytest.mark.parametrize("field", ["expires", "revoked"])
def test_time_outside_the_form_is_refused(
    tmp_path: Path, roster: Roster, field: str, value: str
) -> None:
    refused(tmp_path, roster, [service(**{field: value})], f"{field} must be a quoted time")


# Binary values: encoding, length and the point


def _non_canonical(text: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return text[:-1] + alphabet[alphabet.index(text[-1]) + 1]


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (agent(public_key=ed_key(1) + "="), "public_key is not canonical base64url"),
        (agent(public_key=_non_canonical(ed_key(1))), "public_key is not canonical base64url"),
        (agent(public_key=base64.b64encode(ed_raw(1)).decode()), "public_key"),
        (agent(public_key="not base64url!"), "public_key is not canonical base64url"),
        (agent(public_key=b64(ed_raw(1)[:31])), "public_key must be 32 bytes"),
        (agent(public_key=b64(ed_raw(1) + b"\x00")), "public_key must be 32 bytes"),
        (staff(public_key=b64(p256_raw(1001)[:64])), "public_key must be 65 bytes"),
        (staff(credential_id=b64(b"credential-0001!") + "="), "credential_id is not canonical"),
        (staff(credential_id=_non_canonical(b64(b"credential-01!"))), "credential_id is not canonical"),
        (staff(credential_id=b64(b"x" * 15)), "credential_id must be 16 to 1023 bytes"),
        (staff(credential_id=b64(b"x" * 1024)), "credential_id must be 16 to 1023 bytes"),
    ],
)
def test_binary_value_encoding_and_length(
    tmp_path: Path, roster: Roster, entry: dict[str, Any], match: str
) -> None:
    refused(tmp_path, roster, [entry], match)


def test_credential_id_bounds_are_allowed(tmp_path: Path, roster: Roster) -> None:
    assert load(tmp_path, roster, [staff(credential_id=b64(b"x" * 16))]).get("staff-a-pass-1")
    assert load(tmp_path, roster, [staff(credential_id=b64(b"x" * 1023))]).get("staff-a-pass-1")


def test_order8_constant_is_a_point_of_order_eight() -> None:
    # A point doubles to an order-4 point (y = 0) when x^2 = -y^2, which on the curve
    # -x^2 + y^2 = 1 + d x^2 y^2 means d y^4 + 2 y^2 - 1 = 0.
    p = ED25519_FIELD_PRIME
    d = (-121665 * pow(121666, p - 2, p)) % p
    for y in (ED25519_ORDER8_Y, p - ED25519_ORDER8_Y):
        y2 = y * y % p
        assert (d * y2 * y2 + 2 * y2 - 1) % p == 0
        # -y^2 must be a square for x to exist (Euler's criterion).
        assert pow((-y2) % p, (p - 1) // 2, p) == 1


def _encoding(y: int, sign: int) -> bytes:
    return (y | (sign << 255)).to_bytes(32, "little")


SMALL_ORDER_ENCODINGS = [
    _encoding(y, sign) for y in sorted(ED25519_SMALL_ORDER_Y) for sign in (0, 1)
]
NON_CANONICAL_ENCODINGS = [
    _encoding(ED25519_FIELD_PRIME, 0),
    _encoding(ED25519_FIELD_PRIME + 1, 0),
    _encoding(ED25519_FIELD_PRIME + 1, 1),
    _encoding((1 << 255) - 1, 0),
    _encoding((1 << 255) - 1, 1),
]


@pytest.mark.parametrize("raw", SMALL_ORDER_ENCODINGS + NON_CANONICAL_ENCODINGS)
def test_small_order_or_non_canonical_ed25519_key_is_refused(
    tmp_path: Path, roster: Roster, raw: bytes
) -> None:
    assert not ed25519_public_key_valid(raw)
    refused(tmp_path, roster, [agent(public_key=b64(raw))], "not a valid Ed25519 point")


def test_ordinary_ed25519_keys_are_valid() -> None:
    for n in range(1, 20):
        assert ed25519_public_key_valid(ed_raw(n))


@pytest.mark.parametrize(
    "raw",
    [
        b"\x04" + b"\x00" * 64,
        b"\x04" + p256_raw(1001)[1:33] + bytes(32),
        b"\x02" + p256_raw(1001)[1:],
        b"\x04" + b"\xff" * 64,
    ],
)
def test_passkey_key_that_is_not_a_p256_point_is_refused(
    tmp_path: Path, roster: Roster, raw: bytes
) -> None:
    refused(tmp_path, roster, [staff(public_key=b64(raw))], "not a valid P-256 point")


# Uniqueness


def test_id_twice_is_refused(tmp_path: Path, roster: Roster) -> None:
    second = agent(device="laptop-2", public_key=ed_key(3))
    refused(tmp_path, roster, [agent(), second], "id 'staff-a-laptop-1' appears twice")


def test_public_key_twice_is_refused(tmp_path: Path, roster: Roster) -> None:
    second = agent(id="staff-a-laptop-2", device="laptop-2")
    refused(tmp_path, roster, [agent(), second], "public_key appears twice")


def test_public_key_twice_across_principals_is_refused(tmp_path: Path, roster: Roster) -> None:
    refused(tmp_path, roster, [agent(), service(public_key=ed_key(1))], "public_key appears twice")


def test_credential_id_twice_is_refused(tmp_path: Path, roster: Roster) -> None:
    second = staff(id="staff-a-pass-2", public_key=p256_key(1002))
    refused(tmp_path, roster, [staff(), second], "credential_id appears twice")


# Teams and handles


def test_team_not_in_teams_file_is_refused(tmp_path: Path, roster: Roster) -> None:
    refused(tmp_path, roster, [service(team="unknown-team")], "is not in the teams file")


@pytest.mark.parametrize("builder", [staff, agent])
def test_staff_or_agent_handle_must_be_a_member_of_the_team(
    tmp_path: Path, roster: Roster, builder: Any
) -> None:
    refused(tmp_path, roster, [builder(handle="staff-c")], "is not a member of team 'example'")
    refused(tmp_path, roster, [builder(handle="nobody")], "is not a member of team 'example'")


@pytest.mark.parametrize("builder", [staff, agent])
def test_staff_or_agent_handle_must_not_be_an_agent_login(
    tmp_path: Path, roster: Roster, builder: Any
) -> None:
    refused(tmp_path, roster, [builder(handle="helperbot")], "is a login in the agents map")


def test_handle_member_of_its_own_team_is_allowed(tmp_path: Path, roster: Roster) -> None:
    ring = load(tmp_path, roster, [agent(handle="staff-c", team="other")])
    assert ring.get("staff-a-laptop-1") is not None


# Scopes


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (agent(scopes=["record:read", "record:delete", "device:executor"]), "scope 'record:delete' is unknown"),
        (agent(scopes=["release:query", "device:executor"]), "'release:query' is not allowed for principal agent"),
        (agent(scopes=["keys:invite", "device:executor"]), "'keys:invite' is not allowed for principal agent"),
        (service(scopes=["record:write"]), "'record:write' is not allowed for principal service"),
        (service(scopes=["device:executor"]), "'device:executor' is not allowed for principal service"),
        (service(scopes=["keys:invite"]), "'keys:invite' is not allowed for principal service"),
        (staff(scopes=["device:controller"]), "'device:controller' is not allowed for principal staff"),
        (staff(scopes=["release:query"]), "'release:query' is not allowed for principal staff"),
    ],
)
def test_scope_unknown_or_not_allowed(
    tmp_path: Path, roster: Roster, entry: dict[str, Any], match: str
) -> None:
    refused(tmp_path, roster, [entry], match)


@pytest.mark.parametrize(
    "scopes",
    [["record:read"], ["record:read", "device:executor", "device:controller"]],
)
def test_agent_holds_exactly_one_device_scope(
    tmp_path: Path, roster: Roster, scopes: list[str]
) -> None:
    refused(tmp_path, roster, [agent(scopes=scopes)], "exactly one of")


def test_every_principal_may_read_the_record(tmp_path: Path, roster: Roster) -> None:
    ring = load(
        tmp_path,
        roster,
        [
            staff(scopes=["record:read", "keys:invite"]),
            agent(scopes=["record:read", "device:executor"]),
            service(scopes=["record:read", "release:query"]),
        ],
    )
    assert len(ring.entries) == 3


# RECORD-PROTOCOL.md section 3: the scopes `record:security` and `record:watch` and their loader rules.


def test_the_record_scopes_and_who_may_hold_them(tmp_path: Path, roster: Roster) -> None:
    ring = load(
        tmp_path,
        roster,
        [
            staff(scopes=["record:read", "record:write", "record:security"]),
            agent(scopes=["record:read", "record:write", "record:security", "device:controller"]),
            service(scopes=["record:read", "record:watch", "record:security"]),
        ],
    )
    assert ring.entries["example-guard-1"].scopes == ("record:read", "record:watch", "record:security")


@pytest.mark.parametrize(
    "entry",
    [
        agent(scopes=["record:write", "device:controller"]),
        agent(scopes=["record:security", "device:controller"]),
        staff(scopes=["record:write"]),
        staff(scopes=["record:security"]),
        service(scopes=["record:watch"]),
        service(scopes=["record:security", "release:query"]),
    ],
)
def test_a_record_scope_without_record_read_is_refused(tmp_path: Path, roster: Roster, entry: dict[str, Any]) -> None:
    refused(tmp_path, roster, [entry], "needs record:read")


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (agent(scopes=["record:read", "record:watch", "device:controller"]), "'record:watch' is not allowed for principal agent"),
        (staff(scopes=["record:read", "record:watch"]), "'record:watch' is not allowed for principal staff"),
        (service(scopes=["record:read", "record:write"]), "'record:write' is not allowed for principal service"),
    ],
)
def test_record_watch_only_on_a_service_entry_and_record_write_never_on_one(
    tmp_path: Path, roster: Roster, entry: dict[str, Any], match: str
) -> None:
    refused(tmp_path, roster, [entry], match)


def _team_roster(tmp_path: Path, team: str) -> Roster:
    path = tmp_path / "long-teams.yaml"
    path.write_text(yaml.safe_dump({"teams": {team: {"members": ["staff-a"]}}}), encoding="utf-8")
    return load_roster(path)


@pytest.mark.parametrize("builder", [staff, agent, service])
def test_a_team_of_101_bytes_is_refused_also_when_it_is_in_the_teams_file(tmp_path: Path, builder: Any) -> None:
    team = "t" * 99 + "é"  # 101 bytes in UTF-8
    roster = _team_roster(tmp_path, team)
    refused(tmp_path, roster, [builder(team=team)], "team is longer than 100 bytes")


@pytest.mark.parametrize("builder", [staff, agent, service])
def test_a_team_of_100_bytes_loads(tmp_path: Path, builder: Any) -> None:
    team = "t" * 98 + "é"  # 100 bytes in UTF-8
    roster = _team_roster(tmp_path, team)
    entry = builder(team=team)
    assert load(tmp_path, roster, [entry]).entries[entry["id"]].team == team


def test_two_watcher_keys_of_one_handle_on_one_device_need_distinct_programs(tmp_path: Path, roster: Roster) -> None:
    # RECORD-PROTOCOL.md section 8: a watcher serving two teams has one key per team.
    first = service(
        id="example-watcher-1", handle="example-watcher", scopes=["record:read", "record:watch"], public_key=ed_key(5)
    )
    second = service(
        id="example-watcher-2",
        handle="example-watcher",
        team="other",
        scopes=["record:read", "record:watch"],
        public_key=ed_key(6),
    )
    refused(tmp_path, roster, [first, second], "each needs a program")
    ring = load(tmp_path, roster, [dict(first, program="watch-example"), dict(second, program="watch-other")])
    assert len(ring.entries) == 2


def _second_staff(**changes: Any) -> dict[str, Any]:
    return staff(
        id="staff-b-pass-1",
        handle="staff-b",
        credential_id=b64(b"credential-0002!"),
        public_key=p256_key(1002),
        **changes,
    )


def test_two_handles_holding_keys_invite_are_refused(tmp_path: Path, roster: Roster) -> None:
    entries = [staff(scopes=["keys:invite"]), _second_staff(scopes=["keys:invite"])]
    refused(tmp_path, roster, entries, "more than one handle holds keys:invite")


def test_keys_invite_of_an_entry_revoked_at_start_does_not_count(
    tmp_path: Path, roster: Roster
) -> None:
    for revoked in (PAST, AT_NOW):
        entries = [staff(scopes=["keys:invite"]), _second_staff(scopes=["keys:invite"], revoked=revoked)]
        assert len(load(tmp_path, roster, entries).entries) == 2


def test_keys_invite_of_an_entry_revoked_later_counts(tmp_path: Path, roster: Roster) -> None:
    entries = [staff(scopes=["keys:invite"]), _second_staff(scopes=["keys:invite"], revoked=FUTURE)]
    refused(tmp_path, roster, entries, "more than one handle holds keys:invite")


def test_one_handle_may_hold_keys_invite_on_several_entries(tmp_path: Path, roster: Roster) -> None:
    second = staff(
        id="staff-a-pass-2",
        credential_id=b64(b"credential-0002!"),
        public_key=p256_key(1002),
        scopes=["keys:invite"],
    )
    assert len(load(tmp_path, roster, [staff(scopes=["keys:invite"]), second]).entries) == 2


# Device and program


def _second_agent(**changes: Any) -> dict[str, Any]:
    return agent(id="staff-a-laptop-2", public_key=ed_key(3), **changes)


@pytest.mark.parametrize(
    ("first", "second", "match"),
    [
        (agent(), _second_agent(), "each needs a program"),
        (agent(program="agent-cli"), _second_agent(program="agent-cli"), "one program twice"),
        (
            agent(),
            service(handle="staff-a", device="laptop-1"),
            "each needs a program",
        ),
        (
            agent(),
            _second_agent(revoked=FUTURE),
            "each needs a program",
        ),
    ],
)
def test_device_and_program_pair_must_be_unique(
    tmp_path: Path, roster: Roster, first: dict[str, Any], second: dict[str, Any], match: str
) -> None:
    refused(tmp_path, roster, [first, second], match)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (agent(), _second_agent(device="laptop-2")),
        (agent(program="agent-cli"), _second_agent(program="other-cli")),
        # PASSKEY-PROTOCOL.md section 7.4: a device key without `program` next to a program key.
        (agent(program="agent-cli"), _second_agent()),
        (agent(), _second_agent(revoked=PAST)),
        (agent(), _second_agent(revoked=AT_NOW)),
        (agent(), agent(id="staff-b-laptop-1", handle="staff-b", public_key=ed_key(3))),
    ],
)
def test_device_and_program_pairs_that_are_allowed(
    tmp_path: Path, roster: Roster, first: dict[str, Any], second: dict[str, Any]
) -> None:
    assert len(load(tmp_path, roster, [first, second]).entries) == 2


# PASSKEY-PROTOCOL.md section 7.4: the device key of a device


def _program_key(**changes: Any) -> dict[str, Any]:
    return agent(id="staff-a-laptop-cli", public_key=ed_key(4), program="agent-cli", **changes)


def test_a_device_key_and_a_program_key_on_one_device_load_and_only_the_first_is_the_device_key(
    tmp_path: Path, roster: Roster
) -> None:
    ring = load(tmp_path, roster, [agent(), _program_key()])
    assert device_key(ring, "staff-a-laptop-1", NOW) is ring.get("staff-a-laptop-1")
    assert device_key(ring, "staff-a-laptop-cli", NOW) is None


def test_a_device_key_with_several_program_keys_loads(tmp_path: Path, roster: Roster) -> None:
    third = agent(id="staff-a-laptop-ui", public_key=ed_key(5), program="agent-ui")
    assert len(load(tmp_path, roster, [agent(), _program_key(), third]).entries) == 3


def test_a_revoked_device_key_next_to_its_unrevoked_replacement_loads(tmp_path: Path, roster: Roster) -> None:
    ring = load(tmp_path, roster, [agent(revoked=PAST), _second_agent()])
    assert device_key(ring, "staff-a-laptop-1", NOW) is None
    assert device_key(ring, "staff-a-laptop-2", NOW) is ring.get("staff-a-laptop-2")


def test_a_device_whose_only_agent_entry_carries_program_has_no_device_key(
    tmp_path: Path, roster: Roster
) -> None:
    ring = load(tmp_path, roster, [_program_key()])
    assert device_key(ring, "staff-a-laptop-cli", NOW) is None


def test_two_device_keys_on_one_device_are_refused_also_next_to_a_program_key(
    tmp_path: Path, roster: Roster
) -> None:
    refused(tmp_path, roster, [agent(), _second_agent(), _program_key()], "each needs a program")


def test_a_program_key_next_to_a_service_entry_without_program_is_refused(
    tmp_path: Path, roster: Roster
) -> None:
    entries = [_program_key(), service(handle="staff-a", device="laptop-1")]
    refused(tmp_path, roster, entries, "each needs a program")


def test_a_device_key_next_to_a_service_entry_with_program_is_refused(tmp_path: Path, roster: Roster) -> None:
    entries = [agent(), service(handle="staff-a", device="laptop-1", program="guard-cli")]
    refused(tmp_path, roster, entries, "each needs a program")


@pytest.mark.parametrize(
    ("key_id", "is_device_key"),
    [
        ("staff-a-pass-1", False),
        ("example-guard-1", False),
        ("staff-a-laptop-1", True),
        ("unknown-key", False),
    ],
)
def test_only_an_agent_entry_without_program_is_a_device_key(
    tmp_path: Path, roster: Roster, key_id: str, is_device_key: bool
) -> None:
    ring = load(tmp_path, roster, [staff(), agent(), service()])
    assert (device_key(ring, key_id, NOW) is not None) == is_device_key


def test_a_device_key_revoked_later_stops_being_one_when_the_revocation_takes_effect(
    tmp_path: Path, roster: Roster
) -> None:
    ring = load(tmp_path, roster, [agent(revoked=FUTURE)])
    assert device_key(ring, "staff-a-laptop-1", NOW) is not None
    assert device_key(ring, "staff-a-laptop-1", NOW + 1) is None
