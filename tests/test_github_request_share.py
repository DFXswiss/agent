"""Device-wide sharing of assignee discovery and GitHub login checks."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_cli.coordinator import (
    LOGIN_REUSE_SECONDS,
    login_cached,
    shared_github_runner,
    tick,
)
from agent_cli.coordinator_config import RepositoryConfig, WorkerConfig
from agent_cli.github_request_share import (
    LOGIN_SHARE_FILE,
    SHARE_FILE,
    shared_assigned_issues,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store
from agent_cli.watch import scan_merged
from driver_fixtures import write_drivers
from github_support import configure_accounts
from test_coordinator_support import make_session, make_worker

_LOGIN_TAIL = ["gh", "api", "user", "--jq", ".login"]
_START = 1_700_000_000
_HOUR_STEPS = 3600 // 30
_REPOS = ("example/one", "example/two", "example/three")


def _repositories() -> dict[str, RepositoryConfig]:
    return {
        name: RepositoryConfig(
            repo=name,
            base="develop",
            publication_repo=name,
            check_argv=("/operator/checks", "--full"),
            readiness_argv=("/operator/readiness",),
        )
        for name in _REPOS
    }


def _write_device_accounts(home: Path, session_ids: list[str]) -> None:
    write_drivers(home)
    (home / "github-accounts.json").write_text(
        json.dumps(
            {
                "accounts": {
                    "worker": {
                        "login": "worker-bot",
                        "gh_config_dir": "/test/gh-worker",
                        "git": {
                            "name": "Worker Bot",
                            "email": "worker@example.com",
                            "signing_format": "ssh",
                            "signing_key": "/test/worker.key",
                        },
                    }
                },
                "sessions": {session_id: "worker" for session_id in session_ids},
            }
        ),
        encoding="utf-8",
    )
    (home / "ai-accounts.json").write_text(
        json.dumps(
            {
                "lane_execution": {"mode": "this-device-is-lane-host"},
                "models": {"first": "driver-a", "second": None},
                "accounts": {
                    "driver-a-w": {"provider": "driver-a", "config_dir": "/test/driver-a"},
                },
                "roles": {
                    "impl": {
                        "account": "driver-a-w",
                        "model": "driver-a-model",
                        "access": "workspace-write",
                    },
                    "rev": {
                        "account": "driver-a-w",
                        "model": "driver-a-model",
                        "access": "read-only",
                    },
                },
                "lanes": {
                    "driver-a:implementer": "impl",
                    "driver-a:reviewer": "rev",
                    "driver-a:pr-reviewer-quality": "rev",
                    "driver-a:pr-reviewer-logic": "rev",
                    "driver-a:check": "rev",
                },
                "sessions": {
                    session_id: {"interactive": "impl"} for session_id in session_ids
                },
            }
        ),
        encoding="utf-8",
    )


class _CountingGh:
    def __init__(self) -> None:
        self.logins = 0
        self.discoveries = 0
        self.conditional = 0

    def __call__(self, argv: list[str]) -> Completed:
        if argv[-5:] == _LOGIN_TAIL:
            self.logins += 1
            return Completed(0, "worker-bot", "")
        url = argv[-1]
        if (
            not isinstance(url, str)
            or not url.startswith("repos/")
            or "/issues?" not in url
        ):
            raise AssertionError(argv)
        repo = url[len("repos/") : url.index("/issues?")]
        self.discoveries += 1
        etag = f'W/"assigned-{repo}"'
        header = f"If-None-Match: {etag}"
        if header in argv:
            self.conditional += 1
            return Completed(
                0,
                f"HTTP/2.0 304 Not Modified\r\nETag: {etag}\r\n\r\n",
                "",
            )
        if any(item.startswith("If-None-Match:") for item in argv):
            raise AssertionError(argv)
        return Completed(0, f"HTTP/2.0 200 OK\r\nETag: {etag}\r\n\r\n[]", "")


def _idle_hour(
    store: Store,
    workers: list[WorkerConfig],
    fake: _CountingGh,
    clock: dict[str, float],
) -> tuple[int, int, int]:
    fake.logins = 0
    fake.discoveries = 0
    fake.conditional = 0
    for step in range(_HOUR_STEPS):
        clock["now"] = float(_START + step * 30)
        for worker in workers:
            lines = tick(store, worker, runner=fake)
            assert lines == ["idle"], lines
    return fake.discoveries, fake.logins, fake.conditional


def test_idle_workers_share_discovery_and_login_for_one_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_ids = [f"order-{index}" for index in range(32)]
    store = Store(tmp_path)
    _write_device_accounts(store.home, session_ids)
    for session_id in session_ids:
        make_session(store, session_id, ["spine", "review-loop", "pr-review"])
    base = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        orders=True,
        poll_seconds=30,
        repositories=_repositories(),
    )
    workers = [replace(base, session_id=session_id) for session_id in session_ids]
    clock = {"now": float(_START)}
    monkeypatch.setattr("agent_cli.github_request_share.wall_time", lambda: clock["now"])
    fake = _CountingGh()
    try:
        first = _idle_hour(store, workers[:16], fake, clock)
        for name in (SHARE_FILE, LOGIN_SHARE_FILE):
            record = store.home / name
            if record.is_file():
                record.unlink()
        doubled = _idle_hour(store, workers, fake, clock)
    finally:
        store.close()
    assert first == (360, first[1], 357)
    assert doubled == (360, first[1], 357)
    assert 1 <= first[1] <= 8


@pytest.mark.no_pg
def test_conditional_discovery_reuses_the_body_on_304(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": float(_START)}
    monkeypatch.setattr("agent_cli.github_request_share.wall_time", lambda: clock["now"])
    calls: list[list[str]] = []
    issue = {"number": 7, "title": "Fix"}

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if any(item.startswith("If-None-Match:") for item in argv):
            return Completed(0, 'HTTP/2.0 304 Not Modified\r\nETag: W/"iss"\r\n\r\n', "")
        body = json.dumps([issue])
        return Completed(0, 'HTTP/2.0 200 OK\r\nETag: W/"iss"\r\n\r\n' + body, "")

    kwargs = {
        "repo": "example/project",
        "login": "worker-bot",
        "poll_seconds": 30,
    }
    first = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert first == [issue]
    first.append({"number": 9})
    again = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert again == [issue]
    assert len(calls) == 1
    assert calls[0][:4] == ["gh", "api", "--include", "--paginate"]
    assert "--slurp" in calls[0]
    assert calls[0][-1] == (
        "repos/example/project/issues?assignee=worker-bot&state=open&per_page=100"
    )
    clock["now"] += 30
    second = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert second == [issue]
    assert len(calls) == 2
    assert 'If-None-Match: W/"iss"' in calls[1]


def _paginated_include(
    pages: list[list[dict[str, object]]],
    etags: list[str],
    statuses: list[int],
) -> str:
    if len(pages) != len(etags) or len(etags) != len(statuses):
        raise AssertionError((len(pages), len(etags), len(statuses)))
    parts: list[str] = []
    for etag, code in zip(etags, statuses):
        reason = "Not Modified" if code == 304 else "OK"
        parts.append(f'HTTP/2.0 {code} {reason}\r\nETag: {etag}\r\n\r\n')
    if any(code != 304 for code in statuses):
        parts.append(json.dumps(pages))
    return "".join(parts)


def _share_entry(home: Path) -> dict[str, object]:
    document = json.loads((home / SHARE_FILE).read_text(encoding="utf-8"))
    assert len(document) == 1
    entry = next(iter(document.values()))
    assert isinstance(entry, dict)
    return entry


@pytest.mark.no_pg
def test_two_pages_do_not_reuse_the_first_page_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": float(_START)}
    monkeypatch.setattr("agent_cli.github_request_share.wall_time", lambda: clock["now"])
    page_one = [
        {"number": 40, "title": "newer"},
        {"number": 39, "title": "newer"},
    ]
    page_two = [{"number": 3, "title": "older"}]
    added = {"number": 2, "title": "assigned later"}
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        conditional = any(item.startswith("If-None-Match:") for item in argv)
        if len(calls) == 1:
            assert conditional is False
            return Completed(
                0,
                _paginated_include(
                    [page_one, page_two],
                    ['W/"page-1"', 'W/"page-2"'],
                    [200, 200],
                ),
                "",
            )
        pages = [page_one, [*page_two, added]]
        etags = ['W/"page-1"', 'W/"page-2b"']
        if conditional:
            return Completed(0, _paginated_include(pages, etags, [304, 200]), "")
        return Completed(0, _paginated_include(pages, etags, [200, 200]), "")

    kwargs = {
        "repo": "example/project",
        "login": "worker-bot",
        "poll_seconds": 30,
    }
    first = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert first == [*page_one, *page_two]
    document = json.loads((tmp_path / SHARE_FILE).read_text(encoding="utf-8"))
    assert len(document) == 1
    entry = next(iter(document.values()))
    assert isinstance(entry, dict)
    assert "etag" not in entry
    assert "single_page" not in entry
    # A record written before this fix stored the first page's ETag on the
    # whole multi-page list. That validator must not be sent.
    entry["etag"] = 'W/"page-1"'
    entry["at"] = clock["now"] - 30
    (tmp_path / SHARE_FILE).write_text(json.dumps(document), encoding="utf-8")
    clock["now"] += 30
    second = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert second == [*page_one, *page_two, added]
    assert len(calls) == 2
    assert not any(item.startswith("If-None-Match:") for item in calls[1])


@pytest.mark.no_pg
def test_full_page_does_not_hide_a_new_second_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": float(_START)}
    monkeypatch.setattr("agent_cli.github_request_share.wall_time", lambda: clock["now"])
    page = [{"number": number} for number in range(100, 0, -1)]
    older = {"number": 0, "title": "older"}
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        conditional = any(item.startswith("If-None-Match:") for item in argv)
        if len(calls) == 1:
            assert conditional is False
            return Completed(
                0,
                'HTTP/2.0 200 OK\r\nETag: W/"full"\r\n\r\n' + json.dumps(page),
                "",
            )
        if conditional:
            return Completed(0, 'HTTP/2.0 304 Not Modified\r\nETag: W/"full"\r\n\r\n', "")
        return Completed(
            0,
            _paginated_include([page, [older]], ['W/"full"', 'W/"older"'], [200, 200]),
            "",
        )

    kwargs = {
        "repo": "example/project",
        "login": "worker-bot",
        "poll_seconds": 30,
    }
    first = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert first == page
    assert "etag" not in _share_entry(tmp_path)
    clock["now"] += 30
    second = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert second == [*page, older]
    assert len(calls) == 2
    assert not any(item.startswith("If-None-Match:") for item in calls[1])


@pytest.mark.no_pg
def test_next_link_on_one_short_page_is_not_conditional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": float(_START)}
    monkeypatch.setattr("agent_cli.github_request_share.wall_time", lambda: clock["now"])
    first_page = [{"number": 8, "title": "only page received"}]
    second_page = [{"number": 1, "title": "followed later"}]
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        conditional = any(item.startswith("If-None-Match:") for item in argv)
        if len(calls) == 1:
            assert conditional is False
            return Completed(
                0,
                'HTTP/2.0 200 OK\r\nETag: W/"partial"\r\n'
                'Link: <https://api.github.com/repositories/1/issues?page=2>; rel="next"\r\n'
                "\r\n"
                + json.dumps(first_page),
                "",
            )
        if conditional:
            return Completed(
                0,
                'HTTP/2.0 304 Not Modified\r\nETag: W/"partial"\r\n\r\n',
                "",
            )
        return Completed(
            0,
            _paginated_include(
                [first_page, second_page],
                ['W/"partial"', 'W/"rest"'],
                [200, 200],
            ),
            "",
        )

    kwargs = {
        "repo": "example/project",
        "login": "worker-bot",
        "poll_seconds": 30,
    }
    first = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert first == first_page
    assert "etag" not in _share_entry(tmp_path)
    clock["now"] += 30
    second = shared_assigned_issues(tmp_path, runner, **kwargs)
    assert second == [*first_page, *second_page]
    assert not any(item.startswith("If-None-Match:") for item in calls[1])


@pytest.mark.no_pg
def test_authentication_error_verifies_login_again(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    failure = {"text": "HTTP 403: rate limit exceeded"}

    def base(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[-5:] == _LOGIN_TAIL:
            return Completed(0, "worker-bot", "")
        return Completed(1, "", failure["text"])

    one = ["env", "GH_CONFIG_DIR=/accounts/one", *_LOGIN_TAIL]
    two = ["env", "GH_CONFIG_DIR=/accounts/two", *_LOGIN_TAIL]
    cached = login_cached(base, max_age_seconds=LOGIN_REUSE_SECONDS, cache_dir=tmp_path)
    first = cached(one)
    assert cached(one) is first
    second = cached(two)
    assert cached(two) is second
    assert len(calls) == 2
    assert cached(["env", "GH_CONFIG_DIR=/accounts/two", "gh", "api", "rate"]).returncode == 1
    assert cached(two) is second
    assert len(calls) == 3
    failure["text"] = "HTTP 401: Bad credentials"
    assert cached(
        ["env", "GH_CONFIG_DIR=/accounts/one", "gh", "api", "repos/example/project"]
    ).returncode == 1
    assert len(calls) == 4
    refreshed = cached(one)
    assert refreshed is not first
    assert cached(two) is second
    assert len(calls) == 5
    fresh = login_cached(base, max_age_seconds=LOGIN_REUSE_SECONDS, cache_dir=tmp_path)
    assert fresh(one).stdout == "worker-bot"
    assert len(calls) == 5
    assert fresh(two) is not second
    assert fresh(two).stdout == "worker-bot"
    assert len(calls) == 5


def test_knock_reuses_one_login_across_scans(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {"id": "s1", "kind": "human", "status": "active"},
    )
    configure_accounts(store.home, ["s1"])
    for number, activity_id in ((8, "open-1"), (9, "open-2")):
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "s1",
                "type": "pr.open",
                "payload": {"repo": "dfxswiss/agent", "number": number},
                "result": {
                    "repo": "dfxswiss/agent",
                    "number": number,
                    "url": f"https://github.com/dfxswiss/agent/pull/{number}",
                },
                "execution_status": "done",
            },
        )
    logins: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        if argv[-5:] == _LOGIN_TAIL:
            logins.append(list(argv))
            return Completed(0, "alice", "")
        number = int(argv[argv.index("view") + 1])
        body = {
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
            "url": f"https://github.com/dfxswiss/agent/pull/{number}",
            "number": number,
        }
        return Completed(0, json.dumps(body), "")

    try:
        github = shared_github_runner(runner, home=store.home)
        first = scan_merged(store, github)
        second = scan_merged(store, github)
        third = scan_merged(store, shared_github_runner(runner, home=store.home))
    finally:
        store.close()
    assert first == ([], 0)
    assert second == ([], 0)
    assert third == ([], 0)
    assert len(logins) == 1
