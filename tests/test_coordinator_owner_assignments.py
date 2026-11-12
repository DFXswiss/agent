from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli.coordinator_github import (
    HUB_CONFIGURATION_BLOCKER,
    HubAssignmentMandate,
    MANDATE_DEVIATION_BLOCKER,
    _apply_readiness_checklist,
    _fork_repository,
    _hub_assignment_row,
    _owner_repository_config,
    phase_accept,
    phase_blocked,
    phase_readiness,
)
from agent_cli.coordinator_config import RepositoryConfig
from agent_cli.coordinator_common import CoordinatorError, NonRecoverableCoordinatorError
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
    search_incomplete = False
    search_as_dict = False
    search_total_count: int | None = None
    owner_type = "User"

    def __init__(self) -> None:
        super().__init__()
        self.search_queries: list[str] = []
        self.search_pages: list[dict] | None = None

    def __call__(self, argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if argv[-1] == "users/example":
            return Completed(
                0,
                json.dumps({"login": "example", "type": self.owner_type}),
                "",
            )
        if "search/issues?q=is:issue+is:open" in joined:
            self.search_queries.append(joined)
            qualifier = "org" if self.owner_type == "Organization" else "user"
            assert f"+{qualifier}:example&per_page=100" in joined
            if self.search_pages is not None:
                return Completed(0, json.dumps(self.search_pages), "")
            items = [
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
            ]
            page = {
                "incomplete_results": self.search_incomplete,
                "total_count": self.search_total_count or len(items),
                "items": items,
            }
            return Completed(0, json.dumps(page if self.search_as_dict else [page]), "")
        if argv[:2] == ["gh", "api"] and argv[-1] == "repos/Example/owned/issues/8":
            return Completed(0, json.dumps({
                "number": 8, "state": "open", "title": "Owned issue", "body": "Do it",
                "html_url": "https://github.com/Example/owned/issues/8",
                "assignees": [{"login": "worker-bot"}],
            }), "")
        return super().__call__(argv)


@pytest.mark.parametrize("owner_type", ["User", "Organization"])
def test_owner_discovery_filters_results_and_explicit_repository_wins(
    tmp_path, monkeypatch, owner_type
):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.owner_type = owner_type
    patch_account_runners(monkeypatch, fake)
    lines = discover_assignments(store, worker, fake)
    assert len(fake.search_queries) == 1
    assert sum("example/project#7" in line for line in lines) == 1
    assert sum("Example/owned#8" in line for line in lines) == 1
    sources = [
        row["payload"]["coordinator"]["source"] for row in store.rows("task")
    ]
    explicit = next(source for source in sources if source["repo"].casefold() == "example/project")
    owned = next(source for source in sources if source["repo"] == "Example/owned")
    assert "owner_scoped" not in explicit
    assert owned["owner_scoped"] is True
    assert not any(source["number"] in {9, 10, 11} for source in sources)
    store.close()


@pytest.mark.parametrize("as_dict", [False, True])
def test_owner_discovery_rejects_incomplete_search_results(tmp_path, monkeypatch, as_dict):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.search_incomplete = True
    fake.search_as_dict = as_dict
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert "discover owner example: owner issue search returned incomplete results" in lines
    sources = [
        row["payload"]["coordinator"]["source"] for row in store.rows("task")
    ]
    assert any(source["repo"].casefold() == "example/project" for source in sources)
    assert not any(source["repo"] == "Example/owned" for source in sources)
    store.close()


@pytest.mark.parametrize(
    "page",
    [
        {"total_count": 0, "items": []},
        {"incomplete_results": "true", "total_count": 0, "items": []},
        {"incomplete_results": 0, "total_count": 0, "items": []},
    ],
)
def test_owner_discovery_requires_boolean_false_incomplete_results(
    tmp_path, monkeypatch, page
):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.search_pages = [page]
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert (
        "discover owner example: owner issue search returned incomplete results"
        in lines
    )
    sources = [
        row["payload"]["coordinator"]["source"] for row in store.rows("task")
    ]
    assert not any(source.get("owner_scoped") is True for source in sources)
    store.close()


def test_owner_discovery_rejects_total_count_truncation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.search_total_count = 1001
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert "discover owner example: owner issue search was truncated (5 of 1001 results)" in lines
    sources = [row["payload"]["coordinator"]["source"] for row in store.rows("task")]
    assert any(source["repo"].casefold() == "example/project" for source in sources)
    assert not any(source["repo"] == "Example/owned" for source in sources)
    store.close()


def test_owner_discovery_rejects_total_count_changes_between_pages(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.search_pages = [
        {
            "incomplete_results": False,
            "total_count": 1,
            "items": [
                {
                    **fake.issues[0],
                    "repository_url": "https://api.github.com/repos/example/project",
                }
            ],
        },
        {
            "incomplete_results": False,
            "total_count": 2,
            "items": [
                {
                    "number": 8,
                    "state": "open",
                    "repository_url": "https://api.github.com/repos/Example/owned",
                }
            ],
        },
    ]
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert (
        "discover owner example: owner issue search returned inconsistent "
        "total_count across pages"
    ) in lines
    sources = [
        row["payload"]["coordinator"]["source"] for row in store.rows("task")
    ]
    assert not any(source.get("owner_scoped") is True for source in sources)
    store.close()


def test_owner_discovery_rejects_negative_page_total_count(tmp_path, monkeypatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.search_pages = [
        {"incomplete_results": False, "total_count": -1, "items": []}
    ]
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert (
        "discover owner example: owner issue search page has negative total_count"
        in lines
    )
    sources = [
        row["payload"]["coordinator"]["source"] for row in store.rows("task")
    ]
    assert not any(source.get("owner_scoped") is True for source in sources)
    store.close()


def test_owner_discovery_rejects_duplicate_identity_hiding_missing_issue(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    items = [
        {
            "number": number,
            "state": "open",
            "repository_url": f"https://api.github.com/repos/Example/project-{number}",
        }
        for number in range(1, 101)
    ]
    items.append(dict(items[0]))
    fake.search_pages = [
        {"incomplete_results": False, "total_count": 101, "items": items}
    ]
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert any(
        line.startswith(
            "discover owner example: owner issue search returned incomplete results "
            "(duplicate issue identity example/project-1#1)"
        )
        for line in lines
    )
    sources = [row["payload"]["coordinator"]["source"] for row in store.rows("task")]
    assert any(source["repo"].casefold() == "example/project" for source in sources)
    assert not any(source.get("owner_scoped") is True for source in sources)
    store.close()


@pytest.mark.parametrize(
    "pages",
    [
        [],
        [
            {
                "incomplete_results": False,
                "total_count": 1,
                "items": [{"number": 1, "state": "open"}],
            }
        ],
        [
            {
                "incomplete_results": False,
                "total_count": 1,
                "items": [
                    {
                        "number": 1,
                        "state": "open",
                        "repository_url": "https://api.github.com/repos/Example/one",
                    },
                    {
                        "number": 2,
                        "state": "open",
                        "repository_url": "https://api.github.com/repos/Example/two",
                    },
                ],
            }
        ],
    ],
)
def test_owner_discovery_rejects_invalid_page_sets_and_issue_identities(
    tmp_path, monkeypatch, pages
):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-readiness",),
    )
    fake = OwnerDiscoveryGh()
    fake.search_pages = pages
    patch_account_runners(monkeypatch, fake)

    lines = discover_assignments(store, worker, fake)

    assert any(
        line.startswith(
            "discover owner example: owner issue search returned incomplete results"
        )
        for line in lines
    )
    sources = [row["payload"]["coordinator"]["source"] for row in store.rows("task")]
    assert any(source["repo"].casefold() == "example/project" for source in sources)
    assert not any(source.get("owner_scoped") is True for source in sources)
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


def verified_mandate():
    return HubAssignmentMandate(
        "assignment-row",
        "human-owner",
        "web:human-owner",
        "Example/project#7",
        {"declared": False, "stated_by": "human-owner"},
    )


def assignment_row(
    *, actor="human-owner", payload_actor=None, agent="worker-bot", number=7,
    repo="Example/project", origin_device_id=None, deviation=None,
):
    if payload_actor is None:
        payload_actor = actor
    if origin_device_id is None:
        origin_device_id = f"web:{actor}"
    if deviation is None:
        deviation = {"declared": False, "stated_by": actor}
    return {
        "row_id": "assignment-row",
        "github_login": actor,
        "origin_device_id": origin_device_id,
        "payload": {
            "type": "assignment.created",
            "payload": {
                "repo": repo, "number": number, "agent": agent, "actor": payload_actor,
                "issue_key": f"{repo}#{number}", "deviation": deviation,
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
    mandate, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=reply_logins,
    )
    assert retry is None
    assert (mandate is not None and mandate.row_id == "assignment-row") is matched
    if matched:
        assert mandate is not None
        assert mandate.deviation == {
            "declared": False,
            "stated_by": mandate.actor,
        }


@pytest.mark.parametrize("newer_first", [True, False])
@pytest.mark.parametrize("newer_declares", [False, True])
def test_hub_assignment_uses_the_newest_mandate_row(monkeypatch, newer_first, newer_declares):
    older = assignment_row(deviation={"declared": True} if not newer_declares else None)
    older["row_id"] = "older-row"
    older["updated_at"] = "2026-01-01T00:00:00Z"
    newer = assignment_row(deviation={"declared": True} if newer_declares else None)
    newer["row_id"] = "newer-row"
    newer["updated_at"] = "2026-01-02T00:00:00Z"
    QueryHub.result = {"rows": [newer, older] if newer_first else [older, newer]}
    QueryHub.error = None
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)

    mandate, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
    )

    assert retry is None
    assert mandate is not None and mandate.row_id == "newer-row"
    assert (mandate.deviation == {"declared": True}) is newer_declares


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
    mandate, retry = _hub_assignment_row(
        MetaStore(), repo="example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
        canonical_repo="Example/project",
    )
    assert mandate is not None and mandate.row_id == "assignment-row"
    assert retry is None
    assert seen == [{
        "type": "assignment.created",
        "payload.issue_key": {
            "in": ["example/project#7", "Example/project#7"],
        },
    }]


@pytest.mark.parametrize("issue_key", [None, "Example/project#8"])
def test_hub_assignment_requires_matching_inner_issue_key(monkeypatch, issue_key):
    row = assignment_row()
    if issue_key is None:
        row["payload"]["payload"].pop("issue_key")
    else:
        row["payload"]["payload"]["issue_key"] = issue_key
    QueryHub.result = {"rows": [row]}
    QueryHub.error = None
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)

    mandate, retry = _hub_assignment_row(
        MetaStore(),
        repo="Example/project",
        number=7,
        agent="worker-bot",
        assignment_actor="human-owner",
        reply_logins=("human-owner",),
    )

    assert mandate is None
    assert retry is None


def test_hub_assignment_query_cap_is_retry_not_definite_miss(monkeypatch):
    QueryHub.result = {"rows": [assignment_row()] * 500}
    QueryHub.error = None
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)
    mandate, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
    )
    assert mandate is None
    assert retry == "hub assignment query truncated"


