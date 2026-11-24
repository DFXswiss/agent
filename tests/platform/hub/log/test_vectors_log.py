"""CON-02 (log part) and CON-03: the log vector files are current, every negative vector fails with exactly its code,
the positive parts are reproduced, and the event kind matrix equals contracts/enums/event-kinds.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.crypto import vectors as crypto_vectors
from agent_hub.crypto.encoding import parse_time
from agent_hub.crypto.errors import CODES, CryptoError
from agent_hub.log import catchup, events, platform, rotation, verify, wraps
from agent_hub.log import vectors as log_vectors
from agent_hub.log.batch import BatchResult
from agent_hub.log.entries import PLATFORM_TYPES, Head, check_extends

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = REPO_ROOT / "contracts"
VECTORS = CONTRACTS / "vectors"
FILES = ("log", "log-rotation", "log-wraps", "log-events", "platform-log")


def load(name: str) -> Any:
    return json.loads((VECTORS / f"{name}.json").read_text(encoding="utf-8"))


def context(data: dict[str, Any]) -> log_vectors.Context:
    return log_vectors.Context(data["logs"])


def all_invalid() -> list[tuple[str, dict[str, Any]]]:
    return [(name, case) for name in FILES for case in load(name)["invalid"]]


def test_log_sections_are_discovered_and_current() -> None:
    sections = crypto_vectors.discover(CONTRACTS / "tools")
    names = [section.name for section in sections]
    for name in FILES:
        assert name in names
    assert crypto_vectors.stale(VECTORS, [s for s in sections if s.name in FILES]) == []


def test_log_generator_is_deterministic() -> None:
    for section in log_vectors.SECTIONS:
        assert crypto_vectors.render(section) == (VECTORS / f"{section.name}.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(("file", "case"), all_invalid(), ids=lambda value: value if isinstance(value, str) else "")
def test_con02_log_negative_vectors(file: str, case: dict[str, Any]) -> None:
    assert case["error"] in CODES
    with pytest.raises(CryptoError) as refused:
        context(load(file)).run(case["op"], case["args"])
    assert refused.value.code == case["error"]
    assert refused.value.__context__ is None


def test_con02_every_log_code_has_a_vector() -> None:
    codes = {case["error"] for _, case in all_invalid()}
    log_codes = {
        "seq_conflict",
        "log_empty",
        "log_chain",
        "log_genesis",
        "log_accept",
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
        "wrap_unauthorized",
        "kind_not_allowed",
        "epoch_stale",
        "event_conflict",
        "platform_root_mismatch",
    }
    assert log_codes <= codes


def test_con02_one_broken_variant_per_rule() -> None:
    names = {case["name"] for case in load("log")["invalid"]}
    for rule in ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10"):
        assert any(name.startswith(rule + "_") for name in names), rule
    platform_names = {case["name"] for case in load("platform-log")["invalid"]}
    for rule in ("P1", "P2", "P3", "P4"):
        assert any(name.startswith(rule + "_") for name in platform_names), rule


def test_con02_log_valid_chain_and_projections() -> None:
    data = load("log")
    ctx = context(data)
    log = ctx.log({**data["valid"], "founder": data["founder"]})
    assert [{"seq": s.seq, "hash": s.head} for s in log.states] == data["valid"]["heads"]
    assert [s.projection() for s in log.states] == data["valid"]["projections"]
    types = {cast(dict[str, Any], entry["body"])["type"] for entry in log.entries}
    assert types == {
        "genesis",
        "invite",
        "invite_revoke",
        "accept",
        "role",
        "wg_create",
        "wg_archive",
        "grant",
        "ungrant",
        "device_assign",
        "device_unassign",
        "remove",
        "rotate",
    }
    for case in data["extends"]:
        check_extends(Head(case["head"]["seq"], case["head"]["hash"]), ctx.entries(case))
    for case in data["excluded"]:
        state = ctx.platform({"platform": case["platform"]})
        assert state is not None
        assert sorted(state.excluded(ctx.log({**case, "founder": data["founder"]}).state)) == case["excluded"]
    for case in data["lh"]["needs_refresh"]:
        assert verify.needs_refresh(log, case["lh"]) is case["refresh"]


def test_orgwide_log_assigns_every_workgroup() -> None:
    """ADR-060: a device assigned to the organization reads every workgroup, also a later one; ending the
    organization assignment keeps its explicit assignment; `remove` ends both."""
    data = load("log")
    ctx = context(data)
    orgwide = data["orgwide"]
    log = ctx.log({**orgwide, "founder": data["founder"]})
    assert [{"seq": s.seq, "hash": s.head} for s in log.states] == orgwide["heads"]
    assert [s.projection() for s in log.states] == orgwide["projections"]
    assert [
        {device: s.device_wgs(device) for device in expected}
        for s, expected in zip(log.states, orgwide["device_wgs"], strict=True)
    ] == orgwide["device_wgs"]
    types = {cast(dict[str, Any], entry["body"])["type"] for entry in log.entries}
    assert {"device_assign_org", "device_unassign_org"} <= types
    after_create = log.states[5]
    device = str(cast(dict[str, Any], log.entries[4]["body"])["device"])
    assert after_create.org_assigned(device)
    assert after_create.can_read(device, "wg", after_create.default_wg)
    assert not log.states[6].org_assigned(device)


def test_con02_platform_log_valid() -> None:
    data = load("platform-log")
    log = platform.verify_platform_log(data["logs"]["platform"], data["root"])
    assert [{"seq": s.seq, "hash": s.head} for s in log.states] == data["valid"]["heads"]
    assert [s.projection() for s in log.states] == data["valid"]["projections"]
    assert {cast(dict[str, Any], e["body"])["type"] for e in log.entries} == PLATFORM_TYPES
    for case in data["valid"]["platform_access"]:
        state = log.states[case["count"] - 1]
        assert state.platform_access(case["org"], case["private"]) is case["access"]


def test_con02_rotation_cases_and_batches() -> None:
    data = load("log-rotation")
    ctx = context(data)
    seen = set()
    for case in data["cases"]:
        args = {"log": case["log"], "founder": data["founder"], "platform": data["platform"]}
        before = ctx.log(args)
        after = verify.apply(before.state, case["entry"], founder=data["founder"], platform=ctx.platform(args))
        assert rotation.entry_subject(case["entry"]["body"]) == case["subject"]
        required = rotation.required_set(before.state, after, case["subject"])
        assert rotation.as_json(required) == case["required"], case["name"]
        seen.add(case["entry"]["body"]["type"])
    assert seen >= {"remove", "ungrant", "device_unassign", "accept", "grant", "role", "rotate", "wg_create"}
    for case in data["batches"]:
        result = cast(BatchResult, ctx.run("check_batch", case["args"]))
        assert rotation.as_json(result.required) == case["required"]
        assert rotation.as_json(result.log.state.pending) == case["pending"]
        assert result.log.head.to_json() == case["head"]


def test_con02_wrap_cases_and_catch_up() -> None:
    data = load("log-wraps")
    ctx = context(data)
    for case in data["valid"]:
        ctx.run("check_wrap", case["args"])
    for case in data["cred_valid"]:
        ctx.run("check_cred_wrap", case["args"])
    log = ctx.log({**data["valid"][0]["args"]})
    existing = [
        wraps.WrapSlot(s["scope"], s["scope_id"], s["epoch"], s["recipient_id"]) for s in data["catch_up"]["existing"]
    ]
    for plan in data["catch_up"]["plans"]:
        missing = catchup.missing_wraps(log, plan["wrapper"], existing, plan["excluded"])
        assert [slot.to_json() for slot in missing] == plan["missing"]
    cred_existing = [
        wraps.WrapSlot(s["scope"], s["scope_id"], s["epoch"], s["recipient_id"])
        for s in data["cred_catch_up"]["existing"]
    ]
    for plan in data["cred_catch_up"]["plans"]:
        missing = catchup.missing_cred_wraps(
            [log], plan["owner"], plan["wrapper"], current_epoch=plan["current_epoch"], existing=cred_existing
        )
        assert [slot.to_json() for slot in missing] == plan["missing"]


def test_con02_event_cases() -> None:
    data = load("log-events")
    ctx = context(data)
    for case in data["valid"]:
        header, _rule = cast(tuple[dict[str, Any], events.KindRule], ctx.run("check_event", case["args"]))
        assert header["id"] == case["args"]["event"]["header"]["id"]
    for case in data["hub_valid"]:
        ctx.run("check_event_hub", case["args"])
    assert events.check_replay(data["replay"]["stored"], data["replay"]["incoming"]) is data["replay"]["replayed"]
    assert data["grace_seconds"] == 600
    assert parse_time(data["hub_valid"][2]["args"]["now"]) > parse_time(data["hub_valid"][2]["args"]["rotated_at"])


def test_con03_event_kind_matrix_equals_contract() -> None:
    contract = json.loads((CONTRACTS / "enums" / "event-kinds.json").read_text(encoding="utf-8"))
    contract.pop("description")
    assert events.matrix() == contract
    assert load("log-events")["matrix"] == contract


def test_generator_refuses_a_vector_with_another_code() -> None:
    data = load("log")
    ctx = context(data)
    case = data["invalid"][0]
    with pytest.raises(RuntimeError, match="instead of log_chain"):
        ctx.negative("x", case["op"], case["args"], "log_chain")
    valid = {"log": data["valid"]["log"], "founder": data["founder"], "platform": data["valid"]["platform"]}
    with pytest.raises(RuntimeError, match="no error"):
        ctx.negative("y", "verify_log", valid, "log_chain")
