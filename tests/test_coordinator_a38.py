from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.a38_review import COMPLIANCE_PROMPT, validate_declaration, validate_review_record, validate_visible
from agent_cli.coordinator_a38 import (
    _render_review_comment,
    _run_cli,
    _second_review_request_note,
    validate_second_review_payload,
)
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_config import WorkerConfig
from agent_cli.coordinator_github import phase_formal_approve, phase_leave_draft
from agent_cli.runtime import Completed

pytestmark = pytest.mark.no_pg

HEAD = "a" * 40
TASK_ID = "11111111-1111-4111-8111-111111111111"


def second_payload(*, defects: int = 0, blocks_merge: bool = False) -> dict:
    return {
        "kind": "a38.second-review/v1",
        "task_id": TASK_ID,
        "head": HEAD,
        "defects": defects,
        "blocks_merge": blocks_merge,
        "findings": "A bounded finding." if defects else "",
        "lanes": [
            {
                "id": "conformity-b",
                "provider": "OpenAI",
                "model": "Reviewer Model 2",
                "model_number": "reviewer-model-2",
                "prompt": COMPLIANCE_PROMPT,
                "status": "complete",
                "result": "pass",
            },
            {
                "id": "logic-b",
                "provider": "OpenAI",
                "model": "Reviewer Model 2",
                "model_number": "reviewer-model-2",
                "prompt": COMPLIANCE_PROMPT,
                "status": "complete",
                "result": "pass",
            },
        ],
    }


def a38_worker(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        session_id="worker",
        review_session=None,
        workspace_root=tmp_path,
        repositories={},
        reply_logins=("human",),
        poll_seconds=30,
        lane_timeout=1800,
        check_timeout=600,
        a38=True,
        second_reviewers=("reviewer",),
    )


def test_second_review_schema_and_rendered_comment_pass_guard_validators() -> None:
    payload = validate_second_review_payload(second_payload())
    declaration = {
        "schema": "a38-review/v2",
        "head": HEAD,
        "passes": 1,
        "defects": 0,
        "set_aside": "none",
        "blocks_merge": False,
        "lanes": payload["lanes"],
    }
    task = {"change_summary_en": "The change is complete.", "change_summary_de": "Die Änderung ist vollständig."}
    body = _render_review_comment(task, declaration, rules_bound=True, reviewer="reviewer")
    assert validate_declaration(declaration, head=HEAD, markdown_only=False, rules_bound=True) == []
    assert validate_visible(body, 1) == []
    assert validate_review_record(body, declaration, rules_bound=True) == []
    assert "Second review by @reviewer." in body
    assert len(body) < 65_536


def test_review_comment_refuses_github_oversize_body() -> None:
    prompt = COMPLIANCE_PROMPT + " " + "x" * 17_000
    declaration = {
        "schema": "a38-review/v2",
        "head": HEAD,
        "passes": 1,
        "defects": 0,
        "set_aside": "none",
        "lanes": [
            {
                "id": lane_id,
                "provider": "OpenAI",
                "model": "Reviewer Model",
                "model_number": "reviewer-model",
                "prompt": prompt,
                "status": "complete",
                "result": "pass",
            }
            for lane_id in ("conformity-a", "logic-a")
        ],
    }
    task = {
        "change_summary_en": "The change is complete.",
        "change_summary_de": "Die Änderung ist vollständig.",
    }

    with pytest.raises(CoordinatorError, match="shorter than 65536 characters"):
        _render_review_comment(task, declaration, rules_bound=True)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda p: p.update({"blocks_merge": True}), "blocking defects"),
        (lambda p: p.update({"unknown": True}), "documented keys"),
        (lambda p: p["lanes"][1].update({"model": "Other model"}), "same model"),
        (
            lambda p: p.update({"defects": 0, "blocks_merge": False, "findings": "A real defect."}),
            "findings require defects >= 1",
        ),
    ],
)
def test_second_review_schema_refuses_invalid_results(mutate, reason) -> None:
    payload = second_payload()
    mutate(payload)
    with pytest.raises(CoordinatorError, match=reason):
        validate_second_review_payload(payload)


def test_a38_worker_refuses_formal_approve_and_leave_draft(tmp_path: Path) -> None:
    worker = a38_worker(tmp_path)
    with pytest.raises(CoordinatorError, match="never posts a formal APPROVE"):
        phase_formal_approve(None, worker, {}, None)  # type: ignore[arg-type]
    with pytest.raises(CoordinatorError, match="never leaves draft"):
        phase_leave_draft(None, worker, {}, None)  # type: ignore[arg-type]


def test_a38_cli_uses_worker_check_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv
        captured.update(kwargs)
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.coordinator_a38.run_bounded", fake_run)
    worker = a38_worker(tmp_path)
    result = _run_cli(["policy", "--file", "policy.json"], worker=worker, cwd=tmp_path)
    assert result.returncode == 0
    assert captured["timeout"] == worker.check_timeout


def test_second_review_request_note_is_bounded_for_long_repo_and_session() -> None:
    repo = "o/" + "r" * 98
    session_id = "s" * 64
    url = f"https://github.com/{repo}/pull/123"
    note = _second_review_request_note(url=url, session_id=session_id)
    assert len(repo) == 100
    assert len(session_id) == 64
    assert len(note) <= 1000
    assert url in note
    assert session_id in note
    assert "docs/issue-coordinator.md" in note

    oversized_url = url + "?context=" + "x" * 1200
    truncated = _second_review_request_note(url=oversized_url, session_id=session_id)
    assert len(truncated) == 1000
    assert oversized_url not in truncated
    assert "…" in truncated
    assert session_id in truncated
    assert "docs/issue-coordinator.md" in truncated
