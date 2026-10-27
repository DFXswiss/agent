"""Model slots: which provider fills slot ``first`` and ``second`` is device configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.ai_accounts import AccountError, load_ai_accounts, load_model_slots
from agent_cli.allow import GATE_PAIRS, ready_for_done_blocking
from agent_cli.chain import handoff_prompt, step_vendor_label, steps_for, to_json
from agent_cli.coordinator_common import (
    REQUIRED_LANE_SLOTS,
    checklist_key_for_stage,
    required_lane_slots,
)
from agent_cli.lane import claude_argv, launch, provider_argv
from agent_cli.main import main
from agent_cli.model_slots import (
    DEFAULT_MODEL_SLOTS,
    SUPPORTED_PROVIDERS,
    ModelSlots,
    ModelSlotsError,
    models_from_snapshot,
    parse_model_slots,
)

CLAUDE_DIR = "/operator/path/claude-profile"
CODEX_DIR = "/operator/path/codex-profile"
CLAUDE_MODEL = "operator-claude-model"
CODEX_MODEL = "operator-codex-model"


def write_claude_first(home: Path, *, device_lanes: bool = True, session: str = "s") -> None:
    """Operator configuration: model slot first = claude, second = codex."""
    lanes = {
        "claude:implementer": "first-builder",
        "claude:reviewer": "first-reader",
        "claude:pr-reviewer-quality": "first-reader",
        "claude:pr-reviewer-logic": "first-reader",
        "codex:pr-reviewer-quality": "second-reader",
        "codex:pr-reviewer-logic": "second-reader",
    }
    data: dict = {
        "models": {"first": "claude", "second": "codex"},
        "accounts": {
            "claude-a": {"provider": "claude", "config_dir": CLAUDE_DIR},
            "codex-a": {"provider": "codex", "config_dir": CODEX_DIR},
        },
        "roles": {
            "first-builder": {"account": "claude-a", "model": CLAUDE_MODEL, "access": "workspace-write"},
            "first-reader": {"account": "claude-a", "model": CLAUDE_MODEL, "access": "read-only"},
            "second-reader": {"account": "codex-a", "model": CODEX_MODEL, "access": "read-only"},
        },
    }
    if device_lanes:
        data["lanes"] = lanes
        data["sessions"] = {}
    else:
        data["sessions"] = {session: {"interactive": None, "lanes": lanes}}
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


# --- pure model_slots -------------------------------------------------------


@pytest.mark.no_pg
def test_supported_providers_and_default_order() -> None:
    assert SUPPORTED_PROVIDERS == frozenset({"grok", "codex", "claude"})
    assert DEFAULT_MODEL_SLOTS == ModelSlots("grok", "codex")
    assert parse_model_slots(None) == DEFAULT_MODEL_SLOTS
    assert models_from_snapshot({}) == DEFAULT_MODEL_SLOTS
    assert models_from_snapshot({"models": {"first": "claude", "second": "grok"}}) == ModelSlots(
        "claude", "grok"
    )


@pytest.mark.no_pg
def test_slots_resolve_stage_and_vendor() -> None:
    models = ModelSlots("claude", "codex")
    assert models.for_slot("first") == "claude"
    assert models.for_stage("grok-pr") == "claude"
    assert models.for_stage("codex-pr") == "codex"
    assert models.slot_of("codex") == "second"
    assert models.slot_of("grok") is None
    assert models.as_dict() == {"first": "claude", "second": "codex"}
    with pytest.raises(ModelSlotsError):
        models.for_slot("third")
    with pytest.raises(ModelSlotsError):
        models.for_stage("other-pr")


@pytest.mark.no_pg
@pytest.mark.parametrize(
    "raw",
    [
        {"first": "claude"},
        {"first": "claude", "second": "codex", "third": "grok"},
        {"first": "gemini", "second": "codex"},
        {"first": "codex", "second": "codex"},
        ["claude", "codex"],
    ],
)
def test_models_block_is_validated(raw: object) -> None:
    with pytest.raises(ModelSlotsError):
        parse_model_slots(raw)


# --- ai-accounts.json -------------------------------------------------------


@pytest.mark.no_pg
def test_manifest_models_and_device_lanes(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.models == ModelSlots("claude", "codex")
    assert load_model_slots(tmp_path) == ModelSlots("claude", "codex")
    # Device-wide lanes serve any session that binds nothing of its own.
    implementer = loaded.for_lane("never-bound-session", "implementer", "claude")
    assert implementer.name == "first-builder"
    assert implementer.env_prefix()[-1] == f"CLAUDE_CONFIG_DIR={CLAUDE_DIR}"
    assert "-u" in implementer.env_prefix() and "CLAUDE_CONFIG_DIR" in implementer.env_prefix()
    # The default profile ~/.claude keeps its login outside the directory: the
    # lane clears CLAUDE_CONFIG_DIR and does not point it there.
    from agent_cli.ai_accounts import AIAccount, AIRole

    default_role = AIRole(
        "d", AIAccount("c", "claude", str(Path.home() / ".claude")), "m", "read-only"
    )
    prefix = default_role.env_prefix()
    assert prefix[-2:] == ["-u", "CLAUDE_CONFIG_DIR"]
    assert not any(a.startswith("CLAUDE_CONFIG_DIR=") for a in prefix)
    second = loaded.for_lane("never-bound-session", "pr-reviewer-logic", "codex")
    assert second.env_prefix()[-1] == f"CODEX_HOME={CODEX_DIR}"
    with pytest.raises(AccountError, match="slot grok:implementer"):
        loaded.for_lane("never-bound-session", "implementer", "grok")


@pytest.mark.no_pg
def test_session_lanes_win_over_device_lanes(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["roles"]["other-builder"] = {
        "account": "claude-a",
        "model": "other-model",
        "access": "workspace-write",
    }
    raw["sessions"] = {"s": {"interactive": None, "lanes": {"claude:implementer": "other-builder"}}}
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_ai_accounts(tmp_path)
    assert loaded.for_lane("s", "implementer", "claude").model == "other-model"
    # Unbound slot of a bound session falls back to the device-wide map.
    assert loaded.for_lane("s", "reviewer", "claude").name == "first-reader"


@pytest.mark.no_pg
def test_unconfigured_home_keeps_historical_order_and_no_lanes(tmp_path: Path) -> None:
    loaded = load_ai_accounts(tmp_path)
    assert loaded.models == DEFAULT_MODEL_SLOTS
    assert loaded.lanes == {}
    with pytest.raises(AccountError, match="No AI session configured"):
        loaded.for_lane("s", "implementer", "grok")


@pytest.mark.no_pg
@pytest.mark.parametrize(
    "change",
    ["same-provider", "unknown-provider", "missing-second", "lanes-unknown-role", "lanes-not-object"],
)
def test_malformed_models_and_lanes_rejected(tmp_path: Path, change: str) -> None:
    write_claude_first(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    if change == "same-provider":
        raw["models"]["second"] = "claude"
    elif change == "unknown-provider":
        raw["models"]["first"] = "gemini"
    elif change == "missing-second":
        del raw["models"]["second"]
    elif change == "lanes-unknown-role":
        raw["lanes"]["claude:implementer"] = "nobody"
    elif change == "lanes-not-object":
        raw["lanes"] = ["claude:implementer"]
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AccountError):
        load_ai_accounts(tmp_path)


# --- lane argv --------------------------------------------------------------


@pytest.mark.no_pg
def test_claude_argv_read_only_and_write_shapes() -> None:
    ro = claude_argv(cwd="/work", write=False, model=CLAUDE_MODEL)
    assert ro[0] == "env"
    assert "claude" in ro and "--print" in ro
    assert ro[ro.index("--model") + 1] == CLAUDE_MODEL
    assert ro[ro.index("--add-dir") + 1] == "/work"
    assert ro[ro.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in ro
    assert ro[ro.index("--permission-mode") + 1] == "default"
    disallowed = ro[ro.index("--disallowedTools") + 1 :]
    for tool in ("Edit", "Write", "Bash", "Agent", "WebSearch", "WebFetch"):
        assert tool in disallowed
    allowed = ro[ro.index("--allowedTools") + 1 : ro.index("--disallowedTools")]
    assert set(allowed) == {"Read", "Grep", "Glob"}

    rw = claude_argv(cwd="/work", write=True, model=CLAUDE_MODEL)
    assert rw[rw.index("--permission-mode") + 1] == "acceptEdits"
    allowed = rw[rw.index("--allowedTools") + 1 : rw.index("--disallowedTools")]
    assert {"Edit", "Write", "Read"} <= set(allowed)
    disallowed = rw[rw.index("--disallowedTools") + 1 :]
    assert "Bash" in disallowed and "Agent" in disallowed
    assert "Edit" not in disallowed


@pytest.mark.no_pg
def test_provider_argv_dispatch() -> None:
    assert "claude" in provider_argv("claude", spec_file="/s.md", cwd="/w", write=False, model="m")
    assert "--prompt-file" in provider_argv("grok", spec_file="/s.md", cwd="/w", write=False, model="m")
    codex = provider_argv("codex", spec_file="/s.md", cwd="/w", write=False, model="m", output_file="/o.txt")
    assert "exec" in codex and "/o.txt" in codex
    with pytest.raises(SystemExit, match="output file"):
        provider_argv("codex", spec_file="/s.md", cwd="/w", write=False, model="m")
    with pytest.raises(SystemExit, match="vendor must be claude\\|codex\\|grok"):
        provider_argv("gemini", spec_file="/s.md", cwd="/w", write=False, model="m")


@pytest.mark.no_pg
def test_launch_claude_lane_feeds_spec_on_stdin(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}

    def runner(argv: list[str], stdin_text: str | None):
        seen["argv"] = argv
        seen["stdin"] = stdin_text
        return type("Done", (), {"returncode": 0, "stdout": "ok\nSTATUS: complete\n", "stderr": ""})()

    result = launch(
        role="implementer",
        vendor="claude",
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
    assert "claude" in seen["argv"] and "--print" in seen["argv"]
    assert f"CLAUDE_CONFIG_DIR={CLAUDE_DIR}" in seen["argv"]
    assert "acceptEdits" in seen["argv"]


@pytest.mark.no_pg
def test_launch_rejects_unknown_vendor(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="vendor must be claude\\|codex\\|grok"):
        launch(
            role="implementer",
            vendor="gemini",
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
    assert steps["implementer_done"].slot is None
    assert steps["reviewer_approved"].slot == "first"
    assert steps["grok_pr_quality"].slot == "first"
    assert steps["codex_pr_logic"].slot == "second"
    models = ModelSlots("claude", "codex")
    assert step_vendor_label(steps["reviewer_approved"], models) == "claude"
    assert step_vendor_label(steps["implementer_done"], models) == "claude|codex"
    assert step_vendor_label(steps["codex_pr_quality"], models) == "codex"
    assert step_vendor_label(steps["session_registered"], models) == "-"
    assert "who=claude reviewer" in handoff_prompt(
        steps["reviewer_approved"], task_id="1", session_id="s", models=models
    )
    assert to_json(steps["grok_pr_quality"], models)["vendor"] == "claude"
    assert to_json(steps["grok_pr_quality"], models)["slot"] == "first"
    assert to_json(steps["grok_pr_quality"])["vendor"] == "grok"


@pytest.mark.no_pg
def test_gate_pairs_follow_configured_models() -> None:
    assert [slot for _, _, slot in GATE_PAIRS] == ["first", "first", "second", "second"]
    gates = [
        {"stage": "grok-pr", "dimension": "quality", "vendor": "claude", "verdict": "approved", "head_sha": "a" * 40},
        {"stage": "grok-pr", "dimension": "logic", "vendor": "claude", "verdict": "approved", "head_sha": "a" * 40},
        {"stage": "codex-pr", "dimension": "quality", "vendor": "codex", "verdict": "approved", "head_sha": "a" * 40},
        {"stage": "codex-pr", "dimension": "logic", "vendor": "codex", "verdict": "approved", "head_sha": "a" * 40},
    ]
    checklist = {
        "session_registered": "ja", "contributing_read": "ja", "contributing_ok": "ja",
        "coverage_ok": "ja", "handbook_ok": "ja", "grok_pr_quality": "ja", "grok_pr_logic": "ja",
        "codex_pr_quality": "ja", "codex_pr_logic": "ja", "deviation_declared": "n_a",
        "deviation_granted": "n_a",
    }
    task = {
        "id": "t", "workflow": "review", "checklist": checklist, "gates": gates,
        "local_checks": [], "summaries": {"en": "x", "de": "y"},
        "models": {"first": "claude", "second": "codex"},
    }
    assert ready_for_done_blocking(task) == []
    # The same gates fail under the historical order: the first slot would be grok.
    legacy = dict(task)
    legacy.pop("models")
    blocking = ready_for_done_blocking(legacy)
    assert any("grok-pr/quality=claude/approved" in b for b in blocking)


@pytest.mark.no_pg
def test_coordinator_slot_helpers() -> None:
    models = ModelSlots("claude", "codex")
    assert required_lane_slots(models) == (
        "claude:implementer",
        "claude:reviewer",
        "claude:pr-reviewer-quality",
        "claude:pr-reviewer-logic",
        "codex:pr-reviewer-quality",
        "codex:pr-reviewer-logic",
    )
    assert REQUIRED_LANE_SLOTS == required_lane_slots(DEFAULT_MODEL_SLOTS)
    assert checklist_key_for_stage("grok-pr", "quality") == "grok_pr_quality"
    assert checklist_key_for_stage("codex-pr", "logic") == "codex_pr_logic"


# --- CLI with configured slots ----------------------------------------------


def _run(home: Path, argv: list[str]) -> None:
    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def _task(home: Path, capsys: pytest.CaptureFixture[str]) -> str:
    _run(home, ["init"])
    _run(home, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    _run(home, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("task ")][-1].split()[1]


def test_cli_agent_start_follows_configured_first_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_claude_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "claude"])
    out = capsys.readouterr().out
    assert out.startswith("agent ")
    pr_agent = out.split()[1]
    _run(tmp_path, ["agent", "finish", "--id", pr_agent, "--verdict", "approved"])
    with pytest.raises(SystemExit, match="vendor must be claude\\|codex\\|grok"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-logic", "--vendor", "gemini"])
    _run(tmp_path, ["task", "state", tid, "implementing"])
    _run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="implementer requires --vendor claude\\|codex"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "implementer", "--vendor", "grok", "--round", "1"])
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "implementer", "--vendor", "claude", "--round", "1"])
    aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "done"])
    _run(tmp_path, ["task", "state", tid, "reviewing"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="reviewer requires --vendor claude"):
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "reviewer", "--vendor", "codex", "--round", "1"])


def test_cli_gate_record_requires_the_slot_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_claude_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "grok"])
    grok_agent = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", grok_agent, "--verdict", "approved"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="stage grok-pr requires vendor claude"):
        _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "grok-pr", "--dimension", "quality", "--vendor", "grok", "--verdict", "approved", "--head", "abcdef0", "--agent", grok_agent])
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "claude"])
    claude_agent = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", claude_agent, "--verdict", "approved"])
    capsys.readouterr()
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "grok-pr", "--dimension", "quality", "--vendor", "claude", "--verdict", "approved", "--head", "abcdef0", "--agent", claude_agent])
    assert "gate grok-pr/quality=approved" in capsys.readouterr().out


def test_cli_next_prints_configured_vendor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_claude_first(tmp_path)
    tid = _task(tmp_path, capsys)
    _run(tmp_path, ["next", "--task", tid, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["next"][0]["key"] == "session_registered"
    _run(tmp_path, ["close-step", "--task", tid, "--key", "session_registered", "--source", "script", "--evidence", "registered"])
    _run(tmp_path, ["close-step", "--task", tid, "--key", "spec_written", "--source", "human", "--evidence", "spec"])
    capsys.readouterr()
    _run(tmp_path, ["next", "--task", tid])
    out = capsys.readouterr().out
    assert "vendor=claude|codex" in out
    assert "who=claude|codex implementer" in out


@pytest.mark.no_pg
def test_cli_models_prints_configured_slots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(tmp_path, ["models"])
    assert capsys.readouterr().out == "first=grok\nsecond=codex\n"
    write_claude_first(tmp_path)
    _run(tmp_path, ["models"])
    assert capsys.readouterr().out == "first=claude\nsecond=codex\n"
    _run(tmp_path, ["models", "first"])
    assert capsys.readouterr().out == "claude\n"
    _run(tmp_path, ["models", "--json"])
    assert json.loads(capsys.readouterr().out) == {"first": "claude", "second": "codex"}
    _run(tmp_path, ["models", "second", "--json"])
    assert json.loads(capsys.readouterr().out) == {"second": "codex"}
    with pytest.raises(SystemExit, match="Usage: agent models"):
        _run(tmp_path, ["models", "third"])
    with pytest.raises(SystemExit, match="Usage: agent models"):
        _run(tmp_path, ["models", "--yaml"])


def test_coordinator_snapshots_carry_the_configured_models(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The coordinator feeds checklist_snapshot / _task_snapshot into chain and allow; both need models."""
    from agent_cli.coordinator_github import _task_snapshot
    from agent_cli.coordinator_lanes import checklist_snapshot
    from agent_cli.main import open_store

    write_claude_first(tmp_path)
    tid = _task(tmp_path, capsys)
    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        task = store.row("task", tid)
        assert task is not None
        snap = checklist_snapshot(store, dict(task))
        assert snap["models"] == {"first": "claude", "second": "codex"}
        task_snap = _task_snapshot(store, tid)
        assert task_snap["models"] == {"first": "claude", "second": "codex"}
    finally:
        store.close()


