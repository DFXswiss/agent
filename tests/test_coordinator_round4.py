from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli.coordinator_github import (
    _apply_readiness_checklist,
    _fork_repository,
    _hub_assignment_row,
    _owner_repository_config,
    phase_accept,
    phase_blocked,
    phase_readiness,
)
from agent_cli.coordinator_config import RepositoryConfig
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_runtime import advance_one, discover_assignments
from agent_cli.hub import HubError
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    make_session,
    make_worker,
    patch_account_runners,
    write_accounts,
)


class OwnerDiscoveryGh(FakeGh):
    def __call__(self, argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if "search/issues?q=is:issue+is:open" in joined:
            return Completed(0, json.dumps([
                {"items": [
                    {**self.issues[0], "repository_url": "https://api.github.com/repos/example/project"},
                    {
                        "number": 8, "state": "open", "title": "Owned issue", "body": "Do it",
                        "html_url": "https://github.com/Example/owned/issues/8",
                        "repository_url": "https://api.github.com/repos/Example/owned",
                        "assignees": [{"login": "worker-bot"}],
                    },
                    {
                        "number": 9, "state": "closed",
                        "repository_url": "https://api.github.com/repos/Example/closed",
                    },
                    {
                        "number": 10, "state": "open",
                        "repository_url": "https://api.github.com/repos/Outside/project",
                    },
                    {
                        "number": 11, "state": "open", "repository": {"archived": True},
                        "repository_url": "https://api.github.com/repos/Example/archived",
                    },
                ]},
            ]), "")
        if argv[:2] == ["gh", "api"] and argv[-1] == "repos/Example/owned/issues/8":
            return Completed(0, json.dumps({
                "number": 8, "state": "open", "title": "Owned issue", "body": "Do it",
                "html_url": "https://github.com/Example/owned/issues/8",
                "assignees": [{"login": "worker-bot"}],
            }), "")
        return super().__call__(argv)


def test_owner_discovery_filters_results_and_explicit_repository_wins(tmp_path, monkeypatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    patch_account_runners(monkeypatch, fake)
    lines = discover_assignments(store, worker, fake)
    assert sum("example/project#7" in line for line in lines) == 1
    assert sum("Example/owned#8" in line for line in lines) == 1
    sources = [row["payload"]["coordinator"]["source"] for row in store.rows("task")]
    explicit = next(source for source in sources if source["repo"].casefold() == "example/project")
    owned = next(source for source in sources if source["repo"] == "Example/owned")
    assert "owner_scoped" not in explicit
    assert owned["owner_scoped"] is True
    assert not any(source["number"] in {9, 10, 11} for source in sources)
    store.close()


def test_owner_repository_route_uses_direct_push_permission(tmp_path):
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_check_argv=("/operator/check",),
        owner_readiness_argv=("/operator/ready",),
    )

    def runner(argv: list[str]) -> Completed:
        if argv[-2:] == ["--jq", ".default_branch"]:
            return Completed(0, "main\n", "")
        if argv[-2:] == ["--jq", ".permissions.push"]:
            return Completed(0, "true\n", "")
        raise AssertionError(argv)

    cfg = _owner_repository_config(worker, runner, repo="Example/project", login="worker-bot")
    assert cfg.base == "main"
    assert cfg.publication_repo == "Example/project"
    assert cfg.check_argv == ("/operator/check",)


def test_owner_repository_route_creates_and_verifies_fork(tmp_path):
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/ready",),
    )
    lookups = 0
    posts = 0

    def runner(argv: list[str]) -> Completed:
        nonlocal lookups, posts
        if argv[-2:] == ["--jq", ".default_branch"]:
            return Completed(0, "main", "")
        if argv[-2:] == ["--jq", ".permissions.push"]:
            return Completed(0, "false", "")
        if argv == ["gh", "api", "repos/worker-bot/project"]:
            lookups += 1
            if lookups == 1:
                return Completed(1, "", "not found")
            return Completed(0, json.dumps({"parent": {"full_name": "Example/project"}}), "")
        if argv == ["gh", "api", "-X", "POST", "repos/Example/project/forks"]:
            posts += 1
            return Completed(0, "{}", "")
        raise AssertionError(argv)

    cfg = _owner_repository_config(worker, runner, repo="Example/project", login="worker-bot")
    assert cfg is None
    cfg = _owner_repository_config(
        worker,
        runner,
        repo="Example/project",
        login="worker-bot",
        fork_request_done=True,
    )
    assert cfg is not None
    assert cfg.publication_repo == "worker-bot/project"
    assert posts == 1
    assert lookups == 2


