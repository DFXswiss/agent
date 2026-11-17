"""Device-local AI account, role, and session bindings for static executors.

Operators supply ``$AGENT_HOME/ai-accounts.json`` and, next to it,
``$AGENT_HOME/drivers.json``, which describes every driver the accounts,
models, implementer, lanes and usage selections name. Installation creates no
accounts, roles, selections or drivers. Credentials stay in each account's
driver configuration directory; this manifest stores only directory paths.
Static launch sites resolve these bindings before invoking a driver. A role
that names an account pool (``accounts``) is resolved to one member only by a
coordinator lane start (see ``account_pool``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .drivers import (
    DRIVERS_DOC,
    DRIVERS_FILE,
    DRIVERS_HINT,
    DRIVERS_NOT_CONFIGURED,
    NO_DRIVER_CONFIGURATION,
    PANE_UNCLASSIFIED,
    DriverConfigError,
    DriverConfiguration,
    DriverSpec,
    InteractiveSpec,
    PaneMarkers,
    RecordedNames,
    UsageSpec,
    declared_driver_ids,
    driver_refusal,
    interactive_driver_ids,
    is_home_default_dir,
    lane_driver_ids,
    parse_driver_document,
    parse_recorded_slots,
    require_interactive,
    role_clear_env,
)
from .model_slots import (
    ModelSlots,
    ModelSlotsError,
    parse_implementer,
    parse_model_slots,
)
from .store import StoreError

CONFIG_FILE = "ai-accounts.json"
# Optional device-local inventory of known AI subscriptions (no logins needed).
SUBSCRIPTIONS_FILE = "ai-subscriptions.json"
_SUBSCRIPTION_PROVIDER = re.compile(r"[a-z0-9-]+")
_SUBSCRIPTION_FIELDS = frozenset({"provider", "account", "label", "status", "note"})
TOP_LEVEL_KEYS = frozenset(
    {
        "accounts",
        "roles",
        "sessions",
        "usage_session",
        "usage_accounts",
        "models",
        "implementer",
        "lanes",
        "lane_execution",
        "token_keepalive",
    }
)
# Accepted range of token_keepalive.refresh_before_minutes.
KEEPALIVE_MIN_MINUTES = 5
KEEPALIVE_MAX_MINUTES = 480
LANE_EXECUTION_ERROR = (
    "lane_execution is not configured: lanes never run on a work device; "
    "start lanes through the local harness's lane starter on an external lane host, "
    "or mark this device as a lane host"
)
REMOTE_LANE_EXECUTION_ERROR = (
    f'{LANE_EXECUTION_ERROR}; lane_execution mode "remote" is not supported'
)
ACCESS_VALUES = frozenset({"read-only", "workspace-write"})
# A role names exactly one of ``account`` and ``accounts`` (an account pool).
ROLE_KEYS = frozenset({"account", "accounts", "model", "access"})
# Refusal of a pool role on any path other than a coordinator lane start.
POOL_ROLE_NEEDS_COORDINATOR = (
    "Role {role} names an account pool; a pool account is chosen only when a "
    "coordinator lane starts"
)
# Fixed workflow kinds that must resolve to read-only access. These are protocol
# capabilities, not installed user-role names.
READ_ONLY_WORKFLOW_KINDS = frozenset(
    {"check", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic"}
)


class AccountError(StoreError):
    """Configuration does not authorize this AI account or role selection."""


def _read_driver_document(home: Path) -> dict[str, Any] | None:
    """The ``drivers.json`` object, or ``None`` only when the file is absent.

    A file that exists must hold an object; any other document, ``null``
    included, fails instead of reading as an absent file.
    """
    path = home / DRIVERS_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {DRIVERS_FILE} ({DRIVERS_DOC})") from exc
    if not isinstance(raw, dict):
        raise AccountError(f"{DRIVERS_FILE} must be an object ({DRIVERS_HINT})")
    return raw


def load_driver_configuration(home: Path) -> DriverConfiguration:
    """Read and strictly validate ``drivers.json``; absent means no driver."""
    raw = _read_driver_document(home)
    if raw is None:
        return NO_DRIVER_CONFIGURATION
    try:
        return parse_driver_document(raw)
    except DriverConfigError as exc:
        raise AccountError(f"{exc} ({DRIVERS_HINT})") from exc


def load_recorded_names(home: Path) -> RecordedNames | None:
    """Read only ``recorded_slots`` of ``drivers.json``; absent means none.

    Stored names must stay readable whatever else the document holds, so no
    other key is validated here; a driver is validated where it is used.
    """
    raw = _read_driver_document(home)
    if raw is None:
        return None
    try:
        return parse_recorded_slots(raw.get("recorded_slots"))
    except DriverConfigError as exc:
        raise AccountError(f"{exc} ({DRIVERS_HINT})") from exc


def _require_drivers(config: DriverConfiguration) -> None:
    """Something names a driver, so drivers.json must exist."""
    if not config.present:
        raise AccountError(DRIVERS_NOT_CONFIGURED)


def _model_configuration(
    raw: dict[str, Any], config: DriverConfiguration
) -> tuple[ModelSlots, str]:
    """Translate model-selection errors into the configuration error channel."""
    if raw.get("models") is not None or raw.get("implementer") is not None:
        _require_drivers(config)
    try:
        models = parse_model_slots(raw.get("models"), lane_driver_ids(config.drivers))
        return models, parse_implementer(
            raw.get("implementer"), models, declared_driver_ids(config.drivers)
        )
    except ModelSlotsError as exc:
        raise AccountError(str(exc)) from exc


def _account_driver(
    config: DriverConfiguration, account: str, provider: str
) -> DriverSpec:
    _require_drivers(config)
    spec = config.drivers.get(provider)
    if spec is None:
        raise AccountError(
            f"Unsupported provider for account {account}: {DRIVERS_FILE} "
            f"configures no such driver ({DRIVERS_DOC})"
        )
    if spec.home_env is None:
        raise AccountError(
            f"Unsupported provider for account {account}: driver {provider} has "
            f"no home_env in {DRIVERS_FILE} ({DRIVERS_DOC})"
        )
    return spec


def _lane_slot(config: DriverConfiguration, slot: Any) -> str:
    """A lane slot ``<driver>:<kind>`` must name a driver with a lane section."""
    text = _text(slot, "lane slot")
    _require_drivers(config)
    driver, separator, kind = text.partition(":")
    spec = config.drivers.get(driver)
    if not separator or not kind or spec is None or spec.lane is None:
        raise AccountError(
            f"Lane slot {text} must name a driver with a lane section in "
            f"{DRIVERS_FILE} ({DRIVERS_DOC})"
        )
    return text


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
class AIAccountPool(AIAccount):
    """The account of a pool role before a lane start has chosen a member.

    ``name`` and ``config_dir`` are empty: no process may run on it.
    ``provider`` is the provider every member shares, and ``members`` names
    the pool's accounts in configuration order. Only a coordinator lane start
    chooses a member (``account_pool.select_lane_account``).
    """

    members: tuple[str, ...]


@dataclass(frozen=True)
class AIRole:
    name: str
    account: AIAccount
    model: str
    access: str

    @property
    def is_pool(self) -> bool:
        """True while the role still names its account pool, not one account."""
        return isinstance(self.account, AIAccountPool)

    def require_single_account(self) -> None:
        """Refuse a role that still names its account pool.

        Only a coordinator lane start chooses a pool member; every other launch
        path calls this right after resolving the role, before it records or
        checks anything for the role's account.
        """
        if self.is_pool:
            raise AccountError(POOL_ROLE_NEEDS_COORDINATOR.format(role=self.name))

    def env_prefix(self, drivers: Mapping[str, DriverSpec]) -> list[str]:
        """Return an ``env`` argv prefix that isolates driver configuration.

        Clears every configured credential variable, the network and
        certificate overrides, and every configured home variable for the child
        process only, then sets this driver's ``home_env`` to the account's
        ``config_dir``. Does not mutate ``os.environ``. This is process
        configuration isolation, not a sandbox or a claim that the driver CLI
        is already authenticated.

        A pool role is refused: its account is chosen when a coordinator lane
        starts, and only that resolved role may launch a process.
        """
        self.require_single_account()
        argv = ["env"]
        for key in role_clear_env(drivers):
            argv.extend(["-u", key])
        spec = drivers.get(self.account.provider)
        if spec is None or spec.home_env is None:
            raise AccountError(
                f"Unsupported provider for role {self.name} ({DRIVERS_HINT})"
            )
        if is_home_default_dir(spec, self.account.config_dir):
            # Some driver CLIs keep the login of their default profile outside
            # that directory, so pointing the variable at it would start an
            # empty, logged-out profile. The default profile is selected by
            # leaving the variable unset; any other directory is a named profile
            # the operator logged in with that variable set.
            return argv
        argv.append(f"{spec.home_env}={self.account.config_dir}")
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
    # Spend accounts and window-usage accounts. Empty is unconfigured.
    usage_accounts: tuple[str, ...] = ()
    # The validated drivers.json of the same load.
    driver_configuration: DriverConfiguration = NO_DRIVER_CONFIGURATION
    # Renew window logins this many seconds before expiry; None is off.
    token_keepalive_seconds: int | None = None

    @property
    def drivers(self) -> Mapping[str, DriverSpec]:
        return self.driver_configuration.drivers

    @property
    def recorded_slots(self) -> RecordedNames | None:
        return self.driver_configuration.recorded_slots

    @property
    def pane_driver(self) -> str | None:
        return self.driver_configuration.pane_driver

    def lane_drivers(self) -> tuple[str, ...]:
        """Configured drivers that may fill a model slot or run a lane."""
        return lane_driver_ids(self.drivers)

    def declared_drivers(self) -> tuple[str, ...]:
        """Every configured driver, including record-only implementers."""
        return declared_driver_ids(self.drivers)

    def driver(self, driver_id: str, *, lane: bool = False) -> DriverSpec:
        """The configured driver, refused like an unknown ``--vendor``.

        ``lane`` also requires a lane section.
        """
        choices = self.lane_drivers() if lane else self.declared_drivers()
        refused = driver_refusal(self.driver_configuration, driver_id, choices)
        if refused is not None:
            raise AccountError(refused)
        return self.drivers[driver_id]

    def interactive(self, driver_id: str) -> InteractiveSpec:
        """The driver's interactive section, refused like an unknown ``--provider``."""
        refused = driver_refusal(
            self.driver_configuration,
            driver_id,
            interactive_driver_ids(self.drivers),
            name="provider",
        )
        if refused is not None:
            raise AccountError(refused)
        return require_interactive(self.drivers[driver_id])

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

    def pool_members(self, role: AIRole) -> tuple[AIAccount, ...]:
        """The accounts of a pool role in configuration order; ``()`` otherwise."""
        return tuple(self.accounts[name] for name in _pool_member_names(role))

    def for_session(self, session_id: str) -> AIRole:
        binding = self.sessions.get(session_id)
        if binding is None:
            raise AccountError(f"No AI session configured for {session_id}")
        if binding.interactive is None:
            raise AccountError(f"No interactive AI role configured for session {session_id}")
        return self.roles[binding.interactive]

    def usage_adapter(self, driver: str, snapshot: str, selection: str) -> UsageSpec:
        """The validated usage adapter of a selected driver."""
        return _require_usage(self.driver_configuration, driver, snapshot, selection)

    def pane_markers(self, session_id: str) -> PaneMarkers:
        """Markers that classify a session's tmux pane.

        The driver of the session's interactive role paints that pane. A session
        whose role has no interactive driver, or that has no interactive role, is
        classified with ``pane_driver``; without one the pane cannot be read.
        """
        binding = self.sessions.get(session_id)
        if binding is not None and binding.interactive is not None:
            spec = self.drivers.get(self.roles[binding.interactive].account.provider)
            if spec is not None and spec.interactive is not None:
                return spec.interactive.pane
        pane_driver = self.pane_driver
        if pane_driver is not None:
            interactive = self.drivers[pane_driver].interactive
            if interactive is not None:
                return interactive.pane
        raise AccountError(PANE_UNCLASSIFIED.format(session=session_id))


