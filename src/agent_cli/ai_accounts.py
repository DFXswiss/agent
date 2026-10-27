"""Device-local AI account, role, and session bindings for static executors.

Operators supply ``$AGENT_HOME/ai-accounts.json``. Installation creates no
accounts, roles, or selections. Credentials stay in each account's provider CLI
configuration directory; this manifest stores only directory paths. Static launch sites resolve these bindings before invoking a provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model_slots import (
    DEFAULT_MODEL_SLOTS,
    SUPPORTED_PROVIDERS,
    ModelSlots,
    ModelSlotsError,
    parse_model_slots,
)
from .store import StoreError

CONFIG_FILE = "ai-accounts.json"
TOP_LEVEL_KEYS = frozenset({"accounts", "roles", "sessions", "usage_session", "models", "lanes"})
# Provider CLI configuration-directory variable per supported provider.
PROVIDER_HOME_ENV = {
    "grok": "GROK_HOME",
    "codex": "CODEX_HOME",
    "claude": "CLAUDE_CONFIG_DIR",
}
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
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "GROK_HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
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


def _is_default_claude_dir(config_dir: str) -> bool:
    """True when ``config_dir`` is Claude Code's default profile directory ``~/.claude``."""
    try:
        return Path(config_dir).expanduser().resolve() == (Path.home() / ".claude").resolve()
    except OSError:
        return False


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
        only, then sets the provider's configuration-directory variable
        (``GROK_HOME``, ``CODEX_HOME`` or ``CLAUDE_CONFIG_DIR``) to this role's
        account ``config_dir``. Does not mutate ``os.environ``. This is process
        configuration isolation, not a sandbox or a claim that the provider CLI
        is already authenticated.
        """
        argv = ["env"]
        for key in _CLEAR_ENV:
            argv.extend(["-u", key])
        home_var = PROVIDER_HOME_ENV.get(self.account.provider)
        if home_var is None:
            raise AccountError(f"Unsupported provider for role {self.name}")
        if self.account.provider == "claude" and _is_default_claude_dir(self.account.config_dir):
            # Claude Code keeps the login of its default profile outside that
            # directory (``~/.claude.json`` and the keychain entry of the default
            # profile). Pointing CLAUDE_CONFIG_DIR at ``~/.claude`` would start an
            # empty, logged-out profile. The default profile is selected by
            # leaving the variable unset; any other directory is a named profile
            # the operator logged in with that variable set.
            return argv
        argv.append(f"{home_var}={self.account.config_dir}")
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
    usage_session: str | None = None
    # Which provider fills model slot ``first`` and ``second`` on this device.
    models: ModelSlots = DEFAULT_MODEL_SLOTS
    # Device-wide lane bindings (slot → role) used when a session binds none.
    lanes: dict[str, str] = field(default_factory=dict)

    def for_lane(self, session_id: str, role: str, vendor: str) -> AIRole:
        slot = f"{vendor}:{role}"
        binding = self.sessions.get(session_id)
        role_name = binding.lanes.get(slot) if binding is not None else None
        if role_name is None:
            role_name = self.lanes.get(slot)
        if role_name is None:
            if binding is None and not self.lanes:
                raise AccountError(f"No AI session configured for {session_id}")
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
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_KEYS:
        raise AccountError(
            f"{CONFIG_FILE} accepts only accounts, roles, sessions, usage_session, models, and lanes"
        )
    try:
        models = parse_model_slots(raw.get("models"))
    except ModelSlotsError as exc:
        raise AccountError(str(exc)) from exc
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

    device_lanes_raw = raw.get("lanes")
    if device_lanes_raw is None:
        device_lanes_raw = {}
    if not isinstance(device_lanes_raw, dict):
        raise AccountError("lanes must be an object or null")
    device_lanes: dict[str, str] = {}
    for slot, role_name in device_lanes_raw.items():
        _text(slot, "lane slot")
        bound = _text(role_name, "lane role")
        if bound not in roles:
            raise AccountError(f"Device lane {slot} references an unknown role")
        device_lanes[slot] = bound

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

    usage_session = raw.get("usage_session")
    if usage_session is not None:
        usage_session = _text(usage_session, "usage_session")
        if usage_session not in sessions or sessions[usage_session].interactive is None:
            raise AccountError("usage_session requires a configured interactive session")
        selected = roles[sessions[usage_session].interactive]
        if selected.account.provider != "grok":
            raise AccountError("usage_session currently supports the Grok billing adapter only")
    return AIAccounts(
        accounts,
        roles,
        sessions,
        usage_session,
        models,
        device_lanes,
    )


def load_model_slots(home: Path) -> ModelSlots:
    """The device's model slot configuration (historical order when unset).

    Checks the file is readable JSON with known top-level keys and parses the
    ``models`` block; errors inside ``accounts``, ``roles``, ``sessions`` or
    ``lanes`` do not stop task creation. Launches still validate the whole file.
    """
    path = home / CONFIG_FILE
    if not path.exists():
        return DEFAULT_MODEL_SLOTS
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_KEYS:
        raise AccountError(
            f"{CONFIG_FILE} accepts only accounts, roles, sessions, usage_session, models, and lanes"
        )
    try:
        return parse_model_slots(raw.get("models"))
    except ModelSlotsError as exc:
        raise AccountError(str(exc)) from exc