def test_task_pins_models_at_create(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A later device change does not re-label what a task already recorded."""
    tid = _task(tmp_path, capsys)  # no manifest yet: historical order
    _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "grok"])
    aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
    _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    write_claude_first(tmp_path)  # device now says claude first
    capsys.readouterr()
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "grok-pr", "--dimension", "quality", "--vendor", "grok", "--verdict", "approved", "--head", "abcdef0", "--agent", aid])
    assert "gate grok-pr/quality=approved" in capsys.readouterr().out
    from agent_cli.main import load_task_dict, open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("task", tid).get("models") == {"first": "grok", "second": "codex"}
        assert load_task_dict(store, tid)["models"] == {"first": "grok", "second": "codex"}
    finally:
        store.close()
    # A task created now pins claude first.
    _run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Next"])
    tid2 = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("task ")][-1].split()[1]
    store = open_store()
    try:
        assert store.row("task", tid2).get("models") == {"first": "claude", "second": "codex"}
    finally:
        store.close()


@pytest.mark.no_pg
def test_load_model_slots_ignores_unrelated_manifest_errors(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"] = {"s": {"interactive": None, "lanes": {"claude:implementer": "nobody"}}}
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AccountError):
        load_ai_accounts(tmp_path)
    assert load_model_slots(tmp_path) == ModelSlots("claude", "codex")


@pytest.mark.no_pg
def test_lane_run_rejects_a_missing_cwd(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="--cwd is not a directory"):
        _run(tmp_path, ["lane", "run", "--role", "implementer", "--vendor", "claude", "--session", "s",
                        "--spec-file", str(spec), "--cwd", str(tmp_path / "missing"), "--no-tmux"])



@pytest.mark.no_pg
def test_claude_lane_in_tmux_redirects_the_spec_file(tmp_path: Path) -> None:
    write_claude_first(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}

    def runner(argv: list[str], stdin_text: str | None):
        seen["argv"], seen["stdin"] = argv, stdin_text
        return type("Done", (), {"returncode": 0, "stdout": "STATUS: complete\n", "stderr": ""})()

    for dry in (True, False):
        result = launch(
            role="implementer", vendor="claude", spec_file=str(spec), cwd=str(tmp_path),
            runner=None if dry else runner, dry_run=dry, tmux=True, config_home=tmp_path,
            session_id="s",
        )
        argv = result.argv
        inner = argv[argv.index("--") + 1 :]
        assert inner[:5] == ["sh", "-c", 'f=$1; shift; exec "$@" < "$f"', "sh", str(spec.resolve())]
        assert "claude" in inner and "--print" in inner
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
            role="implementer", vendor="claude", spec_file=str(spec), cwd=str(tmp_path),
            runner=None, dry_run=False, tmux=True, config_home=tmp_path, session_id="s",
        )
    finally:
        lane_mod._run_in_tmux = orig
    assert captured["stdin"] is None
    assert captured["inner"][:2] == ["sh", "-c"]


def test_gates_follow_a_non_default_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """codex-pr needs claude grok-pr gates on a claude-first task; gate-blocked recovers on them."""
    write_claude_first(tmp_path)
    tid = _task(tmp_path, capsys)

    def finished(role: str, vendor: str) -> str:
        _run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", role, "--vendor", vendor])
        aid = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("agent ")][-1].split()[1]
        _run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
        capsys.readouterr()
        return aid

    head = "abcdef0"
    for dim in ("quality", "logic"):
        aid = finished(f"pr-reviewer-{dim}", "claude")
        _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "grok-pr", "--dimension", dim, "--vendor", "claude", "--verdict", "approved", "--head", head, "--agent", aid])
    _run(tmp_path, ["task", "state", tid, "pr-review"])
    q = finished("pr-reviewer-quality", "codex")
    _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "codex-pr", "--dimension", "quality", "--vendor", "codex", "--verdict", "unavailable", "--head", head, "--agent", q, "--evidence", "cli down"])
    from agent_cli.main import open_store

    os.environ["AGENT_HOME"] = str(tmp_path)
    store = open_store()
    try:
        assert store.row("task", tid)["state"] == "gate-blocked"
    finally:
        store.close()
    for dim in ("quality", "logic"):
        aid = finished(f"pr-reviewer-{dim}", "codex")
        _run(tmp_path, ["gate", "record", "--task", tid, "--stage", "codex-pr", "--dimension", dim, "--vendor", "codex", "--verdict", "approved", "--head", head, "--agent", aid])
    store = open_store()
    try:
        assert store.row("task", tid)["state"] == "pr-review"
    finally:
        store.close()



def test_agent_models_task_prints_the_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _task(tmp_path, capsys)  # pinned grok/codex
    write_claude_first(tmp_path)
    _run(tmp_path, ["models", "first"])
    assert capsys.readouterr().out == "claude\n"
    _run(tmp_path, ["models", "first", "--task", tid])
    assert capsys.readouterr().out == "grok\n"
    _run(tmp_path, ["models", "--task", tid, "--json"])
    assert json.loads(capsys.readouterr().out) == {"first": "grok", "second": "codex"}
    with pytest.raises(SystemExit, match="--task needs a value"):
        _run(tmp_path, ["models", "--task"])


@pytest.mark.no_pg
def test_misnamed_models_key_stops_load_model_slots(tmp_path: Path) -> None:
    (tmp_path / "ai-accounts.json").write_text(json.dumps({"model": {"first": "claude", "second": "codex"}}), encoding="utf-8")
    with pytest.raises(AccountError, match="accepts only"):
        load_model_slots(tmp_path)
