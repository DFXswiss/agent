"""Model slots: task pins default from the device's AI configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.ai_accounts import (
    AccountError,
    load_ai_accounts,
    load_model_configuration,
    load_model_slots,
)
from agent_cli.allow import GATE_PAIRS, ready_for_done_blocking
from agent_cli.chain import handoff_prompt, step_vendor_label, steps_for, to_json
from agent_cli.coordinator_common import (
    checklist_key_for_stage,
    required_lane_slots,
)
from agent_cli.lane import launch
from agent_cli.main import main
from agent_cli.model_slots import (
    ModelSlots,
    ModelSlotsError,
    PinnedModelSlotsError,
    canonical_key,
    canonical_stage,
    implementer_from_snapshot,
    models_for_new_record,
    models_from_snapshot,
    parse_implementer,
    parse_model_slots,
    recorded_model_slots,
    recorded_names,
)
from driver_fixtures import (
    DECLARED_DRIVERS,
    LANE_DRIVERS,
    install_recorded_slots,
    provider_argv,
    write_accounts,
    write_drivers,
)

DRIVER_C_DIR = "/operator/path/driver-c-profile"
DRIVER_B_DIR = "/operator/path/driver-b-profile"
DRIVER_C_MODEL = "operator-driver-c-model"
DRIVER_B_MODEL = "operator-driver-b-model"
UNCONFIGURED_MODELS_MESSAGE = (
    "models is not configured: the device's ai-accounts.json must name "
    "models.first and models.second (set by the local harness)"
)


@pytest.fixture(autouse=True)
def _recorded_slots() -> None:
    """Stored names of earlier versions read as a store opened on this home reads them."""
    install_recorded_slots()


def write_driver_c_first(
    home: Path,
    *,
    device_lanes: bool = True,
    session: str = "s",
    implementer: str | None = None,
) -> None:
    """Operator configuration: model slot first = driver-c, second = driver-b."""
    lanes = {
        "driver-c:implementer": "first-builder",
        "driver-c:reviewer": "first-reader",
        "driver-c:pr-reviewer-quality": "first-reader",
        "driver-c:pr-reviewer-logic": "first-reader",
        "driver-b:pr-reviewer-quality": "second-reader",
        "driver-b:pr-reviewer-logic": "second-reader",
    }
    data: dict = {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "driver-c", "second": "driver-b"},
        "accounts": {
            "driver-c-a": {"provider": "driver-c", "config_dir": DRIVER_C_DIR},
            "driver-b-a": {"provider": "driver-b", "config_dir": DRIVER_B_DIR},
        },
        "roles": {
            "first-builder": {"account": "driver-c-a", "model": DRIVER_C_MODEL, "access": "workspace-write"},
            "first-reader": {"account": "driver-c-a", "model": DRIVER_C_MODEL, "access": "read-only"},
            "second-reader": {"account": "driver-b-a", "model": DRIVER_B_MODEL, "access": "read-only"},
        },
    }
    if implementer is not None:
        data["implementer"] = implementer
    if device_lanes:
        data["lanes"] = lanes
        data["sessions"] = {}
    else:
        data["sessions"] = {session: {"interactive": None, "lanes": lanes}}
    write_drivers(home)
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


# --- pure model_slots -------------------------------------------------------


@pytest.mark.no_pg
def test_supported_providers_and_legacy_recorded_order() -> None:
    recorded = recorded_names()
    assert recorded is not None
    with pytest.raises(ModelSlotsError) as excinfo:
        parse_model_slots(None)
    assert str(excinfo.value) == UNCONFIGURED_MODELS_MESSAGE
    assert models_from_snapshot({}) == recorded_model_slots(recorded)
    with pytest.raises(ModelSlotsError, match="task models is not configured"):
        models_for_new_record({})
    assert models_from_snapshot({"models": {"first": "driver-c", "second": "driver-a"}}) == ModelSlots(
        "driver-c", "driver-a"
    )
    assert implementer_from_snapshot({}) == recorded.first


@pytest.mark.no_pg
def test_implementer_parsing_and_snapshot_fallback() -> None:
    models = ModelSlots("driver-c", "driver-b")
    assert parse_implementer(None, models) == "driver-c"
    assert parse_implementer("driver-c", models) == "driver-c"
    assert parse_implementer("driver-e", models) == "driver-e"
    assert implementer_from_snapshot({"models": models.as_dict()}) == "driver-c"
    assert implementer_from_snapshot(
        {"models": models.as_dict(), "implementer": "driver-e"}
    ) == "driver-e"
    with pytest.raises(ModelSlotsError, match="implementer must be a driver id"):
        parse_implementer("not a driver", models)
    with pytest.raises(ModelSlotsError, match="implementer must differ from models.second"):
        parse_implementer("driver-b", models)
    with pytest.raises(ModelSlotsError, match="models.first must be a driver id"):
        parse_model_slots({"first": "first", "second": "driver-b"})
    assert parse_model_slots(
        {"first": "driver-d", "second": "driver-c"}
    ) == ModelSlots("driver-d", "driver-c")
    assert parse_model_slots(
        {"first": "driver-a", "second": "driver-d"}
    ) == ModelSlots("driver-a", "driver-d")
    assert parse_model_slots({"first": "driver-a", "second": None}) == ModelSlots(
        "driver-a", None
    )


@pytest.mark.no_pg
def test_slots_resolve_stage_and_vendor() -> None:
    models = ModelSlots("driver-c", "driver-b")
    assert models.for_slot("first") == "driver-c"
    assert models.for_stage("review-1") == "driver-c"
    assert models.for_stage("review-2") == "driver-b"
    # Keep legacy names here to verify stage and checklist alias compatibility.
    assert models.for_stage("driver-a-pr") == "driver-c"
    assert canonical_stage("driver-b-pr") == "review-2"
    assert canonical_key("driver-a_pr_logic") == "review_1_logic"
    assert models.slot_of("driver-b") == "second"
    assert models.slot_of("driver-a") is None
    assert models.as_dict() == {"first": "driver-c", "second": "driver-b"}
    with pytest.raises(ModelSlotsError):
        models.for_slot("third")
    with pytest.raises(ModelSlotsError):
        models.for_stage("other-pr")

    external = ModelSlots("driver-a", None)
    assert external.for_stage("driver-b-pr") is None
    assert external.slot_of("driver-b") is None
    assert external.as_dict() == {"first": "driver-a", "second": None}


@pytest.mark.no_pg
@pytest.mark.parametrize(
    "raw",
    [
        {"first": "driver-c"},
        {"first": "driver-c", "second": "driver-b", "third": "driver-a"},
        {"first": "not a driver", "second": "driver-b"},
        {"first": "driver-b", "second": "driver-b"},
        ["driver-c", "driver-b"],
    ],
)
def test_models_block_is_validated(raw: object) -> None:
    with pytest.raises(ModelSlotsError):
        parse_model_slots(raw)


# --- ai-accounts.json -------------------------------------------------------


@pytest.mark.no_pg
def test_manifest_models_and_device_lanes(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path, implementer="driver-e")
    loaded = load_ai_accounts(tmp_path)
    assert loaded.models == ModelSlots("driver-c", "driver-b")
    assert loaded.implementer == "driver-e"
    assert load_model_slots(tmp_path) == ModelSlots("driver-c", "driver-b")
    assert load_model_configuration(tmp_path) == (
        ModelSlots("driver-c", "driver-b"),
        "driver-e",
    )
    # Device-wide lanes serve any session that binds nothing of its own.
    implementer = loaded.for_lane("never-bound-session", "implementer", "driver-c")
    assert implementer.name == "first-builder"
    prefix = implementer.env_prefix(loaded.drivers)
    assert prefix[-1] == f"TOOL_C_CONFIG={DRIVER_C_DIR}"
    assert "-u" in prefix and "TOOL_C_CONFIG" in prefix
    # The default profile ~/.tool-c keeps its login outside the directory: the
    # lane clears TOOL_C_CONFIG and does not point it there.
    from agent_cli.ai_accounts import AIAccount, AIRole

    default_role = AIRole(
        "d", AIAccount("c", "driver-c", str(Path.home() / ".tool-c")), "m", "read-only"
    )
    prefix = default_role.env_prefix(loaded.drivers)
    assert prefix[-2:] == ["-u", "TOOL_C_CONFIG"]
    assert not any(a.startswith("TOOL_C_CONFIG=") for a in prefix)
    second = loaded.for_lane("never-bound-session", "pr-reviewer-logic", "driver-b")
    assert second.env_prefix(loaded.drivers)[-1] == f"TOOL_B_HOME={DRIVER_B_DIR}"
    with pytest.raises(AccountError, match="slot driver-a:implementer"):
        loaded.for_lane("never-bound-session", "implementer", "driver-a")


@pytest.mark.no_pg
def test_session_lanes_win_over_device_lanes(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["roles"]["other-builder"] = {
        "account": "driver-c-a",
        "model": "other-model",
        "access": "workspace-write",
    }
    raw["sessions"] = {"s": {"interactive": None, "lanes": {"driver-c:implementer": "other-builder"}}}
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_ai_accounts(tmp_path)
    assert loaded.for_lane("s", "implementer", "driver-c").model == "other-model"
    # Unbound slot of a bound session falls back to the device-wide map.
    assert loaded.for_lane("s", "reviewer", "driver-c").name == "first-reader"


@pytest.mark.no_pg
@pytest.mark.parametrize("manifest", [None, {}, {"accounts": {}, "roles": {}, "sessions": {}}])
def test_unconfigured_models_fail_closed(tmp_path: Path, manifest: dict | None) -> None:
    if manifest is not None:
        (tmp_path / "ai-accounts.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AccountError, match="models is not configured") as excinfo:
        load_ai_accounts(tmp_path)
    assert str(excinfo.value) == UNCONFIGURED_MODELS_MESSAGE
    with pytest.raises(AccountError) as slots_exc:
        load_model_slots(tmp_path)
    assert str(slots_exc.value) == UNCONFIGURED_MODELS_MESSAGE


@pytest.mark.no_pg
@pytest.mark.parametrize(
    "change",
    ["same-provider", "unknown-provider", "missing-second", "lanes-unknown-role", "lanes-not-object"],
)
def test_malformed_models_and_lanes_rejected(tmp_path: Path, change: str) -> None:
    write_driver_c_first(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    if change == "same-provider":
        raw["models"]["second"] = "driver-c"
    elif change == "unknown-provider":
        raw["models"]["first"] = "driver-x"
    elif change == "missing-second":
        del raw["models"]["second"]
    elif change == "lanes-unknown-role":
        raw["lanes"]["driver-c:implementer"] = "nobody"
    elif change == "lanes-not-object":
        raw["lanes"] = ["driver-c:implementer"]
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AccountError):
        load_ai_accounts(tmp_path)


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("implementer", "message"),
    [
        ("driver-x", "implementer must be one of"),
        ("driver-b", "implementer must differ from models.second"),
    ],
)
def test_both_manifest_loaders_reject_invalid_implementer(
    tmp_path: Path, implementer: str, message: str
) -> None:
    write_driver_c_first(tmp_path, implementer=implementer)
    with pytest.raises(AccountError, match=message):
        load_ai_accounts(tmp_path)
    with pytest.raises(AccountError, match=message):
        load_model_slots(tmp_path)


# --- lane argv --------------------------------------------------------------


@pytest.mark.no_pg
def test_driver_c_argv_read_only_and_write_shapes() -> None:
    ro = provider_argv(
        "driver-c", cwd="/work", write=False, model=DRIVER_C_MODEL, read_dirs=("/control",)
    )
    assert ro[0] == "env"
    assert "tool-c" in ro and "--print" in ro
    assert ro[ro.index("--model") + 1] == DRIVER_C_MODEL
    assert ro[ro.index("--add-dir") + 1] == "/control"
    assert ro[ro.index("--mode") + 1] == "default"
    assert ro[ro.index("--tools") + 1] == "Read"

    rw = provider_argv("driver-c", cwd="/work", write=True, model=DRIVER_C_MODEL)
    assert rw[rw.index("--mode") + 1] == "edit"
    assert rw[rw.index("--tools") + 1] == "Read,Edit"
    assert "--add-dir" not in rw
    with pytest.raises(SystemExit, match="write lane takes no extra directories"):
        provider_argv(
            "driver-c", cwd="/work", write=True, model=DRIVER_C_MODEL, read_dirs=("/x",)
        )


@pytest.mark.no_pg
def test_provider_argv_dispatch() -> None:
    assert "tool-c" in provider_argv("driver-c", spec_file="/s.md", cwd="/w", write=False, model="m")
    driver_a = provider_argv("driver-a", spec_file="/s.md", cwd="/w", write=False, model="m")
    assert "--prompt-file" in driver_a
    assert provider_argv(
        "driver-a", spec_file="/s.md", cwd="/w", write=False, model="m",
        read_dirs=("/ignored",),
    ) == driver_a
    driver_b = provider_argv("driver-b", spec_file="/s.md", cwd="/w", write=False, model="m", output_file="/o.txt")
    assert "tool-b" in driver_b and "/o.txt" in driver_b
    assert provider_argv(
        "driver-b", spec_file="/s.md", cwd="/w", write=False, model="m",
        output_file="/o.txt", read_dirs=("/ignored",),
    ) == driver_b
    driver_d = provider_argv(
        "driver-d", spec_file="/s.md", cwd="/w", write=False, model="vendor/model",
        output_file="/o.jsonl", read_dirs=("/control",),
    )
    assert "TOOL_C_BASE_URL=https://gateway.example.test/api" in driver_d
    assert "TOOL_C_API_KEY=" in driver_d
    assert "/o.jsonl" in driver_d and "vendor/model" in driver_d
    with pytest.raises(SystemExit, match="output file"):
        provider_argv("driver-b", spec_file="/s.md", cwd="/w", write=False, model="m")


@pytest.mark.no_pg
def test_launch_driver_c_lane_feeds_spec_on_stdin(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}

    def runner(argv: list[str], stdin_text: str | None):
        seen["argv"] = argv
        seen["stdin"] = stdin_text
        event = {
            "type": "result",
            "result": "ok\nSTATUS: complete\nRESULT: done\n",
            "usage": {},
        }
        return type("Done", (), {"returncode": 0, "stdout": json.dumps(event) + "\n", "stderr": ""})()

    result = launch(
        role="implementer",
        vendor="driver-c",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=runner,
        dry_run=False,
        tmux=False,
        config_home=tmp_path,
        session_id="any-session",
    )
    assert result.status == "complete"
    assert seen["stdin"] == "implement this\n"
    assert "tool-c" in seen["argv"] and "--print" in seen["argv"]
    assert f"TOOL_C_CONFIG={DRIVER_C_DIR}" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--mode") + 1] == "edit"


@pytest.mark.no_pg
def test_launch_rejects_unknown_vendor(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(
        SystemExit,
        match="vendor must be driver-a\\|driver-b\\|driver-c\\|driver-d \\(see drivers.json",
    ):
        launch(
            role="implementer",
            vendor="unconfigured-driver",
            spec_file=str(spec),
            cwd=str(tmp_path),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id="s",
        )


# --- chain / allow / coordinator helpers ------------------------------------


@pytest.mark.no_pg
def test_chain_steps_name_slots_not_vendors() -> None:
    steps = {s.key: s for s in steps_for("implement")}
    assert steps["implementer_done"].slot == "first"
    assert steps["reviewer_approved"].slot == "first"
    assert steps["review_1_quality"].slot == "first"
    assert steps["review_2_logic"].slot == "second"
    models = ModelSlots("driver-c", "driver-b")
    assert step_vendor_label(steps["reviewer_approved"], models) == "driver-c"
    assert step_vendor_label(steps["implementer_done"], models) == "driver-c"
    assert step_vendor_label(steps["review_2_quality"], models) == "driver-b"
    assert step_vendor_label(steps["session_registered"], models) == "-"
    assert "who=driver-c reviewer" in handoff_prompt(
        steps["reviewer_approved"], task_id="1", session_id="s", models=models
    )
    assert to_json(steps["review_1_quality"], models)["vendor"] == "driver-c"
    assert to_json(steps["review_1_quality"], models)["slot"] == "first"
    with pytest.raises(ModelSlotsError, match="models is not configured"):
        to_json(steps["review_1_quality"])
    separate = {"models": models.as_dict(), "implementer": "driver-e"}
    assert (
        step_vendor_label(steps["implementer_done"], models, snapshot=separate)
        == "driver-e"
    )
    assert step_vendor_label(steps["reviewer_approved"], models, snapshot=separate) == "driver-c"
    assert "who=driver-e implementer" in handoff_prompt(
        steps["implementer_done"],
        task_id="1",
        session_id="s",
        models=models,
        snapshot=separate,
    )


@pytest.mark.no_pg
def test_gate_pairs_follow_configured_models() -> None:
    assert [slot for _, _, slot in GATE_PAIRS] == ["first", "first", "second", "second"]
    gates = [
        {"stage": "review-1", "dimension": "quality", "vendor": "driver-c", "verdict": "approved", "head_sha": "a" * 40},
        {"stage": "review-1", "dimension": "logic", "vendor": "driver-c", "verdict": "approved", "head_sha": "a" * 40},
        {"stage": "review-2", "dimension": "quality", "vendor": "driver-b", "verdict": "approved", "head_sha": "a" * 40},
        {"stage": "review-2", "dimension": "logic", "vendor": "driver-b", "verdict": "approved", "head_sha": "a" * 40},
    ]
    checklist = {
        "session_registered": "ja", "contributing_read": "ja", "contributing_ok": "ja",
        "coverage_ok": "ja", "handbook_ok": "ja", "review_1_quality": "ja", "review_1_logic": "ja",
        "review_2_quality": "ja", "review_2_logic": "ja", "deviation_declared": "n_a",
        "deviation_granted": "n_a",
    }
    task = {
        "id": "t", "workflow": "review", "checklist": checklist, "gates": gates,
        "local_checks": [], "summaries": {"en": "x", "de": "y"},
        "models": {"first": "driver-c", "second": "driver-b"},
    }
    assert ready_for_done_blocking(task) == []
    # The same gates fail under the historical order: the first slot would be driver-a.
    legacy = dict(task)
    legacy.pop("models")
    blocking = ready_for_done_blocking(legacy)
    assert any("review-1/quality=driver-c/approved" in b for b in blocking)


@pytest.mark.no_pg
def test_coordinator_slot_helpers() -> None:
    models = ModelSlots("driver-c", "driver-b")
    assert required_lane_slots(models) == (
        "driver-c:implementer",
        "driver-c:reviewer",
        "driver-c:pr-reviewer-quality",
        "driver-c:pr-reviewer-logic",
        "driver-b:pr-reviewer-quality",
        "driver-b:pr-reviewer-logic",
    )
    assert checklist_key_for_stage("review-1", "quality") == "review_1_quality"
    assert checklist_key_for_stage("review-2", "logic") == "review_2_logic"
    assert checklist_key_for_stage("driver-a-pr", "quality") == "review_1_quality"
    assert checklist_key_for_stage("driver-b-pr", "logic") == "review_2_logic"
    assert required_lane_slots(ModelSlots("driver-a", None)) == (
        "driver-a:implementer",
        "driver-a:reviewer",
        "driver-a:pr-reviewer-quality",
        "driver-a:pr-reviewer-logic",
    )


@pytest.mark.no_pg
def test_external_second_review_satisfies_done_gate_without_second_gate_rows() -> None:
    head = "a" * 40
    checklist = {
        "session_registered": "ja",
        "contributing_read": "ja",
        "contributing_ok": "ja",
        "coverage_ok": "ja",
        "handbook_ok": "ja",
        "review_1_quality": "ja",
        "review_1_logic": "ja",
        "review_2_quality": "ja",
        "review_2_logic": "ja",
        "deviation_declared": "n_a",
        "deviation_granted": "n_a",
    }
    prefix = f"external second review by reviewer at {head}: "
    task = {
        "id": "task",
        "workflow": "review",
        "models": {"first": "driver-a", "second": None},
        "payload": {"coordinator": {"a38": {"enabled": True, "second_review": {"head": head, "status": "posted", "reviews": [{"blocks_merge": False, "at": "2026-01-01T00:00:00Z"}]}}}},
        "checklist": checklist,
        "checklist_evidence": {
            "review_2_quality": prefix + "https://example.invalid/comment",
            "review_2_logic": prefix + "https://example.invalid/comment",
        },
        "gates": [
            {"stage": "review-1", "dimension": "quality", "vendor": "driver-a", "verdict": "approved", "head_sha": head},
            {"stage": "review-1", "dimension": "logic", "vendor": "driver-a", "verdict": "approved", "head_sha": head},
        ],
        "local_checks": [],
        "summaries": {"en": "Done.", "de": "Erledigt."},
    }
    assert ready_for_done_blocking(task) == []
    task["checklist_evidence"]["review_2_logic"] = (
        f"external second review by reviewer at {'b' * 40}: https://example.invalid/comment"
    )
    assert any(
        "review-2/logic=external_review_missing" in item
        for item in ready_for_done_blocking(task)
    )
    task["checklist_evidence"]["review_2_logic"] = prefix + "https://example.invalid/comment"
    task.pop("payload")
    assert any(
        "review-2/quality=external_review_missing" in item
        for item in ready_for_done_blocking(task)
    )
    task["payload"] = {"coordinator": {"a38": {"enabled": True}}}
    task["checklist_evidence"]["review_2_logic"] = "ordinary evidence"
    assert any("review-2/logic=external_review_missing" in item for item in ready_for_done_blocking(task))


@pytest.mark.no_pg
def test_models_cli_prints_external_for_null_second(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_drivers(tmp_path)
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps({"models": {"first": "driver-a", "second": None}}),
        encoding="utf-8",
    )
    _run(tmp_path, ["models"])
    assert capsys.readouterr().out.splitlines() == ["first=driver-a", "second=external"]
    _run(tmp_path, ["models", "second", "--json"])
    assert json.loads(capsys.readouterr().out) == {"second": None}


def test_task_create_refuses_external_second_slot_outside_coordinator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps({"models": {"first": "driver-a", "second": None}}),
        encoding="utf-8",
    )
    with pytest.raises(
        SystemExit,
        match="models.second = null is allowed only for coordinator A38 task creation",
    ):
        _task(tmp_path, capsys)


def test_gate_record_refuses_external_second_slot_on_direct_task_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_drivers(tmp_path)
    _run(tmp_path, ["init"])
    _run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "pr-review",
        ],
    )
    capsys.readouterr()
    tid = "11111111-1111-4111-8111-111111111111"
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        store.write(
            "task",
            "insert",
            tid,
            {
                "id": tid,
                "session_id": "s",
                "workflow": "implement",
                "state": "pr-review",
                "title": "External second review",
                "models": {"first": "driver-a", "second": None},
                "payload": {"coordinator": {"a38": {"enabled": True}}},
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="the second review is external"):
        _run(
            tmp_path,
            [
                "gate",
                "record",
                "--task",
                tid,
                "--stage",
                "review-2",
                "--dimension",
                "quality",
                "--vendor",
                "driver-b",
                "--verdict",
                "approved",
                "--head",
                "abcdef0",
                "--agent",
                "missing-agent",
            ],
        )


# --- CLI with configured slots ----------------------------------------------


def _run(home: Path, argv: list[str]) -> None:
    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def _task(home: Path, capsys: pytest.CaptureFixture[str]) -> str:
    if not (home / "drivers.json").exists():
        write_drivers(home)
    if not (home / "ai-accounts.json").exists():
        (home / "ai-accounts.json").write_text(
            json.dumps(
                {
                    "models": {"first": "driver-a", "second": "driver-b"},
                    "lane_execution": {"mode": "this-device-is-lane-host"},
                }
            ),
            encoding="utf-8",
        )
    _run(home, ["init"])
    _run(home, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    _run(home, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("task ")][-1].split()[1]


def test_cli_agent_start_follows_configured_first_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "driver-c"])
    out = capsys.readouterr().out
    assert out.startswith("agent ")
    pr_agent = out.split()[1]
    _run(tmp_path, ["agent", "finish", "--id", pr_agent, "--verdict", "approved"])
    with pytest.raises(SystemExit, match="vendor must be driver-a\\|driver-b\\|driver-c\\|driver-d"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-logic", "--vendor", "driver-x"])
    _run(tmp_path, ["task", "state", tid, "implementing"])
    _run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="implementer requires --vendor driver-c \\(configured implementer\\)"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "implementer", "--vendor", "driver-a", "--round", "1"])
    # The second review slot is not the task's resolved implementer.
    with pytest.raises(SystemExit, match="implementer requires --vendor driver-c \\(configured implementer\\)"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "implementer", "--vendor", "driver-b", "--round", "1"])
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "implementer", "--vendor", "driver-c", "--round", "1"])
    aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "done"])
    _run(tmp_path, ["task", "state", tid, "reviewing"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="reviewer requires --vendor driver-c"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "reviewer", "--vendor", "driver-b", "--round", "1"])


def test_cli_agent_start_records_agent_without_lane_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_drivers(tmp_path)
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps({"models": {"first": "driver-a", "second": "driver-b"}}),
        encoding="utf-8",
    )
    tid = _task(tmp_path, capsys)

    _run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "driver-a",
        ],
    )
    aid = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent ")
    ][-1].split()[1]

    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        row = store.row("agent", aid)
        assert row is not None
        assert row["task_id"] == tid
        assert row["status"] == "working"
    finally:
        store.close()


def test_cli_gate_record_requires_the_slot_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    with pytest.raises(SystemExit, match="stage review-1 requires --vendor driver-c"):
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--stage", "review-1",
                "--vendor", "driver-a",
            ],
        )
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "driver-c"])
    driver_c_agent = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", driver_c_agent, "--verdict", "approved"])
    capsys.readouterr()
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-1", "--dimension", "quality", "--vendor", "driver-c", "--verdict", "approved", "--head", "abcdef0", "--agent", driver_c_agent])
    assert "gate review-1/quality=approved" in capsys.readouterr().out


def _remove_task_models(home: Path, tid: str) -> None:
    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(home)
    store = open_store()
    try:
        task = {
            key: value
            for key, value in (store.row("task", tid) or {}).items()
            if not key.startswith("_")
        }
        task.pop("models", None)
        store.write("task", "update", tid, task)
    finally:
        store.close()


def test_legacy_task_without_models_still_displays_recorded_gates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import load_task_dict, open_store

    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "driver-a"])
    aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-1", "--dimension", "quality", "--vendor", "driver-a", "--verdict", "approved", "--head", "abcdef0", "--agent", aid])
    _remove_task_models(tmp_path, tid)

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        displayed = load_task_dict(store, tid)
    finally:
        store.close()
    assert displayed["models"] == {"first": "driver-a", "second": "driver-b"}
    assert displayed["gates"][0]["vendor"] == "driver-a"


def test_legacy_task_without_models_rejects_new_agent_and_gate_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "driver-a"])
    aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    _remove_task_models(tmp_path, tid)

    with pytest.raises(SystemExit, match="task models is not configured"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-logic", "--vendor", "driver-a"])
    with pytest.raises(SystemExit, match="task models is not configured"):
        _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-1", "--dimension", "quality", "--vendor", "driver-a", "--verdict", "approved", "--head", "abcdef0", "--agent", aid])


def test_legacy_gate_record_fails_before_historical_vendor_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "pr-reviewer-quality", "--vendor", "driver-c",
        ],
    )
    aid = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent ")
    ][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    _remove_task_models(tmp_path, tid)

    with pytest.raises(SystemExit) as excinfo:
        _run(
            tmp_path,
            [
                "gate", "record", "--task", tid, "--stage", "review-1",
                "--dimension", "quality", "--vendor", "driver-c",
                "--verdict", "approved", "--head", "abcdef0", "--agent", aid,
            ],
        )

    message = str(excinfo.value).removeprefix("agent: ")
    assert message.startswith("task models is not configured")
    assert "requires vendor driver-a" not in message
    assert "model slot pinned" not in message


def test_gate_record_keeps_session_precondition_before_new_model_pin_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _task(tmp_path, capsys)
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "pr-reviewer-quality", "--vendor", "driver-a",
        ],
    )
    aid = [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent ")
    ][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    _remove_task_models(tmp_path, tid)
    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        session = store.row("session", "s")
        assert session is not None
        session["status"] = "closed"
        store.write(
            "session",
            "update",
            "s",
            {key: value for key, value in session.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="session s is not active"):
        _run(
            tmp_path,
            [
                "gate", "record", "--task", tid, "--stage", "review-1",
                "--dimension", "quality", "--vendor", "driver-a",
                "--verdict", "approved", "--head", "abcdef0", "--agent", aid,
            ],
        )


def test_legacy_task_reviewer_start_fails_before_historical_vendor_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["task", "state", tid, "implementing"])
    _run(tmp_path, ["round", "start", "--task", tid])
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "driver-a", "--round", "1",
        ],
    )
    implementer_id = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent ")
    ][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", implementer_id, "--verdict", "done"])
    _remove_task_models(tmp_path, tid)

    with pytest.raises(SystemExit) as excinfo:
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "reviewer", "--vendor", "driver-c", "--round", "1",
            ],
        )

    message = str(excinfo.value).removeprefix("agent: ")
    assert message.startswith("task models is not configured")
    assert "requires --vendor driver-a" not in message


def test_legacy_task_without_models_rejects_a_new_lane_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["close-step", "--task", tid, "--key", "session_registered", "--source", "script", "--evidence", "registered"])
    _run(tmp_path, ["close-step", "--task", tid, "--key", "spec_written", "--source", "human", "--evidence", "spec"])
    _remove_task_models(tmp_path, tid)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="task models is not configured"):
        _run(
            tmp_path,
            [
                "run", "--task", tid, "--spec-file", str(spec),
                "--cwd", str(tmp_path), "--no-tmux",
            ],
        )


def test_legacy_task_without_models_rejects_new_work_queries_and_handoffs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _task(tmp_path, capsys)
    _run(
        tmp_path,
        [
            "close-step", "--task", tid, "--key", "session_registered",
            "--source", "script", "--evidence", "registered",
        ],
    )
    _run(
        tmp_path,
        [
            "close-step", "--task", tid, "--key", "spec_written",
            "--source", "human", "--evidence", "spec",
        ],
    )
    _remove_task_models(tmp_path, tid)

    for command in (
        ["next", "--task", tid],
        ["run", "--task", tid, "--dry-run"],
        ["models", "--task", tid],
    ):
        with pytest.raises(SystemExit, match="task models is not configured"):
            _run(tmp_path, command)


def test_cli_next_prints_configured_vendor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["next", "--task", tid, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["next"][0]["key"] == "session_registered"
    _run(tmp_path, ["close-step", "--task", tid, "--key", "session_registered", "--source", "script", "--evidence", "registered"])
    _run(tmp_path, ["close-step", "--task", tid, "--key", "spec_written", "--source", "human", "--evidence", "spec"])
    capsys.readouterr()
    _run(tmp_path, ["next", "--task", tid])
    out = capsys.readouterr().out
    assert "vendor=driver-c" in out
    assert "vendor=driver-c|driver-b" not in out
    assert "who=driver-c implementer" in out


def test_cli_next_and_run_name_the_separate_implementer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path, implementer="driver-e")
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["close-step", "--task", tid, "--key", "session_registered", "--source", "script", "--evidence", "registered"])
    _run(tmp_path, ["close-step", "--task", tid, "--key", "spec_written", "--source", "human", "--evidence", "spec"])
    capsys.readouterr()
    _run(tmp_path, ["next", "--task", tid])
    out = capsys.readouterr().out
    assert "role=implementer vendor=driver-e" in out
    assert "who=driver-e implementer" in out
    _run(tmp_path, ["run", "--task", tid, "--dry-run"])
    assert "who=driver-e implementer" in capsys.readouterr().out


def test_cli_run_hands_off_a_separate_implementer_without_a_local_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path, implementer="driver-e")
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["close-step", "--task", tid, "--key", "session_registered", "--source", "script", "--evidence", "registered"])
    _run(tmp_path, ["close-step", "--task", tid, "--key", "spec_written", "--source", "human", "--evidence", "spec"])
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            [
                "run",
                "--task",
                tid,
                "--spec-file",
                str(spec),
                "--cwd",
                str(tmp_path),
                "--no-tmux",
            ],
        )

    assert exc.value.code == 2
    assert "who=driver-e implementer" in capsys.readouterr().out
    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.rows("agent") == []
    finally:
        store.close()


@pytest.mark.no_pg
def test_cli_models_prints_configured_slots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_drivers(tmp_path)
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps({"models": {"first": "driver-a", "second": "driver-b"}}),
        encoding="utf-8",
    )
    _run(tmp_path, ["models"])
    assert capsys.readouterr().out == "first=driver-a\nsecond=driver-b\n"
    write_driver_c_first(tmp_path)
    _run(tmp_path, ["models"])
    assert capsys.readouterr().out == "first=driver-c\nsecond=driver-b\n"
    _run(tmp_path, ["models", "first"])
    assert capsys.readouterr().out == "driver-c\n"
    _run(tmp_path, ["models", "implementer"])
    assert capsys.readouterr().out == "driver-c\n"
    _run(tmp_path, ["models", "--json"])
    assert json.loads(capsys.readouterr().out) == {"first": "driver-c", "second": "driver-b"}
    _run(tmp_path, ["models", "second", "--json"])
    assert json.loads(capsys.readouterr().out) == {"second": "driver-b"}
    with pytest.raises(SystemExit, match="Usage: agent models"):
        _run(tmp_path, ["models", "third"])
    with pytest.raises(SystemExit, match="Usage: agent models"):
        _run(tmp_path, ["models", "--yaml"])


def test_device_model_errors_are_not_reported_as_invalid_task_pins(
    tmp_path: Path,
) -> None:
    manifest = {
        "models": {"first": "driver-c", "second": "driver-b"},
        "implementer": "driver-x",
    }
    write_drivers(tmp_path)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as models_exc:
        _run(tmp_path, ["models"])
    models_message = str(models_exc.value).removeprefix("agent: ")
    assert models_message.startswith("implementer must be one of")
    assert "invalid models pinned on the task" not in str(models_exc.value)

    manifest["implementer"] = "driver-b"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(manifest), encoding="utf-8")
    _run(tmp_path, ["init"])
    _run(
        tmp_path,
        ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"],
    )
    with pytest.raises(SystemExit) as configured_create_exc:
        _run(
            tmp_path,
            [
                "task", "create", "--session", "s", "--workflow", "implement",
                "--title", "Ship",
            ],
        )
    configured_create_message = str(configured_create_exc.value).removeprefix("agent: ")
    assert configured_create_message == "implementer must differ from models.second"


def test_coordinator_snapshots_carry_the_configured_models(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The coordinator feeds checklist_snapshot / _task_snapshot into chain and allow; both need models."""
    from agent_cli.coordinator_github import _task_snapshot
    from agent_cli.coordinator_lanes import checklist_snapshot
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        snap = checklist_snapshot(store, dict(task))
        assert snap["models"] == {"first": "driver-c", "second": "driver-b"}
        assert snap["implementer"] == "driver-c"
        assert snap["models_pinned"] is True
        # The chain takes the last matching row, so agents come oldest first.
        for aid, started, status in (("a-old", "2026-01-01T00:00:00Z", "working"), ("a-new", "2026-01-02T00:00:00Z", "done")):
            store.write("agent", "insert", aid, {"id": aid, "task_id": tid, "role": "implementer", "vendor": "driver-c", "status": status, "started_at": started})
        snap = checklist_snapshot(store, dict(task))
        assert [a["status"] for a in snap["agents"] if a["role"] == "implementer"] == ["working", "done"]
        # A task created before model slots carries no pin.
        unpinned = {k: v for k, v in dict(task).items() if k != "models"}
        assert checklist_snapshot(store, unpinned)["models_pinned"] is False
        task_snap = _task_snapshot(store, tid)
        assert task_snap["models"] == {"first": "driver-c", "second": "driver-b"}
        assert task_snap["implementer"] == "driver-c"
    finally:
        store.close()


