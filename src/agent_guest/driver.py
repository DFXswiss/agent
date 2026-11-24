"""Agent software behind one interface (ADR-021): ``AgentDriver`` with exactly one implementation, Claude Code.

The driver knows everything agent-specific about starting a session: the command line of 05-runner.md section 4.1,
the model part of the session environment (05 section 10.3, an allow-list; ``sandbox/unit.py`` adds the fixed
variables and nothing is inherited) and the agent version reported in the session record (``agent_kind``,
``agent_version``). Everything is pure: no files are read and no process is started here.

Interface:

* ``AgentDriver`` - protocol: ``kind``, ``version``, ``argv(launch)``, ``model_env(access, env)``
* ``Launch`` - the per-start input of ``argv`` (session, harness file, resume ID, model)

The command line never carries a prompt (HOT-09): Claude Code would start the turn at once, while its MCP servers
still connect in the background, so the turn would run without the platform tools. Every first prompt is delivered
into the TUI once the agent listed the tools (``delivery.py``).
* ``ClaudeCodeDriver(version, binary=..., mcp_config=...)`` - the only implementation
* ``DriverError`` - input the driver refuses (``request_invalid`` for the caller)
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from agent_guest.sandbox import unit

AGENT_KIND = "claude-code"
CLAUDE_BINARY = unit.CLAUDE_BINARY
MCP_CONFIG = "/opt/agent-guest/mcp.json"
TOOLS = "Read,Glob,Grep,Edit,Write"
DISALLOWED_TOOLS = "Bash,Agent,Task,WebFetch,WebSearch,NotebookEdit,Skill"
# 05 section 10.3: the model variables exist only for the platform access (the runner fills them from the catalog).
MODEL_ENV_ACCESS = "platform"
_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")


class DriverError(ValueError):
    """Input that cannot be turned into a safe command line or environment."""


@dataclass(frozen=True)
class Launch:
    """One start of the agent."""

    session_id: str
    harness_file: str
    resume_claude_session_id: str | None = None
    model_id: str | None = None


class AgentDriver(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def version(self) -> str: ...

    def argv(self, launch: Launch) -> list[str]: ...

    def model_env(self, access: str, env: Mapping[str, str]) -> dict[str, str]: ...


class ClaudeCodeDriver:
    """Claude Code 2.1.x as interactive TUI (ADR-025) behind both sandbox layers (ADR-008)."""

    def __init__(self, version: str, binary: str = CLAUDE_BINARY, mcp_config: str = MCP_CONFIG) -> None:
        self._version = version
        self.binary = binary
        self.mcp_config = mcp_config

    @property
    def kind(self) -> str:
        return AGENT_KIND

    @property
    def version(self) -> str:
        return self._version

    def argv(self, launch: Launch) -> list[str]:
        """The command line of 05 section 4.1, without a prompt (module docstring)."""
        if not _ID_RE.fullmatch(launch.session_id):
            raise DriverError("session id")
        args = [
            self.binary,
            "--restricted",
            "--strict-mcp-config",
            "--mcp-config",
            self.mcp_config,
            "--tools",
            TOOLS,
            "--disallowedTools",
            DISALLOWED_TOOLS,
            "--permission-mode",
            "dontAsk",
            "--disable-slash-commands",
            "--append-system-prompt-file",
            launch.harness_file,
        ]
        if launch.resume_claude_session_id is not None:
            if not _ID_RE.fullmatch(launch.resume_claude_session_id):
                raise DriverError("resume id")
            args += ["--resume", launch.resume_claude_session_id]
        if launch.model_id is not None:
            if not _MODEL_RE.fullmatch(launch.model_id):
                raise DriverError("model id")
            args += ["--model", launch.model_id]
        return args

    def model_env(self, access: str, env: Mapping[str, str]) -> dict[str, str]:
        """The runner's model variables, checked against the allow-list of 05 section 10.3."""
        if env and access != MODEL_ENV_ACCESS:
            raise DriverError("model env only for the platform access")
        unknown = sorted(set(env) - unit.MODEL_ENV_KEYS)
        if unknown:
            raise DriverError("model env not on the allow-list")
        return dict(env)


def driver_for(kind: str, version: str, *, binary: str = CLAUDE_BINARY, mcp_config: str = MCP_CONFIG) -> AgentDriver:
    """The driver of ``kind``; v1 knows only Claude Code (ADR-021)."""
    if kind != AGENT_KIND:
        raise DriverError(f"unknown agent kind {kind!r}")
    return ClaudeCodeDriver(version, binary=binary, mcp_config=mcp_config)
