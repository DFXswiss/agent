"""U-GST-hook-summary end to end: hook calls over the API port, the summaries of the control process (KOR-35).

Every producer of routine hook records goes through the journal of the control process: ``PreToolUse`` allow
(``control_routes/hooks.py``), ``PostToolUse`` none (``hooks.py``); a deny, ``UserPromptSubmit`` (``prompts.py``) and
``Stop`` stay single and come after the summary of the calls before them.
"""

from __future__ import annotations

from agent_guest.clock import iso_ms
from agent_guest.control import ControlContext
from agent_guest.hook_summary import HookSummaries
from agent_guest.state import Session
from tests.platform.guest.conftest import START, ControlClient, FakeClock

from .conftest import HookClient, bodies, hook_body, tool_body


def test_routine_calls_are_summarised_and_the_rest_keeps_its_place(
    hook_client: HookClient, context: ControlContext, ws_session: Session, clock: FakeClock
) -> None:
    context.journal.summaries = HookSummaries()  # on, as in production
    read = tool_body("PreToolUse", "Read", {"file_path": "notes.txt"})
    for _ in range(3):
        assert hook_client.post("PreToolUse", read).json()["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert (
            hook_client.post("PostToolUse", tool_body("PostToolUse", "Read", {"file_path": "notes.txt"})).json() == {}
        )
    assert bodies(context, "session.hook") == []
    clock.advance(1)
    denied = hook_client.post("PreToolUse", tool_body("PreToolUse", "Read", {"file_path": "/etc/passwd"})).json()
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    hook_client.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"}))
    reply = ControlClient(context.config.control_socket).request(
        "POST",
        f"/v1/sessions/{ws_session.id}/prompts",
        {"prompt_id": "01a1ee56-5e00-7003-8f1b-340691d99f10", "text": "next"},
    )
    assert reply.status == 200
    hook_client.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="next"))
    hook_client.post("Stop", hook_body("Stop", stop_hook_active=False))
    seen = bodies(context, "session.hook")
    assert [(body["event"], body["decision"]) for body in seen] == [
        ("summary", "allow"),
        ("PreToolUse", "deny"),
        ("summary", "allow"),
        ("UserPromptSubmit", "allow"),
        ("Stop", "none"),
    ]
    assert seen[0] == {
        "event": "summary",
        "decision": "allow",
        "counts": {"PreToolUse": 3, "PostToolUse": 3},
        "tools": {"Read": 3},
        "from": iso_ms(START),
        "to": iso_ms(START),
    }
    assert seen[2]["counts"] == {"PostToolUse": 1}
    assert seen[2]["tools"] == {}