def test_task_pins_models_at_create(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A later device change does not re-label what a task already recorded."""
    tid = _task(tmp_path, capsys)  # explicit harness configuration: driver-a/driver-b
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "driver-a"])
    aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    write_driver_c_first(tmp_path)  # device now says driver-c first
    capsys.readouterr()
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-1", "--dimension", "quality", "--vendor", "driver-a", "--verdict", "approved", "--head", "abcdef0", "--agent", aid])
    assert "gate review-1/quality=approved" in capsys.readouterr().out
    from agent_cli.main import load_task_dict, open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("task", tid).get("models") == {"first": "driver-a", "second": "driver-b"}
        assert load_task_dict(store, tid)["models"] == {"first": "driver-a", "second": "driver-b"}
    finally:
        store.close()
    # A task created now pins driver-c first.
    _run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Next"])
    tid2 = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("task ")][-1].split()[1]
    store = open_store()
    try:
        assert store.row("task", tid2).get("models") == {"first": "driver-c", "second": "driver-b"}
    finally:
        store.close()


def test_task_create_checks_and_pins_one_model_configuration_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The null check and the pin must come from the same manifest snapshot."""
    _run(tmp_path, ["init"])
    _run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"])

    class DriftingLoader:
        def __init__(self) -> None:
            self.calls: list[Path] = []

        def __call__(self, home: Path) -> tuple[ModelSlots, str]:
            self.calls.append(home)
            # The device changes after the first read: a second read would find no second slot.
            return ModelSlots("driver-a", "driver-b" if len(self.calls) == 1 else None), "driver-a"

    loader = DriftingLoader()
    monkeypatch.setattr("agent_cli.main.load_model_configuration", loader)
    monkeypatch.setattr("agent_cli.ai_accounts.load_model_configuration", loader)
    _run(
        tmp_path,
        ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"],
    )
    out = capsys.readouterr().out
    tid = [ln for ln in out.splitlines() if ln.startswith("task ")][-1].split()[1]
    assert len(loader.calls) == 1
    from agent_cli.main import open_store

    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        assert task["models"] == {"first": "driver-a", "second": "driver-b"}
    finally:
        store.close()


def test_task_create_refuses_null_second_without_inserting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, ["init"])
    _run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"])
    calls: list[Path] = []

    def fake_load(home: Path) -> tuple[ModelSlots, str]:
        calls.append(home)
        return ModelSlots("driver-a", None), "driver-a"

    monkeypatch.setattr("agent_cli.main.load_model_configuration", fake_load)
    with pytest.raises(SystemExit, match="models.second = null"):
        _run(
            tmp_path,
            ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"],
        )
    assert len(calls) == 1
    from agent_cli.main import open_store

    store = open_store()
    try:
        assert store.rows("task") == []
    finally:
        store.close()


