from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import agent_core.app as app_module
from agent_core.db import Store, dumps
from agent_core.github import FakeGitHub, GitHubError, RealGitHub, valid_repository_name
from tests.conftest import pair_device, sign_in
from tests.test_agent_pair_control import _pair_as
from tests.test_newest_first import _insert_replica


ASSIGNMENT = {
    "agent": "helperbot",
    "repo": "Acme/Widget",
    "title": "Investigate the failure",
    "body": "Find the root cause and open a draft pull request.",
    "deviation": {"declared": False},
}
REQUEST_ID = "12345678-1234-1234-1234-123456789abc"


def _assignment_fingerprint(payload: dict[str, Any] = ASSIGNMENT) -> str:
    return app_module._request_fingerprint(
        {
            "repo": payload["repo"].casefold(),
            "title": payload["title"],
            "body": payload["body"],
            "agent": payload["agent"].lower(),
            "deviation": payload["deviation"],
        }
    )


def _mark_request_done(
    store: Store,
    actor: str,
    activity_type: str,
    request_id: str,
    owner: str,
) -> None:
    updated = store.execute(
        "UPDATE request_reservation SET status = 'done' "
        "WHERE actor = ? AND activity_type = ? AND request_id = ? "
        "AND status = 'pending' AND owner = ?",
        (actor, activity_type, request_id, owner),
    )
    assert updated.rowcount == 1


class _SlowByteStream(httpx.SyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        clock: list[float],
        seconds_per_chunk: float,
    ) -> None:
        self._chunks = chunks
        self._clock = clock
        self._seconds_per_chunk = seconds_per_chunk

    def __iter__(self):
        for chunk in self._chunks:
            self._clock[0] += self._seconds_per_chunk
            yield chunk


class _BlockingByteStream(httpx.SyncByteStream):
    def __init__(self, wait_timeout: float = 1.0) -> None:
        self.closed = threading.Event()
        self._wait_timeout = wait_timeout

    def __iter__(self):
        closed = self.closed.wait(timeout=self._wait_timeout)
        assert closed, "response body was not closed at its deadline"
        return iter(())

    def close(self) -> None:
        self.closed.set()


def test_real_github_error_exposes_http_status(cfg) -> None:
    github = RealGitHub(
        cfg,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                401, json={"message": "Bad credentials"}, request=request
            )
        ),
    )

    with pytest.raises(GitHubError) as raised:
        github.issue("expired-token", "Acme/Widget", 1)

    assert raised.value.status_code == 401


def test_real_github_rejects_invalid_json(cfg) -> None:
    github = RealGitHub(
        cfg,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b"invalid JSON", request=request
            )
        ),
    )

    with pytest.raises(
        GitHubError, match="GitHub request failed: HTTP 200 returned invalid JSON"
    ) as raised:
        github.issue("tok-alice", "Acme/Widget", 1)

    assert raised.value.status_code == 200


@pytest.mark.parametrize(
    ("method", "args", "status", "body", "message"),
    [
        (
            "create_issue",
            ("tok-alice", "Acme/Widget", "Title", "Body", ["helperbot"]),
            201,
            [],
            "GitHub issue creation returned an invalid response",
        ),
        (
            "recent_issues_by_creator",
            ("tok-alice", "Acme/Widget", "alice"),
            200,
            {},
            "GitHub recent issue lookup returned an invalid response",
        ),
        (
            "repository",
            ("tok-alice", "Acme/Widget"),
            200,
            [],
            "GitHub repository lookup returned an invalid response",
        ),
        (
            "issue",
            ("tok-alice", "Acme/Widget", 1),
            200,
            [],
            "GitHub issue lookup returned an invalid response",
        ),
        (
            "add_assignees",
            ("tok-alice", "Acme/Widget", 1, ["helperbot"]),
            200,
            [],
            "GitHub assignee update returned an invalid response",
        ),
        (
            "create_issue_comment",
            ("tok-alice", "Acme/Widget", 1, "Reply"),
            201,
            [],
            "GitHub comment creation returned an invalid response",
        ),
    ],
)
def test_real_github_rejects_wrong_response_types(
    cfg,
    method: str,
    args: tuple[object, ...],
    status: int,
    body: object,
    message: str,
) -> None:
    github = RealGitHub(
        cfg,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body, request=request)
        ),
    )

    with pytest.raises(GitHubError, match=message):
        getattr(github, method)(*args)


def test_owner_creates_assignment_and_web_activity(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert response.status_code == 201, response.text
    assert response.json() == {
        "repo": "Acme/Widget",
        "number": 1,
        "url": "https://github.test/Acme/Widget/issues/1",
        "agent": "helperbot",
        "actor": "alice",
    }
    github: FakeGitHub = agent_hub.app.state.hub.github
    created = next(call for call in github.issue_calls if call["operation"] == "create_issue")
    assert created["token"] == "tok-alice"
    assert created["assignees"] == ["helperbot"]
    assert created["body"].endswith("Assigned through http://testserver by @alice.")
    state = agent_hub.get("/api/state").json()
    activity = next(row for row in state["activity"] if row.get("type") == "assignment.created")
    assert activity["payload"]["agent"] == "helperbot"
    assert activity["payload"]["actor"] == "alice"
    assert activity["payload"]["issue_key"] == "Acme/Widget#1"
    assert activity["payload"]["url"] == "https://github.com/Acme/Widget/issues/1"
    assert activity["payload"]["deviation"] == {
        "declared": False,
        "stated_by": "alice",
    }
    replica = agent_hub.app.state.hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'activity' AND row_id = ?",
        (activity["id"],),
    )
    assert replica["origin_device_id"] == "web:alice"


def test_assignment_stores_github_canonical_repo_case(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.canonical_repos["acme/widget"] = "Acme/Widget"
    assignment = dict(ASSIGNMENT, repo="acme/widget")

    response = agent_hub.post("/api/assignments", json=assignment)

    assert response.status_code == 201, response.text
    assert response.json()["repo"] == "Acme/Widget"
    created = next(call for call in github.issue_calls if call["operation"] == "create_issue")
    assert created["repo"] == "acme/widget"
    state = agent_hub.get("/api/state").json()
    activity = next(row for row in state["activity"] if row.get("type") == "assignment.created")
    assert activity["payload"]["repo"] == "Acme/Widget"
    assert activity["payload"]["issue_key"] == "Acme/Widget#1"

    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac03"
    token = pair_device(agent_hub, "code-alice", device_id)
    query = agent_hub.post(
        "/sync/query",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "match": {
                "type": "assignment.created",
                "payload.issue_key": "Acme/Widget#1",
            }
        },
    )
    assert query.status_code == 200, query.text
    assert [row["row_id"] for row in query.json()["rows"]] == [activity["id"]]


@pytest.mark.parametrize(
    ("present", "deviation"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(True, None, id="null"),
        pytest.param(True, {}, id="empty-object"),
        pytest.param(True, {"declared": True}, id="declared-true"),
        pytest.param(True, {"declared": False, "extra": False}, id="extra-key"),
        pytest.param(True, {"declared": 0}, id="non-boolean-false"),
        pytest.param(True, [], id="non-object"),
    ],
)
def test_assignment_requires_explicit_no_deviation(
    agent_hub: TestClient, present: bool, deviation: object
) -> None:
    sign_in(agent_hub, "code-alice")
    payload = dict(ASSIGNMENT)
    if present:
        payload["deviation"] = deviation
    else:
        payload.pop("deviation")

    response = agent_hub.post("/api/assignments", json=payload)

    assert response.status_code == 400
    assert response.json() == {
        "detail": 'deviation must be {"declared": false}; a deviation needs an explicit grant on the issue'
    }


def test_assignment_fingerprint_includes_deviation() -> None:
    expected = {
        "repo": "acme/widget",
        "title": ASSIGNMENT["title"],
        "body": ASSIGNMENT["body"],
        "agent": ASSIGNMENT["agent"],
        "deviation": {"declared": False},
    }
    without_deviation = dict(expected)
    without_deviation.pop("deviation")

    assert _assignment_fingerprint() == app_module._request_fingerprint(expected)
    assert _assignment_fingerprint() != app_module._request_fingerprint(without_deviation)