def test_wrong_parent_fork_is_blocked():
    def runner(_argv: list[str]) -> Completed:
        return Completed(0, json.dumps({"parent": {"full_name": "Other/project"}}), "")

    with pytest.raises(CoordinatorError, match="different parent"):
        _fork_repository(
            runner, repo="Example/project", publication_repo="worker-bot/project",
        )


def test_fork_creation_does_not_wait_or_repeat_request():
    lookups = 0
    posts = 0

    def runner(argv: list[str]) -> Completed:
        nonlocal lookups, posts
        if "-X" in argv:
            posts += 1
            return Completed(0, "{}", "")
        lookups += 1
        return Completed(1, "", "not found")

    assert not _fork_repository(
        runner, repo="Example/project", publication_repo="worker-bot/project"
    )
    assert not _fork_repository(
        runner,
        repo="Example/project",
        publication_repo="worker-bot/project",
        create_if_missing=False,
    )
    assert lookups == 2
    assert posts == 1


@pytest.mark.parametrize("detail", ["fork exists", "fork creation is in progress"])
def test_fork_creation_existing_or_in_progress_response_is_pending(detail):
    def runner(argv: list[str]) -> Completed:
        if argv == ["gh", "api", "repos/worker-bot/project"]:
            return Completed(1, "", "not found")
        if argv == ["gh", "api", "-X", "POST", "repos/Example/project/forks"]:
            return Completed(1, "", detail)
        raise AssertionError(argv)

    assert not _fork_repository(
        runner,
        repo="Example/project",
        publication_repo="worker-bot/project",
    )


class QueryHub:
    result = {"rows": []}
    error: Exception | None = None

    def __init__(self, _url: str, _token: str) -> None:
        pass

    def query(self, match):
        assert match == {
            "type": "assignment.created",
            "payload.issue_key": {
                "in": ["Example/project#7", "example/project#7"],
            },
        }
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        pass


class MetaStore:
    def meta(self, key: str):
        return {"hub_url": "https://hub.example", "device_token": "token"}.get(key)


def assignment_row(
    *, actor="human-owner", payload_actor=None, agent="worker-bot", number=7,
    repo="Example/project", origin_device_id=None,
):
    if payload_actor is None:
        payload_actor = actor
    if origin_device_id is None:
        origin_device_id = f"web:{actor}"
    return {
        "row_id": "assignment-row",
        "github_login": actor,
        "origin_device_id": origin_device_id,
        "payload": {
            "type": "assignment.created",
            "payload": {
                "repo": repo, "number": number, "agent": agent, "actor": payload_actor,
            },
        },
    }


@pytest.mark.parametrize(
    "row,reply_logins,matched",
    [
        (assignment_row(), ("human-owner",), True),
        (assignment_row(actor="other"), ("human-owner",), False),
        (assignment_row(), ("different-owner",), False),
        (assignment_row(agent="other-agent"), ("human-owner",), False),
        (assignment_row(payload_actor="other"), ("human-owner",), False),
        (assignment_row(origin_device_id="device-uuid"), ("human-owner",), False),
        (assignment_row(repo="EXAMPLE/PROJECT"), ("human-owner",), True),
    ],
)
def test_hub_assignment_requires_matching_agent_actor_and_reply_login(
    monkeypatch, row, reply_logins, matched
):
    QueryHub.result = {"rows": [row]}
    QueryHub.error = None
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)
    row_id, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=reply_logins,
    )
    assert retry is None
    assert (row_id == "assignment-row") is matched


def test_hub_assignment_query_deduplicates_already_lowercase_repo(monkeypatch):
    seen = []

    class CaptureHub(QueryHub):
        def query(self, match):
            seen.append(match)
            rows = (
                [assignment_row(repo="Example/project")]
                if "Example/project#7" in match["payload.issue_key"]["in"]
                else []
            )
            return {"rows": rows}

    monkeypatch.setattr("agent_cli.coordinator_github.Hub", CaptureHub)
    row_id, retry = _hub_assignment_row(
        MetaStore(), repo="example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
        canonical_repo="Example/project",
    )
    assert row_id == "assignment-row"
    assert retry is None
    assert seen == [{
        "type": "assignment.created",
        "payload.issue_key": {
            "in": ["example/project#7", "Example/project#7"],
        },
    }]


def test_hub_assignment_query_cap_is_retry_not_definite_miss(monkeypatch):
    QueryHub.result = {"rows": [assignment_row()] * 500}
    QueryHub.error = None
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)
    row_id, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
    )
    assert row_id is None
    assert retry == "hub assignment query truncated"


