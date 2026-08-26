"""Optional Telegram status posts. Script side-effect, not the ping bus."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .store import Store

TELEGRAM_API = "https://api.telegram.org"
SKIP_PREFIXES = ("supervise busy", "supervise idle", "supervise wait")
LAST_KEY = "supervise_telegram_last"
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


def is_working_line(line: str) -> bool:
    return line.startswith("supervise busy")


def should_notify(line: str, last: str | None) -> bool:
    if line == "":
        return False
    for prefix in SKIP_PREFIXES:
        if line.startswith(prefix):
            return False
    return line != last


def format_status(session_id: str, line: str) -> str:
    return f"{session_id}\n{line}"


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
    cfg = telegram_config(environ)
    if cfg is None:
        return "telegram skipped"
    token, chat_id = cfg
    clock = time.time() if now is None else now
    busy = is_working_line(line) if working is None else working
    if busy:
        store.sync_set(IDLE_AT_KEY, "")
        return "telegram skipped"
    if should_notify(line, store.sync_get(LAST_KEY)):
        send_message(token, chat_id, format_status(session_id, line), post=post)
        store.sync_set(LAST_KEY, line)
        store.sync_set(IDLE_AT_KEY, str(clock))
        return "telegram sent"
    last_idle = _idle_at(store)
    if last_idle is not None and clock - last_idle < idle_seconds(environ):
        return "telegram skipped"
    send_message(token, chat_id, format_not_working(session_id, line), post=post)
    store.sync_set(IDLE_AT_KEY, str(clock))
    return "telegram sent"
