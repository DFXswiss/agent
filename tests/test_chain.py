"""Pure step-chain logic. No Postgres."""

from __future__ import annotations

import unittest

from agent_cli.allow import CHECKLIST_KEYS
from agent_cli.chain import (
    CHAINS,
    close_allowed,
    handoff_prompt,
    is_error_fix_originated,
    next_steps,
    required_source,
    steps_for,
)


def _pending(workflow: str) -> dict[str, str]:
    return {k: "pending" for k in CHECKLIST_KEYS[workflow]}


class TestChainShape(unittest.TestCase):
    def test_every_workflow_matches_checklist_keys(self) -> None:
        for wf in CHECKLIST_KEYS:
            steps_for(wf)  # raises if mismatch
            self.assertIn(wf, CHAINS)


class TestNextSteps(unittest.TestCase):
    def test_first_spine_step_is_session_registered(self) -> None:
        nxt = next_steps("implement", _pending("implement"))
        self.assertEqual([s.key for s in nxt], ["session_registered"])

    def test_nein_is_retryable_current_step(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        cl["spec_written"] = "ja"
        cl["implementer_done"] = "nein"
        nxt = {s.key for s in next_steps("implement", cl)}
        self.assertEqual(nxt, {"implementer_done"})

    def test_cannot_skip_to_implementer(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        nxt = {s.key for s in next_steps("implement", cl)}
        self.assertEqual(nxt, {"spec_written"})
        self.assertNotIn("implementer_done", nxt)

    def test_parallel_grok_gates_after_push(self) -> None:
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
        ):
            cl[k] = "ja"
        nxt = {s.key for s in next_steps("implement", cl)}
        self.assertEqual(nxt, {"grok_pr_quality", "grok_pr_logic"})

    def test_codex_waits_for_both_grok_gates(self) -> None:
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
            "grok_pr_quality",
        ):
            cl[k] = "ja"
        nxt = {s.key for s in next_steps("implement", cl)}
        self.assertEqual(nxt, {"grok_pr_logic"})
        self.assertNotIn("codex_pr_quality", nxt)

    def test_n_a_counts_as_satisfied_need(self) -> None:
        cl = _pending("review")
        cl["session_registered"] = "ja"
        cl["contributing_read"] = "n_a"
        nxt = {s.key for s in next_steps("review", cl)}
        self.assertEqual(nxt, {"grok_pr_quality", "grok_pr_logic"})

    def test_review_gates_before_contributing_ok(self) -> None:
        cl = _pending("review")
        cl["session_registered"] = "ja"
        cl["contributing_read"] = "ja"
        nxt = {s.key for s in next_steps("review", cl)}
        self.assertEqual(nxt, {"grok_pr_quality", "grok_pr_logic"})
        self.assertNotIn("contributing_ok", nxt)