def test_hub_assignment_transport_error_requests_same_phase_retry(monkeypatch):
    QueryHub.error = HubError("temporarily unavailable")
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)
    row_id, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
    )
    assert row_id is None
    assert "temporarily unavailable" in (retry or "")
    QueryHub.error = None


def _accept_task():
    return {
        "id": "task-1",
        "state": "open",
        "payload": {"coordinator": {
            "phase": "accept",
            "source": {"repo": "Example/project", "number": 7},
        }},
    }


def _accept_worker(tmp_path):
    return replace(make_worker(tmp_path), require_hub_assignment=True)


def _patch_accept_prerequisites(monkeypatch):
    monkeypatch.setattr("agent_cli.coordinator_github.scoped", lambda *_a, **_k: object())
    monkeypatch.setattr(
        "agent_cli.coordinator_github.account_for",
        lambda *_a, **_k: SimpleNamespace(login="worker-bot"),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.verify_issue_assigned",
        lambda *_a, **_k: {
            "repository_url": "https://api.github.com/repos/Example/project"
        },
    )
    monkeypatch.setattr("agent_cli.coordinator_github.save_task", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "agent_cli.coordinator_github.gh_list",
        lambda *_a, **_k: [{
            "id": 70, "event": "assigned", "assignee": {"login": "worker-bot"},
            "actor": {"login": "human-owner", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
        }],
    )


def test_phase_accept_falls_back_to_issue_lookup_for_canonical_repo(
    monkeypatch, tmp_path
):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github.verify_issue_assigned", lambda *_a, **_k: {}
    )
    lookups = []

    def issue_lookup(runner, argv):
        lookups.append((runner, argv))
        return {"repository": {"full_name": "Example/project"}}

    captured = {}

    def hub_assignment(_store, **kwargs):
        captured.update(kwargs)
        return None, "device is not paired with a hub"

    monkeypatch.setattr("agent_cli.coordinator_github.gh_json", issue_lookup)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row", hub_assignment
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker", lambda *_a, **_k: ["published"]
    )
    task = _accept_task()
    task["payload"]["coordinator"]["source"]["repo"] = "example/project"

    assert phase_accept(
        object(), _accept_worker(tmp_path), task, object()
    ) == ["published"]
    assert lookups[0][1] == ["gh", "api", "repos/example/project/issues/7"]
    assert captured["canonical_repo"] == "Example/project"


