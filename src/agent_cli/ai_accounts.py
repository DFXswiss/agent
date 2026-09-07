"""Device-local AI account, role, and session bindings for static executors.

Operators supply ``$AGENT_HOME/ai-accounts.json``. Installation creates no
accounts, roles, or selections. Credentials stay in each account's provider CLI
configuration directory; this manifest stores only directory paths. Launch-site
integration is separate from this loader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import StoreError

CONFIG_FILE = "ai-accounts.json"
SUPPORTED_PROVIDERS = frozenset({"grok", "codex"})
ACCESS_VALUES = frozenset({"read-only", "workspace-write"})
# Fixed workflow kinds that must resolve to read-only access. These are protocol
# capabilities, not installed user-role names.
READ_ONLY_WORKFLOW_KINDS = frozenset(
    {"reviewer", "pr-reviewer-quality", "pr-reviewer-logic"}
)
_CLEAR_ENV = (
    "XAI_API_KEY",
    "GROK_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "GROK_HOME",
    "CODEX_HOME",
)


class AccountError(StoreError):
    """Configuration does not authorize this AI account or role selection."""


def _text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise AccountError(f"{field} must be a non-empty single-line string")
    return value


def _config_dir(value: Any, field: str) -> str:
    text = _text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise AccountError(f"{field} must be an absolute path")
    if ".." in path.parts:
        raise AccountError(f"{field} must not contain parent traversal")
    return text


@dataclass(frozen=True)
class AIAccount:
    name: str
    provider: str
    config_dir: str


@dataclass(frozen=True)
class AIRole:
    name: str
    account: AIAccount
    model: str
    access: str

    def env_prefix(self) -> list[str]:
        """Return an ``env`` argv prefix that isolates provider configuration.

        Clears ambient provider tokens and home variables for the child process
        only, then sets ``GROK_HOME`` or ``CODEX_HOME`` to this role's account
        ``config_dir``. Does not mutate ``os.environ``. This is process
        configuration isolation, not a sandbox or a claim that the provider CLI
        is already authenticated.
        """
        argv = ["env"]
        for key in _CLEAR_ENV:
            argv.extend(["-u", key])
        if self.account.provider == "grok":
            argv.append(f"GROK_HOME={self.account.config_dir}")
        elif self.account.provider == "codex":
            argv.append(f"CODEX_HOME={self.account.config_dir}")
        else:
            raise AccountError(f"Unsupported provider for role {self.name}")
        return argv


@dataclass(frozen=True)
class AISessionBinding:
    interactive: str | None
    lanes: dict[str, str]


@dataclass(frozen=True)
class AIAccounts:
    accounts: dict[str, AIAccount]
    roles: dict[str, AIRole]
    sessions: dict[str, AISessionBinding]

    def for_lane(self, session_id: str, role: str, vendor: str) -> AIRole:
        binding = self.sessions.get(session_id)
        if binding is None:
            raise AccountError(f"No AI session configured for {session_id}")
        slot = f"{vendor}:{role}"
        role_name = binding.lanes.get(slot)
        if role_name is None:
            raise AccountError(f"No AI lane configured for session {session_id} slot {slot}")
        selected = self.roles[role_name]
        if selected.account.provider != vendor:
            raise AccountError(
                f"Lane slot {slot} provider does not match configured account provider"
            )
        if role in READ_ONLY_WORKFLOW_KINDS and selected.access != "read-only":
            raise AccountError(
                f"Workflow kind {role} requires read-only access; refusing writable binding"
            )
        return selected

    def for_session(self, session_id: str) -> AIRole:
        binding = self.sessions.get(session_id)
        if binding is None:
            raise AccountError(f"No AI session configured for {session_id}")
        if binding.interactive is None:
            raise AccountError(f"No interactive AI role configured for session {session_id}")
        return self.roles[binding.interactive]


def load_ai_accounts(home: Path) -> AIAccounts:
    path = home / CONFIG_FILE
    if not path.exists():
        return AIAccounts({}, {}, {})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - {"accounts", "roles", "sessions"}:
        raise AccountError(f"{CONFIG_FILE} accepts only accounts, roles, and sessions")
    accounts_raw = raw.get("accounts")
    roles_raw = raw.get("roles")
    sessions_raw = raw.get("sessions")
    if accounts_raw is None:
        accounts_raw = {}
    if roles_raw is None:
        roles_raw = {}
    if sessions_raw is None:
        sessions_raw = {}
    if (
        not isinstance(accounts_raw, dict)
        or not isinstance(roles_raw, dict)
        or not isinstance(sessions_raw, dict)
    ):
        raise AccountError("accounts, roles, and sessions must be objects or null")

    accounts: dict[str, AIAccount] = {}
    for name, entry in accounts_raw.items():
        _text(name, "account name")
        if not isinstance(entry, dict) or set(entry) - {"provider", "config_dir"}:
            raise AccountError(f"Invalid account fields for {name}")
        provider = _text(entry.get("provider"), "provider")
        if provider not in SUPPORTED_PROVIDERS:
            raise AccountError(f"Unsupported provider for account {name}")
        config_dir = _config_dir(entry.get("config_dir"), "config_dir")
        accounts[name] = AIAccount(name, provider, config_dir)

    roles: dict[str, AIRole] = {}
    for name, entry in roles_raw.items():
        _text(name, "role name")
        if not isinstance(entry, dict) or set(entry) - {"account", "model", "access"}:
            raise AccountError(f"Invalid role fields for {name}")
        account_name = _text(entry.get("account"), "role account")
        if account_name not in accounts:
            raise AccountError(f"Role {name} references an unknown account")
        model = _text(entry.get("model"), "model")
        access = _text(entry.get("access"), "access")
        if access not in ACCESS_VALUES:
            raise AccountError(f"Invalid access for role {name}")
        roles[name] = AIRole(name, accounts[account_name], model, access)

    sessions: dict[str, AISessionBinding] = {}
    for session_id, entry in sessions_raw.items():
        _text(session_id, "session id")
        if not isinstance(entry, dict) or set(entry) - {"interactive", "lanes"}:
            raise AccountError(f"Invalid session fields for {session_id}")
        interactive_raw = entry.get("interactive")
        if interactive_raw is None:
            interactive: str | None = None
        else:
            interactive = _text(interactive_raw, "session interactive")
            if interactive not in roles:
                raise AccountError(
                    f"Session {session_id} interactive references an unknown role"
                )
        lanes_raw = entry.get("lanes")
        if lanes_raw is None:
            lanes_raw = {}
        if not isinstance(lanes_raw, dict):
            raise AccountError(f"Session {session_id} lanes must be an object or null")
        lanes: dict[str, str] = {}
        for slot, role_name in lanes_raw.items():
            _text(slot, "lane slot")
            bound = _text(role_name, "lane role")
            if bound not in roles:
                raise AccountError(
                    f"Session {session_id} lane {slot} references an unknown role"
                )
            lanes[slot] = bound
        sessions[session_id] = AISessionBinding(interactive, lanes)

    return AIAccounts(accounts, roles, sessions)
