"""Egress: process trees, lsof parsing, classification, the log and the sampler."""

from __future__ import annotations

import time
from pathlib import Path

from orchestrator import egress
from orchestrator.config import RunConfig
from orchestrator.run import egress_patterns

PS = "  1     0 /sbin/launchd\n 10     1 hub\n 11    10 python\n 12    11 chrome --x\n 20     1\nbad line\n"
PS += " 30     1 /opt/lima/limactl hostagent wp-t-e2e-r1-pool\n"
LSOF = (
    "p11\ncpython3.12\nf3\nn127.0.0.1:5000->127.0.0.1:20486\nf4\nn*:20490\n"
    "p12\ncchrome\nn192.0.2.7:51000->198.51.100.9:443\nn[::1]:6000->[::1]:7000\nn192.0.2.21:1->192.0.2.22:x\n"
)


def test_descendants_and_connections() -> None:
    assert egress.descendants(PS, [10]) == {10, 11, 12}
    assert egress.descendants(PS, [99]) == {99}
    assert egress.descendants(" 5 6\n 6 5\n", [5]) == {5, 6}
    assert egress.matching(PS, ["wp-t-e2e-", ""]) == {30}
    assert egress.matching(PS, []) == set()
    conns = egress.connections(LSOF)
    assert [(c["pid"], c["command"], c["host"], c["port"]) for c in conns] == [
        (11, "python3.12", "127.0.0.1", 20486),
        (12, "chrome", "198.51.100.9", 443),
        (12, "chrome", "::1", 7000),
        (12, "chrome", "192.0.2.22", None),
    ]


def test_classification() -> None:
    assert egress.is_loopback("localhost")
    assert egress.is_loopback("::1")
    assert egress.is_loopback("2001:db8::1%lo0") is False
    assert not egress.is_loopback("not-an-ip")
    assert egress.classify({"host": "127.0.0.2", "port": 1, "command": "python"}, set())
    assert egress.classify({"host": "192.0.2.9", "port": 1, "command": "python"}, {"192.0.2.9"})
    assert not egress.classify({"host": "198.51.100.9", "port": 443, "command": "python"}, {"192.0.2.9"})
    # A VM's own time sync and name lookups at the device's resolver through its Lima host agent; its HTTPS or DNS
    # to any other server is egress.
    dns = {"192.0.2.53"}
    assert egress.classify({"host": "198.51.100.9", "port": 123, "command": "limactl"}, set(), dns)
    assert egress.classify({"host": "192.0.2.53", "port": 53, "command": "limactl"}, set(), dns)
    assert not egress.classify({"host": "198.51.100.9", "port": 53, "command": "limactl"}, set(), dns)
    assert not egress.classify({"host": "198.51.100.9", "port": 443, "command": "limactl"}, set(), dns)
    assert not egress.classify({"host": "198.51.100.9", "port": 123, "command": "python"}, set(), dns)
    assert egress.resolvers("# x\nnameserver 192.0.2.53\nnameserver\nsearch lan\n") == {"192.0.2.53"}