def test_missing_hub_assignment_publishes_blocker_and_never_starts_acceptance(monkeypatch, tmp_path):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:02:01Z")
    monkeypatch.setattr("agent_cli.coordinator_github._hub_assignment_row", lambda *_a, **_k: (None, None))
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **kwargs: published.append(kwargs) or ["published"],
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.queue_activity",
        lambda *_a, **_k: pytest.fail("acceptance/model path must not start"),
    )
    task = _accept_task()
    lines = phase_accept(object(), _accept_worker(tmp_path), task, object())
    checkpoint = task["payload"]["coordinator"]
    assert lines == ["published"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["blocker"] == "assignment was not issued through the hub"
    assert published[0]["kind"] == "hub-assignment"
    assert published[0]["reply_checkpoint"] is True


def test_recent_missing_hub_assignment_retries_accept_during_grace(monkeypatch, tmp_path):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:01:59Z")
    monkeypatch.setattr("agent_cli.coordinator_github._hub_assignment_row", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **_k: pytest.fail("grace-period mismatch must stay a same-phase retry"),
    )
    task = _accept_task()
    lines = phase_accept(object(), _accept_worker(tmp_path), task, object())
    assert task["payload"]["coordinator"]["phase"] == "accept"
    assert lines == ["hub assignment event not yet visible; retrying accept"]


def test_phase_accept_retries_fork_visibility_then_publishes_blocker(
    monkeypatch, tmp_path
):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: ("assignment-row", None),
    )
    request_done: list[bool] = []

    def pending_fork(*_args, **kwargs):
        done = kwargs["fork_request_done"]
        request_done.append(done)
        if not done:
            kwargs["before_fork_create"]()
            kwargs["after_fork_create"]()
        return None

    monkeypatch.setattr(
        "agent_cli.coordinator_github._owner_repository_config", pending_fork
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **kwargs: published.append(kwargs) or ["published"],
    )
    clock = {"now": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: clock["now"])
    task = _accept_task()
    task["payload"]["coordinator"]["source"]["owner_scoped"] = True
    worker = _accept_worker(tmp_path)

    def runner(_argv: list[str]) -> Completed:
        return Completed(0, "", "")

    assert phase_accept(object(), worker, task, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    source = task["payload"]["coordinator"]["source"]
    assert source["fork_requested_at"] == "2026-01-01T00:00:00Z"
    assert source["fork_request_done_at"] == "2026-01-01T00:00:00Z"
    assert task["payload"]["coordinator"]["phase"] == "accept"
    assert request_done == [False]

    clock["now"] = "2026-01-01T00:04:59Z"
    assert phase_accept(object(), worker, task, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    assert request_done == [False, True]
    assert published == []

    clock["now"] = "2026-01-01T00:05:00Z"
    assert phase_accept(object(), worker, task, runner) == ["published"]
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "accept"
    assert checkpoint["blocker"] == (
        "publication fork did not become visible within 300 seconds"
    )
    assert request_done == [False, True, True]
    assert published == [{"kind": "publication-fork", "reply_checkpoint": True}]


def test_phase_accept_reposts_when_request_completion_was_not_persisted(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_common import save_task as real_save_task

    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: ("assignment-row", None),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.scoped",
        lambda _store, _session_id, runner, **_kwargs: runner,
    )
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:00:00Z")
    store = Store(tmp_path)
    task = _accept_task()
    task["id"] = "11111111-1111-4111-8111-111111111111"
    task["payload"]["coordinator"]["source"]["owner_scoped"] = True
    store.write("task", "insert", task["id"], task)
    saves = {"n": 0}

    def crash_on_second_save(store_arg, task_arg):  # noqa: ANN001
        saves["n"] += 1
        if saves["n"] == 2:
            raise RuntimeError("crash after fork POST")
        real_save_task(store_arg, task_arg)

    monkeypatch.setattr("agent_cli.coordinator_github.save_task", crash_on_second_save)
    posts = {"n": 0}

    def runner(argv: list[str]) -> Completed:
        if argv[-2:] == ["--jq", ".default_branch"]:
            return Completed(0, "main", "")
        if argv[-2:] == ["--jq", ".permissions.push"]:
            return Completed(0, "false", "")
        if argv == ["gh", "api", "repos/worker-bot/project"]:
            return Completed(1, "", "not found")
        if argv == ["gh", "api", "-X", "POST", "repos/Example/project/forks"]:
            posts["n"] += 1
            return Completed(0, "{}", "")
        raise AssertionError(argv)

    with pytest.raises(RuntimeError, match="crash after fork POST"):
        phase_accept(store, _accept_worker(tmp_path), store.row("task", task["id"]), runner)
    persisted = store.row("task", task["id"])
    assert persisted is not None
    assert persisted["payload"]["coordinator"]["source"]["fork_requested_at"] == (
        "2026-01-01T00:00:00Z"
    )
    assert posts["n"] == 1

    monkeypatch.setattr("agent_cli.coordinator_github.save_task", real_save_task)
    assert phase_accept(store, _accept_worker(tmp_path), persisted, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    assert posts["n"] == 2
    completed = store.row("task", task["id"])
    assert completed is not None
    assert completed["payload"]["coordinator"]["source"]["fork_request_done_at"] == (
        "2026-01-01T00:00:00Z"
    )
    assert phase_accept(store, _accept_worker(tmp_path), completed, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    assert posts["n"] == 2


def test_phase_accept_crash_after_intent_save_retries_fork_post(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_common import save_task as real_save_task

    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: ("assignment-row", None),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.scoped",
        lambda _store, _session_id, runner, **_kwargs: runner,
    )
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:00:00Z")
    store = Store(tmp_path)
    task = _accept_task()
    task["id"] = "22222222-2222-4222-8222-222222222222"
    task["payload"]["coordinator"]["source"]["owner_scoped"] = True
    store.write("task", "insert", task["id"], task)
    posts = {"n": 0}

    def runner(argv: list[str]) -> Completed:
        if argv[-2:] == ["--jq", ".default_branch"]:
            return Completed(0, "main", "")
        if argv[-2:] == ["--jq", ".permissions.push"]:
            return Completed(0, "false", "")
        if argv == ["gh", "api", "repos/worker-bot/project"]:
            return Completed(1, "", "not found")
        if argv == ["gh", "api", "-X", "POST", "repos/Example/project/forks"]:
            posts["n"] += 1
            return Completed(0, "{}", "")
        raise AssertionError(argv)

    def crash_after_intent(store_arg, task_arg):  # noqa: ANN001
        real_save_task(store_arg, task_arg)
        raise RuntimeError("crash after fork intent save")

    monkeypatch.setattr("agent_cli.coordinator_github.save_task", crash_after_intent)
    with pytest.raises(RuntimeError, match="crash after fork intent save"):
        phase_accept(store, _accept_worker(tmp_path), store.row("task", task["id"]), runner)
    persisted = store.row("task", task["id"])
    assert persisted is not None
    source = persisted["payload"]["coordinator"]["source"]
    assert source["fork_requested_at"] == "2026-01-01T00:00:00Z"
    assert "fork_request_done_at" not in source
    assert posts["n"] == 0

    monkeypatch.setattr("agent_cli.coordinator_github.save_task", real_save_task)
    assert phase_accept(store, _accept_worker(tmp_path), persisted, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    assert posts["n"] == 1


def test_phase_accept_retries_transient_fork_post_and_accepts_in_progress(
    monkeypatch, tmp_path
):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: ("assignment-row", None),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.scoped",
        lambda _store, _session_id, runner, **_kwargs: runner,
    )
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:00:00Z")
    store = Store(tmp_path)
    task = _accept_task()
    task["id"] = "33333333-3333-4333-8333-333333333333"
    task["payload"]["coordinator"]["source"]["owner_scoped"] = True
    store.write("task", "insert", task["id"], task)
    posts = {"n": 0}

    def runner(argv: list[str]) -> Completed:
        if argv[-2:] == ["--jq", ".default_branch"]:
            return Completed(0, "main", "")
        if argv[-2:] == ["--jq", ".permissions.push"]:
            return Completed(0, "false", "")
        if argv == ["gh", "api", "repos/worker-bot/project"]:
            return Completed(1, "", "not found")
        if argv == ["gh", "api", "-X", "POST", "repos/Example/project/forks"]:
            posts["n"] += 1
            if posts["n"] == 1:
                return Completed(1, "", "temporary service failure")
            return Completed(1, "", "fork creation is in progress")
        raise AssertionError(argv)

    with pytest.raises(CoordinatorError, match="temporary service failure"):
        phase_accept(store, _accept_worker(tmp_path), store.row("task", task["id"]), runner)
    persisted = store.row("task", task["id"])
    assert persisted is not None
    source = persisted["payload"]["coordinator"]["source"]
    assert source["fork_requested_at"] == "2026-01-01T00:00:00Z"
    assert "fork_request_done_at" not in source

    assert phase_accept(store, _accept_worker(tmp_path), persisted, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    completed = store.row("task", task["id"])
    assert completed is not None
    assert completed["payload"]["coordinator"]["source"]["fork_request_done_at"] == (
        "2026-01-01T00:00:00Z"
    )
    assert phase_accept(store, _accept_worker(tmp_path), completed, runner) == [
        "publication fork not yet visible; retrying accept"
    ]
    assert posts["n"] == 2


def test_hub_error_retries_accept_and_rate_limits_blocker(monkeypatch, tmp_path):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (None, "hub unavailable"),
    )
    published = []
    clock = {"now": "2026-01-01T01:00:00Z"}
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: clock["now"])
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **kwargs: published.append(kwargs) or ["published"],
    )
    task = _accept_task()
    lines = phase_accept(object(), _accept_worker(tmp_path), task, object())
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "accept"
    assert "retrying accept" in lines[0]
    assert checkpoint["hub_assignment_retry_published_at"] == "2026-01-01T01:00:00Z"
    assert len(published) == 1
    lines = phase_accept(object(), _accept_worker(tmp_path), task, object())
    assert "retrying accept" in lines[0]
    assert len(published) == 1
    clock["now"] = "2026-01-01T02:00:00Z"
    phase_accept(object(), _accept_worker(tmp_path), task, object())
    assert len(published) == 2
    assert checkpoint["hub_assignment_retry_published_at"] == "2026-01-01T02:00:00Z"


@pytest.mark.parametrize(
    "reason",
    [
        "device is not paired with a hub",
        "hub POST /sync/query → HTTP 400: bad matcher",
        "hub POST /sync/query → HTTP 401: no",
        "HTTP 403",
        "hub assignment query truncated",
    ],
)
def test_hub_configuration_errors_block_with_reply_checkpoint(monkeypatch, tmp_path, reason):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (None, reason),
    )
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **kwargs: published.append(kwargs) or ["published"],
    )
    task = _accept_task()
    assert phase_accept(object(), _accept_worker(tmp_path), task, object()) == ["published"]
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "accept"
    assert checkpoint["blocker_publish_once"] is True
    assert published == [{"kind": "configuration-binding", "reply_checkpoint": True}]


