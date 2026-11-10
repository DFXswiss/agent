"""Pure step-chain logic. No Postgres."""

from __future__ import annotations

import unittest

from agent_cli.allow import CHECKLIST_KEYS
from agent_cli.chain import (
    CHAINS,
    Step,
    _artifact_ok,
    _satisfied,
    _unavailable_gate_ok,
    close_allowed,
    handoff_prompt,
    next_steps,
    required_source,
    steps_for,
)
from agent_cli.model_slots import ModelSlots

CONFIGURED_MODELS = ModelSlots("grok", "codex")


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
        self.assertEqual(nxt, {"review_1_quality", "review_1_logic"})

    def test_codex_waits_for_both_grok_gates(self) -> None:
        cl = _pending("implement")
        for k in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
            "review_1_quality",
        ):
            cl[k] = "ja"
        nxt = {s.key for s in next_steps("implement", cl)}
        self.assertEqual(nxt, {"review_1_logic"})
        self.assertNotIn("review_2_quality", nxt)

    def test_stored_legacy_first_review_keys_satisfy_new_dependency(self) -> None:
        cl = _pending("implement")
        for key in (
            "session_registered",
            "spec_written",
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
        ):
            cl[key] = "ja"
        # Keep legacy keys here to verify stored-checklist compatibility.
        del cl["review_1_quality"]
        del cl["review_1_logic"]
        cl["grok_pr_quality"] = "ja"
        cl["grok_pr_logic"] = "ja"

        nxt = {step.key for step in next_steps("implement", cl)}

        self.assertEqual(nxt, {"review_2_quality", "review_2_logic"})

    def test_n_a_counts_as_satisfied_need(self) -> None:
        cl = _pending("review")
        cl["session_registered"] = "ja"
        cl["contributing_read"] = "n_a"
        nxt = {s.key for s in next_steps("review", cl)}
        self.assertEqual(nxt, {"review_1_quality", "review_1_logic"})

    def test_review_gates_before_contributing_ok(self) -> None:
        cl = _pending("review")
        cl["session_registered"] = "ja"
        cl["contributing_read"] = "ja"
        nxt = {s.key for s in next_steps("review", cl)}
        self.assertEqual(nxt, {"review_1_quality", "review_1_logic"})
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
            "review_1_quality",
            checklist=cl,
            source="script",
            evidence="review",
            snapshot={"gates": []},
        )
        self.assertFalse(v.allowed)
        v2 = close_allowed(
            "implement",
            "review_1_quality",
            checklist=cl,
            source="script",
            evidence="review",
            snapshot={
                "gates": [
                    {
                        "stage": "review-1",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                    }
                ]
            },
        )
        self.assertTrue(v2.allowed)

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
        text = handoff_prompt(
            step, task_id="31", session_id="sess", models=CONFIGURED_MODELS
        )
        self.assertIn("key=implementer_done", text)
        self.assertIn("close-step", text)
        self.assertNotIn("review_1_quality", text)

    def test_handoff_implementer_names_the_first_slot(self) -> None:
        impl = next(s for s in steps_for("implement") if s.key == "implementer_done")
        self.assertEqual(impl.slot, "first")
        text = handoff_prompt(
            impl, task_id="31", session_id="sess", models=CONFIGURED_MODELS
        )
        self.assertIn("who=grok implementer", text)
        self.assertNotIn("grok|codex", text)
        conflicts = next(
            s for s in steps_for("resolve-conflicts") if s.key == "conflicts_resolved"
        )
        self.assertEqual(conflicts.slot, "first")
        conflicts_text = handoff_prompt(
            conflicts, task_id="31", session_id="sess", models=CONFIGURED_MODELS
        )
        self.assertIn("who=grok implementer", conflicts_text)
        reviewer = next(s for s in steps_for("implement") if s.key == "reviewer_approved")
        self.assertEqual(reviewer.slot, "first")
        reviewer_text = handoff_prompt(
            reviewer, task_id="31", session_id="sess", models=CONFIGURED_MODELS
        )
        self.assertIn("who=grok reviewer", reviewer_text)
        script = next(s for s in steps_for("implement") if s.key == "session_registered")
        script_text = handoff_prompt(script, task_id="31", session_id="sess")
        self.assertIn("who=- script", script_text)

    def test_unavailable_gate_close_needs_unavailable_record(self) -> None:
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
            "review_1_quality",
            checklist=cl,
            source="script",
            evidence="lane 402",
            snapshot={
                "gates": [
                    {
                        "stage": "review-1",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                    }
                ]
            },
            status="unavailable",
        )
        self.assertFalse(v.allowed)
        v2 = close_allowed(
            "implement",
            "review_1_quality",
            checklist=cl,
            source="script",
            evidence="lane 402",
            snapshot={
                "gates": [
                    {
                        "stage": "review-1",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "unavailable",
                    }
                ]
            },
            status="unavailable",
        )
        self.assertTrue(v2.allowed)

    def test_late_inner_skips_artifact_ok_only_in_late_states(self) -> None:
        cl = _pending("implement")
        cl["session_registered"] = "ja"
        cl["spec_written"] = "ja"
        empty = {"agents": []}
        for state in ("local-check", "pushing", "pr-review", "gate-blocked"):
            self.assertTrue(
                close_allowed(
                    "implement",
                    "implementer_done",
                    checklist=cl,
                    source="script",
                    evidence="late stamp",
                    snapshot={**empty, "state": state},
                ).allowed,
                msg=f"late skip should apply in {state}",
            )
        for state in ("open", "implementing", "reviewing"):
            denied = close_allowed(
                "implement",
                "implementer_done",
                checklist=cl,
                source="script",
                evidence="late stamp",
                snapshot={**empty, "state": state},
            )
            self.assertFalse(denied.allowed, msg=f"late skip must not apply in {state}")
            self.assertIn("implementer", denied.reason)
        reviewer_too_soon = close_allowed(
            "implement",
            "reviewer_approved",
            checklist=cl,
            source="script",
            evidence="late stamp",
            snapshot={**empty, "state": "pr-review"},
        )
        self.assertFalse(reviewer_too_soon.allowed)
        self.assertIn("not the next step", reviewer_too_soon.reason)
        cl["implementer_done"] = "ja"
        self.assertTrue(
            close_allowed(
                "implement",
                "reviewer_approved",
                checklist=cl,
                source="script",
                evidence="late stamp",
                snapshot={**empty, "state": "pr-review"},
            ).allowed
        )

    def test_resolve_conflicts_late_inner_skips_artifact_ok(self) -> None:
        cl = _pending("resolve-conflicts")
        cl["session_registered"] = "ja"
        empty = {"agents": []}
        for state in ("local-check", "pushing", "pr-review", "gate-blocked"):
            self.assertTrue(
                close_allowed(
                    "resolve-conflicts",
                    "conflicts_resolved",
                    checklist=cl,
                    source="script",
                    evidence="late stamp",
                    snapshot={**empty, "state": state},
                ).allowed,
                msg=f"late skip should apply in {state}",
            )
        for state in ("open", "implementing", "reviewing"):
            denied = close_allowed(
                "resolve-conflicts",
                "conflicts_resolved",
                checklist=cl,
                source="script",
                evidence="late stamp",
                snapshot={**empty, "state": state},
            )
            self.assertFalse(denied.allowed, msg=f"late skip must not apply in {state}")
            self.assertIn("no finished implementer agent", denied.reason)
        cl["conflicts_resolved"] = "ja"
        self.assertTrue(
            close_allowed(
                "resolve-conflicts",
                "reviewer_approved",
                checklist=cl,
                source="script",
                evidence="late stamp",
                snapshot={**empty, "state": "pr-review"},
            ).allowed
        )

    def test_unavailable_is_not_a_satisfied_need(self) -> None:
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
        cl["review_1_quality"] = "unavailable"
        nxt = {s.key for s in next_steps("implement", cl)}
        self.assertIn("review_1_quality", nxt)
        self.assertIn("review_1_logic", nxt)
        self.assertNotIn("review_2_quality", nxt)
        self.assertNotIn("review_2_logic", nxt)

    def test_codex_n_a_second_review_not_posted(self) -> None:
        cl = _pending("review")
        cl["session_registered"] = "ja"
        cl["contributing_read"] = "ja"
        cl["review_1_quality"] = "ja"
        cl["review_1_logic"] = "ja"
        listed = {
            "pull_opener": "alice",
            "trusted_pr_guard": {
                "schema": "pr-guard/v1",
                "a38": {"enforce": [], "exclude": [], "default": "enforce"},
                "one_review_authors": ["alice"],
            },
        }
        allowed = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence="second review not posted",
            snapshot=listed,
            status="n_a",
        )
        self.assertTrue(allowed.allowed)
        denied_standard = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence="second review not posted",
            snapshot={},
            status="n_a",
        )
        self.assertFalse(denied_standard.allowed)
        denied = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence="something else",
            snapshot={},
            status="n_a",
        )
        self.assertFalse(denied.allowed)
        self.assertIn("second review not posted", denied.reason)
        grok = _pending("review")
        grok["session_registered"] = "ja"
        grok["contributing_read"] = "ja"
        grok_denied = close_allowed(
            "review",
            "review_1_quality",
            checklist=grok,
            source="script",
            evidence="second review not posted",
            snapshot={},
            status="n_a",
        )
        self.assertFalse(grok_denied.allowed)

    def test_external_second_review_requires_a38_marker_and_first_gate_head(self) -> None:
        head = "a" * 40
        cl = _pending("review")
        for key in (
            "session_registered",
            "contributing_read",
            "review_1_quality",
            "review_1_logic",
        ):
            cl[key] = "ja"
        evidence = f"external second review by reviewer at {head}: https://example.invalid/review"
        snapshot = {
            "models": {"first": "grok", "second": None},
            "payload": {"coordinator": {"a38": {"enabled": True, "second_review": {"head": head, "status": "posted", "reviews": [{"blocks_merge": False, "at": "2026-01-01T00:00:00Z"}]}}}},
            "gates": [
                {
                    "stage": "review-1",
                    "dimension": dimension,
                    "vendor": "grok",
                    "verdict": "approved",
                    "head_sha": head,
                }
                for dimension in ("quality", "logic")
            ],
        }
        allowed = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence=evidence,
            snapshot=snapshot,
        )
        self.assertTrue(allowed.allowed, allowed.reason)
        stale = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence=f"external second review by reviewer at {'b' * 40}: https://example.invalid/review",
            snapshot=snapshot,
        )
        self.assertFalse(stale.allowed)
        for second_review in (
            {"head": head, "status": "requested"},
            {"head": head, "status": "posted", "reviews": []},
            {"head": head, "status": "posted", "reviews": [{"blocks_merge": True, "at": "x"}]},
            {"head": "b" * 40, "status": "posted", "reviews": [{"blocks_merge": False, "at": "x"}]},
        ):
            unposted = {
                **snapshot,
                "payload": {"coordinator": {"a38": {"enabled": True, "second_review": second_review}}},
            }
            self.assertFalse(
                close_allowed(
                    "review",
                    "review_2_quality",
                    checklist=cl,
                    source="script",
                    evidence=evidence,
                    snapshot=unposted,
                ).allowed
            )
        without_marker = {**snapshot, "payload": {}}
        denied = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence=evidence,
            snapshot=without_marker,
        )
        self.assertFalse(denied.allowed)

    def test_codex_n_a_satisfies_mergeable_need(self) -> None:
        cl = _pending("resolve-conflicts")
        for key in (
            "session_registered",
            "conflicts_resolved",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
            "review_1_quality",
            "review_1_logic",
        ):
            cl[key] = "ja"
        cl["review_2_quality"] = "n_a"
        cl["review_2_logic"] = "n_a"
        self.assertTrue(_satisfied("review_2_quality", cl))
        self.assertTrue(_satisfied("review_2_logic", cl))
        self.assertFalse(_satisfied("review_1_quality", {"review_1_quality": "n_a"}))
        nxt = {s.key for s in next_steps("resolve-conflicts", cl)}
        self.assertIn("mergeable", nxt)
        cl["review_2_logic"] = "pending"
        self.assertFalse(_satisfied("review_2_logic", cl))
        nxt = {s.key for s in next_steps("resolve-conflicts", cl)}
        self.assertNotIn("mergeable", nxt)

    def test_posted_codex_review_replaces_n_a_with_ja(self) -> None:
        cl = _pending("review")
        for key in (
            "session_registered",
            "contributing_read",
            "review_1_quality",
            "review_1_logic",
        ):
            cl[key] = "ja"
        cl["review_2_quality"] = "n_a"
        cl["review_2_logic"] = "n_a"
        snap = {
            "gates": [
                {
                    "stage": "review-2",
                    "dimension": "quality",
                    "vendor": "codex",
                    "verdict": "approved",
                },
                {
                    "stage": "review-2",
                    "dimension": "logic",
                    "vendor": "codex",
                    "verdict": "approved",
                },
            ]
        }
        for key, dim in (("review_2_quality", "quality"), ("review_2_logic", "logic")):
            verdict = close_allowed(
                "review",
                key,
                checklist=cl,
                source="script",
                evidence=f"review-2/{dim} on head",
                snapshot=snap,
                status="ja",
            )
            self.assertTrue(verdict.allowed, verdict.reason)
        empty = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence="  ",
            snapshot=snap,
            status="ja",
        )
        self.assertFalse(empty.allowed)
        self.assertIn("evidence", empty.reason)
        wrong_source = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="human",
            evidence="posted",
            snapshot=snap,
            status="ja",
        )
        self.assertFalse(wrong_source.allowed)
        missing_gate = close_allowed(
            "review",
            "review_2_quality",
            checklist=cl,
            source="script",
            evidence="posted",
            snapshot={"gates": []},
            status="ja",
        )
        self.assertFalse(missing_gate.allowed)
        self.assertIn("not approved", missing_gate.reason)
        grok = dict(cl)
        grok["review_1_quality"] = "n_a"
        denied = close_allowed(
            "review",
            "review_1_quality",
            checklist=grok,
            source="script",
            evidence="posted",
            snapshot={
                "gates": [
                    {
                        "stage": "review-1",
                        "dimension": "quality",
                        "vendor": "grok",
                        "verdict": "approved",
                    }
                ]
            },
            status="ja",
        )
        self.assertFalse(denied.allowed)
        self.assertIn("not the next step", denied.reason)



