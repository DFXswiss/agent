"""Unit tests of the session-side client of the attack suite (agent_doubles.atk.session_client) against a local
HTTP server that echoes what it received."""

from __future__ import annotations

import base64
import io
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_doubles.atk import session_client


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        answer = json.dumps(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body.decode()}
        ).encode()
        self.send_response(429 if self.path == "/limited" else 200)
        self.send_header("Retry-After", "7")
        self.send_header("X-Other", "dropped")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


@pytest.fixture
def server() -> Iterator[int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


def run(requests: list[dict[str, Any]], port: int, token: str = "tok") -> list[dict[str, Any]]:
    ticks = iter(range(100))
    return session_client.run(requests, token, port=port, clock=lambda: float(next(ticks)))


def test_requests_with_json_text_base64_and_the_token(server: int) -> None:
    answers = run(
        [
            {"label": "json", "path": "/mcp", "headers": [["Authorization", "Bearer {token}"]], "json": {"a": 1}},
            {"label": "text", "path": "/limited", "text": "{", "repeat": 2},
            {"label": "b64", "path": "/x", "base64": base64.b64encode(b"raw-body").decode()},
            {"label": "none", "path": "/y"},
            {"label": "fill", "path": "/z", "fill": ["[", "ab", 3, "]"]},
        ],
        server,
    )
    assert [a["label"] for a in answers] == ["json", "text", "text", "b64", "none", "fill"]
    assert json.loads(answers[5]["body"])["body"] == "[ababab]"
    first = json.loads(answers[0]["body"])
    assert first["headers"]["authorization"] == "Bearer tok"
    assert first["body"] == '{"a": 1}'
    assert answers[0]["status"] == 200
    assert answers[0]["seconds"] == 1.0
    assert answers[1]["status"] == 429
    assert answers[1]["headers"] == {"retry-after": "7"}
    assert json.loads(answers[3]["body"])["body"] == "raw-body"
    assert json.loads(answers[4]["body"])["body"] == ""


def test_raw_requests_are_sent_unchanged(server: int) -> None:
    raw = b"POST /raw HTTP/1.1\r\nHost: x\r\nX-Token: {token}\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi"
    [answer] = run([{"label": "raw", "raw": base64.b64encode(raw).decode()}], server)
    assert answer["status"] == 200
    assert answer["headers"] == {"retry-after": "7"}
    echoed = json.loads(answer["body"])
    assert (echoed["path"], echoed["headers"]["x-token"], echoed["body"]) == ("/raw", "tok", "hi")


def test_send_raw_without_a_status_line() -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        conn.recv(1024)
        conn.sendall(b"garbage\r\nNoColonLine\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    status, headers, body = session_client.send_raw(b"x", "127.0.0.1", port)
    thread.join(5)
    listener.close()
    assert (status, headers, body) == (0, {"nocolonline": ""}, "")


def test_a_connection_failure_is_status_zero() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    [answer] = run([{"label": "down", "path": "/"}], port)
    assert answer["status"] == 0
    assert answer["body"].startswith("ConnectionRefusedError")


def test_main_reads_stdin_and_the_token_from_the_environment(server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_client, "PORT", server)
    out = io.StringIO()
    requests = [{"label": "m", "path": "/m", "headers": [["X-T", "{token}"]]}]
    assert session_client.main(io.StringIO(json.dumps(requests)), out, {"ATK_TOKEN": "env-token"}) == 0
    [answer] = json.loads(out.getvalue())["answers"]
    assert json.loads(answer["body"])["headers"]["x-t"] == "env-token"


def test_main_defaults_to_the_process_streams(
    server: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(session_client, "PORT", server)
    monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
    monkeypatch.delenv("ATK_TOKEN", raising=False)
    assert session_client.main() == 0
    assert json.loads(capsys.readouterr().out) == {"answers": []}


def test_an_early_refusal_is_read_although_the_body_was_not_taken() -> None:
    """The guest answers 413 after the headers and closes; the client still reports the answer."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(65536)
        conn.sendall(b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 27\r\nConnection: close\r\n\r\n")
        conn.sendall(b'{"error":"size_exceeded"}\r\n')
        conn.shutdown(socket.SHUT_WR)
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    [answer] = run([{"label": "big", "path": "/mcp", "fill": ["", "a", 32 * 1024 * 1024, ""]}], port)
    thread.join(5)
    listener.close()
    assert answer["status"] == 413
    assert json.loads(answer["body"]) == {"error": "size_exceeded"}