def test_runtime_configuration_error_in_ask_keeps_reply_checkpoint(monkeypatch, tmp_path):
    task = _accept_task()
    checkpoint = task["payload"]["coordinator"]
    checkpoint.update(
        phase="ask",
        resume_phase="implement",
        question_activity_id="question-checkpoint",
        replies_consumed_through=41,
    )
    monkeypatch.setattr("agent_cli.coordinator_runtime._open_tasks", lambda *_a, **_k: [task])
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.repo_cfg",
        lambda *_a, **_k: (_ for _ in ()).throw(CoordinatorError("configuration changed")),
    )
    monkeypatch.setattr("agent_cli.coordinator_runtime.save_task", lambda *_a, **_k: None)
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.publish_blocker",
        lambda *_a, **kwargs: published.append(kwargs) or ["published"],
    )
    assert advance_one(
        object(), make_worker(tmp_path), runner=object(), lane_runner=None
    ) == ["published"]
    assert checkpoint["phase"] == "ask"
    assert checkpoint["resume_phase"] == "implement"
    assert checkpoint["question_activity_id"] == "question-checkpoint"
    assert checkpoint["replies_consumed_through"] == 41
    assert checkpoint["binding_blocker"] == "configuration changed"
    assert "blocker_publish_once" not in checkpoint
    assert published == [
        {
            "kind": "configuration-binding",
            "preserve_reply_checkpoint": True,
            "binding_notice": True,
        }
    ]


