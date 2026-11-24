"""U-RUN-workvm firewall: pf anchor `com.apple/000.agent.workvm` render, watcher, check, install and uninstall
(13-work-vm.md 6.2, 6.3; KOR-29 operations finding 1).

`pfctl`, `launchctl` and file writes are injected; the live answers imitate pfctl's canonical rule output. The
watcher script is run for real by `/bin/sh` with stub commands in place of `warp-cli`, `route`, `pfctl` and `pkill`.
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


def live_anchor(egress: str | None = EGRESS) -> str:
    rules = [*live_rules(601, 47200, egress), *live_rules(602, 47201, egress)]
    return "\n".join(rules) + "\n"


LIVE_ANCHOR = live_anchor()
LIVE_ENTRIES = [*firewall.BLOCKED_V4, *firewall.BLOCKED_V6]
LIVE_TABLE = "".join(f"   {e}\n" for e in LIVE_ENTRIES)
LIVE_MAIN = (
    'scrub-anchor "com.apple/*" all fragment reassemble\n'
    'anchor "com.apple/*" all\n'
    'anchor "com.cloudflare.warp_svc" all\n'
)
ANCHOR = "com.apple/000.agent.workvm"
LIVE_CHILDREN = f"  {ANCHOR}\n  com.apple/200.AirDrop\n  com.apple/250.ApplicationFirewall\n"
LIVE_INFO = "Status: Enabled for 0 days 01:02:03           Debug: Urgent\n"
LIVE_IFACES = "lo0\nen0\n"

Answers = dict[tuple[str, ...], tuple[int, str]]


def healthy() -> Answers:
    return {
        ("/sbin/pfctl", "-s", "info"): (0, LIVE_INFO),
        ("/sbin/pfctl", "-a", ANCHOR, "-sr"): (0, LIVE_ANCHOR),
        ("/sbin/pfctl", "-sr"): (0, LIVE_MAIN),
        ("/sbin/pfctl", "-s", "Interfaces", "-v"): (0, LIVE_IFACES),
        ("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show"): (0, LIVE_TABLE),
        ("/sbin/pfctl", "-a", "com.apple", "-sA"): (0, LIVE_CHILDREN),
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
    assert lines[0].startswith("# pf anchor com.apple/000.agent.workvm")
    assert "internet only on utun4" in lines[1]
    assert "500 Mbit/s (not enforced yet" in lines[2]
    assert lines[3] == "table <workvm_blocked> persist { \\"
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
    assert text.endswith("block return out quick user 602\n")
    # every filter rule names a slot account: the rest of the device is untouched by the anchor
    assert all(" user " in line for line in text.splitlines() if line.startswith(("pass", "block")))


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
    assert text.endswith("block return out quick user 602\n")


def test_render_up_template_is_the_rendered_text_with_the_placeholder() -> None:
    template = firewall.render_up_template(SLOTS, 500)
    assert template.replace("@EGRESS@", EGRESS) == rendered()
    assert template.count("@EGRESS@") == 3


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
    order = [0, 1, 2, 5, 3, 4]
    assert [call for call, _ in run.calls] == [firewall.READ_COMMANDS[i] for i in order]
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
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, "")
    answers[("/sbin/pfctl", "-sr")] = (0, 'pass out quick all\nanchor "com.apple/*" all\n')
    answers[("/sbin/pfctl", "-s", "Interfaces", "-v")] = (0, "lo0 (skip)\nen0\n")
    table = ("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")
    answers[table] = (0, f"   {firewall.BLOCKED_V4[1]}\n")
    answers[("/sbin/pfctl", "-a", "com.apple", "-sA")] = (0, "  com.apple/200.AirDrop\n")
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == (
        "pf_disabled",
        "anchor_missing",
        "not_hooked",
        "not_first",
        "skip_lo0",
        "table_stale",
    )


def test_check_fails_closed_on_failing_commands() -> None:
    answers = {key: (1, out) for key, (_, out) in healthy().items()}
    answers[("/sbin/pfctl", "-s", "info")] = (0, LIVE_INFO)
    assert firewall.check(FakeRun(answers), SLOTS, 500) == firewall.Status(
        ("anchor_missing", "not_hooked", "not_first", "skip_lo0", "table_stale"), None, ("unreadable",)
    )


@pytest.mark.parametrize(
    "live",
    [
        LIVE_ANCHOR.replace("port = smtp", "port = 26", 1),
        LIVE_ANCHOR.replace("user = 602", "user = 603", 1),
        LIVE_ANCHOR.replace("on utun4 proto udp", "on utun5 proto udp", 1),
        LIVE_ANCHOR.replace("pass out quick on utun4 proto tcp", "pass out quick on en0 proto tcp", 1),
        "\n".join(live_rules(601, 47200)) + "\n",
        LIVE_ANCHOR.replace("block return out quick all user = 602", "block drop out quick all user = 602"),
        "\n".join(live_rules(601, 47200) + live_rules(602, 47201, None)) + "\n",
    ],
)
def test_check_detects_an_anchor_mismatch(live: str) -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, live)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("anchor_mismatch",)


def test_check_without_slots_accepts_an_empty_anchor() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, "")
    assert firewall.check(FakeRun(answers), [], 500) == firewall.Status((), None)
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, LIVE_ANCHOR)
    assert firewall.check(FakeRun(answers), [], 500).problems == ("anchor_mismatch",)


@pytest.mark.parametrize(
    "main",
    [
        "",
        'scrub-anchor "com.apple/*" all\nanchor "com.cloudflare.warp_svc" all\n',
        'anchor "com.cloudflare.warp_svc" all\nanchor "com.apple/*" all\n',
        'pass quick on en0 all\nanchor "com.apple/*" all\n',
        'pass in quick on en0 all\nanchor "com.apple/*" all\n',
        'anchor "other" all\nanchor "com.apple/*" all\n',
    ],
)
def test_check_requires_the_hook_before_the_client_and_any_pass_quick(main: str) -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-sr")] = (0, main)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("not_hooked",)
    assert not firewall.hooked(main)


@pytest.mark.parametrize(
    "main",
    [LIVE_MAIN + "pass out quick on en0 all\npass in all\n", 'anchor "com.apple/*" all\n'],
)
def test_check_accepts_a_pass_quick_after_the_hook_and_no_client(main: str) -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-sr")] = (0, main)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()


@pytest.mark.parametrize(
    ("listing", "first"),
    [
        (LIVE_CHILDREN, True),
        (f"\n  {ANCHOR}\n", True),
        (f"  com.apple/000.aaa\n  {ANCHOR}\n", False),
        ("", False),
        ("  com.apple/000.agent.workvm.x\n", False),
    ],
)
def test_first_child_needs_our_anchor_first(listing: str, first: bool) -> None:
    assert firewall.first_child(listing) is first


def test_check_reports_another_first_child() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", "com.apple", "-sA")] = (0, f"  com.apple/000.aaa\n  {ANCHOR}\n")
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("not_first",)


def test_preflight_needs_the_hook_and_reads_only() -> None:
    run = FakeRun({("/sbin/pfctl", "-sr"): (0, LIVE_MAIN)})
    assert firewall.preflight(run) == []
    assert [call for call, _ in run.calls] == [("/sbin/pfctl", "-sr")]
    assert firewall.preflight(FakeRun({("/sbin/pfctl", "-sr"): (0, 'anchor "com.cloudflare.warp_svc" all\n')})) == [
        "not_hooked"
    ]
    assert firewall.preflight(FakeRun({("/sbin/pfctl", "-sr"): (1, "")})) == ["pfctl_failed"]


# `pfctl -a com.apple/000.agent.workvm -sr` of one slot as loaded on a test device whose client interface is utun4
# (KOR-29 rehearsal 2026-11-24): pfctl's optimizer regroups the rules by protocol and prints its own state options.
PF_LOADED = """\
pass out quick on lo0 inet proto tcp from 127.0.0.1 to 127.0.0.1 port = 47200 user = 601 flags S/SA keep state
pass out quick on lo0 inet proto tcp from 127.0.0.1 port = 47200 to 127.0.0.1 user = 601 no state
block return out quick on lo0 proto tcp all user = 601
block return out quick proto tcp from any to <workvm_blocked> user = 601
block return out quick proto tcp from any to any port = 25 user = 601
block return out quick on lo0 proto udp all user = 601
block return out quick proto udp from any to <workvm_blocked> user = 601
pass out quick on utun4 proto tcp all user = 601 flags S/SA keep state (max 4096, source-track rule, \
max-src-conn 2048, max-src-conn-rate 2000/10, if-bound, adaptive.start 2454, adaptive.end 4908, src.track 10)
pass out quick on utun4 proto udp all user = 601 keep state (max 4096, source-track rule, max-src-conn 2048, \
max-src-conn-rate 2000/10, if-bound, adaptive.start 2454, adaptive.end 4908, src.track 10)
block return out quick all user = 601
""".replace("\\\n", "")
# `pfctl -t workvm_blocked -T show` as pf prints it: indented, hosts without a length, mapped addresses dotted
PF_TABLE = "".join(
    f"   {entry}\n"
    for entry in (
        *(e.removesuffix("/32") for e in firewall.BLOCKED_V4),
        *(e.removesuffix("/128").replace("::ffff:0:0/96", "::ffff:0.0.0.0/96") for e in firewall.BLOCKED_V6),
    )
)


@pytest.mark.parametrize("egress", ["utun4", "utun0", "utun17"])
def test_check_passes_on_pf_output_with_any_tunnel_name(egress: str) -> None:
    slot = [Slot(0, "agentwvm00", 601, 47200)]
    answers = healthy()
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, PF_LOADED.replace("utun4", egress))
    answers[("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")] = (0, PF_TABLE)
    assert firewall.check(FakeRun(answers), slot, 500) == firewall.Status((), egress)
    # the same rules in another grouping are the same filter; a missing rule is not
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, "".join(reversed(PF_LOADED.splitlines(keepends=True))))
    assert firewall.check(FakeRun(answers), slot, 500).problems == ()
    broken = PF_LOADED.replace("block return out quick proto udp from any to <workvm_blocked> user = 601\n", "")
    answers[("/sbin/pfctl", "-a", ANCHOR, "-sr")] = (0, broken)
    assert firewall.check(FakeRun(answers), slot, 500).problems == ("anchor_mismatch",)


def test_check_names_the_ranges_the_table_lacks() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")] = (
        0,
        PF_TABLE.replace("   10.0.0.0/8\n", "   10.0.0.0/9\n"),
    )
    status = firewall.check(FakeRun(answers), SLOTS, 500)
    assert (status.problems, status.table_missing) == (("table_stale",), ("10.0.0.0/8",))
    answers[("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")] = (0, "   10.0.0.0/7\n" + PF_TABLE)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()  # a wider entry covers it
    answers[("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")] = (1, "")
    status = firewall.check(FakeRun(answers), SLOTS, 500)
    assert (status.problems, status.table_missing) == (("table_stale",), ("unreadable",))


def test_check_table_ignores_noise_and_needs_every_range() -> None:
    answers = healthy()
    answers[("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")] = (
        0,
        "No ALTQ support in kernel\n" + LIVE_TABLE + "   198.51.100.7\n",
    )
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()
    answers[("/sbin/pfctl", "-a", ANCHOR, "-t", "workvm_blocked", "-T", "show")] = (
        0,
        LIVE_TABLE.replace(f"   {firewall.BLOCKED_V6[-1]}\n", ""),
    )
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ("table_stale",)


def test_sudo_run_prefixes_sudo_non_interactive() -> None:
    run = FakeRun({})
    firewall.sudo_run(run)(["/sbin/pfctl", "-s", "info"], 3.0)
    assert run.calls == [(("/usr/bin/sudo", "-n", "/sbin/pfctl", "-s", "info"), 3.0)]


def test_without_legacy_lines_removes_only_the_earlier_installers_lines() -> None:
    apple = (
        'scrub-anchor "com.apple/*"\nanchor "com.apple/*"\nload anchor "com.apple" from "/etc/pf.anchors/com.apple"\n'
    )
    legacy = 'anchor "agent.workvm"\n' + apple + 'load anchor "agent.workvm" from "/etc/pf.anchors/agent.workvm"\n'
    assert firewall.without_legacy_lines(legacy) == apple
    assert firewall.without_legacy_lines(apple) is apple
    assert firewall.without_legacy_lines("") == ""


def test_daemon_plist_keeps_the_watcher_alive() -> None:
    data = plistlib.loads(firewall.daemon_plist().encode())
    assert data == {
        "Label": "swiss.dfx.agent-runner.pf",
        "ProgramArguments": ["/etc/pf.anchors/agent.workvm.watch"],
        "RunAtLoad": True,
        "KeepAlive": True,
    }


def test_install_plan_loads_only_the_anchor_never_the_main_ruleset() -> None:
    plan = firewall.install_plan(SLOTS, 500)
    path = "/etc/pf.anchors/agent.workvm"
    assert plan[0] == InstallStep("write", path, rendered(None))
    assert plan[1].argv == ("/sbin/pfctl", "-a", ANCHOR, "-n", "-f", path)
    assert plan[2] == InstallStep("write", f"{path}.sample", firewall.render(SLOTS, "utun0", 500))
    assert plan[3].argv == ("/sbin/pfctl", "-a", ANCHOR, "-n", "-f", f"{path}.sample")
    assert plan[4] == InstallStep("write", f"{path}.up", firewall.render_up_template(SLOTS, 500))
    assert plan[5] == InstallStep("write", f"{path}.watch", firewall.watch_script(SLOTS), mode=0o755)
    assert plan[6].path == "/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist"
    assert [s.argv for s in plan[7:]] == [
        ("/sbin/pfctl", "-a", ANCHOR, "-f", path),
        ("/bin/launchctl", "bootout", "system/swiss.dfx.agent-runner.pf"),
        ("/bin/launchctl", "bootstrap", "system", "/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist"),
    ]
    assert [s.may_fail for s in plan] == [False] * 8 + [True, False]
    for step in plan:
        assert "/etc/pf.conf" not in step.argv
        assert step.path != "/etc/pf.conf"
        if step.action == "run" and step.argv[0] == "/sbin/pfctl":
            assert step.argv[1:3] == ("-a", ANCHOR)  # never the main ruleset
    assert "/opt/warp-cli" in firewall.install_plan(SLOTS, 500, warp_cli="/opt/warp-cli")[5].content


def test_uninstall_plan_stops_the_watcher_first_and_never_loads_pf_conf() -> None:
    plan = firewall.uninstall_plan()
    assert plan[0] == InstallStep(
        "run", argv=("/bin/launchctl", "bootout", "system/swiss.dfx.agent-runner.pf"), may_fail=True
    )
    assert plan[3] == InstallStep("run", argv=("/sbin/pfctl", "-a", ANCHOR, "-F", "all"), may_fail=True)
    removed = [step.path for step in plan if step.action == "remove"]
    assert removed == [
        "/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist",
        "/etc/pf.anchors/agent.workvm.watch",
        "/etc/pf.anchors/agent.workvm",
        "/etc/pf.anchors/agent.workvm.sample",
        "/etc/pf.anchors/agent.workvm.up",
        "/var/run/agent.workvm.state",
        "/etc/pf.anchors/agent.workvm.dnctl",
    ]
    assert plan[-1] == InstallStep("unpatch", "/etc/pf.conf")
    assert not any(step.action == "run" and "/etc/pf.conf" in step.argv for step in plan)


# -- watcher: the real script under /bin/sh with stub commands -------------------------------------------------


def _stub(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def run_watcher(
    tmp_path: Path,
    warp: Sequence[str],
    route: Sequence[str],
    rounds: int,
    *,
    pf_fails: int = 0,
    pf_on: bool = True,
    enable_works: bool = True,
    mains: Sequence[str] = (),
    anchor_empty_at: Sequence[int] = (),
    unordered_at: Sequence[int] = (),
) -> str:
    """Run the watcher for `rounds` rounds; round i answers `warp[i]` / `route[i]` and the main ruleset `mains[i]`
    (default `LIVE_MAIN`); in the rounds of `anchor_empty_at` the anchor reads empty. Returns the stub log."""
    (tmp_path / "warp").write_text("".join(f"{line}\n" for line in warp))
    (tmp_path / "route").write_text("".join(f"{line}\n" for line in route))
    for index in range(rounds):
        main = mains[index] if index < len(mains) else LIVE_MAIN
        (tmp_path / f"main.{index + 1}").write_text(main)
    (tmp_path / "empty").write_text("".join(f"{n}\n" for n in anchor_empty_at))
    (tmp_path / "unordered").write_text("".join(f"{n}\n" for n in unordered_at))
    (tmp_path / "pf_fails").write_text(str(pf_fails))
    (tmp_path / "pf_info").write_text("Status: Enabled for 1 days\n" if pf_on else "Status: Disabled\n")
    log = tmp_path / "log"
    warp_cli = _stub(
        tmp_path / "warp-cli",
        f'''n=$(cat "{tmp_path}/n")
[ "$1 $2" = "--accept-tos status" ] || exit 2
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
    enable = f'echo "Status: Enabled" > "{tmp_path}/pf_info"' if enable_works else "exit 1"
    pfctl = _stub(
        tmp_path / "pfctl",
        f'''if [ "$1" = -s ]; then cat "{tmp_path}/pf_info"; exit 0; fi
if [ "$1" = -sr ]; then
  n=$(cat "{tmp_path}/n" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "{tmp_path}/n"
  if [ "$n" -gt {rounds} ]; then kill -TERM "$(cat "{tmp_path}/pid")"; exit 1; fi
  cat "{tmp_path}/main.$n"; exit 0
fi
if [ "$1" = -E ]; then echo "pfctl -E" >> "{log}"; {enable}; exit 0; fi
if [ "$3" = -sA ]; then
  n=$(cat "{tmp_path}/n")
  grep -qx "$n" "{tmp_path}/unordered" && echo "  com.apple/000.aaa"
  echo "  {ANCHOR}"
  exit 0
fi
if [ "$3" = -sr ]; then
  n=$(cat "{tmp_path}/n")
  grep -qx "$n" "{tmp_path}/empty" || echo "block return out quick all user = 601"
  exit 0
fi
echo "pfctl $*" >> "{log}"
if [ "$5" = - ]; then sed \'s/^/  stdin: /\' | grep "pass out quick on utun" | head -1 >> "{log}"; fi
fails=$(cat "{tmp_path}/pf_fails")
if [ "$1" = -q ] && [ "$fails" -gt 0 ]; then echo $((fails - 1)) > "{tmp_path}/pf_fails"; exit 1; fi
exit 0
''',
    )
    pkill = _stub(tmp_path / "pkill", f'echo "pkill $*" >> "{log}"\n')
    (tmp_path / "down").write_text(rendered(None))
    (tmp_path / "up").write_text(firewall.render_up_template(SLOTS, 500))
    script = firewall.watch_script(
        SLOTS,
        warp_cli,
        pfctl=pfctl,
        pkill=pkill,
        route=route_stub,
        anchor_path=str(tmp_path / "down"),
        anchor_up_path=str(tmp_path / "up"),
        state_path=str(tmp_path / "state"),
        seconds=0,
    )
    watcher = _stub(tmp_path / "watch", script.removeprefix("#!/bin/sh\n"))
    log.write_text("")
    # the stub ends the watcher itself (its own parent may be a command substitution's subshell)
    start = f'echo $$ > "{tmp_path}/pid"; exec /bin/sh "{watcher}"'
    result = subprocess.run(["/bin/sh", "-c", start], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode != 0  # ended by the stub's TERM after the last round
    return log.read_text()


CONNECTED = "Status update: Connected"
TCP_UDP_601 = (
    "  stdin: pass out quick on {} proto {{ tcp udp }} from any to any user 601 keep state"
    " (if-bound, max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)"
)


def loads(log: str) -> list[str]:
    return [line for line in log.splitlines() if line.startswith(("pfctl -q", "  stdin"))]


def test_watcher_loads_the_tunnel_interface_and_writes_its_state(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED, CONNECTED], ["utun4", "utun4"], rounds=2).splitlines()
    probe = firewall.resolvers()[0]
    assert log[0] == f"route -n get {probe}"
    assert log[1] == f"pfctl -q -a {ANCHOR} -f -"
    assert log[2] == TCP_UDP_601.format("utun4")
    assert log[3:] == [f"route -n get {probe}"]  # unchanged: no reload in round two
    assert (tmp_path / "state").read_text() == "ok utun4\n"


def test_watcher_falls_back_to_the_fail_closed_anchor(tmp_path: Path) -> None:
    log = run_watcher(
        tmp_path,
        [CONNECTED, "Status update: Disconnected", "fail", CONNECTED, CONNECTED],
        ["utun4", "", "", "en0", "utun12"],
        rounds=5,
    )
    down = f"pfctl -q -a {ANCHOR} -f {tmp_path}/down"
    assert loads(log) == [
        f"pfctl -q -a {ANCHOR} -f -",
        TCP_UDP_601.format("utun4"),
        down,
        f"pfctl -q -a {ANCHOR} -f -",
        TCP_UDP_601.format("utun12"),
    ]
    assert (tmp_path / "state").read_text() == "ok utun12\n"


def test_watcher_writes_the_fail_closed_state(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, ["Status update: Disconnected"], [""], rounds=1)
    assert loads(log) == [f"pfctl -q -a {ANCHOR} -f {tmp_path}/down"]
    assert (tmp_path / "state").read_text() == "ok -\n"


def test_watcher_retries_a_failed_load_and_refreshes(tmp_path: Path) -> None:
    rounds = firewall.WATCH_REFRESH + 3
    log = run_watcher(tmp_path, [CONNECTED] * rounds, ["utun4"] * rounds, rounds=rounds, pf_fails=1)
    # round 1 fails, round 2 loads, round 2 + WATCH_REFRESH reloads
    assert len([line for line in log.splitlines() if line.startswith("pfctl -q")]) == 3


def test_watcher_reloads_an_anchor_someone_flushed(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED] * 3, ["utun4"] * 3, rounds=3, anchor_empty_at=[3])
    assert len([line for line in log.splitlines() if line.startswith("pfctl -q")]) == 2


@pytest.mark.parametrize(
    "main",
    [
        'scrub-anchor "com.apple/*" all\nanchor "com.cloudflare.warp_svc" all\n',
        'anchor "com.cloudflare.warp_svc" all\nanchor "com.apple/*" all\n',
        'pass out quick all\nanchor "com.apple/*" all\n',
        'pass quick on en0 all\nanchor "com.apple/*" all\n',
        'anchor "other" all\nanchor "com.apple/*" all\n',
    ],
)
def test_watcher_stops_the_slots_without_an_effective_hook(tmp_path: Path, main: str) -> None:
    log = run_watcher(tmp_path, [CONNECTED] * 3, ["utun4"] * 3, rounds=3, mains=[LIVE_MAIN, main, LIVE_MAIN])
    lines = log.splitlines()
    kills = ["pkill -KILL -u agentwvm00", "pkill -KILL -u agentwvm01"]
    assert [line for line in lines if line.startswith("pkill")] == kills  # only in round two
    first_kill = lines.index(kills[0])
    # round two loads nothing; when the hook is back, round three loads the anchor again
    assert [line for line in lines[:first_kill] if line.startswith("pfctl -q")] == [f"pfctl -q -a {ANCHOR} -f -"]
    assert [line for line in lines[first_kill:] if line.startswith("pfctl -q")] == [f"pfctl -q -a {ANCHOR} -f -"]
    assert (tmp_path / "state").read_text() == "ok utun4\n"


def test_watcher_stops_the_slots_when_another_child_comes_first(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED] * 2, ["utun4"] * 2, rounds=2, unordered_at=[2])
    lines = log.splitlines()
    assert [line for line in lines if line.startswith("pkill")] == [
        "pkill -KILL -u agentwvm00",
        "pkill -KILL -u agentwvm01",
    ]
    assert lines.index("pkill -KILL -u agentwvm00") > lines.index(f"pfctl -q -a {ANCHOR} -f -")
    assert (tmp_path / "state").read_text() == "unordered\n"


def test_watcher_writes_unhooked_while_the_hook_is_missing(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED], ["utun4"], rounds=1, mains=[""])
    assert loads(log) == []
    assert (tmp_path / "state").read_text() == "unhooked\n"
    assert "route" not in log  # the client is not even asked


def test_watcher_enables_pf_again_when_it_was_disabled(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED, CONNECTED], ["utun4", "utun4"], rounds=2, pf_on=False)
    assert log.splitlines().count("pfctl -E") == 1
    assert (tmp_path / "state").read_text() == "ok utun4\n"


def test_watcher_stops_the_slots_while_pf_stays_disabled(tmp_path: Path) -> None:
    log = run_watcher(tmp_path, [CONNECTED], ["utun4"], rounds=1, pf_on=False, enable_works=False)
    assert log.splitlines()[:3] == ["pfctl -E", "pkill -KILL -u agentwvm00", "pkill -KILL -u agentwvm01"]
    assert (tmp_path / "state").read_text() == "unhooked\n"


def test_watch_script_refuses_invalid_slots() -> None:
    with pytest.raises(RunnerError, match="uid"):
        firewall.watch_script([Slot(0, "a", 1, 47200)])


class Files:
    def __init__(self, pf_conf: str) -> None:
        self.content = {"/etc/pf.conf": pf_conf}
        self.writes: list[tuple[str, int]] = []
        self.removed: list[str] = []

    def write(self, path: str, content: str, mode: int) -> None:
        self.content[path] = content
        self.writes.append((path, mode))

    def read(self, path: str) -> str:
        return self.content[path]

    def remove(self, path: str) -> None:
        self.content.pop(path, None)
        self.removed.append(path)


def test_apply_writes_and_runs_without_touching_pf_conf() -> None:
    files = Files("pass in all\n")
    answers: Answers = {("/bin/launchctl", "bootout", "system/swiss.dfx.agent-runner.pf"): (3, "")}
    run = FakeRun(answers)
    firewall.apply(firewall.install_plan(SLOTS, 500), run, files.write, files.read, files.remove)
    assert files.writes == [
        ("/etc/pf.anchors/agent.workvm", 0o644),
        ("/etc/pf.anchors/agent.workvm.sample", 0o644),
        ("/etc/pf.anchors/agent.workvm.up", 0o644),
        ("/etc/pf.anchors/agent.workvm.watch", 0o755),
        ("/Library/LaunchDaemons/swiss.dfx.agent-runner.pf.plist", 0o644),
    ]
    assert files.content["/etc/pf.conf"] == "pass in all\n"
    assert len(run.calls) == 5


def test_apply_uninstall_removes_files_and_the_legacy_lines() -> None:
    files = Files('anchor "agent.workvm"\npass in all\n')
    files.content["/etc/pf.anchors/agent.workvm"] = "x"
    run = FakeRun({("/sbin/pfctl", "-a", ANCHOR, "-F", "all"): (1, "")})
    firewall.apply(firewall.uninstall_plan(), run, files.write, files.read, files.remove)
    assert files.content == {"/etc/pf.conf": "pass in all\n"}
    assert files.writes == [("/etc/pf.conf", 0o644)]
    assert len(files.removed) == 7
    files.writes.clear()
    firewall.apply([InstallStep("unpatch", "/etc/pf.conf")], run, files.write, files.read, files.remove)
    assert files.writes == []


def test_apply_stops_at_a_failing_check() -> None:
    files = Files("")
    run = FakeRun({("/sbin/pfctl", "-a", ANCHOR, "-n", "-f", "/etc/pf.anchors/agent.workvm"): (1, "")})
    with pytest.raises(RunnerError) as caught:
        firewall.apply(firewall.install_plan(SLOTS, 500), run, files.write, files.read, files.remove)
    assert (caught.value.code, caught.value.name) == ("workvm_firewall_install", "pfctl")
    assert files.writes == [("/etc/pf.anchors/agent.workvm", 0o644)]


def test_check_matches_pfctl_protocol_names() -> None:
    assert firewall.egress_of(LIVE_ANCHOR) == EGRESS
    live = LIVE_ANCHOR.replace("port = smtp", "port = 25")
    answers = healthy()
    answers[firewall.READ_COMMANDS[1]] = (0, live)
    assert firewall.check(FakeRun(answers), SLOTS, 500).problems == ()
