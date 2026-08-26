from __future__ import annotations

from pathlib import Path

from agent_cli.store import Store
from agent_cli.telegram_act import notify_status, should_notify


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_should_notify_skips_busy_idle_wait() -> None:
    assert should_notify("supervise busy assigned=x", None) is False
    assert should_notify("supervise idle", None) is False
    assert should_notify("supervise wait assigned=x last=a/b/c", None) is False
    assert should_notify("supervise commission assigned=x", None) is True
    assert should_notify("supervise commission assigned=x", "supervise commission assigned=x") is False


def test_notify_skipped_without_env(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[object] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(200)

    out = notify_status(
        store,
        "runner-1",
        "supervise commission assigned=x",
        environ={},
        post=post,
    )
    assert out == "telegram skipped"
    assert calls == []


def test_notify_posts_once_then_skips_duplicate(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[tuple[str, object]] = []

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        calls.append((url, json))
        return FakeResponse(200)

    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
    first = notify_status(
        store,
        "runner-1",
        "supervise commission assigned=x",
        environ=env,
        post=post,
    )
    second = notify_status(
        store,
        "runner-1",
        "supervise commission assigned=x",
        environ=env,
        post=post,
    )
    assert first == "telegram sent"
    assert second == "telegram skipped"
    assert len(calls) == 1
    url, body = calls[0]
    assert url.endswith("/bottok/sendMessage")
    assert "tok" in url
    assert isinstance(body, dict)
    assert body["chat_id"] == "123"
    assert body["text"] == "runner-1\nsupervise commission assigned=x"
    assert body["disable_web_page_preview"] is True


def test_notify_http_error_does_not_record_last(tmp_path: Path) -> None:
    store = Store(tmp_path)

    def post(url: str, json: object, timeout: float) -> FakeResponse:
        return FakeResponse(401)

    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
    try:
        notify_status(
            store,
            "runner-1",
            "supervise done assigned=x",
            environ=env,
            post=post,
        )
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "401" in str(exc)
        assert "tok" not in str(exc)
    assert raised is True
    assert store.sync_get("supervise_telegram_last") is None