def load_ai_accounts(
    home: Path, *, defer_model_configuration: bool = False
) -> AIAccounts:
    path = home / CONFIG_FILE
    driver_configuration = load_driver_configuration(home)
    if not path.exists():
        # Device configuration has no built-in model assignment.
        model_configuration_error: str | None = None
        try:
            _model_configuration({}, driver_configuration)
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
            driver_configuration=driver_configuration,
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_KEYS:
        raise AccountError(
            f"{CONFIG_FILE} accepts only accounts, roles, sessions, usage_session, "
            "usage_accounts, models, implementer, lanes, lane_execution, "
            "and token_keepalive"
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
        _account_driver(driver_configuration, name, provider)
        config_dir = _config_dir(entry.get("config_dir"), "config_dir")
        accounts[name] = AIAccount(name, provider, config_dir)

    roles: dict[str, AIRole] = {}
    for name, entry in roles_raw.items():
        _text(name, "role name")
        if not isinstance(entry, dict) or set(entry) - ROLE_KEYS:
            raise AccountError(f"Invalid role fields for {name}")
        if ("account" in entry) == ("accounts" in entry):
            raise AccountError(
                f"Role {name} must name exactly one of account and accounts"
            )
        role_account: AIAccount
        if "account" in entry:
            account_name = _text(entry.get("account"), "role account")
            if account_name not in accounts:
                raise AccountError(f"Role {name} references an unknown account")
            role_account = accounts[account_name]
        else:
            role_account = _role_pool(
                name, entry.get("accounts"), accounts, driver_configuration
            )
        model = _text(entry.get("model"), "model")
        access = _text(entry.get("access"), "access")
        if access not in ACCESS_VALUES:
            raise AccountError(f"Invalid access for role {name}")
        roles[name] = AIRole(name, role_account, model, access)

    device_lanes_raw = raw.get("lanes")
    if device_lanes_raw is None:
        device_lanes_raw = {}
    if not isinstance(device_lanes_raw, dict):
        raise AccountError("lanes must be an object or null")
    device_lanes: dict[str, str] = {}
    for slot, role_name in device_lanes_raw.items():
        _lane_slot(driver_configuration, slot)
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
            if roles[interactive].is_pool:
                # An account pool serves coordinator lanes only; an interactive
                # session and its usage reads need one fixed account.
                raise AccountError(
                    f"Session {session_id} interactive role {interactive} names an "
                    "account pool; an interactive role must name one account"
                )
        lanes_raw = entry.get("lanes")
        if lanes_raw is None:
            lanes_raw = {}
        if not isinstance(lanes_raw, dict):
            raise AccountError(f"Session {session_id} lanes must be an object or null")
        lanes: dict[str, str] = {}
        for slot, role_name in lanes_raw.items():
            _lane_slot(driver_configuration, slot)
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
        _require_usage(driver_configuration, selected.account.provider, "period", "usage_session")
    models: ModelSlots | None = None
    implementer: str | None = None
    model_configuration_error: str | None = None
    try:
        # New model checks follow the manifest's established account, role, and
        # session preconditions. Interactive launchers may defer this check
        # until their older launch preconditions have run.
        models, implementer = _model_configuration(raw, driver_configuration)
    except AccountError as exc:
        if not defer_model_configuration:
            raise
        model_configuration_error = str(exc)
    usage_accounts_raw = raw.get("usage_accounts")
    if usage_accounts_raw is None:
        usage_accounts_raw = []
    if not isinstance(usage_accounts_raw, list):
        raise AccountError("usage_accounts must be a list or null")
    usage_accounts: list[str] = []
    for value in usage_accounts_raw:
        account_name = _text(value, "usage_accounts entry")
        account = accounts.get(account_name)
        if account is None:
            raise AccountError(f"usage_accounts references unknown account {account_name}")
        configured = driver_configuration.drivers.get(account.provider)
        snapshot = (
            configured.usage.snapshot
            if configured is not None and configured.usage is not None
            else None
        )
        _require_usage(
            driver_configuration,
            account.provider,
            "windows" if snapshot == "windows" else "spend",
            f"usage_accounts account {account_name}",
        )
        if account_name in usage_accounts:
            raise AccountError(f"usage_accounts contains duplicate account {account_name}")
        usage_accounts.append(account_name)
    keepalive_seconds = _parse_token_keepalive(raw.get("token_keepalive"))
    for role in roles.values():
        # Selection reads each member's polled window usage, so a member that
        # is not polled could never be chosen correctly.
        for member in _pool_member_names(role):
            if member not in usage_accounts:
                raise AccountError(
                    f"Role {role.name} pool account {member} is not listed in "
                    "usage_accounts; a pool member's usage must be polled"
                )
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
        usage_accounts=tuple(usage_accounts),
        driver_configuration=driver_configuration,
        token_keepalive_seconds=keepalive_seconds,
    )


