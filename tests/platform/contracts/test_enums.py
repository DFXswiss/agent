"""CON-01/CON-03: the closed lists of `contracts/enums/` equal the specification. Each table below quotes its source;
the enums and the enumerations inside the schemas must agree with them."""

from __future__ import annotations

import itertools
import re
from typing import Any

import pytest

from tests.platform.contracts.harness import CONTRACTS, Contracts, load_json

ENUMS = CONTRACTS / "enums"


def enum_file(name: str) -> dict[str, Any]:
    document: dict[str, Any] = load_json(ENUMS / name)
    return document


# 03-api.md section 11 (status per code) and 04-crypto.md section 8 ("status 400 unless listed above").
SPEC_ERRORS: dict[int, str] = {
    400: "request_invalid header_invalid params_invalid non_ascii kind_not_allowed epoch_stale invite_code_invalid"
    " ceremony_invalid client_data_invalid challenge_mismatch origin_mismatch attestation_invalid"
    " authenticator_data_invalid rp_id_mismatch user_presence_missing user_verification_missing credential_invalid"
    " statement_invalid certificate_expiry_invalid counter_regressed"
    # 04-crypto.md section 8 codes not listed in 03-api.md section 11
    " decrypt_failed prf_length recovery_code_length hpke_short invite_code_length wrap_unauthorized log_empty"
    " log_chain log_genesis log_accept log_signature log_rights log_epoch log_type invite_not_in_log invite_consumed"
    " invite_revoked author_not_writer"
    # ADR-060 (KOR-37)
    " account_code_invalid log_admit"
    # 11-agent-access.md section 4 (KOR-42, ADR-067)
    " handover_required handover_unexpected",
    401: "signature_missing key_unknown key_revoked time_skew signature_invalid nonce_replayed session_expired"
    " unauthorized"
    # 11-agent-access.md section 4 (KOR-24)
    " claim_invalid",
    403: "forbidden last_admin last_passkey device_not_assigned platform_invite_required platform_access_denied"
    " pairing_gesture_required placement_forbidden"
    # 14-community.md section 4.1 (KOR-26)
    " community_muted"
    # ADR-078 (contracts/changes/KOR-47.md)
    " identity_forbidden",
    404: "not_found invite_unknown pairing_unknown identity_unproven",
    409: "id_conflict event_conflict seq_conflict version_conflict origin_seq_gap origin_seq_conflict no_device"
    " already_assigned rotation_required no_device_available invite_registered peer_outdated"
    # 14-community.md section 4.1 (KOR-26)
    " community_not_member community_epoch_exists community_no_key placement_invalid",
    410: "invite_expired",
    413: "quota_exceeded",  # KOR-33 (ADR-072 section 6): a context checkpoint over its quota
    429: "rate_limited",
    503: "unavailable",
}
SPEC_SIZE_EXCEEDED = [400, 413]  # listed under 400 and 413 in 03-api.md section 11
SPEC_CLIENT_ONLY = "login_automation_failed prf_missing log_rollback log_fork platform_root_mismatch pairing_mismatch"
# 04-crypto.md section 8, complete (every code must be in error-codes.json).
SPEC_CRYPTO = (
    "non_ascii header_invalid signature_invalid decrypt_failed prf_length prf_missing recovery_code_length hpke_short"
    " invite_code_length invite_code_invalid event_conflict epoch_stale kind_not_allowed size_exceeded"
    " wrap_unauthorized platform_root_mismatch account_code_invalid seq_conflict log_empty log_chain log_genesis"
    " log_accept log_admit log_signature"
    " log_rights log_epoch log_type log_rollback log_fork invite_not_in_log invite_consumed invite_expired"
    " invite_revoked author_not_writer last_admin rotation_required"
)
# 03-api.md section 11.
SPEC_OPERATION_ERRORS = (
    "unknown_type params_invalid repo_not_allowed repo_not_accessible branch_protected branch_exists non_fast_forward"
    " path_invalid secret_detected size_exceeded github_not_connected not_found limit_exceeded budget_exceeded"
    " credit_exhausted"  # 12-billing.md section 6 (contracts/changes/KOR-31.md)
    " rights_denied hub_unreachable upstream_error timeout model_not_allowed tree_mismatch no_changes"
    " workvm_unavailable"  # 13-work-vm.md section 2 (contracts/changes/KOR-29.md)
    " identity_forbidden identity_unavailable"  # ADR-078 (contracts/changes/KOR-47.md)
)
SPEC_SESSION_FAILURES = (
    "model_access_unavailable vm_start_failed guest_unreachable sandbox_failed agent_start_failed agent_exited"
    " prompt_refused delivery_failed device_offline device_shutdown capacity_exhausted runtime_exceeded budget_exceeded"
    " credit_exhausted"  # 12-billing.md section 7 (contracts/changes/KOR-31.md)
    " oom unassigned"
    # KOR-40, ADR-064 section 2: the guest ended the session because a decision hook failed
    " hook_unavailable"
)
# 03-api.md section 6.1 / 02-data-model.md section 4.
SPEC_OUTBOX_KINDS = (
    "event session_transition vm_state operation_transition usage quota metrics audit wrap"
    " runtime"  # 12-billing.md section 4 (contracts/changes/KOR-31.md)
)
SPEC_OUTBOX_AUDIT = (
    "hook.denied read.session.<tool> ping.delivered session.prompt_refused model.provider_outside_allowlist"
    " runner.inbound_rejected runner.log_rollback runner.log_fork runner.alert session.context_restored"
    " session.context_fallback"
    " runner.operator_github_users"  # ADR-078 section 7 (contracts/changes/KOR-47.md)
)
# 03-api.md section 8.1: the session platform tools that read (request_operation writes).
SPEC_MCP_TOOLS = (
    "get_context list_workgroups list_members list_tasks get_task get_task_messages get_whiteboard get_notifications"
    " request_operation get_operation list_operations get_subagent_result"
)
# 02-data-model.md section 2.9 (patterns expanded by the sources named in audit-actions.json).
SPEC_AUDIT_FIXED = (
    "auth.register auth.login auth.recovery auth.passkey_add auth.passkey_remove auth.session_end auth.refused"
    " event.append task.create task.close session.control credential.put credential.delete device.enroll"
    " device.certify device.connect device.disconnect whiteboard.notify ping.delivered hook.denied"
    " session.prompt_refused model.provider_outside_allowlist runner.inbound_rejected runner.log_rollback"
    " runner.log_fork runner.alert session.context_restored session.context_fallback session.context_saved"
    " session.context_refused session.pr_linked"
)
SPEC_AUDIT_PATTERNS = {
    # 04-crypto.md section 4.7
    "log": "genesis invite invite_revoke accept role wg_create wg_archive grant ungrant device_assign device_unassign"
    " device_assign_org device_unassign_org remove rotate admit",
    # 02-data-model.md section 2.4.1
    "session": "requested vm_assigning vm_ready sandbox_ready agent_starting prompt_delivering working idle suspended"
    " resuming stopping stopped failed",
    # 02-data-model.md section 2.5
    "operation": "received rejected accepted duplicate denied executing succeeded failed expired",
    # 02-data-model.md section 2.7
    "device_request": "pending delivered answered failed expired",
    # 04-crypto.md section 4.8
    "platform": "root device_invite device_certify device_decertify role_grant role_revoke user_invite"
    " org_platform_access user_certify user_decertify platform_policy org_operator_subscriptions",  # ADR-079
}
# "invitations" of 02-data-model.md section 2.9, made concrete (decisions/CTR-01.md) and credential rotation
# (03-api.md section 4.8); the rejected outbox item of 03-api.md section 6.1 (contracts/changes/HUB-10.md).
DECIDED_AUDIT = (
    "invite.lookup invite.create invite.revoke invite.register invite.release invite.use invite.expire"
    " credential.rotate outbox.rejected agent.confirm agent.claim agent.revoke notification.read"
    # KOR-40, ADR-063: the request pipeline's rows, read repeats, terminal watching, batched session events and the
    # rows of the formerly unaudited writes.
    " request.refused request.write audit.repeat read.stream.terminal event.append_batch settings.put"
    " auth.passkey_bundle wrap.put agent.pairing"
    # 14-community.md section 4 and ADR-069 decision 5 (KOR-26)
    " community.key_create community.key_request community.key_wrap community.key_reject community.post"
    " community.delete community.mute community.unmute community.moderator_grant community.moderator_revoke"
    # KOR-62: an agent becomes a main orchestrator without a pairing (an account of an agent alone, its recovery).
    " agent.register"
    # ADR-075 section 6
    " task.placement wg.placement"
)
# 12-billing.md sections 8 and 10 (contracts/changes/KOR-31.md): credit changes, USD/CHF rates and the export.
KOR31_AUDIT = "credit.changed billing.fx_set billing.export"
# ADR-078 (contracts/changes/KOR-47.md): defaults, primary mark and the task's identity; the reads come from INDEX.json.
KOR47_AUDIT = "github.default github.primary task.github_identity runner.operator_github_users"
SPEC_TASK_STATES = "scheduling starting working idle failed closed"
# 03-api.md section 4.6.2, ADR-075 section 7: reasons of a waiting task beyond the session failure reasons
SPEC_WAITING_REASONS = "no_device platform_access_required wg_vm_limit placement_full placement_offline keys_pending"
# ADR-076 section 1 (KOR-39): category -> (order, reasons).
SPEC_ATTENTION = {
    "needs_decision": ("since_asc", "question approval connection_missing credit_empty device_missing"),
    "blocked": (
        "since_desc",
        "session_failed guest_error device_unreachable model_access_missing no_capacity operation_refused",
    ),
    "working": ("since_desc", "queued starting working operation subagents"),
    "done": ("since_desc", "idle closed"),
}
# 04-crypto.md section 4.3: (kind, scope, author kind, right).
SPEC_EVENT_MATRIX = [
    ("org.profile", "org", "user", "org_admin"),
    ("wg.profile", "org", "user", "org_admin"),
    ("member.profile", "org", "user", "org_member_self"),
    ("member.name", "org", "user", "org_admin"),
    ("device.profile", "org", "user", "org_admin"),
    ("whiteboard.entry", "org", "user", "org_admin"),
    ("whiteboard.edit", "org", "user", "org_admin"),
    ("whiteboard.delete", "org", "user", "org_admin"),
    ("whiteboard.entry", "wg", "user", "wg_write"),
    ("whiteboard.edit", "wg", "user", "wg_write"),
    ("whiteboard.delete", "wg", "user", "wg_write"),
    ("settings.workgroup", "wg", "user", "wg_write"),
    ("task.meta", "wg", "user", "wg_write"),
    ("task.meta", "wg", "device", "device_assigned_wg"),
    ("prompt", "wg", "user", "wg_write"),
    ("session.output", "wg", "device", "device_assigned_wg_session"),
    ("session.tool", "wg", "device", "device_assigned_wg_session"),
    ("session.hook", "wg", "device", "device_assigned_wg_session"),
    ("session.file", "wg", "device", "device_assigned_wg_session"),
    ("session.terminal", "wg", "device", "device_assigned_wg_session"),
    ("session.status", "wg", "device", "device_assigned_wg_session"),
    ("session.error", "wg", "device", "device_assigned_wg_session"),  # KOR-40, ADR-064 section 5
    ("operation.params", "wg", "device", "device_assigned_wg"),
    ("operation.result", "wg", "device", "device_assigned_wg"),
    ("platform.read", "wg", "device", "device_assigned_wg"),
    # 13-work-vm.md section 8 (contracts/changes/KOR-29.md)
    ("workvm.run", "wg", "device", "device_assigned_wg_session"),
    ("workvm.state", "wg", "device", "device_assigned_wg_session"),
    # ADR-078 section 1 (contracts/changes/KOR-43.md)
    ("test.run", "wg", "device", "device_assigned_wg_session"),
]
# 03-api.md section 10 (event bodies): every kind.
SPEC_EVENT_KINDS = (
    "org.profile wg.profile member.profile member.name device.profile task.meta prompt whiteboard.entry whiteboard.edit"
    " whiteboard.delete settings.workgroup session.output session.tool session.hook session.file session.terminal"
    " session.status session.error operation.params operation.result platform.read workvm.run workvm.state"
    " test.run"
)


