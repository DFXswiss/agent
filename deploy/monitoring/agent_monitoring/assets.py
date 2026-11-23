"""The asset files and the device placeholder `<runner-device>`.

This repository is public and names no machine: the alert file and the inventory tile carry the placeholder, and
`render_devices` replaces it with the runner devices of an installation (the infrastructure patch, the test stack).
In the alert file the placeholder appears in the selector of the absence rule (`server=~"<runner-device>"`, becomes
`server=~"a|b"`) and in one zero-fallback line, which is repeated once per device (the per-device zero fallback of
the infrastructure's absence rules).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).resolve().parents[1]
PLACEHOLDER = "<runner-device>"
ALERT_FILE = ASSETS / "alerting" / "agent-platform.yaml"
DASHBOARD_DIR = ASSETS / "dashboards"
DASHBOARD_FOLDER = "Agent platform"
DASHBOARD_UIDS = ("agent-platform-devices", "agent-platform-vms", "agent-platform-sessions")
ALERT_FOLDER = "Infrastructure"
RULE_UIDS = (
    "agent-runner-textfile-absent",
    "agent-runner-textfile-stale",
    "agent-runner-capacity-low",
    "agent-runner-vm-probe-failed",
    "agent-runner-session-oom",
    "agent-runner-sync-fatal",
    "agent-runner-hub-disconnected",
)
DEVICE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
METRIC_RE = re.compile(r"\bagent_runner_[a-z0-9_]+\b")
SELECTOR = f'server=~"{PLACEHOLDER}"'


def check_devices(devices: list[str]) -> list[str]:
    """The device names, refused when empty, duplicated or not a plain host label (they become label values)."""
    if not devices:
        raise ValueError("at least one runner device is required")
    for name in devices:
        if not DEVICE_RE.fullmatch(name):
            raise ValueError(f"invalid device name {name!r}: lower-case letters, digits and '-' only")
    if len(set(devices)) != len(devices):
        raise ValueError("duplicate device name")
    return devices


def render_devices(text: str, devices: list[str]) -> str:
    """`text` with the selector set to all devices and every line that holds the placeholder repeated per device."""
    check_devices(devices)
    text = text.replace(SELECTOR, 'server=~"' + "|".join(devices) + '"')
    lines = []
    for line in text.splitlines(keepends=True):
        if PLACEHOLDER in line:
            lines += [line.replace(PLACEHOLDER, name) for name in devices]
        else:
            lines.append(line)
    return "".join(lines)


def dashboard(uid: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DASHBOARD_DIR / f"{uid}.json").read_text(encoding="utf-8"))
    return data


def dashboard_queries(data: dict[str, Any]) -> list[str]:
    """Every PromQL text of a dashboard: panel targets and query variables."""
    queries = [target["expr"] for panel in data["panels"] for target in panel.get("targets", [])]
    queries += [item["query"] for item in data["templating"]["list"] if item["type"] == "query"]
    return queries


def alert_queries(doc: dict[str, Any]) -> list[str]:
    """The PromQL text of every rule of a parsed alert file (the queries on the Prometheus data source)."""
    return [
        item["model"]["expr"]
        for group in doc["groups"]
        for rule in group["rules"]
        for item in rule["data"]
        if item["datasourceUid"] == "prometheus"
    ]


def metric_names(query: str) -> set[str]:
    return set(METRIC_RE.findall(query))
