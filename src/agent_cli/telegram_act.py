"""Optional Telegram status posts. Script side-effect, not the ping bus."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .store import Store

TELEGRAM_API = "https://api.telegram.org"
SKIP_PREFIXES = ("supervise busy", "supervise idle", "supervise wait")
LAST_KEY = "supervise_telegram_last"


def telegram_config(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if not isinstance(token, str) or token == "":
        return None
    if not isinstance(chat, str) or chat == "":
        return None
    return token, chat


def should_notify(line: str, last: str | None) -> bool:
    if line == "":
        return False
    for prefix in SKIP_PREFIXES:
        if line.startswith(prefix):
            return False
    return line != last


def format_status(session_id: str, line: str) -> str:
    return f"{session_id}\n{line}"


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


def notify_status(
    store: Store,
    session_id: str,
    line: str,
    *,
    environ: Mapping[str, str] | None = None,
    post: Callable[..., Any] | None = None,
) -> str:
    cfg = telegram_config(environ)
    if cfg is None:
        return "telegram skipped"
    if not should_notify(line, store.sync_get(LAST_KEY)):
        return "telegram skipped"
    token, chat_id = cfg
    send_message(token, chat_id, format_status(session_id, line), post=post)
    store.sync_set(LAST_KEY, line)
    return "telegram sent"
