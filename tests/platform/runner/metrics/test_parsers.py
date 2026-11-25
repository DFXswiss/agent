"""U-RUN-metrics parsers of every probe command against recorded output, including failure output (08-monitoring.md
sections 1 and 7)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_runner.metrics import parsers
from agent_runner.metrics.parsers import ProbeError, ProcessStat

from .conftest import fixture

CONTRACT_FIXTURES = Path(__file__).parents[4] / "contracts" / "fixtures" / "guest" / "metrics.response"


def test_sysctl() -> None:
    assert parsers.parse_sysctl(fixture("sysctl.txt")) == (34359738368, 10, 1795218247)


@pytest.mark.parametrize(
    "text",
    ["34359738368\n10\n", "34359738368\n10\nnot a boot time\n", "34359738368\nten\n{ sec = 1, usec = 0 }\n",
     "34359738368\n0\n{ sec = 1, usec = 0 }\n", ""],
)  # fmt: skip
def test_sysctl_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_sysctl(text)


def test_top_uses_the_last_sample() -> None:
    assert parsers.parse_top_idle(fixture("top.txt")) == 78.83


@pytest.mark.parametrize("text", ["", "Processes: 1 total\n", "CPU usage: 1.0% user, 1.0% sys, 120.5% idle\n"])
def test_top_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_top_idle(text)


def test_vm_stat() -> None:
    assert parsers.parse_vm_stat(fixture("vm_stat.txt") + "not a counter line\n") == (765039 + 163533 + 343534) * 16384


@pytest.mark.parametrize(
    "text", ["", "Mach Virtual Memory Statistics\n", "(page size of 4096 bytes)\nPages active: 1.\n"]
)
def test_vm_stat_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_vm_stat(text)


def test_df() -> None:
    assert parsers.parse_df(fixture("df.txt")) == (838309272 * 1024, 971298980 * 1024)


@pytest.mark.parametrize("text", ["", fixture("df-missing.stderr"), "Filesystem\n/dev/disk1 lots\n", "a\nb\nc\n"])
def test_df_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_df(text)


def test_netstat_counts_active_non_tunnel_links() -> None:
    # en0 and awdl0 carry traffic; lo0, utun*, gif0*, stf0* and nan0* are left out; address rows are skipped.
    assert parsers.parse_netstat(fixture("netstat.txt")) == (6733903200 + 1028934, 111747567845 + 844937)


@pytest.mark.parametrize(
    "text", ["", "Iface Mtu\n", "Name Mtu Network\nen0 1500 <Link#1> 02:00:00:00:00:01 x 0 1 2 0 3 0\n"]
)
def test_netstat_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_netstat(text)


def test_lsof() -> None:
    text = fixture("lsof.txt").replace("{VZ}", "92995").replace("{LIMA}", "/lima/one")
    text += "p92989\nf9\nn/lima/two/disk\np100\nf3\nn/lima/two/disk\n"
    assert parsers.parse_lsof(text) == {"/lima/one/disk": [92995], "/lima/two/disk": [92989, 100]}
    assert parsers.parse_lsof("") == {}


@pytest.mark.parametrize("text", ["n/lima/disk\n", "pabc\n"])
def test_lsof_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_lsof(text)


@pytest.mark.parametrize(
    ("text", "seconds"), [("04:42", 282), ("1:02:03", 3723), ("01-02:04:42", 93882), ("12-00:00:00", 1036800)]
)
def test_etime(text: str, seconds: int) -> None:
    assert parsers.parse_etime(text) == seconds


def test_ps() -> None:
    assert parsers.parse_ps(fixture("ps.txt") + "\n") == {
        92989: ProcessStat(0.0, 143376 * 1024, 282),
        92995: ProcessStat(37.5, 1383216 * 1024, 93882),
    }


@pytest.mark.parametrize("text", ["1 2 3\n", "1 x 3 04:42\n", "1 -1 3 04:42\n", "1 inf 3 04:42\n", "1 0.0 3 4m\n"])
def test_ps_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_ps(text)


def test_nettop() -> None:
    assert parsers.parse_nettop(fixture("nettop.txt") + "garbage line\n") == {
        1: (0, 0),
        455: (0, 5904),
        92989: (2940810, 390411),
        462: (9898, 35217),
    }


@pytest.mark.parametrize("text", ["", "time,bytes_in\n"])
def test_nettop_failures(text: str) -> None:
    with pytest.raises(ProbeError):
        parsers.parse_nettop(text)


def test_pids_text() -> None:
    assert parsers.pids_text([3, 1, 3, 2]) == "1,2,3"


# -- guest --------------------------------------------------------------------------------------------------------


def guest_answer(name: str = "session-counters.json") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CONTRACT_FIXTURES / "valid" / name).read_text(encoding="utf-8"))
    return loaded


def test_guest_metrics_with_and_without_the_optional_counters() -> None:
    parsed = parsers.parse_guest_metrics(guest_answer())
    assert parsed.vm["cpu_usage_usec"] == 10
    assert parsed.load1 == 0.25
    (session,) = parsed.sessions
    assert session.state == "idle"
    assert session.values["egress_denied"] == 7
    assert session.values["mem_max_bytes"] is None
    basic = parsers.parse_guest_metrics(guest_answer("basic.json")).sessions[0]
    assert basic.values["hook_denies"] is None
    limited = guest_answer()
    limited["sessions"][0]["mem_max_bytes"] = 4096
    limited["vm"]["load1"] = 2
    parsed = parsers.parse_guest_metrics(limited)
    assert (parsed.sessions[0].values["mem_max_bytes"], parsed.load1) == (4096, 2.0)


def test_guest_metrics_of_200_sessions_are_read_whole() -> None:
    data = guest_answer()
    (item,) = data["sessions"]
    data["sessions"] = [{**item, "session_id": f"01a1ee56-5e00-7003-8f1b-{n:012x}"} for n in range(200)]
    parsed = parsers.parse_guest_metrics(data)
    assert len(parsed.sessions) == 200  # no cap (ADR-088 section 3)


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        target: Any = data
        *parents, last = path.split(".")
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[last] = value

    return change


def _drop_uptime(data: dict[str, Any]) -> None:
    del data["vm"]["uptime_seconds"]


CHANGES: dict[str, Callable[[dict[str, Any]], None]] = {
    "vm": _set("vm", []),
    "sessions": _set("sessions", {}),
    "duplicate": lambda data: data.update(sessions=data["sessions"] * 2),
    "load-bool": _set("vm.load1", True),
    "load-negative": _set("vm.load1", -0.5),
    "load-text": _set("vm.load1", "1"),
    "vm-field": _drop_uptime,
    "session-type": _set("sessions", ["x"]),
    "session-id": _set("sessions.0.session_id", "../etc"),
    "state": _set("sessions.0.state", "hacked"),
    "negative": _set("sessions.0.io_read_bytes", -1),
    "float": _set("sessions.0.tasks_current", 1.5),
    "optional": _set("sessions.0.oom_kills", "1"),
    "limit-too-big": _set("sessions.0.mem_max_bytes", 2**60),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_guest_answers_outside_the_contract_are_refused(change: str) -> None:
    data = copy.deepcopy(guest_answer())
    CHANGES[change](data)
    with pytest.raises(ProbeError):
        parsers.parse_guest_metrics(data)
