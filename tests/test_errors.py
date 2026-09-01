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
    is_incident_line,
    known_asset_in,
    known_chain_in,
    line_fingerprint,
    load_config,
    redact,
    scan_errors,
    stack_sig,
    template_fingerprint,
    template_signature,
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
    path.write_text(json.dumps({"session_id": "s", "password ": "x"}), encoding="utf-8")
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
        json.dumps({"session_id": "s", "headers": {"api-key": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?api-key=secret"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "client_secret": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "aws_secret_access_key": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="must not contain credentials"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "url": "https://logs.example/q?access_key=secret"}),
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
    digest = redact("Authorization: Digest username=alice, nonce=abc")
    assert "username=alice" not in digest
    assert "nonce=abc" not in digest
    dsn = redact("postgresql://u:p@logs.example/db TimeoutError")
    assert "u:p@" not in dsn
    assert "TimeoutError" in dsn
    cookie = redact("Cookie: sid=1; extra=remain")
    assert "sid=1" not in cookie
    assert "extra=remain" not in cookie
    jwt = redact("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc_def-ghi")
    assert "eyJhbGciOiJIUzI1NiJ9" not in jwt
    akia = redact("AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in akia
    client = redact('{"client_secret":"s3cretvalue"} access-token=abcdef')
    assert "s3cretvalue" not in client
    assert "abcdef" not in client
    aws = redact('{"aws_secret_access_key":"wJalrXUtnFEMI"} aws_secret_access_key=wJalrXUtnFEMI')
    assert "wJalrXUtnFEMI" not in aws
    assert error_class(cleaned) == "TimeoutError"
    sig = stack_sig(cleaned)
    assert len(sig) == 16
    fp = fingerprint(service="api", error_class="TimeoutError", stack_sig=sig, environment="prod")
    assert fp.startswith("api|TimeoutError|")
    assert fp.endswith("|prod")


def test_template_signature_masks_known_chains() -> None:
    ethereum = "Timeout updating balances for Ethereum: Error: Timeout"
    polygon = "Timeout updating balances for Polygon: Error: Timeout"
    assert template_signature(ethereum) == template_signature(polygon)
    # stack_sig stays fine-grained: chain name is not masked there, so the two
    # lines keep separate error.seen identity even though they share a template.
    assert stack_sig(ethereum) != stack_sig(polygon)


def test_template_signature_masks_known_assets() -> None:
    usdc = "Balance for Arbitrum/USDC went low"
    wbtc = "Balance for Arbitrum/WBTC went low"
    assert template_signature(usdc) == template_signature(wbtc)
    assert stack_sig(usdc) != stack_sig(wbtc)


def test_asset_token_regex_masks_longest_match_first() -> None:
    from agent_cli.errors import _ASSET_TOKEN

    # "USD" is a literal prefix of "USDC" — an unsorted alternation would match
    # "USD" first and leave "C" dangling in the masked output.
    assert _ASSET_TOKEN.sub("<ASSET>", "balance in USDC today") == "balance in <ASSET> today"
    assert _ASSET_TOKEN.sub("<ASSET>", "balance in USD today") == "balance in <ASSET> today"


def test_known_asset_in_finds_and_omits() -> None:
    assert known_asset_in("Balance for Arbitrum/USDC went low") == "USDC"
    assert known_asset_in("Failed to get price for token tether -> usd") is None


def test_chain_token_regex_masks_longest_match_first() -> None:
    from agent_cli.errors import _CHAIN_TOKEN

    # "Bitcoin" is a prefix of "BitcoinTestnet4" — an unsorted alternation would
    # match "Bitcoin" first and leave "Testnet4" dangling in the masked output.
    assert _CHAIN_TOKEN.sub("<CHAIN>", "check failed for BitcoinTestnet4 today") == (
        "check failed for <CHAIN> today"
    )
    assert _CHAIN_TOKEN.sub("<CHAIN>", "check failed for Bitcoin today") == (
        "check failed for <CHAIN> today"
    )


def test_known_chain_in_finds_and_omits() -> None:
    assert known_chain_in("Timeout updating balances for Ethereum") == "Ethereum"
    assert known_chain_in("balance check failed for BitcoinTestnet4") == "BitcoinTestnet4"
    assert known_chain_in("Failed to check Bank Frick order status") == "Frick"
    assert known_chain_in("Failed to get price for token tether -> usd") is None


def test_template_fingerprint_format() -> None:
    sig = template_signature("Timeout updating balances for Ethereum")
    tfp = template_fingerprint(
        service="api", error_class="error", template_sig=sig, environment="prod"
    )
    assert tfp == f"api|error|{sig}|prod"


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
    assert "template_fingerprint" in payload
    wakes = store.pending_wakes()
    assert any(w["activity_id"] == created[0] for w in wakes)

    created2, enriched2 = scan_errors(store, fetch)
    assert created2 == []
    assert enriched2 == created
    again = store.row("activity", created[0])
    assert again is not None
    assert again["payload"]["count"] == 2
    assert again["payload"]["template_fingerprint"] == payload["template_fingerprint"]
    assert len([w for w in store.pending_wakes() if w["activity_id"] == created[0]]) == 1
    assert "line_fingerprint" not in payload


def test_line_fingerprint_is_sha256_of_server_container_line() -> None:
    import hashlib

    line = "boom once"
    expect = hashlib.sha256(b"prd\napi\nboom once").hexdigest()
    assert line_fingerprint(server="prd", container="api", line=line) == expect


def test_scan_stores_line_fingerprint_when_labels_present(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    line = "TimeoutError bearer SECRETTOKENVALUE0123456789 boom"

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:00Z",
                    "line": line,
                    "server": "prd",
                    "container": "api",
                }
            ],
            None,
        )

    created, _ = scan_errors(store, fetch)
    payload = store.row("activity", created[0])["payload"]
    raw_fp = line_fingerprint(server="prd", container="api", line=line)
    redacted_fp = line_fingerprint(server="prd", container="api", line=redact(line))
    assert payload["line_fingerprint"] == raw_fp
    assert raw_fp != redacted_fp
    assert "SECRETTOKENVALUE0123456789" not in payload["excerpt"]

    def fetch_no_labels(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [{"ts": "2026-08-23T16:00:01Z", "line": line}],
            None,
        )

    _, enriched = scan_errors(store, fetch_no_labels)
    assert enriched == created
    again = store.row("activity", created[0])["payload"]
    assert "line_fingerprint" not in again

    def fetch_labels(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:02Z",
                    "line": line,
                    "server": "prd",
                    "container": "api",
                }
            ],
            None,
        )

    _, enriched2 = scan_errors(store, fetch_labels)
    assert enriched2 == created
    labeled = store.row("activity", created[0])["payload"]
    assert labeled["line_fingerprint"] == raw_fp


