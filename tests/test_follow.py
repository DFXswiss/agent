from __future__ import annotations

from agent_cli.main import should_sync_on_ws


def test_hello_does_not_sync() -> None:
    assert should_sync_on_ws({"type": "hello"}) is False


def test_keepalive_ping_without_id_does_not_sync() -> None:
    assert should_sync_on_ws({"type": "ping"}) is False


def test_events_triggers_sync() -> None:
    assert should_sync_on_ws({"type": "events"}) is True


def test_ping_with_id_triggers_sync() -> None:
    assert should_sync_on_ws({"type": "ping", "id": "x"}) is True
