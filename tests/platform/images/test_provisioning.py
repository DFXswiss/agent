"""U-RUN-golden (provisioning part): the shell scripts of the golden image are lint-free and follow
docs/platform/spec/05-runner.md section 3.1 (idempotent, guarded, pinned and verified downloads)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

IMAGES = Path(__file__).resolve().parents[3] / "images"
INSTALL = IMAGES / "provision" / "install.sh"
SLIM = IMAGES / "provision" / "10-slim.sh"
HARDEN = IMAGES / "files" / "usr" / "local" / "lib" / "agent-guest" / "harden.sh"
SCRIPTS = [SLIM, INSTALL, HARDEN]


def _inline_scripts(tmp_path: Path) -> list[Path]:
    golden = yaml.safe_load((IMAGES / "golden.yaml").read_text(encoding="utf-8"))
    paths = []
    for index, step in enumerate(golden["provision"]):
        if "script" in step:
            path = tmp_path / f"golden-provision-{index}.sh"
            path.write_text(step["script"], encoding="utf-8")
            paths.append(path)
    return paths


def _lint(paths: list[Path]) -> subprocess.CompletedProcess[str]:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is not None:
        return subprocess.run(
            [shellcheck, "--severity=style", *map(str, paths)], check=False, capture_output=True, text=True
        )
    for path in paths:
        done = subprocess.run(["bash", "-n", str(path)], check=False, capture_output=True, text=True)
        if done.returncode != 0:
            return done
    return done


def test_scripts_are_lint_free(tmp_path: Path) -> None:
    done = _lint([*SCRIPTS, *_inline_scripts(tmp_path)])
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_are_strict_bash(script: Path) -> None:
    lines = script.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/bin/bash"
    assert "set -euo pipefail" in lines


def test_slim_step_is_guarded_and_masks_wait_online() -> None:
    text = SLIM.read_text(encoding="utf-8")
    assert '[ -f "$GUARD" ] && exit 0' in text
    assert text.rstrip().endswith('touch "$GUARD"')
    assert "systemctl mask systemd-networkd-wait-online.service" in text
    # no background job of the base image may reach the internet from an agent VM (E2E egress check, SCN-02)
    assert "systemctl mask fwupd-refresh.timer fwupd-refresh.service" in text
    for timer in ("apt-daily.timer", "apt-daily-upgrade.timer", "motd-news.timer", "update-notifier-download.timer"):
        assert timer in text
    # boot-time internet jobs of the base image (entropy seeding, Ubuntu Pro status): masked, never started (A-20)
    joined = text.replace("\\\n", " ")
    masked = joined.split("systemctl mask pollinate.service", 1)[1].split("\n", 1)[0].split()
    assert set(masked) == {
        "ubuntu-advantage.service",
        "ua-reboot-cmds.service",
        "ua-timer.timer",
        "ua-timer.service",
        "esm-cache.service",
        "apt-news.service",
    }
    # socket-activated root services: the abstract iscsid socket and the LXD installer (ADR-045)
    assert "systemctl mask iscsid.socket iscsid.service lxd-installer.socket lxd-installer.service" in text
    assert "GRUB_TIMEOUT=0" in text
    for package in ("snapd", "modemmanager", "unattended-upgrades", "needrestart"):
        assert package in text


def test_install_applies_the_kernel_settings_before_the_self_test() -> None:
    text = (IMAGES / "provision" / "install.sh").read_text(encoding="utf-8")
    assert "sysctl -q -p /etc/sysctl.d/60-agent-guest.conf" in text
    assert '[ "$(cat /proc/sys/vm/memfd_noexec)" = 2 ] || die' in text


def test_install_is_guarded_by_the_specified_file() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "GUARD=/var/lib/agent-golden.done\n" in text
    assert 'if [ -f "$GUARD" ]; then' in text
    assert text.rstrip().endswith('log "done"')
    assert 'touch "$GUARD"\nlog "done"' in text


def test_install_requires_the_slim_step_and_drops_apple_double_files() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "[ -f /var/lib/agent-golden/slim.done ] || die" in text
    assert """find "$BUNDLE/images" "$BUNDLE/src" -name '._*' -type f -delete""" in text


def test_install_refuses_a_different_test_mode_after_the_guard() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert '[ "$recorded" = "$TEST_MODEL_PORT" ] || die' in text


@pytest.mark.parametrize("script", [SLIM, INSTALL], ids=lambda path: path.name)
def test_apt_waits_for_the_dpkg_lock(script: Path) -> None:
    calls = re.findall(r"^\s*apt-get (.*)$", script.read_text(encoding="utf-8"), re.MULTILINE)
    assert calls
    assert all(call.startswith("-o DPkg::Lock::Timeout=300 ") or call == "clean" for call in calls)


def test_install_reads_every_pin_from_versions_json() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    versions = json.loads((IMAGES / "versions.json").read_text(encoding="utf-8"))
    used = set(re.findall(r"\$\(version ([a-z_0-9]+)\)", text))
    assert used == set(versions) - {"ubuntu_image_url", "ubuntu_image_digest"}
    for value in versions.values():
        assert value not in text


def test_every_download_is_verified_before_use() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert text.count("$(fetch ") == 3
    assert 'sha256sum -c --status || die "sha256 mismatch for $url"' in text
    assert len(re.findall(r"^\s*curl ", text, re.MULTILINE)) == 1  # only inside fetch()


def test_install_places_the_specified_artefacts() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    for fragment in (
        "install -y -q --no-install-recommends tmux nftables acl ",
        "npm install --global --prefix",
        "--offline",
        'install -m 0755 -o root -g root "$binary" /opt/claude/claude',
        'cp -R "$BUNDLE/src/agent_guest" /opt/agent-guest/agent_guest',
        "find /opt/agent-guest/agent_guest -type d -exec chmod 0755 {} +",
        "find /opt/agent-guest/agent_guest -type f -exec chmod 0644 {} +",
        '"$IMAGES/mcp.json" /opt/agent-guest/mcp.json',
        '"$IMAGES/harness/RULES.md" /opt/agent-guest/harness/RULES.md',
        "python3 -E -s -m agent_guest.sandbox.settings --output /etc/claude-code/managed-settings.json",
        "useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin platform",
        "groupadd --system agent-ctl",
        'gpasswd -M "$lima_user" agent-ctl',
        "nft -c -f /etc/nftables.conf",
        "systemctl enable agent-guest-harden.service agent-guest-control.service agent-guest-api.service",
    ):
        assert fragment in text, fragment


def test_install_never_starts_the_guest_services() -> None:
    # The build VM keeps the full sudo of the Lima user until it is stopped; clones start the services at boot.
    text = INSTALL.read_text(encoding="utf-8")
    assert "--now agent-guest" not in text
    assert "systemctl start agent-guest" not in text
    assert "systemctl restart agent-guest" not in text


def test_session_mount_options_are_checked() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "for option in noexec nosuid nodev; do" in text


def test_test_mode_port_is_validated() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "--test-model-port)" in text
    assert "^[1-9][0-9]{0,4}$" in text
    assert "getent ahostsv4 host.lima.internal" in text


def test_harden_restricts_sudo_and_installs_the_token() -> None:
    text = HARDEN.read_text(encoding="utf-8")
    # cloud-init's unrestricted grant is removed before anything else can fail.
    assert text.index('rm -f "$CLOUD_INIT_SUDOERS"') < text.index("lima_user=")
    for fragment in (
        'gpasswd -M "$lima_user" agent-ctl',
        "for group in sudo admin wheel; do",
        'if ! visudo -cqf "$rendered"; then\n  rm -f "$rendered"',
        "SUDOERS_FILE=/etc/sudoers.d/90-agent-guest",
        "TOKEN_FILE=/etc/agent-guest/control-token",
        "^[A-Za-z0-9_-]{43}$",
        'chmod 0600 "$tmp"',
        'mv -f "$tmp" "$TOKEN_FILE"',
        'rm -f "$STAGED"',
    ):
        assert fragment in text, fragment


def test_harden_keeps_sudo_only_in_a_test_mode_golden() -> None:
    text = HARDEN.read_text(encoding="utf-8")
    # The test grant is removed on every boot before any exit path, and written only for a TEST_MODE install record.
    removed = text.index('rm -f "$TEST_SUDOERS_FILE"')
    assert removed < text.index("lima_user=")
    assert text.index('mv -f "$rendered" "$SUDOERS_FILE"') < text.index('python3 -I -c "$TEST_MODE_CHECK"')
    for fragment in (
        "TEST_SUDOERS_FILE=/etc/sudoers.d/91-agent-guest-test",
        "INSTALL_RECORD=/var/lib/agent-golden/install.json",
        'if python3 -I -c "$TEST_MODE_CHECK" "$INSTALL_RECORD" 2>/dev/null; then',
        "printf '%s ALL=(root) NOPASSWD: ALL\\n' \"$lima_user\"",
        'mv -f "$rendered" "$TEST_SUDOERS_FILE"',
    ):
        assert fragment in text, fragment


def _test_mode_check() -> str:
    line = next(line for line in HARDEN.read_text(encoding="utf-8").splitlines() if line.startswith("TEST_MODE_CHECK="))
    return line.split("=", 1)[1].strip("'")


@pytest.mark.parametrize(
    ("record", "granted"),
    [
        ('{"test_mode": true, "test_model_port": 9}', True),
        ('{"test_mode": false, "test_model_port": null}', False),
        ('{"test_mode": "true"}', False),
        ("{}", False),
        ("not json", False),
        (None, False),
    ],
)
def test_harden_test_mode_check_grants_only_a_test_mode_record(
    tmp_path: Path, record: str | None, granted: bool
) -> None:
    path = tmp_path / "install.json"
    if record is not None:
        path.write_text(record, encoding="utf-8")
    done = subprocess.run([sys.executable, "-I", "-c", _test_mode_check(), str(path)], capture_output=True, check=False)
    assert (done.returncode == 0) is granted
