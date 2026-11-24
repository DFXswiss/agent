"""Relays: forwarding, records, cut/restore, unreachable target, stop."""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from orchestrator.relay import Relay


@pytest.fixture
def echo() -> Iterator[tuple[str, int]]:
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                while data := conn.recv(1024):
                    conn.sendall(data)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield ("127.0.0.1", server.getsockname()[1])
    stop.set()
    thread.join()
    server.close()


def wait_for(records: list[dict[str, Any]], event: str) -> None:
    deadline = time.monotonic() + 5
    while not any(r["event"] == event for r in records):
        assert time.monotonic() < deadline, records
        time.sleep(0.02)


def test_forwarding_records_and_cut(echo: tuple[str, int]) -> None:
    records: list[dict[str, Any]] = []
    relay = Relay("hub", ("127.0.0.1", 0), echo, records.append)
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            client.sendall(b"ping")
            assert client.recv(4) == b"ping"
        wait_for(records, "close")
        closed = next(r for r in records if r["event"] == "close")
        assert closed["bytes_up"] == 4
        assert closed["bytes_down"] == 4
        assert closed["link"] == "hub"
        assert closed["target"] == f"127.0.0.1:{echo[1]}"
        assert (closed["first"], closed["reason"]) == ("up", "eof")
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            client.sendall(b"x")
            assert client.recv(1) == b"x"
            relay.cut()
            assert client.recv(1) == b""
        assert relay.state()["cut"] is True
        assert relay.state()["connections"] == 2
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            assert client.recv(1) == b""
        wait_for(records, "refused")
        relay.restore()
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            client.sendall(b"y")
            assert client.recv(1) == b"y"
    finally:
        relay.stop()
    assert relay.state()["listen"] == f"127.0.0.1:{relay.port}"


def test_unreachable_target_is_recorded() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    records: list[dict[str, Any]] = []
    relay = Relay("model", ("127.0.0.1", 0), ("127.0.0.1", dead), records.append)
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            assert client.recv(1) == b""
        wait_for(records, "failed")
        assert records[-1]["error"] == "ConnectionRefusedError"
    finally:
        relay.stop()


def test_tap_copies_both_directions(echo: tuple[str, int], tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    relay = Relay("browser-hub", ("127.0.0.1", 0), echo, records.append, tap=str(tmp_path / "tap"))
    try:
        for payload in (b"first", b"second"):
            with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
                client.sendall(payload)
                assert client.recv(16) == payload
        deadline = time.monotonic() + 5
        while sum(1 for r in records if r["event"] == "close") < 2:
            assert time.monotonic() < deadline, records
            time.sleep(0.02)
    finally:
        relay.stop()
    names = sorted(path.name for path in (tmp_path / "tap").iterdir())
    assert names == ["00001.down", "00001.up", "00002.down", "00002.up"]
    for number, payload in (("00001", b"first"), ("00002", b"second")):
        assert (tmp_path / "tap" / f"{number}.down").read_bytes() == payload  # what the target sent back
        assert (tmp_path / "tap" / f"{number}.up").read_bytes() == payload  # what the client sent


def test_the_end_record_names_the_side_that_ended_first() -> None:
    """A target that reads the request and closes without an answer (`down`, `eof`) and one that resets the
    connection (`down`, `recv:ConnectionResetError`) are told apart from a client that leaves (`up`)."""
    server = socket.create_server(("127.0.0.1", 0))
    modes = iter(("close", "reset"))

    def serve() -> None:
        for mode in modes:
            conn, _ = server.accept()
            conn.recv(16)
            if mode == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    records: list[dict[str, Any]] = []
    relay = Relay("github", ("127.0.0.1", 0), ("127.0.0.1", server.getsockname()[1]), records.append)
    try:
        for count in (1, 2):
            with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
                client.sendall(b"POST")
                assert client.recv(16) == b""
            deadline = time.monotonic() + 5
            while sum(1 for r in records if r["event"] == "close") < count:
                assert time.monotonic() < deadline, records
                time.sleep(0.02)
    finally:
        relay.stop()
        thread.join()
        server.close()
    ends = [(r["first"], r["reason"], r["bytes_up"], r["bytes_down"]) for r in records if r["event"] == "close"]
    assert ends == [("down", "eof", 4, 0), ("down", "recv:ConnectionResetError", 4, 0)]


def test_a_failed_send_ends_the_copy_with_its_error(echo: tuple[str, int]) -> None:
    relay = Relay("model", ("127.0.0.1", 0), echo, lambda _record: None)
    source, peer = socket.socketpair()
    sink = socket.socket()
    sink.close()
    try:
        peer.sendall(b"data")
        ended: list[tuple[str, str]] = []
        counts = [0, 0]
        relay._pump(source, sink, counts, 1, ended)
        assert (ended, counts) == ([("down", "send:OSError")], [0, 0])
    finally:
        peer.close()
        relay.stop()


def test_a_copy_wakes_its_sibling_but_never_releases_a_descriptor() -> None:
    """KOR-16: both copies of a connection end; if each closed both sockets, the second close (and its shutdown) could
    act on a descriptor number the first close already released and a new connection (of any relay of the device)
    received: that connection would end at once, its request forwarded and its answer lost. A copy therefore only
    shuts the sockets down; they stay open until the connection's thread closes them after both copies ended."""
    relay = Relay("github", ("127.0.0.1", 0), ("127.0.0.1", 9), lambda _record: None)
    source, peer = socket.socketpair()
    sink, far = socket.socketpair()
    try:
        peer.sendall(b"POST")
        peer.shutdown(socket.SHUT_WR)
        ended: list[tuple[str, str]] = []
        relay._pump(source, sink, [0, 0], 0, ended)
        assert ended == [("up", "eof")]
        assert source.fileno() != -1
        assert sink.fileno() != -1
        assert far.recv(16) == b"POST"
        assert far.recv(16) == b""  # the sibling copy (reading from the target side) is woken
    finally:
        for sock in (source, peer, sink, far):
            sock.close()
        relay.stop()


def test_every_relayed_connection_releases_its_descriptors_once_it_ended(echo: tuple[str, int]) -> None:
    records: list[dict[str, Any]] = []
    relay = Relay("github", ("127.0.0.1", 0), echo, records.append)
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5):
            pass  # a warm-up connection: the relay's threads exist
        wait_for(records, "close")
        before = len(os.listdir("/dev/fd"))
        for count in range(2, 12):
            with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
                client.sendall(b"x")
                assert client.recv(1) == b"x"
                if count == 11:
                    relay.cut()
                    assert client.recv(1) == b""
            deadline = time.monotonic() + 5
            while sum(1 for r in records if r["event"] == "close") < count:
                assert time.monotonic() < deadline, records
                time.sleep(0.02)
        assert len(os.listdir("/dev/fd")) == before
        assert relay._open == set()
    finally:
        relay.stop()