def test_hub_assignment_transport_error_requests_same_phase_retry(monkeypatch):
    QueryHub.error = HubError("temporarily unavailable")
    monkeypatch.setattr("agent_cli.coordinator_github.Hub", QueryHub)
    mandate, retry = _hub_assignment_row(
        MetaStore(), repo="Example/project", number=7, agent="worker-bot",
        assignment_actor="human-owner", reply_logins=("human-owner",),
    )
    assert mandate is None
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


def test_owner_a38_default_requires_hub_mandate_without_configuration_flag(
    monkeypatch, tmp_path
):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:02:01Z")
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (None, None),
    )
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_args, **kwargs: published.append((_args[4], kwargs)) or ["published"],
    )
    task = _accept_task()
    task["payload"]["coordinator"]["source"]["owner_scoped"] = True
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
        require_hub_assignment=False,
    )

    assert phase_accept(object(), worker, task, object()) == ["published"]
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["blocker"] == MANDATE_DEVIATION_BLOCKER
    assert published == [
        (MANDATE_DEVIATION_BLOCKER, {"kind": "hub-assignment", "reply_checkpoint": True})
    ]


def test_phase_accept_retries_fork_visibility_then_publishes_blocker(
    monkeypatch, tmp_path
):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (verified_mandate(), None),
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
    assert source["hub_assignment_mandate"]["deviation"] == {
        "declared": False,
        "stated_by": "human-owner",
    }
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
        lambda *_a, **_k: (verified_mandate(), None),
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
        lambda *_a, **_k: (verified_mandate(), None),
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
    from agent_cli.coordinator_common import save_task as real_save_task

    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_github.save_task", real_save_task)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (verified_mandate(), None),
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
        lambda *_a, **_k: (None, "hub request failed: [Errno 8] hub.internal.example"),
    )
    published = []
    clock = {"now": "2026-01-01T01:00:00Z"}
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: clock["now"])
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *args, **kwargs: published.append((args[4], kwargs)) or ["published"],
    )
    task = _accept_task()
    lines = phase_accept(object(), _accept_worker(tmp_path), task, object())
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "accept"
    assert "retrying accept" in lines[0]
    assert checkpoint["hub_assignment_retry_published_at"] == "2026-01-01T01:00:00Z"
    assert len(published) == 1
    assert "hub.internal.example" not in published[0][0]
    assert checkpoint["hub_assignment_retry"] == "hub temporarily unavailable"
    assert any("hub.internal.example" in line for line in lines)
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
    messages = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *args, **kwargs: (published.append(kwargs), messages.append(args[4]))
        and ["published"],
    )
    task = _accept_task()
    assert phase_accept(object(), _accept_worker(tmp_path), task, object()) == ["published"]
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "accept"
    assert checkpoint["blocker_publish_once"] is True
    assert published == [{"kind": "configuration-binding", "reply_checkpoint": True}]
    assert messages == [HUB_CONFIGURATION_BLOCKER]
    assert checkpoint["hub_assignment_retry"] == "hub configuration error"