def test_runtime_configuration_error_in_ci_resumes_when_binding_is_restored(
    monkeypatch, tmp_path
):
    task = _accept_task()
    checkpoint = task["payload"]["coordinator"]
    checkpoint.update(phase="ci", execution_binding="pinned-binding")
    broken = {"value": True}

    def repo_config(*_args, **_kwargs):
        if broken["value"]:
            raise CoordinatorError("configuration changed")
        return object()

    monkeypatch.setattr(
        "agent_cli.coordinator_runtime._open_tasks", lambda *_a, **_k: [task]
    )
    monkeypatch.setattr("agent_cli.coordinator_runtime.repo_cfg", repo_config)
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.execution_binding",
        lambda *_a, **_k: "pinned-binding",
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.save_task", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.publish_blocker",
        lambda *_a, **_k: ["published"],
    )
    monkeypatch.setattr("agent_cli.coordinator_runtime.scoped", lambda *_a, **_k: object())
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.account_for",
        lambda *_a, **_k: SimpleNamespace(login="worker-bot"),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.verify_issue_assigned", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.phase_ci", lambda *_a, **_k: ["CI resumed"]
    )

    assert advance_one(
        object(), make_worker(tmp_path), runner=object(), lane_runner=None
    ) == ["published"]
    assert checkpoint["phase"] == "ci"
    assert checkpoint["binding_blocker"] == "configuration changed"
    broken["value"] = False
    assert advance_one(
        object(), make_worker(tmp_path), runner=object(), lane_runner=None
    ) == ["CI resumed"]
    assert checkpoint["phase"] == "ci"
    assert "binding_blocker" not in checkpoint
    assert "binding_blocker_activity_id" not in checkpoint


def test_restored_binding_retries_failed_accept_blocker_without_overwriting_it(
    monkeypatch, tmp_path
):
    task = _accept_task()
    checkpoint = task["payload"]["coordinator"]
    checkpoint.update(
        phase="blocked",
        resume_phase="accept",
        execution_binding="pinned-binding",
        blocker="device is not paired with a hub",
        blocker_publish_once=True,
        blocker_publish_kind="configuration-binding",
        question_activity_id="older-question",
    )
    broken = {"value": True}

    def repo_config(*_args, **_kwargs):
        if broken["value"]:
            raise CoordinatorError("configuration changed")
        return object()

    posts = []

    def post(*_args, **kwargs):
        posts.append(kwargs["body"])
        return f"published-{len(posts)}"

    monkeypatch.setattr("agent_cli.coordinator_runtime._open_tasks", lambda *_a, **_k: [task])
    monkeypatch.setattr("agent_cli.coordinator_runtime.repo_cfg", repo_config)
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.execution_binding",
        lambda *_a, **_k: "pinned-binding",
    )
    monkeypatch.setattr("agent_cli.coordinator_runtime.save_task", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_cli.coordinator_github.save_task", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_cli.coordinator_github.post_issue_comment", post)

    advance_one(object(), make_worker(tmp_path), runner=object(), lane_runner=None)
    assert checkpoint["blocker"] == "device is not paired with a hub"
    assert "blocker_activity_id" not in checkpoint
    assert checkpoint["binding_blocker"] == "configuration changed"
    assert checkpoint["binding_blocker_activity_id"] == "published-1"

    broken["value"] = False
    advance_one(object(), make_worker(tmp_path), runner=object(), lane_runner=None)
    assert posts == [
        "Blocked: configuration changed",
        "Blocked: device is not paired with a hub",
    ]
    assert checkpoint["blocker"] == "device is not paired with a hub"
    assert checkpoint["blocker_activity_id"] == "published-2"
    assert "binding_blocker" not in checkpoint
    assert "binding_blocker_activity_id" not in checkpoint


