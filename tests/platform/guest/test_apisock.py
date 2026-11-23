"""U-GST-control (api.sock): the JSON-lines protocol between the API process and the control process."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_guest import apisock
from agent_guest.apisock import MAX_LINE, ApiClient, ApiSockError, ApiSockServer, token_sha256
from agent_guest.state import StateError


class Failing(Exception):
    def __init__(self, code: str, detail: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def op_echo(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {"args": args, "context": context}


def op_coded(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    raise Failing("not_found", [f"/{index}" for index in range(40)])


def op_plain(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    raise StateError("state_conflict")


def op_crash(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("secret detail that must not leak")


def op_numeric_code(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    error = RuntimeError("x")
    error.code = 5  # type: ignore[attr-defined]
    raise error


OPS = {"echo": op_echo, "coded": op_coded, "plain": op_plain, "crash": op_crash, "numeric": op_numeric_code}


@pytest.fixture
def sock_path(short_tmp: Path) -> Iterator[str]:
    path = str(short_tmp / "a.sock")
    server = ApiSockServer(path, OPS, "ctx")
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def exchange(path: str, *lines: bytes) -> list[dict[str, Any]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(path)
        reader = client.makefile("rb")
        answers = []
        for line in lines:
            client.sendall(line)
            answers.append(json.loads(reader.readline()))
        reader.close()
        return answers


def test_sequential_requests_on_one_connection(sock_path: str) -> None:
    answers = exchange(
        sock_path,
        b'{"id": 1, "op": "echo", "args": {"a": 1}}\n',
        b'{"id": 2, "op": "echo"}\n',
    )
    assert answers == [
        {"id": 1, "ok": True, "result": {"args": {"a": 1}, "context": "ctx"}},
        {"id": 2, "ok": True, "result": {"args": {}, "context": "ctx"}},
    ]


@pytest.mark.parametrize(
    ("line", "answer"),
    [
        (b"{\n", {"id": None, "ok": False, "error": "request_invalid"}),
        (b"[" * 100000 + b"\n", {"id": None, "ok": False, "error": "request_invalid"}),
        (b"[1]\n", {"id": None, "ok": False, "error": "request_invalid"}),
        (b'{"id": "1", "op": "echo"}\n', {"id": None, "ok": False, "error": "request_invalid"}),
        (b'{"id": true, "op": "echo"}\n', {"id": None, "ok": False, "error": "request_invalid"}),
        (b'{"id": 1, "op": "echo", "x": 1}\n', {"id": 1, "ok": False, "error": "request_invalid"}),
        (b'{"id": 1, "op": 5}\n', {"id": 1, "ok": False, "error": "request_invalid"}),
        (b'{"id": 1, "op": "echo", "args": []}\n', {"id": 1, "ok": False, "error": "request_invalid"}),
        (b'{"id": 1, "op": "echo", "op": "x"}\n', {"id": None, "ok": False, "error": "request_invalid"}),
        (b'{"id": 1, "op": "nope"}\n', {"id": 1, "ok": False, "error": "not_found"}),
        (b'{"id": 1, "op": "plain"}\n', {"id": 1, "ok": False, "error": "state_conflict"}),
        (b'{"id": 1, "op": "crash"}\n', {"id": 1, "ok": False, "error": "unavailable"}),
        (b'{"id": 1, "op": "numeric"}\n', {"id": 1, "ok": False, "error": "unavailable"}),
    ],
)
def test_protocol_errors(
    sock_path: str, line: bytes, answer: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert exchange(sock_path, line) == [answer]
    assert "secret detail" not in json.dumps(answer)


def test_error_detail_is_bounded(sock_path: str) -> None:
    (answer,) = exchange(sock_path, b'{"id": 3, "op": "coded"}\n')
    assert answer["error"] == "not_found"
    assert answer["detail"] == [f"/{index}" for index in range(32)]


@pytest.mark.parametrize("data", [b"x" * (MAX_LINE + 1), b'{"id": 1, "op": "echo"}'])
def test_oversized_or_unterminated_line_closes(sock_path: str, data: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(sock_path)
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        reader = client.makefile("rb")
        assert json.loads(reader.readline()) == {"id": None, "ok": False, "error": "size_exceeded"}
        assert reader.readline() == b""
        reader.close()


def test_client_round_trip_and_pool(sock_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client = ApiClient(sock_path)
    try:
        assert client.call("echo", {"x": 1}) == {"args": {"x": 1}, "context": "ctx"}
        first = client._idle[0]
        assert client.call("echo", timeout=10) == {"args": {}, "context": "ctx"}
        assert client._idle == [first]
        with pytest.raises(ApiSockError) as error:
            client.call("coded")
        assert (error.value.code, len(error.value.detail)) == ("not_found", 32)
        with pytest.raises(ApiSockError) as error:
            client.call("plain")
        assert (error.value.code, error.value.detail) == ("state_conflict", [])
        monkeypatch.setattr(apisock, "POOL_SIZE", 1)
        extra = apisock._Connection(sock_path, 1)
        client._release(extra)
        assert client._idle == [first]
    finally:
        client.close()
    assert client._idle == []


def test_client_parallel_calls(sock_path: str) -> None:
    client = ApiClient(sock_path)
    results: list[dict[str, Any]] = []

    def worker(index: int) -> None:
        results.append(client.call("echo", {"i": index}))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    client.close()
    assert sorted(result["args"]["i"] for result in results) == list(range(20))


def test_client_without_server(short_tmp: Path) -> None:
    client = ApiClient(str(short_tmp / "missing.sock"))
    with pytest.raises(ApiSockError) as error:
        client.hello()
    assert error.value.code == "unavailable"


def fake_server(path: str, reply: bytes) -> threading.Thread:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        connection.makefile("rb").readline()
        connection.sendall(reply)
        connection.close()
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("reply", [b"", b"{oops\n", b'{"id": 99, "ok": true, "result": {}}\n', b"[]\n"])
def test_client_refuses_broken_answers(short_tmp: Path, reply: bytes) -> None:
    path = str(short_tmp / "f.sock")
    thread = fake_server(path, reply)
    client = ApiClient(path, timeout=5)
    with pytest.raises(ApiSockError) as error:
        client.call("anything")
    assert error.value.code == "unavailable"
    assert client._idle == []
    thread.join(5)


def test_token_sha256() -> None:
    assert token_sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_api_process_modules_never_load_the_journal(repo_root: Path) -> None:
    """The API process has no write path: its modules never load the journal, the state or the control process.

    (``sqlite3`` itself is not checked: coverage measurement of the subprocess loads it.)"""
    code = (
        "import sys, agent_guest.server, agent_guest.registry as r; r.api_routes(); r.mcp_tools(); r.hook_addons();"
        "print(sorted(m for m in ('agent_guest.journal', 'agent_guest.state', 'agent_guest.control')"
        " if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=repo_root)
    assert done.stdout.strip() == "[]"