def test_runtime_configuration_error_in_ask_keeps_reply_checkpoint(monkeypatch, tmp_path):
    write_accounts(tmp_path)
    store = SimpleNamespace(home=tmp_path)
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
        store, make_worker(tmp_path), runner=object(), lane_runner=None
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
    write_accounts(tmp_path)
    store = SimpleNamespace(home=tmp_path)
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
        store, make_worker(tmp_path), runner=object(), lane_runner=None
    ) == ["published"]
    assert checkpoint["phase"] == "ci"
    assert checkpoint["binding_blocker"] == "configuration changed"
    broken["value"] = False
    assert advance_one(
        store, make_worker(tmp_path), runner=object(), lane_runner=None
    ) == ["CI resumed"]
    assert checkpoint["phase"] == "ci"
    assert "binding_blocker" not in checkpoint
    assert "binding_blocker_activity_id" not in checkpoint


def test_restored_binding_retries_failed_accept_blocker_without_overwriting_it(
    monkeypatch, tmp_path
):
    write_accounts(tmp_path)
    store = SimpleNamespace(home=tmp_path)
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

    advance_one(store, make_worker(tmp_path), runner=object(), lane_runner=None)
    assert checkpoint["blocker"] == "device is not paired with a hub"
    assert "blocker_activity_id" not in checkpoint
    assert checkpoint["binding_blocker"] == "configuration changed"
    assert checkpoint["binding_blocker_activity_id"] == "published-1"

    broken["value"] = False
    advance_one(store, make_worker(tmp_path), runner=object(), lane_runner=None)
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


def _patch_current_models(monkeypatch, models):
    monkeypatch.setattr(
        "agent_cli.coordinator_github.load_ai_accounts",
        lambda *_a, **_k: SimpleNamespace(models=SimpleNamespace(as_dict=lambda: dict(models))),
    )


