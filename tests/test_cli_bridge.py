from __future__ import annotations

import json
import socket

import pytest

from agent_cli.cli_bridge import allowed, handle_request
from agent_cli.runtime import Completed
from agent_cli.stub import parse_endpoint


@pytest.mark.no_pg
def test_allowed_store_and_spine() -> None:
    assert allowed(["task", "list"]) is True
    assert allowed(["next"]) is True
    assert allowed(["close-step"]) is True
    assert allowed(["session", "register", "--id", "s1", "--kind", "runner"]) is True
    assert allowed(["session", "skill", "attach", "--id", "s1", "--skill", "spine"]) is True
    assert allowed(["skills", "path"]) is True
    assert allowed(["status"]) is True
    assert allowed(["gate", "record"]) is True


@pytest.mark.no_pg
def test_denied_control_git_hub() -> None:
    assert allowed(["session", "start", "--id", "s1"]) is False
    assert allowed(["session", "input", "--id", "s1", "--data", "x"]) is False
    assert allowed(["session", "keep-working", "--id", "s1"]) is False
    assert allowed(["run"]) is False
    assert allowed(["github", "pending"]) is False
    assert allowed(["watch", "assigned"]) is False
    assert allowed(["pair"]) is False
    assert allowed(["sync", "--follow"]) is False
    assert allowed([]) is False
    assert allowed(["session"]) is False


@pytest.mark.no_pg
def test_handle_request_runs_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        seen.append(list(argv))
        return Completed(0, "ok\n", "")

    monkeypatch.setattr("agent_cli.cli_bridge.agent_argv", lambda: ["agent"])
    out = handle_request(b'{"argv":["task","list"]}\n', runner=runner)
    assert out == {"exit_code": 0, "stdout": "ok\n", "stderr": ""}
    assert seen == [["agent", "task", "list"]]


@pytest.mark.no_pg
def test_handle_request_rejects_denied() -> None:
    out = handle_request(b'{"argv":["run"]}\n', runner=lambda _a: Completed(0, "", ""))
    assert out["exit_code"] == 2
    assert "not allowed" in out["stderr"]


@pytest.mark.no_pg
def test_handle_request_rejects_bad_json() -> None:
    out = handle_request(b"{", runner=lambda _a: Completed(0, "", ""))
    assert out["exit_code"] == 2
    assert "invalid JSON" in out["stderr"]


@pytest.mark.no_pg
def test_handle_request_rejects_non_object() -> None:
    out = handle_request(b"[]", runner=lambda _a: Completed(0, "", ""))
    assert out["exit_code"] == 2
    assert "object" in out["stderr"]


@pytest.mark.no_pg
def test_handle_request_rejects_bad_argv() -> None:
    out = handle_request(b'{"argv":[""]}', runner=lambda _a: Completed(0, "", ""))
    assert out["exit_code"] == 2
    assert "argv" in out["stderr"]


@pytest.mark.no_pg
def test_parse_endpoint() -> None:
    assert parse_endpoint("127.0.0.1:7846") == ("127.0.0.1", 7846)
    with pytest.raises(SystemExit):
        parse_endpoint("")
    with pytest.raises(SystemExit):
        parse_endpoint("no-port")
    with pytest.raises(SystemExit):
        parse_endpoint(":7846")
    with pytest.raises(SystemExit):
        parse_endpoint("127.0.0.1:0")


@pytest.mark.no_pg
def test_stub_call_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cli import stub

    class _FakeSock:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, data: bytes) -> None:
            self.sent += data

        def recv(self, _n: int) -> bytes:
            if self.sent:
                payload = json.dumps({"exit_code": 0, "stdout": "hi\n", "stderr": ""}) + "\n"
                self.sent = b""
                return payload.encode("utf-8")
            return b""

        def close(self) -> None:
            return None

    fake = _FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: fake)
    code = stub.call(["task", "list"], endpoint="127.0.0.1:7846")
    assert code == 0
