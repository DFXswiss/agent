"""Pure allow-gate logic. No Postgres."""

from __future__ import annotations

import unittest

from agent_cli.allow import (
    CHECKLIST_KEYS,
    JA_EVIDENCE_REQUIRED,
    SECOND_REVIEW_NOT_POSTED,
    evaluate_allow,
    ready_for_done_blocking,
    requires_evidence,
)
from agent_cli.model_slots import ModelSlotsError


def _approved_gates(head: str = "abc123") -> list[dict]:
    return [
        {
            "stage": stage,
            "dimension": dim,
            "vendor": vendor,
            "verdict": "approved",
            "head_sha": head,
        }
        for stage, dim, vendor in (
            ("review-1", "quality", "grok"),
            ("review-1", "logic", "grok"),
            ("review-2", "quality", "codex"),
            ("review-2", "logic", "codex"),
        )
    ]


def _ready_task(
    *,
    tid: int = 1,
    session_id: str = "sess-a",
    workflow: str = "implement",
    state: str = "pr-review",
) -> dict:
    keys = CHECKLIST_KEYS[workflow]
    checklist = {k: "ja" for k in keys}
    # n_a keys may be ja; deviation is often n_a
    if "deviation_declared" in checklist:
        checklist["deviation_declared"] = "n_a"
    if "deviation_granted" in checklist:
        checklist["deviation_granted"] = "n_a"
    local_checks: list[dict] = []
    if "local_check_pass" in keys:
        local_checks = [{"name": "lint", "result": "pass"}]
    return {
        "id": tid,
        "session_id": session_id,
        "workflow": workflow,
        "state": state,
        "checklist": checklist,
        "summaries": {"en": "Adds done-gate.", "de": "Fügt Done-Gate hinzu."},
        "gates": _approved_gates(),
        "local_checks": local_checks,
    }


