"""ATK-01 … ATK-32 in both modes against real VMs (07-testing.md section 2.3); run by scripts/run-atk.sh only.

One parametrized case per attack and mode (``test_attack[ATK-07-fault]``); the session fixture ``atk_results`` runs
the whole suite once (both modes in parallel VMs) and every case asserts its own verdict. ``test_os_probe`` checks
the sandbox self-test that ran in an identical session unit while attack sessions were running.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_doubles.atk import catalog
from tests.platform.atk.fakes import skip_unselected

pytestmark = pytest.mark.atk


def verdict_of(results: dict[str, Any], attack_id: str, mode: str) -> dict[str, Any]:
    skip_unselected(attack_id, mode)
    found = [v for v in results["verdicts"] if v["attack"] == attack_id and v["mode"] == mode]
    if len(found) != 1:
        pytest.fail(f"{attack_id} ({mode}) did not run")
    verdict: dict[str, Any] = found[0]
    return verdict


@pytest.mark.parametrize("mode", catalog.MODES)
@pytest.mark.parametrize("attack_id", catalog.ATTACK_IDS)
def test_attack(atk_results: dict[str, Any], attack_id: str, mode: str) -> None:
    verdict = verdict_of(atk_results, attack_id, mode)
    failed = {name: c["details"] for name, c in verdict["criteria"].items() if not c["ok"]}
    assert not failed, failed
    assert verdict["passed"]


@pytest.mark.parametrize("mode", catalog.MODES)
def test_os_probe(atk_results: dict[str, Any], mode: str) -> None:
    skip_unselected(None, mode)
    probe = atk_results["os_probe"][mode]
    failed = [c for c in probe.get("checks", []) if not c["ok"]]
    assert not failed, (failed, probe.get("error"), probe.get("stderr"))
    assert probe.get("ok") is True, probe
    assert probe["claude_processes_before"] >= 1, "no attack session was running during the probe"
    checks = {c["check"]: c for c in probe.get("checks", [])}
    for attack_id, names in catalog.OS_CHECKS.items():
        for name in names:
            assert name in checks, f"{attack_id}: self-test check {name} missing"
            assert checks[name]["ok"], (attack_id, checks[name])
    assert atk_results["pool_selftest"][mode]["ok"] is True


def test_no_unknown_model_requests(atk_results: dict[str, Any]) -> None:
    assert atk_results["unknown_dispatch"] == []


@pytest.mark.parametrize("mode", catalog.MODES)
def test_detector_control(atk_results: dict[str, Any], mode: str) -> None:
    """The suite-injected violations of the control run fail exactly their criteria (the detectors fire)."""
    skip_unselected(None, mode)
    verdict = [v for v in atk_results["verdicts"] if v["attack"] == catalog.CONTROL.id and v["mode"] == mode]
    assert len(verdict) == 1
    failed = sorted(name for name, c in verdict[0]["criteria"].items() if not c["ok"])
    assert failed == sorted(catalog.CONTROL_FAILS), verdict[0]["criteria"]