def _require_usage(
    config: DriverConfiguration, driver: str, snapshot: str, selection: str
) -> UsageSpec:
    """A usage selection needs its driver's usage adapter of the right shape."""
    spec = config.drivers.get(driver)
    if spec is None or spec.usage is None or spec.usage.snapshot != snapshot:
        raise AccountError(
            f"{selection} uses driver {driver}, which has no {snapshot} usage "
            f"section in {DRIVERS_FILE} ({DRIVERS_DOC})"
        )
    return spec.usage


def _parse_token_keepalive(value: Any) -> int | None:
    """Seconds before expiry at which window logins are renewed; ``None`` is off."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"refresh_before_minutes"}:
        raise AccountError(
            "token_keepalive must be null or an object with only refresh_before_minutes"
        )
    minutes = value["refresh_before_minutes"]
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or not KEEPALIVE_MIN_MINUTES <= minutes <= KEEPALIVE_MAX_MINUTES
    ):
        raise AccountError(
            "token_keepalive.refresh_before_minutes must be an integer from "
            f"{KEEPALIVE_MIN_MINUTES} to {KEEPALIVE_MAX_MINUTES}"
        )
    return minutes * 60
def _role_pool(
    role: str,
    value: Any,
    accounts: Mapping[str, AIAccount],
    config: DriverConfiguration,
) -> AIAccountPool:
    """Validate a role's ``accounts`` list into its unselected pool account.

    At least two distinct configured accounts of one provider whose driver has
    a ``windows`` usage adapter: selection compares their window usage.
    """
    if not isinstance(value, list) or len(value) < 2:
        raise AccountError(
            f"Role {role} accounts must be a list of at least two account names"
        )
    members: list[str] = []
    for item in value:
        member = _text(item, f"role {role} accounts entry")
        if member not in accounts:
            raise AccountError(f"Role {role} references an unknown account {member}")
        if member in members:
            raise AccountError(f"Role {role} accounts lists account {member} twice")
        members.append(member)
    providers = sorted({accounts[member].provider for member in members})
    if len(providers) != 1:
        raise AccountError(
            f"Role {role} accounts must all use one provider, found "
            f"{', '.join(providers)}"
        )
    provider = providers[0]
    _require_usage(config, provider, "windows", f"Role {role} account pool")
    return AIAccountPool(
        name="", provider=provider, config_dir="", members=tuple(members)
    )


def _pool_member_names(role: AIRole) -> tuple[str, ...]:
    account = role.account
    return account.members if isinstance(account, AIAccountPool) else ()


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


def load_model_configuration(home: Path) -> tuple[ModelSlots, str]:
    """The device's review slots and effective implementer selection."""
    path = home / CONFIG_FILE
    driver_configuration = load_driver_configuration(home)
    if not path.exists():
        # Use the central fail-closed error for an unconfigured device.
        return _model_configuration({}, driver_configuration)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_KEYS:
        raise AccountError(
            f"{CONFIG_FILE} accepts only accounts, roles, sessions, usage_session, "
            "usage_accounts, models, implementer, lanes, lane_execution, "
            "and token_keepalive"
        )
    # Device-configuration read: task creation never supplies a built-in assignment.
    return _model_configuration(raw, driver_configuration)