def test_a_late_shutdown_on_a_reused_descriptor_gives_the_recorded_picture() -> None:
    """KOR-16, the situation of the failed connects: a `shutdown` that reaches the descriptor number of a live client
    socket (as a copy thread's late shutdown of an already released number did) ends the connection after the request
    was forwarded: the target received and answered it, the client reads end-of-file, the relay records `up`/`eof`
    with 0 bytes down."""
    server = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []
    answered = threading.Event()

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(64))
            answered.wait(5)
            with contextlib.suppress(OSError):
                conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    records: list[dict[str, Any]] = []
    relay = Relay("github", ("127.0.0.1", 0), ("127.0.0.1", server.getsockname()[1]), records.append)
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            request = b"POST /login/device/code HTTP/1.1\r\n\r\n"
            client.sendall(request)
            deadline = time.monotonic() + 5
            while not received:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with relay._lock:
                accepted = next(s for s in relay._open if s.getsockname()[1] == relay.port)
            stale = socket.socket(fileno=os.dup(accepted.fileno()))
            stale.shutdown(socket.SHUT_RDWR)
            stale.close()
            assert client.recv(64) == b""
            # the target answers only once the relay recorded the end: an answer racing the upstream EOF would end the
            # connection on the downstream side first (a send error) on a loaded machine
            wait_for(records, "close")
            answered.set()
    finally:
        relay.stop()
        thread.join()
        server.close()
    closed = next(r for r in records if r["event"] == "close")
    assert received == [request]
    assert (closed["first"], closed["reason"], closed["bytes_up"], closed["bytes_down"]) == (
        "up",
        "eof",
        len(request),
        0,
    )


def test_a_tap_that_cannot_be_written_ends_the_connection_with_its_error(echo: tuple[str, int], tmp_path: Path) -> None:
    tap = tmp_path / "tap"
    tap.mkdir()
    (tap / "00001.up").mkdir()  # the copy cannot open its tap file
    records: list[dict[str, Any]] = []
    relay = Relay("browser-hub", ("127.0.0.1", 0), echo, records.append, tap=str(tap))
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            client.sendall(b"x")
            assert client.recv(1) == b""
        wait_for(records, "close")
    finally:
        relay.stop()
    closed = next(r for r in records if r["event"] == "close")
    assert (closed["first"], closed["reason"], closed["bytes_down"]) == ("up", "tap:IsADirectoryError", 0)