def test_assignment_authorization_and_missing_token(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-bob")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 403

    agent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac01"
    agent_token = _pair_as(agent_hub, "code-alice", agent_id, "helperbot")
    denied = agent_hub.post(
        "/api/assignments",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=ASSIGNMENT,
    )
    assert denied.status_code == 403

    owner_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac02"
    owner_token = pair_device(agent_hub, "code-alice", owner_id)
    hub = agent_hub.app.state.hub
    hub.github_tokens.pop("alice", None)
    hub.store.delete_oauth_token("alice")
    missing = agent_hub.post(
        "/api/assignments",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=ASSIGNMENT,
    )
    assert missing.status_code == 409
    assert "sign in on the website once" in missing.json()["detail"]


@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("POST", "/api/assignments", ASSIGNMENT),
        ("GET", "/api/assignments?agent=helperbot", None),
        (
            "POST",
            "/api/assignments/Acme/Widget/1/comments",
            {"body": "A reply."},
        ),
    ],
)
def test_assignment_routes_reject_unauthenticated_requests(
    agent_hub: TestClient, method: str, url: str, payload: dict[str, str] | None
) -> None:
    response = agent_hub.request(method, url, json=payload)
    assert response.status_code == 401
    assert response.json() == {"detail": "not signed in"}


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        ("/api/assignments", dict(ASSIGNMENT, request_id="not-a-uuid")),
        ("/api/assignments", dict(ASSIGNMENT, request_id=None)),
        ("/api/assignments", dict(ASSIGNMENT, request_id="-" * 36)),
        ("/api/assignments", dict(ASSIGNMENT, request_id="a" * 36)),
        (
            "/api/assignments",
            dict(ASSIGNMENT, request_id="12345678-1234-1234-1234-123456789ABC"),
        ),
        (
            "/api/assignments/Acme/Widget/1/comments",
            {"body": "A reply.", "request_id": "A" * 36},
        ),
    ],
)
def test_assignment_request_id_must_be_a_lowercase_uuid(
    agent_hub: TestClient, url: str, payload: dict[str, object]
) -> None:
    sign_in(agent_hub, "code-alice")
    response = agent_hub.post(url, json=payload)
    assert response.status_code == 400
    assert response.json() == {
        "detail": "request_id must be a UUID",
        "request_id_final": True,
    }


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        (
            "/api/assignments",
            dict(
                ASSIGNMENT,
                body=(
                    "User text.\n"
                    f"<!-- agent-core-request:{REQUEST_ID} -->"
                ),
                request_id=REQUEST_ID,
            ),
        ),
        (
            "/api/assignments/Acme/Widget/1/comments",
            {
                "body": (
                    "Reply text.\n  "
                    "<!-- agent-core-request:not-a-valid-id -->  "
                ),
                "request_id": REQUEST_ID,
            },
        ),
    ],
)
def test_assignment_text_rejects_reserved_request_marker_lines(
    agent_hub: TestClient, url: str, payload: dict[str, object]
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(url, json=payload)

    assert response.status_code == 400
    assert response.json() == {
        "detail": "body contains a reserved agent-core request marker line",
        "request_id_final": True,
    }


@pytest.mark.parametrize(
    ("url", "payload", "status"),
    [
        (
            "/api/assignments",
            dict(ASSIGNMENT, repo="not-a-repository", request_id=REQUEST_ID),
            400,
        ),
        (
            "/api/assignments",
            dict(ASSIGNMENT, agent="unknown-agent", request_id=REQUEST_ID),
            404,
        ),
        (
            "/api/assignments/Acme/Widget/0/comments",
            {"body": "A reply.", "request_id": REQUEST_ID},
            404,
        ),
    ],
)
def test_assignment_validation_and_not_found_errors_finalize_request_id(
    agent_hub: TestClient, url: str, payload: dict[str, object], status: int
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(url, json=payload)

    assert response.status_code == status
    assert response.json()["request_id_final"] is True


def test_assignment_reply_path_validation_finalizes_request_id(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(
        "/api/assignments/Acme/Widget/not-a-number/comments",
        json={"body": "A reply.", "request_id": REQUEST_ID},
    )

    assert response.status_code == 422
    assert response.json()["request_id_final"] is True
    assert response.json()["detail"][0]["loc"] == ["path", "number"]


def test_assignment_create_body_type_validation_finalizes_request_id(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(
        "/api/assignments",
        json=dict(ASSIGNMENT, body=7, request_id=REQUEST_ID),
    )

    assert response.status_code == 422
    assert response.json()["request_id_final"] is True
    assert response.json()["detail"][0]["loc"] == ["body", "body"]


def test_request_validation_request_id_field_is_assignment_route_only(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(
        "/api/session/control",
        json={"request_id": REQUEST_ID},
    )

    assert response.status_code == 422
    assert "request_id_final" not in response.json()


def test_assignment_non_json_validation_has_no_request_id_final(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(
        "/api/assignments",
        content="{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "request_id_final" not in response.json()


def test_assignment_forbidden_error_finalizes_request_id(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-bob")

    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 403
    assert response.json()["request_id_final"] is True


def test_assignment_create_request_id_replays_recorded_success(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)

    created = agent_hub.post("/api/assignments", json=payload)
    replayed = agent_hub.post(
        "/api/assignments", json=dict(payload, repo="acme/widget")
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    # The replay answers with the stored canonical issue link.
    assert replayed.json() == {**created.json(), "url": "https://github.com/Acme/Widget/issues/1"}
    calls = [call for call in github.issue_calls if call["operation"] == "create_issue"]
    assert len(calls) == 1
    assert calls[0]["body"].splitlines()[-1] == (
        f"<!-- agent-core-request:{REQUEST_ID} -->"
    )
    state = agent_hub.get("/api/state").json()
    activity = next(row for row in state["activity"] if row.get("type") == "assignment.created")
    assert activity["payload"]["request_id"] == REQUEST_ID
    assert activity["payload"]["request_fingerprint"] == _assignment_fingerprint()
    reservation = agent_hub.app.state.hub.store.query_one(
        "SELECT owner, status, fingerprint FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert reservation is not None
    assert reservation["status"] == "done"
    assert reservation["fingerprint"] == _assignment_fingerprint()
    assert len(reservation["owner"]) == 32
    assert set(reservation["owner"]) <= set("0123456789abcdef")
    events = agent_hub.app.state.hub.store.query(
        "SELECT payload FROM ledger_event WHERE origin_device_id = ? "
        "AND table_name = 'activity'",
        ("web:alice",),
    )
    assert len(events) == 1
    assert REQUEST_ID in events[0]["payload"]


def test_assignment_create_takeover_reauth_recovers_issue_after_timeout(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    create_issue = github.create_issue

    def create_then_timeout(*args, **kwargs):
        create_issue(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", create_then_timeout)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)
    failed = agent_hub.post("/api/assignments", json=payload)
    monkeypatch.setattr(github, "create_issue", create_issue)
    possible_write = agent_hub.app.state.hub.store.query_one(
        "SELECT prior_write FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert possible_write is not None
    assert possible_write["prior_write"] == 1
    agent_hub.app.state.hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    recent_issues = github.recent_issues_by_creator

    def expired_token(*_args, **_kwargs):
        raise GitHubError("GitHub request failed: HTTP 401", status_code=401)

    monkeypatch.setattr(github, "recent_issues_by_creator", expired_token)
    reauth = agent_hub.post("/api/assignments", json=payload)
    without_token = agent_hub.post("/api/assignments", json=payload)
    pending_reservation = agent_hub.app.state.hub.store.query_one(
        "SELECT status, lease_until, prior_write FROM request_reservation "
        "WHERE request_id = ?",
        (REQUEST_ID,),
    )
    monkeypatch.setattr(github, "recent_issues_by_creator", recent_issues)
    sign_in(agent_hub, "code-alice")
    recovered = agent_hub.post("/api/assignments", json=payload)

    assert failed.status_code == 502
    assert reauth.status_code == 409
    assert reauth.json() == {
        "detail": "sign in on the website again",
        "request_id_final": False,
    }
    assert without_token.status_code == 409
    assert without_token.json() == {
        "detail": "sign in on the website once so the hub holds your GitHub token",
        "request_id_final": False,
    }
    assert pending_reservation is not None
    assert dict(pending_reservation) == {
        "status": "pending",
        "lease_until": "",
        "prior_write": 1,
    }
    assert recovered.status_code == 201
    assert recovered.json()["number"] == 1
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1
    assert len([row for row in github.issues.values() if row["number"] == 1]) == 1


def test_assignment_issue_recovery_searches_beyond_first_page_and_matches_content(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_issue = github.create_issue

    def create_then_timeout(*args, **kwargs):
        create_issue(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", create_then_timeout)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)
    assert agent_hub.post("/api/assignments", json=payload).status_code == 502
    monkeypatch.setattr(github, "create_issue", create_issue)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    reservation = hub.store.query_one(
        "SELECT created_at FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert reservation is not None
    created = dict(github.issues[("Acme/Widget", 1)])

    def paginated_recovery(
        _token: str,
        _repo: str,
        _creator: str,
        timeout: float | None = None,
        since: str | None = None,
        stop_at=None,
    ) -> list[dict[str, object]]:
        assert timeout is not None and 0 < timeout <= app_module.ASSIGNMENT_RECOVERY_SECONDS
        assert since == reservation["created_at"]
        marker = app_module._request_marker(REQUEST_ID)
        decoys = [
            {
                **created,
                "number": number,
                "body": f"unrelated-{number}\n\n{marker}",
                "user": {"login": "mallory" if number == 35 else "alice"},
            }
            for number in range(2, 37)
        ]
        rows = decoys + [created]
        assert stop_at is not None and stop_at(created)
        return rows

    monkeypatch.setattr(github, "recent_issues_by_creator", paginated_recovery)
    recovered = agent_hub.post("/api/assignments", json=payload)

    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["number"] == 1
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1


def test_assignment_create_reauth_after_write_keeps_id_and_reuses_issue(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    issue = github.issue
    expired = True

    def expire_after_create(*args, **kwargs):
        nonlocal expired
        if expired:
            expired = False
            raise GitHubError("GitHub request failed: HTTP 401", status_code=401)
        return issue(*args, **kwargs)

    monkeypatch.setattr(github, "issue", expire_after_create)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)

    failed = agent_hub.post("/api/assignments", json=payload)
    assert failed.status_code == 409
    assert failed.json() == {
        "detail": "sign in on the website again",
        "url": "https://github.test/Acme/Widget/issues/1",
        "number": 1,
        "request_id_final": False,
    }

    sign_in(agent_hub, "code-alice")
    recovered = agent_hub.post("/api/assignments", json=payload)

    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["number"] == 1
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1


def test_assignment_reply_request_id_replays_recorded_success(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    payload = {"body": "A reply.", "request_id": REQUEST_ID}

    created = agent_hub.post("/api/assignments/Acme/Widget/1/comments", json=payload)
    replayed = agent_hub.post("/api/assignments/acme/widget/1/comments", json=payload)

    assert created.status_code == 201
    assert replayed.status_code == 200
    calls = [
        call
        for call in github.issue_calls
        if call["operation"] == "create_issue_comment"
    ]
    assert len(calls) == 1
    assert calls[0]["body"].splitlines()[-1] == (
        f"<!-- agent-core-request:{REQUEST_ID} -->"
    )
    state = agent_hub.get("/api/state").json()
    activity = next(row for row in state["activity"] if row.get("type") == "assignment.reply")
    assert activity["payload"]["request_id"] == REQUEST_ID
    assert activity["payload"]["request_fingerprint"] == app_module._request_fingerprint(
        {"owner": "acme", "name": "widget", "number": 1, "body": "A reply."}
    )
    assert replayed.json() == {"url": activity["payload"]["url"]}


def test_assignment_reply_takeover_reauth_recovers_comment_after_timeout(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    create_comment = github.create_issue_comment

    def comment_then_timeout(*args, **kwargs):
        create_comment(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue_comment", comment_then_timeout)
    payload = {"body": "A reply.", "request_id": REQUEST_ID}
    failed = agent_hub.post("/api/assignments/Acme/Widget/1/comments", json=payload)
    monkeypatch.setattr(github, "create_issue_comment", create_comment)
    possible_write = agent_hub.app.state.hub.store.query_one(
        "SELECT prior_write FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert possible_write is not None
    assert possible_write["prior_write"] == 1
    agent_hub.app.state.hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    issue = github.issue

    def expired_token(*_args, **_kwargs):
        raise GitHubError("GitHub request failed: HTTP 401", status_code=401)

    monkeypatch.setattr(github, "issue", expired_token)
    reauth = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    )
    without_token = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    )
    pending_reservation = agent_hub.app.state.hub.store.query_one(
        "SELECT status, lease_until, prior_write FROM request_reservation "
        "WHERE request_id = ?",
        (REQUEST_ID,),
    )
    monkeypatch.setattr(github, "issue", issue)
    sign_in(agent_hub, "code-alice")
    recovered = agent_hub.post("/api/assignments/Acme/Widget/1/comments", json=payload)

    assert failed.status_code == 502
    assert reauth.status_code == 409
    assert reauth.json() == {
        "detail": "sign in on the website again",
        "request_id_final": False,
    }
    assert without_token.status_code == 409
    assert without_token.json() == {
        "detail": "sign in on the website once so the hub holds your GitHub token",
        "request_id_final": False,
    }
    assert pending_reservation is not None
    assert dict(pending_reservation) == {
        "status": "pending",
        "lease_until": "",
        "prior_write": 1,
    }
    assert recovered.status_code == 201
    assert len(
        [
            call
            for call in github.issue_calls
            if call["operation"] == "create_issue_comment"
        ]
    ) == 1
    assert len(github.comments[("Acme/Widget", 1)]) == 1


def test_assignment_comment_recovery_searches_beyond_first_page_and_matches_content(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_comment = github.create_issue_comment

    def comment_then_timeout(*args, **kwargs):
        create_comment(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue_comment", comment_then_timeout)
    payload = {"body": "A reply.", "request_id": REQUEST_ID}
    assert agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    ).status_code == 502
    monkeypatch.setattr(github, "create_issue_comment", create_comment)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    reservation = hub.store.query_one(
        "SELECT created_at FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert reservation is not None
    created = dict(github.comments[("Acme/Widget", 1)][0])

    def paginated_recovery(
        _token: str,
        _repo: str,
        _number: int,
        timeout: float | None = None,
        since: str | None = None,
        stop_at=None,
    ) -> list[dict[str, object]]:
        assert timeout is not None and 0 < timeout <= app_module.ASSIGNMENT_RECOVERY_SECONDS
        assert since == reservation["created_at"]
        marker = app_module._request_marker(REQUEST_ID)
        decoys = [
            {
                **created,
                "id": comment_id,
                "body": f"unrelated-{comment_id}\n\n{marker}",
                "user": {"login": "mallory" if comment_id == 101 else "alice"},
            }
            for comment_id in range(2, 103)
        ]
        rows = decoys + [created]
        assert stop_at is not None and stop_at(created)
        return rows

    monkeypatch.setattr(github, "issue_comments", paginated_recovery)
    recovered = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    )

    assert recovered.status_code == 201, recovered.text
    assert len(
        [
            call
            for call in github.issue_calls
            if call["operation"] == "create_issue_comment"
        ]
    ) == 1


def test_assignment_recovery_error_is_retryable_and_does_not_write(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_issue = github.create_issue

    def create_then_timeout(*args, **kwargs):
        create_issue(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", create_then_timeout)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)
    assert agent_hub.post("/api/assignments", json=payload).status_code == 502
    monkeypatch.setattr(github, "create_issue", create_issue)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )

    def incomplete(*_args, **_kwargs):
        raise GitHubError("GitHub recent issue lookup did not complete within page limit")

    monkeypatch.setattr(github, "recent_issues_by_creator", incomplete)
    response = agent_hub.post("/api/assignments", json=payload)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1
    assert hub.store.query(
        "SELECT * FROM row_replica WHERE table_name = 'activity' "
        "AND origin_device_id = 'web:alice'"
    ) == []


def test_assignment_reply_recovery_stream_deadline_does_not_start_write(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_comment = github.create_issue_comment

    def comment_then_timeout(*args, **kwargs):
        create_comment(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue_comment", comment_then_timeout)
    payload = {"body": "A reply.", "request_id": REQUEST_ID}
    assert agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    ).status_code == 502
    monkeypatch.setattr(github, "create_issue_comment", create_comment)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    clock = [0.0]
    body = dumps(github.comments[("Acme/Widget", 1)]).encode()

    def request(_request: httpx.Request) -> httpx.Response:
        middle = len(body) // 2
        return httpx.Response(
            200,
            stream=_SlowByteStream(
                [body[:middle], body[middle:]], clock, 6.0
            ),
        )

    recovery_github = RealGitHub(
        hub.cfg,
        transport=httpx.MockTransport(request),
    )
    monkeypatch.setattr(github, "issue_comments", recovery_github.issue_comments)
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert len(
        [
            call
            for call in github.issue_calls
            if call["operation"] == "create_issue_comment"
        ]
    ) == 1
    assert len(github.comments[("Acme/Widget", 1)]) == 1


def test_assignment_request_is_busy_without_calling_github(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    assert hub.store.reserve_request(
        "alice",
        "assignment.created",
        REQUEST_ID,
        _assignment_fingerprint(),
        "existing-owner",
        "2026-11-05T10:00:00Z",
        "9999-01-01T00:00:00Z",
    ) == "reserved"
    calls_before = list(hub.github.issue_calls)

    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert hub.github.issue_calls == calls_before


def test_assignment_create_rejects_reserved_id_for_different_content_without_github(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    assert hub.store.reserve_request(
        "alice",
        "assignment.created",
        REQUEST_ID,
        _assignment_fingerprint(dict(ASSIGNMENT, title="Different title")),
        "existing-owner",
        "2026-11-05T10:00:00Z",
        "9999-01-01T00:00:00Z",
    ) == "reserved"
    calls_before = list(hub.github.issue_calls)

    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request id was used for different content; reload the assignment list",
        "request_id_final": True,
    }
    assert hub.github.issue_calls == calls_before


def test_assignment_reply_rejects_completed_id_for_different_content_without_github(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    first = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "First reply.", "request_id": REQUEST_ID},
    )
    assert first.status_code == 201, first.text
    github: FakeGitHub = agent_hub.app.state.hub.github
    calls_before = list(github.issue_calls)

    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "Changed reply.", "request_id": REQUEST_ID},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request id was used for different content; reload the assignment list",
        "request_id_final": True,
    }
    assert github.issue_calls == calls_before


def test_completed_assignment_request_without_replay_row_is_conflict(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    assert hub.store.reserve_request(
        "alice",
        "assignment.created",
        REQUEST_ID,
        _assignment_fingerprint(),
        "existing-owner",
        "2026-11-05T10:00:00Z",
        "2026-11-05T10:02:00Z",
    ) == "reserved"
    _mark_request_done(
        hub.store,
        "alice",
        "assignment.created",
        REQUEST_ID,
        "existing-owner",
    )
    calls_before = list(hub.github.issue_calls)

    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request already completed; reload the assignment list",
        "request_id_final": True,
    }
    assert hub.github.issue_calls == calls_before


def test_assignment_expired_request_reservation_is_taken_over(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    assert hub.store.reserve_request(
        "alice",
        "assignment.created",
        REQUEST_ID,
        _assignment_fingerprint(),
        "expired-owner",
        "2000-01-01T00:00:00Z",
        "2000-01-01T00:02:00Z",
    ) == "reserved"

    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 201, response.text
    reservation = hub.store.query_one(
        "SELECT status FROM request_reservation WHERE actor = ? "
        "AND activity_type = ? AND request_id = ?",
        ("alice", "assignment.created", REQUEST_ID),
    )
    assert reservation is not None
    assert reservation["status"] == "done"


def test_assignment_takeover_stops_old_owner_before_github_write(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    original_renew = hub.store.renew_request

    def take_over_before_renew(*args):
        reservation = hub.store.query_one(
            "SELECT owner FROM request_reservation WHERE request_id = ?", (REQUEST_ID,)
        )
        assert reservation is not None
        assert reservation["owner"] != "new-owner"
        hub.store.execute(
            "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
            ("2000-01-01T00:00:00Z", REQUEST_ID),
        )
        assert hub.store.reserve_request(
            "alice",
            "assignment.created",
            REQUEST_ID,
            _assignment_fingerprint(),
            "new-owner",
            "2026-11-05T10:00:00Z",
            "9999-01-01T00:00:00Z",
        ) == "taken_over"
        return original_renew(*args)

    # Another request takes the lease over right before this one renews it for the write.
    monkeypatch.setattr(hub.store, "renew_request", take_over_before_renew)
    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    reservation = hub.store.query_one(
        "SELECT owner, status FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert reservation is not None
    assert dict(reservation) == {"owner": "new-owner", "status": "pending"}
    assert not [
        call for call in github.issue_calls if call["operation"] == "create_issue"
    ]


def test_assignment_takeover_after_github_write_rejects_old_owner_without_event(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    original_issue = github.issue
    published: list[dict[str, object]] = []

    async def capture_publish(event: dict[str, object]) -> None:
        published.append(event)

    def take_over_after_write(*args):
        reservation = hub.store.query_one(
            "SELECT fingerprint FROM request_reservation WHERE request_id = ?",
            (REQUEST_ID,),
        )
        assert reservation is not None
        hub.store.execute(
            "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
            ("2000-01-01T00:00:00Z", REQUEST_ID),
        )
        assert hub.store.reserve_request(
            "alice",
            "assignment.created",
            REQUEST_ID,
            reservation["fingerprint"],
            "new-owner",
            "2026-11-05T10:00:00Z",
            "9999-01-01T00:00:00Z",
        ) == "taken_over"
        return original_issue(*args)

    monkeypatch.setattr(github, "issue", take_over_after_write)
    monkeypatch.setattr(hub, "publish", capture_publish)
    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1
    assert hub.store.query(
        "SELECT * FROM ledger_event WHERE origin_device_id = ? "
        "AND table_name = 'activity'",
        ("web:alice",),
    ) == []
    assert hub.store.query(
        "SELECT * FROM row_replica WHERE origin_device_id = ? "
        "AND table_name = 'activity'",
        ("web:alice",),
    ) == []
    assert published == []
    reservation = hub.store.query_one(
        "SELECT owner, status FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    assert reservation is not None
    assert dict(reservation) == {"owner": "new-owner", "status": "pending"}


def test_assignment_reply_renew_failure_stops_before_github_write(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    monkeypatch.setattr(hub.store, "renew_request", lambda *_args: False)

    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "A reply.", "request_id": REQUEST_ID},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert not [
        call
        for call in github.issue_calls
        if call["operation"] == "create_issue_comment"
    ]


def test_assignment_renews_again_before_add_assignees(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    github.omit_assignees_on_create = True
    original_renew = hub.store.renew_request
    renew_calls = 0

    def lose_reservation_before_second_write(*args) -> bool:
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 2:
            return False
        return original_renew(*args)

    monkeypatch.setattr(hub.store, "renew_request", lose_reservation_before_second_write)
    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == 409
    assert renew_calls == 2
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1
    assert not [
        call for call in github.issue_calls if call["operation"] == "add_assignees"
    ]


def test_assignment_reply_releases_reservation_after_prewrite_denial(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    original_issue = github.issue
    issue_calls = 0

    def deny_once(token: str, repo: str, number: int) -> dict:
        nonlocal issue_calls
        issue_calls += 1
        issue = original_issue(token, repo, number)
        if issue_calls == 1:
            issue["assignees"] = []
        return issue

    monkeypatch.setattr(github, "issue", deny_once)
    payload = {"body": "A reply.", "request_id": REQUEST_ID}

    denied = agent_hub.post("/api/assignments/Acme/Widget/1/comments", json=payload)
    retried = agent_hub.post("/api/assignments/Acme/Widget/1/comments", json=payload)

    assert denied.status_code == 403
    assert retried.status_code == 201, retried.text
    assert issue_calls == 2


def test_assignment_reply_keeps_prior_write_reservation_after_ownership_denial(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_comment = github.create_issue_comment

    def comment_then_timeout(*args, **kwargs):
        create_comment(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue_comment", comment_then_timeout)
    payload = {"body": "A reply.", "request_id": REQUEST_ID}
    assert agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    ).status_code == 502
    monkeypatch.setattr(github, "create_issue_comment", create_comment)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    issue = github.issue
    deny_ownership = True

    def deny_once(*args, **kwargs):
        nonlocal deny_ownership
        row = issue(*args, **kwargs)
        if deny_ownership:
            deny_ownership = False
            row["assignees"] = []
        return row

    monkeypatch.setattr(github, "issue", deny_once)
    denied = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    )
    reservation = hub.store.query_one(
        "SELECT status, lease_until, prior_write FROM request_reservation "
        "WHERE request_id = ?",
        (REQUEST_ID,),
    )
    recovered = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    )

    assert denied.status_code == 403
    assert denied.json()["request_id_final"] is False
    assert reservation is not None
    assert dict(reservation) == {
        "status": "pending",
        "lease_until": "",
        "prior_write": 1,
    }
    assert recovered.status_code == 201, recovered.text
    assert len(
        [
            call
            for call in github.issue_calls
            if call["operation"] == "create_issue_comment"
        ]
    ) == 1
    assert len(github.comments[("Acme/Widget", 1)]) == 1


def test_assignment_create_keeps_prior_write_reservation_after_renew_failure(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    github.omit_assignees_on_create = True
    create_issue = github.create_issue

    def create_then_timeout(*args, **kwargs):
        create_issue(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", create_then_timeout)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)
    assert agent_hub.post("/api/assignments", json=payload).status_code == 502
    monkeypatch.setattr(github, "create_issue", create_issue)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    renew = hub.store.renew_request
    monkeypatch.setattr(hub.store, "renew_request", lambda *_args: False)

    lost = agent_hub.post("/api/assignments", json=payload)
    reservation = hub.store.query_one(
        "SELECT status, lease_until, prior_write FROM request_reservation "
        "WHERE request_id = ?",
        (REQUEST_ID,),
    )
    monkeypatch.setattr(hub.store, "renew_request", renew)
    recovered = agent_hub.post("/api/assignments", json=payload)

    assert lost.status_code == 409
    assert lost.json()["request_id_final"] is False
    assert reservation is not None
    assert dict(reservation) == {
        "status": "pending",
        "lease_until": "",
        "prior_write": 1,
    }
    assert recovered.status_code == 201, recovered.text
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1


def test_assignment_create_keeps_prior_write_reservation_after_not_found(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_issue = github.create_issue

    def create_then_timeout(*args, **kwargs):
        create_issue(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", create_then_timeout)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)
    assert agent_hub.post("/api/assignments", json=payload).status_code == 502
    monkeypatch.setattr(github, "create_issue", create_issue)
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    issue = github.issue

    def missing_issue(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="unknown issue")

    monkeypatch.setattr(github, "issue", missing_issue)
    denied = agent_hub.post("/api/assignments", json=payload)
    reservation = hub.store.query_one(
        "SELECT status, lease_until, prior_write FROM request_reservation "
        "WHERE request_id = ?",
        (REQUEST_ID,),
    )
    monkeypatch.setattr(github, "issue", issue)
    recovered = agent_hub.post("/api/assignments", json=payload)

    assert denied.status_code == 404
    assert denied.json()["request_id_final"] is False
    assert reservation is not None
    assert dict(reservation) == {
        "status": "pending",
        "lease_until": "",
        "prior_write": 1,
    }
    assert recovered.status_code == 201, recovered.text
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1


def test_recovering_create_keeps_its_lease_after_an_uncertain_write(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)

    def timeout_without_issue(*_args, **_kwargs):
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", timeout_without_issue)
    assert agent_hub.post("/api/assignments", json=payload).status_code == 502
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )
    create_calls: list[int] = []

    def count_and_timeout(*_args, **_kwargs):
        create_calls.append(1)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue", count_and_timeout)

    recovering = agent_hub.post("/api/assignments", json=payload)
    lease = hub.store.query_one(
        "SELECT lease_until FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    resubmitted = agent_hub.post("/api/assignments", json=payload)

    assert recovering.status_code == 502
    assert lease is not None and lease["lease_until"] > "2000-01-01T00:00:00Z"
    assert resubmitted.status_code == 409
    assert resubmitted.json()["detail"] == "request is still in progress; retry shortly"
    assert create_calls == [1]


@pytest.mark.parametrize(("status_code", "expected"), [(None, 409), (403, 502), (404, 502)])
def test_recovering_reply_reports_a_lasting_github_error_and_keeps_the_reservation(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch, status_code, expected: int
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    create_comment = github.create_issue_comment

    def comment_then_timeout(*args, **kwargs):
        create_comment(*args, **kwargs)
        raise GitHubError("GitHub request failed: ReadTimeout")

    monkeypatch.setattr(github, "create_issue_comment", comment_then_timeout)
    payload = {"body": "A reply.", "request_id": REQUEST_ID}
    assert agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json=payload
    ).status_code == 502
    hub.store.execute(
        "UPDATE request_reservation SET lease_until = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00Z", REQUEST_ID),
    )

    def failing_lookup(*_args, **_kwargs):
        raise GitHubError(f"GitHub request failed: HTTP {status_code}", status_code=status_code)

    monkeypatch.setattr(github, "issue_comments", failing_lookup)
    recovering = agent_hub.post("/api/assignments/Acme/Widget/1/comments", json=payload)
    reservation = hub.store.query_one(
        "SELECT status, prior_write FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )

    assert recovering.status_code == expected
    assert recovering.json()["request_id_final"] is False
    if expected == 502:
        assert f"HTTP {status_code}" in recovering.json()["detail"]
        assert recovering.json()["url"] == "https://github.com/Acme/Widget/issues/1"
        assert recovering.json()["number"] == 1
    assert dict(reservation) == {"status": "pending", "prior_write": 1}


@pytest.mark.parametrize("route", ["create", "reply"])
def test_definite_write_error_lets_an_immediate_retry_take_over(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    if route == "reply":
        assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
        name, path, payload = (
            "create_issue_comment",
            "/api/assignments/Acme/Widget/1/comments",
            {"body": "A reply.", "request_id": REQUEST_ID},
        )
    else:
        name, path, payload = "create_issue", "/api/assignments", dict(ASSIGNMENT, request_id=REQUEST_ID)
    original = getattr(github, name)

    def rejected(*_args, **_kwargs):
        raise GitHubError("GitHub request failed: HTTP 422", status_code=422)

    monkeypatch.setattr(github, name, rejected)
    first = agent_hub.post(path, json=payload)
    lease = hub.store.query_one(
        "SELECT status, lease_until FROM request_reservation WHERE request_id = ?",
        (REQUEST_ID,),
    )
    monkeypatch.setattr(github, name, original)
    retry = agent_hub.post(path, json=payload)

    assert first.status_code == 502
    assert first.json()["request_id_final"] is False
    assert dict(lease) == {"status": "pending", "lease_until": ""}
    assert retry.status_code == 201, retry.text


def test_concurrent_assignment_requests_create_one_issue(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    original_create = github.create_issue
    entered = threading.Event()
    release = threading.Event()

    def blocking_create(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(github, "create_issue", blocking_create)
    payload = dict(ASSIGNMENT, request_id=REQUEST_ID)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(agent_hub.post, "/api/assignments", json=payload)
        assert entered.wait(timeout=5)
        second = executor.submit(agent_hub.post, "/api/assignments", json=payload)
        second_response = second.result(timeout=5)
        release.set()
        first_response = first.result(timeout=5)

    assert sorted((first_response.status_code, second_response.status_code)) == [201, 409]
    busy = first_response if first_response.status_code == 409 else second_response
    assert busy.json() == {
        "detail": "request is still in progress; retry shortly",
        "request_id_final": False,
    }
    assert len(
        [call for call in github.issue_calls if call["operation"] == "create_issue"]
    ) == 1


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"repo": "not-a-repository"}, "repo must be Owner/Name"),
        ({"title": ""}, "title must be a string of length 1..256"),
        ({"title": "x" * 257}, "title must be a string of length 1..256"),
        ({"body": ""}, "body must be a string of length 1..60000"),
        ({"body": "x" * 60001}, "body must be a string of length 1..60000"),
    ],
)
def test_assignment_create_validation_errors(
    agent_hub: TestClient, changes: dict[str, str], detail: str
) -> None:
    sign_in(agent_hub, "code-alice")
    response = agent_hub.post("/api/assignments", json=dict(ASSIGNMENT, **changes))
    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("repo", ["Acme/.", "Acme/..", "./x", "a/b/c"])
def test_assignment_create_rejects_repository_dot_segments(
    agent_hub: TestClient, repo: str
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, repo=repo)
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "repo must be Owner/Name"}
    assert not valid_repository_name(repo)


@pytest.mark.parametrize(
    "url",
    [
        "/api/assignments/Acme/%2E/1/comments",
        "/api/assignments/Acme/%2E%2E/1/comments",
        "/api/assignments/%2E/x/1/comments",
        "/api/assignments/a/b/c/1/comments",
    ],
)
def test_assignment_reply_rejects_repository_dot_segments(
    agent_hub: TestClient, url: str
) -> None:
    sign_in(agent_hub, "code-alice")

    response = agent_hub.post(url, json={"body": "A reply."})

    assert response.status_code == 404


@pytest.mark.parametrize("body", ["", "x" * 60001])
def test_assignment_reply_body_length_errors(
    agent_hub: TestClient, body: str
) -> None:
    sign_in(agent_hub, "code-alice")
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": body}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "body must be a string of length 1..60000"}


@pytest.mark.parametrize(
    "call",
    [
        lambda github: github.create_issue("token", "Acme/..", "title", "body", []),
        lambda github: github.recent_issues_by_creator("token", "Acme/..", "alice"),
        lambda github: github.repository("token", "Acme/.."),
        lambda github: github.issue("token", "Acme/..", 1),
        lambda github: github.add_assignees("token", "Acme/..", 1, ["helperbot"]),
        lambda github: github.create_issue_comment("token", "Acme/..", 1, "body"),
        lambda github: github.issue_comments("token", "Acme/..", 1),
    ],
)
def test_real_github_rejects_invalid_repo_before_building_request_path(
    cfg, call
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid repository reached HTTP transport")

    github = RealGitHub(cfg, transport=httpx.MockTransport(unexpected_request))

    with pytest.raises(GitHubError, match="invalid GitHub repository name"):
        call(github)


@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("POST", "/api/assignments", dict(ASSIGNMENT, agent="unknownbot")),
        ("GET", "/api/assignments?agent=unknownbot", None),
        (
            "POST",
            "/api/assignments/Acme/Widget/0/comments",
            {"body": "A reply."},
        ),
    ],
)
def test_assignment_unknown_agent_and_issue_errors(
    agent_hub: TestClient, method: str, url: str, payload: dict[str, str] | None
) -> None:
    sign_in(agent_hub, "code-alice")
    response = agent_hub.request(method, url, json=payload)
    assert response.status_code == 404
    assert response.json()["detail"] in {"unknown agent", "unknown issue"}


@pytest.mark.parametrize("operation", ["create", "reply"])
def test_assignment_write_requires_agent_ownership(
    agent_hub: TestClient, operation: str
) -> None:
    if operation == "reply":
        sign_in(agent_hub, "code-alice")
        assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    sign_in(agent_hub, "code-bob")
    response = (
        agent_hub.post("/api/assignments", json=ASSIGNMENT)
        if operation == "create"
        else agent_hub.post(
            "/api/assignments/Acme/Widget/1/comments", json={"body": "No access."}
        )
    )
    assert response.status_code == 403


@pytest.mark.parametrize("operation", ["create", "reply"])
def test_assignment_write_requires_a_stored_github_token(
    agent_hub: TestClient, operation: str
) -> None:
    sign_in(agent_hub, "code-alice")
    if operation == "reply":
        assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.github_tokens.pop("alice", None)
    hub.store.delete_oauth_token("alice")
    response = (
        agent_hub.post(
            "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
        )
        if operation == "create"
        else agent_hub.post(
            "/api/assignments/Acme/Widget/1/comments",
            json={"body": "A reply.", "request_id": REQUEST_ID},
        )
    )
    assert response.status_code == 409
    assert "sign in on the website" in response.json()["detail"]
    assert response.json()["request_id_final"] is True


@pytest.mark.parametrize("operation", ["create", "reply"])
def test_assignment_github_failures_are_502(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    if operation == "reply":
        assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201

    def reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GitHubError("GitHub request failed: HTTP 403", status_code=403)

    monkeypatch.setattr(
        hub.github,
        "create_issue" if operation == "create" else "issue",
        reject,
    )
    response = (
        agent_hub.post(
            "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
        )
        if operation == "create"
        else agent_hub.post(
            "/api/assignments/Acme/Widget/1/comments",
            json={"body": "A reply.", "request_id": REQUEST_ID},
        )
    )
    assert response.status_code == 502
    assert "HTTP 403" in response.json()["detail"]
    assert response.json()["request_id_final"] is False


def test_assignment_rejects_created_issue_without_number_or_url(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    monkeypatch.setattr(
        hub.github,
        "create_issue",
        lambda *_args, **_kwargs: {"number": 1},
    )

    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "GitHub issue creation returned no issue number or URL"
    )


def test_assignment_marks_only_a_lost_assignee_as_assignee_missing(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    github.omit_assignees_on_create = True
    monkeypatch.setattr(github, "add_assignees", lambda *_args, **_kwargs: {"assignees": []})

    lost = agent_hub.post("/api/assignments", json=ASSIGNMENT)

    assert lost.status_code == 502
    assert lost.json()["assignee_missing"] is True
    assert lost.json()["url"]

    def fail_verify(*_args, **_kwargs):
        raise GitHubError("GitHub request failed: HTTP 500", status_code=500)

    monkeypatch.setattr(github, "add_assignees", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    github.omit_assignees_on_create = False
    monkeypatch.setattr(github, "issue", fail_verify)
    other = agent_hub.post("/api/assignments", json=dict(ASSIGNMENT, title="Another title"))

    assert other.status_code == 502
    assert "assignee_missing" not in other.json()


def test_assignment_rejects_repository_without_canonical_full_name(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    monkeypatch.setattr(
        hub.github,
        "create_issue",
        lambda *_args, **_kwargs: {
            "number": 1,
            "html_url": "https://github.test/Acme/Widget/issues/1",
            "assignees": [{"login": "helperbot"}],
        },
    )
    monkeypatch.setattr(
        hub.github,
        "repository",
        lambda *_args, **_kwargs: {"full_name": "not-a-repository"},
    )

    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "GitHub repository lookup returned no canonical full_name"
    )


@pytest.mark.parametrize(
    ("github_status", "response_status", "token_retained"),
    ((401, 409, False), (403, 502, True)),
)
def test_assignment_create_handles_github_auth_status(
    agent_hub: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    github_status: int,
    response_status: int,
    token_retained: bool,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub

    def reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GitHubError(
            f"GitHub request failed: HTTP {github_status}", status_code=github_status
        )

    monkeypatch.setattr(hub.github, "create_issue", reject)
    response = agent_hub.post(
        "/api/assignments", json=dict(ASSIGNMENT, request_id=REQUEST_ID)
    )

    assert response.status_code == response_status
    if github_status == 401:
        # The write had started, so the id stays retryable: a retry after sign-in
        # finds no marker and creates the issue once.
        assert response.json() == {
            "detail": "sign in on the website again",
            "request_id_final": False,
        }
    else:
        assert "HTTP 403" in response.json()["detail"]
        assert response.json()["request_id_final"] is False
    assert hub.store.get_oauth_token("alice") == ("tok-alice" if token_retained else "")
    assert (hub.github_tokens.get("alice") == "tok-alice") is token_retained


def test_assignment_reply_expired_token_asks_reauth(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub

    def expired(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GitHubError("GitHub request failed: HTTP 401", status_code=401)

    monkeypatch.setattr(hub.github, "issue", expired)
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "A reply."}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "sign in on the website again"}
    assert hub.store.get_oauth_token("alice") == ""
    assert "alice" not in hub.github_tokens


def test_assignment_retries_missing_assignee_then_returns_issue_url(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.omit_assignees_on_create = True
    github.omit_assignees_after_add = True
    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert response.status_code == 502
    assert response.json()["url"] == "https://github.test/Acme/Widget/issues/1"
    assert response.json()["number"] == 1
    assert [call["operation"] for call in github.issue_calls] == [
        "create_issue",
        "issue",
        "add_assignees",
    ]


def _assignee_add_scenario(
    agent_hub: TestClient, add_assignees: list[dict[str, str]]
) -> tuple[object, list[tuple[str, str]]]:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            httpx.Response(
                201,
                json={
                    "number": 1,
                    "html_url": "https://github.test/Acme/Widget/issues/1",
                    "repository_url": "https://api.github.test/repos/Acme/Widget",
                    "assignees": [],
                },
            ),
            httpx.Response(200, json={"number": 1, "assignees": []}),
            httpx.Response(201, json={"number": 1, "assignees": add_assignees}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return next(responses)

    hub.github = RealGitHub(hub.cfg, transport=httpx.MockTransport(handler))
    return agent_hub.post("/api/assignments", json=ASSIGNMENT), calls


def test_assignment_accepts_201_add_and_the_add_response_decides(
    agent_hub: TestClient,
) -> None:
    response, calls = _assignee_add_scenario(agent_hub, [{"login": "helperbot"}])
    assert response.status_code == 201, response.text
    assert [method for method, _url in calls] == ["POST", "GET", "POST"]
    assert calls[2][1].endswith("/repos/Acme/Widget/issues/1/assignees")
    assert response.json()["number"] == 1
    state = agent_hub.get("/api/state").json()
    created = [row for row in state["activity"] if row.get("type") == "assignment.created"]
    assert len(created) == 1
    assert created[0]["payload"]["number"] == 1


def test_assignment_add_response_without_agent_returns_issue_url(
    agent_hub: TestClient,
) -> None:
    response, calls = _assignee_add_scenario(agent_hub, [])
    assert response.status_code == 502
    assert response.json()["url"] == "https://github.test/Acme/Widget/issues/1"
    assert response.json()["number"] == 1
    assert [method for method, _url in calls] == ["POST", "GET", "POST"]


def test_assignment_transport_errors_are_bounded_and_preserve_created_issue(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub

    def fail_create(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "transport included tok-alice and a sensitive query", request=request
        )

    hub.github = RealGitHub(hub.cfg, transport=httpx.MockTransport(fail_create))
    failed_create = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert failed_create.status_code == 502
    assert failed_create.json() == {"detail": "GitHub request failed: ConnectError"}
    assert "tok-alice" not in failed_create.text

    calls = 0

    def fail_after_create(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                201,
                json={
                    "number": 2,
                    "html_url": "https://github.test/Acme/Widget/issues/2",
                    "repository_url": "https://api.github.test/repos/Acme/Widget",
                    "assignees": [{"login": "helperbot"}],
                },
            )
        raise httpx.ReadTimeout(
            "transport included tok-alice and a sensitive query", request=request
        )

    hub.github = RealGitHub(hub.cfg, transport=httpx.MockTransport(fail_after_create))
    failed_verify = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert failed_verify.status_code == 502
    assert failed_verify.json() == {
        "detail": "GitHub request failed: ReadTimeout",
        "url": "https://github.test/Acme/Widget/issues/2",
        "number": 2,
    }
    assert "tok-alice" not in failed_verify.text


def test_assignment_list_comments_and_reply_authorization(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    created = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert created.status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.create_issue_comment(
        "tok-helperbot", "Acme/Widget", 1, "Which failure signature should I prioritize?"
    )
    listed = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert listed.status_code == 200
    row = listed.json()["assignments"][0]
    assert row["state"] == "open"
    assert row["assignees"] == ["helperbot"]
    assert row["comments"][0]["author"] == "helperbot"
    assert "prioritize" in row["comments"][0]["body"]

    replied = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "Prioritize the first reproducible 500 response."},
    )
    assert replied.status_code == 201
    assert "issuecomment" in replied.json()["url"]
    reply_call = [call for call in github.issue_calls if call["operation"] == "create_issue_comment"][-1]
    assert reply_call["token"] == "tok-alice"
    state = agent_hub.get("/api/state").json()
    reply = next(row for row in state["activity"] if row.get("type") == "assignment.reply")
    assert reply["payload"]["agent"] == "helperbot"
    assert reply["payload"]["issue_key"] == "Acme/Widget#1"
    assert reply["payload"]["url"] == "https://github.com/Acme/Widget/issues/1"
    replica = agent_hub.app.state.hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'activity' AND row_id = ?",
        (reply["id"],),
    )
    assert replica["origin_device_id"] == "web:alice"

    sign_in(agent_hub, "code-bob")
    denied = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "I do not own this agent."}
    )
    assert denied.status_code == 403


def test_identical_replay_of_a_stored_hub_authored_event_is_acknowledged(
    agent_hub: TestClient,
) -> None:
    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac05"
    token = pair_device(agent_hub, "code-alice", device_id)
    event = {
        "origin_device_id": device_id,
        "origin_seq": 1,
        "table": "activity",
        "op": "insert",
        "row_id": "stored-before-the-rule",
        "payload": {"id": "stored-before-the-rule", "type": "assignment.created", "payload": {}},
        "occurred_at": "2026-11-05T10:00:00Z",
    }
    agent_hub.app.state.hub.store.execute(
        "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (device_id, 1, "activity", "insert", event["row_id"], dumps(event["payload"]), event["occurred_at"], event["occurred_at"]),
    )

    replay = agent_hub.post(
        "/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [event]}
    )
    fresh = agent_hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [dict(event, origin_seq=2, row_id="new-forgery")]},
    )

    assert replay.status_code == 200, replay.text
    assert fresh.status_code == 400
    assert fresh.json()["detail"] == "assignment activity is hub-authored only"


def test_enrichment_racing_a_reply_does_not_cache_stale_comments(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    read_comments = github.issue_comments

    def comments_then_reply(*args, **kwargs):
        rows = read_comments(*args, **kwargs)
        app_module._forget_cached_issue(hub, "Acme/Widget", 1)
        return rows

    monkeypatch.setattr(github, "issue_comments", comments_then_reply)

    listed = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert listed.status_code == 200
    assert listed.json()["assignments"][0]["enriched"] is True
    assert not [key for key in hub.issue_cache if key[2] == 1]


@pytest.mark.parametrize("activity_type", ["assignment.created", "assignment.reply"])
def test_device_cannot_push_hub_authored_assignment_activity(
    agent_hub: TestClient, activity_type: str
) -> None:
    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac04"
    token = pair_device(agent_hub, "code-alice", device_id)
    pushed = agent_hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "origin_device_id": device_id,
                    "origin_seq": 1,
                    "table": "activity",
                    "op": "insert",
                    "row_id": "forged-assignment",
                    "payload": {
                        "id": "forged-assignment",
                        "type": activity_type,
                        "payload": {
                            "agent": "helperbot",
                            "actor": "alice",
                            "url": "javascript:alert(1)",
                        },
                    },
                    "occurred_at": "2026-11-05T10:00:00Z",
                }
            ]
        },
    )
    assert pushed.status_code == 400
    assert pushed.json()["detail"] == "assignment activity is hub-authored only"
    assert (
        agent_hub.app.state.hub.store.query_one(
            "SELECT row_id FROM row_replica WHERE table_name = 'activity' AND row_id = ?",
            ("forged-assignment",),
        )
        is None
    )


def test_assignment_list_ignores_non_web_and_actor_mismatched_rows(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    store = agent_hub.app.state.hub.store
    for row_id, origin, actor in (
        ("trusted-assignment", "web:alice", "alice"),
        ("device-assignment", "device-alice", "alice"),
        ("mismatched-assignment", "web:alice", "bob"),
    ):
        store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                row_id,
                origin,
                "alice",
                dumps(
                    {
                        "id": row_id,
                        "type": "assignment.created",
                        "payload": {
                            "agent": "helperbot",
                            "actor": actor,
                            "repo": "Acme/Widget",
                            "number": 99,
                            "title": row_id,
                            "url": "javascript:alert(1)",
                        },
                    }
                ),
                "2026-11-05T10:00:00Z",
            ),
        )

    listed = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert listed.status_code == 200
    assert [row["title"] for row in listed.json()["assignments"]] == ["trusted-assignment"]


def test_query_and_subscription_snapshots_ignore_untrusted_assignment_rows(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    store = agent_hub.app.state.hub.store
    for row_id, origin, actor in (
        ("trusted-query-assignment", "web:alice", "alice"),
        ("device-query-assignment", "device-alice", "alice"),
        ("mismatched-query-assignment", "web:alice", "bob"),
    ):
        store.execute(
            "INSERT INTO row_replica "
            "(table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                row_id,
                origin,
                "alice",
                dumps(
                    {
                        "id": row_id,
                        "type": "assignment.created",
                        "payload": {"agent": "helperbot", "actor": actor},
                    }
                ),
                "2026-11-05T10:00:00Z",
            ),
        )
    token = pair_device(
        agent_hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac05"
    )
    headers = {"Authorization": f"Bearer {token}"}
    match = {"type": "assignment.created"}

    query = agent_hub.post("/sync/query", headers=headers, json={"match": match})
    assert query.status_code == 200, query.text
    assert [row["row_id"] for row in query.json()["rows"]] == [
        "trusted-query-assignment"
    ]

    subscribed = agent_hub.put(
        "/sync/subscriptions",
        headers=headers,
        json={"subscriptions": [{"match": match}]},
    )
    assert subscribed.status_code == 200, subscribed.text
    pull = agent_hub.get("/sync/pull", headers=headers)
    assert pull.status_code == 200, pull.text
    assert [row["row_id"] for row in pull.json()["subscriptions"]] == [
        "trusted-query-assignment"
    ]


def test_assignment_list_returns_newest_20_of_130_fake_comments(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.comments[("Acme/Widget", 1)] = [
        {
            "id": index,
            "html_url": f"https://github.test/Acme/Widget/issues/1#issuecomment-{index}",
            "user": {"login": "helperbot"},
            "created_at": f"{index:03d}",
            "body": f"comment-{index:03d}",
        }
        for index in range(130)
    ]

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert response.status_code == 200
    comments = response.json()["assignments"][0]["comments"]
    assert [comment["body"] for comment in comments] == [
        f"comment-{index:03d}" for index in range(129, 109, -1)
    ]


def test_assignment_comment_body_cut_has_4000_character_boundary(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.comments[("Acme/Widget", 1)] = [
        {
            "id": 1,
            "html_url": "https://github.test/Acme/Widget/issues/1#issuecomment-1",
            "user": {"login": "helperbot"},
            "created_at": "2026-11-05T10:00:00Z",
            "body": "a" * 4000,
        },
        {
            "id": 2,
            "html_url": "https://github.test/Acme/Widget/issues/1#issuecomment-2",
            "user": {"login": "helperbot"},
            "created_at": "2026-11-05T10:01:00Z",
            "body": "b" * 4001,
        },
    ]

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    comments = response.json()["assignments"][0]["comments"]
    assert comments[0] == {
        "author": "helperbot",
        "created_at": "2026-11-05T10:01:00Z",
        "updated_at": "2026-11-05T10:01:00Z",
        "body": "b" * 4000,
        "url": "https://github.test/Acme/Widget/issues/1#issuecomment-2",
    }
    assert comments[1]["body"] == "a" * 4000


def test_assignment_comments_with_equal_timestamps_use_comment_id_descending(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.comments[("Acme/Widget", 1)] = [
        {
            "id": index,
            "html_url": f"https://github.test/Acme/Widget/issues/1#issuecomment-{index}",
            "user": {"login": "helperbot"},
            "created_at": "2026-11-05T10:00:00Z" if index else "2026-11-05T09:00:00Z",
            "body": f"comment-{index}",
        }
        for index in (2, 0, 3, 1)
    ]

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    comments = response.json()["assignments"][0]["comments"]
    assert [comment["body"] for comment in comments] == [
        "comment-3",
        "comment-2",
        "comment-1",
        "comment-0",
    ]


def test_assignment_comments_use_latest_change_with_comment_id_tiebreak(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.comments[("Acme/Widget", 1)] = [
        {
            "id": index,
            "html_url": f"https://github.test/Acme/Widget/issues/1#issuecomment-{index}",
            "user": {"login": "helperbot"},
            "created_at": created_at,
            "updated_at": updated_at,
            "body": body,
        }
        for index, created_at, updated_at, body in (
            (1, "2026-11-05T09:00:00Z", "2026-11-05T12:00:00Z", "edited older"),
            (2, "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z", "tie first"),
            (3, "2026-11-05T10:01:00Z", "2026-11-05T11:00:00Z", "tie second"),
        )
    ]

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    comments = response.json()["assignments"][0]["comments"]
    assert [comment["body"] for comment in comments] == [
        "edited older",
        "tie second",
        "tie first",
    ]
    assert comments[0]["updated_at"] == "2026-11-05T12:00:00Z"


def test_assignment_comments_read_three_pages_with_remaining_timeouts(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    clock = [0.0]
    calls: list[tuple[int, float]] = []

    def request(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]["read"]
        assert isinstance(timeout, float)
        if not request.url.path.endswith("/comments"):
            calls.append((0, timeout))
            clock[0] = 1.0
            return httpx.Response(
                200,
                json={
                    "number": 1,
                    "state": "open",
                    "assignees": [{"login": "helperbot"}],
                },
            )
        page = int(request.url.params.get("page", "1"))
        calls.append((page, timeout))
        start = (page - 1) * 100 + 1
        end = min(page * 100, 205)
        rows = [
            {
                "id": comment_id,
                "html_url": (
                    "https://github.test/Acme/Widget/issues/1"
                    f"#issuecomment-{comment_id}"
                ),
                "user": {"login": "helperbot"},
                "created_at": f"2026-11-05T10:{comment_id:03d}:00Z",
                "updated_at": (
                    "2099-01-01T00:00:00Z"
                    if comment_id == 1
                    else f"2026-11-05T10:{comment_id:03d}:00Z"
                ),
                "body": "old edited comment" if comment_id == 1 else f"comment-{comment_id}",
            }
            for comment_id in range(start, end + 1)
        ]
        clock[0] = float(page + 1)
        headers = {}
        if page == 1:
            headers = {
                "Link": (
                    '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="next", '
                    '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=3>; rel="last"'
                )
            }
        return httpx.Response(200, json=rows, headers=headers)

    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    hub.github = RealGitHub(hub.cfg, transport=httpx.MockTransport(request))
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    row = response.json()["assignments"][0]
    assert row["enriched"] is True
    assert row["comments"][0]["body"] == "old edited comment"
    assert [page for page, _timeout in calls] == [0, 1, 2, 3]
    assert [timeout for _page, timeout in calls] == [10.0, 9.0, 8.0, 7.0]


def test_assignment_comment_pagination_error_discards_partial_enrichment(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub

    def request(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/comments"):
            return httpx.Response(
                200,
                json={"state": "open", "assignees": [{"login": "helperbot"}]},
            )
        if request.url.params.get("page") == "2":
            raise httpx.ReadTimeout(
                "second comment page timed out", request=request
            )
        return httpx.Response(
            200,
            json=[
                {"id": index, "created_at": str(index), "body": str(index)}
                for index in range(100)
            ],
            headers={
                "Link": (
                    '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="next", '
                    '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
                )
            },
        )

    hub.github = RealGitHub(hub.cfg, transport=httpx.MockTransport(request))
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    row = response.json()["assignments"][0]
    assert row["enriched"] is False
    assert "comments" not in row


def test_assignment_enrichment_stops_at_deadline_during_streamed_body(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    calls: list[str] = []
    stream = _BlockingByteStream()

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(app_module, "ASSIGNMENT_ENRICHMENT_SECONDS", 0.2)
    hub.github = RealGitHub(hub.cfg, transport=httpx.MockTransport(request))
    started = time.monotonic()
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["assignments"][0]["enriched"] is False
    assert calls == ["/repos/Acme/Widget/issues/1"]
    assert stream.closed.is_set()
    assert elapsed < 0.75


def test_assignment_list_enriches_only_newest_50(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    for number in range(1, 53):
        github.issues[("Acme/Widget", number)] = {
            "number": number,
            "html_url": f"https://github.test/Acme/Widget/issues/{number}",
            "repository_url": "https://api.github.test/repos/Acme/Widget",
            "state": "open",
            "assignees": [{"login": "helperbot"}],
        }
        hub.store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                f"assignment-{number:02d}",
                "web:alice",
                "alice",
                dumps(
                    {
                        "id": f"assignment-{number:02d}",
                        "type": "assignment.created",
                        "payload": {
                            "agent": "helperbot",
                            "actor": "alice",
                            "repo": "Acme/Widget",
                            "number": number,
                            "title": f"assignment-{number:02d}",
                        },
                    }
                ),
                f"2026-11-05T10:{number:02d}:00Z",
            ),
        )

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    rows = response.json()["assignments"]
    assert [row["number"] for row in rows] == list(range(52, 0, -1))
    assert [row["enriched"] for row in rows] == [True] * 50 + [False] * 2
    assert len([call for call in github.issue_calls if call["operation"] == "issue"]) == 50
    assert (
        len([call for call in github.issue_calls if call["operation"] == "issue_comments"])
        == 50
    )


def test_assignment_list_stops_enrichment_and_drops_expired_token(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "activity",
            "second-assignment",
            "web:alice",
            "alice",
            dumps(
                {
                    "id": "second-assignment",
                    "type": "assignment.created",
                    "payload": {
                        "agent": "helperbot",
                        "actor": "alice",
                        "repo": "Acme/Widget",
                        "number": 1,
                        "title": "Second mandate",
                    },
                }
            ),
            "2099-01-01T00:00:00Z",
        ),
    )
    calls = 0

    def expired(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise GitHubError("GitHub request failed: HTTP 401", status_code=401)

    monkeypatch.setattr(hub.github, "issue", expired)
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert len(response.json()["assignments"]) == 2
    assert all(not row["enriched"] for row in response.json()["assignments"])
    assert calls == 1
    assert hub.store.get_oauth_token("alice") == ""
    assert "alice" not in hub.github_tokens


def test_assignment_list_stops_enrichment_after_transport_error(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "activity",
            "transport-assignment",
            "web:alice",
            "alice",
            dumps(
                {
                    "id": "transport-assignment",
                    "type": "assignment.created",
                    "payload": {
                        "agent": "helperbot",
                        "actor": "alice",
                        "repo": "Acme/Widget",
                        "number": 1,
                    },
                }
            ),
            "2099-01-01T00:00:00Z",
        ),
    )
    calls = 0

    def transport_error(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise GitHubError("GitHub request failed: connection reset")

    monkeypatch.setattr(hub.github, "issue", transport_error)
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert all(not row["enriched"] for row in response.json()["assignments"])
    assert calls == 1
    assert hub.store.get_oauth_token("alice") == "tok-alice"


def test_assignment_list_stops_enrichment_when_budget_is_spent(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "activity",
            "slow-assignment",
            "web:alice",
            "alice",
            dumps(
                {
                    "id": "slow-assignment",
                    "type": "assignment.created",
                    "payload": {
                        "agent": "helperbot",
                        "actor": "alice",
                        "repo": "Acme/Widget",
                        "number": 1,
                    },
                }
            ),
            "2099-01-01T00:00:00Z",
        ),
    )
    clock = [0.0]
    issue = hub.github.issue
    issue_calls_before = len(
        [call for call in hub.github.issue_calls if call["operation"] == "issue"]
    )
    comment_calls_before = len(
        [
            call
            for call in hub.github.issue_calls
            if call["operation"] == "issue_comments"
        ]
    )

    def slow_issue(*args: object, **kwargs: object) -> dict[str, object]:
        result = issue(*args, **kwargs)
        clock[0] = app_module.ASSIGNMENT_ENRICHMENT_SECONDS + 1
        return result

    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hub.github, "issue", slow_issue)
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert all(not row["enriched"] for row in response.json()["assignments"])
    assert len(
        [call for call in hub.github.issue_calls if call["operation"] == "issue"]
    ) == issue_calls_before + 1
    assert len(
        [
            call
            for call in hub.github.issue_calls
            if call["operation"] == "issue_comments"
        ]
    ) == comment_calls_before


def test_assignment_list_keeps_token_when_enrichment_is_forbidden(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GitHubError("GitHub request failed: HTTP 403", status_code=403)

    monkeypatch.setattr(hub.github, "issue", forbidden)
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert response.json()["assignments"][0]["enriched"] is False
    assert hub.github_tokens["alice"] == "tok-alice"
    assert hub.store.get_oauth_token("alice") == "tok-alice"


@pytest.mark.parametrize("status_code", [403, 404, 429, 500])
def test_assignment_list_continues_after_one_http_enrichment_error(
    agent_hub: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.store.execute(
        "INSERT INTO row_replica "
        "(table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "activity",
            "newer-assignment",
            "web:alice",
            "alice",
            dumps(
                {
                    "id": "newer-assignment",
                    "type": "assignment.created",
                    "payload": {
                        "agent": "helperbot",
                        "actor": "alice",
                        "repo": "Acme/Widget",
                        "number": 1,
                        "title": "Newer assignment",
                    },
                }
            ),
            "2099-01-01T00:00:00Z",
        ),
    )
    enrich = app_module._assignment_enrichment
    calls = 0

    def fail_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitHubError(
                f"GitHub request failed: HTTP {status_code}",
                status_code=status_code,
            )
        return enrich(*args, **kwargs)

    monkeypatch.setattr(app_module, "_assignment_enrichment", fail_first)
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    rows = response.json()["assignments"]
    assert [row["title"] for row in rows] == [
        "Newer assignment",
        ASSIGNMENT["title"],
    ]
    assert [row["enriched"] for row in rows] == [False, True]
    assert calls == 2
    assert hub.store.get_oauth_token("alice") == "tok-alice"


def test_assignment_list_prunes_and_bounds_issue_cache(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    now = time.monotonic()
    hub.issue_cache[("alice", "expired/old", 1)] = (now - 2, {})
    hub.issue_cache[("alice", "expired/new", 2)] = (now - 1, {})
    for number in range(502):
        hub.issue_cache[("alice", "Acme/Widget", number)] = (now + number + 1, {})

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert len(hub.issue_cache) == 500
    assert ("alice", "expired/old", 1) not in hub.issue_cache
    assert ("alice", "expired/new", 2) not in hub.issue_cache
    assert ("alice", "Acme/Widget", 0) not in hub.issue_cache
    assert ("alice", "Acme/Widget", 1) not in hub.issue_cache
    assert ("alice", "Acme/Widget", 2) in hub.issue_cache


def test_fake_github_mutations_resolve_mixed_case_repo_to_canonical_name() -> None:
    github = FakeGitHub({"code-alice": "alice"})
    github.canonical_repos["acme/widget"] = "Acme/Widget"
    created = github.create_issue("tok-alice", "acme/widget", "Title", "Body", [])

    fetched = github.issue("tok-alice", "ACME/WIDGET", created["number"])
    assigned = github.add_assignees(
        "tok-alice", "ACME/WIDGET", created["number"], ["helperbot"]
    )
    comment = github.create_issue_comment(
        "tok-alice", "ACME/WIDGET", created["number"], "Reply"
    )
    comments = github.issue_comments(
        "tok-alice", "ACME/WIDGET", created["number"]
    )

    assert fetched["html_url"] == "https://github.test/Acme/Widget/issues/1"
    assert assigned["assignees"] == [{"login": "helperbot"}]
    assert comment["html_url"] == (
        "https://github.test/Acme/Widget/issues/1#issuecomment-1"
    )
    assert [item["body"] for item in comments] == ["Reply"]

    with pytest.raises(GitHubError) as raised:
        github.issue_comments("tok-alice", "ACME/WIDGET", 999)
    assert raised.value.status_code == 404


def test_reply_targets_every_assigned_agent_timeline(
    two_agent_hub: TestClient,
) -> None:
    sign_in(two_agent_hub, "code-alice")
    created = two_agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert created.status_code == 201
    github: FakeGitHub = two_agent_hub.app.state.hub.github
    github.issues[("Acme/Widget", 1)]["assignees"] = [
        {"login": "secondbot"},
        {"login": "helperbot"},
    ]

    response = two_agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "Reply to both agents."},
    )
    assert response.status_code == 201
    state = two_agent_hub.get("/api/state").json()
    reply = next(row for row in state["activity"] if row.get("type") == "assignment.reply")
    assert reply["payload"]["agent"] == "helperbot"
    assert reply["payload"]["agents"] == ["helperbot", "secondbot"]

    _insert_replica(
        two_agent_hub,
        table="session",
        row_id="second-agent-session",
        updated_at="2026-11-05T12:00:00Z",
        login="secondbot",
        payload={"id": "second-agent-session", "created_at": "2026-11-05T12:00:00Z"},
    )
    timeline = two_agent_hub.get(
        "/api/session-timeline?id=second-agent-session"
    ).json()["timeline"]
    assert reply["id"] in [row["row_id"] for row in timeline]


def test_reply_reaches_agents_of_other_owners_but_needs_an_own_agent(
    mixed_owner_hub: TestClient,
) -> None:
    sign_in(mixed_owner_hub, "code-alice")
    created = mixed_owner_hub.post("/api/assignments", json=ASSIGNMENT)
    assert created.status_code == 201
    github: FakeGitHub = mixed_owner_hub.app.state.hub.github
    github.issues[("Acme/Widget", 1)]["assignees"] = [
        {"login": "secondbot"},
        {"login": "helperbot"},
    ]

    sign_in(mixed_owner_hub, "code-bob")
    response = mixed_owner_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "Reply from the second owner."},
    )
    assert response.status_code == 201
    state = mixed_owner_hub.get("/api/state").json()
    reply = next(row for row in state["activity"] if row.get("type") == "assignment.reply")
    assert reply["payload"]["agent"] == "secondbot"
    assert reply["payload"]["agents"] == ["helperbot", "secondbot"]

    github.issues[("Acme/Widget", 1)]["assignees"] = [{"login": "helperbot"}]
    refused = mixed_owner_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "Not my agent."},
    )
    assert refused.status_code == 403


def test_reply_accepts_owner_device_bearer_and_rejects_agent_device_bearer(
    agent_hub: TestClient,
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    owner_token = pair_device(
        agent_hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac21"
    )
    agent_token = _pair_as(
        agent_hub,
        "code-alice",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac22",
        "helperbot",
    )

    owner_reply = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"body": "Owner bearer reply."},
    )
    agent_reply = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"body": "Agent bearer reply."},
    )

    assert owner_reply.status_code == 201
    assert agent_reply.status_code == 403


def test_reply_to_a_transferred_issue_comments_on_the_issue_that_authorized_it(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    moved = {
        "number": 9,
        "html_url": "https://github.test/Acme/Other/issues/9",
        "repository_url": "https://api.github.test/repos/Acme/Other",
        "assignees": [{"login": "helperbot"}],
    }
    github.issues[("Acme/Other", 9)] = moved
    original_issue = github.issue

    def transferred(token: str, repo: str, number: int) -> dict:
        if (repo, number) == ("Acme/Widget", 1):
            return dict(moved)
        return original_issue(token, repo, number)

    monkeypatch.setattr(github, "issue", transferred)
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "After the move."}
    )
    assert response.status_code == 201, response.text
    comment = [call for call in github.issue_calls if call["operation"] == "create_issue_comment"][-1]
    assert (comment["repo"], comment["number"]) == ("Acme/Other", 9)
    state = agent_hub.get("/api/state").json()
    reply = next(row for row in state["activity"] if row.get("type") == "assignment.reply")
    assert reply["payload"]["issue_key"] == "Acme/Other#9"
    assert reply["payload"]["number"] == 9


def test_real_github_comments_follows_last_page_for_newest_comments(cfg) -> None:
    calls: list[dict[str, object]] = []
    first_page = [{"id": index} for index in range(100)]
    last_page = [{"id": index} for index in range(100, 130)]

    def handler(request: httpx.Request) -> httpx.Response:
        params = {key: int(value) for key, value in request.url.params.multi_items()}
        calls.append(params)
        if params.get("page") == 2:
            return httpx.Response(200, json=last_page)
        return httpx.Response(
            200,
            json=first_page,
            headers={
                "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="next", '
                '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
            },
        )

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))
    comments = github.issue_comments("tok-alice", "Acme/Widget", 1)
    assert [comment["id"] for comment in comments[-20:]] == list(range(110, 130))
    assert calls == [{"per_page": 100}, {"per_page": 100, "page": 2}]


@pytest.mark.parametrize("invalid_page", [1, 2])
def test_real_github_comments_rejects_non_list_page(
    cfg, invalid_page: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == invalid_page:
            return httpx.Response(200, json={"message": "unexpected object"})
        return httpx.Response(
            200,
            json=[{"id": 1}],
            headers={
                "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
            },
        )

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))

    with pytest.raises(
        GitHubError, match="GitHub comment lookup returned an invalid response"
    ):
        github.issue_comments("tok-alice", "Acme/Widget", 1)


@pytest.mark.parametrize("invalid_page", [1, 2])
def test_real_github_comments_rejects_non_object_page_item(
    cfg, invalid_page: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        body: list[object] = [{"id": page}]
        if page == invalid_page:
            body.append("invalid comment")
        headers: dict[str, str] = {}
        if page == 1:
            headers = {
                "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
            }
        return httpx.Response(200, json=body, headers=headers)

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))

    with pytest.raises(
        GitHubError, match="GitHub comment lookup returned an invalid response"
    ):
        github.issue_comments("tok-alice", "Acme/Widget", 1)


def test_real_github_lists_recent_issues_by_creator(cfg) -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params: dict[str, object] = dict(request.url.params)
        params["per_page"] = int(params["per_page"])
        seen.append(
            (
                request.method,
                str(request.url).partition("?")[0],
                params,
            )
        )
        return httpx.Response(200, json=[{"number": 3}, {"number": 2}])

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))
    rows = github.recent_issues_by_creator("tok-alice", "Acme/Widget", "alice")

    assert [row["number"] for row in rows] == [3, 2]
    assert seen == [
        (
            "GET",
            "https://api.github.com/repos/Acme/Widget/issues",
            {
                "creator": "alice",
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 30,
            },
        )
    ]


def test_real_github_comments_keeps_all_pages_when_last_page_is_short(cfg) -> None:
    calls: list[dict[str, object]] = []
    first_page = [{"id": index} for index in range(100)]
    last_page = [{"id": index} for index in range(100, 105)]

    def handler(request: httpx.Request) -> httpx.Response:
        params = {key: int(value) for key, value in request.url.params.multi_items()}
        calls.append(params)
        if params.get("page") == 2:
            return httpx.Response(
                200,
                json=last_page,
                headers={
                    "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=1>; rel="prev"'
                },
            )
        return httpx.Response(
            200,
            json=first_page,
            headers={
                "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
            },
        )

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))
    comments = github.issue_comments("tok-alice", "Acme/Widget", 1)
    assert [comment["id"] for comment in comments[-20:]] == list(range(85, 105))
    assert calls == [
        {"per_page": 100},
        {"per_page": 100, "page": 2},
    ]


def test_real_github_recovery_paginates_issues_back_to_reservation_time(cfg) -> None:
    calls: list[dict[str, object]] = []

    def request(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        params["per_page"] = int(params["per_page"])
        if "page" in params:
            params["page"] = int(params["page"])
        calls.append(params)
        if params.get("page") == 2:
            return httpx.Response(
                200,
                json=[
                    {"number": 101, "created_at": "2026-11-05T09:59:59Z"},
                    {"number": 100, "created_at": "2026-11-05T09:59:58Z"},
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"number": number, "created_at": "2026-11-05T10:00:00Z"}
                for number in range(201, 101, -1)
            ],
            headers={
                "Link": (
                    '<https://api.github.com/repos/Acme/Widget/issues?per_page=100&page=2>; rel="next", '
                    '<https://api.github.com/repos/Acme/Widget/issues?per_page=100&page=2>; rel="last"'
                )
            },
        )

    github = RealGitHub(cfg, transport=httpx.MockTransport(request))
    rows = github.recent_issues_by_creator(
        "tok-alice",
        "Acme/Widget",
        "alice",
        10.0,
        "2026-11-05T10:00:00Z",
    )

    assert rows[-1]["number"] == 100
    assert calls == [
        {
            "creator": "alice",
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
        },
        {
            "creator": "alice",
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
            "page": 2,
        },
    ]


def test_real_github_recovery_paginates_comments_backward_to_marker(cfg) -> None:
    calls: list[int] = []

    def request(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        calls.append(page)
        start = (page - 1) * 100 + 1
        rows = [
            {
                "id": comment_id,
                "created_at": "2026-11-05T10:00:00Z",
                "body": "target" if comment_id == 150 else "other",
            }
            for comment_id in range(start, start + 100)
        ]
        headers = {}
        if page == 1:
            headers = {
                "Link": (
                    '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="next", '
                    '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=3>; rel="last"'
                )
            }
        return httpx.Response(200, json=rows, headers=headers)

    github = RealGitHub(cfg, transport=httpx.MockTransport(request))
    rows = github.issue_comments(
        "tok-alice",
        "Acme/Widget",
        1,
        10.0,
        "2026-11-05T09:59:59Z",
        lambda row: row.get("body") == "target",
    )

    assert any(row["id"] == 150 for row in rows)
    assert calls == [1, 3, 2]


def test_real_github_does_not_start_read_with_no_time_left(cfg) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport started with no time left")

    github = RealGitHub(cfg, transport=httpx.MockTransport(unexpected_request))

    with pytest.raises(GitHubError, match="time budget exhausted"):
        github.issue("tok-alice", "Acme/Widget", 1, 0.0)


def test_real_github_write_stream_is_stopped_by_the_deadline(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_core.github as github_module

    stream = _BlockingByteStream(wait_timeout=2.0)
    expected_body = {
        "title": "Title",
        "body": "Body",
        "assignees": ["helperbot"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/Acme/Widget/issues"
        assert json.loads(request.content) == expected_body
        return httpx.Response(201, stream=stream)

    monkeypatch.setattr(github_module, "GITHUB_REQUEST_TIMEOUT", 0.2)
    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))
    started = time.monotonic()

    with pytest.raises(GitHubError, match="time budget exhausted"):
        github.create_issue(
            "tok-alice", "Acme/Widget", "Title", "Body", ["helperbot"]
        )

    elapsed = time.monotonic() - started
    assert stream.closed.is_set()
    assert elapsed < 1.5


def test_real_github_does_not_follow_redirect_after_deadline(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    calls: list[str] = []

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        clock[0] = 10.0
        return httpx.Response(
            301,
            headers={"Location": "/repositories/42/issues/1"},
        )

    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    github = RealGitHub(cfg, transport=httpx.MockTransport(request))

    with pytest.raises(GitHubError, match="time budget exhausted") as raised:
        github.issue("tok-alice", "Acme/Widget", 1, 10.0)

    assert raised.value.status_code is None
    assert calls == ["/repos/Acme/Widget/issues/1"]


def test_real_github_deadline_closing_before_the_first_chunk_is_a_budget_error(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_core.github as github_module

    class ImmediateTimer:
        def __init__(self, _interval, function, args=()):
            self.function = function
            self.args = args
            self.daemon = False

        def start(self) -> None:
            self.function(*self.args)

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(github_module.threading, "Timer", ImmediateTimer)
    github = RealGitHub(
        cfg,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"number": 1})
        ),
    )

    with pytest.raises(GitHubError, match="time budget exhausted") as raised:
        github.issue("tok-alice", "Acme/Widget", 1, 10.0)

    assert raised.value.status_code is None


def test_real_github_deadline_watchdog_runs_before_connect(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_core.github as github_module

    events: list[str] = []
    real_timer = github_module.threading.Timer

    class RecordingTimer:
        def __init__(self, interval, function, args=()):
            events.append(f"timer {interval:.0f}")
            self.inner = real_timer(interval, function, args=args)
            self.daemon = True

        def start(self) -> None:
            events.append("start")
            self.inner.daemon = True
            self.inner.start()

        def cancel(self) -> None:
            events.append("cancel")
            self.inner.cancel()

    def request(request: httpx.Request) -> httpx.Response:
        events.append(f"send {request.url.path}")
        if request.url.path == "/repos/Acme/Widget/issues/1":
            return httpx.Response(301, headers={"Location": "/repositories/42/issues/1"})
        return httpx.Response(200, json={"number": 1})

    monkeypatch.setattr(github_module.threading, "Timer", RecordingTimer)
    github = RealGitHub(cfg, transport=httpx.MockTransport(request))

    assert github.issue("tok-alice", "Acme/Widget", 1, 10.0)["number"] == 1
    assert events == [
        "timer 10",
        "start",
        "send /repos/Acme/Widget/issues/1",
        "send /repositories/42/issues/1",
        "cancel",
    ]


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_real_github_reads_compressed_responses(cfg, encoding: str) -> None:
    import gzip
    import zlib

    def compress(data: bytes) -> bytes:
        return gzip.compress(data) if encoding == "gzip" else zlib.compress(data)

    def request(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.dumps(
                {"number": 7, "html_url": "https://github.com/Acme/Widget/issues/7"}
            ).encode()
            return httpx.Response(201, headers={"Content-Encoding": encoding}, content=compress(body))
        body = json.dumps([{"id": 1, "body": "hello", "user": {"login": "alice"}}]).encode()
        return httpx.Response(200, headers={"Content-Encoding": encoding}, content=compress(body))

    github = RealGitHub(cfg, transport=httpx.MockTransport(request))

    created = github.create_issue("tok-alice", "Acme/Widget", "title", "body", [])
    comments = github.issue_comments("tok-alice", "Acme/Widget", 7)

    assert created["number"] == 7
    assert comments[0]["body"] == "hello"


def test_reply_drops_every_viewers_cached_copy_of_the_issue(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.issue_cache[("bob", "acme/widget", 1)] = (time.monotonic() + 60, {"enriched": True})
    hub.issue_cache[("carol", "Acme/Widget", 1)] = (time.monotonic() + 60, {"enriched": True})
    hub.issue_cache[("carol", "Acme/Widget", 2)] = (time.monotonic() + 60, {"enriched": True})

    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "A reply."}
    )

    assert response.status_code == 201
    assert set(hub.issue_cache) == {("carol", "Acme/Widget", 2)}


def test_assignment_github_calls_use_to_thread(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_to_thread = app_module.asyncio.to_thread
    calls: list[str] = []

    async def recording_to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "The first reply."},
    )
    assert response.status_code == 201
    assert calls == ["create_issue", "issue", "issue", "create_issue_comment"]


def test_assignment_form_links_created_issue_and_locks_resubmission() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert '<input type="checkbox" name="deviation" required>' in html
    assert 'name="deviation" required checked' not in html
    assert "I authorize no deviation from the repository's contribution rules" in html
    assert (
        "Issue was created but the agent assignment failed — open it instead of submitting again"
        in html
    )
    assert "submit.disabled = true" in html
    assert 'form.addEventListener("input", unlock)' in html


def test_assignment_loads_have_a_deadline_and_keep_forms_visible() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "const ASSIGNMENT_LOAD_TIMEOUT_MS = 20000" in html
    assert "controller.abort();" in html
    assert "clearTimeout(timeout);" in html
    assert "if (result.error)" in html
    assert "renderAgentCards(owned" in html
    assert "Loading assignments…" in html
    assert "partialAssignmentLocks.clear();" in html
    assert "assignmentLoads.delete(agent);" in html
    assert "assignmentResults.delete(agent);" in html
    assert "function clearError()" in html
    assert html.count("clearError();") == 1


def test_action_errors_survive_the_state_poll() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    submit = html[html.index('document.addEventListener("submit"'):]
    submit = submit[: submit.index("\n    });")]
    assert 'getElementById("err")' not in submit
    assert 'getElementById("action-err")' in submit
    assert "clearError();" not in submit
    assert submit.count("clearActionError();") == 2
    poll = html[html.index('jsonGet("/api/state"'):]
    poll = poll[: poll.index("return p;")]
    assert "clearActionError" not in poll
    assert '<p id="action-err" hidden></p>' in html
    reset = html[html.index("function clearHubTables()"):]
    assert "clearActionError();" in reset[: reset.index("\n    }")]


def test_assignment_and_timeline_links_use_safe_href() -> None:
    static = Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static"
    index = (static / "index.html").read_text(encoding="utf-8")
    session = (static / "session.html").read_text(encoding="utf-8")

    for html in (index, session):
        assert "function safeHref(url)" in html
        assert 'parsed.protocol === "https:"' in html
        assert 'parsed.host === "github.com"' in html
    assert "safeHref(r.url)" in index
    assert "safeHref(a.url)" in index
    assert "safeHref(c.url)" in index
    assert "safeHref(url)" in index
    assert "safeHref(inner.url)" in session
    assert 'href="${esc(a.url)}"' not in index
    assert 'href="${esc(inner.url)}"' not in session


@pytest.mark.parametrize(
    ("status", "method", "call"),
    [
        (301, "GET", lambda gh: gh.issue("tok-alice", "Acme/Old", 7)),
        (
            307,
            "POST",
            lambda gh: gh.create_issue_comment("tok-alice", "Acme/Old", 7, "hello"),
        ),
    ],
)
def test_real_github_follows_rename_redirects_with_the_token(
    cfg, status: int, method: str, call
) -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path.startswith("/repos/Acme/Old/"):
            new_url = str(request.url).replace("/repos/Acme/Old/", "/repositories/42/")
            return httpx.Response(status, headers={"Location": new_url})
        return httpx.Response(
            201 if method == "POST" else 200,
            json={"number": 7, "id": 1, "body": "hello", "html_url": "https://github.com/Acme/New/issues/7"},
        )

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))
    result = call(github)
    assert result["number"] == 7
    assert [entry[0] for entry in seen] == [method, method]
    assert seen[1][1].startswith("/repositories/42/")
    assert seen[1][2] == "Bearer tok-alice"


def test_real_github_follows_write_redirect_with_method_body_and_token(cfg) -> None:
    expected_body = {
        "title": "Title",
        "body": "Body",
        "assignees": ["helperbot"],
    }
    seen: list[tuple[str, str, dict[str, object], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content),
                request.headers.get("Authorization"),
            )
        )
        if request.url.path == "/repos/Acme/Old/issues":
            return httpx.Response(307, headers={"Location": "/repositories/42/issues"})
        return httpx.Response(
            201,
            json={
                "number": 42,
                "html_url": "https://github.com/Acme/New/issues/42",
            },
        )

    github = RealGitHub(cfg, transport=httpx.MockTransport(handler))
    result = github.create_issue(
        "tok-alice", "Acme/Old", "Title", "Body", ["helperbot"]
    )

    assert result["number"] == 42
    assert seen == [
        ("POST", "/repos/Acme/Old/issues", expected_body, "Bearer tok-alice"),
        ("POST", "/repositories/42/issues", expected_body, "Bearer tok-alice"),
    ]
