"""The control-socket test client reads an answer that arrives before the request body was sent (INF-01).

A server that answers from the request head alone (404 for an unknown path) and closes can meet the client between the
two sends of `http.client`; under load the body then hits a closed socket. The answer is already readable.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .conftest import ControlClient

ANSWER = b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"


@pytest.fixture
def early_server(short_tmp: Path) -> Iterator[str]:
    """Answers after the head and closes without reading the body."""
    path = str(short_tmp / "early.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            head = b""
            while b"\r\n\r\n" not in head:
                head += connection.recv(4096)
            connection.sendall(ANSWER)
            connection.shutdown(socket.SHUT_RDWR)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield path
    thread.join(5)
    listener.close()


def test_an_answer_before_the_body_is_read(early_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    send = http.client.HTTPConnection.send
    calls: list[int] = []

    def late_body(self: http.client.HTTPConnection, data: Any) -> None:
        calls.append(len(data))
        if len(calls) == 2:  # the body: sent after the server answered and closed
            time.sleep(0.3)
        send(self, data)

    monkeypatch.setattr(http.client.HTTPConnection, "send", late_body)
    reply = ControlClient(early_server).request("POST", "/v1/sessions/not-a-uuid/control", {"action": "stop"})
    assert (reply.status, reply.json()) == (404, {})
    assert len(calls) == 2
