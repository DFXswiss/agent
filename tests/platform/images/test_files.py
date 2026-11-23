"""U-RUN-golden (static files): nftables, units, mounts, sudoers, runtime directories and mcp.json of the golden
image (docs/platform/spec/05-runner.md sections 4.1, 4.3, 5.1). `nft -c` runs inside the test build."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

IMAGES = Path(__file__).resolve().parents[3] / "images"
FILES = IMAGES / "files"
UNITS = FILES / "etc" / "systemd" / "system"

# 05-runner.md section 4.3, in order.
SPEC_NFT_RULES = [
    "meta skuid 20000-29999 oif lo tcp dport 7700 accept",
    "meta skuid 20000-29999 ct state established,related accept",
    "meta skuid 20000-29999 counter drop",
    'meta skuid "platform" tcp dport 443 accept',
    'meta skuid "platform" udp dport 53 accept',
    'meta skuid "platform" tcp dport 53 accept',
    'meta skuid "platform" oif lo accept',
    'meta skuid "platform" ct state established,related accept',
    'meta skuid "platform" counter drop',
]


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Parse a systemd unit file; keys may repeat, so every value is a list."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for raw in (UNITS / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, value = line.split("=", 1)
        current.setdefault(key, []).append(value)
    return sections


def test_nft_file_holds_exactly_the_specified_rules() -> None:
    text = (FILES / "etc/nftables.d/agent-sessions.nft").read_text(encoding="utf-8")
    body = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert body[:3] == [
        "table inet agent_sessions {",
        "chain out {",
        "type filter hook output priority 0; policy accept;",
    ]
    assert body[3:-2] == SPEC_NFT_RULES
    assert body[-2:] == ["}", "}"]


def test_nftables_conf_loads_the_rule_directory() -> None:
    lines = (FILES / "etc/nftables.conf").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/sbin/nft -f"
    assert [line for line in lines if not line.startswith("#")] == [
        "flush ruleset",
        'include "/etc/nftables.d/*.nft"',
    ]


def test_test_mode_rule_only_opens_the_model_double_for_the_platform_user() -> None:
    text = (FILES / "fragments/agent-sessions.test.nft.in").read_text(encoding="utf-8")
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == [
        'insert rule inet agent_sessions out meta skuid "platform" ip daddr @GATEWAY@ tcp dport @PORT@ accept'
    ]
    # Loaded after agent-sessions.nft (glob order), so the table exists when the rule is inserted.
    assert sorted(["agent-sessions.nft", "agent-sessions.test.nft"])[0] == "agent-sessions.nft"
    assert "/etc/nftables.d/agent-sessions.test.nft" in (IMAGES / "provision/install.sh").read_text(encoding="utf-8")


def test_memfds_are_never_executable() -> None:
    """ADR-045: `vm.memfd_noexec = 2` - no fileless exec through /proc/self/fd from a session (ATS-03)."""
    lines = (FILES / "etc" / "sysctl.d" / "60-agent-guest.conf").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line and not line.startswith("#")] == ["vm.memfd_noexec = 2"]


def test_session_mount_fragment() -> None:
    lines = (FILES / "fragments/fstab").read_text(encoding="utf-8").splitlines()
    assert lines == ["/srv/sessions /srv/sessions none bind,noexec,nosuid,nodev 0 0"]


def test_sudoers_fragment_allows_only_restarting_the_guest_services() -> None:
    text = (FILES / "usr/local/lib/agent-guest/sudoers.in").read_text(encoding="utf-8")
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == [
        "Cmnd_Alias AGENT_GUEST_RESTART = /usr/bin/systemctl restart agent-guest-control, "
        "/usr/bin/systemctl restart agent-guest-control.service, /usr/bin/systemctl restart agent-guest-api, "
        "/usr/bin/systemctl restart agent-guest-api.service",
        "@LIMA_USER@ ALL=(root) NOPASSWD: AGENT_GUEST_RESTART",
    ]
    assert "*" not in text  # sudoers wildcards also match blanks, i.e. further arguments


def test_runtime_and_state_directories() -> None:
    lines = (FILES / "etc/tmpfiles.d/agent-guest.conf").read_text(encoding="utf-8").splitlines()
    entries = [line.split() for line in lines if line and not line.startswith("#")]
    assert entries == [
        ["d", "/run/agent-guest", "0711", "root", "root", "-"],
        ["d", "/run/agent-guest/harness", "0711", "root", "root", "-"],
        ["d", "/var/lib/agent-guest", "0700", "root", "root", "-"],
        ["d", "/etc/agent-guest", "0700", "root", "root", "-"],
    ]


def test_control_unit() -> None:
    sections = unit("agent-guest-control.service")
    service = sections["Service"]
    assert service["User"] == ["root"]
    # Module name shared with GST-01 (docs/platform/decisions/SBX-01.md).
    assert service["ExecStart"] == ["/usr/bin/python3 -E -s -m agent_guest.control"]
    assert service["WorkingDirectory"] == ["/opt/agent-guest"]
    assert service["Restart"] == ["always"]
    assert service["NoNewPrivileges"] == ["yes"]
    assert "agent-guest-harden.service" in sections["Unit"]["After"][0].split()
    # A failed boot hardening keeps the guest services stopped (fail closed).
    assert sections["Unit"]["Requires"] == ["agent-guest-harden.service"]
    assert sections["Install"]["WantedBy"] == ["cloud-init.target"]


def test_api_unit() -> None:
    sections = unit("agent-guest-api.service")
    service = sections["Service"]
    assert service["User"] == ["platform"]
    assert service["Group"] == ["platform"]
    assert service["ExecStart"] == ["/usr/bin/python3 -E -s -m agent_guest.server"]
    assert service["WorkingDirectory"] == ["/opt/agent-guest"]
    for key, value in (
        ("Restart", "always"),
        ("NoNewPrivileges", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"),
        ("PrivateTmp", "yes"),
    ):
        assert service[key] == [value], key
    assert not {"ReadWritePaths", "StateDirectory", "RuntimeDirectory", "LogsDirectory"} & set(service)
    # the transient unit files hold every session's environment (tokens): hidden from the API process too (A-19)
    assert service["InaccessiblePaths"] == ["-/run/systemd/transient"]
    assert sections["Unit"]["After"] == ["agent-guest-control.service agent-guest-harden.service"]
    assert sections["Unit"]["Requires"] == ["agent-guest-harden.service"]
    assert sections["Install"]["WantedBy"] == ["cloud-init.target"]


def test_harden_unit_runs_every_boot_after_cloud_init() -> None:
    sections = unit("agent-guest-harden.service")
    assert sections["Unit"]["After"] == ["cloud-final.service"]
    assert sections["Unit"]["Before"] == ["agent-guest-control.service agent-guest-api.service"]
    # Skipped only during the golden build (flag on /run, written by install.sh, gone at the next boot).
    assert sections["Unit"]["ConditionPathExists"] == ["!/run/agent-golden-build"]
    assert "touch /run/agent-golden-build" in (IMAGES / "provision/install.sh").read_text(encoding="utf-8")
    assert sections["Service"]["Type"] == ["oneshot"]
    assert sections["Service"]["RemainAfterExit"] == ["yes"]
    (exec_start,) = sections["Service"]["ExecStart"]
    assert (FILES / exec_start.lstrip("/")).is_file()
    # cloud-final.service is ordered after multi-user.target; cloud-init.target after cloud-final avoids a cycle.
    assert sections["Install"]["WantedBy"] == ["cloud-init.target"]


@pytest.mark.parametrize("name", ["agent-guest-control.service", "agent-guest-api.service"])
def test_services_never_give_up_restarting(name: str) -> None:
    sections = unit(name)
    assert sections["Unit"]["StartLimitIntervalSec"] == ["0"]
    assert sections["Service"]["RestartSec"] == ["1s"]


def test_installed_tree_is_limited_to_etc_and_usr() -> None:
    tops = {path.relative_to(FILES).parts[0] for path in FILES.rglob("*") if path.is_file()}
    assert tops == {"etc", "usr", "fragments"}


def test_mcp_json_is_the_specified_server() -> None:
    config = json.loads((IMAGES / "mcp.json").read_text(encoding="utf-8"))
    assert config == {
        "mcpServers": {
            "platform": {
                "type": "http",
                "url": "http://127.0.0.1:7700/mcp",
                "headers": {"Authorization": "Bearer ${PLATFORM_SESSION_TOKEN}"},
            }
        }
    }


def test_no_file_names_a_lima_user() -> None:
    for path in FILES.rglob("*"):
        if path.is_file():
            assert not re.search(r"^\s*(lima|ubuntu)\s+ALL", path.read_text(encoding="utf-8"), re.MULTILINE)
