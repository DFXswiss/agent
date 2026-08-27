from __future__ import annotations

from pathlib import Path

from agent_cli.store import Store
from agent_cli.telegram_act import TELEGRAM_IDLE_TICKS, notify_status, reset_idle_clock


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _post_env() -> dict[str, str]:
    return {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}


def test_pages_only_after_consecutive_idle_ticks_and_only_once(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[dict[str, object]] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        assert isinstance(json, dict)
        calls.append(json)
        return FakeResponse(200)

    env = _post_env()
    for n in range(1, TELEGRAM_IDLE_TICKS):
        store.sync_set("supervise_idle_streak", str(n))
        out = notify_status(
            store,
            "runner-1",
            "supervise quiet",
            environ=env,
            post=post,
            working=False,
        )
        assert out == "telegram skipped"
    store.sync_set("supervise_idle_streak", str(TELEGRAM_IDLE_TICKS))
    first = notify_status(
        store,
        "runner-1",
        "supervise stalled",
        environ=env,
        post=post,
        working=False,
    )
    second = notify_status(
        store,
        "runner-1",
        "supervise stalled",
        environ=env,
        post=post,
        working=False,
    )
    assert first == "telegram sent"
    assert second == "telegram skipped"
    assert len(calls) == 1
    assert calls[0]["text"] == "not working\nrunner-1\nsupervise stalled"


def test_busy_resets_page_flag(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(json)
        return FakeResponse(200)

    env = _post_env()
    store.sync_set("supervise_idle_streak", str(TELEGRAM_IDLE_TICKS))
    notify_status(
        store,
        "runner-1",
        "supervise stalled",
        environ=env,
        post=post,
        working=False,
    )
    assert len(calls) == 1
    notify_status(
        store,
        "runner-1",
        "supervise busy",
        environ=env,
        post=post,
        working=True,
    )
    store.sync_set("supervise_idle_streak", str(TELEGRAM_IDLE_TICKS))
    again = notify_status(
        store,
        "runner-1",
        "supervise stalled",
        environ=env,
        post=post,
        working=False,
    )
    assert again == "telegram sent"
    assert len(calls) == 2


def test_reset_idle_clock_clears_streak(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.sync_set("supervise_idle_streak", "99")
    store.sync_set("supervise_telegram_paged", "1")
    reset_idle_clock(store, working=False)
    assert store.sync_get("supervise_idle_streak") == "0"
    assert store.sync_get("supervise_telegram_paged") == ""


def test_notify_skipped_without_env(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(200)

    store.sync_set("supervise_idle_streak", "99")
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
