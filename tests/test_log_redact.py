from __future__ import annotations

import logging
import logging.config
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from uvicorn.logging import AccessFormatter
from websockets.sync.client import connect

from agent_core.log_redact import REDACTED, DropKeyPathFilter, RedactTokenFilter, log_config, redact
from tests.conftest import pair_device

SECRET = "eyJkZXZpY2UiOiJhIn0=.0123abcd"


def record(name: str, msg: str, args: tuple) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)


def test_filter_redacts_http_access_record() -> None:
    rec = record(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/sync/ws?token={SECRET}", "1.1", 403),
    )
    assert RedactTokenFilter().filter(rec) is True
    assert rec.args == ("127.0.0.1:5000", "GET", f"/sync/ws?token={REDACTED}", "1.1", 403)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(rec)
    assert line == f'127.0.0.1:5000 - "GET /sync/ws?token={REDACTED} HTTP/1.1" 403 Forbidden'


def test_filter_redacts_websocket_accept_record() -> None:
    rec = record(
        "uvicorn.error",
        '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:5000", f"/sync/ws?token={SECRET}"),
    )
    RedactTokenFilter().filter(rec)
    assert rec.getMessage() == f'127.0.0.1:5000 - "WebSocket /sync/ws?token={REDACTED}" [accepted]'


def test_filter_redacts_preformatted_message() -> None:
    rec = record("uvicorn.error", f'127.0.0.1:5000 - "WebSocket /sync/ws?token={SECRET}" 403', ())
    RedactTokenFilter().filter(rec)
    assert rec.getMessage() == f'127.0.0.1:5000 - "WebSocket /sync/ws?token={REDACTED}" 403'


def test_filter_leaves_lines_without_token_untouched() -> None:
    args = ("127.0.0.1:5000", "GET", "/sync/pull?since=12&limit=50", "1.1", 200)
    rec = record("uvicorn.access", '%s - "%s %s HTTP/%s" %d', args)
    RedactTokenFilter().filter(rec)
    assert rec.msg == '%s - "%s %s HTTP/%s" %d'
    assert rec.args == args

    plain = record("uvicorn.error", "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)", ("http", "0.0.0.0", 8787))
    RedactTokenFilter().filter(plain)
    assert plain.getMessage() == "Uvicorn running on http://0.0.0.0:8787 (Press CTRL+C to quit)"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (f"/sync/ws?v=2&token={SECRET}", f"/sync/ws?v=2&token={REDACTED}"),
        (f"/sync/ws?token={SECRET}&v=2", f"/sync/ws?token={REDACTED}&v=2"),
        (f"/sync/ws?a=1&token={SECRET}&v=2", f"/sync/ws?a=1&token={REDACTED}&v=2"),
        ("/sync/ws?token=abc%2Edef%26ghi&v=2", f"/sync/ws?token={REDACTED}&v=2"),
        ("/sync/ws%3Fa%3D1%26token%3Dabc.def%26v%3D2", f"/sync/ws%3Fa%3D1%26token%3D{REDACTED}%26v%3D2"),
        ("/sync/ws%253Ftoken%253Dabc.def%2526v%253D2", f"/sync/ws%253Ftoken%253D{REDACTED}%2526v%253D2"),
        (f"/sync/ws?t%6Fken={SECRET}&v=2", f"/sync/ws?t%6Fken={REDACTED}&v=2"),
        (f"/sync/ws?%74%6f%6B%65%6E={SECRET}", f"/sync/ws?%74%6f%6B%65%6E={REDACTED}"),
        (f"token={SECRET}&v=2", f"token={REDACTED}&v=2"),
        (f"/sync/ws?token={REDACTED}&v=2", f"/sync/ws?token={REDACTED}&v=2"),
    ],
)
def test_redact_query_positions_and_encodings(target: str, expected: str) -> None:
    assert redact(target) == expected


@pytest.mark.parametrize(
    "target",
    [
        "/sync/ws?access_token=abc.def",
        "/sync/ws?notoken=abc.def&v=2",
        "/sync/ws?v=2&device_token=abc.def",
        "/sync/ws?acc%65ss_token=abc.def",
        "/sync/ws%3Faccess_token%3Dabc.def",
        "/sync/ws?TOKEN=abc.def",
        "/sync/token=abc.def",
    ],
)
def test_redact_leaves_other_parameter_names_untouched(target: str) -> None:
    assert redact(target) == target


def test_redact_token_next_to_similar_names() -> None:
    target = f"/sync/ws?access_token=public&token={SECRET}&notoken=public"
    assert redact(target) == f"/sync/ws?access_token=public&token={REDACTED}&notoken=public"


def test_filter_redacts_traceback_and_stack() -> None:
    try:
        raise RuntimeError(f"bad request /sync/ws?v=2&token={SECRET}")
    except RuntimeError:
        rec = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", (), sys.exc_info()
        )
    rec.stack_info = f"Stack (most recent call last):\n  url = '/sync/ws?token={SECRET}'"
    RedactTokenFilter().filter(rec)
    line = logging.Formatter("%(levelname)s %(message)s").format(rec)
    assert "RuntimeError: bad request /sync/ws?v=2&token=<redacted>" in line
    assert "url = '/sync/ws?token=<redacted>'" in line
    assert SECRET not in line


