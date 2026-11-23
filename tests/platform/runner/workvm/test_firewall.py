"""U-RUN-workvm firewall: pf anchor `agent.workvm` render, watcher, check and install (13-work-vm.md 6.2, 6.3).

`pfctl`, `launchctl` and file writes are injected; the live answers imitate pfctl's canonical rule output. The
watcher script is run for real by `/bin/sh` with stub commands in place of `warp-cli`, `route`, `pfctl` and `dnctl`.
"""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from agent_runner.errors import RunnerError
from agent_runner.workvm import firewall
from agent_runner.workvm.firewall import InstallStep, Slot

SLOTS = [Slot(1, "agentwvm01", 602, 47201), Slot(0, "agentwvm00", 601, 47200)]
EGRESS = "utun4"


def rendered(egress: str | None = EGRESS) -> str:
    return firewall.render(SLOTS, egress, 500)


def live_rules(uid: int, port: int, egress: str | None = EGRESS) -> list[str]:
    rules = [
        f"pass out quick on lo0 inet proto tcp from 127.0.0.1 to 127.0.0.1 port = {port} user = {uid} flags S/SA"
        " keep state",
        f"pass out quick on lo0 inet proto tcp from 127.0.0.1 port = {port} to 127.0.0.1 user = {uid} no state",
        f"block return out quick on lo0 proto tcp all user = {uid}",
        f"block return out quick on lo0 proto udp all user = {uid}",
        f"block return out quick proto tcp from any to <workvm_blocked> user = {uid}",
        f"block return out quick proto udp from any to <workvm_blocked> user = {uid}",
        f"block return out quick proto tcp from any to any port = smtp user = {uid}",
    ]
    if egress:
        rules += [
            f"pass out quick on {egress} proto tcp all user = {uid} flags S/SA keep state (if-bound, max 4096,"
            " source-track rule, max-src-conn 2048, max-src-conn-rate 2000/10)",
            f"pass out quick on {egress} proto udp all user = {uid} keep state (if-bound, max 4096, source-track rule,"
            " max-src-conn 2048, max-src-conn-rate 2000/10)",
        ]
    return [*rules, f"block return out quick all user = {uid}"]


def live_icmp(egress: str | None = EGRESS) -> list[str]:
    passed = (
        [
            f"pass out quick on {egress} inet proto icmp all icmp-type echoreq keep state (if-bound)",
            f"pass out quick on {egress} inet6 proto ipv6-icmp all icmp6-type echoreq keep state (if-bound)",
        ]
        if egress
        else []
    )
    return [
        *passed,
        "block drop out quick on ! lo0 inet proto icmp all icmp-type echoreq",
        "block drop out quick on ! lo0 inet6 proto ipv6-icmp all icmp6-type echoreq",
    ]


def live_anchor(egress: str | None = EGRESS) -> str:
    rules = [*live_rules(601, 47200, egress), *live_rules(602, 47201, egress), *live_icmp(egress)]
    return "\n".join(rules) + "\n"


LIVE_ANCHOR = live_anchor()
LIVE_ENTRIES = [*firewall.BLOCKED_V4, *firewall.BLOCKED_V6]
LIVE_TABLE = "".join(f"   {e}\n" for e in LIVE_ENTRIES)
LIVE_MAIN = 'scrub-anchor "com.apple/*" all fragment reassemble\nanchor "com.apple/*" all\nanchor "agent.workvm" all\n'
LIVE_INFO = "Status: Enabled for 0 days 01:02:03           Debug: Urgent\n"
LIVE_IFACES = "lo0\nen0\n"

Answers = dict[tuple[str, ...], tuple[int, str]]


def healthy() -> Answers:
    return {
        ("/sbin/pfctl", "-s", "info"): (0, LIVE_INFO),
        ("/sbin/pfctl", "-a", "agent.workvm", "-sr"): (0, LIVE_ANCHOR),
        ("/sbin/pfctl", "-sr"): (0, LIVE_MAIN),
        ("/sbin/pfctl", "-s", "Interfaces", "-v"): (0, LIVE_IFACES),
        ("/sbin/pfctl", "-a", "agent.workvm", "-t", "workvm_blocked", "-T", "show"): (0, LIVE_TABLE),
    }


