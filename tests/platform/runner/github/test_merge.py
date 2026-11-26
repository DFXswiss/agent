"""U-RUN-github (KOR-51, ADR-093 sections 4-6): review verdicts and security reports, the reviews and checks of a pull
request, and `github.merge` with the workgroup's formal criteria and separation of duties, through the operation
pipeline against the GitHub double. Without criteria only GitHub's own rules decide (E-11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from agent_doubles.github.repos import CheckRun, CommitStatus, Review
from agent_runner.github.executors import EXECUTORS as GITHUB_EXECUTORS
from agent_runner.github.merge import checks_state
from agent_runner.github.op_prechecks import PRECHECKS as GITHUB_PRECHECKS
from agent_runner.operations import records
from agent_runner.orgs.db import session as store_session
from agent_runner.reviews.executors import EXECUTORS as REVIEW_EXECUTORS
from agent_runner.reviews.op_prechecks import PRECHECKS as REVIEW_PRECHECKS

from ..sessions.kit import WG
from . import run10_kit
from .conftest import REPO
from .run10_kit import R10, refused, run10

REVIEW = {"name": "review", "operations": ["review.verdict", "github.list_reviews"], "reads": "all"}
SUMMARY = "The change is correct."
READER_OPS = ["github.list_reviews", "github.get_checks", "security.report"]


def repo(github: Any) -> Any:
    return github.double.world.repo("octo-org", "hello")


def feature(github: Any) -> str:
    return str(repo(github).refs["refs/heads/feature"])


def with_reviews(monkeypatch: Any) -> None:
    monkeypatch.setattr(run10_kit, "EXECUTORS", {**GITHUB_EXECUTORS, **REVIEW_EXECUTORS})
    monkeypatch.setattr(run10_kit, "PRECHECKS", {**GITHUB_PRECHECKS, **REVIEW_PRECHECKS})


async def profiles(r: R10, sessions: dict[str, Any]) -> None:
    async with store_session(r.ok.env.pool) as conn:
        await conn.execute(
            "INSERT INTO runner.profile_state (id, section, received_at) VALUES (1, %s, now())"
            " ON CONFLICT (id) DO UPDATE SET section = EXCLUDED.section",
            (Jsonb({"api": 1, "sessions": sessions, "workgroups": {WG: {}}}),),
        )


async def pushed(r: R10, op: records.Operation, sha: str) -> None:
    """Turn a succeeded operation of a session into its `github.push` of `sha` (what the session wrote)."""
    async with store_session(r.ok.env.pool) as conn:
        await conn.execute(
            "UPDATE runner.operation SET type = 'github.push', params = %s WHERE id = %s",
            (Jsonb({"repo": REPO, "branch": "feature", "commit_sha": sha}), op.id),
        )


def verdict(head: str, outcome: str = "passed") -> dict[str, Any]:
    return {"repo": REPO, "number": 1, "head_sha": head, "outcome": outcome, "summary": SUMMARY}


def merge(head: str) -> dict[str, Any]:
    return {"repo": REPO, "number": 1, "head_sha": head}


def test_verdicts_reviews_and_checks(store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: Any) -> None:
    with_reviews(monkeypatch)
    github = start_github()
    head = feature(github)
    hello = repo(github)
    hello.reviews.append(Review(901, 1, "other-user", "APPROVED", head, "Fine.", 1_800_000_000))
    hello.check_runs.append(CheckRun(902, "ci", head, "completed", "success"))
    hello.statuses.append(CommitStatus(903, head, "lint", "pending"))

    async def body(r: R10) -> None:
        reviewer, reader = await r.session(1), await r.session(2)
        await profiles(r, {reviewer: REVIEW})
        evidence = [{"kind": "note", "ref": "read the diff"}, {"kind": "test_run", "ref": "tr-0123456789ab"}]
        op = await r.run(reviewer, "review.verdict", {**verdict(head), "evidence": evidence})
        assert op.state == "succeeded", op.answer
        assert op.result == {
            "verdict_id": op.id,
            "repo": REPO,
            "number": 1,
            "head_sha": head,
            "outcome": "passed",
            "profile": "review",
        }
        listed = await r.run(reader, "github.list_reviews", {"repo": REPO, "number": 1})
        assert listed.result is not None, listed.answer
        assert listed.result["head_sha"] == head
        assert [(item["author"], item["state"]) for item in listed.result["reviews"]] == [("other-user", "APPROVED")]
        assert [(item["verdict_id"], item["profile"]) for item in listed.result["verdicts"]] == [(op.id, "review")]
        await profiles(
            r, {reviewer: REVIEW, reader: {**REVIEW, "name": "own", "reads": "task", "operations": READER_OPS}}
        )
        listed = await r.run(reader, "github.list_reviews", {"repo": REPO, "number": 1})
        assert listed.result is not None, listed.answer
        assert len(listed.result["verdicts"]) == 1  # the same task
        checks = await r.run(reader, "github.get_checks", {"repo": REPO, "ref": "feature"})
        assert checks.result is not None
        assert (checks.result["sha"], checks.result["state"]) == (head, "pending")
        report = await r.run(reader, "security.report", {**REPORT, "run_id": "tr-0123456789ab"})
        assert refused(report) == "not_found"
        report = await r.run(reader, "security.report", REPORT)
        assert report.result is not None
        assert report.result["report_id"] == report.id
        async with store_session(r.ok.env.pool) as conn:
            await conn.execute(
                "UPDATE runner.operation SET type = 'test.run', result = %s WHERE id = %s",
                (Jsonb({"run_id": "tr-0123456789ab"}), checks.id),
            )
        scanned = await r.run(reader, "security.report", {**REPORT, "run_id": "tr-0123456789ab"}, key="scan")
        assert scanned.state == "succeeded", scanned.answer

    run10(store_dsn, github, tmp_path, body)


REPORT = {
    "repo": REPO,
    "ref": "feature",
    "tool": "semgrep",
    "passed": True,
    "findings": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
}


def test_without_criteria_github_decides(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github()
    head = feature(github)

    async def body(r: R10) -> None:
        await r.ok.settings([REPO], protected=[])
        sid = await r.session()
        outdated = await r.run(sid, "github.merge", merge("0" * 40))
        assert refused(outdated) == "merge_outdated"
        op = await r.run(sid, "github.merge", {**merge(head), "method": "squash"})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        assert op.result["merged"] is True
        pull = repo(github).pulls[1]
        assert (pull.state, pull.merged_sha) == ("closed", op.result["sha"])
        again = await r.run(sid, "github.merge", merge(head), key="again")
        assert refused(again) == "merge_unsupported"

    run10(store_dsn, github, tmp_path, body)


def test_criteria_and_separate_duties(store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: Any) -> None:
    with_reviews(monkeypatch)
    github = start_github()
    head = feature(github)
    criteria = {"passed_verdicts": 1, "verdict_profile": "review", "checks_green": True, "github_approvals": 1}

    async def body(r: R10) -> None:
        await r.ok.settings([REPO], protected=[], separate_duties=True, merge_criteria=criteria)
        writer, reviewer, merger = await r.session(1), await r.session(2), await r.session(3)
        await profiles(r, {reviewer: REVIEW})
        wrote = await r.run(writer, "github.get_checks", {"repo": REPO, "ref": "feature"})
        await pushed(r, wrote, head)
        own = await r.run(writer, "review.verdict", verdict(head))
        assert refused(own) == "rights_denied"
        assert "separate duties" in own.answer["error"]["message"]  # type: ignore[index]
        mine = await r.run(writer, "github.merge", merge(head))
        assert refused(mine) == "rights_denied"
        missing = await r.run(merger, "github.merge", merge(head))
        assert refused(missing) == "rights_denied"
        assert "1 passed verdict(s)" in missing.answer["error"]["message"]  # type: ignore[index]
        assert (await r.run(reviewer, "review.verdict", verdict(head))).state == "succeeded"
        waiting = await r.run(merger, "github.merge", merge(head), key="checks")
        assert "checks are none" in waiting.answer["error"]["message"]  # type: ignore[index]
        repo(github).check_runs.append(CheckRun(902, "ci", head, "completed", "success"))
        approvals = await r.run(merger, "github.merge", merge(head), key="approvals")
        assert "approving GitHub review" in approvals.answer["error"]["message"]  # type: ignore[index]
        repo(github).reviews.append(Review(901, 1, "other-user", "APPROVED", head, "", 1_800_000_000))
        op = await r.run(merger, "github.merge", merge(head), key="ok")
        assert op.state == "succeeded", op.answer

    run10(store_dsn, github, tmp_path, body)


def test_a_rejection_blocks_and_github_refuses_a_conflict(
    store_dsn: str, start_github: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    with_reviews(monkeypatch)
    github = start_github()
    head = feature(github)

    async def body(r: R10) -> None:
        await r.ok.settings([REPO], protected=[], merge_criteria={"passed_verdicts": 1})
        reviewer, other, merger = await r.session(1), await r.session(2), await r.session(3)
        await profiles(r, {reviewer: REVIEW, other: REVIEW})
        await r.run(reviewer, "review.verdict", verdict(head))
        await r.run(other, "review.verdict", verdict(head, "rejected"))
        blocked = await r.run(merger, "github.merge", merge(head))
        assert "rejected this head" in blocked.answer["error"]["message"]  # type: ignore[index]
        await r.ok.settings([REPO], protected=[])
        github.double.world.add_commit({"repo": REPO, "branch": "main", "commit": {"files": {"src/app.py": "x = 9\n"}}})
        conflict = await r.run(merger, "github.merge", merge(head), key="conflict")
        assert conflict.state == "failed"
        assert conflict.answer["error"]["code"] == "rights_denied"  # type: ignore[index]

    run10(store_dsn, github, tmp_path, body)


def test_the_state_of_checks() -> None:
    run = {"name": "ci", "status": "completed", "conclusion": "success"}
    assert checks_state([], []) == "none"
    assert checks_state([run], []) == "success"
    assert checks_state([{**run, "conclusion": "skipped"}], [{"context": "x", "state": "success"}]) == "success"
    assert checks_state([{**run, "status": "in_progress", "conclusion": None}], []) == "pending"
    assert checks_state([], [{"context": "x", "state": "pending"}]) == "pending"
    assert checks_state([{**run, "conclusion": "failure"}], []) == "failure"
    assert checks_state([], [{"context": "x", "state": "error"}]) == "failure"
