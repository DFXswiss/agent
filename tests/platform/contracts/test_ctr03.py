"""CON-01/CON-03 (devices, events, logs): the contracts of CTR-03 are complete against the specification tables and
consistent with the closed lists of CTR-01 and the guest, MCP, operation and audit contracts of CTR-02.

Quoted tables: 03-api.md sections 5, 6.1, 6.2, 10; 04-crypto.md sections 4.4, 4.5, 4.7, 4.8. The crypto vectors of
CRY-01 (`contracts/vectors/`) must be valid instances of these schemas.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_core.passkey.certificate import build_statement
from tests.platform.contracts.harness import CONTRACTS, ID_BASE, Contracts, StrictValidator, load_json

# 03-api.md section 6.2 (hints from hub to device, in the order of the specification).
SPEC_HINTS = (
    "hello reconcile session.start session.control prompt events log wraps credentials request ping decertified"
    " browser"  # 13-work-vm.md section 13.6 (KOR-44)
)
# 03-api.md section 5 (frames of GET /v1/stream).
# 03-api.md section 5 plus `community` of 14-community.md section 5 (KOR-26) and `attention` of section 4.6.0
# (KOR-39).
SPEC_SSE_FRAMES = (
    "hello event terminal task session operation log wraps notification device_response metrics community resync"
    " attention"
    " browser_action browser_live"  # 13-work-vm.md section 13.6 (KOR-44)
)
# 04-crypto.md section 4.7 (membership-log entry types) and section 4.8 (platform-log entry types).
SPEC_MEMBERSHIP_TYPES = (
    "genesis invite invite_revoke accept role wg_create wg_archive grant ungrant device_assign device_unassign"
    " device_assign_org device_unassign_org remove rotate admit agent_add"
)
SPEC_PLATFORM_TYPES = (
    "root device_invite device_certify device_decertify role_grant role_revoke user_invite org_platform_access"
    " user_certify user_decertify platform_policy org_operator_subscriptions"  # ADR-079
)
# 04-crypto.md section 4.4 and 02-data-model.md section 2.7 (device_request.kind).
SPEC_CREDENTIAL_KINDS = "github anthropic_api_key claude_subscription mailbox"
SPEC_REQUEST_KINDS = "github.connect claude.login credential.check mailbox.check github.pull_status"
# 03-api.md section 6.1: the device endpoints (CTR-03 rows of INDEX.json) and the two live channels.
SPEC_CHANNELS = {
    "/v1/stream": ("ws/sse/query.json", "ws/sse/frame.json"),
    "/v1/device/ws": ("ws/device/query.json", "ws/device/hint.json"),
}
SPEC_LOG_FILES = {"log/invitation-package.json", "log/session-statement.json"}


def enum_file(name: str) -> dict[str, Any]:
    document: dict[str, Any] = load_json(CONTRACTS / "enums" / name)
    return document


def defs(contracts: Contracts, rel: str) -> dict[str, Any]:
    found: dict[str, Any] = contracts.schemas[rel]["$defs"]
    return found


def def_errors(contracts: Contracts, target: str, instance: Any) -> list[str]:
    """Validation errors of `instance` against `<file>#/$defs/<name>` (contract-relative)."""
    validator = StrictValidator({"$ref": ID_BASE + target}, registry=contracts.registry)
    return [error.message for error in validator.iter_errors(instance)]


def consts(variants: list[dict[str, Any]], key: str, contracts: Contracts, rel: str) -> list[str]:
    """The `const` of `key` in every variant (variants may be local `$ref`s)."""
    found = []
    for variant in variants:
        schema = contracts.resolve(rel, variant["$ref"]) if "$ref" in variant else variant
        assert isinstance(schema, dict)
        if "oneOf" in schema:
            found.extend(sorted(set(consts(schema["oneOf"], key, contracts, rel))))
            continue
        found.append(schema["properties"][key]["const"])
    return found


def schema_names(directory: str) -> set[str]:
    return {p.relative_to(CONTRACTS).as_posix() for p in (CONTRACTS / directory).rglob("*.json")}


# ------------------------------------------------------------------------------------------------ INDEX.json


def test_device_rows_are_complete(index: dict[str, Any], contracts: Contracts) -> None:
    assert "CTR-03" not in index["pending_owners"]
    rows = [e for e in index["endpoints"] if e["owner"] == "CTR-03"]
    later = {e["path"] for e in index["endpoints"] if e["owner"] in ("KOR-33", "KOR-44")}
    assert {e["path"] for e in rows} | later >= {
        e["path"] for e in index["endpoints"] if e["path"].startswith("/v1/device/")
    }
    assert len(rows) == 20  # KOR-47: list, GET and PUT of /v1/device/github-identities
    for e in rows:
        assert e["request"] in contracts.schemas, e
        assert e["response"] in contracts.schemas, e
        if e["streaming"]:
            assert (e["request"], e["response"]) == SPEC_CHANNELS[e["path"]]
    on_disk = {p for p in schema_names("api") if p.startswith("api/device-")}
    # later packages add device rows of their own (KOR-33: the context checkpoints and pull-request links)
    device_rows = [e for e in index["endpoints"] if e["name"].startswith("device-")]
    assert on_disk == {e[part] for e in device_rows if not e["streaming"] for part in ("request", "response")}


def test_live_channel_index(contracts: Contracts) -> None:
    channels = load_json(CONTRACTS / "ws" / "INDEX.json")["channels"]
    assert [c["path"] for c in channels] == list(SPEC_CHANNELS)
    sse, device = channels
    assert [f["type"] for f in sse["from_hub"]] == SPEC_SSE_FRAMES.split()
    assert [f["type"] for f in device["from_hub"]] == SPEC_HINTS.split()
    assert [f["type"] for f in device["from_client"]] == ["heartbeat"]
    assert sse["from_client"] == []
    for channel in channels:
        assert (channel["query"], channel["frames"]) == SPEC_CHANNELS[channel["path"]]
        for frame in channel["from_hub"] + channel["from_client"]:
            assert frame["data"] in contracts.schemas, frame
    listed = {f["data"] for c in channels for f in c["from_hub"] + c["from_client"]}
    listed |= {rel for pair in SPEC_CHANNELS.values() for rel in pair}
    assert {rel for rel in contracts.schemas if rel.startswith("ws/") and not rel.endswith("objects.json")} == listed


def test_hint_and_frame_unions(contracts: Contracts) -> None:
    device = defs(contracts, "ws/device/objects.json")
    assert consts(device["hint"]["oneOf"], "type", contracts, "ws/device/objects.json") == SPEC_HINTS.split()
    sse = defs(contracts, "ws/sse/objects.json")
    assert consts(sse["frame"]["oneOf"], "event", contracts, "ws/sse/objects.json") == SPEC_SSE_FRAMES.split()


@pytest.mark.parametrize("hint", SPEC_HINTS.split())
def test_hints_carry_no_content(contracts: Contracts, hint: str) -> None:
    """03-api.md section 6.2: a hint carries IDs and sequence numbers only."""
    allowed = {
        "type",
        "hub_time",
        "session_id",
        "action",
        "scope_id",
        "scope_seq",
        "org_id",
        "seq",
        "owner_id",
        "request_id",
        "wg_id",
        "task_id",
        "level",
        "entry_id",
        "via_agent",  # a flag of the prompt hint (ADR-083), no content
    }
    definition = defs(contracts, "ws/device/objects.json")[f"hint_{hint.replace('.', '_')}"]
    for variant in definition.get("oneOf", [definition]):
        assert set(variant["properties"]) <= allowed, hint


# ------------------------------------------------------------------------------------------------ events


def test_event_kinds_table(contracts: Contracts) -> None:
    matrix = enum_file("event-kinds.json")
    table = load_json(CONTRACTS / "events" / "kinds.json")["kinds"]
    kinds = [row["kind"] for row in table]
    assert sorted(kinds) == sorted({row["kind"] for row in matrix["kinds"]})
    assert len(kinds) == len(set(kinds))
    for row in table:
        rows = [m for m in matrix["kinds"] if m["kind"] == row["kind"]]
        assert row["scopes"] == sorted({m["scope"] for m in rows}), row
        assert row["authors"] == sorted({a for m in rows for a in m["authors"]}), row
        limits = matrix["size_limits"]
        assert row["max_ct_bytes"] == limits.get(row["kind"], limits["default_ct_bytes"]), row
        # devices pull every kind but the watched kinds and the admin-given names (ADR-073: the sessions name members
        # from their own profiles; 13-work-vm.md section 13.6: the watched kinds)
        assert row["device_pull"] is (row["kind"] not in {"session.terminal", "browser.action", "member.name"}), row
        assert row["body"] == f"events/{row['kind']}.json"
        assert row["body"] in contracts.schemas
    bodies = schema_names("events") - {"events/objects.json", "events/kinds.json"}
    assert bodies == {row["body"] for row in table}


def test_event_header_variants(contracts: Contracts) -> None:
    """The kind-restricted headers of events/objects.json differ from common.json only in `kind` and, since every
    device-authored kind is workgroup-scoped (04-crypto.md section 4.3), in `scope` = "wg" and a set `wg`."""
    common = defs(contracts, "common.json")
    events = defs(contracts, "events/objects.json")
    base = common["event_header"]
    matrix = enum_file("event-kinds.json")["kinds"]
    device_kinds = sorted({m["kind"] for m in matrix if "device" in m["authors"]} - {"session.terminal"})
    assert {m["scope"] for m in matrix if "device" in m["authors"]} == {"wg"}
    for name, kinds in (("header_device", device_kinds), ("header_operation_params", ["operation.params"])):
        header = events[name]
        assert header["required"] == base["required"], name
        assert set(header["properties"]) == set(base["properties"]), name
        for key, prop in header["properties"].items():
            if key == "kind":
                assert sorted(prop.get("enum", [prop.get("const")])) == kinds, name
            elif key == "scope":
                assert prop == {"type": "string", "const": "wg"}, name
            elif key == "wg":
                assert prop == {"$ref": "../common.json#/$defs/uuid"}, name
            else:
                expected = dict(base["properties"][key])
                if "$ref" in expected:
                    expected["$ref"] = "../common.json" + expected["$ref"]
                assert prop == expected, (name, key)
    other = common["event_header_other"]["properties"]["kind"]["enum"]
    assert sorted(other) == sorted({m["kind"] for m in matrix} - {"session.terminal"})


@pytest.mark.parametrize("kind", ["session.output", "session.tool", "session.hook", "session.file", "session.terminal"])
def test_session_bodies_are_the_journal_bodies(contracts: Contracts, kind: str) -> None:
    """03-api.md section 10: session.* event bodies are the journal bodies of section 7.2 (CTR-02)."""
    name = f"body_{kind.replace('.', '_')}"
    # session.hook: one call or the summary of routine calls (contracts/changes/KOR-35.md)
    target = f"{name}_record" if kind == "session.hook" else name
    assert defs(contracts, "events/objects.json")[name] == {"$ref": f"../guest/journal/record.json#/$defs/{target}"}


def test_operation_bodies_cover_every_type_and_read_tool(contracts: Contracts) -> None:
    events = defs(contracts, "events/objects.json")
    types = [t["type"] for t in load_json(CONTRACTS / "ops" / "INDEX.json")["types"]]
    params = events["body_operation_params"]["oneOf"]
    assert [v["properties"]["type"]["const"] for v in params] == types
    for variant in params:
        name = variant["properties"]["type"]["const"].replace(".", "_")
        assert variant["properties"]["params"] == {"$ref": f"../ops/objects.json#/$defs/params_{name}"}
    tools = [
        t["tool"]
        for t in load_json(CONTRACTS / "mcp" / "INDEX.json")["tools"]
        if t["via"] == "platform.query" or t["via"] == "local"
    ]
    reads = events["body_platform_read"]["oneOf"]
    assert [v["properties"]["tool"]["const"] for v in reads] == tools
    for variant in reads:
        tool = variant["properties"]["tool"]["const"]
        assert variant["properties"]["arguments"] == {"$ref": f"../mcp/objects.json#/$defs/input_{tool}"}


# ------------------------------------------------------------------------------------------------ outbox


def test_outbox_kinds(contracts: Contracts) -> None:
    kinds = enum_file("outbox-item-kinds.json")["kinds"]
    outbox = defs(contracts, "outbox/objects.json")
    assert [v["properties"]["item_kind"]["const"] for v in outbox["item"]["oneOf"]] == kinds
    for variant in outbox["item"]["oneOf"]:
        kind = variant["properties"]["item_kind"]["const"]
        assert variant["properties"]["body"] == {"$ref": f"#/$defs/body_{kind}"}
    assert schema_names("outbox") == {f"outbox/{k}.json" for k in kinds} | {"outbox/objects.json", "outbox/item.json"}
    for variant in outbox["body_audit"]["oneOf"]:
        result = variant["properties"]["result"]["enum"]
        assert set(result) == {"ok", "denied", "rejected", "duplicate", "failed"}


def test_outbox_audit_details_match_the_audit_schemas(contracts: Contracts) -> None:
    """The runner's audit item inlines the detail schema `audit/<action>.json` of CTR-02 per action."""
    actions = enum_file("outbox-audit-actions.json")["actions"]
    variants = defs(contracts, "outbox/objects.json")["body_audit"]["oneOf"]
    assert [v["properties"]["action"]["const"] for v in variants] == actions
    for action, variant in zip(actions, variants, strict=True):
        source = {k: v for k, v in contracts.schemas[f"audit/{action}.json"].items() if k not in DOC_KEYS}
        assert variant["properties"]["detail"] == rebased(source, "audit", "outbox"), action
    samples = sorted((CONTRACTS / "fixtures" / "outbox" / "audit" / "valid").glob("*.json"))
    seen = set()
    for path in samples:
        item = load_json(path)
        contracts.validate(f"audit/{item['action']}.json", item["detail"])
        seen.add(item["action"])
    assert seen == set(actions)