def common_enum(contracts: Contracts, name: str) -> list[str]:
    values: list[str] = contracts.schemas["common.json"]["$defs"][name]["enum"]
    return values


def test_error_codes() -> None:
    document = enum_file("error-codes.json")
    rows = document["codes"]
    codes = [row["code"] for row in rows]
    assert len(set(codes)) == len(codes)
    expected = {code: [status] for status, text in SPEC_ERRORS.items() for code in text.split()}
    expected["size_exceeded"] = SPEC_SIZE_EXCEEDED
    expected |= {code: [] for code in SPEC_CLIENT_ONLY.split()}
    assert {row["code"]: row["statuses"] for row in rows} == expected
    for row in rows:
        assert set(row) == {"code", "statuses", "origin", "source"}
        assert row["origin"] == ("client" if row["code"] in SPEC_CLIENT_ONLY.split() else "hub")
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", row["code"])
    assert set(SPEC_CRYPTO.split()) <= set(codes)


def test_error_codes_fit_the_error_body(contracts: Contracts) -> None:
    for row in enum_file("error-codes.json")["codes"]:
        if row["origin"] == "hub":
            contracts.validate("api/error.response.json", {"error": row["code"]})


@pytest.mark.parametrize(
    ("name", "key", "spec"),
    [
        ("operation-error-codes.json", "codes", SPEC_OPERATION_ERRORS),
        ("session-failure-reasons.json", "reasons", SPEC_SESSION_FAILURES),
        ("outbox-item-kinds.json", "kinds", SPEC_OUTBOX_KINDS),
    ],
)
def test_closed_lists(name: str, key: str, spec: str) -> None:
    document = enum_file(name)
    assert set(document) == {"description", key}
    assert document[key] == spec.split()