def test_task_pins_only_a_separate_implementer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path, implementer="driver-c")
    tid = _task(tmp_path, capsys)
    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        assert "implementer" not in task
        assert implementer_from_snapshot(task) == "driver-c"
    finally:
        store.close()

    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["implementer"] = "driver-e"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    _run(
        tmp_path,
        ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Separate"],
    )
    separate_tid = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("task ")
    ][-1].split()[1]
    store = open_store()
    try:
        separate = store.row("task", separate_tid)
        assert separate is not None
        assert separate["models"] == {"first": "driver-c", "second": "driver-b"}
        assert separate["implementer"] == "driver-e"
        assert implementer_from_snapshot(separate) == "driver-e"
    finally:
        store.close()


@pytest.mark.no_pg
def test_load_model_slots_ignores_unrelated_manifest_errors(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"] = {"s": {"interactive": None, "lanes": {"driver-c:implementer": "nobody"}}}
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AccountError):
        load_ai_accounts(tmp_path)
    assert load_model_slots(tmp_path) == ModelSlots("driver-c", "driver-b")


@pytest.mark.no_pg
def test_lane_run_rejects_a_missing_cwd(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="--cwd is not a directory"):
        _run(tmp_path, ["lane", "run", "--role", "implementer", "--vendor", "driver-c", "--session", "s",
                        "--spec-file", str(spec), "--cwd", str(tmp_path / "missing"), "--no-tmux"])



