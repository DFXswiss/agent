from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.errors import (
    config_path,
    cursor_path,
    default_fetch,
    error_class,
    fingerprint,
    load_config,
    redact,
    scan_errors,
    stack_sig,
)
from agent_cli.store import Store, StoreError


def _runner_session(store: Store, sid: str = "runner-1") -> None:
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "status": "active",
            "skills": ["spine", "review-loop", "pr-review", "error-fix"],
        },
    )


def _write_config(home: Path, session_id: str = "runner-1") -> None:
    config_path(home).write_text(
        json.dumps({"session_id": session_id, "service": "api", "environment": "prod", "repo": "org/app"}),
        encoding="utf-8",
    )


def test_load_config_missing(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="error-fix.json not found"):
        load_config(config_path(tmp_path))


def test_load_config_missing_session_id(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(StoreError, match="session_id is missing"):
        load_config(path)


def test_load_config_rejects_credentials(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    path.write_text(json.dumps({"session_id": "s", "password": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "headers": {"token": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "headers": {"Token": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?api_key=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?access_token=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q#password=x"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(json.dumps({"session_id": "s", "api_key": "x"}), encoding="utf-8")
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://u:p@logs.example/q"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)


def test_redact_and_fingerprint() -> None:
    raw = "TimeoutError bearer SECRETTOKENVALUE0123456789 user@example.com"
    cleaned = redact(raw)
    assert "SECRETTOKENVALUE0123456789" not in cleaned
    assert "user@example.com" not in cleaned
    assert "[redacted]" in cleaned
    quoted = redact('{"password":"hunter2"} Authorization: Basic abcdef Cookie: sid=1 https://u:p@host/x')
    assert "hunter2" not in quoted
    assert "abcdef" not in quoted
    assert "sid=1" not in quoted
    assert "u:p@" not in quoted
    cookie = redact("Cookie: sid=1; extra=remain")
    assert "sid=1" not in cookie
    assert "extra=remain" not in cookie
    jwt = redact("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc_def-ghi")
    assert "eyJhbGciOiJIUzI1NiJ9" not in jwt
    akia = redact("AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in akia
    assert error_class(cleaned) == "TimeoutError"
    sig = stack_sig(cleaned)
    assert len(sig) == 16
    fp = fingerprint(service="api", error_class="TimeoutError", stack_sig=sig, environment="prod")
    assert fp.startswith("api|TimeoutError|")
    assert fp.endswith("|prod")


def test_scan_inserts_once_then_enriches(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError bearer SECRETTOKENVALUE0123456789 boom"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], "2026-08-23T16:00:00Z")

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["type"] == "error.seen"
    assert row["session_id"] == "runner-1"
    payload = row["payload"]
    assert payload["service"] == "api"
    assert payload["environment"] == "prod"
    assert payload["class"] == "TimeoutError"
    assert payload["count"] == 1
    assert payload["repo"] == "org/app"
    assert payload["evidence"] is None
    assert "SECRETTOKENVALUE0123456789" not in payload["excerpt"]
    assert "fingerprint" in payload
    wakes = store.pending_wakes()
    assert any(w["activity_id"] == created[0] for w in wakes)

    created2, enriched2 = scan_errors(store, fetch)
    assert created2 == []
    assert enriched2 == created
    again = store.row("activity", created[0])
    assert again is not None
    assert again["payload"]["count"] == 2
    assert len([w for w in store.pending_wakes() if w["activity_id"] == created[0]]) == 1


def test_scan_requires_error_fix_skill(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write("session", "insert", "runner-1", {"id": "runner-1", "kind": "runner", "status": "active", "skills": ["spine"]})
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": "boom"}], None)

    with pytest.raises(StoreError, match="does not have skill error-fix"):
        scan_errors(store, fetch)


def test_skip_opens_new_seen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError once"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    eid = created[0]
    fp = store.row("activity", eid)["payload"]["fingerprint"]
    store.write(
        "activity",
        "insert",
        "skip-1",
        {
            "id": "skip-1",
            "session_id": "runner-1",
            "type": "error.skip",
            "payload": {"error_id": eid, "fingerprint": fp, "reason": "noisy"},
            "execution_status": "done",
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != eid


def test_open_seen_is_preferred_over_newer_closed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick = 0

    def now() -> str:
        nonlocal tick
        tick += 1
        return f"2026-08-23T16:00:{tick:02d}Z"

    monkeypatch.setattr("agent_cli.store.utcnow", now)
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError once"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    a_id = created[0]
    fp = store.row("activity", a_id)["payload"]["fingerprint"]
    store.write(
        "activity",
        "insert",
        "skip-1",
        {
            "id": "skip-1",
            "session_id": "runner-1",
            "type": "error.skip",
            "payload": {"error_id": a_id, "fingerprint": fp, "reason": "noisy"},
            "execution_status": "done",
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    b_id = created2[0]
    stored = store.row("activity", a_id)
    assert stored is not None
    payload = {key: value for key, value in stored.items() if not key.startswith("_")}
    store.write("activity", "update", a_id, payload)
    assert store.rows("activity")[0]["id"] == a_id

    created3, enriched3 = scan_errors(store, fetch)
    assert created3 == []
    assert enriched3 == [b_id]
    b_row = store.row("activity", b_id)
    assert b_row is not None
    assert b_row["payload"]["count"] == 2
    seen_ids = [
        row["id"] for row in store.rows("activity") if row["type"] == "error.seen"
    ]
    assert set(seen_ids) == {a_id, b_id}


def test_done_task_opens_new_seen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError twice"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    eid = created[0]
    store.write(
        "task",
        "insert",
        "task-1",
        {
            "id": "task-1",
            "session_id": "runner-1",
            "workflow": "implement",
            "state": "done",
            "payload": {"error_id": eid, "repo": "org/app"},
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != eid


def test_failed_task_opens_new_seen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError failed"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": line}], None)

    created, _ = scan_errors(store, fetch)
    eid = created[0]
    store.write(
        "task",
        "insert",
        "task-fail",
        {
            "id": "task-fail",
            "session_id": "runner-1",
            "workflow": "implement",
            "state": "failed",
            "payload": {"error_id": eid, "repo": "org/app"},
        },
    )
    created2, enriched2 = scan_errors(store, fetch)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != eid


def test_default_fetch_uses_forward_cursor_and_netrc(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeAuth:
        pass

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {"service": "api"},
                            "values": [
                                ["2000000000000000002", "TimeoutError late"],
                                ["2000000000000000001", "TimeoutError early"],
                            ],
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["trust_env"] = kwargs.get("trust_env")

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            captured["url"] = url
            captured["params"] = params
            captured["auth"] = auth
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", FakeAuth)
    monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    lines, cursor = default_fetch(
        {"url": "https://logs.example/query_range", "query": '{job="api"}'},
        "2000000000000000000",
    )
    assert captured["trust_env"] is True
    assert isinstance(captured["auth"], FakeAuth)
    assert captured["params"]["direction"] == "forward"
    assert captured["params"]["start"] == "2000000000000000001"
    assert captured["url"] == "https://logs.example/query_range"
    assert [row["line"] for row in lines] == ["TimeoutError early", "TimeoutError late"]
    assert cursor == "2000000000000000002"


def test_default_fetch_skips_lines_without_nanosecond_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuth:
        pass

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {"service": "api"},
                            "values": [
                                ["not-a-ns", "TimeoutError skip"],
                                ["2000000000000000003", "TimeoutError keep"],
                            ],
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", FakeAuth)
    monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    lines, cursor = default_fetch(
        {"url": "https://logs.example/query_range", "query": '{job="api"}'},
        "2000000000000000000",
    )
    assert [row["line"] for row in lines] == ["TimeoutError keep"]
    assert cursor == "2000000000000000003"


def test_default_fetch_prefers_env_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class BoomAuth:
        def __init__(self) -> None:
            raise AssertionError("NetRCAuth must not run when env auth is set")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"result": []}}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            captured["auth"] = auth
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", BoomAuth)
    monkeypatch.setenv("AGENT_ERROR_FIX_USER", "alice")
    monkeypatch.setenv("AGENT_ERROR_FIX_PASSWORD", "secret")
    default_fetch({"url": "https://logs.example/query_range", "query": "{job=\"api\"}"}, None)
    assert captured["auth"] == ("alice", "secret")


@pytest.mark.parametrize(
    ("user", "password"),
    [("alice", None), (None, "secret")],
)
def test_default_fetch_rejects_incomplete_env_auth(
    monkeypatch: pytest.MonkeyPatch, user: str | None, password: str | None
) -> None:
    class BoomAuth:
        def __init__(self) -> None:
            raise AssertionError("NetRCAuth must not run when env auth is incomplete")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"result": []}}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("Client must not run when env auth is incomplete")

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None, auth: object = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("agent_cli.errors.httpx.Client", FakeClient)
    monkeypatch.setattr("agent_cli.errors.httpx.NetRCAuth", BoomAuth)
    if user is None:
        monkeypatch.delenv("AGENT_ERROR_FIX_USER", raising=False)
    else:
        monkeypatch.setenv("AGENT_ERROR_FIX_USER", user)
    if password is None:
        monkeypatch.delenv("AGENT_ERROR_FIX_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("AGENT_ERROR_FIX_PASSWORD", password)
    with pytest.raises(StoreError, match="incomplete"):
        default_fetch(
            {"url": "https://logs.example/query_range", "query": "{job=\"api\"}"},
            None,
        )


def test_scan_rejects_unreadable_cursor(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    cursor_path(tmp_path).mkdir()

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        raise AssertionError("fetch must not run when the cursor is unreadable")

    with pytest.raises(StoreError, match="error-fix.cursor"):
        scan_errors(store, fetch)
