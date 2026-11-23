"""U-GST-control ``GET /v1/metrics`` (03-api.md section 7.1, 08-monitoring.md section 1, RUN-15).

The VM and cgroup readers run against recorded Linux file formats under ``fixtures/`` (the Linux suite
``guest_linux/test_metrics_cgroup.py`` runs the same readers against a real kernel); ``nft`` is a recording fake
answering with the JSON shape of nftables 1.0.9.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import metrics, sessions
from agent_guest.control import ControlContext
from agent_guest.control_routes import metrics as route
from agent_guest.schema import SchemaSet
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, ControlClient, make_session

FIXTURES = Path(__file__).parent / "fixtures"
NFT_LIST = (FIXTURES / "nft-list.json").read_text(encoding="utf-8")
OTHER_ID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
MISSING_TABLE = "Error: No such file or directory\nlist table inet agent_metrics\n                 ^^^^^^^^^^^^^\n"


def statvfs(_path: str) -> os.statvfs_result:
    return os.statvfs_result((4096, 4096, 1000, 250, 200, 100, 50, 50, 0, 255))


class FakeNft:
    """Answers ``nft`` like nftables 1.0.9; records every call and the scripts applied."""

    def __init__(self, listing: str | None = NFT_LIST, *, apply_code: int = 0, list_code: int = 0) -> None:
        self.listing = listing
        self.apply_code = apply_code
        self.list_code = list_code
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == metrics.NFT_TIMEOUT
        assert kwargs["check"] is False
        self.calls.append(list(argv))
        if argv[1:3] == ["-f", "-"]:
            self.scripts.append(kwargs["input"])
            return subprocess.CompletedProcess(list(argv), self.apply_code, "", "" if self.apply_code == 0 else "err")
        if self.listing is None:
            return subprocess.CompletedProcess(list(argv), 1, "", MISSING_TABLE)
        return subprocess.CompletedProcess(list(argv), self.list_code, self.listing, "boom" if self.list_code else "")


def sources(tmp_path: Path, nft: Any = None, clock: list[float] | None = None) -> metrics.Sources:
    ticks = clock if clock is not None else [100.0]
    return metrics.Sources(
        proc_root=str(FIXTURES / "proc"),
        cgroup_root=str(FIXTURES / "cgroup"),
        root_fs=str(tmp_path),
        statvfs=statvfs,
        clock_ticks=100,
        run=nft if nft is not None else FakeNft(),
        monotonic=lambda: ticks[0],
    )


def copy_proc(tmp_path: Path) -> Path:
    target = tmp_path / "proc"
    shutil.copytree(FIXTURES / "proc", target)
    return target


# -- VM ----------------------------------------------------------------------------------------------------------


def test_vm_values_from_proc_and_statvfs(tmp_path: Path) -> None:
    assert metrics.vm_values(sources(tmp_path)) == {
        "cpu_usage_usec": (4705 + 12 + 3301 + 0 + 97 + 5) * 10_000,
        "mem_used_bytes": (8132508 - 7516304) * 1024,
        "mem_total_bytes": 8132508 * 1024,
        "disk_used_bytes": 750 * 4096,
        "disk_total_bytes": 1000 * 4096,
        "net_rx_bytes": 52428800 + 2048,
        "net_tx_bytes": 1048576 + 1024,
        "uptime_seconds": 1234,
        "load1": 0.42,
    }


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("stat", "intr 1 2\n"),
        ("stat", "cpu  1 2 three 4 5 6 7 8\n"),
        ("meminfo", "MemTotal: 10 kB\n"),
        ("meminfo", "MemTotal: 10 MB\nMemAvailable: 5 kB\n"),
        ("net/dev", "h1\nh2\n"),
        ("net/dev", "h1\nh2\n  eth0 1 2 3\n"),
        ("net/dev", "h1\nh2\n  eth0: 1 2 3\n"),
        ("net/dev", "h1\nh2\n  eth0: 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 x\n"),
        ("uptime", ""),
        ("uptime", "soon\n"),
        ("loadavg", ""),
        ("loadavg", "high 1 1\n"),
        ("loadavg", "-1.0 0 0\n"),
        ("loadavg", "nan 0 0\n"),
        ("loadavg", "inf 0 0\n"),
    ],
)
def test_malformed_vm_sources_are_unavailable(tmp_path: Path, name: str, content: str) -> None:
    proc = copy_proc(tmp_path)
    (proc / name).write_text(content, encoding="ascii")
    with pytest.raises(metrics.MetricsUnavailable):
        metrics.vm_values(metrics.Sources(proc_root=str(proc), statvfs=statvfs, clock_ticks=100))


def test_missing_or_unreadable_vm_sources_are_unavailable(tmp_path: Path) -> None:
    proc = copy_proc(tmp_path)
    (proc / "uptime").unlink()
    with pytest.raises(metrics.MetricsUnavailable, match="uptime"):
        metrics.vm_values(metrics.Sources(proc_root=str(proc), statvfs=statvfs, clock_ticks=100))
    (proc / "uptime").write_bytes(b"\xff\xfe\n")
    with pytest.raises(metrics.MetricsUnavailable, match="uptime"):
        metrics.vm_values(metrics.Sources(proc_root=str(proc), statvfs=statvfs, clock_ticks=100))

    def failing(_path: str) -> os.statvfs_result:
        raise OSError("gone")

    with pytest.raises(metrics.MetricsUnavailable, match="statvfs"):
        metrics.vm_values(metrics.Sources(proc_root=str(FIXTURES / "proc"), statvfs=failing, clock_ticks=100))


def test_counts_are_clamped_to_the_contract_range() -> None:
    assert metrics._count(-5) == 0
    assert metrics._count(2**60) == metrics.MAX_COUNT
    assert metrics.parse_meminfo("MemTotal: 10 kB\nMemAvailable: 20 kB\n") == (0, 10240)


# -- cgroup -------------------------------------------------------------------------------------------------------


def test_cgroup_values_of_a_running_unit() -> None:
    directory = metrics.unit_cgroup(str(FIXTURES / "cgroup"), SESSION_ID)
    assert directory.endswith(f"/system.slice/agent-session-{SESSION_ID}.service")
    assert metrics.cgroup_values(directory) == {
        "cpu_usage_usec": 2500000,
        "mem_current_bytes": 734003200,
        "mem_max_bytes": 4294967296,
        "tasks_current": 17,
        "io_read_bytes": 1048576 + 4096,
        "io_write_bytes": 4194304,
        "oom_kills": 1,
    }


def test_cgroup_values_without_a_unit_are_zero(tmp_path: Path) -> None:
    assert metrics.cgroup_values(str(tmp_path / "absent")) == {
        "cpu_usage_usec": 0,
        "mem_current_bytes": 0,
        "mem_max_bytes": None,
        "tasks_current": 0,
        "io_read_bytes": 0,
        "io_write_bytes": 0,
        "oom_kills": 0,
    }


def test_malformed_cgroup_files_count_as_zero_and_max_is_no_limit(tmp_path: Path) -> None:
    (tmp_path / "cpu.stat").write_text("usage_usec many\nuser_usec\n", encoding="ascii")
    (tmp_path / "memory.current").write_text("lots\n", encoding="ascii")
    (tmp_path / "memory.max").write_text("max\n", encoding="ascii")
    (tmp_path / "memory.events").write_text("oom_kill -1\n", encoding="ascii")
    (tmp_path / "io.stat").write_text("8:0 rbytes=x wbytes=12 dbytes=3 junk\n8:16\n", encoding="ascii")
    values = metrics.cgroup_values(str(tmp_path))
    assert values["cpu_usage_usec"] == 0
    assert values["mem_current_bytes"] == 0
    assert values["mem_max_bytes"] is None
    assert values["oom_kills"] == 0
    assert (values["io_read_bytes"], values["io_write_bytes"]) == (0, 12)


# -- journal counters ---------------------------------------------------------------------------------------------


def hook(decision: str) -> dict[str, Any]:
    return {"event": "PreToolUse", "tool": "Bash", "decision": decision}


def usage(**counts: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": "req-1",
        "model": "claude-sonnet-4-5",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "thinking_tokens": 0,
        "context_window_tokens": 200000,
        "request_bytes": 0,
        "response_bytes": 0,
        "status": 200,
    }
    body.update(counts)
    return body


def test_journal_counters_count_each_record_once(context: ControlContext) -> None:
    context.state.save_session(make_session())
    journal = context.journal
    for decision in ("deny", "allow", "block", "none"):
        journal.append("session.hook", hook(decision), SESSION_ID)
    journal.append("guest.error", {"code": "hook_handler_failed"}, SESSION_ID)
    journal.append("guest.error", {"code": "subagent_hook_seen"}, SESSION_ID)
    journal.append("guest.error", {"code": "hook_handler_failed"})  # VM level: no session
    journal.append(
        "usage",
        usage(
            input_tokens=100,
            cache_read_tokens=20,
            cache_write_tokens=3,
            output_tokens=40,
            thinking_tokens=2,
            request_bytes=900,
            response_bytes=300,
        ),
        SESSION_ID,
    )
    journal.append("session.state", {"state": "idle", "at": "2026-11-21T10:00:00.000Z"}, SESSION_ID)
    counters = metrics.JournalCounters()
    counters.update(context)
    expected = {
        "hook_denies": 2,
        "hook_errors": 1,
        "model_input_tokens": 123,
        "model_output_tokens": 42,
        "model_request_bytes": 900,
        "model_response_bytes": 300,
    }
    assert counters.of(SESSION_ID) == expected
    counters.update(context)
    assert counters.of(SESSION_ID) == expected
    journal.append("session.hook", hook("deny"), SESSION_ID)
    counters.update(context)
    assert counters.of(SESSION_ID)["hook_denies"] == 3
    assert counters.of(OTHER_ID) == dict.fromkeys(metrics.COUNTERS, 0)


def test_journal_counters_ignore_values_that_are_no_counts(context: ControlContext) -> None:
    with context.journal.transaction() as db:
        db.execute(
            "INSERT INTO journal (ts, session_id, kind, body) VALUES (?, ?, ?, ?)",
            ("2026-11-21T10:00:00.000Z", SESSION_ID, "usage", json.dumps([1, 2])),
        )
        db.execute(
            "INSERT INTO journal (ts, session_id, kind, body) VALUES (?, ?, ?, ?)",
            ("2026-11-21T10:00:00.000Z", SESSION_ID, "usage", json.dumps({"input_tokens": True, "output_tokens": -4})),
        )
    counters = metrics.JournalCounters()
    counters.update(context)
    assert counters.of(SESSION_ID) == dict.fromkeys(metrics.COUNTERS, 0)
    assert counters.last_seq == 2


# -- egress counters ----------------------------------------------------------------------------------------------


def test_egress_creates_the_table_and_chains_when_missing() -> None:
    nft = FakeNft(listing=None)
    assert metrics.EgressCounters(nft).sync({"01a1ee56-5e00-7003-8f1b-340691d99f13": 20003, SESSION_ID: 20001}) == {
        SESSION_ID: 0,
        "01a1ee56-5e00-7003-8f1b-340691d99f13": 0,
    }
    assert nft.calls[0] == ["nft", "-j", "-a", "list", "table", "inet", "agent_metrics"]
    script = nft.scripts[0].splitlines()
    assert script[:2] == metrics.base_script()
    assert script[2:] == metrics.add_script(SESSION_ID, 20001) + metrics.add_script(
        "01a1ee56-5e00-7003-8f1b-340691d99f13", 20003
    )
    assert "add rule inet agent_metrics s01a1ee565e0070038f1b340691d99f03 counter" in script
    assert "add rule inet agent_metrics out meta skuid 20001 jump s01a1ee565e0070038f1b340691d99f03" in script
    assert not any(" drop" in line or " accept" in line.split("{")[0] for line in script)


def test_egress_reads_counters_adds_new_and_removes_ended_users() -> None:
    nft = FakeNft()
    # OTHER_ID ended; a new session reuses its UID 20007 and starts at 0 (the chain belongs to the session).
    assert metrics.EgressCounters(nft).sync({SESSION_ID: 20000, "01a1ee56-5e00-7003-8f1b-340691d99f15": 20007}) == {
        SESSION_ID: 9,
        "01a1ee56-5e00-7003-8f1b-340691d99f15": 0,
    }
    assert nft.scripts == [
        "\n".join(
            [
                *metrics.add_script("01a1ee56-5e00-7003-8f1b-340691d99f15", 20007),
                "delete rule inet agent_metrics out handle 10",
                "flush chain inet agent_metrics s01a1ee565e0070038f1b340691d99f04",
                "delete chain inet agent_metrics s01a1ee565e0070038f1b340691d99f04",
            ]
        )
        + "\n"
    ]


def test_egress_without_changes_applies_nothing() -> None:
    nft = FakeNft()
    assert metrics.EgressCounters(nft).sync({SESSION_ID: 20000, OTHER_ID: 20007}) == {SESSION_ID: 9, OTHER_ID: 2}
    assert nft.scripts == []
    assert len(nft.calls) == 1


@pytest.mark.parametrize(
    "nft",
    [
        FakeNft(list_code=1),
        FakeNft(listing="not json"),
        FakeNft(listing='{"other": []}'),
        FakeNft(listing="[1, 2]"),
        FakeNft(listing=None, apply_code=1),
    ],
    ids=["list-refused", "not-json", "no-nftables-key", "not-an-object", "apply-refused"],
)
def test_egress_failures_leave_the_counts_out(nft: FakeNft) -> None:
    assert metrics.EgressCounters(nft).sync({SESSION_ID: 20000}) is None


def test_egress_without_nft_binary_is_none() -> None:
    def missing(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("nft")

    def hung(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), kwargs["timeout"])

    assert metrics.EgressCounters(missing).sync({SESSION_ID: 20000}) is None
    assert metrics.EgressCounters(hung).sync({SESSION_ID: 20000}) is None


def test_parse_ruleset_skips_foreign_entries() -> None:
    document = {
        "nftables": [
            "text",
            {"chain": {"name": "out"}},
            {"chain": {"name": 7}},
            {"rule": {"chain": "out", "expr": [{"jump": {"target": "u1"}}]}},  # no handle
            {"rule": {"chain": "out", "handle": 4, "expr": ["x", {"jump": {"target": "elsewhere"}}, {"jump": 3}]}},
            {
                "rule": {
                    "chain": "s01a1ee565e0070038f1b340691d99f03",
                    "handle": 5,
                    "expr": [{"counter": {"packets": "9"}}, {"counter": 1}],
                }
            },
            {"rule": {"chain": 9, "handle": 6, "expr": [{"counter": {"packets": 3}}]}},
            {"rule": {"chain": "s01a1ee565e0070038f1b340691d99f0x", "expr": "none"}},
        ]
    }
    parsed = metrics.parse_ruleset(json.dumps(document))
    assert parsed.exists
    assert parsed.chains == set()
    assert parsed.jumps == {}
    assert parsed.packets == {}


# -- collector and route ------------------------------------------------------------------------------------------


def own_session(
    tmp_path: Path, session_id: str = SESSION_ID, state: str = "working", uid: int | None = None
) -> Session:
    base = tmp_path / "sessions" / session_id
    (base / "work" / "src").mkdir(parents=True)
    (base / "work" / "a.txt").write_bytes(b"x" * 100)
    (base / "work" / "src" / "b.py").write_bytes(b"y" * 23)
    (base / "work" / ".git").mkdir()
    (base / "work" / ".git" / "HEAD").write_bytes(b"z" * 1000)
    (base / "work" / "link").symlink_to(base / "work" / "a.txt")
    created = make_session(session_id, token=f"token-{session_id}", state=state)
    return replace(created, uid=os.getuid() if uid is None else uid, workspace=str(base / "work"))


def test_route_answers_the_contract_shape(
    context: ControlContext, control_client: ControlClient, schemas: SchemaSet, tmp_path: Path
) -> None:
    nft = FakeNft(listing=None)
    clock = [100.0]
    metrics.install(context, metrics.Collector(sources(tmp_path, nft, clock)))
    working = own_session(tmp_path)
    context.state.save_session(working)
    context.state.save_session(own_session(tmp_path, OTHER_ID, state="sandbox_ready", uid=-1))
    stopped = make_session("01a1ee56-5e00-7003-8f1b-340691d99f05", token="t-stopped", state="stopped")
    context.state.save_session(stopped)
    context.journal.append("session.hook", hook("deny"), SESSION_ID)

    reply = control_client.request("GET", "/v1/metrics")
    assert reply.status == 200
    body = reply.json()
    assert schemas.errors("guest/metrics.response.json", body) == []
    assert body["vm"]["uptime_seconds"] == 1234
    first, second = body["sessions"]
    assert first == {
        "session_id": SESSION_ID,
        "cpu_usage_usec": 2500000,
        "mem_current_bytes": 734003200,
        "mem_max_bytes": 4294967296,
        "tasks_current": 17,
        "io_read_bytes": 1052672,
        "io_write_bytes": 4194304,
        "oom_kills": 1,
        "workspace_bytes": 123,
        "state": "working",
        "hook_denies": 1,
        "hook_errors": 0,
        "model_input_tokens": 0,
        "model_output_tokens": 0,
        "model_request_bytes": 0,
        "model_response_bytes": 0,
        "egress_denied": 0,
    }
    # No unit, no user yet: zeros, no workspace walk, no egress chain.
    assert second["session_id"] == OTHER_ID
    assert (second["cpu_usage_usec"], second["mem_max_bytes"], second["workspace_bytes"]) == (0, None, 0)
    assert "egress_denied" not in second
    assert nft.scripts[0].splitlines()[2:] == metrics.add_script(SESSION_ID, os.getuid())

    # The walk is cached for WORKSPACE_TTL seconds; ended sessions drop out of the cache.
    (Path(working.workspace) / "c.bin").write_bytes(b"c" * 1000)
    assert control_client.request("GET", "/v1/metrics").json()["sessions"][0]["workspace_bytes"] == 123
    clock[0] += metrics.WORKSPACE_TTL
    assert control_client.request("GET", "/v1/metrics").json()["sessions"][0]["workspace_bytes"] == 1123
    context.state.save_session(replace(working, state="stopped"))
    assert [item["session_id"] for item in control_client.request("GET", "/v1/metrics").json()["sessions"]] == [
        OTHER_ID
    ]
    assert SESSION_ID not in metrics.collector(context).workspace


def test_route_without_nft_leaves_egress_out(context: ControlContext, tmp_path: Path) -> None:
    collector = metrics.install(context, metrics.Collector(sources(tmp_path, FakeNft(list_code=1))))
    context.state.save_session(own_session(tmp_path))
    listed = collector.collect(context)["sessions"]
    assert "egress_denied" not in listed[0]


def test_at_most_64_sessions_sorted_by_id(context: ControlContext, tmp_path: Path) -> None:
    collector = metrics.install(context, metrics.Collector(sources(tmp_path)))
    ids = [f"01a1ee56-5e00-7003-8f1b-3406910{index:05x}" for index in range(70)]
    for index in reversed(range(70)):
        context.state.save_session(replace(make_session(ids[index], token=f"token-{index}", state="idle"), uid=-1))
    listed = collector.collect(context)["sessions"]
    assert [item["session_id"] for item in listed] == ids[:64]


def test_unreadable_vm_sources_answer_unavailable(
    context: ControlContext, control_client: ControlClient, tmp_path: Path
) -> None:
    broken = sources(tmp_path)
    broken.proc_root = str(tmp_path / "no-proc")
    metrics.install(context, metrics.Collector(broken))
    reply = control_client.request("GET", "/v1/metrics")
    assert (reply.status, reply.json()["error"]) == (503, "unavailable")


def test_metrics_request_takes_no_body(control_client: ControlClient) -> None:
    reply = control_client.request("GET", "/v1/metrics?extra=1")
    assert reply.status == 400


def test_sandbox_ready_adds_the_egress_chain(context: ControlContext, tmp_path: Path) -> None:
    nft = FakeNft(listing=None)
    metrics.install(context, metrics.Collector(sources(tmp_path, nft)))
    context.state.save_session(own_session(tmp_path, uid=20009))
    assert route.sandbox_ready in sessions.session_events()["sandbox_ready"]
    route.sandbox_ready(context, SESSION_ID)
    thread = metrics.collector(context).egress_thread
    assert thread is not None
    thread.join(10)
    assert nft.scripts[0].splitlines()[2:] == metrics.add_script(SESSION_ID, 20009)


def test_collector_is_created_once_per_context(context: ControlContext) -> None:
    created = metrics.collector(context)
    assert metrics.collector(context) is created
    assert created.sources.proc_root == metrics.PROC_ROOT
    assert created.sources.clock_ticks == os.sysconf("SC_CLK_TCK")


def test_io_accounting_is_turned_on_once_for_units_without_io_stat(context: ControlContext, tmp_path: Path) -> None:
    root = tmp_path / "cgroup"
    unit_dir = Path(metrics.unit_cgroup(str(root), SESSION_ID))
    unit_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[0] == "systemctl" and len(calls) > 1:
            raise FileNotFoundError("systemctl")
        return subprocess.CompletedProcess(list(argv), 1, "", "No such file or directory")

    use = sources(tmp_path, run)
    use.cgroup_root = str(root)
    collector = metrics.install(context, metrics.Collector(use))
    context.state.save_session(replace(make_session(), uid=-1))
    collector.collect(context)
    collector.collect(context)
    systemctl = [call for call in calls if call[0] == "systemctl"]
    assert systemctl == [metrics.io_accounting_argv(SESSION_ID)]
    assert systemctl[0][-2:] == [f"agent-session-{SESSION_ID}.service", "IOAccounting=yes"]
    # A unit that has io.stat needs nothing; a failing systemctl is ignored; ended sessions are forgotten.
    other = make_session(OTHER_ID, token="t-other", state="idle")
    other_dir = Path(metrics.unit_cgroup(str(root), OTHER_ID))
    other_dir.mkdir(parents=True)
    context.state.save_session(replace(other, uid=-1))
    collector.collect(context)
    assert collector.io_enabled == {SESSION_ID, OTHER_ID}
    (other_dir / "io.stat").write_text("", encoding="ascii")
    context.state.save_session(replace(make_session(), uid=-1, state="stopped"))
    collector.collect(context)
    assert collector.io_enabled == {OTHER_ID}
    collector.io_enabled.clear()
    collector.collect(context)
    assert collector.io_enabled == set()


def test_counts_of_ended_sessions_are_forgotten(context: ControlContext, tmp_path: Path) -> None:
    collector = metrics.install(context, metrics.Collector(sources(tmp_path)))
    context.state.save_session(replace(make_session(), uid=-1))
    context.journal.append("session.hook", hook("deny"), SESSION_ID)
    collector.collect(context)
    assert SESSION_ID in collector.journal.counts
    context.state.save_session(replace(make_session(), uid=-1, state="stopped"))
    collector.collect(context)
    assert collector.journal.counts == {}