def read_tools() -> list[str]:
    return [tool for tool in SPEC_MCP_TOOLS.split() if tool != "request_operation"]


def expand(spec: str) -> list[str]:
    out: list[str] = []
    for action in spec.split():
        if action == "read.session.<tool>":
            out += [f"read.session.{tool}" for tool in read_tools()]
        else:
            out.append(action)
    return out


def test_outbox_audit_actions() -> None:
    document = enum_file("outbox-audit-actions.json")
    assert document["actions"] == expand(SPEC_OUTBOX_AUDIT)


def test_audit_actions(index: dict[str, Any]) -> None:
    document = enum_file("audit-actions.json")
    actions = [row["action"] for row in document["actions"]]
    assert len(set(actions)) == len(actions)
    for row in document["actions"]:
        assert set(row) == {"action", "group"}
        assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", row["action"]), row
    expected = set(SPEC_AUDIT_FIXED.split()) | set(DECIDED_AUDIT.split()) | set(KOR31_AUDIT.split())
    expected |= set(KOR47_AUDIT.split())
    for prefix, values in SPEC_AUDIT_PATTERNS.items():
        expected |= {f"{prefix}.{value}" for value in values.split()}
    expected |= {f"read.{e['read_resource']}" for e in index["endpoints"] if e["read_resource"]}
    expected |= {f"read.session.{tool}" for tool in read_tools()}
    assert set(actions) == expected
    assert set(enum_file("outbox-audit-actions.json")["actions"]) <= set(actions)