def test_published_configuration_blocker_is_not_republished(monkeypatch, tmp_path):
    task = _accept_task()
    checkpoint = task["payload"]["coordinator"]
    checkpoint.update(
        phase="blocked",
        resume_phase="accept",
        blocker="device is not paired with a hub",
        blocker_publish_once=True,
        blocker_publish_kind="configuration-binding",
        question_activity_id="published-checkpoint",
        blocker_activity_id="published-blocker",
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.phase_read_replies",
        lambda *_a, **_k: ["waiting for authorized reply"],
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **_k: pytest.fail("published configuration blocker must not repeat"),
    )
    assert phase_blocked(
        object(), make_worker(tmp_path), task, object()
    ) == ["waiting for authorized reply"]


def test_failed_configuration_blocker_post_is_retried(monkeypatch, tmp_path):
    task = _accept_task()
    checkpoint = task["payload"]["coordinator"]
    checkpoint.update(
        phase="blocked",
        resume_phase="accept",
        blocker="device is not paired with a hub",
        blocker_publish_once=True,
        blocker_publish_kind="configuration-binding",
        question_activity_id="older-question",
        replies_consumed_through=41,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.phase_read_replies",
        lambda *_a, **_k: ["waiting for authorized reply"],
    )
    monkeypatch.setattr("agent_cli.coordinator_github.save_task", lambda *_a, **_k: None)
    attempts = []

    def post(*_args, **_kwargs):
        attempts.append(_kwargs)
        if len(attempts) == 1:
            raise CoordinatorError("post failed")
        return "published-blocker"

    monkeypatch.setattr("agent_cli.coordinator_github.post_issue_comment", post)
    worker = make_worker(tmp_path)
    first = phase_blocked(object(), worker, task, object())
    assert "blocker publish failed" in first[-1]
    assert "blocker_activity_id" not in checkpoint
    phase_blocked(object(), worker, task, object())
    assert checkpoint["blocker_activity_id"] == "published-blocker"
    assert checkpoint["question_activity_id"] == "published-blocker"
    assert checkpoint["replies_consumed_through"] is None
    assert len(attempts) == 2
    phase_blocked(object(), worker, task, object())
    assert len(attempts) == 2


def test_owner_scoped_accept_pins_effective_repository_binding(monkeypatch, tmp_path):
    _patch_accept_prerequisites(monkeypatch)
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-ready",),
    )
    effective = RepositoryConfig(
        "Example/owned", "main", "worker-bot/owned",
        ("/operator/check",), ("/operator/owner-ready",),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._owner_repository_config",
        lambda *_a, **_k: effective,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.execution_binding",
        lambda *_a, **_k: "pinned-binding",
    )
    monkeypatch.setattr("agent_cli.coordinator_github.queue_activity", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_cli.coordinator_github.execute_github", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_cli.coordinator_github.set_checklist", lambda *_a, **_k: None)

    class AcceptedStore:
        def row(self, table, _row_id):
            assert table == "activity"
            return {"execution_status": "done"}

    task = _accept_task()
    task["payload"]["coordinator"]["source"].update(
        repo="Example/owned", owner_scoped=True
    )
    lines = phase_accept(AcceptedStore(), worker, task, object())
    checkpoint = task["payload"]["coordinator"]
    source = checkpoint["source"]
    assert checkpoint["execution_binding"] == "pinned-binding"
    assert source["base"] == "main"
    assert source["publication_repo"] == "worker-bot/owned"
    assert source["repository_config"]["check_argv"] == ["/operator/check"]
    assert checkpoint["phase"] == "checkout"
    assert "acceptance published" in lines[0]


