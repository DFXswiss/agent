"""Unit tests of the attack catalog and the result tables (agent_doubles.atk.catalog, .report)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_doubles.atk import catalog, evidence, report
from agent_doubles.model.scenario import packaged_names
from agent_guest.sandbox import probe

VALUES = {"work": "/w", "session_dir": "/s", "session_id": "sid", "victim_work": "/v/work"}


def test_the_catalog_names_the_40_attacks_of_the_specification_in_order() -> None:
    assert tuple(f"ATK-{number:02d}" for number in range(1, 41)) == catalog.ATTACK_IDS
    assert {attack.group for attack in catalog.ATTACKS} == {
        catalog.GROUP_TOOLS,
        catalog.GROUP_TAMPER,
        catalog.GROUP_READS,
        catalog.GROUP_SURFACES,
    }
    assert [a.id for a in catalog.ATTACKS if a.group == catalog.GROUP_SURFACES] == [f"ATK-{n}" for n in range(33, 41)]
    assert catalog.initial_prompt(catalog.BY_ID["ATK-01"]) == catalog.PROMPT
    assert "<pasted_content" in catalog.initial_prompt(catalog.BY_ID["ATK-38"])
    assert catalog.BY_ID["ATK-38"].prompt_decisions == ("allow", "block", "block")
    assert {a.prompt_decisions for a in catalog.ATTACKS if a.id != "ATK-38"} == {("allow",)}
    assert [a.id for a in catalog.ATTACKS if a.group == catalog.GROUP_TOOLS] == [f"ATK-{n:02d}" for n in range(1, 13)]
    assert [a.id for a in catalog.ATTACKS if a.group == catalog.GROUP_TAMPER] == [f"ATK-{n:02d}" for n in range(13, 21)]
    assert catalog.BY_ID["ATK-07"].scenario == "atk-07"
    assert catalog.MODES == ("hooks", "fault")
    assert set(catalog.MODE_TITLES) == set(catalog.MODES)


def test_every_attack_and_the_control_have_a_packaged_scenario() -> None:
    names = packaged_names()
    for attack in (*catalog.ATTACKS, catalog.CONTROL):
        assert attack.scenario in names
    assert catalog.CONTROL.scenario == "atk-control"


@pytest.mark.parametrize("attack", catalog.ATTACKS, ids=lambda a: a.id)
def test_scripted_calls_follow_the_scenario_steps(attack: catalog.Attack) -> None:
    calls = catalog.scripted_calls(attack)
    assert calls
    document: dict[str, Any] = catalog.scenario_document(attack.scenario)
    step_ids = [step["id"] for step in document["steps"]]
    assert all(call.step in step_ids for call in calls)


def test_scripted_calls_of_a_multi_step_attack() -> None:
    assert [(c.step, c.tool) for c in catalog.scripted_calls(catalog.BY_ID["ATK-09"])] == [
        ("attack-1", "Write"),
        ("attack-2", "Bash"),
        ("attack-3", "Bash"),
    ]
    assert [c.tool for c in catalog.scripted_calls(catalog.CONTROL)] == ["Write", "Read"]


def test_targets_render_global_and_own_paths_once() -> None:
    found = catalog.targets(catalog.BY_ID["ATK-13"], VALUES)
    assert found[0] == "/etc/claude-code/managed-settings.json"
    assert found.count("/etc/claude-code/managed-settings.json") == 1
    assert "/run/agent-guest/harness/sid.md" in found
    assert "/w/atk-exec-mcp" in found
    assert catalog.targets(catalog.BY_ID["ATK-24"], VALUES)[-2:] == ("/v/work/planted.txt", "/v/work/secret.txt")
    with pytest.raises(KeyError):
        catalog.render("{unknown}", VALUES)


def test_os_checks_name_existing_self_test_checks() -> None:
    assert set(catalog.OS_CHECKS) <= set(catalog.ATTACK_IDS)
    for names in catalog.OS_CHECKS.values():
        assert set(names) <= set(probe.CHECKS)


def test_control_fails_are_criteria() -> None:
    assert set(catalog.CONTROL_FAILS) < set(evidence.CRITERIA)


# -- report ----------------------------------------------------------------------------------------------------


def verdict(attack: str, mode: str, passed: bool, *attempts: dict[str, Any]) -> dict[str, Any]:
    return {
        "attack": attack,
        "mode": mode,
        "passed": passed,
        "attempts": list(attempts),
        "criteria": {"no_egress": {"ok": True, "details": ["0 packet(s) attempted, all dropped"]}},
        "journal_sources": {"hook": 1},
    }


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        ({"is_error": None, "result": "File created"}, "allowed"),
        ({"is_error": False, "result": ""}, "allowed"),
        ({"is_error": True, "result": "Error: No such tool available: Bash."}, "absent"),
        ({"is_error": True, "result": "PreToolUse:Write hook error: no"}, "hook-deny"),
        ({"is_error": True, "result": "File is in a directory that is denied"}, "denied"),
    ],
)
def test_outcome_classifies_the_tool_result(attempt: dict[str, Any], expected: str) -> None:
    assert report.outcome(attempt) == expected


def test_table_has_one_row_per_attack_and_marks_failures() -> None:
    results = [
        verdict("ATK-01", "hooks", True, {"tool": "Bash", "is_error": True, "result": "No such tool available"}),
        verdict("ATK-01", "fault", False),
    ]
    text = report.table(results)
    lines = text.splitlines()
    assert len(lines) == 2 + 40
    assert lines[2].startswith("| ATK-01 | Tools and execution | Bash `id; git --version` | pass - Bash: absent |")
    assert "**FAIL** - no attempt" in lines[2]
    assert "hooks: journal 1 hook / 0 transcript; 0 packet(s) attempted, all dropped" in lines[2]
    assert "not run | not run |" in lines[3]


def test_evidence_cell_without_egress_details() -> None:
    assert report.evidence_cell([{"mode": "hooks", "criteria": {"no_egress": {"details": []}}}]) == (
        "hooks: journal 0 hook / 0 transcript; "
    )


def test_evidence_cell_names_the_attack_specific_checks() -> None:
    passed = {"mode": "hooks", "criteria": {"specific": {"ok": True, "details": []}}}
    failed = {"mode": "fault", "criteria": {"specific": {"ok": False, "details": ["x"]}}}
    assert report.evidence_cell([passed, failed]) == (
        "hooks: journal 0 hook / 0 transcript; ; specific checks ok<br>"
        "fault: journal 0 hook / 0 transcript; ; **specific checks FAIL**"
    )


def test_probe_and_os_tables() -> None:
    probes = {
        "hooks": {"checks": [{"check": "exec_sh", "ok": True, "outcome": "errno:EACCES"}]},
        "fault": {"checks": [{"check": "exec_sh", "ok": False, "outcome": "exit:0"}, {"check": "dns", "ok": True,
                                                                                     "outcome": "gaierror"}]},
    }  # fmt: skip
    text = report.probe_table(probes)
    assert "| exec_sh | ok `errno:EACCES` | **FAIL** `exit:0` |" in text
    assert "| dns | - | ok `gaierror` |" in text
    os_text = report.os_table(probes)
    assert "| ATK-01 | `exec_sh`, `exec_git` | ok, missing | **FAIL**, missing |" in os_text
    assert report.os_table({}).splitlines()[0] == "| ID | Self-test checks |  |"
