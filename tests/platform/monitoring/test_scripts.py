"""U-MON test-stack scripts: syntax, argument checks, and the limactl calls of up.sh and down.sh against a recording
`limactl` double (the real stack runs on the runner device: `up.sh … --check`, acceptance report of MON-01)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from .conftest import MONITORING

STACK = MONITORING / "test-stack"
SCRIPTS = ("up.sh", "down.sh", "provision.sh", "check.sh", "archive.sh", "fetch.sh")
FAKE_LIMACTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$LIMACTL_LOG"
case "$*" in *"sudo cat /etc/agent-monitoring/up.conf") printf '%s\\n' "$LIMACTL_CONF"; exit 0 ;; esac
if [ "$1" = list ]; then
  case "$LIMACTL_STATE" in
    missing) exit 1 ;;
    *) [ "$3" = '{{.Status}}' ] && echo "$LIMACTL_STATE" || echo "$4" ;;
  esac
fi
exit 0
"""


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_executable_bash(name: str) -> None:
    path = STACK / name
    assert os.access(path, os.X_OK)
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    subprocess.run(["bash", "-n", str(path)], check=True)
    assert "set -euo pipefail" in path.read_text(encoding="utf-8")


def run(
    script: str, *args: str, tmp_path: Path, state: str = "missing", path: bool = True, conf: str = ""
) -> tuple[int, str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "limactl"
    fake.write_text(FAKE_LIMACTL, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "limactl.log"
    log.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin" if path else "/usr/bin:/bin",
        "LIMACTL_LOG": str(log),
        "LIMACTL_STATE": state,
        "LIMACTL_CONF": conf,
        "HOME": str(tmp_path),
    }
    done = subprocess.run(["bash", str(STACK / script), *args], capture_output=True, text=True, env=env, check=False)
    return done.returncode, done.stderr, log.read_text(encoding="utf-8").splitlines()


def set_expression(calls: list[str]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    """Mounts, port forwards and the further assignments of the `create --set` expression."""
    create = next(call for call in calls if call.startswith("create "))
    expression = create.split("--set ", 1)[1].rsplit(" ", 1)[0]
    mounts, forwards, *rest = expression.split(" | ")
    return json.loads(mounts.split(" = ", 1)[1]), json.loads(forwards.split(" = ", 1)[1]), rest


def test_up_creates_provisions_and_checks(tmp_path: Path) -> None:
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    code, err, calls = run(
        "up.sh", "--textfile-dir", str(textfile_dir), "--prefix", "wp-t-", "--server", "runner-a",
        "--forward-port", "21390", "--check", "--expect-active", "agent-runner-capacity-low", tmp_path=tmp_path,
    )  # fmt: skip
    assert code == 0, err
    assert calls[0] == "list --format {{.Status}} wp-t-monitoring"
    assert calls[1].startswith("create --tty=false --name wp-t-monitoring --set ")
    assert calls[1].endswith(f" {STACK}/lima.yaml")
    mounts, forwards, rest = set_expression(calls)
    assert rest == []
    assert mounts == [
        {"location": str(MONITORING), "mountPoint": "/opt/agent-monitoring", "writable": False},
        {"location": str(textfile_dir), "mountPoint": "/mnt/agent-runner-textfile", "writable": False},
    ]
    assert forwards == [
        {"guestPort": 9090, "hostIP": "127.0.0.1", "hostPort": 21390},
        {"guestPort": 3000, "hostIP": "127.0.0.1", "hostPort": 21391},
        {"guestPortRange": [1, 65535], "ignore": True},
    ]
    assert calls[2] == "start --tty=false wp-t-monitoring"
    assert calls[3].startswith("shell --workdir / wp-t-monitoring sudo bash -c ")
    assert calls[3].endswith(
        f" _ /etc/agent-monitoring/up.conf assets={MONITORING} textfile={textfile_dir} forward=21390"
    )
    assert (
        calls[4] == "shell --workdir / wp-t-monitoring sudo bash /opt/agent-monitoring/test-stack/provision.sh runner-a"
    )
    assert calls[5] == (
        "shell --workdir / wp-t-monitoring sudo bash /opt/agent-monitoring/test-stack/check.sh runner-a "
        "--expect-active agent-runner-capacity-low"
    )
    assert "grafana http://127.0.0.1:21391" in err


def test_up_offline_mounts_the_archives_and_sets_the_parameter(tmp_path: Path) -> None:
    textfile_dir, archives = tmp_path / "textfile", tmp_path / "archives"
    textfile_dir.mkdir()
    archives.mkdir()
    code, err, calls = run(
        "up.sh", "--textfile-dir", str(textfile_dir), "--archives", str(archives), "--prefix", "wp-t-",
        tmp_path=tmp_path,
    )  # fmt: skip
    assert code == 0, err
    mounts, _, rest = set_expression(calls)
    assert mounts[2] == {"location": str(archives), "mountPoint": "/mnt/agent-monitoring-archives", "writable": False}
    assert rest == ['.param.AGENT_MONITORING_OFFLINE = "1"']
    assert calls[3].endswith(f" textfile={textfile_dir} forward=none archives={archives}")


def test_up_reuses_an_offline_vm_only_with_the_same_archives(tmp_path: Path) -> None:
    conf = f"assets={MONITORING} textfile={tmp_path} forward=none archives={tmp_path}"
    args = ("--textfile-dir", str(tmp_path), "--archives", str(tmp_path))
    assert run("up.sh", *args, tmp_path=tmp_path, state="Running", conf=conf)[0] == 0
    code, err, _ = run("up.sh", "--textfile-dir", str(tmp_path), tmp_path=tmp_path, state="Running", conf=conf)
    assert (code, "run down.sh first" in err) == (2, True)


def test_up_reuses_a_running_vm_with_the_same_mounts(tmp_path: Path) -> None:
    conf = f"assets={MONITORING} textfile={tmp_path} forward=none"
    code, err, calls = run("up.sh", "--textfile-dir", str(tmp_path), tmp_path=tmp_path, state="Running", conf=conf)
    assert code == 0, err
    assert calls == [
        "list --format {{.Status}} monitoring",
        "shell --workdir / monitoring sudo cat /etc/agent-monitoring/up.conf",
        "shell --workdir / monitoring sudo bash /opt/agent-monitoring/test-stack/provision.sh runner-device",
    ]


def test_up_refuses_a_vm_with_other_mounts_or_forwards(tmp_path: Path) -> None:
    conf = f"assets={MONITORING} textfile={tmp_path} forward=21390"
    code, err, calls = run("up.sh", "--textfile-dir", str(tmp_path), tmp_path=tmp_path, state="Stopped", conf=conf)
    assert code == 2
    assert "exists with other mounts or forwards" in err
    assert "run down.sh first" in err
    assert calls[1] == "start --tty=false monitoring"
    assert not any("provision.sh" in call for call in calls)


def test_up_creates_without_forwards(tmp_path: Path) -> None:
    code, err, calls = run("up.sh", "--textfile-dir", str(tmp_path), "--check", tmp_path=tmp_path)
    assert code == 0, err
    _, forwards, _ = set_expression(calls)
    assert forwards == [{"guestPortRange": [1, 65535], "ignore": True}]
    assert calls[-1].endswith("check.sh runner-device")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "usage"),
        (["--bogus"], "usage"),
        (["--textfile-dir", "TMP", "--prefix"], "usage"),
        (["--textfile-dir", '/tmp/a"b'], "letters, digits"),
        (["--textfile-dir", "relative"], "must be absolute"),
        (["--textfile-dir", "/nonexistent-agent-monitoring"], "does not exist"),
        (["--textfile-dir", "TMP", "--server", "Bad"], "invalid --server"),
        (["--textfile-dir", "TMP", "--prefix", "a b"], "invalid --prefix"),
        (["--textfile-dir", "TMP", "--forward-port", "80"], "invalid --forward-port"),
        (["--textfile-dir", "TMP", "--forward-port", "x"], "invalid --forward-port"),
        (["--textfile-dir", "TMP", "--archives", "relative"], "--archives: absolute"),
        (["--textfile-dir", "TMP", "--archives", "/nonexistent-agent-archives"], "fill it with fetch.sh"),
        (["--textfile-dir", "TMP", "--archives"], "usage"),
    ],
)
def test_up_refuses_bad_arguments(tmp_path: Path, args: list[str], message: str) -> None:
    args = [str(tmp_path) if item == "TMP" else item for item in args]
    code, err, calls = run("up.sh", *args, tmp_path=tmp_path)
    assert code == 2
    assert message in err.lower() or message in err
    assert calls == []