@pytest.mark.no_pg
def test_driver_c_lane_in_tmux_redirects_the_spec_file(tmp_path: Path) -> None:
    write_driver_c_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}

    def runner(argv: list[str], stdin_text: str | None):
        seen["argv"], seen["stdin"] = argv, stdin_text
        inner = argv[argv.index("--") + 1 :]
        Path(inner[5]).write_text(
            json.dumps(
                {
                    "type": "result",
                    "result": "output file\nSTATUS: complete",
                    "usage": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return type("Done", (), {"returncode": 0, "stdout": "runner stdout", "stderr": ""})()

    for dry in (True, False):
        result = launch(
            role="implementer", vendor="driver-c", spec_file=str(spec), cwd=str(tmp_path),
            runner=None if dry else runner, dry_run=dry, tmux=True, config_home=tmp_path,
            session_id="s",
        )
        argv = result.argv
        inner = argv[argv.index("--") + 1 :]
        assert inner[:5] == [
            "sh",
            "-c",
            'spec=$1; out=$2; shift 2; exec "$@" < "$spec" > "$out"',
            "sh",
            str(spec.resolve()),
        ]
        assert inner[5]
        assert "tool-c" in inner and "--print" in inner
        if not dry:
            assert seen["stdin"] is None
            assert result.stdout == "output file\nSTATUS: complete"
    # Without an injected runner the tmux holder must not type the spec into the pane.
    import agent_cli.lane as lane_mod

    captured: dict = {}

    def fake_tmux(inner, *, name, cwd, stdin_text):  # type: ignore[no-untyped-def]
        captured["inner"], captured["stdin"] = inner, stdin_text
        from subprocess import CompletedProcess

        return CompletedProcess(inner, 0, "STATUS: complete\n", "")

    orig = lane_mod._run_in_tmux
    lane_mod._run_in_tmux = fake_tmux
    try:
        launch(
            role="implementer", vendor="driver-c", spec_file=str(spec), cwd=str(tmp_path),
            runner=None, dry_run=False, tmux=True, config_home=tmp_path, session_id="s",
        )
    finally:
        lane_mod._run_in_tmux = orig
    assert captured["stdin"] is None
    assert captured["inner"][:2] == ["sh", "-c"]


def test_gates_follow_a_non_default_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """review-2 needs driver-c review-1 gates on a driver-c-first task; gate-blocked recovers on them."""
    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)

    def finished(role: str, vendor: str) -> str:
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", role, "--vendor", vendor])
        aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
        _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
        capsys.readouterr()
        return aid

    head = "abcdef0"
    for dim in ("quality", "logic"):
        aid = finished(f"pr-reviewer-{dim}", "driver-c")
        _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-1", "--dimension", dim, "--vendor", "driver-c", "--verdict", "approved", "--head", head, "--agent", aid])
    _run(tmp_path, ["task", "state", tid, "pr-review"])
    q = finished("pr-reviewer-quality", "driver-b")
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-2", "--dimension", "quality", "--vendor", "driver-b", "--verdict", "unavailable", "--head", head, "--agent", q, "--evidence", "cli down"])
    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("task", tid)["state"] == "gate-blocked"
    finally:
        store.close()
    for dim in ("quality", "logic"):
        aid = finished(f"pr-reviewer-{dim}", "driver-b")
        _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "review-2", "--dimension", dim, "--vendor", "driver-b", "--verdict", "approved", "--head", head, "--agent", aid])
    store = open_store()
    try:
        assert store.row("task", tid)["state"] == "pr-review"
    finally:
        store.close()


