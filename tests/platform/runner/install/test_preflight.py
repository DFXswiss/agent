"""U-RUN-install-preflight: the checks of a Mac before it becomes an execution device (ADR-074 section 3): macOS on
Apple silicon at a supported version, virtualization, memory and free space, in fixed order with closed codes, and
the host memory reserve of an own Mac."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runner.install import preflight

from .conftest import Kit

GIB = 1024**3
SW_VERS = ["/usr/bin/sw_vers", "-productVersion"]
HV = ["/usr/sbin/sysctl", "-n", "kern.hv_support"]
MEMSIZE = ["/usr/sbin/sysctl", "-n", "hw.memsize"]


def good_mac(kit: Kit) -> None:
    kit.run.answer(SW_VERS, stdout="15.1\n")
    kit.run.answer(HV, stdout="1\n")
    kit.run.answer(MEMSIZE, stdout=f"{64 * GIB}\n")


def by_name(kit: Kit) -> dict[str, preflight.Check]:
    return {check.name: check for check in preflight.run_checks(kit.system)}


def test_a_suitable_mac_passes_every_check(kit: Kit) -> None:
    good_mac(kit)
    checks = preflight.run_checks(kit.system)
    assert [check.fields() for check in checks] == [
        {"name": "os", "ok": True, "value": "darwin"},
        {"name": "macos", "ok": True, "value": "15.1"},
        {"name": "arch", "ok": True, "value": "arm64"},
        {"name": "virtualization", "ok": True, "value": "1"},
        {"name": "memory", "ok": True, "value": "64GiB"},
        {"name": "disk", "ok": True, "value": "500GiB"},
    ]
    assert preflight.failures(checks) == []
    # nothing is changed: only the three read-only programs ran
    assert kit.run.calls == [SW_VERS, HV, MEMSIZE]


def test_another_operating_system_stops_at_the_first_check(kit: Kit) -> None:
    kit.system.platform = "linux"
    checks = preflight.run_checks(kit.system)
    assert [check.fields() for check in checks] == [
        {"name": "os", "ok": False, "value": "linux", "code": "unsupported_os"}
    ]
    assert preflight.failures(checks) == ["unsupported_os"]
    assert kit.run.calls == []


def test_an_unsuitable_mac_fails_with_every_code(kit: Kit) -> None:
    kit.run.answer(SW_VERS, stdout="13.6.1\n")
    kit.run.answer(HV, stdout="0\n")
    kit.run.answer(MEMSIZE, stdout=f"{8 * GIB}\n")
    kit.system.machine = lambda: "x86_64"
    kit.system.disk_free = lambda path: 49 * GIB + GIB - 1
    checks = preflight.run_checks(kit.system)
    assert [check.fields() for check in checks][1:] == [
        {"name": "macos", "ok": False, "value": "13.6", "code": "macos_too_old"},
        {"name": "arch", "ok": False, "value": "x86_64", "code": "not_apple_silicon"},
        {"name": "virtualization", "ok": False, "value": "0", "code": "virtualization_unavailable"},
        {"name": "memory", "ok": False, "value": "8GiB", "code": "memory_too_small"},
        {"name": "disk", "ok": False, "value": "49GiB", "code": "disk_too_full"},
    ]
    assert preflight.failures(checks) == [
        "macos_too_old",
        "not_apple_silicon",
        "virtualization_unavailable",
        "memory_too_small",
        "disk_too_full",
    ]


def test_unreadable_facts_count_as_failures(kit: Kit) -> None:
    kit.run.answer(SW_VERS, rc=1)
    kit.run.answer(HV, rc=1)
    kit.run.answer(MEMSIZE, rc=1)
    found = by_name(kit)
    assert found["macos"].fields() == {"name": "macos", "ok": False, "value": "unknown", "code": "macos_too_old"}
    assert found["virtualization"].value == "unknown"
    assert not found["virtualization"].ok
    assert found["memory"].fields() == {"name": "memory", "ok": False, "value": "0GiB", "code": "memory_too_small"}


def test_the_disk_is_measured_at_the_layout_root(kit: Kit) -> None:
    good_mac(kit)
    seen: list[Path] = []

    def free(path: Path) -> int:
        seen.append(path)
        return 50 * GIB

    kit.system.disk_free = free
    assert by_name(kit)["disk"].fields() == {"name": "disk", "ok": True, "value": "50GiB"}
    assert seen == [kit.layout.root]


@pytest.mark.parametrize(
    ("text", "shown", "ok"),
    [
        ("14\n", "14.0", True),
        ("14.0", "14.0", True),
        ("26.0.1", "26.0", True),
        ("13.99", "13.99", False),
        ("garbage", "unknown", False),
        ("14.1.2.3.4", "unknown", False),
        ("", "unknown", False),
    ],
)
def test_macos_versions(kit: Kit, text: str, shown: str, ok: bool) -> None:
    good_mac(kit)
    kit.run.answer(SW_VERS, stdout=text)
    check = by_name(kit)["macos"]
    assert (check.value, check.ok) == (shown, ok)


def test_memory_is_counted_in_whole_gib(kit: Kit) -> None:
    good_mac(kit)
    kit.run.answer(MEMSIZE, stdout=str(16 * GIB - 1))
    assert by_name(kit)["memory"].value == "15GiB"
    kit.run.answer(MEMSIZE, stdout=str(16 * GIB))
    assert by_name(kit)["memory"].ok
    kit.run.answer(MEMSIZE, stdout="-1")
    assert by_name(kit)["memory"].value == "0GiB"


@pytest.mark.parametrize(
    ("answer", "reserve"),
    [
        ((0, f"{16 * GIB}\n"), 4096),
        ((0, f"{64 * GIB}\n"), 16384),
        ((0, f"{256 * GIB}\n"), 32768),
        ((0, "many"), 4096),
        ((1, ""), 4096),
    ],
)
def test_the_memory_reserve_is_a_quarter_within_bounds(kit: Kit, answer: tuple[int, str], reserve: int) -> None:
    kit.run.answer(MEMSIZE, rc=answer[0], stdout=answer[1])
    assert preflight.memory_reserve_mib(kit.system) == reserve