def test_up_needs_limactl(tmp_path: Path) -> None:
    code, err, _ = run("up.sh", "--textfile-dir", str(tmp_path), tmp_path=tmp_path, path=False)
    assert (code, "limactl not found" in err) == (2, True)


def test_down_deletes_an_existing_vm(tmp_path: Path) -> None:
    code, err, calls = run("down.sh", "--prefix", "wp-t-", tmp_path=tmp_path, state="Running")
    assert code == 0, err
    assert calls[-1] == "delete -f wp-t-monitoring"
    assert "deleted wp-t-monitoring" in err


def test_down_is_idempotent_and_checks_arguments(tmp_path: Path) -> None:
    code, err, calls = run("down.sh", tmp_path=tmp_path)
    assert (code, "monitoring does not exist" in err) == (0, True)
    assert not any(call.startswith("delete") for call in calls)
    assert run("down.sh", "--prefix", "A B", tmp_path=tmp_path)[0] == 2
    assert run("down.sh", "--what", tmp_path=tmp_path)[0] == 2
    assert run("down.sh", tmp_path=tmp_path, path=False)[0] == 2


def test_provision_and_check_refuse_bad_servers(tmp_path: Path) -> None:
    done = subprocess.run(
        ["bash", str(STACK / "provision.sh"), "Bad Name"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 2
    assert "invalid server name" in done.stderr
    done = subprocess.run(["bash", str(STACK / "check.sh")], capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "usage: check.sh SERVER" in done.stderr


def test_lima_template_keeps_every_port_inside_and_mounts_nothing() -> None:
    template = yaml.safe_load((STACK / "lima.yaml").read_text(encoding="utf-8"))
    assert template["base"] == "template:ubuntu-24.04"
    assert template["vmType"] == "vz"
    assert template["mounts"] == []
    assert template["portForwards"] == [{"guestPortRange": [1, 65535], "ignore": True}]
    assert template["containerd"] == {"system": False, "user": False}


def test_lima_template_drops_outbound_traffic_from_the_first_boot_in_offline_mode() -> None:
    template = yaml.safe_load((STACK / "lima.yaml").read_text(encoding="utf-8"))
    assert template["param"] == {"AGENT_MONITORING_OFFLINE": "0"}
    [boot] = template["provision"]
    assert boot["mode"] == "boot"
    script = boot["script"]
    assert '[ "{{.Param.AGENT_MONITORING_OFFLINE}}" = 1 ] || exit 0' in script
    rules = [line.strip() for line in script.splitlines()]
    assert "type filter hook output priority 0; policy drop;" in rules
    accepted = [line for line in rules if line.endswith(" accept")]
    assert accepted == [
        'oifname "lo" accept',
        "ct state established,related accept",
        "udp dport { 53, 123 } accept",
        "tcp dport 53 accept",
    ]


def test_check_verifies_the_offline_drop() -> None:
    text = (STACK / "check.sh").read_text(encoding="utf-8")
    assert "if [ -d /mnt/agent-monitoring-archives ]; then" in text
    assert "nft list chain inet agent_monitoring_offline output | grep -q 'policy drop;'" in text
    assert 'step "offline: outbound drop missing"\n    exit 1' in text


def test_provision_takes_the_archives_from_the_mount_when_offline() -> None:
    text = (STACK / "provision.sh").read_text(encoding="utf-8")
    assert "[ -d /mnt/agent-monitoring-archives ] && offline=/mnt/agent-monitoring-archives" in text
    assert 'bash "$assets/test-stack/archive.sh" "$url" "$sum" "$cache" $offline' in text
    assert "curl -fsSL" not in text  # downloads only through archive.sh


# -- archive.sh and fetch.sh against a fake curl -------------------------------------------------------------------

FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
[ -z "$CURL_FAIL" ] || exit 22
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done
printf '%s' "$CURL_BODY" > "$out"
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def archive_env(tmp_path: Path, body: str = "content", fail: bool = False) -> dict[str, str]:
    bin_dir = tmp_path / "curl-bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CURL_LOG": str(tmp_path / "curl.log"),
        "CURL_BODY": body,
        "CURL_FAIL": "1" if fail else "",
    }


def archive(tmp_path: Path, *args: str, **env: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(STACK / "archive.sh"), *args],
        capture_output=True,
        text=True,
        env=archive_env(tmp_path, **env),  # type: ignore[arg-type]
        check=False,
    )


def curl_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "curl.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


URL = "https://example.org/releases/tool-1.0.tar.gz"


def test_archive_downloads_checks_and_keeps(tmp_path: Path) -> None:
    target = tmp_path / "cache"
    done = archive(tmp_path, URL, sha("content"), str(target))
    assert done.returncode == 0, done.stderr
    assert (target / "tool-1.0.tar.gz").read_text(encoding="utf-8") == "content"
    assert "downloading tool-1.0.tar.gz" in done.stderr
    assert archive(tmp_path, URL, sha("content"), str(target)).returncode == 0
    assert len(curl_calls(tmp_path)) == 1  # the second call keeps the checked file


def test_archive_refuses_a_download_with_another_digest(tmp_path: Path) -> None:
    done = archive(tmp_path, URL, sha("other"), str(tmp_path / "cache"))
    assert done.returncode == 1
    assert "SHA-256 mismatch for tool-1.0.tar.gz" in done.stderr
    assert list((tmp_path / "cache").iterdir()) == []
    failed = archive(tmp_path, URL, sha("content"), str(tmp_path / "cache"), fail=True)
    assert failed.returncode != 0


def test_archive_offline_copies_and_never_downloads(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool-1.0.tar.gz").write_text("content", encoding="utf-8")
    done = archive(tmp_path, URL, sha("content"), str(tmp_path / "cache"), str(source))
    assert done.returncode == 0, done.stderr
    assert "copying tool-1.0.tar.gz" in done.stderr
    assert (tmp_path / "cache" / "tool-1.0.tar.gz").read_text(encoding="utf-8") == "content"
    missing = archive(tmp_path, URL, sha("other"), str(tmp_path / "cache2"), str(source))
    assert missing.returncode == 1
    assert "missing in" in missing.stderr
    assert "run fetch.sh on the host" in missing.stderr
    assert curl_calls(tmp_path) == []


def test_archive_checks_its_arguments(tmp_path: Path) -> None:
    done = archive(tmp_path, URL, "not-a-digest", str(tmp_path))
    assert (done.returncode, "invalid SHA-256" in done.stderr) == (2, True)
    assert archive(tmp_path).returncode != 0


def test_fetch_puts_every_pinned_archive_into_the_directory(tmp_path: Path) -> None:
    stack = tmp_path / "stack"
    stack.mkdir()
    for name in ("fetch.sh", "archive.sh"):
        shutil.copy(STACK / name, stack / name)
    lines = [
        f"{item}_URL=https://example.org/{item.lower()}.tar.gz\n{item}_SHA256={sha('content')}"
        for item in ("PROMETHEUS", "NODE_EXPORTER", "GRAFANA")
    ]
    (stack / "versions.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    target = tmp_path / "archives"
    done = subprocess.run(
        ["bash", str(stack / "fetch.sh"), str(target)],
        capture_output=True,
        text=True,
        env=archive_env(tmp_path),
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert sorted(path.name for path in target.iterdir()) == [
        "grafana.tar.gz",
        "node_exporter.tar.gz",
        "prometheus.tar.gz",
    ]
    assert f"fetch: {target} holds the archives" in done.stderr
    assert subprocess.run(["bash", str(STACK / "fetch.sh")], capture_output=True, check=False).returncode != 0


def test_fetch_reads_the_pinned_versions() -> None:
    text = (STACK / "fetch.sh").read_text(encoding="utf-8")
    assert '. "$here/versions.env"' in text
    assert "for item in PROMETHEUS NODE_EXPORTER GRAFANA; do" in text
