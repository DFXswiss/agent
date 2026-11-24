"""U-HUB/U-RUN coverage gate: the platform coverage gate is exactly the one of docs/platform/spec/07-testing.md
section 3 (hub, runner, guest, doubles and the agent tool in one configuration since the repositories were unified,
INF-06), and the legacy `[tool.coverage]` of pyproject.toml is not the one the platform run uses."""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "run": {
        "source": [
            "agent_hub",
            "agent_runner",
            "agent_guest",
            "agent_doubles",
            "agent_platform",
            "scripts/probes/probelib",
            "deploy/monitoring/agent_monitoring",
        ],
        "branch": ["true"],
        "parallel": ["true"],
        "patch": ["subprocess"],
    },
    "paths": {"source": ["src/", "/opt/agent-guest/src/", "*/agent/src/"]},
    "report": {
        "fail_under": ["100"],
        "show_missing": ["true"],
        "skip_covered": ["true"],
        "exclude_also": ['if __name__ == "__main__":'],
    },
}


def test_coverage_configuration_is_pinned() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(REPO_ROOT / ".coveragerc-platform")
    found = {
        section: {
            key: [line.strip() for line in value.splitlines() if line.strip()] for key, value in parser[section].items()
        }
        for section in parser.sections()
    }
    assert found == EXPECTED


def test_legacy_coverage_configuration_is_unchanged() -> None:
    """`[tool.coverage]` stays the legacy configuration of agent_cli; the hub gate moved to .coveragerc-platform."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert data["tool"]["coverage"]["run"]["source"] == ["agent_cli"]
    # the floor belongs to the legacy suite and only moves up (CONTRIBUTING.md); it is never the platform gate
    assert data["tool"]["coverage"]["report"]["fail_under"] < 100
