from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.no_pg

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / "src" / "agent_cli" / "skills"
NOT_DONE = "A draft plus local tests is not done"


def test_contributing_states_pr_done_contract() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert NOT_DONE in text
    assert "Push the branch to this repository" in text
    assert "Do not open the pull request from a personal fork" in text
    assert "Four lane verdicts" in text
    assert "those four `approved` verdicts on this head" in text
    assert "do not substitute another vendor" in text
    assert "Empty, partial, timeout, or unavailable" in text
    assert "is not zero findings" in text
    assert "`skipped` and `cancelled` are not green" in text
    assert "agent allow --action pr-ready" in text
    assert "Do not mark ready if it denies" in text
    assert "it is not the leave-draft verdict" in text
    assert "A human merges" in text
    assert "session that authored the diff does not sit those reviews" in text


def test_design_locks_pr_done() -> None:
    text = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    section = text.split("### 19.6 Pull request done", 1)[1].split("## 20.", 1)[0]
    assert "Pull request done" in text
    assert NOT_DONE in text
    assert NOT_DONE in section
    assert "Opening a draft is not done" in text
    assert "only checks that a task is in `pushing` or `pr-review`" in section
    assert "When spine and pr-review are attached" in section
    assert "Without those skills, the target repository" in section
    assert "does not sit those PR reviews" in section
    assert "Empty, partial, timeout, or unavailable" in section
    assert "is not zero findings" in section
    assert "agent allow --action pr-ready" in section
    assert "`skipped` and `cancelled` are not green" in section
    assert "do not substitute another vendor" in section
    assert "Vendors are `grok`, then `codex`" in section
    assert "four lane verdicts" in section
    assert "those four `approved` verdicts on this head" in section


def test_pr_review_and_spine_point_at_pr_done() -> None:
    pr_review = (PACKAGED / "pr-review" / "SKILL.md").read_text(encoding="utf-8")
    spine = (PACKAGED / "spine" / "SKILL.md").read_text(encoding="utf-8")
    review_loop = (PACKAGED / "review-loop" / "SKILL.md").read_text(encoding="utf-8")
    assert NOT_DONE in pr_review
    assert "session that authored the diff does not" in pr_review
    assert "agent allow --action pr-ready" in pr_review
    assert "Do not\n  substitute another vendor" in pr_review or "Do not substitute another vendor" in pr_review
    assert "unavailable output is not zero findings" in pr_review
    assert "four lane verdicts on this head are approved" in pr_review
    assert "those four `approved` verdicts on this head" in pr_review
    assert NOT_DONE in spine
    assert "not pull-request done" in spine
    assert "not the pull-request review" in review_loop
    assert "Inner implement/review rounds (`review-loop`) are not the PR reviews" in (
        ROOT / "CONTRIBUTING.md"
    ).read_text(encoding="utf-8")


def test_agents_md_and_readme_point_at_contributing() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    stub = (ROOT / "skills" / "session-store" / "SKILL.md").read_text(encoding="utf-8")
    packaged = (PACKAGED / "session-store" / "SKILL.md").read_text(encoding="utf-8")
    assert NOT_DONE in agents
    assert "CONTRIBUTING.md" in agents
    assert "A draft plus local tests is not a finished pull request" in readme
    assert NOT_DONE in stub
    assert "for this repository" in stub
    assert "store encoding when that skill is" in stub
    assert NOT_DONE in packaged
    assert "for this repository" in packaged
    assert "store encoding when that skill is" in packaged