def load_model_slots(home: Path) -> ModelSlots:
    """The device's explicitly configured model slots.

    Checks the file is readable JSON with known top-level keys and validates the
    ``models`` and ``implementer`` selections against ``drivers.json``; errors
    inside ``accounts``, ``roles``, ``sessions``, ``lanes``, ``lane_execution``
    or ``usage_accounts`` do not stop task creation. Launches still validate
    the whole file.
    """
    return load_model_configuration(home)[0]


@dataclass(frozen=True)
class AISubscription:
    """One known AI subscription; listing it needs no login on this device."""

    provider: str
    account: str
    label: str | None = None
    status: str | None = None
    note: str | None = None


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def load_ai_subscriptions(home: Path) -> tuple[AISubscription, ...] | None:
    """Read ``$AGENT_HOME/ai-subscriptions.json``; ``None`` when it is absent.

    The inventory lists every known subscription, including those whose login
    is on no device. It holds no credentials. Unknown keys are rejected.
    """
    path = home / SUBSCRIPTIONS_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {SUBSCRIPTIONS_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) != {"subscriptions"}:
        raise AccountError(f"{SUBSCRIPTIONS_FILE} accepts only subscriptions")
    entries = raw["subscriptions"]
    if not isinstance(entries, list):
        raise AccountError("subscriptions must be a list")
    seen: set[tuple[str, str]] = set()
    subscriptions: list[AISubscription] = []
    for index, entry in enumerate(entries):
        where = f"subscriptions[{index}]"
        if not isinstance(entry, dict):
            raise AccountError(f"{where} must be an object")
        unknown = set(entry) - _SUBSCRIPTION_FIELDS
        if unknown:
            raise AccountError(
                f"{where} has unknown keys: {', '.join(sorted(map(str, unknown)))}"
            )
        provider = entry.get("provider")
        if not isinstance(provider, str) or not _SUBSCRIPTION_PROVIDER.fullmatch(provider):
            raise AccountError(
                f"{where}.provider must be a lowercase token of a-z, 0-9 and -"
            )
        account = _text(entry.get("account"), f"{where}.account")
        key = (provider, account.casefold())
        if key in seen:
            raise AccountError(f"{where} repeats provider {provider} account {account}")
        seen.add(key)
        subscriptions.append(
            AISubscription(
                provider=provider,
                account=account,
                label=_optional_text(entry.get("label"), f"{where}.label"),
                status=_optional_text(entry.get("status"), f"{where}.status"),
                note=_optional_text(entry.get("note"), f"{where}.note"),
            )
        )
    return tuple(subscriptions)
