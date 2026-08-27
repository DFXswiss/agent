from __future__ import annotations

from pathlib import Path

from agent_cli.store import Store
from agent_cli.telegram_act import notify_status, reset_idle_clock


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _env() -> dict[str, str]:
    return {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}


def test_idle_prompt_does_not_page(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(json)
        return FakeResponse(200)

    store.sync_set("supervise_idle_streak", "99")
    out = notify_status(
        store,
        "runner-1",
        "supervise stalled assigned=x streak=99",
        environ=_env(),
        post=post,
        working=True,
    )
    assert out == "telegram skipped"
    assert calls == []


def test_pages_once_when_session_is_gone(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[dict[str, object]] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        assert isinstance(json, dict)
        calls.append(json)
        return FakeResponse(200)

    first = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=_env(),
        post=post,
        working=False,
    )
    second = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=_env(),
        post=post,
        working=False,
    )
    assert first == "telegram sent"
    assert second == "telegram skipped"
    assert calls[0]["text"] == "not working\nrunner-1\nsupervise idle"


def test_session_back_allows_a_later_page(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(json)
        return FakeResponse(200)

    notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=_env(),
        post=post,
        working=False,
    )
    notify_status(
        store,
        "runner-1",
        "supervise busy",
        environ=_env(),
        post=post,
        working=True,
    )
    again = notify_status(
        store,
        "runner-1",
        "supervise idle",
        environ=_env(),
        post=post,
        working=False,
    )
    assert again == "telegram sent"
    assert len(calls) == 2


def test_reset_idle_clock_clears_page_flag(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.sync_set("supervise_telegram_paged", "1")
    reset_idle_clock(store, working=True)
    assert store.sync_get("supervise_telegram_paged") == ""


def test_httpx_timeout_becomes_runtime_error(tmp_path: Path) -> None:
    import httpx

    store = Store(tmp_path)

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        raise httpx.TimeoutException("timeout")

    try:
        notify_status(
            store,
            "runner-1",
            "supervise idle",
            environ=_env(),
            post=post,
            working=False,
        )
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "telegram send failed" in str(exc)
    assert raised is True


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
