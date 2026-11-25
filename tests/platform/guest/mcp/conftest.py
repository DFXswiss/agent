"""Fixtures of the MCP suite (GST-04): a JSON-RPC client for ``/mcp`` and a fake runner that answers pending calls
through the control socket as RUN-08 will (``POST /v1/pending/{id}/answer``).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_guest.control import ControlContext
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, ControlClient

CONTEXT_RESULT = {
    "org": {"id": "01a1ee56-5e00-7003-8f1b-340691d99f10", "name": "DFX"},
    "workgroup": {"id": "01a1ee56-5e00-7003-8f1b-340691d99f11", "name": "Default", "is_default": True},
    "task": {
        "id": "01a1ee56-5e00-7003-8f1b-340691d99f12",
        "title": "Write the notes",
        "state": "working",
        "owner": {"id": "01a1ee56-5e00-7003-8f1b-340691d99f13", "display_name": "Ada"},
    },
    "session": {"id": SESSION_ID, "parent_session_id": None, "depth": 0, "model_id": None},
    "rights": {"workgroup": "write"},
    "limits": {
        "subagent_max_depth": 5,
        "subagent_max_parallel": 50,
        "subagent_max_total": 0,
        "subagent_max_runtime_seconds": None,
    },
    "api_version": 1,
}


@dataclass
class Rpc:
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class McpClient:
    def __init__(self, port: int) -> None:
        self.port = port
        self.next_id = 0

    def post(self, raw: bytes, *, token: str | None = SESSION_TOKEN, scheme: str = "Bearer") -> Rpc:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = f"{scheme} {token}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.request("POST", "/mcp", body=raw, headers=headers)
            response = connection.getresponse()
            return Rpc(response.status, response.headers, response.read())
        finally:
            connection.close()

    def call(self, method: str, params: dict[str, Any] | None = None, **options: Any) -> Rpc:
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.post(json.dumps(message).encode(), **options)

    def tool(self, name: str, arguments: object = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        reply = self.call("tools/call", params)
        assert reply.status == 200, reply.body
        result: dict[str, Any] = reply.json()["result"]
        return result


@pytest.fixture
def mcp(api_port: int) -> McpClient:
    return McpClient(api_port)


@dataclass
class FakeRunner:
    """Answers ``platform.query`` records from the journal with ``answer(body) -> pending answer`` (or not at all)."""

    context: ControlContext
    control: ControlClient
    answer: Callable[[dict[str, Any]], dict[str, Any] | None]
    seen: list[dict[str, Any]] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    def run(self) -> None:
        after = 0
        while not self.stop.is_set():
            records, head = self.context.journal.read(after, 100, wait=0.05)
            after = head if not records else records[-1]["seq"]
            for record in records:
                if record["kind"] != "platform.query":
                    continue
                self.seen.append({"session_id": record["session_id"], **record["body"]})
                reply = self.answer(record["body"])
                if reply is not None:
                    status = self.control.request("POST", f"/v1/pending/{record['body']['pending_id']}/answer", reply)
                    assert status.status == 200, status.body
            time.sleep(0.01)


@pytest.fixture
def fake_runner(context: ControlContext, control_client: ControlClient) -> Iterator[Callable[..., FakeRunner]]:
    started: list[tuple[FakeRunner, threading.Thread]] = []

    def start(answer: Callable[[dict[str, Any]], dict[str, Any] | None]) -> FakeRunner:
        runner = FakeRunner(context, control_client, answer)
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        started.append((runner, thread))
        return runner

    yield start
    for runner, thread in started:
        runner.stop.set()
        thread.join(5)
