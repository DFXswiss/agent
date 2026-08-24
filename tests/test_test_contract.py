from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.main import packaged_skills_dir
from agent_cli.skills import SKILL_NAMES

pytestmark = pytest.mark.no_pg

CONTRACT = "Tests exist to find and document product defects"


def test_no_fifth_skill_name() -> None:
    assert SKILL_NAMES == ("spine", "review-loop", "pr-review", "error-fix")
    assert not (packaged_skills_dir() / "test" / "SKILL.md").exists()


def test_test_contract_is_locked_in_skills_and_contributing() -> None:
    root = Path(__file__).resolve().parents[1]
    packaged = packaged_skills_dir()
    files = [
        packaged / "spine" / "SKILL.md",
        packaged / "review-loop" / "SKILL.md",
        packaged / "pr-review" / "SKILL.md",
        root / "CONTRIBUTING.md",
        root / "DESIGN.md",
        root / "README.md",
    ]
    missing = [str(path) for path in files if CONTRACT not in path.read_text(encoding="utf-8")]
    assert missing == []
    contributing = (root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "BUGS.md" in contributing
    assert "expected-fail" in contributing
    assert "not found" in contributing