class TestEvaluateAllow(unittest.TestCase):
    def test_merged_is_terminal_for_claim_done(self) -> None:
        task = _ready_task(state="merged")
        task["checklist"]["local_check_pass"] = "pending"
        result = evaluate_allow(
            "claim-done",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertTrue(result.allowed)

    def test_claim_done_without_tasks_allows(self) -> None:
        r = evaluate_allow(
            "claim-done",
            session_id="sess-a",
            task_id=None,
            session_tasks=[],
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.reason, "allow")

    def test_claim_done_open_task_pending_denies(self) -> None:
        task = _ready_task()
        task["checklist"]["pushed"] = "pending"
        r = evaluate_allow(
            "claim-done",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertFalse(r.allowed)
        self.assertTrue(any("pushed=pending" in b for b in r.blocking))

    def test_claim_done_ready_task_allows(self) -> None:
        task = _ready_task()
        self.assertEqual(ready_for_done_blocking(task), [])
        r = evaluate_allow(
            "claim-done",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertTrue(r.allowed)

    def test_claim_done_rejects_an_implementer_pinned_to_models_second(self) -> None:
        task = _ready_task()
        task["models"] = {"first": "claude", "second": "codex"}
        task["implementer"] = "codex"
        with self.assertRaisesRegex(
            ModelSlotsError, "implementer must differ from models.second"
        ):
            evaluate_allow(
                "claim-done",
                session_id="sess-a",
                task_id=None,
                session_tasks=[task],
            )

    def test_claim_done_ignores_other_session(self) -> None:
        foreign = _ready_task(tid=9, session_id="other", state="implementing")
        foreign["checklist"]["pushed"] = "pending"
        r = evaluate_allow(
            "claim-done",
            session_id="sess-a",
            task_id=None,
            session_tasks=[foreign],
        )
        self.assertTrue(r.allowed)

    def test_pr_ready_without_task_denies(self) -> None:
        r = evaluate_allow(
            "pr-ready",
            session_id="sess-a",
            task_id=None,
            session_tasks=[],
        )
        self.assertFalse(r.allowed)
        self.assertIn("agent task", r.reason)

    def test_pr_ready_implementing_denies(self) -> None:
        task = _ready_task(state="implementing")
        r = evaluate_allow(
            "pr-ready",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertFalse(r.allowed)

    def test_pr_ready_pr_review_allows(self) -> None:
        task = _ready_task(state="pr-review")
        r = evaluate_allow(
            "pr-ready",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertTrue(r.allowed)

    def test_pr_create_without_draft_denies(self) -> None:
        r = evaluate_allow(
            "pr-create",
            session_id=None,
            task_id=None,
            session_tasks=[],
            create_has_draft=False,
        )
        self.assertFalse(r.allowed)
        self.assertIn("--draft", r.reason)

    def test_pr_create_with_draft_allows(self) -> None:
        r = evaluate_allow(
            "pr-create",
            session_id=None,
            task_id=None,
            session_tasks=[],
            create_has_draft=True,
        )
        self.assertTrue(r.allowed)

    def test_task_done_without_ready_denies(self) -> None:
        task = _ready_task()
        task["checklist"]["local_check_pass"] = "pending"
        r = evaluate_allow(
            "task-done",
            session_id="sess-a",
            task_id="1",
            session_tasks=[task],
        )
        self.assertFalse(r.allowed)
        self.assertTrue(any("local_check_pass" in b for b in r.blocking))

    def test_ja_requires_evidence_for_every_key(self) -> None:
        for key in JA_EVIDENCE_REQUIRED:
            self.assertTrue(
                requires_evidence("ja", key),
                msg=f"ja needs evidence for {key}",
            )
        # and generically for any key
        self.assertTrue(requires_evidence("ja", "any_key"))

    def test_claim_done_ignores_superseded_like_done(self) -> None:
        task = _ready_task(state="superseded")
        task["checklist"]["pushed"] = "pending"
        r = evaluate_allow(
            "claim-done",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertTrue(r.allowed)

    def test_pr_ready_gate_blocked_denies(self) -> None:
        task = _ready_task(state="gate-blocked")
        r = evaluate_allow(
            "pr-ready",
            session_id="sess-a",
            task_id=None,
            session_tasks=[task],
        )
        self.assertFalse(r.allowed)

    def test_task_done_unavailable_gate_key_blocks(self) -> None:
        task = _ready_task()
        task["checklist"]["review_1_quality"] = "unavailable"
        r = evaluate_allow(
            "task-done",
            session_id="sess-a",
            task_id="1",
            session_tasks=[task],
        )
        self.assertFalse(r.allowed)
        self.assertTrue(any("review_1_quality=unavailable" in b for b in r.blocking))

    def test_both_codex_n_a_skips_codex_ready_blockers(self) -> None:
        task = _ready_task()
        task["checklist"]["review_2_quality"] = "n_a"
        task["checklist"]["review_2_logic"] = "n_a"
        task["pull_opener"] = "alice"
        task["trusted_pr_guard"] = {
            "schema": "pr-guard/v1",
            "a38": {"enforce": [], "exclude": [], "default": "enforce"},
            "one_review_authors": ["alice"],
        }
        task["checklist_evidence"] = {
            "review_2_quality": SECOND_REVIEW_NOT_POSTED,
            "review_2_logic": SECOND_REVIEW_NOT_POSTED,
        }
        task["gates"] = [g for g in task["gates"] if g["stage"] == "review-1"]
        blocking = ready_for_done_blocking(task)
        joined = " ".join(blocking)
        self.assertNotIn("review_2_quality", joined)
        self.assertNotIn("review_2_logic", joined)
        self.assertNotIn("review-2", joined)
        self.assertEqual(blocking, [])

    def test_both_codex_n_a_without_the_evidence_still_blocks(self) -> None:
        task = _ready_task()
        task["checklist"]["review_2_quality"] = "n_a"
        task["checklist"]["review_2_logic"] = "n_a"
        task["pull_opener"] = "alice"
        task["trusted_pr_guard"] = {
            "schema": "pr-guard/v1",
            "a38": {"enforce": [], "exclude": [], "default": "enforce"},
            "one_review_authors": ["alice"],
        }
        task["checklist_evidence"] = {
            "review_2_quality": "seed",
            "review_2_logic": SECOND_REVIEW_NOT_POSTED,
        }
        task["gates"] = [g for g in task["gates"] if g["stage"] == "review-1"]
        blocking = ready_for_done_blocking(task)
        joined = " ".join(blocking)
        self.assertIn("review_2_quality=n_a", joined)
        self.assertIn("review_2_logic=n_a", joined)

    def test_both_codex_n_a_without_trusted_list_still_blocks(self) -> None:
        task = _ready_task()
        task["checklist"]["review_2_quality"] = "n_a"
        task["checklist"]["review_2_logic"] = "n_a"
        task["gates"] = [g for g in task["gates"] if g["stage"] == "review-1"]
        blocking = ready_for_done_blocking(task)
        joined = " ".join(blocking)
        self.assertIn("review_2_quality=n_a", joined)
        self.assertIn("review_2_logic=n_a", joined)

    def test_only_codex_quality_n_a_still_blocks(self) -> None:
        task = _ready_task()
        task["checklist"]["review_2_quality"] = "n_a"
        blocking = ready_for_done_blocking(task)
        self.assertTrue(any("review_2_quality" in b for b in blocking))

    def test_both_codex_n_a_still_blocks_grok_n_a(self) -> None:
        task = _ready_task()
        task["checklist"]["review_2_quality"] = "n_a"
        task["checklist"]["review_2_logic"] = "n_a"
        task["checklist"]["review_1_quality"] = "n_a"
        blocking = ready_for_done_blocking(task)
        self.assertTrue(any("review_1_quality" in b for b in blocking))

    def test_both_codex_n_a_still_blocks_missing_grok_gate(self) -> None:
        task = _ready_task()
        task["checklist"]["review_2_quality"] = "n_a"
        task["checklist"]["review_2_logic"] = "n_a"
        task["gates"] = [
            g
            for g in task["gates"]
            if not (g["stage"] == "review-1" and g["dimension"] == "quality")
        ]
        blocking = ready_for_done_blocking(task)
        self.assertTrue(any("gate:review-1/quality" in b for b in blocking))


if __name__ == "__main__":
    unittest.main()