class TestImplementerRowFromBeforeTheSlotBinding(unittest.TestCase):
    def test_an_unpinned_task_closes_with_a_second_slot_row(self) -> None:
        impl = next(s for s in steps_for("implement") if s.key == "implementer_done")
        snapshot = {
            "agents": [{"role": "implementer", "vendor": "codex", "status": "done"}],
            "implementer_verdict": "done",
            "models_pinned": False,
        }
        self.assertEqual(_artifact_ok(impl, snapshot), "")
        conflicts = next(
            s for s in steps_for("resolve-conflicts") if s.key == "conflicts_resolved"
        )
        self.assertEqual(_artifact_ok(conflicts, snapshot), "")

    def test_a_pinned_task_needs_the_slot_first_row(self) -> None:
        impl = next(s for s in steps_for("implement") if s.key == "implementer_done")
        snapshot = {
            "agents": [
                {"role": "implementer", "vendor": "grok", "status": "working"},
                {"role": "implementer", "vendor": "codex", "status": "done"},
            ],
            "implementer_verdict": "done",
            "models": {"first": "grok", "second": "codex"},
            "models_pinned": True,
        }
        self.assertEqual(_artifact_ok(impl, snapshot), "no finished implementer agent")
        snapshot["agents"].append({"role": "implementer", "vendor": "grok", "status": "done"})
        self.assertEqual(_artifact_ok(impl, snapshot), "")

    def test_a_separate_implementer_pin_closes_only_with_that_provider(self) -> None:
        impl = next(s for s in steps_for("implement") if s.key == "implementer_done")
        snapshot = {
            "agents": [
                {"role": "implementer", "vendor": "grok", "status": "done"},
            ],
            "implementer_verdict": "done",
            "models": {"first": "grok", "second": "codex"},
            "implementer": "deepseek",
            "models_pinned": True,
        }
        self.assertEqual(_artifact_ok(impl, snapshot), "no finished implementer agent")
        snapshot["agents"].append(
            {"role": "implementer", "vendor": "deepseek", "status": "done"}
        )
        self.assertEqual(_artifact_ok(impl, snapshot), "")

    def test_a_row_outside_the_slots_does_not_close_the_step(self) -> None:
        impl = next(s for s in steps_for("implement") if s.key == "implementer_done")
        snapshot = {
            "agents": [{"role": "implementer", "vendor": "claude", "status": "done"}],
            "implementer_verdict": "done",
            "models_pinned": False,
        }
        self.assertEqual(_artifact_ok(impl, snapshot), "no finished implementer agent")


class TestPrReviewerWithoutSlot(unittest.TestCase):
    def test_pr_reviewer_step_without_slot_reports_instead_of_raising(self) -> None:
        step = Step(key="future_pr_logic", needs=(), kind="agent", role="pr-reviewer-logic")
        self.assertEqual(_artifact_ok(step, {"gates": []}), "future_pr_logic has no model slot")
        self.assertEqual(
            _unavailable_gate_ok(step, {"gates": []}), "future_pr_logic has no model slot"
        )


if __name__ == "__main__":
    unittest.main()