@pytest.mark.parametrize(
    ("current_models", "a38"),
    [({"first": "codex", "second": "grok"}, False), ({"first": "grok", "second": "codex"}, True)],
)
def test_owner_scoped_accept_refuses_models_changed_since_task_creation(
    monkeypatch, tmp_path, current_models, a38
):
    _patch_accept_prerequisites(monkeypatch)
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_readiness_argv=("/operator/owner-ready",),
        a38=a38,
    )
    effective = RepositoryConfig(
        "Example/owned", "main", "worker-bot/owned",
        ("/operator/check",), ("/operator/owner-ready",),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._owner_repository_config",
        lambda *_a, **_k: effective,
    )
    _patch_current_models(monkeypatch, current_models)
    binding_calls: list[object] = []
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.execution_binding",
        lambda *a, **_k: binding_calls.append(a) or "pinned-binding",
    )
    task = _accept_task()
    task["models"] = {"first": "grok", "second": "codex"}
    task["payload"]["coordinator"]["source"].update(
        repo="Example/owned", owner_scoped=True
    )

    with pytest.raises(CoordinatorError, match="model configuration changed before acceptance"):
        phase_accept(SimpleNamespace(home=tmp_path), worker, task, object())
    assert binding_calls == []
    assert "execution_binding" not in task["payload"]["coordinator"]
    assert "repository_config" not in task["payload"]["coordinator"]["source"]


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
    _patch_current_models(monkeypatch, {"first": "grok", "second": "codex"})
    monkeypatch.setattr("agent_cli.coordinator_github.queue_activity", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_cli.coordinator_github.execute_github", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_cli.coordinator_github.set_checklist", lambda *_a, **_k: None)

    class AcceptedStore:
        home = tmp_path

        def row(self, table, _row_id):
            assert table == "activity"
            return {"execution_status": "done"}

    task = _accept_task()
    task["models"] = {"first": "grok", "second": "codex"}
    task["payload"]["coordinator"]["source"].update(
        repo="Example/owned", owner_scoped=True
    )
    lines = phase_accept(AcceptedStore(), worker, task, object())
    checkpoint = task["payload"]["coordinator"]
    source = checkpoint["source"]
    assert checkpoint["execution_binding"] == "pinned-binding"
    assert source["base"] == "main"
    assert source["publication_repo"] == "worker-bot/owned"
    assert source["repository_config"] == {
        "repo": "Example/owned", "base": "main", "publication_repo": "worker-bot/owned"
    }
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
                "hub_assignment_mandate": {
                    "row_id": "assignment-row",
                    "actor": "human-owner",
                    "origin": "web:human-owner",
                    "issue_key": "Example/owned#7",
                    "deviation": {
                        "declared": False,
                        "stated_by": "human-owner",
                    },
                },
                "repository_config": {
                    "repo": "Example/owned",
                    "base": "main",
                    "publication_repo": "worker-bot/owned",
                    "check_argv": [],
                    "readiness_argv": [],
                },
            },
            "a38": {
                "enabled": True,
                "base_sha": base,
                "policy_revision": base,
                "report": {
                    "head": head,
                    "base_sha": base,
                    "policy_revision": base,
                },
                "first_comment": {"head": head},
                "second_review": {
                    "head": head,
                    "status": "posted",
                    "reviews": [
                        {
                            "reviewer": "outside-reviewer",
                            "blocks_merge": False,
                            "at": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
            },
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
    external_review_evidence = (
        f"external second review by outside-reviewer at {head}: "
        "https://github.com/example/owned/pull/42#issuecomment-1"
    )
    for row in store.rows("checklist_item"):
        if row.get("task_id") != task_id or row.get("key") not in {
            "review_2_quality",
            "review_2_logic",
        }:
            continue
        updated = {
            key: value for key, value in row.items() if not key.startswith("_")
        }
        updated["evidence"] = external_review_evidence
        updated["source"] = "script"
        store.write("checklist_item", "update", row["id"], updated)
    for dimension in ("quality", "logic"):
        store.write(
            "review_gate",
            "insert",
            f"{task_id}-{dimension}",
            {
                "id": f"{task_id}-{dimension}",
                "task_id": task_id,
                "stage": "review-1",
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
            "baseRefOid": base,
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
        assert checklist[key]["evidence"] == (
            "hub mandate assignment-row: human-owner stated deviation declared=false"
        )


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
                "id": "gate-1",
                "_origin_device_id": "test-device",
                "task_id": "task-1",
                "stage": "review-1",
                "dimension": "quality",
                "verdict": "approved",
                "head_sha": "a" * 40,
                "recorded_at": "2026-01-01T00:00:00Z",
            }]

        def row_insert_sequences(self, table):
            return {
                row["id"]: (row["_origin_device_id"], sequence)
                for sequence, row in enumerate(self.rows(table), start=1)
            }

    with pytest.raises(CoordinatorError, match="configure owner_readiness_argv"):
        _apply_readiness_checklist(
            GateStore(),
            replace(make_worker(tmp_path), a38=True, review_session=None),
            _accept_task(),
            {"owner_a38_default": True, "deviation": {"declared": False}},
            head="a" * 40,
        )


@pytest.mark.parametrize(
    "mandate",
    [
        None,
        {
            "row_id": "assignment-row",
            "actor": "human-owner",
            "origin": "web:human-owner",
            "issue_key": "Example/project#7",
        },
        {
            "row_id": "assignment-row",
            "actor": "human-owner",
            "origin": "web:human-owner",
            "issue_key": "Example/project#7",
            "deviation": {"declared": True, "stated_by": "human-owner"},
        },
        {
            "row_id": "assignment-row",
            "actor": "human-owner",
            "origin": "web:human-owner",
            "issue_key": "Example/project#7",
            "deviation": {"declared": False, "stated_by": "different-owner"},
        },
    ],
    ids=["no-hub-row", "missing-field", "declared-true", "stated-by-mismatch"],
)
def test_owner_a38_default_refuses_non_explicit_deviation_statements(
    monkeypatch, tmp_path, mandate
):
    calls = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.set_checklist",
        lambda _store, _task, key, status, evidence, **kwargs: calls.append(
            (key, status, evidence, kwargs)
        ),
    )

    class GateStore:
        def rows(self, table):
            assert table == "review_gate"
            return [{
                "id": "gate-1",
                "_origin_device_id": "test-device",
                "task_id": "task-1",
                "stage": "review-1",
                "dimension": "quality",
                "verdict": "approved",
                "head_sha": "a" * 40,
                "recorded_at": "2026-01-01T00:00:00Z",
            }]

        def row_insert_sequences(self, table):
            return {
                row["id"]: (row["_origin_device_id"], sequence)
                for sequence, row in enumerate(self.rows(table), start=1)
            }

    task = _accept_task()
    source = task["payload"]["coordinator"]["source"]
    source.update(
        assignment_actor="human-owner",
        assignment_event_id=70,
        hub_assignment_row_id="assignment-row" if mandate is not None else None,
    )
    if mandate is not None:
        source["hub_assignment_mandate"] = mandate

    with pytest.raises(CoordinatorError, match=MANDATE_DEVIATION_BLOCKER):
        _apply_readiness_checklist(
            GateStore(),
            replace(make_worker(tmp_path), a38=True, review_session=None),
            task,
            {"owner_a38_default": True, "deviation": {"declared": False}},
            head="a" * 40,
        )

    assert [key for key, *_rest in calls] == ["contributing_ok"]


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
                "id": "gate-1", "_origin_device_id": "test-device",
                "task_id": "task-1", "stage": "review-1", "dimension": "quality",
                "verdict": verdict, "head_sha": head,
                "recorded_at": "2026-01-01T00:00:00Z",
            }]

        def row_insert_sequences(self, table):
            return {
                row["id"]: (row["_origin_device_id"], sequence)
                for sequence, row in enumerate(self.rows(table), start=1)
            }

    with pytest.raises(CoordinatorError, match="approved review-1/quality"):
        _apply_readiness_checklist(
            GateStore(),
            replace(make_worker(tmp_path), a38=True, review_session=None),
            _accept_task(),
            {"owner_a38_default": True, "deviation": {"declared": False}},
            head="a" * 40,
        )