def test_task_models_changes_second_for_new_agents_and_gates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)

    def finished(role: str, vendor: str, stage: str | None = None) -> str:
        argv = [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", role, "--vendor", vendor,
        ]
        if stage is not None:
            argv.extend(["--stage", stage])
        _run(tmp_path, argv)
        aid = [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("agent ")
        ][-1].split()[1]
        _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
        capsys.readouterr()
        return aid

    head = "abcdef0"
    for dimension in ("quality", "logic"):
        aid = finished(f"pr-reviewer-{dimension}", "driver-c")
        _run(
            tmp_path,
            [
                "gate", "record", "--task", tid, "--stage", "review-1",
                "--dimension", dimension, "--vendor", "driver-c",
                "--verdict", "approved", "--head", head, "--agent", aid,
            ],
        )
    capsys.readouterr()
    _run(tmp_path, ["task", "state", tid, "pr-review"])
    old_aid = finished("pr-reviewer-quality", "driver-b", "review-2")
    _run(
        tmp_path,
        [
            "gate", "record", "--task", tid, "--stage", "review-2",
            "--dimension", "quality", "--vendor", "driver-b",
            "--verdict", "approved", "--head", head, "--agent", old_aid,
        ],
    )
    capsys.readouterr()

    _run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "second",
            "--vendor", "driver-d", "--reason", "driver-b quota exhausted",
        ],
    )
    assert capsys.readouterr().out == (
        f"task {tid} models.second=driver-d (was driver-b)\n"
    )

    with pytest.raises(SystemExit, match="stage review-2 requires --vendor driver-d"):
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--stage", "review-2",
                "--vendor", "driver-b",
            ],
        )
    aid = finished("pr-reviewer-quality", "driver-d", "review-2")
    with pytest.raises(SystemExit, match="stage review-2 requires vendor driver-d"):
        _run(
            tmp_path,
            [
                "gate", "record", "--task", tid, "--stage", "review-2",
                "--dimension", "quality", "--vendor", "driver-b",
                "--verdict", "approved", "--head", head, "--agent", aid,
            ],
        )
    _run(
        tmp_path,
        [
            "gate", "record", "--task", tid, "--stage", "review-2",
            "--dimension", "quality", "--vendor", "driver-d",
            "--verdict", "approved", "--head", head, "--agent", aid,
        ],
    )
    capsys.readouterr()

    _run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["models"] == {"first": "driver-c", "second": "driver-d"}
    assert task["model_changes"][-1]["slot"] == "second"
    assert task["model_changes"][-1]["from"] == "driver-b"
    assert task["model_changes"][-1]["to"] == "driver-d"
    assert task["model_changes"][-1]["reason"] == "driver-b quota exhausted"

    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        gates = store.rows("review_gate")
        assert {(g["stage"], g["dimension"], g["vendor"]) for g in gates} == {
            ("review-1", "quality", "driver-c"),
            ("review-1", "logic", "driver-c"),
            ("review-2", "quality", "driver-b"),
            ("review-2", "quality", "driver-d"),
        }
    finally:
        store.close()