def test_session_states(contracts: Contracts) -> None:
    document = enum_file("session-states.json")
    states = SPEC_AUDIT_PATTERNS["session"].split()
    assert document["states"] == states
    assert common_enum(contracts, "session_state") == states
    assert document["initial"] == "requested"
    assert document["terminal"] == ["stopped", "failed"]
    assert document["milestones"] == {"ready_at": "prompt_delivering", "first_working_at": "working"}
    transitions = document["transitions"]
    assert list(transitions) == states
    chain = states[:7]
    for before, after in itertools.pairwise(chain):
        assert after in transitions[before]
    assert {"idle"} <= set(transitions["working"])
    assert {"working", "suspended"} <= set(transitions["idle"])
    assert transitions["suspended"][0] == "resuming"
    assert {"working", "suspended"} <= set(transitions["resuming"])
    assert transitions["stopping"] == ["stopped", "failed"]
    for state in states:
        assert set(transitions[state]) <= set(states)
        if state in ("stopped", "failed"):
            assert transitions[state] == []
        else:
            assert "failed" in transitions[state]


def test_task_states(contracts: Contracts) -> None:
    document = enum_file("task-states.json")
    assert document["states"] == SPEC_TASK_STATES.split()
    assert common_enum(contracts, "task_state") == SPEC_TASK_STATES.split()
    assert document["terminal"] == ["closed"]
    assert set(document["scheduling_reasons"]) <= set(SPEC_SESSION_FAILURES.split()) | set(SPEC_WAITING_REASONS.split())
    assert common_enum(contracts, "task_waiting_reason") == SPEC_WAITING_REASONS.split()
    assert set(document["scheduling_categories"]) == set(document["scheduling_reasons"])
    assert set(document["scheduling_categories"].values()) == {
        "waiting_for_capacity",
        "waiting_for_device",
        "waiting_for_credit",
    }


def test_operation_and_request_states(contracts: Contracts) -> None:
    assert common_enum(contracts, "operation_state") == SPEC_AUDIT_PATTERNS["operation"].split()
    assert common_enum(contracts, "request_state") == SPEC_AUDIT_PATTERNS["device_request"].split()
    defs = contracts.schemas["common.json"]["$defs"]
    log_types = [schema["properties"]["type"]["const"] for name, schema in defs.items() if name.startswith("log_body_")]
    assert sorted(set(log_types)) == sorted(SPEC_AUDIT_PATTERNS["log"].split())
    platform_types = [
        schema["properties"]["type"]["const"] for name, schema in defs.items() if name.startswith("platform_body_")
    ]
    assert platform_types == SPEC_AUDIT_PATTERNS["platform"].split()


