"""U-GST-policy rule 1a (KOR-39, ADR-076 section 3; 05-runner.md section 5.3): ``AskUserQuestion`` of a main session
becomes the operation ``human.question`` of the session and is denied with the fixed sentence; a subagent's call is
denied by rule 2 as before. The question text is built deterministically from the tool input."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.control import ControlContext
from agent_guest.control_routes import hooks as control_hooks
from agent_guest.journal import JournalError
from agent_guest.policy import QUESTION_MAX, REASONS, TOOL_FORBIDDEN, Policy, question_text
from agent_guest.schema import SchemaSet
from agent_guest.state import Session
from tests.platform.guest.conftest import make_session
from tests.platform.guest.hooks.conftest import HookClient, bodies, tool_body

PASSED = (
    "Your question was passed to the person through agent.dfx.swiss. End your turn now; the answer arrives as your "
    "next prompt."
)
ASK_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": "Which database should the service use?",
            "header": "Database",
            "options": [
                {"label": "PostgreSQL", "description": "the platform default"},
                {"label": "SQLite"},
            ],
            "multiSelect": False,
        },
        {"question": "Deploy now?", "options": [{"label": "Yes"}, {"label": "No"}]},
    ]
}
ASK_TEXT = (
    "Which database should the service use?\n- PostgreSQL: the platform default\n- SQLite\n\nDeploy now?\n- Yes\n- No"
)


# ---------------------------------------------------------------- question text


def test_the_question_lists_every_question_with_its_options() -> None:
    assert question_text(ASK_INPUT) == ASK_TEXT


@pytest.mark.parametrize(
    ("tool_input", "expected"),
    [
        ({}, ""),
        ({"questions": "Why?"}, ""),
        ({"questions": ["Why?", 5, {"question": ""}, {"question": "  "}, {"question": 3}]}, ""),
        ({"questions": [{"question": "Why?"}]}, "Why?"),
        ({"questions": [{"question": "Why?", "options": "a, b"}]}, "Why?"),
        (
            {
                "questions": [
                    {
                        "question": "Pick",
                        "options": [
                            "plain",
                            " ",
                            7,
                            {"label": "", "description": "only a description"},
                            {"label": "x", "description": ""},
                            {},
                        ],
                    }
                ]
            },
            "Pick\n- plain\n- only a description\n- x",
        ),
        ({"questions": [{"question": "a\x00b"}]}, "ab"),
    ],
)
def test_the_question_is_arranged_formally(tool_input: dict[str, Any], expected: str) -> None:
    assert question_text(tool_input) == expected


def test_the_question_is_cut_to_the_operation_limit() -> None:
    text = question_text({"questions": [{"question": "q" * 5000}]})
    assert (len(text), QUESTION_MAX) == (4000, 4000)


def test_the_question_passes_the_operation_contract(schemas: SchemaSet) -> None:
    assert schemas.errors("ops/human.question.params.json", {"question": ASK_TEXT}) == []


# ---------------------------------------------------------------- policy


def test_policy_rule_1a() -> None:
    policy = Policy("/w", "/h.md")
    decision = policy.evaluate("AskUserQuestion", {}, ask=True)
    assert (decision.decision, decision.rule, decision.reason) == ("deny", 1, PASSED)
    assert REASONS["question_passed"] == PASSED
    decision = policy.evaluate("AskUserQuestion", {})
    assert (decision.decision, decision.rule, decision.reason) == (
        "deny",
        2,
        TOOL_FORBIDDEN.format(name="AskUserQuestion"),
    )
    # `ask` never changes the decision for another tool
    assert policy.evaluate("Bash", {}, ask=True).rule == 2
    assert policy.evaluate("mcp__platform__get_context", {}, ask=True).rule == 1


# ---------------------------------------------------------------- end to end


@pytest.fixture
def depth(monkeypatch: pytest.MonkeyPatch) -> dict[str, int | None]:
    found: dict[str, int | None] = {"depth": 0}
    monkeypatch.setattr(control_hooks, "session_depth", lambda context, session_id: found["depth"])
    return found


def test_a_main_session_question_becomes_an_operation(
    hook_client: HookClient, context: ControlContext, session: Session, depth: dict[str, int | None]
) -> None:
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "AskUserQuestion", ASK_INPUT)).json()
    assert answer == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": PASSED,
        }
    }
    (request,) = bodies(context, "operation.request")
    assert request["type"] == "human.question"
    assert request["params"] == {"question": ASK_TEXT}
    assert request["local_check"] == "passed"
    assert {"operation_id", "pending_id", "requested_at"} <= set(request)
    assert "idempotency_key" not in request
    assert bodies(context, "session.hook") == [
        {"event": "PreToolUse", "tool": "AskUserQuestion", "decision": "deny", "rule": 1, "reason": PASSED}
    ]
    # the pending call exists like that of an MCP request_operation; nobody waits for it
    assert context.pending.wait(request["pending_id"], 0, session.id) is None


@pytest.mark.parametrize("value", [1, 2, None])
def test_a_subagent_or_unknown_session_is_denied_by_rule_2(
    hook_client: HookClient, context: ControlContext, session: Session, depth: dict[str, int | None], value: int | None
) -> None:
    depth["depth"] = value
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "AskUserQuestion", ASK_INPUT)).json()
    reason = TOOL_FORBIDDEN.format(name="AskUserQuestion")
    assert answer["hookSpecificOutput"]["permissionDecisionReason"] == reason
    assert bodies(context, "operation.request") == []
    assert bodies(context, "session.hook") == [
        {"event": "PreToolUse", "tool": "AskUserQuestion", "decision": "deny", "rule": 2, "reason": reason}
    ]


def test_a_call_without_question_text_is_denied_by_rule_2(
    hook_client: HookClient, context: ControlContext, session: Session, depth: dict[str, int | None]
) -> None:
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "AskUserQuestion", {"questions": []})).json()
    assert answer["hookSpecificOutput"]["permissionDecisionReason"] == TOOL_FORBIDDEN.format(name="AskUserQuestion")
    assert bodies(context, "operation.request") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_name": "Read", "question": "Why?"},
        {"tool_name": "AskUserQuestion", "question": ""},
        {"tool_name": "AskUserQuestion", "question": "q" * 4001},
        {"tool_name": "AskUserQuestion", "question": 5},
        {"tool_name": "AskUserQuestion", "question": "Why?", "other": 1},
    ],
)
def test_the_control_op_refuses_a_bad_question(api_client: ApiClient, session: Session, extra: dict[str, Any]) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call("hook.pre_tool_use", {"session_id": session.id, "tool_input": {}, **extra})
    assert error.value.code == "request_invalid"


@pytest.mark.parametrize(("code", "expected"), [("unavailable", "unavailable"), ("schema", "request_invalid")])
def test_a_journal_failure_of_the_operation_request(monkeypatch: pytest.MonkeyPatch, code: str, expected: str) -> None:
    session = make_session()

    def create(session_id: str, kind: str, body: dict[str, Any]) -> str:
        raise JournalError(code)

    context = SimpleNamespace(
        state=SimpleNamespace(session=lambda session_id: session),
        journal=SimpleNamespace(clock=SimpleNamespace(now=lambda: 1_790_000_000.0)),
        pending=SimpleNamespace(create=create),
    )
    monkeypatch.setattr(control_hooks, "session_depth", lambda context, session_id: 0)
    with pytest.raises(JournalError) as error:
        control_hooks.pre_tool_use(
            cast(ControlContext, context),
            {"session_id": session.id, "tool_name": "AskUserQuestion", "tool_input": {}, "question": "Why?"},
        )
    assert error.value.code == expected
