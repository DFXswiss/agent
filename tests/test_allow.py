"""Pure allow-gate logic. No Postgres."""

from __future__ import annotations

import unittest

from agent_cli.allow import (
    CHECKLIST_KEYS,
    JA_EVIDENCE_REQUIRED,
    evaluate_allow,
    ready_for_done_blocking,
    requires_evidence,
)


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
            ("grok-pr", "quality", "grok"),
            ("grok-pr", "logic", "grok"),
            ("codex-pr", "quality", "codex"),
            ("codex-pr", "logic", "codex"),
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
        task["checklist"]["grok_pr_quality"] = "unavailable"
        r = evaluate_allow(
            "task-done",
            session_id="sess-a",
            task_id="1",
            session_tasks=[task],
        )
        self.assertFalse(r.allowed)
        self.assertTrue(any("grok_pr_quality=unavailable" in b for b in r.blocking))

    def test_both_codex_n_a_skips_codex_ready_blockers(self) -> None:
        task = _ready_task()
        task["checklist"]["codex_pr_quality"] = "n_a"
        task["checklist"]["codex_pr_logic"] = "n_a"
        task["gates"] = [g for g in task["gates"] if g["stage"] == "grok-pr"]
        blocking = ready_for_done_blocking(task)
        joined = " ".join(blocking)
        self.assertNotIn("codex_pr_quality", joined)
        self.assertNotIn("codex_pr_logic", joined)
        self.assertNotIn("codex-pr", joined)
        self.assertEqual(blocking, [])

    def test_only_codex_quality_n_a_still_blocks(self) -> None:
        task = _ready_task()
        task["checklist"]["codex_pr_quality"] = "n_a"
        blocking = ready_for_done_blocking(task)
        self.assertTrue(any("codex_pr_quality" in b for b in blocking))

    def test_both_codex_n_a_still_blocks_grok_n_a(self) -> None:
        task = _ready_task()
        task["checklist"]["codex_pr_quality"] = "n_a"
        task["checklist"]["codex_pr_logic"] = "n_a"
        task["checklist"]["grok_pr_quality"] = "n_a"
        blocking = ready_for_done_blocking(task)
        self.assertTrue(any("grok_pr_quality" in b for b in blocking))

    def test_both_codex_n_a_still_blocks_missing_grok_gate(self) -> None:
        task = _ready_task()
        task["checklist"]["codex_pr_quality"] = "n_a"
        task["checklist"]["codex_pr_logic"] = "n_a"
        task["gates"] = [
            g
            for g in task["gates"]
            if not (g["stage"] == "grok-pr" and g["dimension"] == "quality")
        ]
        blocking = ready_for_done_blocking(task)
        self.assertTrue(any("gate:grok-pr/quality" in b for b in blocking))


if __name__ == "__main__":
    unittest.main()
