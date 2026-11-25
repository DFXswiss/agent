"""U-GST-profiles (KOR-51, ADR-090 section 3): the guest is the third check of a rights profile - the platform tools a
profile excludes are not offered (``--disallowedTools``), and an operation the profile does not allow is recorded as
locally rejected (``rights_denied``) when the API process hands it to the control process, so the runner never
executes it. Without a profile nothing changes."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent_guest import profiles, sessions
from agent_guest.control import ControlContext
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID
from tests.platform.guest.test_control import OPERATION

READ_ONLY = {"name": "read_only", "operations": ["github.get_issue", "workvm.request"], "reads": "all"}


def test_without_a_profile_nothing_is_refused_or_hidden() -> None:
    assert profiles.operations(None) is None
    assert profiles.disallowed_tools(None) == ()
    assert profiles.refusal(None, "github.push") is None


def test_the_tools_a_profile_does_not_offer() -> None:
    hidden = profiles.disallowed_tools(READ_ONLY)
    assert hidden
    assert all(name.startswith("mcp__platform__browser_") for name in hidden)
    everything = profiles.disallowed_tools({"name": "none", "operations": [], "reads": "task"})
    assert "mcp__platform__workvm_exec" in everything
    assert "mcp__platform__request_operation" not in everything
    assert "mcp__platform__get_task" not in everything


def test_refusal() -> None:
    assert profiles.refusal(READ_ONLY, "github.get_issue") is None
    refused = profiles.refusal(READ_ONLY, "github.push")
    assert refused is not None
    assert refused["code"] == "rights_denied"
    assert "read_only" in refused["message"]


def test_an_operation_outside_the_profile_is_recorded_rejected(
    api_client: object,
    control_client: object,
    context: ControlContext,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passed = {**OPERATION, "local_check": "passed"}
    api_client.pending_create(SESSION_ID, "operation", passed)  # type: ignore[attr-defined]
    monkeypatch.setattr(sessions.SessionManager, "profile", lambda _self, _session_id: READ_ONLY)
    pending = api_client.pending_create(SESSION_ID, "operation", passed)  # type: ignore[attr-defined]
    allowed = {**passed, "operation_id": "01a1ee56-5e00-7004-8f1b-340691d99f05", "type": "github.get_issue"}
    api_client.pending_create(SESSION_ID, "operation", allowed)  # type: ignore[attr-defined]
    records = control_client.request("GET", "/v1/journal").json()["records"]  # type: ignore[attr-defined]
    bodies = [record["body"] for record in records if record["kind"] == "operation.request"]
    assert bodies[0]["local_check"] == "passed"
    assert bodies[1]["pending_id"] == pending
    assert (bodies[1]["local_check"], bodies[1]["local_error"]["code"]) == ("rejected", "rights_denied")
    assert bodies[2]["local_check"] == "passed"
    assert context is not None


def test_the_stored_profile(tmp_path: Path, context: ControlContext, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = sessions.manager(context)
    monkeypatch.setattr(manager, "paths", dataclasses.replace(manager.paths, meta_dir=str(tmp_path)))
    assert manager.profile(SESSION_ID) is None
    manager._write_meta(SESSION_ID, {"profile": READ_ONLY})
    assert manager.profile(SESSION_ID) == READ_ONLY
    manager._write_meta(SESSION_ID, {"profile": "broken"})
    assert manager.profile(SESSION_ID) is None
