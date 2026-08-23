from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.errors import (
    config_path,
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


def test_redact_and_fingerprint() -> None:
    raw = "TimeoutError bearer SECRETTOKENVALUE0123456789 user@example.com"
    cleaned = redact(raw)
    assert "SECRETTOKENVALUE0123456789" not in cleaned
    assert "user@example.com" not in cleaned
    assert "[redacted]" in cleaned
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
