"""U-DBL-model (shared part): journal, state document, server plumbing, CLI helpers and client of the doubles."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from agent_doubles.common import cli
from agent_doubles.common.client import DoubleClient, Response
from agent_doubles.common.journal import BODY_TEXT_LIMIT, Journal, decode_body, redact_headers, redact_value
from agent_doubles.common.server import MAX_BODY, BodyError, DoubleHandler, DoubleServer, validate_host
from agent_doubles.common.state import state_document

# --- journal ----------------------------------------------------------------------------------


def test_redaction_hides_credentials() -> None:
    headers = redact_headers(
        [
            ("Authorization", "Bearer secret-value"),
            ("X-Api-Key", "abc"),
            ("Cookie", "c=1"),
            ("Accept", "*/*"),
            ("X-Multi", "a"),
            ("x-multi", "b"),
            ("Proxy-Authorization", "token-without-scheme"),
        ]
    )
    assert headers == {
        "authorization": "Bearer <redacted 12 chars>",
        "x-api-key": "<redacted 3 chars>",
        "cookie": "<redacted 3 chars>",
        "accept": "*/*",
        "x-multi": "a, b",
        "proxy-authorization": "<redacted 20 chars>",
    }
    assert redact_value("Accept", "x") == "x"


def test_decode_body_forms() -> None:
    assert decode_body(b"") is None
    assert decode_body(b'{"a": 1}') == {"a": 1}
    assert decode_body(b"\xff" + b"x" * (BODY_TEXT_LIMIT + 10)) == "�" + "x" * (BODY_TEXT_LIMIT - 1)


def test_journal_records_and_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = Journal("demo", path)
    first = journal.open("GET", "/a", "q=1", {"accept": "*/*"}, None)
    first.status, first.note, first.extra["step"] = 200, "fine", "s1"
    journal.close(first)
    second = journal.open("POST", "/b", "", {}, {"k": "v"})
    journal.close(second)
    assert len(journal) == 2
    records = journal.records()
    assert records[0] == {
        "seq": 1,
        "double": "demo",
        "method": "GET",
        "path": "/a",
        "query": "q=1",
        "headers": {"accept": "*/*"},
        "body": None,
        "status": 200,
        "note": "fine",
        "step": "s1",
    }
    journal.clear()
    assert journal.records() == []
    assert journal.open("GET", "/c", "", {}, None).seq == 3
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    Journal("memory").close(first)


def test_state_document() -> None:
    assert state_document("x", done=True, failures=[], requests=2, extra=1) == {
        "double": "x",
        "extra": 1,
        "failures": [],
        "requests": 2,
        "done": True,
        "ok": True,
    }
    assert not state_document("x", done=True, failures=["bad"], requests=0)["ok"]
    assert not state_document("x", done=False, failures=[], requests=0)["ok"]


# --- server -----------------------------------------------------------------------------------


class EchoHandler(DoubleHandler):
    """Answers with the body length, or the body error, and a chunked variant on /chunked."""

    calls: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.do_POST()

    def do_HEAD(self) -> None:
        self.send_json(200, {"head": True})

    def do_POST(self) -> None:
        try:
            body = self.read_body()
        except BodyError as exc:
            self.send_json(exc.status, {"error": exc.message})
            return
        if self.path == "/chunked":
            self.start_chunked(200, "text/plain", (("x-extra", "1"),))
            self.write_chunk(b"ab")
            self.write_chunk(b"cd")
            self.end_chunked()
            return
        self.send_body(200, str(len(body)).encode(), "text/plain", (("x-extra", "2"),))


def raw_request(server: DoubleServer, data: bytes) -> bytes:
    with socket.create_connection((server.host, server.port), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def test_server_routes_and_lifecycle() -> None:
    with DoubleServer(EchoHandler, "127.0.0.1", 0) as server:
        assert server.url == f"http://127.0.0.1:{server.port}"
        client = DoubleClient(server.url)
        response = client.request("POST", "/", b"12345")
        assert (response.status, response.body, response.headers["x-extra"]) == (200, b"5", "2")
        assert client.request("POST", "/", {"a": 1}).body == b"8"
        assert client.request("GET", "/").body == b"0"
        head = client.request("HEAD", "/")
        assert (head.status, head.body) == (200, b"")
        chunked = client.request("POST", "/chunked")
        assert (chunked.body, chunked.headers["transfer-encoding"]) == (b"abcd", "chunked")
        bad = raw_request(server, b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n")
        assert bad.startswith(b"HTTP/1.1 400")
        assert b"connection: close" in bad.lower()
        unsized = raw_request(server, b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        assert unsized.startswith(b"HTTP/1.1 411")
        huge = raw_request(server, f"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n".encode())
        assert huge.startswith(b"HTTP/1.1 413")


def test_server_serve_forever_in_calling_thread() -> None:
    server = DoubleServer(EchoHandler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    assert DoubleClient(server.url).request("POST", "/", b"x").body == b"1"
    server.stop()
    thread.join(5)
    assert not thread.is_alive()


def test_server_ipv6_and_refusals() -> None:
    with DoubleServer(EchoHandler, "::1", 0) as server:
        assert server.url == f"http://[::1]:{server.port}"
        assert DoubleClient(server.url).request("POST", "/", b"ab").body == b"2"
    DoubleServer(EchoHandler, "127.0.0.1", 0).stop()
    for host in ("", " 127.0.0.1", "127.0.0.1 ", "a b", "0.0.0.0", "::", "*"):
        with pytest.raises(ValueError, match="invalid bind address"):
            validate_host(host)
    with pytest.raises(ValueError, match="invalid port"):
        DoubleServer(EchoHandler, "127.0.0.1", 70000)


# --- client -----------------------------------------------------------------------------------


def test_client_refuses_bad_urls() -> None:
    for url in ("https://127.0.0.1:1", "http://127.0.0.1", "http:///x"):
        with pytest.raises(ValueError, match="invalid double URL"):
            DoubleClient(url)
    assert Response(200, {}, b"[1]").json() == [1]


# --- cli --------------------------------------------------------------------------------------


class FakeDouble:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.events: list[str] = []

    @property
    def url(self) -> str:
        return "http://127.0.0.1:4242"

    def start(self) -> FakeDouble:
        self.events.append("start")
        return self

    def stop(self) -> None:
        self.events.append("stop")

    def state(self) -> dict[str, Any]:
        return {"ok": self.ok}


def test_cli_arguments_and_port_file(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    cli.add_server_arguments(parser)
    args = parser.parse_args(["--port", "5", "--port-file", str(tmp_path / "p"), "--journal", str(tmp_path / "j")])
    assert (args.host, args.port, args.port_file, args.journal) == ("127.0.0.1", 5, tmp_path / "p", tmp_path / "j")
    cli.write_port_file(tmp_path / "p", 1234)
    assert (tmp_path / "p").read_text() == "1234\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_serve_until_signal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = signal.getsignal(signal.SIGINT)

    def by_signal(stop: threading.Event) -> None:
        signal.raise_signal(signal.SIGINT)
        cli.poll_wait(stop)

    double = FakeDouble(ok=True)
    assert cli.serve_until_signal(double, tmp_path / "port", by_signal) == cli.EXIT_OK
    assert double.events == ["start", "stop"]
    assert (tmp_path / "port").read_text() == "4242\n"
    out = capsys.readouterr()
    assert out.out == "http://127.0.0.1:4242\n"
    assert json.loads(out.err) == {"ok": True}
    assert signal.getsignal(signal.SIGINT) is before

    def immediate(stop: threading.Event) -> None:
        """Return at once, as if the stop signal had already arrived."""

    assert cli.serve_until_signal(FakeDouble(ok=False), None, immediate) == cli.EXIT_NOT_OK


def test_poll_wait_returns_when_set() -> None:
    stop = threading.Event()
    timer = threading.Timer(0.25, stop.set)
    timer.start()
    cli.poll_wait(stop)
    assert stop.is_set()


def test_serve_until_signal_stops_on_error() -> None:
    double = FakeDouble(ok=True)

    def broken(stop: threading.Event) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cli.serve_until_signal(double, None, broken)
    assert double.events == ["start", "stop"]
