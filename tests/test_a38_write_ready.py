"""Write-collaborator Ready waiver for the A38 author-report gate."""

from __future__ import annotations

import unittest
from typing import Any

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli import a38_guard
from agent_cli.a38_guard import reconcile_pull, status_context_enforce
from test_a38_guard import (
    AUTHOR_ID,
    BOT_ID,
    HEAD,
    BASE,
    REPO,
    FakeAPI,
    _workflow_yaml,
)
from test_pr_lifecycle import LifecycleAPI  # noqa: E402


MAINTAINER_ID = 3003


def _ready_event(
    *,
    login: str,
    uid: int,
    created_at: str = "2026-09-05T12:00:00Z",
    eid: int = 1,
    actor_type: str = "User",
) -> dict[str, Any]:
    return {
        "event": "ready_for_review",
        "id": eid,
        "created_at": created_at,
        "actor": {"id": uid, "login": login, "type": actor_type},
    }


class WriteReadyWaiverTests(unittest.TestCase):
    def test_ready_no_report_no_write_still_fails(self) -> None:
        fake = FakeAPI()
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.state_for_status, "failure")
        self.assertIn(
            "no author local-CI report comment on this pull request",
            result.reasons,
        )
        self.assertFalse(result.write_ready)

    def test_ready_no_report_author_write_passes_and_lifecycle_holds(self) -> None:
        fake = LifecycleAPI()
        fake.comments.clear()
        fake.permissions["author"] = {
            "permission": "write",
            "user": {"id": AUTHOR_ID},
        }
        fake.runs[0].update(status="in_progress", conclusion=None)
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pass")
        self.assertTrue(result.write_ready)
        self.assertEqual(result.write_ready_reason, "author has write")
        self.assertEqual(result.state_for_status, "success")
        self.assertIn("author has write", result.description)
        self.assertEqual(result.lifecycle.get("action"), "unchanged")
        self.assertEqual(fake.transitions, [])
        self.assertFalse(fake.pull["draft"])

    def test_ready_no_report_ready_actor_admin_passes_and_lifecycle_holds(self) -> None:
        fake = LifecycleAPI()
        fake.comments.clear()
        fake.timeline = [
            _ready_event(login="maintainer", uid=MAINTAINER_ID, eid=2),
        ]
        fake.permissions["maintainer"] = {
            "permission": "admin",
            "user": {"id": MAINTAINER_ID},
        }
        fake.runs[0].update(status="completed", conclusion="failure")
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pass")
        self.assertTrue(result.write_ready)
        self.assertEqual(result.write_ready_reason, "ready by write collaborator")
        self.assertIn("write collaborator", result.description)
        self.assertEqual(result.lifecycle.get("action"), "unchanged")
        self.assertEqual(fake.transitions, [])

    def test_ready_bot_ready_actor_cannot_grant_waiver(self) -> None:
        fake = FakeAPI()
        fake.timeline = [
            _ready_event(
                login="github-actions[bot]",
                uid=BOT_ID,
                actor_type="Bot",
            ),
        ]
        # Even a forged permission map must not grant the waiver for bot logins.
        fake.permissions["github-actions[bot]"] = {
            "permission": "admin",
            "user": {"id": BOT_ID},
        }
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertFalse(result.write_ready)
        self.assertIn(
            "no author local-CI report comment on this pull request",
            result.reasons,
        )

    def test_ready_actor_triage_or_read_no_waiver(self) -> None:
        for role in ("triage", "read"):
            with self.subTest(role=role):
                fake = FakeAPI()
                fake.timeline = [
                    _ready_event(login="maintainer", uid=MAINTAINER_ID),
                ]
                fake.permissions["maintainer"] = {
                    "permission": role,
                    "user": {"id": MAINTAINER_ID},
                }
                result = reconcile_pull(fake.api(), REPO, 1)
                self.assertFalse(result.ok)
                self.assertFalse(result.write_ready)

    def test_ready_actor_permission_id_mismatch_no_waiver(self) -> None:
        fake = FakeAPI()
        fake.timeline = [
            _ready_event(login="maintainer", uid=MAINTAINER_ID),
        ]
        fake.permissions["maintainer"] = {
            "permission": "admin",
            "user": {"id": 9999},
        }
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertFalse(result.write_ready)

    def test_draft_author_write_no_blocking_status_and_comment_waives_report(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.permissions["author"] = {
            "permission": "write",
            "user": {"id": AUTHOR_ID},
        }
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pass")
        self.assertTrue(result.draft)
        self.assertTrue(result.write_ready)
        self.assertEqual(result.state_for_status, "")
        self.assertTrue(
            any(w == "status:skipped:draft" for w in result.writes)
            or not any(w.startswith("status:create:") for w in result.writes)
        )
        self.assertNotIn(
            status_context_enforce("develop"),
            [s["context"] for s in fake.statuses],
        )
        self.assertEqual(a38_guard._assessment_exit_code(result), 0)
        body = result.comment_body
        self.assertNotIn("still required before Ready", body)
        self.assertIn("not required", body)
        self.assertIn("write", body.lower())

    def test_author_write_does_not_waive_workflow_problems(self) -> None:
        fake = FakeAPI()
        fake.permissions["author"] = {
            "permission": "write",
            "user": {"id": AUTHOR_ID},
        }
        # Head introduces an unclassified workflow job → inventory failure.
        fake.files[(HEAD, ".github/workflows/test.yml")] = _workflow_yaml(
            ["pytest", "extra"]
        )
        result = reconcile_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertTrue(result.write_ready)
        self.assertTrue(any("unclassified" in r for r in result.reasons))


if __name__ == "__main__":
    unittest.main()