def test_schema_enums_equal_the_closed_lists(contracts: Contracts) -> None:
    """The schemas reference the closed lists (03-api.md section 11) through enums of common.json."""
    hub = [row["code"] for row in enum_file("error-codes.json")["codes"] if row["origin"] == "hub"]
    assert common_enum(contracts, "hub_error_code") == hub
    assert common_enum(contracts, "operation_error_code") == enum_file("operation-error-codes.json")["codes"]
    assert common_enum(contracts, "session_failure_reason") == enum_file("session-failure-reasons.json")["reasons"]
    assert sorted(common_enum(contracts, "event_kind")) == sorted(SPEC_EVENT_KINDS.split())


def test_event_kind_matrix(contracts: Contracts) -> None:
    document = enum_file("event-kinds.json")
    rows = document["kinds"]
    matrix = [(row["kind"], row["scope"], author, row["right"]) for row in rows for author in row["authors"]]
    assert sorted(matrix) == sorted(SPEC_EVENT_MATRIX)
    assert set(document["rights"]) == {right for *_, right in SPEC_EVENT_MATRIX}
    assert document["notify_allowed"] == ["whiteboard.entry"]
    assert document["size_limits"] == {"default_ct_bytes": 262144, "session.terminal": 65536}
    kinds = sorted({row["kind"] for row in rows})
    assert kinds == sorted(SPEC_EVENT_KINDS.split())
    assert sorted(common_enum(contracts, "event_kind")) == kinds
    rules = {"empty", "set", "scope_id", "target", "author", "device", "entry", "operation", "read_target", "subject"}
    for row in rows:
        assert set(row) <= {"kind", "scope", "authors", "right", "header", "note"}
        assert set(row["header"]) <= {"wg", "task", "session", "ref"}
        assert set(row["header"].values()) <= rules
    by_kind = {(row["kind"], row["scope"], tuple(row["authors"])): row["header"] for row in rows}
    # 03-api.md section 10: task.meta and prompt address the task (session="" and ref="").
    for kind in ("task.meta", "prompt"):
        assert by_kind[(kind, "wg", ("user",))]["session"] == "empty"
        assert by_kind[(kind, "wg", ("user",))]["ref"] == "empty"
    # 04-crypto.md section 4.3: wg.profile names its target in wg; member.profile ref = author;
    # device.profile ref = device.
    assert by_kind[("wg.profile", "org", ("user",))]["wg"] == "target"
    assert by_kind[("member.profile", "org", ("user",))]["ref"] == "author"
    assert by_kind[("device.profile", "org", ("user",))]["ref"] == "device"
    # ADR-073: member.name names a user or a user invitation of the org at lh
    assert by_kind[("member.name", "org", ("user",))]["ref"] == "subject"
    assert by_kind[("operation.result", "wg", ("device",))]["ref"] == "operation"
    for row in rows:
        if row["scope"] == "org" and row["kind"] != "wg.profile":
            assert row["header"]["wg"] == "empty", row


def test_attention_categories(contracts: Contracts) -> None:
    document = enum_file("attention.json")
    assert set(document) == {"description", "categories"}
    rows = document["categories"]
    for row in rows:
        assert set(row) == {"category", "order", "reasons"}, row
    assert {row["category"]: (row["order"], row["reasons"]) for row in rows} == {
        category: (order, reasons.split()) for category, (order, reasons) in SPEC_ATTENTION.items()
    }
    assert [row["category"] for row in rows] == list(SPEC_ATTENTION)
    assert common_enum(contracts, "attention_category") == list(SPEC_ATTENTION)
    row_schema = contracts.schemas["api/objects.json"]["$defs"]["attention_row"]
    assert row_schema["required"] == list(row_schema["properties"])
    for row in rows:
        for reason in row["reasons"]:
            assert check_reason(contracts, reason), reason


def check_reason(contracts: Contracts, reason: str) -> bool:
    pattern: str = contracts.schemas["common.json"]["$defs"]["attention_reason"]["pattern"]
    return re.fullmatch(pattern.removeprefix("^").removesuffix("$"), reason) is not None


def test_enum_files_are_described() -> None:
    names = sorted(path.name for path in ENUMS.glob("*.json"))
    assert names == [
        "attention.json",
        "audit-actions.json",
        "error-codes.json",
        "event-kinds.json",
        "operation-error-codes.json",
        "outbox-audit-actions.json",
        "outbox-item-kinds.json",
        "session-failure-reasons.json",
        "session-states.json",
        "task-states.json",
    ]
    for name in names:
        assert enum_file(name)["description"]
