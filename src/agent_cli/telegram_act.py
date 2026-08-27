"""Optional Telegram status posts. Script side-effect, not the ping bus."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .store import Store

TELEGRAM_API = "https://api.telegram.org"
IDLE_AT_KEY = "supervise_telegram_idle_at"
IDLE_NOTIFY_SECONDS = 600


def telegram_config(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if not isinstance(token, str) or token == "":
        return None
    if not isinstance(chat, str) or chat == "":
        return None
    return token, chat


def idle_seconds(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = env.get("TELEGRAM_IDLE_SECONDS", "")
    if isinstance(raw, str) and raw.isdigit():
        value = int(raw)
        if value >= 1:
            return value
    return IDLE_NOTIFY_SECONDS


def format_not_working(session_id: str, line: str) -> str:
    return f"not working\n{session_id}\n{line}"


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    post: Callable[..., Any] | None = None,
) -> None:
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    sender = post if post is not None else httpx.post
    try:
        response = sender(url, json=body, timeout=10.0)
    except OSError as exc:
        raise RuntimeError("telegram send failed") from exc
    status = getattr(response, "status_code", None)
    if status != 200:
        raise RuntimeError(f"telegram send failed: HTTP {status}")


def reset_idle_clock(store: Store, *, working: bool, now: float | None = None) -> None:
    """Follow start must not inherit a stale idle timestamp."""
    if working:
        store.sync_set(IDLE_AT_KEY, "")
        return
    clock = time.time() if now is None else now
    store.sync_set(IDLE_AT_KEY, str(clock))


def _idle_at(store: Store) -> float | None:
    raw = store.sync_get(IDLE_AT_KEY)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def notify_status(
    store: Store,
    session_id: str,
    line: str,
    *,
    environ: Mapping[str, str] | None = None,
    post: Callable[..., Any] | None = None,
    now: float | None = None,
    working: bool | None = None,
) -> str:
    """Post not-working only after a full idle window of grok_working False.

    Turn gaps of a few seconds must not page the operator.
    """
    cfg = telegram_config(environ)
    if cfg is None:
        return "telegram skipped"
    token, chat_id = cfg
    clock = time.time() if now is None else now
    busy = working if working is not None else line.startswith("supervise busy")
    if busy:
        store.sync_set(IDLE_AT_KEY, "")
        return "telegram skipped"
    last_idle = _idle_at(store)
    window = idle_seconds(environ)
    if last_idle is None:
        store.sync_set(IDLE_AT_KEY, str(clock))
        return "telegram skipped"
    if clock - last_idle < window:
        return "telegram skipped"
    send_message(token, chat_id, format_not_working(session_id, line), post=post)
    store.sync_set(IDLE_AT_KEY, str(clock))
    return "telegram sent"
