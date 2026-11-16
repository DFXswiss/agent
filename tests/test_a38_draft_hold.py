"""Human draft hold for the A38 lifecycle reconciler."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli import a38_guard
from agent_cli.a38_guard import DraftHold, load_draft_hold, reconcile_pull
from test_a38_guard import AUTHOR_ID, BOT_ID, REPO, FakeAPI
from test_pr_lifecycle import LifecycleAPI

MAINTAINER_ID = 3003


def _event(
    kind: str,
    *,
    uid: int,
    login: str,
    created_at: str = "2026-09-05T12:00:00Z",
    actor_type: str = "User",
    eid: int = 1,
) -> dict[str, Any]:
    return {
        "event": kind,
        "id": eid,
        "created_at": created_at,
        "actor": {"id": uid, "login": login, "type": actor_type},
    }


def _draft(
    *,
    uid: int = MAINTAINER_ID,
    login: str = "maintainer",
    created_at: str = "2026-09-05T12:00:00Z",
    actor_type: str = "User",
    eid: int = 1,
) -> dict[str, Any]:
    return _event(
        "convert_to_draft",
        uid=uid,
        login=login,
        created_at=created_at,
        actor_type=actor_type,
        eid=eid,
    )


def _ready(
    *,
    uid: int = MAINTAINER_ID,
    login: str = "maintainer",
    created_at: str = "2026-09-05T12:00:00Z",
    actor_type: str = "User",
    eid: int = 2,
) -> dict[str, Any]:
    return _event(
        "ready_for_review",
        uid=uid,
        login=login,
        created_at=created_at,
        actor_type=actor_type,
        eid=eid,
    )


def _hold(fake: FakeAPI) -> tuple[str, DraftHold | None]:
    return load_draft_hold(
        fake.api(), REPO, 1, guard_user_id=BOT_ID
    )


def test_newest_human_draft_is_a_hold() -> None:
    fake = FakeAPI()
    fake.timeline = [_draft()]
    status, hold = _hold(fake)
    assert status == "ok"
    assert hold == DraftHold(MAINTAINER_ID, "maintainer", "2026-09-05T12:00:00Z")


def test_newer_human_ready_releases_the_hold_but_the_state_is_pending() -> None:
    fake = FakeAPI()
    fake.timeline = [
        _draft(created_at="2026-09-05T10:00:00Z"),
        _ready(created_at="2026-09-05T12:00:00Z", eid=2),
    ]
    assert _hold(fake) == ("pending", None)


def test_non_user_ready_does_not_release_a_hold() -> None:
    fake = FakeAPI()
    fake.timeline = [
        _draft(created_at="2026-09-05T10:00:00Z"),
        _ready(created_at="2026-09-05T12:00:00Z", actor_type="Bot", eid=2),
    ]
    status, hold = _hold(fake)
    assert status == "ok"
    assert hold is not None
    assert hold.created_at == "2026-09-05T10:00:00Z"


def test_non_user_draft_does_not_hold() -> None:
    fake = FakeAPI()
    fake.timeline = [_draft(actor_type="Bot", uid=9090, login="ci")]
    assert _hold(fake) == ("ok", None)


def test_guard_own_draft_does_not_hold() -> None:
    fake = FakeAPI()
    fake.timeline = [
        _ready(created_at="2026-09-05T10:00:00Z"),
        _draft(uid=BOT_ID, login="github-actions[bot]", created_at="2026-09-05T12:00:00Z"),
    ]
    assert _hold(fake) == ("ok", None)


def test_older_human_draft_after_a_guard_draft_still_holds() -> None:
    fake = FakeAPI()
    fake.timeline = [
        _draft(created_at="2026-09-05T10:00:00Z"),
        _draft(
            uid=BOT_ID,
            login="github-actions[bot]",
            created_at="2026-09-05T12:00:00Z",
            eid=3,
        ),
    ]
    status, hold = _hold(fake)
    assert status == "ok"
    assert hold is not None
    assert hold.login == "maintainer"


def test_malformed_draft_fails_closed() -> None:
    cases = (
        {"event": "convert_to_draft", "id": 1, "created_at": "2026-09-05T12:00:00Z"},
        {
            "event": "convert_to_draft",
            "id": 1,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"login": "maintainer", "type": "User"},
        },
        {
            "event": "convert_to_draft",
            "id": 1,
            "actor": {"id": MAINTAINER_ID, "login": "maintainer", "type": "User"},
        },
        {
            "event": "convert_to_draft",
            "id": 1,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": MAINTAINER_ID, "login": "", "type": "User"},
        },
        {
            "event": "convert_to_draft",
            "id": 1,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": MAINTAINER_ID, "login": "maintainer"},
        },
        {
            "event": "convert_to_draft",
            "id": 1,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": MAINTAINER_ID, "login": "maintainer", "type": None},
        },
    )
    for event in cases:
        fake = FakeAPI()
        fake.timeline = [event]
        assert _hold(fake) == ("unavailable", None), event


def test_malformed_ready_is_skipped_and_an_older_draft_holds() -> None:
    fake = FakeAPI()
    fake.timeline = [
        _draft(created_at="2026-09-05T10:00:00Z"),
        {
            "event": "ready_for_review",
            "id": 2,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"login": "maintainer", "type": "User"},
        },
    ]
    status, hold = _hold(fake)
    assert status == "ok"
    assert hold is not None


def test_newest_ready_without_a_hold_is_pending() -> None:
    fake = FakeAPI()
    fake.timeline = [_ready()]
    assert _hold(fake) == ("pending", None)


def test_newest_ready_with_older_draft_events_of_every_actor_is_pending() -> None:
    fake = FakeAPI()
    fake.timeline = [
        _ready(created_at="2026-09-05T10:00:00Z", eid=1),
        _draft(
            uid=BOT_ID,
            login="github-actions[bot]",
            created_at="2026-09-05T11:00:00Z",
            eid=2,
        ),
        _ready(created_at="2026-09-05T12:00:00Z", eid=3),
    ]
    assert _hold(fake) == ("pending", None)


def test_empty_timeline_is_not_pending() -> None:
    assert _hold(FakeAPI()) == ("ok", None)


def test_timeline_404_is_unavailable() -> None:
    fake = FakeAPI()
    fake.timeline_404 = True
    assert _hold(fake) == ("unavailable", None)


def test_timeline_403_is_unavailable() -> None:
    fake = FakeAPI()
    fake.denied_prefixes.append(f"/repos/{REPO}/issues/1/timeline")
    assert _hold(fake) == ("unavailable", None)


def test_timeline_401_is_unavailable() -> None:
    fake = FakeAPI()
    inner = fake.request_fn

    def request_fn(method: str, url: str, body: bytes | None = None):
        if method.upper() == "GET" and urlparse(url).path.startswith(
            f"/repos/{REPO}/issues/1/timeline"
        ):
            return 401, {}, {}
        return inner(method, url, body)

    fake.request_fn = request_fn
    assert _hold(fake) == ("unavailable", None)


def test_draft_is_held_and_the_comment_is_not_rewritten() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.timeline = [_draft(created_at="2100-01-01T00:00:00Z")]
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.ok
    assert first.lifecycle["action"] == "unchanged"
    assert "draft held by maintainer" in first.lifecycle["reasons"]
    assert fake.transitions == []
    assert fake.pull["draft"]
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert len(bodies) == 1
    assert "Draft held" in bodies[0]
    assert "Entwurf bleibt" in bodies[0]
    assert "until a person marks it ready" in bodies[0]

    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.lifecycle["action"] == "unchanged"
    assert "draft held by maintainer" in second.lifecycle["reasons"]
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert len(bodies) == 1
    assert fake.transitions == []


def test_unreadable_hold_keeps_draft_and_writes_the_comment() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.timeline_404 = True
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "unchanged"
    assert "draft hold unavailable" in result.lifecycle["reasons"]
    assert fake.transitions == []
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert len(bodies) == 1
    assert "cannot read who set this pull request to draft" in bodies[0]
    assert "kann nicht lesen" in bodies[0]


def test_draft_event_without_an_actor_type_keeps_draft_and_writes_the_comment() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.timeline = [
        {
            "event": "convert_to_draft",
            "id": 1,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": MAINTAINER_ID, "login": "maintainer"},
        }
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "unchanged"
    assert "draft hold unavailable" in result.lifecycle["reasons"]
    assert fake.transitions == []
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert len(bodies) == 1
    assert "cannot read who set this pull request to draft" in bodies[0]
    assert "kann nicht lesen" in bodies[0]


def test_pending_hold_keeps_draft_without_a_new_comment() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.timeline = [_ready()]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "unchanged"
    assert "draft hold pending" in result.lifecycle["reasons"]
    assert fake.transitions == []
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert bodies == []


def test_a_review_record_after_the_hold_releases_it() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.timeline = [_draft(created_at="2026-01-01T00:00:00Z")]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "ready"
    assert fake.transitions == [False]
    assert not fake.pull["draft"]
    assert not any(
        "draft held by" in reason for reason in result.lifecycle["reasons"]
    )


def test_ready_pull_with_an_active_hold_returns_to_draft() -> None:
    fake = LifecycleAPI()
    fake.timeline = [_draft(created_at="2100-01-01T00:00:00Z")]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "draft"
    assert fake.transitions == [True]
    assert fake.pull["draft"]
    assert "draft held by maintainer" in result.lifecycle["reasons"]


def test_restore_write_ready_respects_an_active_hold() -> None:
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.runs[0].update(status="in_progress", conclusion=None)
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.lifecycle["action"] == "draft"
    assert fake.pull["draft"]
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.timeline = [
        _draft(
            uid=BOT_ID,
            login="github-actions[bot]",
            created_at="2026-09-05T12:05:00Z",
            eid=3,
        ),
        _draft(created_at="2100-01-01T00:00:00Z", eid=4),
    ]
    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.lifecycle["action"] == "unchanged"
    assert "draft held by maintainer" in second.lifecycle["reasons"]
    assert fake.transitions == [True]
    assert fake.pull["draft"]


def test_hold_appearing_at_the_final_check_blocks_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    calls: list[int] = []

    def staged_hold(api, repo, number, *, guard_user_id):
        calls.append(1)
        if len(calls) == 1:
            return "ok", None
        return "ok", DraftHold(
            MAINTAINER_ID, "maintainer", "2100-01-01T00:00:00Z"
        )

    monkeypatch.setattr(a38_guard, "load_draft_hold", staged_hold)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert len(calls) >= 2
    assert result.lifecycle["action"] == "unchanged"
    assert "draft held by maintainer" in result.lifecycle["reasons"]
    assert fake.transitions == []
    assert fake.pull["draft"]


def test_pending_at_the_final_check_blocks_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    calls: list[int] = []

    def staged_hold(api, repo, number, *, guard_user_id):
        calls.append(1)
        if len(calls) == 1:
            return "ok", None
        return "pending", None

    monkeypatch.setattr(a38_guard, "load_draft_hold", staged_hold)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert len(calls) >= 2
    assert result.lifecycle["action"] == "unchanged"
    assert "draft hold pending" in result.lifecycle["reasons"]
    assert fake.transitions == []
    assert fake.pull["draft"]


def test_a_review_record_before_the_hold_does_not_release_it() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    # The injected first-run review record for this head is created in 2099.
    fake.timeline = [_draft(created_at="2100-01-01T00:00:00Z")]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "unchanged"
    assert "draft held by maintainer" in result.lifecycle["reasons"]
    assert fake.transitions == []
    assert fake.pull["draft"]


def test_guard_draft_survives_an_unreadable_hold_and_restores() -> None:
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.runs[0].update(status="in_progress", conclusion=None)
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.lifecycle["action"] == "draft"
    assert fake.pull["draft"]
    assert fake.transitions == [True]

    fake.timeline_404 = True
    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.lifecycle["action"] == "unchanged"
    assert fake.pull["draft"]
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert len(bodies) == 1
    assert '"phase": "held"' in bodies[0]
    assert '"guard_drafted": true' in bodies[0]

    fake.timeline_404 = False
    fake.timeline = [
        _ready(created_at="2101-01-01T00:00:00Z", eid=2),
        _draft(
            uid=BOT_ID,
            login="github-actions[bot]",
            created_at="2102-01-01T00:00:00Z",
            eid=3,
        ),
    ]
    fake.permissions["maintainer"] = {
        "permission": "admin",
        "user": {"id": MAINTAINER_ID, "login": "maintainer", "type": "User"},
    }
    third = reconcile_pull(fake.api(), REPO, 1)
    assert third.lifecycle["action"] == "ready"
    assert fake.transitions[-1] is False
    assert not fake.pull["draft"]


def test_a_person_draft_held_record_does_not_carry_guard_provenance() -> None:
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.pull["draft"] = True
    fake.runs[0].update(status="in_progress", conclusion=None)
    fake.timeline = [_draft(created_at="2100-01-01T00:00:00Z")]
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.lifecycle["action"] == "unchanged"
    assert fake.pull["draft"]
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert len(bodies) == 1
    assert '"phase": "held"' in bodies[0]
    assert '"guard_drafted": false' in bodies[0]

    # The person's ready releases the hold; the previous held record still
    # must not read as a guard draft, so Ready is not restored.
    fake.timeline = [
        _draft(created_at="2100-01-01T00:00:00Z", eid=1),
        _ready(created_at="2101-01-01T00:00:00Z", eid=2),
        _draft(
            uid=BOT_ID,
            login="github-actions[bot]",
            created_at="2102-01-01T00:00:00Z",
            eid=3,
        ),
    ]
    fake.permissions["maintainer"] = {
        "permission": "admin",
        "user": {"id": MAINTAINER_ID, "login": "maintainer", "type": "User"},
    }
    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.write_ready
    assert second.lifecycle["action"] == "unchanged"
    assert fake.pull["draft"]
    assert fake.transitions == []


def test_dry_run_reports_the_hold_without_writing() -> None:
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.timeline = [_draft(created_at="2100-01-01T00:00:00Z")]
    result = reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    assert result.lifecycle["action"] == "unchanged"
    assert "draft held by maintainer" in result.lifecycle["reasons"]
    assert fake.transitions == []
    bodies = [c["body"] for c in fake.comments if "PR-GUARD:LIFECYCLE:v1" in c.get("body", "")]
    assert bodies == []
