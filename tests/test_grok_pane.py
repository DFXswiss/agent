from __future__ import annotations

from agent_cli.grok_pane import (
    grok_pane_is_idle,
    grok_pane_is_working,
    grok_permission_prompt,
)

THINKING = """
  /agent-home/sessions/worker                                        15K / 500K
     \u276f da ist Post id f82bd22b
    \u280b Thinking\u2026 4.3s                                         16s \u21e3 15.1k [stop]
  Help improve Grok                                         [Opt out] [Opt in]
  Shift+Tab:mode  \u2502  Esc:cancel  \u2502  Ctrl+x:shortcuts
"""

PREPARING = """
    \u280b Preparing list_dir (4)\u2026 0.6s                          4.1s \u21e3 17.1k [stop]
  Shift+Tab:mode  \u2502  Esc:cancel  \u2502  Ctrl+x:shortcuts
"""

IDLE = """
     Worked for 1.8s
  \u2502 \u276f                                                                        \u2502
  Shift+Tab:mode  \u2502  Ctrl+x:shortcuts
"""


def test_thinking_is_working() -> None:
    assert grok_pane_is_working(THINKING) is True


def test_preparing_tool_is_working() -> None:
    assert grok_pane_is_working(PREPARING) is True


def test_waiting_for_response_is_working() -> None:
    pane = "    Waiting for response\u2026 1.2s                           27m43s [stop]\n"
    assert grok_pane_is_working(pane) is True


def test_idle_prompt_is_not_working() -> None:
    assert grok_pane_is_working(IDLE) is False


def test_permission_prompt_is_working() -> None:
    pane = (
        "  1 (\u25cf) Yes, and don't ask again for anything (always-approve mode)\n"
        "  1/3:select  \u2502  Tab:next option\n"
    )
    assert grok_pane_is_working(pane) is False
    assert grok_permission_prompt(pane) is True


def test_design_quote_is_not_a_permission_prompt() -> None:
    pane = (
        "A Grok tool-approval modal (`1/3:select` → Enter) is still cleared.\n"
        "  │ ❯                                                                        │\n"
        "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
    )
    assert grok_permission_prompt(pane) is False
    assert grok_pane_is_working(pane) is False


def test_always_approve_footer_is_not_working() -> None:
    pane = (
        "     Worked for 1.8s\n"
        "  \u2502 \u276f                                                                        \u2502\n"
        "  \u2500 Grok 4.6 (high) \u00b7 always-approve \u2500\n"
        "  Shift+Tab:mode  \u2502  Ctrl+x:shortcuts\n"
    )
    assert grok_pane_is_working(pane) is False
    assert grok_permission_prompt(pane) is False


def test_empty_is_not_working() -> None:
    assert grok_pane_is_working("") is False


def test_ansi_thinking_is_working() -> None:
    pane = "\x1b[32m  Thinking...\x1b[0m  2.0s   [stop]\n"
    assert grok_pane_is_working(pane) is True
