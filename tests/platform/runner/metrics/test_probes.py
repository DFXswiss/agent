"""U-RUN-metrics probe execution, device probe and host side of the VM probe (08-monitoring.md section 1: fixed argv,
5 s timeout, a failing probe yields `probe_ok 0`, never an exception)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.metrics import device, probes, vm
from agent_runner.metrics.parsers import ProbeError
from agent_runner.metrics.probes import Tools
from agent_runner.vm.lima import Instance

from .conftest import FakeRun, fixture, tools

WALL = 1795218247.0 + 3600.5


# -- probes -------------------------------------------------------------------------------------------------------


def test_run_probe_runs_the_argv_and_maps_failures() -> None:
    done = probes.run_probe([sys.executable, "-c", "print('ok')"], 5.0)
    assert (done.returncode, done.stdout) == (0, "ok\n")
    assert probes.run_probe(["/nonexistent/run15-tool"], 5.0).returncode == 127
    assert probes.run_probe([sys.executable, "-c", "import time; time.sleep(5)"], 0.2).returncode == 124


def test_read_text(tmp_path: Path) -> None:
    (tmp_path / "ok").write_text("123\n", encoding="utf-8")
    (tmp_path / "bad").write_bytes(b"\xff\xfe")
    assert probes.read_text(str(tmp_path / "ok")) == "123\n"
    assert probes.read_text(str(tmp_path / "bad")) is None
    assert probes.read_text(str(tmp_path / "missing")) is None


def test_probe_accepts_listed_exit_codes_and_swallows_parse_errors(fake_run: FakeRun) -> None:
    def parse(text: str) -> str:
        if text == "bad":
            raise ProbeError("bad")
        return text.upper()

    fake_run.answers = {"one": (1, "out"), "zero": (0, "out"), "bad": (0, "bad")}
    use = tools(fake_run)

    async def body() -> list[str | None]:
        return [
            await probes.probe(use, ["/bin/zero"], parse),
            await probes.probe(use, ["/bin/one"], parse),
            await probes.probe(use, ["/bin/one"], parse, frozenset({0, 1})),
            await probes.probe(use, ["/bin/bad"], parse),
        ]

    assert asyncio.run(body()) == ["OUT", None, "OUT", None]


def test_default_tools_run_in_a_thread() -> None:
    default = Tools()
    assert default.run is probes.run_probe
    assert asyncio.run(probes.probe(default, [sys.executable, "-c", "print(7)"], str.strip)) == "7"


# -- device -------------------------------------------------------------------------------------------------------


def test_device_sample_from_recorded_output(fake_run: FakeRun) -> None:
    values = asyncio.run(device.sample(tools(fake_run), {"LIMA_HOME": "/lima"}, WALL))
    assert values == device.DeviceValues(
        cpu_ratio=round((100 - 78.83) / 100 * 10, 4),
        mem_used_bytes=(765039 + 163533 + 343534) * 16384,
        mem_total_bytes=34359738368,
        disk_used_bytes=838309272 * 1024,
        disk_total_bytes=971298980 * 1024,
        net_rx_bytes=6733903200 + 1028934,
        net_tx_bytes=111747567845 + 844937,
        uptime_seconds=3600,
        ok=True,
    )
    assert sorted(fake_run.calls) == sorted(
        [list(device.SYSCTL_ARGV), list(device.TOP_ARGV), list(device.VM_STAT_ARGV), ["/bin/df", "-k", "/lima"],
         list(device.NETSTAT_ARGV)]
    )  # fmt: skip


def test_device_sample_with_failing_probes(fake_run: FakeRun) -> None:
    fake_run.answers["sysctl"] = (1, "sysctl: unknown oid 'kern.boottime'\n")
    fake_run.answers["df"] = (1, "")
    values = asyncio.run(device.sample(tools(fake_run), {}, WALL))
    assert values.ok is False
    assert (values.cpu_ratio, values.mem_total_bytes, values.uptime_seconds, values.disk_used_bytes) == (
        None,
        None,
        None,
        None,
    )
    assert values.mem_used_bytes is not None
    assert values.net_rx_bytes is not None


def test_device_boot_time_in_the_future_is_zero_uptime(fake_run: FakeRun) -> None:
    assert asyncio.run(device.sample(tools(fake_run), {}, 1000.0)).uptime_seconds == 0


def test_lima_home() -> None:
    assert device.lima_home({"LIMA_HOME": "/x"}) == "/x"
    assert device.lima_home({"HOME": "/home/runner"}) == "/home/runner/.lima"
    assert device.lima_home({}) == os.path.join(os.path.expanduser("~"), ".lima")


# -- VM host side -------------------------------------------------------------------------------------------------


def instance(tmp_path: Path, name: str, status: str = "Running", ha: str | None = "92989") -> Instance:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "disk").write_bytes(b"\0" * 8192)
    if ha is not None:
        (directory / "ha.pid").write_text(ha + "\n", encoding="utf-8")
    return Instance(name, status, str(directory), 4, 8 * 1024**3, 20 * 1024**3, {})


def answers_for(*instances: Instance) -> dict[str, tuple[int, str]]:
    lsof = "".join(
        fixture("lsof.txt").replace("{VZ}", str(92995 + index)).replace("{LIMA}", item.dir)
        for index, item in enumerate(instances)
    )
    return {"lsof": (1, lsof), "ps": (0, fixture("ps.txt")), "nettop": (0, fixture("nettop.txt"))}


def test_host_values_of_running_instances(tmp_path: Path) -> None:
    one = instance(tmp_path, "wp-test-arp-1")
    stopped = instance(tmp_path, "wp-test-arp-2", status="Stopped")
    run = FakeRun(answers_for(one))
    found = asyncio.run(vm.host_values(tools(run), [one, stopped]))
    blocks = os.stat(Path(one.dir) / "disk").st_blocks * 512
    assert found == {
        "wp-test-arp-1": vm.HostVm(
            vz_pid=92995,
            hostagent_pid=92989,
            cpu_ratio=0.375,
            memory_bytes=(1383216 + 143376) * 1024,
            uptime_seconds=93882,
            disk_used_bytes=blocks,
            net_rx_bytes=2940810,
            net_tx_bytes=390411,
        )
    }
    assert run.calls[0] == ["/usr/sbin/lsof", "-F", "pn", f"{one.dir}/disk"]
    assert run.calls[1] == ["/bin/ps", "-o", "pid=,%cpu=,rss=,etime=", "-p", "92989,92995"]
    assert run.calls[2] == list(vm.NETTOP_ARGV)


def test_host_values_without_running_instances_run_nothing(tmp_path: Path) -> None:
    run = FakeRun({})
    assert asyncio.run(vm.host_values(tools(run), [instance(tmp_path, "a", status="Stopped")])) == {}
    assert run.calls == []


def test_host_values_when_the_probes_fail(tmp_path: Path) -> None:
    one = instance(tmp_path, "wp-test-arp-1")

    def failing_stat(path: str) -> os.stat_result:
        raise FileNotFoundError(path)

    run = FakeRun({"lsof": (2, ""), "ps": (1, "garbage"), "nettop": (1, "")})
    found = asyncio.run(vm.host_values(tools(run, stat=failing_stat), [one]))
    assert found["wp-test-arp-1"] == vm.HostVm(None, 92989, None, None, None, None, None, None)
    # ps still runs for the hostagent; nettop for its bytes
    assert [call[0] for call in run.calls] == ["/usr/sbin/lsof", "/bin/ps", "/usr/bin/nettop"]


def test_host_values_without_hostagent_pid(tmp_path: Path) -> None:
    one = instance(tmp_path, "wp-test-arp-1", ha=None)
    two = instance(tmp_path, "wp-test-arp-2", ha="garbage")
    run = FakeRun(answers_for(one, two))
    found = asyncio.run(vm.host_values(tools(run), [one, two]))
    assert found["wp-test-arp-1"].hostagent_pid is None
    assert found["wp-test-arp-1"].memory_bytes == 1383216 * 1024  # VZ only
    assert found["wp-test-arp-1"].net_rx_bytes is None
    assert "/usr/bin/nettop" not in [call[0] for call in run.calls]


def test_host_values_without_any_pid_skip_ps(tmp_path: Path) -> None:
    one = instance(tmp_path, "wp-test-arp-1", ha="0")
    run = FakeRun({"lsof": (1, "")})
    found = asyncio.run(vm.host_values(tools(run), [one]))
    assert found["wp-test-arp-1"].vz_pid is None
    assert [call[0] for call in run.calls] == ["/usr/sbin/lsof"]


# -- guest pull failures ------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.closed = False

    async def metrics(self) -> dict[str, Any]:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome == "slow":
            await asyncio.sleep(10)
        return dict(self.outcome)

    async def aclose(self) -> None:
        self.closed = True


class FakeEnv:
    def __init__(self, client: FakeClient | BaseException) -> None:
        self.answer = client

    async def client(self, row: Any) -> FakeClient:
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class FakeManager:
    def __init__(self, client: FakeClient | BaseException) -> None:
        self.env = FakeEnv(client)


@pytest.mark.parametrize(
    "outcome",
    [RunnerError("vm_token_mismatch"), OSError("socket")],
    ids=["token", "socket-path"],
)
def test_guest_metrics_without_a_client_is_none(outcome: BaseException) -> None:
    assert asyncio.run(vm.guest_metrics(FakeManager(outcome), object())) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "outcome", [GuestUnreachable("metrics"), {"vm": {}}, "slow"], ids=["unreachable", "invalid", "timeout"]
)
def test_guest_metrics_failures_are_none_and_close_the_client(outcome: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "GUEST_TIMEOUT_S", 0.05)
    client = FakeClient(outcome)
    assert asyncio.run(vm.guest_metrics(FakeManager(client), object())) is None  # type: ignore[arg-type]
    assert client.closed
