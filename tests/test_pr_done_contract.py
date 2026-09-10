from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.no_pg

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / "src" / "agent_cli" / "skills"
NOT_DONE = "A draft plus local tests is not done"


def _ws(text: str) -> str:
    """Collapse whitespace so prose line-wrapping is not a contract failure."""
    return " ".join(text.split())


def test_contributing_states_ready_for_review_contract() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    prose = _ws(text)
    assert NOT_DONE in text
    assert "Push the branch to this repository" in text
    assert "Do not open the pull request from a personal fork" in text
    assert "Four lane verdicts" in text
    assert "those four `approved` verdicts on this head" in text
    assert "do not substitute another vendor" in prose
    assert "Empty, partial, timeout, or unavailable" in text
    assert "is not zero findings" in text
    assert "`skipped` and `cancelled` are not green" in text
    assert "skipped` or `neutral` as green" in text
    assert "`cancelled` and failed still block" in text
    assert "agent allow --action pr-ready" in text
    assert "Do not mark ready if it denies" in text
    assert "it is not the leave-draft verdict" in text
    assert "A human merges" in text
    assert "session that authored the diff does not sit those reviews" in prose
    assert "Ready for review is still not merge and not completion" in prose
    assert "finished, done, or completed at that point" in prose


def test_design_locks_ready_for_review() -> None:
    text = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    section = text.split("### 19.6 Ready for review", 1)[1].split("## 20.", 1)[0]
    prose = _ws(text)
    section_prose = _ws(section)
    assert "Ready for review" in text
    assert NOT_DONE in text
    assert NOT_DONE in section
    assert "Opening a draft is not done" in text
    assert "Leaving draft is Ready for review, not completion" in prose
    assert "only checks that a task is in `pushing` or `pr-review`" in section_prose
    assert "When spine and pr-review are attached" in section_prose
    assert "Without those skills, the target repository" in section_prose
    assert "does not sit those PR reviews" in section_prose
    assert "Empty, partial, timeout, or unavailable" in section_prose
    assert "is not zero findings" in section_prose
    assert "agent allow --action pr-ready" in section_prose
    assert "`skipped` and `cancelled` are not green" in section_prose
    assert "skipped` or `neutral` as green" in section_prose
    assert "`cancelled` and failed still block" in section_prose
    assert "do not substitute another vendor" in section_prose
    assert "Vendors are `grok`, then `codex`" in section_prose
    assert "four lane verdicts" in section_prose
    assert "those four `approved` verdicts on this head" in section_prose
    assert "Ready for review is still not merge and not completion" in section_prose
    assert "ledger `task-done` is not pull-request completion" in section_prose


def test_pr_review_and_spine_point_at_ready_for_review() -> None:
    pr_review = (PACKAGED / "pr-review" / "SKILL.md").read_text(encoding="utf-8")
    spine = (PACKAGED / "spine" / "SKILL.md").read_text(encoding="utf-8")
    review_loop = (PACKAGED / "review-loop" / "SKILL.md").read_text(encoding="utf-8")
    pr_review_prose = _ws(pr_review)
    spine_prose = _ws(spine)
    assert NOT_DONE in pr_review
    assert "session that authored the diff does not" in pr_review_prose
    assert "agent allow --action pr-ready" in pr_review
    assert "Do not substitute another vendor" in pr_review_prose
    assert "unavailable output is not zero findings" in pr_review_prose
    assert "four lane verdicts on this head are approved" in pr_review_prose
    assert "those four `approved` verdicts on this head" in pr_review_prose
    assert "not merge and not pull-request completion" in pr_review_prose
    assert NOT_DONE in spine_prose
    assert "not Ready for review" in spine_prose
    assert "not pull-request completion" in spine_prose
    assert "not the pull-request review" in review_loop
    assert "Inner implement/review rounds (`review-loop`) are not the PR reviews" in (
        ROOT / "CONTRIBUTING.md"
    ).read_text(encoding="utf-8")


def test_agents_md_and_readme_point_at_contributing() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    stub = (ROOT / "skills" / "session-store" / "SKILL.md").read_text(encoding="utf-8")
    packaged = (PACKAGED / "session-store" / "SKILL.md").read_text(encoding="utf-8")
    lifecycle = (ROOT / "docs" / "pull-request-lifecycle.md").read_text(encoding="utf-8")
    agents_prose = _ws(agents)
    lifecycle_prose = _ws(lifecycle)
    assert NOT_DONE in agents
    assert "CONTRIBUTING.md" in agents
    assert "pull-request-lifecycle.md" in agents
    assert "Ready for review is still not merge and not completion" in agents_prose
    assert "Never finished, done, or completed" in lifecycle_prose
    assert "Still not merged and still not completed" in lifecycle_prose
    assert "Leaving draft is **Ready for review**, not pull-request completion" in lifecycle_prose
    assert "A draft plus local tests is not a finished pull request" in readme
    assert NOT_DONE in stub
    assert "for this repository" in stub
    assert "store encoding when that skill is" in stub
    assert NOT_DONE in packaged
    assert "for this repository" in packaged
    assert "store encoding when that skill is" in packaged
