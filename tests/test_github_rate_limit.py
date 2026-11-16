"""GitHub rate-limit waits stay distinct from authentication failures.

The coordinator tests use the real Account.runner. Fakes supply gh results only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_cli.a38_errors import RateLimitError
from agent_cli.coordinator import tick
from agent_cli.coordinator_runtime import GITHUB_RATE_LIMIT_PREFIX
from agent_cli.github_accounts import Account, AccountError
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator import patch_execute_github, seed_task
from test_coordinator_support import FakeGh, make_session, make_worker, write_accounts

EPOCH = 1_700_000_000
_PRIMARY = (
    "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n"
    "Authorization: Bearer ghp_secretvalue\n"
)
_AUTH = (
    "gh: Bad credentials (HTTP 401)\n"
    "x-ratelimit-remaining: 0\n"
    "x-ratelimit-reset: 1700003600\n"
)
_TID = "77777777-7777-7777-7777-777777777777"


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait(account: str, epoch: int) -> str:
    return f"GitHub rate limit exhausted for account {account}; resets at {_stamp(epoch)}"


def _unavailable(account: str) -> str:
    return f"GitHub rate limit exhausted for account {account}; reset time unavailable"


def _gh(argv: list[str]) -> list[str]:
    return argv[argv.index("gh") :]


def _account(**extra: Any) -> Account:
    return Account(name="worker", login="worker-bot", gh_config_dir="/test/gh-worker", **extra)


def _secret_absent(text: str) -> None:
    assert "ghp_secretvalue" not in text
    assert "Authorization" not in text


@pytest.mark.no_pg
def test_http_401_with_remaining_zero_is_authentication_failure() -> None:
    calls: list[list[str]] = []

    def base(argv: list[str]) -> Completed:
        calls.append(_gh(argv))
        return Completed(1, "", _AUTH)

    with pytest.raises(AccountError, match=r"Authentication failed for account worker; no fallback$"):
        _account().runner(base)
    assert calls == [["gh", "api", "user", "--jq", ".login"]]


@pytest.mark.no_pg
def test_non_rate_limit_403_stays_authentication_failure() -> None:
    def base(argv: list[str]) -> Completed:
        return Completed(1, "", "gh: Resource not accessible by integration (HTTP 403)\n")

    with pytest.raises(AccountError, match="Authentication failed for account worker; no fallback"):
        _account().runner(base)


@pytest.mark.no_pg
def test_primary_headers_name_the_reset_and_drop_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: EPOCH)
    calls: list[list[str]] = []

    def base(argv: list[str]) -> Completed:
        cmd = _gh(argv)
        calls.append(cmd)
        assert cmd[:3] != ["gh", "api", "rate_limit"]
        return Completed(
            1,
            "",
            "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n"
            "x-ratelimit-remaining: 0\n"
            "x-ratelimit-reset: 1700003600\n"
            "Authorization: Bearer ghp_secretvalue\n",
        )

    with pytest.raises(RateLimitError) as caught:
        _account().runner(base)
    message = str(caught.value)
    assert message == _wait("worker", 1_700_003_600)
    assert caught.value.account == "worker"
    assert caught.value.retry_at == datetime.fromtimestamp(1_700_003_600, tz=timezone.utc)
    _secret_absent(message)
    assert calls == [["gh", "api", "user", "--jq", ".login"]]


@pytest.mark.no_pg
@pytest.mark.parametrize("status", [403, 429])
def test_secondary_retry_after_does_not_probe(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: EPOCH)
    calls: list[list[str]] = []

    def base(argv: list[str]) -> Completed:
        calls.append(_gh(argv))
        return Completed(
            1,
            "",
            f"gh: You have exceeded a secondary rate limit. (HTTP {status})\nretry-after: 120\n",
        )

    with pytest.raises(RateLimitError) as caught:
        _account().runner(base)
    assert str(caught.value) == _wait("worker", EPOCH + 120)
    assert calls == [["gh", "api", "user", "--jq", ".login"]]


@pytest.mark.no_pg
def test_missing_reset_does_not_invent_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: EPOCH)
    calls: list[list[str]] = []

    def base(argv: list[str]) -> Completed:
        cmd = _gh(argv)
        calls.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            return Completed(1, "", _PRIMARY)
        assert cmd[:3] == ["gh", "api", "rate_limit"]
        return Completed(
            0,
            json.dumps(
                {
                    "resources": {"search": {"remaining": False, "reset": 1}},
                    "rate": {"remaining": 5, "reset": EPOCH + 3600},
                }
            ),
            "",
        )

    with pytest.raises(RateLimitError) as caught:
        _account().runner(base)
    message = str(caught.value)
    assert message == _unavailable("worker")
    assert caught.value.retry_at is None
    assert caught.value.account == "worker"
    _secret_absent(message)
    assert [cmd[:3] for cmd in calls] == [["gh", "api", "user"], ["gh", "api", "rate_limit"]]


@pytest.mark.no_pg
def test_probe_oserror_is_not_an_authentication_failure() -> None:
    def base(argv: list[str]) -> Completed:
        if _gh(argv)[:3] == ["gh", "api", "user"]:
            return Completed(1, "", _PRIMARY)
        raise OSError("rate_limit probe failed")

    with pytest.raises(RateLimitError, match="reset time unavailable") as caught:
        _account().runner(base)
    assert "Cannot authenticate" not in str(caught.value)
    _secret_absent(str(caught.value))


@pytest.mark.no_pg
def test_rate_limit_endpoint_is_not_probed_again() -> None:
    calls: list[list[str]] = []

    def base(argv: list[str]) -> Completed:
        cmd = _gh(argv)
        calls.append(cmd[:3])
        if cmd[:3] == ["gh", "api", "user"]:
            return Completed(0, "worker-bot\n", "")
        return Completed(1, "", _PRIMARY)

    scoped = _account().runner(base)
    with pytest.raises(RateLimitError, match="reset time unavailable"):
        scoped(["gh", "api", "rate_limit"])
    assert calls.count(["gh", "api", "rate_limit"]) == 1


@pytest.mark.no_pg
def test_later_git_command_uses_the_same_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: EPOCH)

    def base(argv: list[str]) -> Completed:
        if "gh" in argv and _gh(argv)[:3] == ["gh", "api", "user"]:
            return Completed(0, "worker-bot\n", "")
        return Completed(
            1,
            "",
            "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n"
            "x-ratelimit-remaining: 0\n"
            "x-ratelimit-reset: 1700003600\n",
        )

    scoped = _account(
        git_identity={
            "name": "Worker Bot",
            "email": "worker@example.com",
            "signing_format": "ssh",
            "signing_key": "/test/worker.key",
        }
    ).runner(base)
    with pytest.raises(RateLimitError) as caught:
        scoped(["git", "status"])
    assert str(caught.value) == _wait("worker", 1_700_003_600)


def _blocked_events(store: Store) -> list[dict[str, Any]]:
    found = []
    for row in store.rows("activity"):
        payload = row.get("payload")
        if (
            row.get("type") == "order.event"
            and isinstance(payload, dict)
            and payload.get("kind") == "blocked"
        ):
            found.append(row)
    return found


def _stored_text(store: Store, tid: str, lines: list[str]) -> str:
    task = store.row("task", tid)
    return json.dumps({"task": task, "activity": store.rows("activity"), "lines": lines})


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: dict[str, Any],
    clock: dict[str, int],
) -> tuple[Store, Any, list[list[str]], Any]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        cmd = _gh(argv) if "gh" in argv else []
        limited = clock["epoch"] < mode["reset"] and mode["kind"] in {
            "primary",
            "secondary",
            "unavailable",
            "step",
        }
        if cmd[:5] == ["gh", "api", "user", "--jq", ".login"]:
            if mode["kind"] == "auth":
                return Completed(1, "", _AUTH)
            if limited and mode["kind"] != "step":
                if mode["kind"] == "secondary":
                    return Completed(
                        1,
                        "",
                        "gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
                        "retry-after: 120\n",
                    )
                return Completed(1, "", _PRIMARY)
            config = next(part.split("=", 1)[1] for part in argv if part.startswith("GH_CONFIG_DIR="))
            login = "review-bot" if config.endswith("gh-review") else "worker-bot"
            return Completed(0, login, "")
        if cmd[:3] == ["gh", "api", "rate_limit"]:
            if mode["kind"] == "unavailable":
                body = {"rate": {"remaining": 5, "reset": mode["reset"]}}
            else:
                body = {
                    "resources": {
                        "search": {"remaining": False, "reset": 1},
                        "core": {"remaining": 0, "reset": mode["reset"], "limit": 5000},
                    },
                    "rate": {"remaining": 0, "reset": mode["reset"]},
                }
            return Completed(0, json.dumps(body), "")
        if mode["kind"] == "step" and clock["epoch"] < mode["reset"]:
            return Completed(1, "", _PRIMARY)
        if mode["kind"] == "step-auth":
            return Completed(1, "", "gh: Bad credentials (HTTP 401)\n")
        return fake(argv)

    monkeypatch.setattr("agent_cli.coordinator_runtime.utcnow", lambda: _stamp(clock["epoch"]))
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: clock["epoch"])
    patch_execute_github(monkeypatch)
    seed_task(
        store,
        worker,
        _TID,
        {
            "id": _TID,
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "Fix widget",
            "repo": "example/project",
            "ref": None,
            "payload": {
                "coordinator": {
                    "phase": "accept",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a-seed",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix widget",
                    },
                    "worker_login": "worker-bot",
                }
            },
            "state": "open",
            "current_round": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    return store, worker, calls, runner


def _assert_still_waiting(store: Store, lines: list[str], message: str) -> None:
    task = store.row("task", _TID)
    coord = task["payload"]["coordinator"]
    assert lines == [message]
    assert coord["phase"] == "accept"
    assert task.get("current_round") == 0
    assert "blocker" not in coord
    assert _blocked_events(store) == []
    assert "Authentication failed" not in message
    assert "blocked:" not in "\n".join(lines)
    _secret_absent(_stored_text(store, _TID, lines))


def test_rate_limited_login_waits_and_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset = EPOCH + 3600
    mode = {"kind": "primary", "reset": reset}
    clock = {"epoch": EPOCH}
    store, worker, calls, runner = _install(tmp_path, monkeypatch, mode, clock)
    message = _wait("worker", reset)

    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "worker") == _stamp(reset)
    assert [_gh(argv)[:3] for argv in calls] == [
        ["gh", "api", "user"],
        ["gh", "api", "rate_limit"],
    ]

    clock["epoch"] = reset - 1
    before = len(calls)
    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert len(calls) == before

    clock["epoch"] = reset
    lines = tick(store, worker, runner=runner)
    task = store.row("task", _TID)
    assert task["payload"]["coordinator"]["phase"] == "checkout", lines
    assert task.get("current_round") == 0
    assert _blocked_events(store) == []
    assert "blocked:" not in "\n".join(lines)
    _secret_absent(_stored_text(store, _TID, lines))


def test_real_401_keeps_the_authentication_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mode = {"kind": "auth", "reset": EPOCH + 3600}
    clock = {"epoch": EPOCH}
    store, worker, _calls, runner = _install(tmp_path, monkeypatch, mode, clock)
    lines = tick(store, worker, runner=runner)
    task = store.row("task", _TID)
    assert lines == ["preflight blocked: Authentication failed for account worker; no fallback"]
    assert task["payload"]["coordinator"]["phase"] == "accept"
    assert "blocker" not in task["payload"]["coordinator"]
    assert task.get("current_round") == 0
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "worker") is None
    assert _blocked_events(store) == []


def test_real_401_mid_step_keeps_the_existing_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mode = {"kind": "step-auth", "reset": EPOCH + 3600}
    clock = {"epoch": EPOCH}
    store, worker, _calls, runner = _install(tmp_path, monkeypatch, mode, clock)
    lines = tick(store, worker, runner=runner)
    task = store.row("task", _TID)
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "accept"
    blocker = str(coord.get("blocker") or "")
    assert "Bad credentials" in blocker
    assert "rate limit" not in blocker.lower()
    assert task.get("current_round") == 0
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "worker") is None
    assert any("blocked:" in line for line in lines)


def test_secondary_limit_waits_for_retry_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset = EPOCH + 120
    mode = {"kind": "secondary", "reset": reset}
    clock = {"epoch": EPOCH}
    store, worker, calls, runner = _install(tmp_path, monkeypatch, mode, clock)
    message = _wait("worker", reset)

    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert all(_gh(argv)[:3] != ["gh", "api", "rate_limit"] for argv in calls if "gh" in argv)

    clock["epoch"] = reset - 1
    before = len(calls)
    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert len(calls) == before

    clock["epoch"] = reset
    lines = tick(store, worker, runner=runner)
    task = store.row("task", _TID)
    assert task["payload"]["coordinator"]["phase"] == "checkout", lines
    assert task.get("current_round") == 0
    assert _blocked_events(store) == []
    assert "blocked:" not in "\n".join(lines)


def test_mid_step_rate_limit_resumes_after_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset = EPOCH + 3600
    mode = {"kind": "step", "reset": reset}
    clock = {"epoch": EPOCH}
    store, worker, calls, runner = _install(tmp_path, monkeypatch, mode, clock)
    message = _wait("worker", reset)

    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    cmds = [_gh(argv) for argv in calls if "gh" in argv]
    assert cmds[-2][-1] == "repos/example/project/issues/7"
    assert cmds[-1][:3] == ["gh", "api", "rate_limit"]
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "worker") == _stamp(reset)

    clock["epoch"] = reset - 1
    before = len(calls)
    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert len(calls) == before

    clock["epoch"] = reset
    lines = tick(store, worker, runner=runner)
    task = store.row("task", _TID)
    assert task["payload"]["coordinator"]["phase"] == "checkout", lines
    assert task.get("current_round") == 0
    assert _blocked_events(store) == []
    assert "blocked:" not in "\n".join(lines)
    _secret_absent(_stored_text(store, _TID, lines))


def test_unavailable_reset_does_not_block_or_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mode = {"kind": "unavailable", "reset": EPOCH + 3600}
    clock = {"epoch": EPOCH}
    store, worker, calls, runner = _install(tmp_path, monkeypatch, mode, clock)
    message = _unavailable("worker")

    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "worker") is None
    before = len(calls)

    lines = tick(store, worker, runner=runner)
    _assert_still_waiting(store, lines, message)
    assert len(calls) > before
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "worker") is None