def test_task_models_changes_implementer_and_omits_a_redundant_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path, implementer="driver-c")
    tid = _task(tmp_path, capsys)

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert "implementer" not in (store.row("task", tid) or {})
    finally:
        store.close()

    _run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "implementer",
            "--vendor", "driver-e", "--reason", "use the separate implementer",
        ],
    )
    assert capsys.readouterr().out == (
        f"task {tid} implementer=driver-e (was driver-c)\n"
    )
    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        assert task["models"] == {"first": "driver-c", "second": "driver-b"}
        assert task["implementer"] == "driver-e"
    finally:
        store.close()

    _run(tmp_path, ["task", "state", tid, "implementing"])
    _run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="implementer requires --vendor driver-e"):
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "implementer", "--vendor", "driver-c", "--round", "1",
            ],
        )
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "driver-e", "--round", "1",
        ],
    )
    capsys.readouterr()

    _run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "implementer",
            "--vendor", "driver-c", "--reason", "follow model slot first again",
        ],
    )
    assert capsys.readouterr().out == (
        f"task {tid} implementer=driver-c (was driver-e)\n"
    )
    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        assert "implementer" not in task
        assert [change["to"] for change in task["model_changes"]] == [
            "driver-e",
            "driver-c",
        ]
    finally:
        store.close()


