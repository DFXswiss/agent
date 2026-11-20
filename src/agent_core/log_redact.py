"""Keep device tokens out of the process log."""

from __future__ import annotations

import copy
import logging
import re
from typing import Any
from urllib.parse import unquote

from uvicorn.config import LOGGING_CONFIG

from .signing import KEY_PREFIX

REDACTED = "<redacted>"


def _escaped(code: int) -> str:
    return rf"(?i:%(?:25)*{code:02X})"


# `token=<value>` for a query parameter named exactly `token`: at the start of
# a query string or after `&`, not `access_token=` or `notoken=`. The request
# target may itself be percent-encoded (`%3Ftoken%3D<value>%26...`, any
# encoding depth) and the name may be spelled with percent-escapes (`t%6Fken=`).
_NAME = "".join(f"(?:{c}|{_escaped(ord(c))})" for c in "token")
_AMP = _escaped(ord("&"))
_TOKEN = re.compile(
    rf"(^|[?&\s\"']|{_escaped(ord('?'))}|{_AMP})({_NAME})"
    rf"(?:(=)[^&\s\"']+|({_escaped(ord('='))})(?:(?!{_AMP})[^&\s\"'])+)"
)
_EXCEPTIONS = logging.Formatter()


def redact(text: str) -> str:
    return _TOKEN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or m.group(4)}{REDACTED}", text)


def _redact_arg(arg: Any) -> Any:
    if isinstance(arg, (bool, int, float)) or arg is None:
        return arg
    text = arg if isinstance(arg, str) else str(arg)
    clean = redact(text)
    return arg if clean == text else clean


class RedactTokenFilter(logging.Filter):
    """Rewrite the `token` query value in a record's message, args and traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_arg(arg) for key, arg in record.args.items()}
        # Formatters reuse `exc_text` when it is set, so the traceback is
        # rendered here and redacted before any handler formats the record.
        if isinstance(record.exc_info, tuple) and not record.exc_text:
            record.exc_text = _EXCEPTIONS.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


# The WebSocket lines of uvicorn on `uvicorn.error`: `<client> - "WebSocket <path and query>" ...`,
# as a format with arguments or already formatted.
_WEBSOCKET_FORMAT = '"WebSocket %s"'
_WEBSOCKET_LINE = re.compile(r' - "WebSocket (\S+)"')


def _under_key_prefix(target: str) -> bool:
    path = target.split("?", 1)[0]
    return path.startswith(KEY_PREFIX) or unquote(path).startswith(KEY_PREFIX)


class DropKeyPathFilter(logging.Filter):
    """Drop uvicorn's own HTTP access and WebSocket lines for a path under `/api/key/`.

    uvicorn's own line for a request contains its path and its full query. Under `/api/key/` both
    can carry text a client chose, such as an object id or a handle (RECORD-PROTOCOL.md section 13).
    This filter drops uvicorn's own line for every such request; it adds no line in its place.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access":
            # `%s - "%s %s HTTP/%s" %d` with the client, method, path and query, version, status.
            return not _any_under_key_prefix(record.args)
        if record.name == "uvicorn.error" and isinstance(record.msg, str):
            if _WEBSOCKET_FORMAT in record.msg:
                return not _any_under_key_prefix(record.args)
            match = _WEBSOCKET_LINE.search(record.msg)
            return match is None or not _under_key_prefix(match.group(1))
        return True


def _any_under_key_prefix(args: Any) -> bool:
    if isinstance(args, dict):
        values = list(args.values())
    elif isinstance(args, tuple):
        values = list(args)
    else:
        return False
    return any(isinstance(value, str) and _under_key_prefix(value) for value in values)


def log_config() -> dict[str, Any]:
    """uvicorn's logging config with the filters on every handler.

    HTTP access lines go through `uvicorn.access`, WebSocket lines through
    `uvicorn.error`; filtering at the handlers covers both.
    """
    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {
        "redact_token": {"()": RedactTokenFilter},
        "drop_key_path": {"()": DropKeyPathFilter},
    }
    for handler in config["handlers"].values():
        handler["filters"] = ["redact_token", "drop_key_path"]
    return config
