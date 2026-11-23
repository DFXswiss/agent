"""Fixtures of the monitoring assets (MON-01): `deploy/monitoring/agent_monitoring` is imported from the checkout (the
assets are files of the repository, not an installed package), and a synthetic infrastructure repository with the
shape the prepared patch expects (one monitoring stack, the absence rule with its per-host zero fallback, the
inventory, host folders with `node_exporter.args`)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MONITORING = REPO / "deploy" / "monitoring"

if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

STACK = "infrastructure/hub-a/monitoring-stack"
ABSENT_RULE = """apiVersion: 1
groups:
  - orgId: 1
    name: fleet-host-memory
    folder: Infrastructure
    interval: 1m
    rules:
      - uid: fleet-host-memory-low
        title: low
        condition: C
        data: []
      - uid: fleet-host-memory-absent
        title: Mac host memory samples missing
        condition: C
        data:
          - refId: A
            model:
              expr: >-
                sum by (server) (
                  count_over_time(node_memory_total_bytes{server=~"host-a|runner-a"}[10m])
                )
                or label_replace(vector(0), "server", "host-a", "", "")
                or label_replace(vector(0), "server", "runner-a", "", "")
              instant: true
      - uid: fleet-host-memory-other
        title: other
        condition: C
        data: []
"""
INVENTORY = """# inventory
server_display_order = ["host-a", "runner-a"]

[[servers]]
key = "host-a"
name = "host-a"

[[servers]]
key = "runner-a"
name = "runner-a"
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def infra(tmp_path: Path) -> Path:
    """A committed checkout of the synthetic infrastructure repository."""
    root = tmp_path / "infra"
    write(root, f"{STACK}/dashboard-inventory.toml", INVENTORY)
    write(root, f"{STACK}/grafana/provisioning/alerting/fleet-host-memory.yaml", ABSENT_RULE)
    write(root, f"{STACK}/grafana/provisioning/dashboards/json/overview.json", "{}\n")
    for host in ("runner-a", "runner-b"):
        write(
            root, f"infrastructure/{host}/node_exporter.args", f"--collector.textfile.directory=/opt/{host}/textfile\n"
        )
    write(root, "infrastructure/runner-a/README.md", "# runner-a\n\nHost notes.\n")
    git("init", "-q", "-b", "develop", cwd=root)
    git("add", "-A", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q",
        "-m", "Base.", cwd=root)  # fmt: skip
    return root