def test_read_resolvers(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text("nameserver 192.0.2.1\n")
    assert egress.read_resolvers(conf) == {"192.0.2.1"}
    assert egress.read_resolvers(tmp_path / "missing") == set()


def test_log_sampler_and_violations(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, PS if argv[0] == "ps" else LSOF

    log = egress.EgressLog(tmp_path / "dev" / "egress.jsonl", run, clock=lambda: 1.5)
    assert log.records() == []
    log.peers = {"192.0.2.22"}
    log.write({"source": "relay", "link": "hub", "event": "open"})
    log.sample([10])
    log.sample([10])
    log.sample([])
    records = log.records()
    assert len(records) == 5
    assert records[1]["ts"] == 1.5
    assert calls[1][-3:] == ["10,11,12", "-F", "pcn"]
    assert len(calls) == 4
    bad = egress.violations(records)
    assert [r["remote"] for r in bad] == ["198.51.100.9:443"]
    log.start(lambda: [10], 0.01)
    time.sleep(0.05)
    log.stop()
    log.stop()


def test_sampler_fails_closed(tmp_path: Path) -> None:
    answers = {"ps": (0, PS), "lsof": (2, "")}

    def run(argv: list[str]) -> tuple[int, str]:
        return answers[argv[0]]

    log = egress.EgressLog(tmp_path / "egress.jsonl", run, clock=lambda: 2.0)
    log.match = ["wp-t-e2e-"]
    log.sample([])
    answers["ps"] = (1, "")
    log.sample([10])
    answers["ps"] = (0, " 1 0 launchd\n")
    log.sample([])
    errors = [r["error"] for r in egress.violations(log.records())]
    assert errors == ["lsof exited with 2", "ps exited with 1"]
    assert log.samples == 0

    def broken(argv: list[str]) -> tuple[int, str]:
        raise ValueError("bad output")

    crashing = egress.EgressLog(tmp_path / "crash.jsonl", broken)
    crashing.start(lambda: [10], 0.01)
    time.sleep(0.05)
    crashing.stop()
    assert egress.violations(crashing.records())[0]["error"] == "sampler failed: ValueError"


def test_run_patterns_leave_foreign_golden_builds_out() -> None:
    """Only the run's own detached processes are sampled by name: the Lima host agents of its VMs carry the run
    prefix. A shared golden cache being built by another work package on the same device is not the run's egress."""
    config = RunConfig(
        topology="t1",
        core_src=Path("/c"),
        agent_src=Path("/a"),
        artifacts=Path("/x"),
        port_base=21000,
        vm_prefix="wp-t-",
    )
    patterns = egress_patterns(config)
    assert patterns == [config.run_prefix]
    ps = (
        "10 1 /opt/homebrew/bin/limactl hostagent --pidfile /h/.lima/wp-t-e2e-r1-arp-1/ha.pid wp-t-e2e-r1-arp-1\n"
        "11 1 /opt/homebrew/bin/limactl hostagent --pidfile /h/.lima/cache-golden-0123456789ab/ha.pid"
        " cache-golden-0123456789ab\n"
        "12 1 /opt/homebrew/bin/limactl hostagent cache-golden-build-0123456789ab\n"
    )
    assert egress.matching(ps, patterns) == {10}


def test_work_vm_connections_are_logged_and_allowed(tmp_path: Path) -> None:
    """A work VM is the session's own machine with internet (ADR-059): its host agent's connections are
    logged with `workvm: true`; a session VM's host agent with the same connection stays a violation."""
    ps = (
        " 40 1 /opt/homebrew/bin/limactl hostagent --pidfile /h/.lima/wp-t-e2e-r1-wvm-0a1b2c3d/ha.pid"
        " wp-t-e2e-r1-wvm-0a1b2c3d\n"
        " 41 1 /opt/homebrew/bin/limactl hostagent wp-t-e2e-r1-pool-1\n"
    )
    lsof = (
        "p40\nclimactl\nn192.0.2.7:51000->1.1.1.1:53\nn[::1]:6000->[::1]:7000\n"
        "p41\nclimactl\nn192.0.2.7:51001->198.51.100.9:443\n"
    )
    assert egress.workvm_agents(ps) == {40}

    def run(argv: list[str]) -> tuple[int, str]:
        return (0, ps) if argv[0] == "ps" else (0, lsof)

    log = egress.EgressLog(tmp_path / "egress.jsonl", run, clock=lambda: 3.0)
    log.match = ["wp-t-e2e-"]
    log.sample([])
    records = log.records()
    assert [(r["pid"], r["allowed"], r.get("workvm", False)) for r in records] == [
        (40, True, True),
        (40, True, False),
        (41, False, False),
    ]
    assert [r["remote"] for r in egress.violations(records)] == ["198.51.100.9:443"]
