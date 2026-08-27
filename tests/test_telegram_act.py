from __future__ import annotations

from pathlib import Path

from agent_cli.store import Store
from agent_cli.telegram_act import notify_status


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_idle_arms_then_posts_after_ten_minutes(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[dict[str, object]] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        assert isinstance(json, dict)
        calls.append(json)
        return FakeResponse(200)

    env = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "123",
        "TELEGRAM_IDLE_SECONDS": "600",
    }
    first = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=1_000.0,
        working=False,
    )
    soon = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=1_000.0 + 60,
        working=False,
    )
    later = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=1_000.0 + 600,
        working=False,
    )
    assert first == "telegram skipped"
    assert soon == "telegram skipped"
    assert later == "telegram sent"
    assert len(calls) == 1
    assert calls[0]["text"] == "not working\nrunner-1\nsupervise idle"


def test_busy_clears_timer_so_short_gaps_do_not_page(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(json)
        return FakeResponse(200)

    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
    notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=1_000.0,
        working=False,
    )
    busy = notify_status(
        store,
        "runner-1",
        "supervise busy assigned=x",
        environ=env,
        post=post,
        now=1_010.0,
        working=True,
    )
    gap = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=1_020.0,
        working=False,
    )
    assert busy == "telegram skipped"
    assert gap == "telegram skipped"
    assert calls == []


def test_reset_idle_clock_drops_stale_timestamp(tmp_path: Path) -> None:
    from agent_cli.telegram_act import reset_idle_clock

    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(json)
        return FakeResponse(200)

    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
    store.sync_set("supervise_telegram_idle_at", "1")
    reset_idle_clock(store, working=False, now=10_000.0)
    out = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=10_000.0,
        working=False,
    )
    assert out == "telegram skipped"
    assert calls == []
    reset_idle_clock(store, working=True, now=10_001.0)
    assert store.sync_get("supervise_telegram_idle_at") == ""


def test_notify_skipped_without_env(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(200)

    out = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ={},
        post=post,
        working=False,
    )
    assert out == "telegram skipped"
    assert calls == []


def test_notify_http_error_does_not_advance_timer(tmp_path: Path) -> None:
    store = Store(tmp_path)

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        return FakeResponse(401)

    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
    notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=env,
        post=post,
        now=1_000.0,
        working=False,
    )
    try:
        notify_status(
            store,
            "runner-1",
            "supervise idle",
            environ=env,
            post=post,
            now=1_000.0 + 600,
            working=False,
        )
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "401" in str(exc)
        assert "tok" not in str(exc)
    assert raised is True
    assert store.sync_get("supervise_telegram_idle_at") == "1000.0"
