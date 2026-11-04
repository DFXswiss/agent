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
)
from agent_cli.coordinator_config import RepositoryConfig
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_runtime import discover_assignments
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
    assert cfg.publication_repo == "worker-bot/project"
    assert posts == 1
    assert lookups == 2


def test_wrong_parent_fork_is_blocked():
    def runner(_argv: list[str]) -> Completed:
        return Completed(0, json.dumps({"parent": {"full_name": "Other/project"}}), "")

    with pytest.raises(CoordinatorError, match="different parent"):
        _fork_repository(
            runner, repo="Example/project", publication_repo="worker-bot/project",
            sleep_fn=lambda _seconds: None,
        )


def test_fork_creation_wait_is_bounded_to_ten_lookups():
    lookups = 0
    sleeps = 0

    def runner(argv: list[str]) -> Completed:
        nonlocal lookups
        if "-X" in argv:
            return Completed(0, "{}", "")
        lookups += 1
        return Completed(1, "", "not found" if lookups == 1 else "not ready")

    def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1

    with pytest.raises(CoordinatorError, match="10 tries"):
        _fork_repository(
            runner, repo="Example/project", publication_repo="worker-bot/project", sleep_fn=sleep
        )
    assert lookups == 11  # one existence check, then ten bounded readiness checks
    assert sleeps == 9


class QueryHub:
    result = {"rows": []}
    error: Exception | None = None

    def __init__(self, _url: str, _token: str) -> None:
        pass

    def query(self, match):
        assert match == {"type": "assignment.created", "payload.repo": "Example/project"}
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        pass


class MetaStore:
    def meta(self, key: str):
        return {"hub_url": "https://hub.example", "device_token": "token"}.get(key)


def assignment_row(*, actor="human-owner", agent="worker-bot", number=7):
    return {
        "row_id": "assignment-row",
        "github_login": actor,
        "payload": {
            "type": "assignment.created",
            "payload": {"repo": "Example/project", "number": number, "agent": agent},
        },
    }


@pytest.mark.parametrize(
    "row,reply_logins,matched",
    [
        (assignment_row(), ("human-owner",), True),
        (assignment_row(actor="other"), ("human-owner",), False),
        (assignment_row(), ("different-owner",), False),
        (assignment_row(agent="other-agent"), ("human-owner",), False),
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
    monkeypatch.setattr("agent_cli.coordinator_github.verify_issue_assigned", lambda *_a, **_k: {})
    monkeypatch.setattr("agent_cli.coordinator_github.save_task", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "agent_cli.coordinator_github.gh_list",
        lambda *_a, **_k: [{
            "id": 70, "event": "assigned", "assignee": {"login": "worker-bot"},
            "actor": {"login": "human-owner", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
        }],
    )


def test_missing_hub_assignment_publishes_blocker_and_never_starts_acceptance(monkeypatch, tmp_path):
    _patch_accept_prerequisites(monkeypatch)
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
    assert checkpoint["phase"] == "accept"
    assert checkpoint["blocker"] == "assignment was not issued through the hub"
    assert published[0]["kind"] == "hub-assignment"
    assert published[0]["reply_checkpoint"] is True


def test_hub_error_retries_accept_without_publishing_blocker(monkeypatch, tmp_path):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (None, "hub unavailable"),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_a, **_k: pytest.fail("transient hub errors must not spam blockers"),
    )
    task = _accept_task()
    lines = phase_accept(object(), _accept_worker(tmp_path), task, object())
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "accept"
    assert "retrying accept" in lines[0]


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


def test_owner_a38_default_readiness_uses_conformity_evidence(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.set_checklist",
        lambda _store, _task, key, status, evidence, **kwargs:
            calls.append((key, status, evidence, kwargs["source"])),
    )
    task = _accept_task()
    _apply_readiness_checklist(
        object(),
        replace(make_worker(tmp_path), a38=True, review_session=None),
        task,
        {"owner_a38_default": True, "deviation": {"declared": False}},
        head="a" * 40,
    )
    assert calls == [
        ("contributing_ok", "ja", f"conformity-a approved at {'a' * 40}", "script"),
        ("deviation_declared", "n_a", "no deviation declared", "script"),
        ("deviation_granted", "n_a", "no deviation declared", "script"),
    ]
