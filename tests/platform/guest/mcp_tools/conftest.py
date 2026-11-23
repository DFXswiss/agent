"""Fixtures of the platform tool suite (GST-07): contract fixtures of every tool, the MCP client of GST-04's suite, a
second session and a fake runner that answers ``platform.query`` and ``operation.request`` records through the control
socket as RUN-07/RUN-08/RUN-11 will (``POST /v1/pending/{id}/answer``).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_guest.control import ControlContext
from agent_guest.state import Session
from tests.platform.guest.conftest import ControlClient, make_session
from tests.platform.guest.mcp.conftest import McpClient

OTHER_SESSION_ID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
OTHER_SESSION_TOKEN = "b3RoZXItc2Vzc2lvbi10b2tlbi1mb3ItdGVzdHMtMDAx"
FORWARDED_KINDS = ("platform.query", "operation.request")

Answer = Callable[[str, dict[str, Any], str], dict[str, Any] | None]


def contract_fixtures(contracts_dir: Path, name: str, validity: str) -> list[tuple[str, Any]]:
    """``(file stem, document)`` of ``contracts/fixtures/mcp/tools/<name>/<validity>/*.json``, sorted."""
    folder = contracts_dir / "fixtures" / "mcp" / "tools" / name / validity
    return [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in sorted(folder.glob("*.json"))]


@pytest.fixture
def mcp(api_port: int) -> McpClient:
    return McpClient(api_port)


@pytest.fixture
def other_session(context: ControlContext) -> Session:
    """A second working session of the same VM."""
    created = make_session(OTHER_SESSION_ID, OTHER_SESSION_TOKEN)
    context.state.save_session(created)
    return created


def forwarded(context: ControlContext) -> list[dict[str, Any]]:
    """Every ``platform.query``/``operation.request`` record of the journal (``kind``, ``session_id``, ``body``)."""
    records, _ = context.journal.read(0, 1000)
    return [
        {"kind": record["kind"], "session_id": record["session_id"], "body": record["body"]}
        for record in records
        if record["kind"] in FORWARDED_KINDS
    ]


@dataclass
class FakeRunner:
    """Answers forwarded records with ``answer(kind, body, session_id) -> pending answer`` (``None``: not at all)."""

    context: ControlContext
    control: ControlClient
    answer: Answer
    seen: list[dict[str, Any]] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    def run(self) -> None:
        after = 0
        while not self.stop.is_set():
            records, head = self.context.journal.read(after, 100, wait=0.05)
            after = head if not records else records[-1]["seq"]
            for record in records:
                if record["kind"] not in FORWARDED_KINDS:
                    continue
                self.seen.append({"kind": record["kind"], "session_id": record["session_id"], **record["body"]})
                reply = self.answer(record["kind"], record["body"], record["session_id"])
                if reply is not None:
                    path = f"/v1/pending/{record['body']['pending_id']}/answer"
                    status = self.control.request("POST", path, reply)
                    assert status.status == 200, status.body
            time.sleep(0.01)


@pytest.fixture
def fake_runner(context: ControlContext, control_client: ControlClient) -> Iterator[Callable[[Answer], FakeRunner]]:
    started: list[tuple[FakeRunner, threading.Thread]] = []

    def start(answer: Answer) -> FakeRunner:
        runner = FakeRunner(context, control_client, answer)
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        started.append((runner, thread))
        return runner

    yield start
    for runner, thread in started:
        runner.stop.set()
        thread.join(5)