def test_scan_omits_line_fingerprint_when_server_is_not_utf8(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:00Z",
                    "line": "TimeoutError boom",
                    "server": "\ud800",
                    "container": "api",
                }
            ],
            None,
        )

    created, _ = scan_errors(store, fetch)
    payload = store.row("activity", created[0])["payload"]
    assert "line_fingerprint" not in payload
    assert payload["class"] == "TimeoutError"


def test_scan_requires_error_fix_skill(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write("session", "insert", "runner-1", {"id": "runner-1", "kind": "runner", "status": "active", "skills": ["spine"]})
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": "boom"}], None)

    with pytest.raises(StoreError, match="does not have skill error-fix"):
        scan_errors(store, fetch)


def test_fingerprint_uses_full_redacted_line(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)
    prefix = "x" * 500

    def fetch_timeout(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:00Z", "line": prefix + " TimeoutError boom"}], None)

    created, _ = scan_errors(store, fetch_timeout)
    assert len(created) == 1
    assert store.row("activity", created[0])["payload"]["class"] == "TimeoutError"

    def fetch_value(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return ([{"ts": "2026-08-23T16:00:01Z", "line": prefix + " ValueError boom"}], None)

    created2, enriched2 = scan_errors(store, fetch_value)
    assert enriched2 == []
    assert len(created2) == 1
    assert created2[0] != created[0]
    assert store.row("activity", created2[0])["payload"]["class"] == "ValueError"


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
    assert "server" not in lines[0]
    assert "container" not in lines[0]


def test_default_fetch_maps_server_and_container_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                            "stream": {
                                "service": "api",
                                "server": "prd",
                                "container_name": "api",
                            },
                            "values": [["2000000000000000001", "TimeoutError one"]],
                        },
                        {
                            "stream": {"service": "api", "server": "prd"},
                            "values": [["2000000000000000002", "TimeoutError two"]],
                        },
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
    lines, _cursor = default_fetch(
        {"url": "https://logs.example/query_range", "query": '{job="api"}'},
        None,
    )
    both = next(row for row in lines if row["line"] == "TimeoutError one")
    partial = next(row for row in lines if row["line"] == "TimeoutError two")
    assert both["server"] == "prd"
    assert both["container"] == "api"
    assert partial["server"] == "prd"
    assert "container" not in partial


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


def test_is_incident_line_keeps_timeout_error_and_logger_level() -> None:
    assert is_incident_line("TimeoutError boom") is True
    assert is_incident_line("2026-08-23 12:00:00 ERROR [Service] failed") is True


def test_is_incident_line_drops_access_logs_and_plain_error_word() -> None:
    assert is_incident_line("POST /v1/log/clientError 204 - - 12ms") is False
    assert is_incident_line("TRACE /v1/log/clientError 204 -") is False
    assert is_incident_line("CONNECT /v1/log/clientError 204 -") is False
    assert is_incident_line("\x1b[32mGET /health 200\x1b[0m ok") is False
    assert is_incident_line("LOG request failed with an error during retry") is False


def test_scan_errors_drops_non_incident_lines(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _write_config(tmp_path)

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {
                    "ts": "2026-08-23T16:00:00Z",
                    "line": "POST /v1/log/clientError 204 - - 12ms",
                },
                {
                    "ts": "2026-08-23T16:00:01Z",
                    "line": "\x1b[32mGET /health 200\x1b[0m ok",
                },
                {
                    "ts": "2026-08-23T16:00:02Z",
                    "line": "LOG request failed with an error during retry",
                },
                {
                    "ts": "2026-08-23T16:00:05Z",
                    "line": "TRACE /v1/log/clientError 204 -",
                },
                {
                    "ts": "2026-08-23T16:00:06Z",
                    "line": "CONNECT /v1/log/clientError 204 -",
                },
                {"ts": "2026-08-23T16:00:03Z", "line": "TimeoutError boom"},
                {
                    "ts": "2026-08-23T16:00:04Z",
                    "line": "2026-08-23 12:00:00 ERROR [Service] failed",
                },
            ],
            "cursor-1",
        )

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 2
    classes = {
        store.row("activity", aid)["payload"]["class"] for aid in created
    }
    assert classes == {"TimeoutError", "error"}
    assert cursor_path(tmp_path).read_text(encoding="utf-8").strip() == "cursor-1"


def test_line_must_not_match_drops_otherwise_valid_error(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    config_path(tmp_path).write_text(
        json.dumps(
            {
                "session_id": "runner-1",
                "service": "api",
                "environment": "prod",
                "repo": "org/app",
                "line_must_not_match": "noisy",
            }
        ),
        encoding="utf-8",
    )

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {"ts": "2026-08-23T16:00:00Z", "line": "ERROR noisy Service boom"},
                {"ts": "2026-08-23T16:00:01Z", "line": "ERROR keep Service boom"},
            ],
            None,
        )

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 1
    assert "keep" in store.row("activity", created[0])["payload"]["excerpt"]