def test_task_models_changes_implementer_after_a_blocked_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["task", "state", tid, "implementing"])
    _run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "driver-c", "--round", "1",
        ],
    )
    aid = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent ")
    ][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "blocked"])
    capsys.readouterr()

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("task", tid)["state"] == "failed"
    finally:
        store.close()

    _run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "implementer",
            "--vendor", "driver-e", "--reason", "driver-c implementer unavailable",
        ],
    )
    assert capsys.readouterr().out == (
        f"task {tid} implementer=driver-e (was driver-c)\n"
    )
    _run(tmp_path, ["round", "start", "--task", tid])
    assert capsys.readouterr().out == f"task {tid} round 2\n"
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "driver-e", "--round", "2",
        ],
    )
    assert capsys.readouterr().out.startswith("agent ")


def test_task_models_first_change_moves_an_absent_implementer_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path, implementer="driver-c")
    tid = _task(tmp_path, capsys)
    _run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "first",
            "--vendor", "driver-d", "--reason", "driver-c quota exhausted",
        ],
    )
    assert capsys.readouterr().out == (
        f"task {tid} models.first=driver-d (was driver-c)\n"
    )

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        assert task["models"] == {"first": "driver-d", "second": "driver-b"}
        assert "implementer" not in task
    finally:
        store.close()

    _run(tmp_path, ["task", "state", tid, "implementing"])
    _run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="implementer requires --vendor driver-d"):
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "implementer", "--vendor", "driver-c", "--round", "1",
            ],
        )
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "driver-d", "--round", "1",
        ],
    )
    assert capsys.readouterr().out.startswith("agent ")


def test_task_models_keeps_recorded_agent_rows_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "pr-reviewer-quality", "--vendor", "driver-c",
        ],
    )
    aid = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent ")
    ][-1].split()[1]

    _run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "first",
            "--vendor", "driver-d", "--reason", "driver-c quota exhausted",
        ],
    )
    capsys.readouterr()
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("agent", aid)["vendor"] == "driver-c"
    finally:
        store.close()
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    assert f"agent {aid} verdict=approved" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="stage review-1 requires --vendor driver-d"):
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--stage", "review-1",
                "--vendor", "driver-c",
            ],
        )


