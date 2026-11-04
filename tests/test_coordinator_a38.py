from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.a38_review import COMPLIANCE_PROMPT, validate_declaration, validate_review_record, validate_visible
from agent_cli.coordinator_a38 import (
    _render_review_comment,
    validate_second_review_payload,
)
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_config import WorkerConfig
from agent_cli.coordinator_github import phase_formal_approve, phase_leave_draft

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


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda p: p.update({"blocks_merge": True}), "blocking defects"),
        (lambda p: p.update({"unknown": True}), "documented keys"),
        (lambda p: p["lanes"][1].update({"model": "Other model"}), "same model"),
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