def test_filter_redacts_preformatted_exception_text() -> None:
    rec = record("uvicorn.error", "failed", ())
    rec.exc_text = f"ValueError: /sync/ws?token={SECRET}"
    RedactTokenFilter().filter(rec)
    assert rec.exc_text == f"ValueError: /sync/ws?token={REDACTED}"


def test_log_config_filters_every_handler() -> None:
    config = log_config()
    assert set(config["handlers"]) >= {"default", "access"}
    for handler in config["handlers"].values():
        assert handler["filters"] == ["redact_token", "drop_key_path"]
    assert config["filters"]["redact_token"] == {"()": RedactTokenFilter}
    assert config["filters"]["drop_key_path"] == {"()": DropKeyPathFilter}
    assert config["loggers"]["uvicorn.access"]["handlers"] == ["access"]


OBJECT_ID = "0b6f3c2e-4d1a-4f8e-9c3b-7a5d2e1f0c9d"
MARKER = "marker-7f3e9a1c"
ACCESS = '%s - "%s %s HTTP/%s" %d'


def _access(target: str) -> logging.LogRecord:
    return record("uvicorn.access", ACCESS, ("127.0.0.1:5000", "GET", target, "1.1", 401))


@pytest.mark.parametrize(
    "target",
    [
        f"/api/key/record/object?id={OBJECT_ID}",
        f"/api/key/record/comments?task={OBJECT_ID}",
        f"/api/key/record/events?object={OBJECT_ID}",
        "/api/key/record/tasks?assignee=example-handle",
        "/api/key/release?repository=example/repo&pull_request=1",
        f"/api/key/record/{MARKER}",
        f"/api/key/record/{MARKER}/",
        f"/api/%6Bey/record/{MARKER}",
        f"/api%2Fkey/record/{MARKER}",
        "/api/key/",
    ],
)
def test_access_line_under_the_key_prefix_is_dropped(target: str) -> None:
    assert DropKeyPathFilter().filter(_access(target)) is False


@pytest.mark.parametrize(
    "target",
    ["/health", "/sync/pull?since=12", f"/api/keys/{MARKER}", f"/x/api/key/{MARKER}", "/api/key", "/api/record/"],
)
def test_access_line_outside_the_key_prefix_is_kept(target: str) -> None:
    rec = _access(target)
    assert DropKeyPathFilter().filter(rec) is True
    assert rec.args == ("127.0.0.1:5000", "GET", target, "1.1", 401)


def test_access_line_with_dict_args_or_no_args() -> None:
    rec = record("uvicorn.access", "%(path)s", ())
    rec.args = {"path": f"/api/key/record/object?id={OBJECT_ID}"}
    assert DropKeyPathFilter().filter(rec) is False
    rec.args = {"path": "/health"}
    assert DropKeyPathFilter().filter(rec) is True
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain", None, None)
    assert DropKeyPathFilter().filter(plain) is True


@pytest.mark.parametrize("suffix", ["403", "[accepted]", "%d"])
def test_websocket_line_under_the_key_prefix_is_dropped(suffix: str) -> None:
    args: tuple = ("127.0.0.1:5000", f"/api/key/record/{MARKER}?id={OBJECT_ID}")
    if suffix == "%d":
        args = (*args, 403)
    rec = record("uvicorn.error", f'%s - "WebSocket %s" {suffix}', args)
    assert DropKeyPathFilter().filter(rec) is False
    kept = record("uvicorn.error", f'%s - "WebSocket %s" {suffix}', ("127.0.0.1:5000", "/sync/ws?v=2", 403)[: len(args)])
    assert DropKeyPathFilter().filter(kept) is True


def test_preformatted_websocket_line_under_the_key_prefix_is_dropped() -> None:
    line = f'127.0.0.1:5000 - "WebSocket /api/key/record/{MARKER}?id={OBJECT_ID}" 403'
    assert DropKeyPathFilter().filter(record("uvicorn.error", line, ())) is False
    kept = '127.0.0.1:5000 - "WebSocket /sync/ws?v=2" 403'
    assert DropKeyPathFilter().filter(record("uvicorn.error", kept, ())) is True


def test_other_lines_naming_a_key_path_are_kept() -> None:
    # The request lines of KEY-PROTOCOL.md section 5 and RECORD-PROTOCOL.md section 13 go through
    # `uvicorn.error` and name an exact endpoint path; they stay.
    for msg, args in (
        ("record request %s %s %s %d %s", ("example-agent-1", "GET", "/api/key/record/object", 200, "ok")),
        ("key request %s %s %s %d", ("example-service-1", "GET", "/api/key/release", 200)),
    ):
        assert DropKeyPathFilter().filter(record("uvicorn.error", msg, args)) is True
    other = record("agent_core", ACCESS, ("127.0.0.1:5000", "GET", f"/api/key/{MARKER}", "1.1", 401))
    assert DropKeyPathFilter().filter(other) is True
    not_text = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, RuntimeError("x"), None, None)
    assert DropKeyPathFilter().filter(not_text) is True