class FakeRun:
    def __init__(self, answers: Mapping[tuple[str, ...], tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append((key, timeout))
        code, out = self.answers.get(key, (0, ""))
        return subprocess.CompletedProcess(list(argv), code, out, "")


def test_render_follows_the_spec_rule_order_per_slot() -> None:
    text = rendered()
    assert text == rendered()
    lines = text.splitlines()
    assert lines[2] == "table <workvm_blocked> persist { \\"
    assert "internet only on utun4" in lines[1]
    assert "500 Mbit/s" in lines[1]
    slot0 = lines.index("# slot 00 agentwvm00")
    assert lines.index("# slot 01 agentwvm01") > slot0
    assert lines[slot0 + 1 : slot0 + 9] == [
        "pass out quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port 47200 user 601 flags S/SA keep state",
        "pass out quick on lo0 proto tcp from 127.0.0.1 port 47200 to 127.0.0.1 user 601 no state",
        "block return out quick on lo0 proto { tcp udp } user 601",
        "block return out quick proto { tcp udp } from any to <workvm_blocked> user 601",
        "block return out quick proto tcp from any to any port 25 user 601",
        "pass out quick on utun4 proto { tcp udp } from any to any user 601 keep state"
        " (if-bound, max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)",
        "block return out quick user 601",
        "# slot 01 agentwvm01",
    ]
    assert text.endswith(
        "block return out quick user 602\n"
        "# ICMP echo of the device: only through the tunnel (pf `user` does not match ICMP)\n"
        "pass out quick on utun4 inet proto icmp all icmp-type echoreq keep state (if-bound)\n"
        "pass out quick on utun4 inet6 proto icmp6 all icmp6-type echoreq keep state (if-bound)\n"
        "block drop out quick on ! lo0 inet proto icmp all icmp-type echoreq\n"
        "block drop out quick on ! lo0 inet6 proto icmp6 all icmp6-type echoreq\n"
    )


def test_render_fail_closed_has_no_internet_rule() -> None:
    text = rendered(None)
    assert "fail-closed, no internet" in text
    assert "pass out quick on utun" not in text
    assert [line for line in text.splitlines() if line.startswith("pass")] == [
        "pass out quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port 47200 user 601 flags S/SA keep state",
        "pass out quick on lo0 proto tcp from 127.0.0.1 port 47200 to 127.0.0.1 user 601 no state",
        "pass out quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port 47201 user 602 flags S/SA keep state",
        "pass out quick on lo0 proto tcp from 127.0.0.1 port 47201 to 127.0.0.1 user 602 no state",
    ]
    assert text.endswith("block drop out quick on ! lo0 inet6 proto icmp6 all icmp6-type echoreq\n")


def test_render_up_template_is_the_rendered_text_with_the_placeholder() -> None:
    template = firewall.render_up_template(SLOTS, 500)
    assert template.replace("@EGRESS@", EGRESS) == rendered()
    assert template.count("@EGRESS@") == 5


def test_resolvers_are_two_public_addresses() -> None:
    import ipaddress  # noqa: PLC0415

    found = [ipaddress.IPv4Address(a) for a in firewall.resolvers()]
    assert len(found) == 2
    assert all(a.is_global for a in found)


def test_blocked_ranges_are_the_special_purpose_ranges() -> None:
    for cidr in (
        firewall._v4(10, 0, 0, 0, 8),
        firewall._v4(100, 64, 0, 0, 10),
        firewall._v4(127, 0, 0, 0, 8),
        firewall._v4(169, 254, 0, 0, 16),
        firewall._v4(198, 18, 0, 0, 15),
        firewall._v4(240, 0, 0, 0, 4),
    ):
        assert cidr in firewall.BLOCKED_V4
    for cidr in ("::1/128", "fc" + "00::/7", "fe" + "80::/10", "ff00::/8", "64:ff9b::/96", "::ffff:0:0/96"):
        assert cidr in firewall.BLOCKED_V6


def test_render_without_slots_holds_only_the_table() -> None:
    text = firewall.render([], None, 1)
    assert "user" not in text
    assert text.rstrip().endswith("}")


@pytest.mark.parametrize(
    ("slots", "field"),
    [
        ([Slot(i, f"a{i}", 601 + i, 47200 + i) for i in range(101)], "slots"),
        ([Slot(100, "a", 601, 47200)], "index"),
        ([Slot(-1, "a", 601, 47200)], "index"),
        ([Slot(0, "a", 601, 47200), Slot(0, "b", 602, 47201)], "index"),
        ([Slot(0, "A", 601, 47200)], "account"),
        ([Slot(0, "a" * 33, 601, 47200)], "account"),
        ([Slot(0, "a", 500, 47200)], "uid"),
        ([Slot(0, "a", 2**31 + 1, 47200)], "uid"),
        ([Slot(0, "a", 601, 47200), Slot(1, "b", 601, 47201)], "uid"),
        ([Slot(0, "a", 601, 1023)], "ssh_port"),
        ([Slot(0, "a", 601, 65536)], "ssh_port"),
        ([Slot(0, "a", 601, 47200), Slot(1, "b", 602, 47200)], "ssh_port"),
    ],
)
def test_render_refuses_invalid_slots(slots: list[Slot], field: str) -> None:
    with pytest.raises(RunnerError) as caught:
        firewall.render(slots, EGRESS, 500)
    assert (caught.value.code, caught.value.name) == ("workvm_firewall_invalid", field)


@pytest.mark.parametrize(
    ("egress", "mbit", "field"),
    [
        ("en0", 500, "egress_interface"),
        ("utun", 500, "egress_interface"),
        ("utun1234", 500, "egress_interface"),
        ("utun4;", 500, "egress_interface"),
        ("", 500, "egress_interface"),
        (EGRESS, 0, "bandwidth_mbit"),
        (None, 100_001, "bandwidth_mbit"),
    ],
)
def test_render_refuses_invalid_network_input(egress: str | None, mbit: int, field: str) -> None:
    with pytest.raises(RunnerError) as caught:
        firewall.render(SLOTS, egress, mbit)
    assert (caught.value.code, caught.value.name) == ("workvm_firewall_invalid", field)


def test_render_dummynet_two_pipes_per_slot() -> None:
    assert firewall.render_dummynet(SLOTS, 500) == (
        "pipe 4700 config bw 500Mbit/s\npipe 4701 config bw 500Mbit/s\n"
        "pipe 4702 config bw 500Mbit/s\npipe 4703 config bw 500Mbit/s\n"
    )
    assert firewall.render_dummynet([], 500) == ""
    with pytest.raises(RunnerError, match="bandwidth_mbit"):
        firewall.render_dummynet(SLOTS, 0)
    with pytest.raises(RunnerError, match="uid"):
        firewall.render_dummynet([Slot(0, "a", 1, 47200)], 500)


def test_egress_of_reads_the_internet_pass_rule() -> None:
    assert firewall.egress_of(LIVE_ANCHOR) == EGRESS
    assert firewall.egress_of(live_anchor(None)) is None
    assert firewall.egress_of("pass out quick on en0 proto tcp all user = 601\n") is None
    assert firewall.egress_of("") is None


def test_check_passes_on_the_healthy_filter() -> None:
    run = FakeRun(healthy())
    status = firewall.check(run, SLOTS, 500)
    assert status == firewall.Status((), EGRESS)
    assert status.internet
    assert [call for call, _ in run.calls] == list(firewall.READ_COMMANDS)
    assert all(timeout == firewall.PFCTL_TIMEOUT for _, timeout in run.calls)


def test_check_passes_fail_closed_without_internet() -> None:
    answers = healthy()
    answers[firewall.READ_COMMANDS[1]] = (0, live_anchor(None))
    status = firewall.check(FakeRun(answers), SLOTS, 500)
    assert status == firewall.Status((), None)
    assert not status.internet


def test_check_with_problems_reports_no_internet() -> None:
    answers = healthy()
    answers[firewall.READ_COMMANDS[0]] = (0, "Status: Disabled\n")
    status = firewall.check(FakeRun(answers), SLOTS, 500)
    assert status == firewall.Status(("pf_disabled",), EGRESS)
    assert not status.internet


def test_check_stops_when_pfctl_cannot_read() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-s", "info")] = (1, "")
    run = FakeRun(answers)
    assert firewall.check(run, SLOTS, 500) == firewall.Status(("pfctl_failed",), None)
    assert len(run.calls) == 1


def test_check_reports_every_problem() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-s", "info")] = (0, "Status: Disabled\n")
    answers[("/sbin/pfctl", "-a", "agent.workvm", "-sr")] = (0, "")
    answers[("/sbin/pfctl", "-sr")] = (0, 'pass out quick all\nanchor "agent.workvm" all\n')
    answers[("/sbin/pfctl", "-s", "Interfaces", "-v")] = (0, "lo0 (skip)\nen0\n")
    table = ("/sbin/pfctl", "-a", "agent.workvm", "-t", "workvm_blocked", "-T", "show")
    answers[table] = (0, f"   {firewall.BLOCKED_V4[1]}\n")
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == (
        "pf_disabled",
        "anchor_missing",
        "not_referenced",
        "skip_lo0",
        "table_stale",
    )


def test_check_fails_closed_on_failing_commands() -> None:
    answers = {key: (1, out) for key, (_, out) in healthy().items()}
    answers[("/sbin/pfctl", "-s", "info")] = (0, LIVE_INFO)
    assert firewall.check(FakeRun(answers), SLOTS, 500) == firewall.Status(
        ("anchor_missing", "not_referenced", "skip_lo0", "table_stale"), None
    )


@pytest.mark.parametrize(
    "live",
    [
        LIVE_ANCHOR.replace("port = smtp", "port = 26", 1),
        LIVE_ANCHOR.replace("user = 602", "user = 603", 1),
        LIVE_ANCHOR.replace("on utun4 proto udp", "on utun5 proto udp", 1),
        LIVE_ANCHOR.replace("pass out quick on utun4 proto tcp", "pass out quick on en0 proto tcp", 1),
        "\n".join(live_rules(601, 47200)) + "\n",
        "\n".join(live_rules(602, 47201) + live_rules(601, 47200)) + "\n",
        LIVE_ANCHOR.replace("block return out quick all user = 602", "block drop out quick all user = 602"),
        "\n".join(live_rules(601, 47200) + live_rules(602, 47201, None)) + "\n",
    ],
)
def test_check_detects_an_anchor_mismatch(live: str) -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", "agent.workvm", "-sr")] = (0, live)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("anchor_mismatch",)


def test_check_without_slots_accepts_an_empty_anchor() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", "agent.workvm", "-sr")] = (0, "")
    assert firewall.check(FakeRun(answers), [], 500) == firewall.Status((), None)
    answers[("/sbin/pfctl", "-a", "agent.workvm", "-sr")] = (0, LIVE_ANCHOR)
    assert firewall.check(FakeRun(answers), [], 500).problems == ("anchor_mismatch",)


@pytest.mark.parametrize(
    "main",
    ["", 'anchor "com.apple/*" all\n', 'pass quick on en0 all\nanchor "agent.workvm" all\n'],
)
def test_check_requires_the_reference_before_any_pass_quick(main: str) -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-sr")] = (0, main)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("not_referenced",)


def test_check_accepts_a_pass_quick_after_the_reference() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-sr")] = (0, LIVE_MAIN + "pass out quick on en0 all\npass in all\n")
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()


def test_check_table_ignores_noise_and_needs_every_range() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", "agent.workvm", "-t", "workvm_blocked", "-T", "show")] = (
        0,
        "No ALTQ support in kernel\n" + LIVE_TABLE + "   198.51.100.7\n",
    )
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()
    answers[("/sbin/pfctl", "-a", "agent.workvm", "-t", "workvm_blocked", "-T", "show")] = (
        0,
        LIVE_TABLE.replace(f"   {firewall.BLOCKED_V6[-1]}\n", ""),
    )
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("table_stale",)


def test_sudo_run_prefixes_sudo_non_interactive() -> None:
    run = FakeRun({})
    firewall.sudo_run(run)(["/sbin/pfctl", "-s", "info"], 3.0)
    assert run.calls == [(("/usr/bin/sudo", "-n", "/sbin/pfctl", "-s", "info"), 3.0)]


def test_patched_pf_conf_inserts_before_the_first_rule_and_is_idempotent() -> None:
    current = 'scrub-anchor "com.apple/*"\nanchor "com.apple/*"\n\npass in all\nblock out all\n'
    patched = firewall.patched_pf_conf(current)
    assert patched.splitlines() == [
        'scrub-anchor "com.apple/*"',
        'anchor "com.apple/*"',
        "",
        'anchor "agent.workvm"',
        "pass in all",
        "block out all",
        'load anchor "agent.workvm" from "/etc/pf.anchors/agent.workvm"',
    ]
    assert firewall.patched_pf_conf(patched) == patched


def test_patched_pf_conf_appends_without_rules() -> None:
    assert firewall.patched_pf_conf("") == (
        'anchor "agent.workvm"\nload anchor "agent.workvm" from "/etc/pf.anchors/agent.workvm"\n'
    )
    assert firewall.patched_pf_conf('anchor "agent.workvm"') == (
        'anchor "agent.workvm"\nload anchor "agent.workvm" from "/etc/pf.anchors/agent.workvm"\n'
    )


def test_daemon_plist_keeps_the_watcher_alive() -> None:
    data = plistlib.loads(firewall.daemon_plist().encode())
    assert data == {
        "Label": "swiss.dfx.agent-runner.pf",
        "ProgramArguments": ["/etc/pf.anchors/agent.workvm.watch"],
        "RunAtLoad": True,
        "KeepAlive": True,
    }


def test_install_plan_checks_before_loading() -> None:
    plan = firewall.install_plan(SLOTS, 500)
    assert plan[0] == InstallStep("write", "/etc/pf.anchors/agent.workvm", rendered(None))
    assert plan[1].argv == ("/sbin/pfctl", "-a", "agent.workvm", "-n", "-f", "/etc/pf.anchors/agent.workvm")
    assert plan[2] == InstallStep("write", "/etc/pf.anchors/agent.workvm.sample", firewall.render(SLOTS, "utun0", 500))
    assert plan[3].argv == ("/sbin/pfctl", "-a", "agent.workvm", "-n", "-f", "/etc/pf.anchors/agent.workvm.sample")
    assert plan[4] == InstallStep("write", "/etc/pf.anchors/agent.workvm.up", firewall.render_up_template(SLOTS, 500))
    assert plan[5] == InstallStep("write", "/etc/pf.anchors/agent.workvm.dnctl", firewall.render_dummynet(SLOTS, 500))
    assert plan[6] == InstallStep("write", "/etc/pf.anchors/agent.workvm.watch", firewall.watch_script(), mode=0o755)
    assert plan[7].path == "/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist"
    assert plan[8] == InstallStep("patch", "/etc/pf.conf")
    assert [s.argv for s in plan[9:]] == [
        ("/sbin/pfctl", "-n", "-f", "/etc/pf.conf"),
        ("/sbin/pfctl", "-f", "/etc/pf.conf"),
        ("/bin/launchctl", "bootout", "system/swiss.dfx.agent-runner.pf"),
        ("/bin/launchctl", "bootstrap", "system", "/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist"),
    ]
    assert [s.may_fail for s in plan] == [False] * 11 + [True, False]
    assert "/opt/warp-cli" in firewall.install_plan(SLOTS, 500, warp_cli="/opt/warp-cli")[6].content


# -- watcher: the real script under /bin/sh with stub commands -------------------------------------------------


def _stub(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def run_watcher(
    tmp_path: Path, warp: Sequence[str], route: Sequence[str], rounds: int, *, pf_fails: int = 0, pf_on: bool = True
) -> str:
    """Run the watcher for `rounds` rounds; round i answers `warp[i]` / `route[i]`. Returns the pfctl log."""
    (tmp_path / "warp").write_text("".join(f"{line}\n" for line in warp))
    (tmp_path / "route").write_text("".join(f"{line}\n" for line in route))
    (tmp_path / "pf_fails").write_text(str(pf_fails))
    (tmp_path / "pf_info").write_text("Status: Enabled for 1 days\n" if pf_on else "Status: Disabled\n")
    log = tmp_path / "log"
    warp_cli = _stub(
        tmp_path / "warp-cli",
        f'''n=$(cat "{tmp_path}/n" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "{tmp_path}/n"
if [ "$n" -gt {rounds} ]; then kill -TERM $PPID; exit 1; fi
answer=$(sed -n "${{n}}p" "{tmp_path}/warp")
[ "$answer" = fail ] && exit 1
echo "$answer"
''',
    )
    route_stub = _stub(
        tmp_path / "route-stub",
        f'''echo "route $*" >> "{log}"
n=$(cat "{tmp_path}/n"); iface=$(sed -n "${{n}}p" "{tmp_path}/route")
printf \'   route to: x\\n  interface: %s\\n\' "$iface"
''',
    )
    pfctl = _stub(
        tmp_path / "pfctl",
        f'''if [ "$1" = -s ]; then cat "{tmp_path}/pf_info"; exit 0; fi
echo "pfctl $*" >> "{log}"
if [ "$5" = - ]; then sed \'s/^/  stdin: /\' | grep "pass out quick on utun" | head -1 >> "{log}"; fi
fails=$(cat "{tmp_path}/pf_fails")
if [ "$1" = -q ] && [ "$fails" -gt 0 ]; then echo $((fails - 1)) > "{tmp_path}/pf_fails"; exit 1; fi
exit 0
''',
    )
    dnctl = _stub(tmp_path / "dnctl", f'echo "dnctl $*" >> "{log}"\n')
    (tmp_path / "down").write_text(rendered(None))
    (tmp_path / "up").write_text(firewall.render_up_template(SLOTS, 500))
    script = firewall.watch_script(
        warp_cli,
        pfctl=pfctl,
        dnctl=dnctl,
        route=route_stub,
        anchor_path=str(tmp_path / "down"),
        anchor_up_path=str(tmp_path / "up"),
        dummynet_path=str(tmp_path / "dn"),
        seconds=0,
    )
    watcher = _stub(tmp_path / "watch", script.removeprefix("#!/bin/sh\n"))
    result = subprocess.run(["/bin/sh", watcher], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode != 0  # ended by the stub's TERM after the last round
    return log.read_text()


CONNECTED = "Status update: Connected"


def test_watcher_takes_the_enable_reference_and_loads_the_tunnel_interface(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED, CONNECTED], ["utun4", "utun4"], rounds=2).splitlines()
    probe = firewall.resolvers()[0]
    assert log[:2] == [f"dnctl -q {tmp_path}/dn", "pfctl -E"]
    assert log[2] == f"route -n get {probe}"
    assert log[3] == "pfctl -q -a agent.workvm -f -"
    assert log[4].startswith("  stdin: pass out quick on utun4 proto { tcp udp }")
    assert log[5:] == [f"route -n get {probe}"]  # unchanged: no reload in round two


def test_watcher_falls_back_to_the_fail_closed_anchor(tmp_path: Path) -> None:
    log = run_watcher(
        tmp_path,
        [CONNECTED, "Status update: Disconnected", "fail", CONNECTED, CONNECTED],
        ["utun4", "", "", "en0", "utun12"],
        rounds=5,
    )
    loads = [line for line in log.splitlines() if line.startswith(("pfctl -q", "  stdin"))]
    down = f"pfctl -q -a agent.workvm -f {tmp_path}/down"
    assert loads == [
        "pfctl -q -a agent.workvm -f -",
        "  stdin: pass out quick on utun4 proto { tcp udp } from any to any user 601 keep state"
        " (if-bound, max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)",
        down,
        "pfctl -q -a agent.workvm -f -",
        "  stdin: pass out quick on utun12 proto { tcp udp } from any to any user 601 keep state"
        " (if-bound, max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)",
    ]


def test_watcher_retries_a_failed_load_and_refreshes(tmp_path: Path) -> None:
    rounds = firewall.WATCH_REFRESH + 3
    log = run_watcher(tmp_path, [CONNECTED] * rounds, ["utun4"] * rounds, rounds=rounds, pf_fails=1)
    loads = [line for line in log.splitlines() if line.startswith("pfctl -q")]
    # round 1 fails, round 2 loads, round 2 + WATCH_REFRESH reloads
    assert len(loads) == 3


class Files:
    def __init__(self, pf_conf: str) -> None:
        self.content = {"/etc/pf.conf": pf_conf}
        self.writes: list[tuple[str, int]] = []

    def write(self, path: str, content: str, mode: int) -> None:
        self.content[path] = content
        self.writes.append((path, mode))

    def read(self, path: str) -> str:
        return self.content[path]


def test_apply_writes_patches_and_runs() -> None:
    files = Files("pass in all\n")
    answers: Answers = {("/bin/launchctl", "bootout", "system/swiss.dfx.agent-runner.pf"): (3, "")}
    run = FakeRun(answers)
    firewall.apply(firewall.install_plan(SLOTS, 500), run, files.write, files.read)
    assert files.writes == [
        ("/etc/pf.anchors/agent.workvm", 0o644),
        ("/etc/pf.anchors/agent.workvm.sample", 0o644),
        ("/etc/pf.anchors/agent.workvm.up", 0o644),
        ("/etc/pf.anchors/agent.workvm.dnctl", 0o644),
        ("/etc/pf.anchors/agent.workvm.watch", 0o755),
        ("/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist", 0o644),
        ("/etc/pf.conf", 0o644),
    ]
    assert files.content["/etc/pf.conf"].startswith('anchor "agent.workvm"\npass in all\n')
    assert len(run.calls) == 6
    files.writes.clear()
    firewall.apply([InstallStep("patch", "/etc/pf.conf")], run, files.write, files.read)
    assert files.writes == []


def test_apply_stops_at_a_failing_check() -> None:
    files = Files("")
    run = FakeRun({("/sbin/pfctl", "-a", "agent.workvm", "-n", "-f", "/etc/pf.anchors/agent.workvm"): (1, "")})
    with pytest.raises(RunnerError) as caught:
        firewall.apply(firewall.install_plan(SLOTS, 500), run, files.write, files.read)
    assert (caught.value.code, caught.value.name) == ("workvm_firewall_install", "pfctl")
    assert files.content["/etc/pf.conf"] == ""


def test_watcher_enables_pf_again_when_it_was_disabled(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED, CONNECTED], ["utun4", "utun4"], rounds=2, pf_on=False)
    assert log.splitlines().count("pfctl -E") == 3  # at start and in each of the two rounds


def test_check_matches_pfctl_protocol_names() -> None:
    assert firewall.egress_of(LIVE_ANCHOR) == EGRESS
    answers = healthy()
    assert "ipv6-icmp" in answers[firewall.READ_COMMANDS[1]][1]
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()
