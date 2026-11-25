"""U-GST-sessions: the agent driver (ADR-021): command line of 05-runner.md §4.1 and model environment of §10.3."""

from __future__ import annotations

import dataclasses

import pytest

from agent_guest.driver import (
    AGENT_KIND,
    AgentDriver,
    ClaudeCodeDriver,
    DriverError,
    Launch,
    driver_for,
)
from agent_guest.sandbox import unit

SID = "01a1ee56-5e00-7003-8f1b-340691d99f03"
HARNESS = f"/run/agent-guest/harness/{SID}.md"
RESUME = "5d0c9a3e-7f61-4b6e-9a42-3c1d2e4f5a6b"
BASE = [
    "/opt/claude/claude",
    "--restricted",
    "--strict-mcp-config",
    "--mcp-config",
    "/opt/agent-guest/mcp.json",
    "--tools",
    "Read,Glob,Grep,Edit,Write",
    "--disallowedTools",
    "Bash,Agent,Task,WebFetch,NotebookEdit,Skill,WebSearch",
    "--permission-mode",
    "dontAsk",
    "--disable-slash-commands",
    "--append-system-prompt-file",
    HARNESS,
]


@pytest.fixture
def driver() -> ClaudeCodeDriver:
    return ClaudeCodeDriver("2.1.330")


def test_minimal_command_line_is_exactly_the_specified_one(driver: ClaudeCodeDriver) -> None:
    assert driver.argv(Launch(session_id=SID, harness_file=HARNESS)) == BASE


@pytest.mark.parametrize("access", ["subscription", "api_key"])
def test_web_search_is_offered_only_to_anthropic_access(driver: ClaudeCodeDriver, access: str) -> None:
    """13-work-vm.md section 13.7: the managed settings allow WebSearch for every session of the VM; the command line
    offers it per start."""
    argv = driver.argv(Launch(SID, HARNESS, access=access))
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,WebSearch"
    assert argv[argv.index("--disallowedTools") + 1] == "Bash,Agent,Task,WebFetch,NotebookEdit,Skill"


@pytest.mark.parametrize("access", ["platform", "unknown"])
def test_web_search_is_disallowed_for_every_other_access(driver: ClaudeCodeDriver, access: str) -> None:
    assert driver.argv(Launch(SID, HARNESS, access=access)) == BASE


def test_resume_and_model_follow_in_order(driver: ClaudeCodeDriver) -> None:
    launch = Launch(SID, HARNESS, resume_claude_session_id=RESUME, model_id="claude-sonnet-5-5")
    assert driver.argv(launch) == [*BASE, "--resume", RESUME, "--model", "claude-sonnet-5-5"]


def test_the_command_line_never_carries_a_prompt() -> None:
    """HOT-09: a prompt on the command line starts the turn before the MCP servers are connected, so a launch has no
    field for one and the driver offers no way to put one there."""
    assert [item.name for item in dataclasses.fields(Launch)] == [
        "session_id",
        "harness_file",
        "resume_claude_session_id",
        "model_id",
        "access",
    ]
    assert not hasattr(ClaudeCodeDriver, "prompt_fits")
    assert "--" not in ClaudeCodeDriver("2.1.330").argv(Launch(SID, HARNESS, RESUME, "claude-sonnet-5-5"))


def test_custom_binary_and_mcp_config() -> None:
    driver = ClaudeCodeDriver("2.1.330", binary="/opt/agent-test/claude", mcp_config="/etc/agent-test/mcp.json")
    argv = driver.argv(Launch(SID, HARNESS))
    assert argv[0] == "/opt/agent-test/claude"
    assert argv[argv.index("--mcp-config") + 1] == "/etc/agent-test/mcp.json"


@pytest.mark.parametrize(
    "launch",
    [
        Launch("not-a-uuid", HARNESS),
        Launch(SID.upper(), HARNESS),
        Launch(SID, HARNESS, resume_claude_session_id="../x"),
        Launch(SID, HARNESS, model_id="-p"),
        Launch(SID, HARNESS, model_id="a b"),
    ],
)
def test_unsafe_launch_input_is_refused(driver: ClaudeCodeDriver, launch: Launch) -> None:
    with pytest.raises(DriverError):
        driver.argv(launch)


def test_model_env_is_the_allow_list_of_the_platform_access(driver: ClaudeCodeDriver) -> None:
    env = dict.fromkeys(unit.MODEL_ENV_KEYS, "m")
    assert driver.model_env("platform", env) == env
    assert driver.model_env("subscription", {}) == {}
    assert driver.model_env("api_key", {}) == {}
    with pytest.raises(DriverError):
        driver.model_env("subscription", {"ANTHROPIC_MODEL": "m"})
    with pytest.raises(DriverError):
        driver.model_env("platform", {"ANTHROPIC_BASE_URL": "http://evil"})


def test_kind_version_and_factory() -> None:
    driver: AgentDriver = driver_for(AGENT_KIND, "2.1.330", binary="/opt/claude/claude")
    assert (driver.kind, driver.version) == ("claude-code", "2.1.330")
    assert isinstance(driver, ClaudeCodeDriver)
    with pytest.raises(DriverError):
        driver_for("codex", "1.0")