def _owner_a38_early_merge_task(store, worker, *, explicit_statement):
    from agent_cli.allow import CHECKLIST_KEYS
    from agent_cli.coordinator_a38 import merged_base_evidence_satisfied
    from agent_cli.local_ci import LocalCiReport, SCHEMA_ID, render_block

    head = "a" * 40
    base = "b" * 40
    task_id = "55555555-5555-4555-8555-555555555555"
    report_body = render_block(
        LocalCiReport(
            schema=SCHEMA_ID,
            repo="Example/owned",
            head=head,
            private=False,
            recorded_at="2026-01-01T00:00:00Z",
            required=(),
            runs=(),
        )
    )
    source = {
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
    }
    if explicit_statement:
        source["hub_assignment_mandate"] = {
            "row_id": "assignment-row",
            "actor": "human-owner",
            "origin": "web:human-owner",
            "issue_key": "Example/owned#7",
            "deviation": {"declared": False, "stated_by": "human-owner"},
        }
    task = {
        "id": task_id,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "pr-review",
        "repo": "Example/owned",
        "ref": "42",
        "models": {"first": "grok", "second": None},
        "change_summary_en": "The early merge retains trusted readiness evidence.",
        "change_summary_de": "Der frühe Merge behält vertrauenswürdige Readiness-Evidenz.",
        "payload": {"coordinator": {
            "phase": "await_merge",
            "head_sha": head,
            "base_sha": base,
            "worktree": str(worker.workspace_root / task_id),
            "pr_number": 42,
            "source": source,
            "a38": {
                "enabled": True,
                # These early-merge cases start after the suite and report were
                # measured on the same head and base that GitHub reports below.
                "base_sha": base,
                "policy_revision": base,
                "report": {
                    "head": head,
                    "base_sha": base,
                    "policy_revision": base,
                    "activity_id": "owner-early-merge-report",
                    "body": report_body,
                },
                "second_review": {
                    "head": head,
                    "status": "posted",
                    "requirement_satisfied_at": "2026-01-01T00:00:00Z",
                    "reviews": [
                        {
                            "blocks_merge": False,
                            "at": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
            },
        }},
    }
    store.write("task", "insert", task_id, task)
    for key in CHECKLIST_KEYS["implement"]:
        status = (
            "pending"
            if key in {"contributing_ok", "deviation_declared", "deviation_granted"}
            else "ja"
        )
        evidence = "seed" if status == "ja" else None
        if key in {"review_2_quality", "review_2_logic"}:
            evidence = (
                f"external second review by outside-reviewer at {head}: "
                "https://github.com/Example/owned/pull/42#issuecomment-2"
            )
        store.write(
            "checklist_item",
            "insert",
            f"{task_id}-{key}",
            {
                "id": f"{task_id}-{key}",
                "task_id": task_id,
                "key": key,
                "status": status,
                "evidence": evidence,
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
                "stage": "review-1",
                "dimension": dimension,
                "vendor": "grok",
                "verdict": "approved",
                "head_sha": head,
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
    store.write(
        "local_check",
        "insert",
        f"{task_id}-a38-local",
        {
            "id": f"{task_id}-a38-local",
            "task_id": task_id,
            "name": "a38",
            "command": "agent a38 run",
            "result": "pass",
            "output": "ok",
            "ran_at": "2026-01-01T00:00:00Z",
            "head_sha": head,
        },
    )
    assert merged_base_evidence_satisfied(
        store,
        store.row("task", task_id),
        head=head,
        base_sha=base,
    )
    return task_id, head


def _patch_early_merge_readiness(monkeypatch, head):
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._repository_visibility",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.scoped", lambda _store, _session, runner: runner
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.gh_json",
        lambda *_args: {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "d" * 40,
            "merged_at": "2026-10-01T12:00:00Z",
            "merged_by": {"login": "human-owner", "type": "User"},
            "base": {"ref": "main", "sha": "b" * 40},
            "head": {"sha": head},
            "html_url": "https://github.com/Example/owned/pull/42",
        },
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.verify_signed_clean_head",
        lambda *_args: head,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._trusted_done_review",
        lambda *_args: ("worker-bot", {}),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._observe_exact_head_ci",
        lambda *_args, **_kwargs: SimpleNamespace(kind="green"),
    )


def test_a38_merge_during_ci_rechecks_all_green_and_uses_real_allow(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    _patch_early_merge_readiness(monkeypatch, head)

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["human merge verified Example/owned#42; task done"]
    saved = store.row("task", task_id)
    assert saved["state"] == "done"
    checklist = {
        row["key"]: row
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    assert checklist["contributing_ok"]["status"] == "ja"
    for key in ("deviation_declared", "deviation_granted"):
        assert checklist[key]["status"] == "n_a"
        assert checklist[key]["evidence"] == (
            "hub mandate assignment-row: human-owner stated deviation declared=false"
        )


@pytest.mark.parametrize(
    "report_private",
    [True, False],
    ids=["private-to-public", "public-to-private"],
)
def test_a38_merged_report_visibility_change_blocks_completion(
    monkeypatch, tmp_path, report_private
):
    from agent_cli.coordinator_github import phase_await_merge
    from agent_cli.local_ci import LocalCiReport, SCHEMA_ID, render_block

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    task = store.row("task", task_id)
    report = task["payload"]["coordinator"]["a38"]["report"]
    report["body"] = render_block(
        LocalCiReport(
            schema=SCHEMA_ID,
            repo="Example/owned",
            head=head,
            private=report_private,
            recorded_at="2026-01-01T00:00:00Z",
            required=(),
            runs=(),
        )
    )
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _patch_early_merge_readiness(monkeypatch, head)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._repository_visibility",
        lambda *_args: not report_private,
    )

    with pytest.raises(
        NonRecoverableCoordinatorError,
        match="merged PR visibility differs from the stored A38 report",
    ):
        phase_await_merge(store, worker, store.row("task", task_id), object())

    assert store.row("task", task_id)["state"] != "done"


def test_a38_merged_visibility_uses_configured_github_runner(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    _patch_early_merge_readiness(monkeypatch, head)
    github_calls: list[list[str]] = []

    def configured_runner(argv):
        github_calls.append(argv)
        return Completed(0, "false", "")

    def raw_runner(_argv):
        raise AssertionError("raw runner must not perform GitHub calls")

    monkeypatch.setattr(
        "agent_cli.coordinator_github.scoped",
        lambda _store, _session, _runner: configured_runner,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._repository_visibility",
        lambda api, repo: api(
            ["gh", "api", f"repos/{repo}", "--jq", ".private"]
        ).stdout == "true",
    )

    assert phase_await_merge(
        store, worker, store.row("task", task_id), raw_runner
    ) == ["human merge verified Example/owned#42; task done"]
    assert github_calls == [
        ["gh", "api", "repos/Example/owned", "--jq", ".private"]
    ]


@pytest.mark.parametrize(
    ("review", "expected", "blocker_kind", "visibility_calls"),
    (
        ("missing", "visibility", None, 1),
        (
            "blocking",
            "blocker merged-with-blocking-second-review",
            "merged-with-blocking-second-review",
            0,
        ),
        (
            "late-passing",
            "blocker merged-without-second-review",
            "merged-without-second-review",
            0,
        ),
    ),
    ids=("missing", "blocking", "late-passing"),
)
def test_a38_merged_second_review_precedes_unavailable_visibility(
    monkeypatch, tmp_path, review, expected, blocker_kind, visibility_calls
):
    from agent_cli import coordinator_github
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
        second_review_timeout_seconds=600,
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    _patch_early_merge_readiness(monkeypatch, head)
    task = store.row("task", task_id)
    a38 = task["payload"]["coordinator"]["a38"]
    if review == "missing":
        a38.pop("second_review")
    elif review == "blocking":
        a38["second_review"]["reviews"] = [{"blocks_merge": True}]
    else:
        a38["second_review"]["requirement_satisfied_at"] = (
            "2026-10-01T12:11:00Z"
        )
        a38["second_review"]["reviews"][-1]["at"] = "2026-10-01T12:11:00Z"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    now = (
        "2026-10-01T12:05:00Z"
        if review == "missing"
        else "2026-10-01T12:12:00Z"
    )
    monkeypatch.setattr(coordinator_github, "utcnow", lambda: now)
    visibility_observations: list[str] = []

    def unavailable_visibility(*_args):
        visibility_observations.append("lookup")
        raise CoordinatorError("visibility unavailable")

    monkeypatch.setattr(
        "agent_cli.coordinator_a38._repository_visibility",
        unavailable_visibility,
    )
    published: list[str] = []
    monkeypatch.setattr(
        coordinator_github,
        "publish_blocker",
        lambda *_args, kind="blocker", **_kwargs: (
            published.append(kind) or [f"blocker {kind}"]
        ),
    )

    lines = phase_await_merge(
        store, worker, store.row("task", task_id), object()
    )

    if expected == "visibility":
        assert lines == [
            "merge observed but repository visibility is temporarily unavailable; "
            "retrying await_merge"
        ]
    else:
        assert lines == [expected]
    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert saved["a38"]["merged_waiting_since"] == "2026-10-01T12:00:00Z"
    assert visibility_observations == ["lookup"] * visibility_calls
    assert published == ([] if blocker_kind is None else [blocker_kind])
    assert saved["phase"] == ("await_merge" if blocker_kind is None else "blocked")


@pytest.mark.parametrize(
    ("kind", "detail", "failures", "action_required"),
    (
        ("failed", "CI failed", ("tests:failure",), ()),
        ("failed", "CI failed", ("tests:cancelled",), ()),
        ("failed", "CI failed", ("tests:skipped",), ()),
        ("failed", "CI failed", ("tests:neutral",), ()),
        (
            "action_required",
            "CI action_required (external authorization)",
            (),
            (".github/workflows/tests.yml",),
        ),
        ("protocol", "PR check rollup malformed", (), ()),
    ),
    ids=(
        "failed",
        "cancelled",
        "uncovered-skipped",
        "uncovered-neutral",
        "action-required",
        "malformed",
    ),
)
def test_a38_merged_task_blocks_non_green_cumulative_ci_before_real_allow(
    monkeypatch,
    tmp_path,
    kind,
    detail,
    failures,
    action_required,
):
    from agent_cli.coordinator_github import _CiObservation, phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    _patch_early_merge_readiness(monkeypatch, head)
    monkeypatch.setattr(
        "agent_cli.coordinator_github._observe_exact_head_ci",
        lambda *_args, **_kwargs: _CiObservation(
            kind,
            detail,
            failures=failures,
            action_required=action_required,
        ),
    )
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_args, **kwargs: published.append((_args[4], kwargs))
        or ["published"],
    )

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["published"]

    saved = store.row("task", task_id)
    checkpoint = saved["payload"]["coordinator"]
    assert saved["state"] != "done"
    assert checkpoint["phase"] == "blocked"
    assert "cumulative GitHub CI is not green on merged head" in checkpoint[
        "blocker"
    ]
    assert "human intervention" in checkpoint["blocker"]
    assert "resume_phase" not in checkpoint
    assert published == [
        (checkpoint["blocker"], {"kind": "merged-ci-non-green"})
    ]


@pytest.mark.parametrize("kind", ("pending", "transport"))
def test_a38_merged_task_retries_unavailable_cumulative_ci_after_deadline_check(
    monkeypatch, tmp_path, kind
):
    from agent_cli.coordinator_github import _CiObservation, phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    _patch_early_merge_readiness(monkeypatch, head)
    detail = (
        "checks pending"
        if kind == "pending"
        else "inventory temporarily unavailable"
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github._observe_exact_head_ci",
        lambda *_args, **_kwargs: _CiObservation(kind, detail),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_github.utcnow",
        lambda: "2026-11-05T10:00:00Z",
    )
    before = store.row("task", task_id)

    first_lines = phase_await_merge(
        store, worker, store.row("task", task_id), object()
    )
    after_first = store.row("task", task_id)
    expected_after_first = copy.deepcopy(before)
    expected_after_first["payload"]["coordinator"]["a38"][
        "merged_waiting_since"
    ] = "2026-10-01T12:00:00Z"
    expected_after_first["updated_at"] = after_first["updated_at"]

    assert "retrying await_merge" in first_lines[0]
    assert after_first == expected_after_first
    assert after_first["payload"]["coordinator"]["phase"] == "await_merge"
    assert after_first["state"] != "done"
    assert store.rows("activity") == []

    second_lines = phase_await_merge(
        store, worker, store.row("task", task_id), object()
    )

    assert "retrying await_merge" in second_lines[0]
    assert store.row("task", task_id) == after_first
    assert store.rows("activity") == []


def test_a38_merged_task_times_out_second_review_while_ci_stays_pending(
    monkeypatch, tmp_path
):
    from agent_cli import coordinator_github
    from agent_cli.coordinator_github import _CiObservation, phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
        second_review_timeout_seconds=600,
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    task = store.row("task", task_id)
    del task["payload"]["coordinator"]["a38"]["second_review"]
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _patch_early_merge_readiness(monkeypatch, head)
    observations: list[str] = []

    def pending_ci(*_args, **_kwargs):
        observations.append("pending")
        return _CiObservation("pending", "checks pending")

    monkeypatch.setattr(
        "agent_cli.coordinator_github._observe_exact_head_ci", pending_ci
    )
    clock = iter(
        (
            "2026-10-01T12:00:00Z",
            "2026-10-01T12:05:00Z",
            "2026-10-01T12:11:00Z",
        )
    )
    monkeypatch.setattr(coordinator_github, "utcnow", lambda: next(clock))
    published: list[str] = []
    monkeypatch.setattr(
        coordinator_github,
        "publish_blocker",
        lambda *_args, kind="blocker", **_kwargs: (
            published.append(kind) or [f"blocker {kind}"]
        ),
    )

    first = phase_await_merge(store, worker, store.row("task", task_id), object())
    second = phase_await_merge(store, worker, store.row("task", task_id), object())
    third = phase_await_merge(store, worker, store.row("task", task_id), object())

    assert "retrying await_merge" in first[0]
    assert "retrying await_merge" in second[0]
    assert third == ["blocker merged-without-second-review"]
    assert observations == ["pending", "pending"]
    assert published == ["merged-without-second-review"]
    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert saved["a38"]["merged_waiting_since"] == "2026-10-01T12:00:00Z"
    assert saved["phase"] == "blocked"


def test_a38_review_satisfied_after_merged_deadline_blocks_completion(
    monkeypatch, tmp_path
):
    from agent_cli import coordinator_github
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
        second_review_timeout_seconds=600,
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    task = store.row("task", task_id)
    a38 = task["payload"]["coordinator"]["a38"]
    a38["merged_waiting_since"] = "2026-11-05T10:00:00Z"
    a38["second_review"]["requirement_satisfied_at"] = "2026-11-05T10:11:00Z"
    a38["second_review"]["reviews"][-1]["at"] = "2026-11-05T10:11:00Z"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _patch_early_merge_readiness(monkeypatch, head)
    monkeypatch.setattr(
        coordinator_github, "utcnow", lambda: "2026-11-05T10:12:00Z"
    )
    published: list[str] = []
    monkeypatch.setattr(
        coordinator_github,
        "publish_blocker",
        lambda *_args, kind="blocker", **_kwargs: (
            published.append(kind) or [f"blocker {kind}"]
        ),
    )

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["blocker merged-without-second-review"]
    saved = store.row("task", task_id)
    assert saved["state"] != "done"
    assert saved["payload"]["coordinator"]["phase"] == "blocked"
    assert published == ["merged-without-second-review"]


def test_a38_review_satisfied_before_merged_deadline_completes_after_green_ci(
    monkeypatch, tmp_path
):
    from agent_cli import coordinator_github
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
        second_review_timeout_seconds=600,
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    task = store.row("task", task_id)
    a38 = task["payload"]["coordinator"]["a38"]
    a38["merged_waiting_since"] = "2026-11-05T10:00:00Z"
    a38["second_review"]["requirement_satisfied_at"] = "2026-11-05T10:09:00Z"
    a38["second_review"]["reviews"][-1]["at"] = "2026-11-05T10:09:00Z"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _patch_early_merge_readiness(monkeypatch, head)
    monkeypatch.setattr(
        coordinator_github, "utcnow", lambda: "2026-11-05T10:12:00Z"
    )

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["human merge verified Example/owned#42; task done"]
    assert store.row("task", task_id)["state"] == "done"


def test_a38_merged_task_blocks_second_review_while_ci_is_pending(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_github import _CiObservation, phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    task = store.row("task", task_id)
    task["payload"]["coordinator"]["a38"]["second_review"]["reviews"] = [
        {"blocks_merge": True}
    ]
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _patch_early_merge_readiness(monkeypatch, head)
    ci_observations: list[str] = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github._observe_exact_head_ci",
        lambda *_args, **_kwargs: (
            ci_observations.append("pending")
            or _CiObservation("pending", "checks pending")
        ),
    )
    published: list[str] = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_args, kind="blocker", **_kwargs: (
            published.append(kind) or [f"blocker {kind}"]
        ),
    )

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["blocker merged-with-blocking-second-review"]
    assert ci_observations == []
    assert published == ["merged-with-blocking-second-review"]
    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert saved["phase"] == "blocked"


def test_a38_merged_pr_base_move_invalidates_evidence_before_task_done(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=True
    )
    _patch_early_merge_readiness(monkeypatch, head)
    moved_base = "c" * 40
    task = store.row("task", task_id)
    checkpoint = task["payload"]["coordinator"]
    checkpoint["evidence"] = {"tests_pass": True, "tests_head": head}
    checkpoint["a38"]["report"] = {
        "head": head,
        "base_sha": "b" * 40,
        "policy_revision": "b" * 40,
    }
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )

    def merged_on_moved_base(*_args):
        return {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "d" * 40,
            "merged_at": "2026-10-01T12:00:00Z",
            "merged_by": {"login": "human-owner", "type": "User"},
            "base": {"ref": "main", "sha": moved_base},
            "head": {"sha": head},
            "html_url": "https://github.com/Example/owned/pull/42",
        }

    monkeypatch.setattr("agent_cli.coordinator_github.gh_json", merged_on_moved_base)

    with pytest.raises(
        NonRecoverableCoordinatorError,
        match=(
            "task-done refused: merged PR base moved after evidence from "
            f"{'b' * 40} to {moved_base}; a merged PR cannot be remeasured"
        ),
    ):
        phase_await_merge(store, worker, store.row("task", task_id), object())

    saved = store.row("task", task_id)
    checkpoint = saved["payload"]["coordinator"]
    assert saved["state"] != "done"
    assert checkpoint["phase"] == "await_merge"
    assert checkpoint["base_sha"] == "b" * 40
    assert checkpoint["base_moved_after_evidence"] == {
        "old_base": "b" * 40,
        "new_base": moved_base,
    }
    assert "report" not in checkpoint["a38"]
    assert "tests_pass" not in checkpoint["evidence"]
    checklist = {
        row["key"]: row
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    for key in (
        "local_check_pass",
        "contributing_ok",
        "deviation_declared",
        "deviation_granted",
    ):
        assert checklist[key]["status"] == "pending"
        assert checklist[key]["evidence"] is None
        assert checklist[key]["source"] == "script"


def test_a38_early_merge_reruns_readiness_when_it_ran_on_an_older_head(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(store, worker, explicit_statement=True)
    task = store.row("task", task_id)
    # Readiness keys closed on an older head (before a blocking review produced this head).
    from agent_cli.coordinator_lanes import set_checklist

    for key, status, source in (
        ("contributing_ok", "ja", "script"),
        ("deviation_declared", "n_a", "human"),
        ("deviation_granted", "n_a", "human"),
    ):
        set_checklist(store, task, key, status, "closed on an older head", source=source)
    task["payload"]["coordinator"].setdefault("evidence", {})["readiness_head"] = "b" * 40
    store.write("task", "update", task_id, {k: v for k, v in task.items() if not k.startswith("_")})
    _patch_early_merge_readiness(monkeypatch, head)

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["human merge verified Example/owned#42; task done"]
    saved = store.row("task", task_id)
    assert saved["payload"]["coordinator"]["evidence"]["readiness_head"] == head


def test_owner_a38_accept_blocks_a_hub_mandate_without_the_no_deviation_statement(
    monkeypatch, tmp_path
):
    _patch_accept_prerequisites(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_github.utcnow", lambda: "2026-01-01T00:02:01Z")
    mandate = verified_mandate()
    monkeypatch.setattr(
        "agent_cli.coordinator_github._hub_assignment_row",
        lambda *_a, **_k: (
            HubAssignmentMandate(
                mandate.row_id, mandate.actor, mandate.origin, mandate.issue_key, None
            ),
            None,
        ),
    )
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_args, **kwargs: published.append((_args[4], kwargs)) or ["published"],
    )
    task = _accept_task()
    task["payload"]["coordinator"]["source"]["owner_scoped"] = True
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
        require_hub_assignment=False,
    )

    assert phase_accept(object(), worker, task, object()) == ["published"]
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "accept"
    assert checkpoint["blocker"] == MANDATE_DEVIATION_BLOCKER
    assert "hub_assignment_mandate" not in checkpoint["source"]


def test_a38_early_merge_without_hub_statement_publishes_readiness_blocker(
    monkeypatch, tmp_path
):
    from agent_cli.coordinator_github import phase_await_merge

    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        a38=True,
        review_session=None,
        second_reviewers=("outside-reviewer",),
    )
    task_id, head = _owner_a38_early_merge_task(
        store, worker, explicit_statement=False
    )
    _patch_early_merge_readiness(monkeypatch, head)
    published = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.publish_blocker",
        lambda *_args, **kwargs: published.append((_args[4], kwargs)) or ["published"],
    )

    assert phase_await_merge(
        store, worker, store.row("task", task_id), object()
    ) == ["published"]
    saved = store.row("task", task_id)
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "await_merge"
    assert checkpoint["blocker"] == MANDATE_DEVIATION_BLOCKER
    checklist = {
        row["key"]: row["status"]
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    assert checklist["contributing_ok"] == "ja"
    assert checklist["deviation_declared"] == "pending"
    assert checklist["deviation_granted"] == "pending"
    assert published == [
        (MANDATE_DEVIATION_BLOCKER, {"kind": "readiness", "reply_checkpoint": True})
    ]


@pytest.mark.parametrize(
    ("a38", "allow_ready", "draft", "accepted"),
    [
        (True, True, False, True),
        (True, True, True, True),
        (False, False, False, False),
        (True, False, False, False),
    ],
)
def test_verify_draft_accepts_a_ready_pr_only_for_an_a38_push(
    monkeypatch, tmp_path, a38, allow_ready, draft, accepted
):
    from agent_cli import coordinator_git

    worker = replace(make_worker(tmp_path), a38=a38)
    cfg = RepositoryConfig("Example/project", "main", "worker-bot/project", (), ())
    pull = {
        "state": "open",
        "draft": draft,
        "user": {"login": "worker-bot"},
        "base": {"ref": "main", "repo": {"full_name": "Example/project"}},
        "head": {"ref": "task-1", "sha": "a" * 40, "repo": {"full_name": "worker-bot/project"}},
    }
    monkeypatch.setattr(coordinator_git, "gh_json", lambda *a, **k: pull)
    monkeypatch.setattr(coordinator_git, "scoped", lambda *a, **k: object())
    monkeypatch.setattr(
        coordinator_git, "account_for", lambda *a, **k: SimpleNamespace(login="worker-bot")
    )

    def verify():
        return coordinator_git._verify_draft(
            object(), worker, object(), "Example/project", 42, cfg, "task-1",
            allow_ready=allow_ready,
        )

    if accepted:
        assert verify() is pull
    else:
        with pytest.raises(CoordinatorError, match="Draft identity, route, head or state changed"):
            verify()


def test_owner_scoped_commands_stay_local_and_out_of_evidence(tmp_path):
    from agent_cli.coordinator_git import repo_cfg
    from agent_cli.coordinator_github import _mandate_provenance

    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_check_argv=("/secret/host/path/check", "--token=sentinel"),
        owner_readiness_argv=("/secret/host/path/ready",),
    )
    source = {
        "repo": "Example/owned",
        "number": 7,
        "assignment_actor": "human-owner",
        "assignment_event_id": 11,
        "repository_config": {
            "repo": "Example/owned", "base": "main", "publication_repo": "worker-bot/owned"
        },
        "body": "issue text",
    }
    task = {"payload": {"coordinator": {"source": source}}}

    cfg = repo_cfg(worker, "Example/owned", task)

    assert cfg.check_argv == ("/secret/host/path/check", "--token=sentinel")
    assert cfg.readiness_argv == ("/secret/host/path/ready",)
    provenance = _mandate_provenance(source)
    assert provenance == {
        "repo": "Example/owned", "number": 7,
        "assignment_actor": "human-owner", "assignment_event_id": 11,
    }
    assert "secret" not in str(provenance) and "sentinel" not in str(provenance)
