"""U-GST-logins (HOT-26): no resume and no restore brings an old login of Claude Code back.

A session was ended because its access was revoked ("Please run /login") and started again; it failed at once with
"401 OAuth access token has expired" although every subscription token at the hub was valid and renewed. The model
access of a session is the gateway's alone (``ANTHROPIC_AUTH_TOKEN`` is the session token); a login file of Claude
Code's own in the session's Claude directory would win over it. A suspended sandbox resumed in place and a session
restored from its checkpoint under its own ID keep ``home/.claude`` - so every create, resume and restore removes the
login file. Without ``forget_logins`` these tests fail: the expired login is still there when the agent starts."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.state import Session
from tests.platform.guest.conftest import ControlClient
from tests.platform.guest.sessions.conftest import RESUME, SESSION, request
from tests.platform.guest.sessions.test_contexts import CID, OBJECT, context_json, stage_raw, tar_of

EXPIRED = {"claudeAiOauth": {"accessToken": "expired-access", "refreshToken": "old-refresh", "expiresAt": 1}}


def claude_dir(manager: sessions.SessionManager) -> Path:
    return Path(manager.paths.session_dir(SESSION), "home", ".claude")


def plant_expired_login(manager: sessions.SessionManager) -> Path:
    """The login file an old start or the agent itself left in the sandbox."""
    login = claude_dir(manager) / ".credentials.json"
    login.write_text(json.dumps(EXPIRED))
    return login


def suspend(manager: sessions.SessionManager, context: ControlContext) -> None:
    manager.start(SESSION)
    manager.stop(SESSION, "suspended")
    session = context.state.session(SESSION)
    assert session is not None
    assert session.state == "suspended"


def resumed_request() -> dict[str, Any]:
    body = request(resume_claude_session_id=RESUME)
    body.pop("initial_prompt")
    return body


def test_a_resume_in_place_drops_an_expired_login(manager: sessions.SessionManager, context: ControlContext) -> None:
    manager.create(request(resume_claude_session_id=RESUME))
    suspend(manager, context)
    login = plant_expired_login(manager)
    manager.create(resumed_request())
    assert not login.exists()
    assert (claude_dir(manager) / ".claude.json").exists()  # the pre-seeded state stays
    manager.stop(SESSION)


def test_a_restore_from_the_checkpoint_drops_an_expired_login(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    assert stage_raw(control_client, tar_of(("context.json", context_json()))).status == 200
    restore = request(restore={"claude_session_id": CID, "object_id": OBJECT})
    restore.pop("initial_prompt")
    assert control_client.request("POST", "/v1/sessions", restore).status == 200
    manager.stop(SESSION)
    session = context.state.session(SESSION)
    assert session is not None
    context.state.save_session(Session(SESSION, session.uid, "0" * 64, "suspended", session.workspace, CID, "x"))
    login = plant_expired_login(manager)
    # the checkpoint of the same session comes back (restore under its own ID): its old directory is kept
    assert stage_raw(control_client, tar_of(("context.json", context_json(session_id=SESSION)))).status == 200
    reply = control_client.request("POST", "/v1/sessions", restore)
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"}), reply.body
    assert not login.exists()
    manager.stop(SESSION)


def test_forget_logins_handles_a_directory_a_link_and_nothing(tmp_path: Path) -> None:
    base = tmp_path / "s"
    sessions.forget_logins(str(base))  # no sandbox: nothing to do
    claude = base / "home" / ".claude"
    claude.mkdir(parents=True)
    sessions.forget_logins(str(base))  # no login file: nothing to do
    (claude / ".credentials.json").mkdir()
    (claude / ".credentials.json" / "x").write_text("x")
    sessions.forget_logins(str(base))
    assert not (claude / ".credentials.json").exists()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    os.symlink(outside, claude / ".credentials.json")
    sessions.forget_logins(str(base))
    assert not os.path.lexists(claude / ".credentials.json")
    assert outside.exists()  # a link is removed, never followed


def test_a_claude_directory_that_is_a_link_is_not_followed(tmp_path: Path) -> None:
    base = tmp_path / "s"
    (base / "home").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / ".credentials.json").write_text("{}")
    os.symlink(target, base / "home" / ".claude")
    with pytest.raises(OSError, match=r"symbolic links|Not a directory") as refused:
        sessions.forget_logins(str(base))
    assert refused.value.errno in (errno.ELOOP, errno.ENOTDIR)
    assert (target / ".credentials.json").exists()


def test_a_login_file_that_cannot_be_removed_fails_the_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = tmp_path / "s" / "home" / ".claude"
    claude.mkdir(parents=True)
    (claude / ".credentials.json").write_text("{}")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "unlink", refuse)
    with pytest.raises(PermissionError):
        sessions.forget_logins(str(tmp_path / "s"))
