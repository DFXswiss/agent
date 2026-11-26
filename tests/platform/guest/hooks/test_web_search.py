"""U-GST-policy rule 1b (13-work-vm.md section 13.7, decision KOR-44 point 11): Claude Code's own ``WebSearch`` is
allowed for a session whose model access is ``api_key`` or ``subscription`` and denied by rule 2 for the platform
access (and for an unknown access); ``WebFetch`` stays denied for every access. A session with the rights profile
``analysis`` (ADR-095 section 2) is denied it by rule 2 whatever its access."""

from __future__ import annotations

import pytest

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.control_routes import hooks as control_hooks
from agent_guest.policy import TOOL_FORBIDDEN, WEB_SEARCH_ACCESS, Decision, Policy
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID
from tests.platform.guest.hooks.conftest import HookClient, bodies, tool_body

SEARCH = {"query": "agent platform browser"}


def test_the_accesses_with_web_search() -> None:
    assert frozenset({"api_key", "subscription"}) == WEB_SEARCH_ACCESS


def test_policy_rule_1b() -> None:
    policy = Policy("/w", "/h.md")
    assert policy.evaluate("WebSearch", SEARCH, web_search=True) == Decision("allow", 1)
    denied = policy.evaluate("WebSearch", SEARCH)
    assert (denied.decision, denied.rule, denied.reason) == ("deny", 2, TOOL_FORBIDDEN.format(name="WebSearch"))
    # `web_search` never allows another tool
    assert policy.evaluate("WebFetch", {"url": "https://example.com/"}, web_search=True).rule == 2
    assert policy.evaluate("Bash", {}, web_search=True).rule == 2


@pytest.mark.parametrize(
    ("access", "expected"),
    [("subscription", ("allow", 1)), ("api_key", ("allow", 1)), ("platform", ("deny", 2)), (None, ("deny", 2))],
)
def test_the_session_access_decides(
    *,
    hook_client: HookClient,
    context: ControlContext,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    access: str | None,
    expected: tuple[str, int],
) -> None:
    decision, rule = expected
    asked: list[str] = []

    def session_access(ctx: ControlContext, session_id: str) -> str | None:
        asked.append(session_id)
        return access

    monkeypatch.setattr(control_hooks, "session_access", session_access)
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "WebSearch", SEARCH)).json()
    assert answer["hookSpecificOutput"]["permissionDecision"] == decision
    assert asked == [session.id]
    (record,) = bodies(context, "session.hook")
    assert (record["tool"], record["decision"], record["rule"]) == ("WebSearch", decision, rule)


def test_other_tools_do_not_look_up_the_access(
    hook_client: HookClient, context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def session_access(ctx: ControlContext, session_id: str) -> str | None:
        raise AssertionError("not needed")

    monkeypatch.setattr(control_hooks, "session_access", session_access)
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "WebFetch", {"url": "https://example.com/"}))
    assert answer.json()["hookSpecificOutput"]["permissionDecision"] == "deny"


ANALYSIS = {"name": "analysis", "operations": ["human.question"], "reads": "all"}


@pytest.mark.parametrize(
    ("profile", "expected"),
    [(None, ("allow", 1)), ({**ANALYSIS, "name": "read_only"}, ("allow", 1)), (ANALYSIS, ("deny", 2))],
)
def test_the_analysis_profile_withholds_web_search(
    *,
    hook_client: HookClient,
    context: ControlContext,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    profile: dict[str, object] | None,
    expected: tuple[str, int],
) -> None:
    decision, rule = expected
    monkeypatch.setattr(control_hooks, "session_access", lambda ctx, session_id: "subscription")
    monkeypatch.setattr(control_hooks, "session_profile", lambda ctx, session_id: profile)
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "WebSearch", SEARCH)).json()
    assert answer["hookSpecificOutput"]["permissionDecision"] == decision
    (record,) = bodies(context, "session.hook")
    assert (record["tool"], record["decision"], record["rule"]) == ("WebSearch", decision, rule)
    if decision == "deny":
        assert record["reason"] == TOOL_FORBIDDEN.format(name="WebSearch")
        assert answer["hookSpecificOutput"]["permissionDecisionReason"] == record["reason"]


def test_the_profile_is_read_from_the_session_meta(context: ControlContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions.SessionManager, "profile", lambda _self, session_id: ANALYSIS)
    assert control_hooks.session_profile(context, SESSION_ID) == ANALYSIS