DOC_KEYS = {"$schema", "$id", "title"}


def rebased(node: Any, source_dir: str, target_dir: str) -> Any:
    """`node` with every file reference rewritten from `source_dir` to `target_dir` (both under contracts/)."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "$ref":
                file, fragment = value.split("#", 1)
                target = posixpath.normpath(posixpath.join(source_dir, file))
                out[key] = posixpath.relpath(target, target_dir) + "#" + fragment
            else:
                out[key] = rebased(value, source_dir, target_dir)
        return out
    if isinstance(node, list):
        return [rebased(value, source_dir, target_dir) for value in node]
    return node


def test_rebased() -> None:
    assert rebased({"a": [{"$ref": "../common.json#/$defs/uuid"}]}, "audit", "outbox") == {
        "a": [{"$ref": "../common.json#/$defs/uuid"}]
    }
    assert rebased({"$ref": "read.session.json#/$defs/x"}, "audit", "outbox") == {
        "$ref": "../audit/read.session.json#/$defs/x"
    }


def test_closed_lists_are_not_copied_with_drift(contracts: Contracts) -> None:
    """Inline copies of the session and VM states equal their sources (02-data-model.md sections 2.4, 4)."""
    states = defs(contracts, "common.json")["session_state"]["enum"]
    non_failed = [state for state in states if state != "failed"]
    outbox = defs(contracts, "outbox/objects.json")
    assert outbox["body_session_transition"]["oneOf"][0]["properties"]["state"]["enum"] == non_failed
    status = defs(contracts, "events/objects.json")["body_session_status"]["oneOf"][1]
    assert status["properties"]["state"]["enum"] == non_failed
    vm_states = ["pool", "assigning", "running", "stopping", "stopped", "deleted"]
    assert outbox["body_vm_state"]["properties"]["state"]["enum"] == vm_states
    heartbeat = defs(contracts, "ws/device/objects.json")["heartbeat"]
    assert heartbeat["properties"]["vms"]["items"]["properties"]["state"]["enum"] == vm_states


def test_outbox_metric_samples_extend_the_row(contracts: Contracts) -> None:
    sample = defs(contracts, "outbox/objects.json")["metric_sample"]
    row = defs(contracts, "api/objects.json")["metric_sample"]
    # `session_id`: work VM samples only (KOR-29), optional in the schema
    assert set(sample["properties"]) == set(row["properties"]) | {"entity_kind", "entity_id", "session_id"}
    assert sample["required"] == ["entity_kind", "entity_id", *row["required"]]


# ------------------------------------------------------------------------------------------------ bodies, logs


def test_credential_and_request_bodies(contracts: Contracts) -> None:
    common = defs(contracts, "common.json")
    assert common["credential_kind"]["enum"] == SPEC_CREDENTIAL_KINDS.split()
    assert common["request_kind"]["enum"] == SPEC_REQUEST_KINDS.split()
    assert schema_names("credentials") == {f"credentials/{k}.json" for k in SPEC_CREDENTIAL_KINDS.split()}
    assert schema_names("requests") == {
        f"requests/{k}.{part}.json" for k in SPEC_REQUEST_KINDS.split() for part in ("request", "response")
    } | {"requests/objects.json"}
    client_only = [row["code"] for row in enum_file("error-codes.json")["codes"] if row["origin"] != "hub"]
    assert not set(defs(contracts, "requests/objects.json")["failure_code"]["enum"]) & set(client_only)


def test_log_entries_per_type(contracts: Contracts) -> None:
    common = defs(contracts, "common.json")
    membership = SPEC_MEMBERSHIP_TYPES.split()
    platform = SPEC_PLATFORM_TYPES.split()
    expected = {f"log/membership/{t}.json" for t in membership} | {f"log/platform/{t}.json" for t in platform}
    assert schema_names("log") == expected | SPEC_LOG_FILES
    for t in membership:
        schema = contracts.schemas[f"log/membership/{t}.json"]
        body = schema["properties"]["body"]
        refs = [alt["$ref"] for alt in body["anyOf"]] if "anyOf" in body else [body["$ref"]]
        names = ["log_body_invite_user", "log_body_invite_device"] if t == "invite" else [f"log_body_{t}"]
        assert refs == [f"../../common.json#/$defs/{n}" for n in names], t
        for name in names:
            assert common[name]["properties"]["type"]["const"] == t
        assert ("inv_sig" in schema["required"]) is (t == "accept"), t
    for t in platform:
        schema = contracts.schemas[f"log/platform/{t}.json"]
        assert schema["properties"]["body"] == {"$ref": f"../../common.json#/$defs/platform_body_{t}"}
        assert common[f"platform_body_{t}"]["properties"]["type"]["const"] == t
        assert schema["required"] == ["body", "sig"]


def test_log_vectors_fit_the_entry_schemas(contracts: Contracts) -> None:
    """CON-02 (KOR-07, AUD-01 B-22): every entry of the valid logs of the CRY-02 vectors validates against its entry
    schema of `contracts/log/` (membership logs against `log/membership/<type>.json`, platform logs against
    `log/platform/<type>.json`), and together they use every entry type."""
    seen: set[str] = set()
    platform = set(SPEC_PLATFORM_TYPES.split())
    files = [path for path in sorted(VECTORS.glob("*.json")) if "logs" in load_json(path)]
    assert len(files) >= 5
    for path in files:
        for log_name, entries in load_json(path)["logs"].items():
            for index, entry in enumerate(entries):
                kind = entry["body"]["type"]
                rel = f"log/{'platform' if kind in platform else 'membership'}/{kind}.json"
                assert rel in contracts.schemas, (path.name, log_name, index)
                assert contracts.errors(rel, entry) == [], (path.name, log_name, index)
                seen.add(rel)
    assert seen == schema_names("log") - SPEC_LOG_FILES
    # the check bites: a vector entry with a field the contract does not know, or without its signature, is refused
    entry = load_json(CONTRACTS / "vectors" / "log.json")["logs"]["main"][0]
    rel = f"log/membership/{entry['body']['type']}.json"
    assert contracts.errors(rel, {**entry, "note": "x"})
    assert contracts.errors(rel, {key: value for key, value in entry.items() if key != "sig"})


# ------------------------------------------------------------------------------------------------ vectors (CRY-01)

VECTORS = CONTRACTS / "vectors"


def vector(name: str) -> dict[str, Any]:
    document: dict[str, Any] = load_json(VECTORS / name)
    return document


def json_text(text: str) -> object:
    return json.loads(text)


def test_event_vectors(contracts: Contracts) -> None:
    events = vector("event.json")["events"]
    assert events
    for item in events:
        assert def_errors(contracts, "common.json#/$defs/event", item["event"]) == []
        kind = item["event"]["header"]["kind"]
        contracts.validate(f"events/{kind}.json", json_text(item["body_json"]))


def test_credential_vector(contracts: Contracts) -> None:
    document = vector("credential.json")
    assert def_errors(contracts, "common.json#/$defs/credential_record", document["record"]) == []
    contracts.validate(f"credentials/{document['record']['header']['kind']}.json", json_text(document["body_json"]))


def test_request_vectors(contracts: Contracts) -> None:
    document = vector("request.json")
    for part, envelope in (("request", "device_request_envelope"), ("response", "device_response_envelope")):
        items = document[f"{part}s"]
        assert items
        for item in items:
            assert def_errors(contracts, f"common.json#/$defs/{envelope}", item["envelope"]) == []
            kind = item["envelope"]["header"]["kind"]
            contracts.validate(f"requests/{kind}.{part}.json", json_text(item["body_json"]))


def test_invitation_package_vector(contracts: Contracts) -> None:
    document = vector("invite.json")
    contracts.validate("log/invitation-package.json", document["package"])
    assert json_text(document["package_body_json"]) == document["package"]


def test_enrollment_vector(contracts: Contracts) -> None:
    contracts.validate("api/device-enroll.request.json", vector("enroll.json")["body"])


def test_dedupe_vectors(contracts: Contracts) -> None:
    tags = vector("dedupe.json")["tags"]
    assert tags
    for item in tags:
        dedupe = {"epoch": 1, "tag": item["tag"]}
        request = load_json(CONTRACTS / "fixtures" / "api" / "device-operations.request" / "valid" / "basic.json")
        contracts.validate("api/device-operations.request.json", {**request, "dedupe": dedupe, "type": item["type"]})


def test_browser_frame_vector(contracts: Contracts) -> None:
    """04-crypto.md section 4.13: the frame of the vectors is a valid body of the live route and of the stream frame;
    the schema's ct limits are the b64u lengths of 29 bytes and 512 KiB + 28 bytes."""
    document = vector("browserframe.json")
    frame = document["frame"]
    contracts.validate("api/device-browser-live.request.json", {"frame": frame})
    contracts.validate("ws/sse/browser_live.json", {"session_id": frame["session"], "frame": frame})
    ct = defs(contracts, "common.json")["browser_frame"]["properties"]["ct"]
    limits = document["limits"]
    assert ct["minLength"] == -(-4 * limits["frame_ct_min"] // 3)
    assert ct["maxLength"] == -(-4 * limits["frame_ct_max"] // 3)
    for case in document["invalid"]:
        if case["op"] == "check_frame" and case["error"] != "non_ascii":
            assert def_errors(contracts, "common.json#/$defs/browser_frame", case["args"]["frame"]), case["name"]


def test_statement_of_the_reference_is_valid(contracts: Contracts) -> None:
    key = ec.derive_private_key(7, ec.SECP256R1()).public_key()
    point = key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    for origin in ("https://agent.dfx.swiss", "http://localhost:8788"):
        text = build_statement(origin=origin, session_public_key=point, hub_value=bytes(32), expires=1_795_000_000)
        contracts.validate("log/session-statement.json", text.decode())


def test_def_errors_reports_messages(contracts: Contracts) -> None:
    assert def_errors(contracts, "common.json#/$defs/uuid", "x")
    assert def_errors(contracts, "common.json#/$defs/uuid", "01930001-0000-7000-8000-000000000001") == []


def test_vector_files_exist() -> None:
    assert {p.name for p in VECTORS.glob("*.json")} >= {
        "event.json",
        "credential.json",
        "request.json",
        "invite.json",
        "enroll.json",
    }
