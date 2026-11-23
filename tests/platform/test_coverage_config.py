"""The coverage gate of agent_hub is exactly the one of docs/platform/spec/07-testing.md section 3."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "run": {"source": ["agent_hub"], "branch": True},
    "report": {
        "fail_under": 100,
        "show_missing": True,
        "skip_covered": True,
        "exclude_also": ['if __name__ == "__main__":'],
    },
}


def test_coverage_configuration_is_pinned() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert data["tool"]["coverage"] == EXPECTED