def test_load_config_rejects_invalid_line_filters(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    path.write_text(
        json.dumps({"session_id": "s", "line_must_match": ""}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="line_must_match is invalid"):
        load_config(path)
    path.write_text(
        json.dumps({"session_id": "s", "line_must_not_match": "("}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="line_must_not_match is invalid"):
        load_config(path)


def test_line_must_match_keeps_and_drops(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    config_path(tmp_path).write_text(
        json.dumps(
            {
                "session_id": "runner-1",
                "service": "api",
                "environment": "prod",
                "repo": "org/app",
                "line_must_match": "keep",
            }
        ),
        encoding="utf-8",
    )

    def fetch(_cfg: dict, _cursor: str | None) -> tuple[list[dict], str | None]:
        return (
            [
                {"ts": "2026-08-23T16:00:00Z", "line": "ERROR drop Service boom"},
                {"ts": "2026-08-23T16:00:01Z", "line": "ERROR keep Service boom"},
            ],
            None,
        )

    created, enriched = scan_errors(store, fetch)
    assert enriched == []
    assert len(created) == 1
    assert "keep" in store.row("activity", created[0])["payload"]["excerpt"]


def test_token_masking_only_matches_whole_tokens() -> None:
    """Without boundary anchors "Base" matches inside "Based", "SOL" inside
    "RESOLVE" and "COMP" inside "COMPLETE" — unrelated errors would then be
    masked as chain/asset variants and labelled with a token that has nothing to
    do with them."""
    for line in (
        "Based on the previous failure the job aborted",
        "RESOLVE failed for host",
        "COMPLETE checkout failed",
        "DAILY reconciliation failed",
        "UNIQUE constraint violated on table users",
        "POLICY denied the request",
        "BATCH job failed",
        "MANAGEMENT api unreachable",
        "LINKING accounts failed",
        "SANDBOX unavailable",
    ):
        assert known_chain_in(line) is None, line
        assert known_asset_in(line) is None, line


def test_token_masking_still_matches_real_names_next_to_punctuation() -> None:
    assert known_chain_in("Balance for Arbitrum/USDC went low") == "Arbitrum"
    assert known_asset_in("Balance for Arbitrum/USDC went low") == "USDC"
    # Tickers that are not word-character-only, or start with a digit, still match.
    assert known_asset_in("USDC.e drift on Arbitrum") == "USDC.e"
    assert known_asset_in("low balance 1INCH on Ethereum") == "1INCH"


def test_template_signature_does_not_group_unrelated_words_with_assets() -> None:
    # "UNIQUE" must not collapse onto the same template as a real ticker just
    # because "UNI" is a prefix of it.
    assert template_signature("UNIQUE constraint failed") != template_signature(
        "UNI constraint failed"
    )


def test_template_fingerprint_fields_cannot_collide() -> None:
    """service, error_class and environment are free text from the log source.
    An unescaped join would let two different field tuples produce one
    fingerprint and group unrelated errors under a single template."""
    first = template_fingerprint(
        service="a", error_class="b|c", template_sig="sig", environment="e"
    )
    second = template_fingerprint(
        service="a|b", error_class="c", template_sig="sig", environment="e"
    )
    assert first != second

    # The escape itself must not become a new collision route.
    assert template_fingerprint(
        service="a%7Cb", error_class="c", template_sig="sig", environment="e"
    ) != second


def test_asset_alternation_must_try_the_longest_name_first() -> None:
    """The boundary anchors settle word-character names on their own, but not a
    ticker containing punctuation: "." is not a word character, so "USDC" would
    match inside "USDC.e" and strip it down to the wrong asset."""
    assert known_asset_in("USDC.e drift on Arbitrum") == "USDC.e"
    assert known_asset_in("USDC drift on Arbitrum") == "USDC"


def test_token_boundaries_are_unicode_aware() -> None:
    """An ASCII-only boundary class would let a ticker glued to non-Latin letters
    or to an underscore still count as a whole token."""
    assert known_asset_in("ЖUSDCб drift") is None
    assert known_asset_in("USDC_balance drift") is None
    assert known_chain_in("Ethereumб handler") is None
    # Real names next to ordinary punctuation still match.
    assert known_asset_in("balance for USDC, low") == "USDC"


def test_a_prefix_name_does_not_survive_its_longer_form_losing_the_boundary() -> None:
    """"USDC.e" glued to a word character fails its own trailing boundary. Without
    blocking the continuation the engine falls back to "USDC", whose boundary
    passes because "." is not a word character — so the same glued text would
    yield a ticker here but None in "USDC_balance"."""
    assert known_asset_in("USDC_balance") is None
    assert known_asset_in("USDC.e_balance") is None
    assert known_asset_in("USDC.eб") is None
    # The longer name still matches on its own, and a sentence-final period is
    # not a continuation.
    assert known_asset_in("USDC.e drift on Arbitrum") == "USDC.e"
    assert known_asset_in("balance in USDC.") == "USDC"
