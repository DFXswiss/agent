"""Device-local AI account, role, and session bindings for static executors.

Operators supply ``$AGENT_HOME/ai-accounts.json``. Installation creates no
accounts, roles, or selections. Credentials stay in each account's provider CLI
configuration directory; this manifest stores only directory paths. Static
launch sites resolve these bindings before invoking a provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lane import LANE_STRIP_ENV
from .model_slots import (
    SUPPORTED_PROVIDERS,
    ModelSlots,
    ModelSlotsError,
    parse_implementer,
    parse_model_slots,
)
from .store import StoreError

CONFIG_FILE = "ai-accounts.json"
TOP_LEVEL_KEYS = frozenset(
    {
        "accounts",
        "roles",
        "sessions",
        "usage_session",
        "models",
        "implementer",
        "lanes",
        "lane_execution",
    }
)
LANE_EXECUTION_ERROR = (
    "lane_execution is not configured: lanes never run on a work device; "
    "start lanes through the local harness's lane starter on an external lane host, "
    "or mark this device as a lane host"
)
REMOTE_LANE_EXECUTION_ERROR = (
    f'{LANE_EXECUTION_ERROR}; lane_execution mode "remote" is not supported'
)
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
    *LANE_STRIP_ENV,
    "GROK_HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)


class AccountError(StoreError):
    """Configuration does not authorize this AI account or role selection."""


def _model_configuration(raw: dict[str, Any]) -> tuple[ModelSlots, str]:
    """Translate model-selection errors into the configuration error channel."""
    try:
        models = parse_model_slots(raw.get("models"))
        return models, parse_implementer(raw.get("implementer"), models)
    except ModelSlotsError as exc:
        raise AccountError(str(exc)) from exc


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
class LaneExecution:
    mode: str


@dataclass(frozen=True)
class AIAccounts:
    accounts: dict[str, AIAccount]
    roles: dict[str, AIRole]
    sessions: dict[str, AISessionBinding]
    # Explicit provider assignment for model slots ``first`` and ``second``.
    _models: ModelSlots | None
    # Effective implementation provider; absent configuration resolves to models.first.
    _implementer: str | None
    usage_session: str | None = None
    # Device-wide lane bindings (slot → role) used when a session binds none.
    lanes: dict[str, str] = field(default_factory=dict)
    # Explicit declaration that prevents a work device from running model lanes.
    lane_execution: LaneExecution | None = None
    lane_execution_error: str = LANE_EXECUTION_ERROR
    model_configuration_error: str | None = None

    def require_model_configuration(self) -> tuple[ModelSlots, str]:
        """Return the explicit slot selection or its deferred configuration error."""
        if self._models is None or self._implementer is None:
            raise AccountError(
                self.model_configuration_error
                or "models configuration is invalid"
            )
        return self._models, self._implementer

    @property
    def models(self) -> ModelSlots:
        return self.require_model_configuration()[0]

    @property
    def implementer(self) -> str:
        return self.require_model_configuration()[1]

    def require_lane_execution(self) -> LaneExecution:
        if self.lane_execution is None:
            raise AccountError(self.lane_execution_error)
        return self.lane_execution

    def for_lane(
        self,
        session_id: str,
        role: str,
        vendor: str,
        *,
        require_execution: bool = True,
    ) -> AIRole:
        """Resolve a lane binding before optionally checking its execution host.

        Launchers with additional established preconditions defer the execution
        check and call :meth:`require_lane_execution` at their start boundary.
        """
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
        if require_execution:
            self.require_lane_execution()
        return selected

    def for_session(self, session_id: str) -> AIRole:
        binding = self.sessions.get(session_id)
        if binding is None:
            raise AccountError(f"No AI session configured for {session_id}")
        if binding.interactive is None:
            raise AccountError(f"No interactive AI role configured for session {session_id}")
        return self.roles[binding.interactive]


def load_ai_accounts(
    home: Path, *, defer_model_configuration: bool = False
) -> AIAccounts:
    path = home / CONFIG_FILE
    if not path.exists():
        # Device configuration has no built-in model assignment.
        model_configuration_error: str | None = None
        try:
            _model_configuration({})
        except AccountError as exc:
            if not defer_model_configuration:
                raise
            model_configuration_error = str(exc)
        return AIAccounts(
            {},
            {},
            {},
            None,
            None,
            model_configuration_error=model_configuration_error,
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_KEYS:
        raise AccountError(
            f"{CONFIG_FILE} accepts only accounts, roles, sessions, usage_session, "
            "models, implementer, lanes, and lane_execution"
        )
    lane_execution_raw = raw.get("lane_execution")
    lane_execution = _parse_lane_execution(lane_execution_raw)
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
    models: ModelSlots | None = None
    implementer: str | None = None
    model_configuration_error: str | None = None
    try:
        # New model checks follow the manifest's established account, role, and
        # session preconditions. Interactive launchers may defer this check
        # until their older launch preconditions have run.
        models, implementer = _model_configuration(raw)
    except AccountError as exc:
        if not defer_model_configuration:
            raise
        model_configuration_error = str(exc)
    return AIAccounts(
        accounts=accounts,
        roles=roles,
        sessions=sessions,
        usage_session=usage_session,
        _models=models,
        lanes=device_lanes,
        _implementer=implementer,
        lane_execution=lane_execution,
        lane_execution_error=_lane_execution_error(lane_execution_raw),
        model_configuration_error=model_configuration_error,
    )


def _parse_lane_execution(value: Any) -> LaneExecution | None:
    """Parse the harness declaration; invalid or absent values stay fail-closed.

    The full manifest also serves read-only model/account lookup paths, so this
    parser deliberately records an invalid declaration as unconfigured.  A
    launch calls ``require_lane_execution`` and emits the single public error.
    """
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    if mode == "this-device-is-lane-host" and set(value) == {"mode"}:
        return LaneExecution(mode=mode)
    return None


def _lane_execution_error(value: Any) -> str:
    if isinstance(value, dict) and value.get("mode") == "remote":
        return REMOTE_LANE_EXECUTION_ERROR
    return LANE_EXECUTION_ERROR


def load_lane_execution(home: Path) -> LaneExecution:
    """Load the explicit declaration that this device is a lane host."""
    path = home / CONFIG_FILE
    if not path.exists():
        raise AccountError(LANE_EXECUTION_ERROR)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict):
        raise AccountError(LANE_EXECUTION_ERROR)
    value = raw.get("lane_execution")
    execution = _parse_lane_execution(value)
    if execution is None:
        raise AccountError(_lane_execution_error(value))
    return execution


def load_model_configuration(home: Path) -> tuple[ModelSlots, str]:
    """The device's review slots and effective implementer selection."""
    path = home / CONFIG_FILE
    if not path.exists():
        # Use the central fail-closed error for an unconfigured device.
        return _model_configuration({})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_KEYS:
        raise AccountError(
            f"{CONFIG_FILE} accepts only accounts, roles, sessions, usage_session, "
            "models, implementer, lanes, and lane_execution"
        )
    # Device-configuration read: task creation never supplies a built-in assignment.
    return _model_configuration(raw)


def load_model_slots(home: Path) -> ModelSlots:
    """The device's explicitly configured model slots.

    Checks the file is readable JSON with known top-level keys and validates the
    ``models`` and ``implementer`` selections; errors inside ``accounts``,
    ``roles``, ``sessions``, ``lanes`` or ``lane_execution`` do not stop task
    creation. Launches still validate the whole file.
    """
    return load_model_configuration(home)[0]
