"""Fixtures of the hook suites (GST-03): an HTTP client for ``/hooks/<Event>``, hook bodies, journal readers.

Builds on the guest fixtures of ``tests/platform/guest/conftest.py`` (control process, API server, session).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest.control import ControlContext
from agent_guest.control_routes import hooks as control_hooks
from agent_guest.hook_summary import HookSummaries
from agent_guest.limits import HookLimits
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN

OTHER_SESSION_ID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
OTHER_TOKEN = "b3RoZXItc2Vzc2lvbi10b2tlbi1mb3ItdGVzdHMtOTk5"


class HookReply:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body)


class HookClient:
    """POSTs hook bodies to the API port like Claude Code (bearer session token)."""

    def __init__(self, port: int, token: str | None = SESSION_TOKEN) -> None:
        self.port = port
        self.token = token

    def post(
        self,
        event: str,
        body: object = None,
        *,
        raw: bytes | None = None,
        token: str | None = "",
        headers: Mapping[str, str] | None = None,
    ) -> HookReply:
        sent = dict(headers or {})
        use = self.token if token == "" else token
        if use is not None:
            sent.setdefault("Authorization", f"Bearer {use}")
        data = raw if raw is not None else json.dumps(body).encode()
        sent.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", f"/hooks/{event}", body=data, headers=sent)
            response = connection.getresponse()
            return HookReply(response.status, response.read())
        finally:
            connection.close()


def hook_body(event: str, **fields: Any) -> dict[str, Any]:
    """A Claude Code hook body with the common fields."""
    return {
        "session_id": "5f0c6a3e-2b1d-4c8e-9a7f-1e2d3c4b5a69",
        "transcript_path": "/srv/sessions/x/home/.claude/projects/w/5f0c.jsonl",
        "cwd": "/srv/sessions/x/work",
        "permission_mode": "dontAsk",
        "hook_event_name": event,
        **fields,
    }


def tool_body(event: str, tool: str, tool_input: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return hook_body(event, tool_name=tool, tool_input=tool_input, **fields)


def records(context: ControlContext, kind: str | None = None) -> list[dict[str, Any]]:
    found, _ = context.journal.read(after=0, limit=1000)
    return [record for record in found if kind is None or record["kind"] == kind]


def bodies(context: ControlContext, kind: str) -> list[dict[str, Any]]:
    return [record["body"] for record in records(context, kind)]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = (tmp_path / "srv" / "sessions" / SESSION_ID / "work").resolve()
    path.mkdir(parents=True)
    (path / "notes.txt").write_text("n", encoding="utf-8")
    return path


@pytest.fixture
def ws_session(context: ControlContext, session: Session, workspace: Path) -> Session:
    """The fixture session with a real workspace directory in tmp."""
    moved = replace(session, workspace=str(workspace))
    context.state.save_session(moved)
    return moved


@pytest.fixture
def hook_client(api_port: int) -> HookClient:
    return HookClient(api_port)


@pytest.fixture
def fixtures_dir(contracts_dir: Path) -> Path:
    return contracts_dir / "fixtures" / "mcp" / "hooks"


@pytest.fixture(autouse=True)
def single_hook_records(request: pytest.FixtureRequest) -> None:
    """The hook suites check the record of every call; the summaries of routine calls are tested on their own
    (``tests/platform/guest/test_hook_summary.py``), so they are off here."""
    if "context" in request.fixturenames:
        context: ControlContext = request.getfixturevalue("context")
        context.journal.summaries = HookSummaries(HookLimits(summary_seconds=0))


@pytest.fixture
def no_hints() -> Iterator[None]:
    control_hooks.FILE_HINTS.take(SESSION_ID)
    yield
    control_hooks.FILE_HINTS.take(SESSION_ID)