def test_owner_a38_default_readiness_closes_human_steps_and_reaches_await_ready(
    monkeypatch, tmp_path
):
    from agent_cli.allow import CHECKLIST_KEYS

    runner = FakeGh()
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    head = "a" * 40
    base = "b" * 40
    task_id = "11111111-1111-4111-8111-111111111111"
    task = {
        "id": task_id,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "pr-review",
        "repo": "Example/owned",
        "ref": "42",
        "models": {"first": "grok", "second": None},
        "payload": {"coordinator": {
            "phase": "readiness",
            "head_sha": head,
            "base_sha": base,
            "worktree": str(worker.workspace_root / task_id),
            "pr_number": 42,
            "evidence": {"tests_pass": True, "tests_head": head},
            "source": {
                "repo": "Example/owned",
                "number": 7,
                "owner_scoped": True,
                "assignment_actor": "human-owner",
                "assignment_event_id": 70,
                "hub_assignment_row_id": "assignment-row",
                "repository_config": {
                    "repo": "Example/owned",
                    "base": "main",
                    "publication_repo": "worker-bot/owned",
                    "check_argv": [],
                    "readiness_argv": [],
                },
            },
            "a38": {"enabled": True},
        }},
    }
    store.write("task", "insert", task_id, task)
    for key in CHECKLIST_KEYS["implement"]:
        status = "pending" if key in {
            "contributing_ok", "deviation_declared", "deviation_granted",
        } else "ja"
        store.write(
            "checklist_item",
            "insert",
            f"{task_id}-{key}",
            {
                "id": f"{task_id}-{key}",
                "task_id": task_id,
                "key": key,
                "status": status,
                "evidence": "seed" if status == "ja" else None,
                "source": "human" if key == "spec_written" else "script",
            },
        )
    for dimension in ("quality", "logic"):
        store.write(
            "review_gate",
            "insert",
            f"{task_id}-{dimension}",
            {
                "id": f"{task_id}-{dimension}",
                "task_id": task_id,
                "stage": "grok-pr",
                "dimension": dimension,
                "vendor": "grok",
                "verdict": "approved",
                "head_sha": head,
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )

    monkeypatch.setattr(
        "agent_cli.coordinator_github.verify_signed_clean_head",
        lambda *_args: head,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._fetch_pr_json",
        lambda *_args: {
            "headRefOid": head,
            "baseRefName": "main",
            "author": {"login": "worker-bot"},
            "isDraft": True,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
        },
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.account_for",
        lambda *_args: SimpleNamespace(login="worker-bot"),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._fresh_ci_still_green",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.require_ready_evidence",
        lambda *_args: None,
    )

    lines = phase_readiness(store, worker, store.row("task", task_id), runner)
    assert "readiness ok" in lines[0]
    saved = store.row("task", task_id)
    assert saved["payload"]["coordinator"]["phase"] == "await_ready"
    checklist = {
        row["key"]: row
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    assert checklist["contributing_ok"]["source"] == "script"
    for key in ("deviation_declared", "deviation_granted"):
        assert checklist[key]["status"] == "n_a"
        assert checklist[key]["source"] == "human"
        assert "actor=human-owner" in checklist[key]["evidence"]
        assert "event_id=70" in checklist[key]["evidence"]
        assert "hub_assignment_row_id=assignment-row" in checklist[key]["evidence"]


def test_owner_a38_default_readiness_without_verified_actor_requires_configuration(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "agent_cli.coordinator_github.set_checklist",
        lambda *_args, **_kwargs: pytest.fail("checklist must remain unchanged"),
    )

    class GateStore:
        def rows(self, table):
            assert table == "review_gate"
            return [{
                "task_id": "task-1",
                "stage": "grok-pr",
                "dimension": "quality",
                "verdict": "approved",
                "head_sha": "a" * 40,
                "recorded_at": "2026-01-01T00:00:00Z",
            }]

    with pytest.raises(CoordinatorError, match="configure owner_readiness_argv"):
        _apply_readiness_checklist(
            GateStore(),
            replace(make_worker(tmp_path), a38=True, review_session=None),
            _accept_task(),
            {"owner_a38_default": True, "deviation": {"declared": False}},
            head="a" * 40,
        )


@pytest.mark.parametrize("verdict,head", [("rejected", "a" * 40), ("approved", "b" * 40)])
def test_owner_a38_default_readiness_requires_current_approved_quality_gate(
    monkeypatch, tmp_path, verdict, head
):
    monkeypatch.setattr(
        "agent_cli.coordinator_github.set_checklist",
        lambda *_a, **_k: pytest.fail("checklist must remain unchanged"),
    )

    class GateStore:
        def rows(self, table):
            assert table == "review_gate"
            return [{
                "task_id": "task-1", "stage": "grok-pr", "dimension": "quality",
                "verdict": verdict, "head_sha": head,
                "recorded_at": "2026-01-01T00:00:00Z",
            }]

    with pytest.raises(CoordinatorError, match="approved grok-pr/quality"):
        _apply_readiness_checklist(
            GateStore(),
            replace(make_worker(tmp_path), a38=True, review_session=None),
            _accept_task(),
            {"owner_a38_default": True, "deviation": {"declared": False}},
            head="a" * 40,
        )