def test_task_models_refuses_invalid_or_unavailable_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    base = ["task", "models", "--id", tid, "--slot", "second"]

    def new_task() -> str:
        _run(
            tmp_path,
            [
                "task", "create", "--session", "s", "--workflow", "implement",
                "--title", "Ship",
            ],
        )
        return [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("task ")
        ][-1].split()[1]

    with pytest.raises(SystemExit, match="unknown task"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", "missing-task", "--slot", "second",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )
    with pytest.raises(SystemExit, match="slot must be"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", tid, "--slot", "third",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )
    with pytest.raises(SystemExit, match="vendor must be"):
        _run(tmp_path, [*base, "--vendor", "driver-x", "--reason", "quota"])
    with pytest.raises(SystemExit, match="--reason must not be empty"):
        _run(tmp_path, [*base, "--vendor", "driver-d", "--reason", "   "])
    with pytest.raises(SystemExit, match="models.first and models.second must be different"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", tid, "--slot", "first",
                "--vendor", "driver-b", "--reason", "duplicate the second slot",
            ],
        )
    with pytest.raises(SystemExit, match="implementer must differ from models.second"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", tid, "--slot", "implementer",
                "--vendor", "driver-b", "--reason", "duplicate the second slot",
            ],
        )

    for state in ("done", "merged", "superseded"):
        os.environ["AGENT_HOME"] = str(tmp_path)
        store = open_store()
        try:
            closed = {
                key: value
                for key, value in (store.row("task", tid) or {}).items()
                if not key.startswith("_")
            }
            closed["state"] = state
            store.write("task", "update", tid, closed)
        finally:
            store.close()
        with pytest.raises(SystemExit, match="closed task"):
            _run(tmp_path, [*base, "--vendor", "driver-d", "--reason", "quota"])

    legacy_tid = new_task()
    _remove_task_models(tmp_path, legacy_tid)
    with pytest.raises(SystemExit, match="task models is not configured"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", legacy_tid, "--slot", "second",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )

    external_tid = new_task()
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        external = {
            key: value
            for key, value in (store.row("task", external_tid) or {}).items()
            if not key.startswith("_")
        }
        external["models"] = {"first": "driver-a", "second": None}
        store.write("task", "update", external_tid, external)
    finally:
        store.close()
    with pytest.raises(SystemExit, match="second review is external"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", external_tid, "--slot", "second",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )


def test_task_models_refuses_coordinator_tasks_without_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path)
    tid = _task(tmp_path, capsys)
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        task = {
            key: value
            for key, value in (store.row("task", tid) or {}).items()
            if not key.startswith("_")
        }
        task["payload"] = {"coordinator": {"a38": {"enabled": True}}}
        store.write("task", "update", tid, task)
        before = store.row("task", tid)
    finally:
        store.close()

    with pytest.raises(
        SystemExit,
        match="coordinator tasks keep the model slots pinned from ai-accounts.json when the coordinator accepted them",
    ):
        _run(
            tmp_path,
            [
                "task", "models", "--id", tid, "--slot", "first",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("task", tid) == before
    finally:
        store.close()


def test_task_models_refuses_a_task_owned_by_another_device(tmp_path: Path) -> None:
    from agent_cli.store import Store

    write_drivers(tmp_path)
    _run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "foreign-session",
                "payload": {
                    "id": "foreign-session",
                    "kind": "human",
                    "status": "active",
                    "skills": ["spine"],
                },
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 2,
                "table": "task",
                "op": "insert",
                "row_id": "foreign-task",
                "payload": {
                    "id": "foreign-task",
                    "session_id": "foreign-session",
                    "workflow": "implement",
                    "state": "implementing",
                    "models": {"first": "driver-c", "second": "driver-b"},
                },
                "occurred_at": "2026-08-13T12:00:01Z",
            }
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="owned by another device"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", "foreign-task", "--slot", "second",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )


def test_task_models_requires_a_spine_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    write_driver_c_first(tmp_path)
    _run(tmp_path, ["init"])
    _run(
        tmp_path,
        ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"],
    )
    _run(
        tmp_path,
        [
            "task", "create", "--session", "s", "--workflow", "implement",
            "--title", "Ship",
        ],
    )
    tid = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("task ")
    ][-1].split()[1]
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        session = {
            key: value
            for key, value in (store.row("session", "s") or {}).items()
            if not key.startswith("_")
        }
        session["skills"] = []
        store.write("session", "update", "s", session)
    finally:
        store.close()
    with pytest.raises(SystemExit, match="does not have skill spine"):
        _run(
            tmp_path,
            [
                "task", "models", "--id", tid, "--slot", "second",
                "--vendor", "driver-d", "--reason", "quota",
            ],
        )



def test_agent_models_task_prints_the_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _task(tmp_path, capsys)  # pinned driver-a/driver-b
    write_driver_c_first(tmp_path)
    _run(tmp_path, ["models", "first"])
    assert capsys.readouterr().out == "driver-c\n"
    _run(tmp_path, ["models", "first", "--task", tid])
    assert capsys.readouterr().out == "driver-a\n"
    _run(tmp_path, ["models", "implementer", "--task", tid])
    assert capsys.readouterr().out == "driver-a\n"
    _run(tmp_path, ["models", "--task", tid, "--json"])
    assert json.loads(capsys.readouterr().out) == {"first": "driver-a", "second": "driver-b"}
    with pytest.raises(SystemExit, match="--task needs a value"):
        _run(tmp_path, ["models", "--task"])


def test_agent_models_implementer_prints_device_setting_or_task_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_driver_c_first(tmp_path, implementer="driver-e")
    tid = _task(tmp_path, capsys)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["implementer"] = "driver-c"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    _run(tmp_path, ["models", "implementer"])
    assert capsys.readouterr().out == "driver-c\n"
    _run(tmp_path, ["models", "implementer", "--task", tid])
    assert capsys.readouterr().out == "driver-e\n"
    _run(tmp_path, ["models", "implementer", "--task", tid, "--json"])
    assert json.loads(capsys.readouterr().out) == {"implementer": "driver-e"}


@pytest.mark.no_pg
def test_misnamed_models_key_stops_load_model_slots(tmp_path: Path) -> None:
    (tmp_path / "ai-accounts.json").write_text(json.dumps({"model": {"first": "driver-c", "second": "driver-b"}}), encoding="utf-8")
    with pytest.raises(AccountError, match="accepts only"):
        load_model_slots(tmp_path)


# --- model slots from drivers.json -------------------------------------------

DRIVER_HINT = "(see drivers.json, docs/ai-accounts.md#drivers)"


@pytest.mark.no_pg
def test_device_selections_name_configured_drivers() -> None:
    lanes = ("driver-a", "driver-b", "driver-c")
    assert parse_model_slots(
        {"first": "driver-a", "second": "driver-b"}, lanes
    ) == ModelSlots("driver-a", "driver-b")
    assert parse_model_slots({"first": "driver-c", "second": None}, lanes) == ModelSlots(
        "driver-c", None
    )
    with pytest.raises(ModelSlotsError) as first:
        parse_model_slots({"first": "driver-e", "second": "driver-b"}, lanes)
    assert str(first.value) == (
        f"models.first must be one of driver-a|driver-b|driver-c {DRIVER_HINT}"
    )
    with pytest.raises(ModelSlotsError) as second:
        parse_model_slots({"first": "driver-a", "second": "driver-x"}, lanes)
    assert str(second.value).startswith(
        "models.second must be null or one of driver-a|driver-b|driver-c"
    )
    with pytest.raises(ModelSlotsError) as empty:
        parse_model_slots({"first": "driver-a", "second": None}, ())
    assert str(empty.value) == (
        f"models.first must be one of (no driver configured) {DRIVER_HINT}"
    )
    models = ModelSlots("driver-a", "driver-b")
    assert parse_implementer("driver-e", models, ("driver-a", "driver-e")) == "driver-e"
    with pytest.raises(ModelSlotsError) as implementer:
        parse_implementer("driver-x", models, ("driver-a", "driver-e"))
    assert str(implementer.value) == (
        f"implementer must be one of driver-a|driver-e {DRIVER_HINT}"
    )
    with pytest.raises(ModelSlotsError, match="implementer must differ from models.second"):
        parse_implementer("driver-b", models, ("driver-a", "driver-b"))


@pytest.mark.no_pg
def test_stored_pins_check_their_shape_only() -> None:
    # A task recorded on a device with other drivers stays readable here.
    pinned = {"models": {"first": "driver-x", "second": "driver-y"}, "implementer": "driver-z"}
    assert models_from_snapshot(pinned) == ModelSlots("driver-x", "driver-y")
    assert models_for_new_record(pinned) == ModelSlots("driver-x", "driver-y")
    assert implementer_from_snapshot(pinned) == "driver-z"
    for bad in ("second", "a b", "", 7, "x" * 33):
        with pytest.raises(PinnedModelSlotsError, match="models.first must be a driver id"):
            models_from_snapshot({"models": {"first": bad, "second": "driver-y"}})
    with pytest.raises(PinnedModelSlotsError, match="models.second must be null or a driver id"):
        models_for_new_record({"models": {"first": "driver-x", "second": "a/b"}})
    with pytest.raises(PinnedModelSlotsError, match="implementer must be a driver id"):
        implementer_from_snapshot({**pinned, "implementer": "first"})


@pytest.mark.no_pg
def test_models_command_reads_the_driver_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_drivers(tmp_path)
    data = {
        "models": {"first": "driver-c", "second": "driver-b"},
        "implementer": "driver-e",
    }
    (tmp_path / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")
    _run(tmp_path, ["models", "--json"])
    assert json.loads(capsys.readouterr().out) == {"first": "driver-c", "second": "driver-b"}
    _run(tmp_path, ["models", "implementer"])
    assert capsys.readouterr().out.strip() == "driver-e"
    (tmp_path / "drivers.json").unlink()
    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, ["models"])
    message = str(excinfo.value)
    assert message.startswith("agent: drivers is not configured")
    assert "drivers.json" in message and "docs/ai-accounts.md" in message


def test_cli_vendor_flags_follow_the_driver_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_drivers(tmp_path)
    write_accounts(tmp_path)
    tid = _task(tmp_path, capsys)
    lane_choices = "\\|".join(LANE_DRIVERS)
    declared_choices = "\\|".join(DECLARED_DRIVERS)
    models = ["task", "models", "--id", tid, "--reason", "operator choice"]
    with pytest.raises(SystemExit, match=f"vendor must be {lane_choices} \\(see drivers.json"):
        _run(tmp_path, [*models, "--slot", "first", "--vendor", "driver-e"])
    with pytest.raises(SystemExit, match=f"vendor must be {declared_choices} \\(see"):
        _run(tmp_path, [*models, "--slot", "implementer", "--vendor", "driver-x"])
    _run(tmp_path, [*models, "--slot", "implementer", "--vendor", "driver-e"])
    _run(tmp_path, [*models, "--slot", "first", "--vendor", "driver-c"])
    _run(tmp_path, ["models", "--task", tid, "--json"])
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "first": "driver-c",
        "second": "driver-b",
    }
    with pytest.raises(SystemExit, match=f"vendor must be {lane_choices} "):
        _run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid, "--role",
                "reviewer", "--vendor", "driver-e", "--round", "1",
            ],
        )
    with pytest.raises(SystemExit, match=f"vendor must be {lane_choices} "):
        _run(
            tmp_path,
            [
                "gate", "record", "--task", tid, "--stage", "review-1",
                "--dimension", "quality", "--vendor", "driver-e", "--verdict",
                "approved", "--head", "a" * 40, "--agent", "agent-id",
            ],
        )
    (tmp_path / "drivers.json").unlink()
    with pytest.raises(SystemExit, match="drivers is not configured"):
        _run(tmp_path, [*models, "--slot", "first", "--vendor", "driver-d"])