class TestCloseAllowed(unittest.TestCase):
    def test_wrong_order_denied(self) -> None:
        cl = _pending("implement")
        v = close_allowed(
            "implement",
            "implementer_done",
            checklist=cl,
            source="script",
            evidence="diff",
        )
        self.assertFalse(v.allowed)
        self.assertIn("not the next step", v.reason)

    def test_agent_key_rejects_human_source(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        cl["spec_written"] = "ja"
        v = close_allowed(
            "implement",
            "implementer_done",
            checklist=cl,
            source="human",
            evidence="round 1",
            snapshot={
                "agents": [
                    {"role": "implementer", "vendor": "grok", "status": "done", "note": "done"}
                ]
            },
        )
        self.assertFalse(v.allowed)
        self.assertIn("source script", v.reason)

    def test_human_key_rejects_script_source(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        v = close_allowed(
            "implement",
            "spec_written",
            checklist=cl,
            source="script",
            evidence="spec.md",
        )
        self.assertFalse(v.allowed)
        self.assertIn("source human", v.reason)

    def test_close_current_script_step(self) -> None:
        cl = _pending("implement")
        v = close_allowed(
            "implement",
            "session_registered",
            checklist=cl,
            source="script",
            evidence="session register",
            snapshot={"session_active": True},
        )
        self.assertTrue(v.allowed)

    def test_implementer_close_needs_finished_agent(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        cl["spec_written"] = "ja"
        v = close_allowed(
            "implement",
            "implementer_done",
            checklist=cl,
            source="script",
            evidence="round 1",
            snapshot={"agents": []},
        )
        self.assertFalse(v.allowed)
        self.assertIn("implementer", v.reason)
        v2 = close_allowed(
            "implement",
            "implementer_done",
            checklist=cl,
            source="script",
            evidence="round 1",
            snapshot={
                "agents": [
                    {"role": "implementer", "vendor": "grok", "status": "done", "note": ""}
                ],
                "implementer_verdict": "done",
            },
        )
        self.assertTrue(v2.allowed)
        blocked = close_allowed(
            "implement",
            "implementer_done",
            checklist=cl,
            source="script",
            evidence="round 1",
            snapshot={
                "agents": [
                    {"role": "implementer", "vendor": "grok", "status": "done", "note": ""}
                ],
                "implementer_verdict": "blocked",
            },
        )
        self.assertFalse(blocked.allowed)

    def test_reviewer_uses_round_verdict_not_note(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        cl["spec_written"] = "ja"
        cl["implementer_done"] = "ja"
        snap_ok = {
            "agents": [
                {"role": "reviewer", "vendor": "grok", "status": "done", "note": ""}
            ],
            "reviewer_verdict": "approved",
        }
        self.assertTrue(
            close_allowed(
                "implement",
                "reviewer_approved",
                checklist=cl,
                source="script",
                evidence="round 1",
                snapshot=snap_ok,
            ).allowed
        )
        snap_lie = {
            "agents": [
                {
                    "role": "reviewer",
                    "vendor": "grok",
                    "status": "done",
                    "note": "unapproved",
                }
            ],
            "reviewer_verdict": "rejected",
        }
        self.assertFalse(
            close_allowed(
                "implement",
                "reviewer_approved",
                checklist=cl,
                source="script",
                evidence="round 1",
                snapshot=snap_lie,
            ).allowed
        )

    def test_gate_close_needs_approved_record(self) -> None:
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
        ):
            cl[k] = "ja"
        v = close_allowed(
            "implement",
            "grok_pr_quality",
            checklist=cl,
            source="script",
            evidence="review",
            snapshot={"gates": []},
        )
        self.assertFalse(v.allowed)
        v2 = close_allowed(
            "implement",
            "grok_pr_quality",
            checklist=cl,
            source="script",
            evidence="review",
            snapshot={
                "gates": [
                    {
                        "stage": "grok-pr",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                    }
                ]
            },
        )
        self.assertTrue(v2.allowed)

    def test_local_check_bound_head_last_wins_over_earlier_fail(self) -> None:
        """Same-head fail then later pass must allow closing local_check_pass."""
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
        ):
            cl[k] = "ja"
        head = "cccccccccccccccccccccccccccccccccccccccc"
        allowed = close_allowed(
            "implement",
            "local_check_pass",
            checklist=cl,
            source="script",
            evidence="run auto",
            snapshot={
                "head_sha": head,
                "local_checks": [
                    {
                        "name": "local",
                        "result": "fail",
                        "head_sha": head,
                    },
                    {
                        "name": "local",
                        "result": "pass",
                        "head_sha": head,
                    },
                ],
            },
        )
        self.assertTrue(allowed.allowed)

    def test_gate_close_rejects_stale_head_approval(self) -> None:
        """An approved gate for a different head must not satisfy the current head."""
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
        ):
            cl[k] = "ja"
        head_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        head_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        stale = close_allowed(
            "implement",
            "grok_pr_quality",
            checklist=cl,
            source="script",
            evidence="review",
            snapshot={
                "head_sha": head_a,
                "gates": [
                    {
                        "stage": "grok-pr",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                        "head_sha": head_b,
                    }
                ],
            },
        )
        self.assertFalse(stale.allowed)
        fresh = close_allowed(
            "implement",
            "grok_pr_quality",
            checklist=cl,
            source="script",
            evidence="review",
            snapshot={
                "head_sha": head_a,
                "gates": [
                    {
                        "stage": "grok-pr",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                        "head_sha": head_b,
                    },
                    {
                        "stage": "grok-pr",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                        "head_sha": head_a,
                    },
                ],
            },
        )
        self.assertTrue(fresh.allowed)

    def test_no_evidence_denied(self) -> None:
        cl = _pending("implement")
        v = close_allowed(
            "implement",
            "session_registered",
            checklist=cl,
            source="script",
            evidence="",
        )
        self.assertFalse(v.allowed)

    def test_required_source(self) -> None:
        spec = next(s for s in steps_for("implement") if s.key == "spec_written")
        impl = next(s for s in steps_for("implement") if s.key == "implementer_done")
        self.assertEqual(required_source(spec), "human")
        self.assertEqual(required_source(impl), "script")

    def test_handoff_names_only_this_key(self) -> None:
        step = next(s for s in steps_for("implement") if s.key == "implementer_done")
        text = handoff_prompt(step, task_id="31", session_id="sess")
        self.assertIn("key=implementer_done", text)
        self.assertIn("close-step", text)
        self.assertNotIn("grok_pr_quality", text)

    def test_error_fix_carve_out_needs_confirmed_fix(self) -> None:
        """payload.error_id alone (no error_fix_confirmed) is not originated."""
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        snap_seen_only = {
            "payload": {"error_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            "error_fix_confirmed": False,
        }
        self.assertFalse(is_error_fix_originated(snap_seen_only))
        denied = close_allowed(
            "implement",
            "spec_written",
            checklist=cl,
            source="script",
            evidence="auto spec",
            snapshot=snap_seen_only,
        )
        self.assertFalse(denied.allowed)

        snap_confirmed = {
            "payload": {"error_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            "error_fix_confirmed": True,
        }
        self.assertTrue(is_error_fix_originated(snap_confirmed))
        allowed = close_allowed(
            "implement",
            "spec_written",
            checklist=cl,
            source="script",
            evidence="auto spec",
            snapshot=snap_confirmed,
        )
        self.assertTrue(allowed.allowed)

    def test_error_fix_deviation_n_a_script_carve_out(self) -> None:
        """Confirmed error-fix may script-author deviation_* only as n_a."""
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
            "grok_pr_quality",
            "grok_pr_logic",
            "codex_pr_quality",
            "codex_pr_logic",
            "contributing_ok",
        ):
            cl[k] = "ja"
        snap = {
            "payload": {"error_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            "error_fix_confirmed": True,
        }
        evidence = "error-fix task: no CONTRIBUTING.md deviation, mechanically generated"
        declared = close_allowed(
            "implement",
            "deviation_declared",
            checklist=cl,
            source="script",
            evidence=evidence,
            status="n_a",
            snapshot=snap,
        )
        self.assertTrue(declared.allowed)
        cl["deviation_declared"] = "n_a"
        granted = close_allowed(
            "implement",
            "deviation_granted",
            checklist=cl,
            source="script",
            evidence=evidence,
            status="n_a",
            snapshot=snap,
        )
        self.assertTrue(granted.allowed)

        cl["deviation_declared"] = "pending"
        denied_ja = close_allowed(
            "implement",
            "deviation_declared",
            checklist=cl,
            source="script",
            evidence=evidence,
            status="ja",
            snapshot=snap,
        )
        self.assertFalse(denied_ja.allowed)
        self.assertIn("source human", denied_ja.reason)

    def test_error_fix_deviation_n_a_denied_without_confirmed_fix(self) -> None:
        """Without confirmed error.fix, deviation n_a still requires human source."""
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
            "grok_pr_quality",
            "grok_pr_logic",
            "codex_pr_quality",
            "codex_pr_logic",
            "contributing_ok",
        ):
            cl[k] = "ja"
        snap_seen_only = {
            "payload": {"error_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            "error_fix_confirmed": False,
        }
        denied = close_allowed(
            "implement",
            "deviation_declared",
            checklist=cl,
            source="script",
            evidence="no deviation",
            status="n_a",
            snapshot=snap_seen_only,
        )
        self.assertFalse(denied.allowed)
        self.assertIn("source human", denied.reason)


if __name__ == "__main__":
    unittest.main()