@contextmanager
def _restored_uvicorn_loggers() -> Iterator[None]:
    loggers = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.access", "uvicorn.error")]
    saved = [(log, list(log.handlers), log.level, log.propagate, log.disabled) for log in loggers]
    try:
        yield
    finally:
        for log, handlers, level, propagate, disabled in saved:
            for handler in log.handlers:
                if handler not in handlers:
                    handler.close()
            log.handlers[:] = handlers
            log.setLevel(level)
            log.propagate = propagate
            log.disabled = disabled


def test_configured_access_handler_writes_no_key_path(capsys: pytest.CaptureFixture[str]) -> None:
    """A `uvicorn.access` record goes through the handlers that `log_config()` builds."""
    config = log_config()
    assert config["handlers"]["access"]["stream"] == "ext://sys.stdout"
    with _restored_uvicorn_loggers():
        logging.config.dictConfig(config)
        log = logging.getLogger("uvicorn.access")
        log.info(ACCESS, "127.0.0.1:5000", "GET", f"/api/key/record/object?id={OBJECT_ID}", "1.1", 401)
        log.info(ACCESS, "127.0.0.1:5000", "GET", f"/api/key/record/{MARKER}", "1.1", 401)
        log.info(ACCESS, "127.0.0.1:5000", "GET", "/health", "1.1", 200)
    output = capsys.readouterr().out
    assert '"GET /health HTTP/1.1" 200' in output
    assert OBJECT_ID not in output
    assert MARKER not in output
    assert "/api/key/" not in output


def test_server_log_has_path_but_not_token(hub: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    token = pair_device(hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa77")
    loggers = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.access", "uvicorn.error")]
    saved = [(log, list(log.handlers), log.level, log.propagate, log.disabled) for log in loggers]
    server = uvicorn.Server(
        uvicorn.Config(hub.app, host="127.0.0.1", port=0, log_level="info", log_config=log_config())
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        with connect(f"ws://127.0.0.1:{port}/sync/ws?v=2&token={token}") as ws:
            assert '"hello"' in ws.recv(timeout=5)
        with pytest.raises(Exception):
            connect(f"ws://127.0.0.1:{port}/sync/missing?token={token}")
        assert httpx.get(f"http://127.0.0.1:{port}/health?token={token}").status_code == 200
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        stopped = not thread.is_alive()
        for log, handlers, level, propagate, disabled in saved:
            for handler in log.handlers:
                if handler not in handlers:
                    handler.close()
            log.handlers[:] = handlers
            log.setLevel(level)
            log.propagate = propagate
            log.disabled = disabled
    assert stopped, "server thread did not stop"

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert f'"WebSocket /sync/ws?v=2&token={REDACTED}" [accepted]' in output
    assert f"/sync/missing?token={REDACTED}" in output
    assert f'"GET /health?token={REDACTED} HTTP/1.1" 200' in output
    assert token not in output
    assert token.split(".")[0] not in output


def test_server_log_holds_no_path_or_query_under_the_key_prefix(
    hub: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """RECORD-PROTOCOL.md section 13: an object id, a handle, a repository or a path a client chose
    under `/api/key/` never enters the process log, also not through uvicorn's own access and
    WebSocket lines of a real server."""
    handle = "example-handle-3d9b"
    repository = "example-org/example-repo-5c2a"
    targets = [
        f"/api/key/record/object?id={OBJECT_ID}",
        f"/api/key/record/comments?task={OBJECT_ID}",
        f"/api/key/record/events?object={OBJECT_ID}",
        f"/api/key/record/tasks?assignee={handle}",
        f"/api/key/release?repository={repository}&pull_request=7",
        f"/api/key/record/{MARKER}",
        f"/api/key/record/{MARKER}-slash/",
        f"/api/%6Bey/record/{MARKER}-encoded",
    ]
    with _restored_uvicorn_loggers():
        server = uvicorn.Server(
            uvicorn.Config(hub.app, host="127.0.0.1", port=0, log_level="info", log_config=log_config())
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert time.monotonic() < deadline, "server did not start"
                time.sleep(0.02)
            port = server.servers[0].sockets[0].getsockname()[1]
            for target in targets:
                assert httpx.get(f"http://127.0.0.1:{port}{target}").status_code == 401
            for method in ("POST", "HEAD", "PATCH"):
                url = f"http://127.0.0.1:{port}/api/key/record/{MARKER}-{method.lower()}?id={OBJECT_ID}"
                assert httpx.request(method, url).status_code == 401
            with pytest.raises(Exception):
                connect(f"ws://127.0.0.1:{port}/api/key/record/{MARKER}-ws?id={OBJECT_ID}")
            assert httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            stopped = not thread.is_alive()
    assert stopped, "server thread did not stop"

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert '"GET /health HTTP/1.1" 200' in output
    for text in (OBJECT_ID, MARKER, handle, repository, "/api/key/", "/api/%6Bey/", "WebSocket /api/"):
        assert text not in output
