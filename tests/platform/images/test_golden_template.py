"""U-RUN-golden (definition part): the Lima template images/golden.yaml and the pins in images/versions.json
(docs/platform/spec/05-runner.md sections 3.1 and 15, ADR-009)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

IMAGES = Path(__file__).resolve().parents[3] / "images"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION_KEYS = {
    "claude_code",
    "claude_code_tarball_url",
    "claude_code_tarball_sha256",
    "claude_code_native_tarball_url",
    "claude_code_native_tarball_sha256",
    "node",
    "node_tarball_url",
    "node_tarball_sha256",
    "ubuntu_image_url",
    "ubuntu_image_digest",
}
CONTROL_SOCKET_FORWARD = {
    "guestSocket": "/run/agent-guest/control.sock",
    "hostSocket": "{{.Dir}}/sock/agent-guest.sock",
}
STAGED_TOKEN = "/run/agent-guest-param/control-token"


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    data = yaml.safe_load((IMAGES / "golden.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def versions() -> dict[str, str]:
    data = json.loads((IMAGES / "versions.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_versions_have_exactly_the_pinned_fields(versions: dict[str, str]) -> None:
    assert set(versions) == VERSION_KEYS
    assert all(isinstance(value, str) and value for value in versions.values())


def test_claude_code_is_the_specified_baseline(versions: dict[str, str]) -> None:
    assert versions["claude_code"] == "2.1.330"
    version = versions["claude_code"]
    assert versions["claude_code_tarball_url"] == (
        f"https://registry.npmjs.org/@anthropic-ai/claude-code/-/claude-code-{version}.tgz"
    )
    assert versions["claude_code_native_tarball_url"] == (
        f"https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/claude-code-linux-arm64-{version}.tgz"
    )


def test_node_tarball_matches_the_node_version(versions: dict[str, str]) -> None:
    node = versions["node"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", node)
    assert versions["node_tarball_url"] == f"https://nodejs.org/dist/v{node}/node-v{node}-linux-arm64.tar.xz"


@pytest.mark.parametrize("key", sorted(key for key in VERSION_KEYS if key.endswith("_sha256")))
def test_digests_are_set(versions: dict[str, str], key: str) -> None:
    assert SHA256.match(versions[key])


def test_ubuntu_image_is_a_dated_release_with_digest(versions: dict[str, str]) -> None:
    assert re.fullmatch(
        r"https://cloud-images\.ubuntu\.com/releases/noble/release-\d{8}/ubuntu-24\.04-server-cloudimg-arm64\.img",
        versions["ubuntu_image_url"],
    )
    digest = versions["ubuntu_image_digest"]
    assert digest.startswith("sha256:")
    assert SHA256.match(digest.removeprefix("sha256:"))


def test_template_image_is_the_pinned_one(golden: dict[str, Any], versions: dict[str, str]) -> None:
    assert golden["images"] == [
        {"location": versions["ubuntu_image_url"], "arch": "aarch64", "digest": versions["ubuntu_image_digest"]}
    ]


def test_platform_and_lima_version(golden: dict[str, Any]) -> None:
    assert str(golden["minimumLimaVersion"]) == "2.3.0"
    assert golden["vmType"] == "vz"
    assert golden["arch"] == "aarch64"


def test_no_mounts_no_containerd_no_forwarding_of_ssh_agents(golden: dict[str, Any]) -> None:
    assert golden["mounts"] == []
    assert golden["containerd"] == {"system": False, "user": False}
    assert golden["ssh"] == {"forwardAgent": False, "forwardX11": False, "forwardX11Trusted": False}


def test_only_the_control_socket_is_forwarded(golden: dict[str, Any]) -> None:
    rules = golden["portForwards"]
    assert rules[0] == CONTROL_SOCKET_FORWARD
    # Every other rule ignores ports, so Lima's implicit fallback rule never forwards a guest port.
    assert rules[1:] == [
        {"guestIP": "0.0.0.0", "proto": "any", "ignore": True},
        {"guestIP": "127.0.0.1", "proto": "any", "ignore": True},
    ]


def test_resources_have_defaults(golden: dict[str, Any]) -> None:
    assert golden["cpus"] == 4
    assert golden["memory"] == "8GiB"
    assert golden["disk"] == "20GiB"


def test_control_token_is_a_parameter_without_default(golden: dict[str, Any]) -> None:
    assert golden["param"] == {"AGENT_GUEST_CONTROL_TOKEN": ""}


def test_provisioning_is_system_mode_with_existing_files(golden: dict[str, Any]) -> None:
    steps = golden["provision"]
    assert [step["mode"] for step in steps] == ["system", "system"]
    assert steps[0] == {"mode": "system", "file": "provision/10-slim.sh"}
    assert (IMAGES / steps[0]["file"]).is_file()


def test_token_step_only_stages_the_parameter_for_the_harden_unit(golden: dict[str, Any]) -> None:
    script = golden["provision"][1]["script"]
    assert "$PARAM_AGENT_GUEST_CONTROL_TOKEN" in script
    assert f">{STAGED_TOKEN}" in script
    assert "umask 077" in script
    assert "install -d -m 0700 -o root -g root /run/agent-guest-param" in script
    harden = (IMAGES / "files/usr/local/lib/agent-guest/harden.sh").read_text(encoding="utf-8")
    assert f"STAGED={STAGED_TOKEN}\n" in harden


def test_template_does_not_spell_the_versions_twice(versions: dict[str, str]) -> None:
    text = (IMAGES / "golden.yaml").read_text(encoding="utf-8")
    assert versions["claude_code"] not in text
    assert versions["node"] not in text


def test_lima_accepts_the_template() -> None:
    limactl = shutil.which("limactl")
    if limactl is None:
        pytest.skip("limactl is not installed on this machine; the runner device validates the template")
    version = subprocess.run([limactl, "--version"], check=True, capture_output=True, text=True).stdout.split()[-1]
    if tuple(int(part) for part in version.split(".")[:2]) < (2, 3):
        pytest.skip(f"limactl {version} is older than the required 2.3.0; the runner device validates the template")
    done = subprocess.run(
        [limactl, "template", "validate", str(IMAGES / "golden.yaml")], check=False, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
