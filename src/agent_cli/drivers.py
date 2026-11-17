"""Driver configuration: everything specific to one AI driver.

The repository ships no driver table and no driver default. The operator file
``$AGENT_HOME/drivers.json`` describes each driver: its executable and argument
templates, how the spec and the output travel, its configuration-directory
variable, the credential variables to scrub, capability flags, the usage
adapter, the attribution display name and the terminal pane markers. This module
validates that document strictly and builds argv from its templates. A missing
or incomplete description fails loudly; nothing falls back to a built-in value.

Pure module: no Postgres, no network, no subprocess.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

DRIVERS_FILE = "drivers.json"
DRIVERS_DOC = "docs/ai-accounts.md#drivers"
DRIVERS_HINT = f"see {DRIVERS_FILE}, {DRIVERS_DOC}"
DRIVERS_NOT_CONFIGURED = (
    f"drivers is not configured: {DRIVERS_FILE} must describe every driver that "
    "ai-accounts.json names in accounts, models, implementer, lanes and usage "
    f"selections ({DRIVERS_DOC})"
)
# The refusal of a driver that cannot do a job: an unknown ``--vendor`` or
# ``--provider``, a task pin, or a driver without the section the job needs.
DRIVER_REFUSED = f"{{name}} must be {{choices}} ({DRIVERS_HINT})"
JUDGMENT_UNSUPPORTED = (
    "driver {driver} has no lane.argv.judgment; it cannot run a check lane"
)
OUTPUT_FILE_REQUIRED = "driver {driver} requires an output file"
PANE_UNCLASSIFIED = (
    "session {session} has no interactive driver and no pane_driver is configured; "
    f"cannot classify its pane ({DRIVERS_HINT})"
)
SESSION_ENV_NOT_CONFIGURED = (
    f"no driver in {DRIVERS_FILE} configures interactive.session_env; "
    f"pass --session ({DRIVERS_HINT})"
)
WRITE_READ_DIRS_REFUSED = "write lane takes no extra directories"

DOCUMENT_KEYS = ("drivers", "recorded_slots", "pane_driver")
DRIVER_KEYS = (
    "display_name",
    "home_env",
    "home_default_dir",
    "credential_env",
    "lane",
    "interactive",
    "usage",
)
LANE_KEYS = (
    "spec",
    "output",
    "may_run_commands",
    "audit_stderr_when_empty",
    "argv",
    "read_dir_args",
    "write_hardening",
    "env",
    "credential_file",
)
LANE_ARGV_KEYS = ("read", "write", "judgment")
CREDENTIAL_FILE_KEYS = ("file", "env")
INTERACTIVE_KEYS = (
    "argv_new",
    "argv_resume",
    "session_id",
    "read_only_args",
    "hardening_args",
    "session_env",
    "pane",
)
PANE_KEYS = ("working", "queued", "idle", "permission", "permission_confirm", "chrome")
USAGE_KEYS = ("snapshot", "credential", "proxy_from_environment", "requests", "fields")
WINDOWS_USAGE_KEYS = (
    "snapshot",
    "label",
    "poll_seconds",
    "credential",
    "account_file",
    "proxy_from_environment",
    "requests",
    "fields",
    "window",
    "limits",
)
USAGE_REQUEST_KEYS = ("name", "url", "headers")
JSON_ENTRY_CREDENTIAL_KEYS = (
    "file",
    "format",
    "entry_prefix",
    "token",
    "expires_at",
    "account",
)
TEXT_CREDENTIAL_KEYS = ("file", "format")
OAUTH_FILE_CREDENTIAL_KEYS = (
    "file",
    "format",
    "label",
    "object",
    "token",
    "token_name",
    "expires_at",
    "expires_unit",
    "subscription",
    "login_name",
    "renew",
    "multiplier",
    "keychain",
)
OAUTH_FILE_CREDENTIAL_REQUIRED = (
    "file",
    "format",
    "label",
    "object",
    "token",
    "token_name",
    "expires_at",
    "expires_unit",
    "subscription",
    "login_name",
    "renew",
)
MULTIPLIER_KEYS = ("plan", "pattern", "field")
KEYCHAIN_KEYS = (
    "argv",
    "service",
    "user_env",
    "user_pattern",
    "fallback_user",
    "missing_status",
    "platform",
    "service_suffix",
)
KEYCHAIN_REQUIRED = (
    "argv",
    "service",
    "user_env",
    "user_pattern",
    "fallback_user",
    "missing_status",
    "platform",
)
ACCOUNT_FILE_KEYS = ("file", "object", "field", "default_name")
ACCOUNT_FILE_REQUIRED = ("file", "object", "field")
WINDOW_FIELD_KEYS = ("used_percent", "resets_at", "round")
LIMIT_FIELD_KEYS = (
    "kind",
    "weekly_all",
    "weekly_model",
    "used_percent",
    "resets_at",
    "scope",
    "model",
    "model_names",
)
WINDOWS_FIELDS = ("five_hour", "weekly", "limits")
PRODUCT_FIELD_KEYS = ("list", "product", "used_percent")
PERIOD_FIELDS = (
    "tier",
    "used_percent",
    "period_type",
    "period_start",
    "period_end",
    "products",
    "prepaid_val",
    "on_demand_cap_val",
    "on_demand_used_val",
)
SPEND_FIELDS = (
    "usage_usd",
    "usage_daily_usd",
    "usage_weekly_usd",
    "usage_monthly_usd",
    "limit_usd",
    "limit_remaining_usd",
    "limit_reset",
)
RECORDED_SLOT_KEYS = ("first", "second")

SPEC_MODES = ("stdin", "file-arg")
OUTPUT_MODES = ("event-stream", "last-message-file")
LANE_MODES = ("read", "write")
SESSION_ID_FORMATS = ("uuid",)
USAGE_SNAPSHOTS = ("period", "spend", "windows")
USAGE_CREDENTIAL_FORMATS = {
    "period": "json-entry",
    "spend": "text",
    "windows": "oauth-file",
}
EXPIRES_UNITS = ("epoch-ms",)
WINDOW_ROUNDS = ("minute",)
SERVICE_SUFFIXES = ("nfc-sha256-8",)
# A windows adapter names its own poll; a value outside this range is refused.
POLL_SECONDS_MAX = 86400
KEYCHAIN_PLACEHOLDERS = frozenset({"service", "user"})
# Every usage request sends these itself; a configured header may not replace them.
USAGE_FIXED_HEADERS = frozenset({"authorization", "accept"})
# Lane template placeholders; ``{dir}`` exists only inside ``read_dir_args``.
LANE_PLACEHOLDERS = frozenset({"model", "cwd", "spec_file", "output_file"})
READ_DIR_PLACEHOLDERS = frozenset({"dir"})
INTERACTIVE_PLACEHOLDERS = frozenset({"session_id", "model"})
# Ids ``first`` and ``second`` would collide with the slot-derived names.
RESERVED_DRIVER_IDS = frozenset({"first", "second"})
DRIVER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# An oauth credential file and an account file may hide with one leading dot.
DOT_FILE_RE = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9._-]*")
REQUEST_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]{1,32})\}")
TEXT_LIMIT = 1000
URL_LIMIT = 2000
ARGV_LIMIT = 200
LIST_LIMIT = 100
REGEX_LIMIT = 500
REGEX_LIST_LIMIT = 20
SUBSTRING_LIST_LIMIT = 50
REQUEST_LIMIT = 10
HEADER_LIMIT = 20

# Generic network and certificate overrides. They are cleared for every lane,
# pane and role so an intercepting proxy never sees a driver's credentials.
NETWORK_OVERRIDE_ENV = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
# Shell redirects of a lane held in a tmux pane. A pane's stdin is a terminal
# and its scrollback is bounded, so the spec and the stream go through files.
_REDIRECT_SPEC_AND_STREAM = 'spec=$1; out=$2; shift 2; exec "$@" < "$spec" > "$out"'
_REDIRECT_STREAM = 'out=$1; shift; exec "$@" > "$out"'
_REDIRECT_SPEC = 'f=$1; shift; exec "$@" < "$f"'


class DriverConfigError(ValueError):
    """The driver document does not describe a driver completely and validly."""


@dataclass(frozen=True)
class CredentialFile:
    """Key file in the account's configuration directory, exported after launch."""

    file: str
    env: str


@dataclass(frozen=True)
class LaneSpec:
    spec: str
    output: str
    may_run_commands: bool
    audit_stderr_when_empty: bool
    argv_read: tuple[str, ...]
    argv_write: tuple[str, ...]
    argv_judgment: tuple[str, ...] | None = None
    read_dir_args: tuple[str, ...] | None = None
    write_hardening: tuple[tuple[str, ...], ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    credential_file: CredentialFile | None = None


@dataclass(frozen=True)
class PaneMarkers:
    """How a driver's terminal user interface paints its state."""

    working: tuple[re.Pattern[str], ...]
    idle: tuple[re.Pattern[str], ...]
    permission: tuple[re.Pattern[str], ...]
    permission_confirm: tuple[str, ...]
    chrome: tuple[str, ...]
    # Each group: case-insensitive substrings that must all be present.
    queued: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class InteractiveSpec:
    argv_new: tuple[str, ...]
    argv_resume: tuple[str, ...]
    session_id: str
    read_only_args: tuple[str, ...]
    hardening_args: tuple[str, ...]
    pane: PaneMarkers
    session_env: str | None = None


@dataclass(frozen=True)
class UsageMultiplier:
    """Plan name plus the one-group pattern that names its multiplier."""

    plan: str
    pattern: re.Pattern[str]
    field: str


@dataclass(frozen=True)
class UsageKeychain:
    """How a windows login is read from the platform secret store."""

    argv: tuple[str, ...]
    service: str
    user_env: str
    user_pattern: re.Pattern[str]
    fallback_user: str
    missing_status: int
    platform: str
    service_suffix: str | None = None


@dataclass(frozen=True)
class UsageCredential:
    file: str
    format: str
    entry_prefix: str | None = None
    token: str | None = None
    expires_at: str | None = None
    account: str | None = None
    label: str | None = None
    object_key: str | None = None
    token_name: str | None = None
    expires_unit: str | None = None
    subscription: str | None = None
    login_name: str | None = None
    renew: str | None = None
    multiplier: UsageMultiplier | None = None
    keychain: UsageKeychain | None = None


@dataclass(frozen=True)
class UsageAccountFile:
    """JSON file that names the login; ``object_key`` is the JSON key ``object``."""

    file: str
    object_key: str
    field: str
    default_name: str | None = None


@dataclass(frozen=True)
class UsageWindowFields:
    """Response keys of one usage window, and how its reset time is rounded."""

    used_percent: str
    resets_at: str
    round: str


@dataclass(frozen=True)
class UsageLimitFields:
    """Response keys of a limits list and the kind values that select a window."""

    kind: str
    weekly_all: str
    weekly_model: str
    used_percent: str
    resets_at: str
    scope: str
    model: str
    model_names: tuple[str, ...]


@dataclass(frozen=True)
class UsageRequest:
    name: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProductFields:
    """Pointers for a product list: ``list`` names a response, the rest an item."""

    list: str
    product: str
    used_percent: str


@dataclass(frozen=True)
class UsageSpec:
    snapshot: str
    credential: UsageCredential
    proxy_from_environment: bool
    requests: tuple[UsageRequest, ...]
    # Snapshot field -> JSON pointer whose first segment names a request.
    fields: tuple[tuple[str, str], ...]
    products: ProductFields | None = None
    label: str | None = None
    poll_seconds: int | None = None
    account_file: UsageAccountFile | None = None
    window: UsageWindowFields | None = None
    limits: UsageLimitFields | None = None

    def pointer(self, name: str) -> str:
        for key, value in self.fields:
            if key == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True)
class DriverSpec:
    id: str
    display_name: str | None = None
    home_env: str | None = None
    home_default_dir: str | None = None
    credential_env: tuple[str, ...] = ()
    lane: LaneSpec | None = None
    interactive: InteractiveSpec | None = None
    usage: UsageSpec | None = None


@dataclass(frozen=True)
class RecordedNames:
    """Driver ids that an earlier version wrote into stored names.

    Earlier versions named the first and second review stage after the driver
    that ran it. These ids only name history; they need not be configured
    drivers. The maps derive every such stored name and the neutral slot it
    stands for.
    """

    first: str
    second: str

    def slots(self) -> tuple[tuple[str, str], ...]:
        return (("first", self.first), ("second", self.second))

    def stage_slots(self) -> dict[str, str]:
        """Stored gate or agent stage -> slot."""
        return {f"{driver}-pr": slot for slot, driver in self.slots()}

    def key_slots(self) -> dict[str, tuple[str, str]]:
        """Stored checklist key -> (slot, dimension)."""
        return {
            f"{driver}_pr_{dimension}": (slot, dimension)
            for slot, driver in self.slots()
            for dimension in ("quality", "logic")
        }

    def phase_slots(self) -> dict[str, str]:
        """Stored coordinator phase -> slot."""
        return {f"pr_gates_{driver}": slot for slot, driver in self.slots()}


@dataclass(frozen=True)
class DriverConfiguration:
    drivers: dict[str, DriverSpec] = field(default_factory=dict)
    recorded_slots: RecordedNames | None = None
    pane_driver: str | None = None
    # False when the operator supplied no drivers.json at all.
    present: bool = True


NO_DRIVER_CONFIGURATION = DriverConfiguration(present=False)


@dataclass(frozen=True)
class LaneIO:
    """How one lane passes its spec and returns its output.

    ``output_file``: the launcher creates an output file and reads the lane's
    final text (``last-message-file``) or event stream from it.
    ``stdin``: the launcher passes the spec text on stdin (typed into the pane
    under tmux). ``redirect``: shell text that redirects inside a tmux pane;
    ``redirect_args`` names its positional arguments in order.
    """

    output: str
    output_file: bool
    stdin: bool
    audit_stderr_when_empty: bool
    redirect: str | None = None
    redirect_args: tuple[str, ...] = ()


# --- driver ids --------------------------------------------------------------


def valid_driver_id(value: object) -> bool:
    """True for a syntactically valid driver id (configured or not)."""
    return (
        isinstance(value, str)
        and DRIVER_ID_RE.fullmatch(value) is not None
        and value not in RESERVED_DRIVER_IDS
    )


def choice_text(ids: Sequence[str]) -> str:
    return "|".join(ids) if ids else "(no driver configured)"


def lane_driver_ids(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Drivers that may fill a model slot or run a lane, sorted."""
    return tuple(sorted(d for d, spec in drivers.items() if spec.lane is not None))


def declared_driver_ids(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Every configured driver, including record-only implementers, sorted."""
    return tuple(sorted(drivers))


def interactive_driver_ids(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Drivers that may start an interactive session, sorted."""
    return tuple(sorted(d for d, spec in drivers.items() if spec.interactive is not None))


def driver_refusal(
    config: DriverConfiguration,
    driver_id: str,
    choices: Sequence[str],
    *,
    name: str = "vendor",
) -> str | None:
    """The refusal of ``driver_id`` when it is not one of ``choices``, else ``None``.

    ``choices`` are the configured drivers that can do the job. Without a
    ``drivers.json`` the refusal says that drivers are not configured.
    """
    if not config.present:
        return DRIVERS_NOT_CONFIGURED
    if driver_id in choices:
        return None
    return DRIVER_REFUSED.format(name=name, choices=choice_text(choices))


# --- validation ----------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    raise DriverConfigError(message)


def _object(
    value: Any,
    path: str,
    keys: Sequence[str],
    required: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    if set(value) - set(keys):
        _fail(f"{path} accepts only {', '.join(keys)}")
    for key in required:
        if value.get(key) is None:
            _fail(f"{path}.{key} is required")
    return value


def _line(value: Any, path: str, *, empty: bool = False, limit: int = TEXT_LIMIT) -> str:
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or len(value) > limit
        or any(ch in value for ch in ("\x00", "\n", "\r"))
    ):
        kind = "a single-line string" if empty else "a non-empty single-line string"
        _fail(f"{path} must be {kind} of at most {limit} characters")
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{path} must be true or false")
    return value


def _choice(value: Any, path: str, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail(f"{path} must be one of {', '.join(choices)}")
    return value


def _env_name(value: Any, path: str) -> str:
    if not isinstance(value, str) or ENV_NAME_RE.fullmatch(value) is None:
        _fail(f"{path} must be an environment variable name")
    return value


def _file_name(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or FILE_NAME_RE.fullmatch(value) is None
    ):
        _fail(f"{path} must be a plain file name")
    return value


def _dot_file_name(value: Any, path: str) -> str:
    """A plain file name, optionally hidden by one leading dot."""
    if (
        not isinstance(value, str)
        or len(value) > 255
        or DOT_FILE_RE.fullmatch(value) is None
    ):
        _fail(f"{path} must be a plain file name")
    return value


def _bounded_int(value: Any, path: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
        _fail(f"{path} must be an integer from {low} to {high}")
    return value


def _one_group(value: Any, path: str) -> re.Pattern[str]:
    text = _line(value, path, limit=REGEX_LIMIT)
    try:
        pattern = re.compile(text)
    except re.error:
        _fail(f"{path} is not a valid regular expression")
    if pattern.groups != 1:
        _fail(f"{path} must contain exactly one group")
    return pattern


def _list(value: Any, path: str, *, limit: int, non_empty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (non_empty and not value) or len(value) > limit:
        size = "1 to" if non_empty else "at most"
        _fail(f"{path} must be a list of {size} {limit} entries")
    return value


def _env_names(value: Any, path: str) -> tuple[str, ...]:
    items = _list(value, path, limit=LIST_LIMIT, non_empty=False)
    return tuple(_env_name(item, f"{path}[{i}]") for i, item in enumerate(items))


def _args(value: Any, path: str) -> tuple[str, ...]:
    items = _list(value, path, limit=ARGV_LIMIT, non_empty=False)
    return tuple(_line(item, f"{path}[{i}]", empty=True) for i, item in enumerate(items))


def _placeholder(element: str) -> str | None:
    match = _PLACEHOLDER_RE.fullmatch(element)
    return match.group(1) if match else None


def _template(
    value: Any,
    path: str,
    *,
    allowed: frozenset[str],
    required: Sequence[str],
    forbidden: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    items = _list(value, path, limit=ARGV_LIMIT)
    used: list[str] = []
    for i, item in enumerate(items):
        element = _line(item, f"{path}[{i}]", empty=True)
        name = _placeholder(element)
        if name is None:
            if "{" in element or "}" in element:
                _fail(f"{path}[{i}] may use a placeholder only as the whole element")
            continue
        if name not in allowed:
            _fail(f"{path}[{i}] uses unknown placeholder {{{name}}}")
        if forbidden and name in forbidden:
            _fail(f"{path}[{i}] uses {{{name}}}, {forbidden[name]}")
        used.append(name)
    for name in required:
        if name not in used:
            _fail(f"{path} must use {{{name}}}")
    return tuple(items)


def _regexes(value: Any, path: str) -> tuple[re.Pattern[str], ...]:
    items = _list(value, path, limit=REGEX_LIST_LIMIT)
    patterns: list[re.Pattern[str]] = []
    for i, item in enumerate(items):
        text = _line(item, f"{path}[{i}]", limit=REGEX_LIMIT)
        try:
            patterns.append(re.compile(text))
        except re.error:
            _fail(f"{path}[{i}] is not a valid regular expression")
    return tuple(patterns)


def _substrings(value: Any, path: str) -> tuple[str, ...]:
    items = _list(value, path, limit=SUBSTRING_LIST_LIMIT)
    return tuple(
        _line(item, f"{path}[{i}]", limit=REGEX_LIMIT) for i, item in enumerate(items)
    )


def _home_default_dir(value: Any, path: str) -> str:
    text = _line(value, path)
    candidate = Path(text)
    if not (text.startswith("~/") or candidate.is_absolute()):
        _fail(f"{path} must be absolute or start with ~/")
    if ".." in candidate.parts:
        _fail(f"{path} must not contain parent traversal")
    return text


def _https_url(value: Any, path: str) -> str:
    text = _line(value, path, limit=URL_LIMIT)
    try:
        parts = urlsplit(text)
    except ValueError:
        _fail(f"{path} must be an https:// URL")
    if parts.scheme != "https" or not parts.netloc or any(ch.isspace() for ch in text):
        _fail(f"{path} must be an https:// URL")
    return text


def _pointer(value: Any, path: str) -> str:
    text = _line(value, path)
    if not text.startswith("/") or re.search(r"~(?![01])", text):
        _fail(f"{path} must be a JSON pointer")
    return text


def _request_pointer(value: Any, path: str, requests: Sequence[str]) -> str:
    pointer = _pointer(value, path)
    if pointer.split("/")[1] not in requests:
        _fail(f"{path} must start with the name of a configured request")
    return pointer


def _parse_lane(raw: Any, path: str, home_env: str | None) -> LaneSpec:
    lane = _object(
        raw,
        path,
        LANE_KEYS,
        ("spec", "output", "may_run_commands", "audit_stderr_when_empty", "argv"),
    )
    spec = _choice(lane["spec"], f"{path}.spec", SPEC_MODES)
    output = _choice(lane["output"], f"{path}.output", OUTPUT_MODES)
    required = ["model"]
    if spec == "file-arg":
        required.append("spec_file")
    forbidden: dict[str, str] = {}
    if output == "last-message-file":
        required.append("output_file")
    else:
        forbidden["output_file"] = "which only lane.output last-message-file provides"
    argv = _object(lane["argv"], f"{path}.argv", LANE_ARGV_KEYS, ("read", "write"))
    templates = {
        key: _template(
            argv[key],
            f"{path}.argv.{key}",
            allowed=LANE_PLACEHOLDERS,
            required=required,
            forbidden=forbidden,
        )
        for key in LANE_ARGV_KEYS
        if argv.get(key) is not None
    }
    read_dir_args: tuple[str, ...] | None = None
    if lane.get("read_dir_args") is not None:
        read_dir_args = _template(
            lane["read_dir_args"],
            f"{path}.read_dir_args",
            allowed=READ_DIR_PLACEHOLDERS,
            required=("dir",),
        )
        if sum(1 for item in read_dir_args if _placeholder(item) == "dir") != 1:
            _fail(f"{path}.read_dir_args must use {{dir}} exactly once")
    hardening: tuple[tuple[str, ...], ...] = ()
    if lane.get("write_hardening") is not None:
        groups = _list(
            lane["write_hardening"], f"{path}.write_hardening", limit=LIST_LIMIT
        )
        parsed_groups: list[tuple[str, ...]] = []
        for i, group in enumerate(groups):
            group_path = f"{path}.write_hardening[{i}]"
            parsed = _args(group, group_path)
            if not parsed:
                _fail(f"{group_path} must not be empty")
            parsed_groups.append(parsed)
        hardening = tuple(parsed_groups)
    env: tuple[tuple[str, str], ...] = ()
    if lane.get("env") is not None:
        env_raw = lane["env"]
        if not isinstance(env_raw, dict) or len(env_raw) > LIST_LIMIT:
            _fail(f"{path}.env must be an object of at most {LIST_LIMIT} variables")
        env = tuple(
            (
                _env_name(name, f"{path}.env key"),
                _line(value, f"{path}.env.{name}", empty=True),
            )
            for name, value in env_raw.items()
        )
    credential_file: CredentialFile | None = None
    if lane.get("credential_file") is not None:
        cf_path = f"{path}.credential_file"
        cf = _object(lane["credential_file"], cf_path, CREDENTIAL_FILE_KEYS, CREDENTIAL_FILE_KEYS)
        if home_env is None:
            _fail(f"{cf_path} requires home_env")
        if output != "event-stream":
            _fail(f"{cf_path} requires lane.output event-stream")
        credential_file = CredentialFile(
            _file_name(cf["file"], f"{cf_path}.file"),
            _env_name(cf["env"], f"{cf_path}.env"),
        )
    return LaneSpec(
        spec=spec,
        output=output,
        may_run_commands=_bool(lane["may_run_commands"], f"{path}.may_run_commands"),
        audit_stderr_when_empty=_bool(
            lane["audit_stderr_when_empty"], f"{path}.audit_stderr_when_empty"
        ),
        argv_read=templates["read"],
        argv_write=templates["write"],
        argv_judgment=templates.get("judgment"),
        read_dir_args=read_dir_args,
        write_hardening=hardening,
        env=env,
        credential_file=credential_file,
    )


def _parse_pane(raw: Any, path: str) -> PaneMarkers:
    pane = _object(
        raw,
        path,
        PANE_KEYS,
        ("working", "idle", "permission", "permission_confirm", "chrome"),
    )
    queued: tuple[tuple[str, ...], ...] = ()
    if pane.get("queued") is not None:
        groups = _list(pane["queued"], f"{path}.queued", limit=REGEX_LIST_LIMIT)
        queued = tuple(
            _substrings(group, f"{path}.queued[{i}]") for i, group in enumerate(groups)
        )
    return PaneMarkers(
        working=_regexes(pane["working"], f"{path}.working"),
        idle=_regexes(pane["idle"], f"{path}.idle"),
        permission=_regexes(pane["permission"], f"{path}.permission"),
        permission_confirm=_substrings(
            pane["permission_confirm"], f"{path}.permission_confirm"
        ),
        chrome=_substrings(pane["chrome"], f"{path}.chrome"),
        queued=queued,
    )


def _parse_interactive(raw: Any, path: str) -> InteractiveSpec:
    interactive = _object(
        raw,
        path,
        INTERACTIVE_KEYS,
        (
            "argv_new",
            "argv_resume",
            "session_id",
            "read_only_args",
            "hardening_args",
            "pane",
        ),
    )
    templates = {
        key: _template(
            interactive[key],
            f"{path}.{key}",
            allowed=INTERACTIVE_PLACEHOLDERS,
            required=("session_id", "model"),
        )
        for key in ("argv_new", "argv_resume")
    }
    session_env: str | None = None
    if interactive.get("session_env") is not None:
        session_env = _env_name(interactive["session_env"], f"{path}.session_env")
    return InteractiveSpec(
        argv_new=templates["argv_new"],
        argv_resume=templates["argv_resume"],
        session_id=_choice(
            interactive["session_id"], f"{path}.session_id", SESSION_ID_FORMATS
        ),
        read_only_args=_args(interactive["read_only_args"], f"{path}.read_only_args"),
        hardening_args=_args(interactive["hardening_args"], f"{path}.hardening_args"),
        pane=_parse_pane(interactive["pane"], f"{path}.pane"),
        session_env=session_env,
    )


def _parse_usage_credential(raw: Any, path: str, snapshot: str) -> UsageCredential:
    expected = USAGE_CREDENTIAL_FORMATS[snapshot]
    if not isinstance(raw, dict):
        _fail(f"{path} must be an object")
    if raw.get("format") != expected:
        _fail(f"{path}.format must be {expected} for a {snapshot} snapshot")
    if expected == "text":
        credential = _object(raw, path, TEXT_CREDENTIAL_KEYS, TEXT_CREDENTIAL_KEYS)
        return UsageCredential(_file_name(credential["file"], f"{path}.file"), expected)
    credential = _object(
        raw, path, JSON_ENTRY_CREDENTIAL_KEYS, JSON_ENTRY_CREDENTIAL_KEYS
    )
    return UsageCredential(
        file=_file_name(credential["file"], f"{path}.file"),
        format=expected,
        entry_prefix=_line(credential["entry_prefix"], f"{path}.entry_prefix"),
        token=_line(credential["token"], f"{path}.token"),
        expires_at=_line(credential["expires_at"], f"{path}.expires_at"),
        account=_line(credential["account"], f"{path}.account"),
    )


def _parse_usage_requests(raw: Any, path: str) -> tuple[UsageRequest, ...]:
    items = _list(raw, path, limit=REQUEST_LIMIT)
    requests: list[UsageRequest] = []
    for i, item in enumerate(items):
        item_path = f"{path}[{i}]"
        request = _object(item, item_path, USAGE_REQUEST_KEYS, ("name", "url"))
        name = request["name"]
        if not isinstance(name, str) or REQUEST_NAME_RE.fullmatch(name) is None:
            _fail(f"{item_path}.name must be a request name")
        if any(existing.name == name for existing in requests):
            _fail(f"{item_path}.name repeats request {name}")
        headers_raw = request.get("headers")
        if headers_raw is None:
            headers_raw = {}
        if not isinstance(headers_raw, dict) or len(headers_raw) > HEADER_LIMIT:
            _fail(f"{item_path}.headers must be an object of at most {HEADER_LIMIT} headers")
        headers: list[tuple[str, str]] = []
        for header, value in headers_raw.items():
            if not isinstance(header, str) or HEADER_NAME_RE.fullmatch(header) is None:
                _fail(f"{item_path}.headers has an invalid header name")
            if header.lower() in USAGE_FIXED_HEADERS:
                _fail(f"{item_path}.headers.{header} is set by the usage adapter itself")
            headers.append((header, _line(value, f"{item_path}.headers.{header}", empty=True)))
        requests.append(
            UsageRequest(name, _https_url(request["url"], f"{item_path}.url"), tuple(headers))
        )
    return tuple(requests)


def _parse_multiplier(raw: Any, path: str) -> UsageMultiplier:
    multiplier = _object(raw, path, MULTIPLIER_KEYS, MULTIPLIER_KEYS)
    return UsageMultiplier(
        plan=_line(multiplier["plan"], f"{path}.plan"),
        pattern=_one_group(multiplier["pattern"], f"{path}.pattern"),
        field=_line(multiplier["field"], f"{path}.field"),
    )


def _parse_keychain(raw: Any, path: str) -> UsageKeychain:
    keychain = _object(raw, path, KEYCHAIN_KEYS, KEYCHAIN_REQUIRED)
    suffix: str | None = None
    if keychain.get("service_suffix") is not None:
        suffix = _choice(
            keychain["service_suffix"], f"{path}.service_suffix", SERVICE_SUFFIXES
        )
    user_pattern = _line(keychain["user_pattern"], f"{path}.user_pattern", limit=REGEX_LIMIT)
    try:
        compiled = re.compile(user_pattern)
    except re.error:
        _fail(f"{path}.user_pattern is not a valid regular expression")
    return UsageKeychain(
        argv=_template(
            keychain["argv"],
            f"{path}.argv",
            allowed=KEYCHAIN_PLACEHOLDERS,
            required=("service", "user"),
        ),
        service=_line(keychain["service"], f"{path}.service"),
        user_env=_env_name(keychain["user_env"], f"{path}.user_env"),
        user_pattern=compiled,
        fallback_user=_line(keychain["fallback_user"], f"{path}.fallback_user"),
        missing_status=_bounded_int(keychain["missing_status"], f"{path}.missing_status", 0, 255),
        platform=_line(keychain["platform"], f"{path}.platform"),
        service_suffix=suffix,
    )


def _parse_oauth_credential(raw: Any, path: str) -> UsageCredential:
    if not isinstance(raw, dict):
        _fail(f"{path} must be an object")
    if raw.get("format") != "oauth-file":
        _fail(f"{path}.format must be oauth-file for a windows snapshot")
    credential = _object(raw, path, OAUTH_FILE_CREDENTIAL_KEYS, OAUTH_FILE_CREDENTIAL_REQUIRED)
    multiplier: UsageMultiplier | None = None
    if credential.get("multiplier") is not None:
        multiplier = _parse_multiplier(credential["multiplier"], f"{path}.multiplier")
    keychain: UsageKeychain | None = None
    if credential.get("keychain") is not None:
        keychain = _parse_keychain(credential["keychain"], f"{path}.keychain")
    return UsageCredential(
        file=_dot_file_name(credential["file"], f"{path}.file"),
        format="oauth-file",
        token=_line(credential["token"], f"{path}.token"),
        expires_at=_line(credential["expires_at"], f"{path}.expires_at"),
        label=_line(credential["label"], f"{path}.label"),
        object_key=_line(credential["object"], f"{path}.object"),
        token_name=_line(credential["token_name"], f"{path}.token_name"),
        expires_unit=_choice(
            credential["expires_unit"], f"{path}.expires_unit", EXPIRES_UNITS
        ),
        subscription=_line(credential["subscription"], f"{path}.subscription"),
        login_name=_line(credential["login_name"], f"{path}.login_name"),
        renew=_line(credential["renew"], f"{path}.renew"),
        multiplier=multiplier,
        keychain=keychain,
    )


def _parse_account_file(
    raw: Any, path: str, home_default_dir: str | None
) -> UsageAccountFile:
    account_file = _object(raw, path, ACCOUNT_FILE_KEYS, ACCOUNT_FILE_REQUIRED)
    default_name: str | None = None
    if account_file.get("default_name") is not None:
        default_name = _dot_file_name(account_file["default_name"], f"{path}.default_name")
        if home_default_dir is None:
            _fail(f"{path}.default_name requires home_default_dir")
    return UsageAccountFile(
        file=_dot_file_name(account_file["file"], f"{path}.file"),
        object_key=_line(account_file["object"], f"{path}.object"),
        field=_line(account_file["field"], f"{path}.field"),
        default_name=default_name,
    )


def _parse_window_fields(raw: Any, path: str) -> UsageWindowFields:
    window = _object(raw, path, WINDOW_FIELD_KEYS, WINDOW_FIELD_KEYS)
    return UsageWindowFields(
        used_percent=_line(window["used_percent"], f"{path}.used_percent"),
        resets_at=_line(window["resets_at"], f"{path}.resets_at"),
        round=_choice(window["round"], f"{path}.round", WINDOW_ROUNDS),
    )


def _parse_limit_fields(raw: Any, path: str) -> UsageLimitFields:
    limits = _object(raw, path, LIMIT_FIELD_KEYS, LIMIT_FIELD_KEYS)
    names = _list(limits["model_names"], f"{path}.model_names", limit=LIST_LIMIT)
    return UsageLimitFields(
        kind=_line(limits["kind"], f"{path}.kind"),
        weekly_all=_line(limits["weekly_all"], f"{path}.weekly_all"),
        weekly_model=_line(limits["weekly_model"], f"{path}.weekly_model"),
        used_percent=_line(limits["used_percent"], f"{path}.used_percent"),
        resets_at=_line(limits["resets_at"], f"{path}.resets_at"),
        scope=_line(limits["scope"], f"{path}.scope"),
        model=_line(limits["model"], f"{path}.model"),
        model_names=tuple(_line(name, f"{path}.model_names[{i}]") for i, name in enumerate(names)),
    )


def _parse_windows_usage(
    raw: dict[str, Any], path: str, home_default_dir: str | None
) -> UsageSpec:
    usage = _object(raw, path, WINDOWS_USAGE_KEYS, WINDOWS_USAGE_KEYS)
    credential = _parse_oauth_credential(usage["credential"], f"{path}.credential")
    requests = _parse_usage_requests(usage["requests"], f"{path}.requests")
    names = [request.name for request in requests]
    fields_path = f"{path}.fields"
    fields_raw = _object(usage["fields"], fields_path, WINDOWS_FIELDS, WINDOWS_FIELDS)
    pointers = tuple(
        (key, _request_pointer(fields_raw[key], f"{fields_path}.{key}", names))
        for key in WINDOWS_FIELDS
    )
    return UsageSpec(
        snapshot="windows",
        credential=credential,
        proxy_from_environment=_bool(
            usage["proxy_from_environment"], f"{path}.proxy_from_environment"
        ),
        requests=requests,
        fields=pointers,
        label=_line(usage["label"], f"{path}.label"),
        poll_seconds=_bounded_int(
            usage["poll_seconds"], f"{path}.poll_seconds", 1, POLL_SECONDS_MAX
        ),
        account_file=_parse_account_file(
            usage["account_file"], f"{path}.account_file", home_default_dir
        ),
        window=_parse_window_fields(usage["window"], f"{path}.window"),
        limits=_parse_limit_fields(usage["limits"], f"{path}.limits"),
    )


def _parse_usage(raw: Any, path: str, home_default_dir: str | None) -> UsageSpec:
    if not isinstance(raw, dict):
        _fail(f"{path} must be an object")
    if raw.get("snapshot") == "windows":
        return _parse_windows_usage(raw, path, home_default_dir)
    usage = _object(raw, path, USAGE_KEYS, USAGE_KEYS)
    snapshot = _choice(usage["snapshot"], f"{path}.snapshot", USAGE_SNAPSHOTS)
    credential = _parse_usage_credential(usage["credential"], f"{path}.credential", snapshot)
    requests = _parse_usage_requests(usage["requests"], f"{path}.requests")
    names = [request.name for request in requests]
    keys = PERIOD_FIELDS if snapshot == "period" else SPEND_FIELDS
    fields_path = f"{path}.fields"
    fields_raw = _object(usage["fields"], fields_path, keys, keys)
    pointers: list[tuple[str, str]] = []
    products: ProductFields | None = None
    for key in keys:
        if key == "products":
            product_path = f"{fields_path}.products"
            raw_products = _object(
                fields_raw[key], product_path, PRODUCT_FIELD_KEYS, PRODUCT_FIELD_KEYS
            )
            products = ProductFields(
                list=_request_pointer(raw_products["list"], f"{product_path}.list", names),
                product=_pointer(raw_products["product"], f"{product_path}.product"),
                used_percent=_pointer(
                    raw_products["used_percent"], f"{product_path}.used_percent"
                ),
            )
            continue
        pointers.append(
            (key, _request_pointer(fields_raw[key], f"{fields_path}.{key}", names))
        )
    return UsageSpec(
        snapshot=snapshot,
        credential=credential,
        proxy_from_environment=_bool(
            usage["proxy_from_environment"], f"{path}.proxy_from_environment"
        ),
        requests=requests,
        fields=tuple(pointers),
        products=products,
    )


def _driver_id(value: Any, path: str) -> str:
    if not valid_driver_id(value):
        _fail(
            f"{path} must be a driver id (a letter or digit, then letters, digits, "
            "'-' or '_', at most 32 characters, not first or second)"
        )
    return value


def _parse_driver(driver_id: str, raw: Any) -> DriverSpec:
    path = f"drivers.{driver_id}"
    driver = _object(raw, path, DRIVER_KEYS)
    home_env = (
        _env_name(driver["home_env"], f"{path}.home_env")
        if driver.get("home_env") is not None
        else None
    )
    lane = (
        _parse_lane(driver["lane"], f"{path}.lane", home_env)
        if driver.get("lane") is not None
        else None
    )
    display_name: str | None = None
    if driver.get("display_name") is not None:
        display_name = _line(driver["display_name"], f"{path}.display_name", limit=100)
    elif lane is not None:
        _fail(f"{path}.display_name is required for a driver with a lane section")
    home_default_dir = (
        _home_default_dir(driver["home_default_dir"], f"{path}.home_default_dir")
        if driver.get("home_default_dir") is not None
        else None
    )
    return DriverSpec(
        id=driver_id,
        display_name=display_name,
        home_env=home_env,
        home_default_dir=home_default_dir,
        credential_env=(
            _env_names(driver["credential_env"], f"{path}.credential_env")
            if driver.get("credential_env") is not None
            else ()
        ),
        lane=lane,
        interactive=(
            _parse_interactive(driver["interactive"], f"{path}.interactive")
            if driver.get("interactive") is not None
            else None
        ),
        usage=(
            _parse_usage(driver["usage"], f"{path}.usage", home_default_dir)
            if driver.get("usage") is not None
            else None
        ),
    )


def _check_home_env_survives(drivers: Mapping[str, DriverSpec]) -> None:
    """Refuse a variable that would clear or replace a configured home variable.

    A role prefix sets the driver's ``home_env`` to the account's directory;
    the lane scrub, the fixed lane environment and the credential wrapper run
    after it. Any of them naming a home variable would move the driver off the
    configured account without a visible error.
    """
    owners: dict[str, str] = {}
    for spec in drivers.values():
        if spec.home_env is not None:
            owners.setdefault(spec.home_env, spec.id)
            if spec.home_env in NETWORK_OVERRIDE_ENV:
                _fail(
                    f"drivers.{spec.id}.home_env {spec.home_env} is a network "
                    "override that every lane clears"
                )

    def check(name: str, path: str) -> None:
        owner = owners.get(name)
        if owner is not None:
            _fail(f"{path} names home_env {name} of driver {owner}")

    for spec in drivers.values():
        path = f"drivers.{spec.id}"
        for i, name in enumerate(spec.credential_env):
            check(name, f"{path}.credential_env[{i}]")
        if spec.lane is None:
            continue
        for name, _value in spec.lane.env:
            check(name, f"{path}.lane.env.{name}")
        if spec.lane.credential_file is not None:
            check(spec.lane.credential_file.env, f"{path}.lane.credential_file.env")


def parse_drivers(raw: Any) -> dict[str, DriverSpec]:
    """Validate the ``drivers`` object; unknown keys fail at every level."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("drivers must be an object or null")
    drivers = {
        _driver_id(driver_id, "drivers key"): _parse_driver(driver_id, entry)
        for driver_id, entry in raw.items()
    }
    _check_home_env_survives(drivers)
    return drivers


def parse_recorded_slots(raw: Any) -> RecordedNames | None:
    if raw is None:
        return None
    slots = _object(raw, "recorded_slots", RECORDED_SLOT_KEYS, RECORDED_SLOT_KEYS)
    first = _driver_id(slots["first"], "recorded_slots.first")
    second = _driver_id(slots["second"], "recorded_slots.second")
    if first == second:
        _fail("recorded_slots.first and recorded_slots.second must differ")
    return RecordedNames(first, second)


def parse_driver_document(raw: Any) -> DriverConfiguration:
    """Validate the whole ``drivers.json`` document."""
    document = _object(raw, "drivers.json", DOCUMENT_KEYS)
    drivers = parse_drivers(document.get("drivers"))
    pane_driver: str | None = None
    if document.get("pane_driver") is not None:
        pane_driver = _driver_id(document["pane_driver"], "pane_driver")
        spec = drivers.get(pane_driver)
        if spec is None or spec.interactive is None:
            _fail(f"pane_driver {pane_driver} must name a driver with an interactive section")
    return DriverConfiguration(
        drivers=drivers,
        recorded_slots=parse_recorded_slots(document.get("recorded_slots")),
        pane_driver=pane_driver,
    )


# --- templates and lane argv ---------------------------------------------------


def expand_template(template: Sequence[str], values: Mapping[str, str | None]) -> list[str]:
    """Substitute whole-element placeholders; other elements stay literal."""
    out: list[str] = []
    for element in template:
        name = _placeholder(element)
        if name is None:
            out.append(element)
            continue
        value = values.get(name)
        if value is None:
            raise SystemExit(f"driver template needs a value for {{{name}}}")
        out.append(value)
    return out


def require_lane(spec: DriverSpec) -> LaneSpec:
    """The lane section; a launch site refuses other drivers with ``driver_refusal``."""
    if spec.lane is None:
        raise SystemExit(
            DRIVER_REFUSED.format(name="vendor", choices="a driver with a lane section")
        )
    return spec.lane


def require_interactive(spec: DriverSpec) -> InteractiveSpec:
    """The interactive section; a start refuses other drivers with ``driver_refusal``."""
    if spec.interactive is None:
        raise SystemExit(
            DRIVER_REFUSED.format(
                name="provider", choices="a driver with an interactive section"
            )
        )
    return spec.interactive


def lane_io(spec: DriverSpec, *, tmux: bool) -> LaneIO:
    """Derive spec passing, output source and tmux redirects for one lane."""
    lane = require_lane(spec)
    stdin = lane.spec == "stdin"
    wrapped = lane.credential_file is not None
    output_file = tmux or wrapped or lane.output == "last-message-file"
    redirect: str | None = None
    redirect_args: tuple[str, ...] = ()
    if tmux:
        if wrapped:
            # The credential wrapper already writes the stream to the output file.
            if stdin:
                redirect, redirect_args = _REDIRECT_SPEC, ("spec_file",)
        elif lane.output == "event-stream":
            if stdin:
                redirect = _REDIRECT_SPEC_AND_STREAM
                redirect_args = ("spec_file", "output_file")
            else:
                redirect, redirect_args = _REDIRECT_STREAM, ("output_file",)
        # A last-message-file lane writes its own output file; a stdin spec is
        # typed into the pane.
    if redirect is not None:
        stdin = False
    return LaneIO(
        output=lane.output,
        output_file=output_file,
        stdin=stdin,
        audit_stderr_when_empty=lane.audit_stderr_when_empty,
        redirect=redirect,
        redirect_args=redirect_args,
    )


def redirect_argv(
    io: LaneIO, argv: Sequence[str], *, spec_file: str, output_file: str | None
) -> list[str]:
    """Wrap ``argv`` in the tmux redirect of ``io``; unchanged without one."""
    if io.redirect is None:
        return list(argv)
    values = {"spec_file": spec_file, "output_file": output_file}
    args: list[str] = []
    for name in io.redirect_args:
        value = values[name]
        if not value:
            raise SystemExit(f"tmux lane redirect needs {name}")
        args.append(value)
    return ["sh", "-c", io.redirect, "sh", *args, *argv]


def credential_wrapper(spec: DriverSpec) -> str:
    """Shell text that exports the key file's content, then runs the lane.

    The key never appears in argv. Positional arguments: the output file, then
    the lane argv. A missing, unreadable or empty key file ends the lane as
    unavailable before the driver starts.
    """
    lane = require_lane(spec)
    credential = lane.credential_file
    if credential is None or spec.home_env is None:
        raise SystemExit(f"driver {spec.id} has no lane.credential_file")
    home = spec.home_env
    missing = (
        f"printf '%s\\n' 'STATUS: unavailable' '{spec.id} key missing' >&2; exit 2"
    )
    return (
        f"out=$1; shift; key_file=${{{home}:+${home}/{credential.file}}}; "
        f'if [ -z "$key_file" ] || [ ! -r "$key_file" ]; then {missing}; fi; '
        f'key=$(cat "$key_file" 2>/dev/null) || {{ {missing}; }}; '
        f'if [ -z "$key" ]; then {missing}; fi; '
        f'export {credential.env}="$key"; exec "$@" > "$out"'
    )


def _child_argv(
    spec: DriverSpec,
    inner: list[str],
    *,
    scrub: Sequence[str],
    output_file: str | None,
) -> list[str]:
    lane = require_lane(spec)
    argv = ["env"]
    for key in scrub:
        argv.extend(["-u", key])
    argv.extend(f"{name}={value}" for name, value in lane.env)
    if lane.credential_file is not None:
        argv.extend(["sh", "-c", credential_wrapper(spec), "sh", str(output_file)])
    argv.extend(inner)
    return argv


def _require_output_file(spec: DriverSpec, output_file: str | None) -> None:
    lane = require_lane(spec)
    needs = lane.output == "last-message-file" or lane.credential_file is not None
    if needs and not output_file:
        raise SystemExit(OUTPUT_FILE_REQUIRED.format(driver=spec.id))


def lane_argv(
    spec: DriverSpec,
    mode: str,
    *,
    scrub: Sequence[str],
    spec_file: str,
    cwd: str,
    model: str,
    output_file: str | None = None,
    read_dirs: Sequence[str] = (),
) -> list[str]:
    """Build a read or write lane: scrub prefix, fixed env, wrapper, template.

    ``read_dirs`` are passed only when the driver configures ``read_dir_args``;
    such a driver refuses extra directories on a write lane.
    """
    lane = require_lane(spec)
    if mode not in LANE_MODES:
        raise SystemExit(f"lane mode must be {'|'.join(LANE_MODES)}")
    _require_output_file(spec, output_file)
    if mode == "write" and read_dirs and lane.read_dir_args is not None:
        raise SystemExit(WRITE_READ_DIRS_REFUSED)
    values = {
        "model": model,
        "cwd": cwd,
        "spec_file": spec_file,
        "output_file": output_file,
    }
    inner = expand_template(lane.argv_read if mode == "read" else lane.argv_write, values)
    if lane.read_dir_args is not None:
        for directory in read_dirs:
            inner.extend(expand_template(lane.read_dir_args, {"dir": directory}))
    return _child_argv(spec, inner, scrub=scrub, output_file=output_file)


def judgment_argv(
    spec: DriverSpec,
    *,
    scrub: Sequence[str],
    spec_file: str,
    cwd: str,
    model: str,
    output_file: str | None = None,
) -> list[str]:
    """argv of a lane without tools and without file access, for one judgment."""
    lane = require_lane(spec)
    if lane.argv_judgment is None:
        raise SystemExit(JUDGMENT_UNSUPPORTED.format(driver=spec.id))
    _require_output_file(spec, output_file)
    inner = expand_template(
        lane.argv_judgment,
        {"model": model, "cwd": cwd, "spec_file": spec_file, "output_file": output_file},
    )
    return _child_argv(spec, inner, scrub=scrub, output_file=output_file)


def _contains_group(argv: Sequence[str], group: Sequence[str]) -> bool:
    size = len(group)
    return any(
        list(argv[i : i + size]) == list(group) for i in range(len(argv) - size + 1)
    )


def harden_write_argv(argv: Sequence[str], spec: DriverSpec) -> list[str]:
    """Append each configured hardening group that the argv does not contain.

    Process argv hardening for the coordinator implementer, not a sandbox.
    """
    out = list(argv)
    groups = spec.lane.write_hardening if spec.lane is not None else ()
    for group in groups:
        if not _contains_group(out, group):
            out.extend(group)
    return out


# --- environment scrubbing ------------------------------------------------------


def _ordered_union(groups: Sequence[Sequence[str]]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for group in groups:
        for name in group:
            seen.setdefault(name, None)
    return tuple(seen)


def credential_scrub(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Every configured credential variable, in configuration order."""
    return _ordered_union([spec.credential_env for spec in drivers.values()])


def home_env_names(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Every configured configuration-directory variable, in configuration order."""
    return _ordered_union(
        [(spec.home_env,) for spec in drivers.values() if spec.home_env is not None]
    )


def lane_scrub_env(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Variables cleared for every lane and every lane pane.

    This is ``role_clear_env`` without the home variables. The role prefix runs
    first and already clears them, so this list adds ``-u`` pairs to the argv
    but never changes the child's environment.
    """
    return _ordered_union([credential_scrub(drivers), NETWORK_OVERRIDE_ENV])


def role_clear_env(drivers: Mapping[str, DriverSpec]) -> tuple[str, ...]:
    """Variables a role prefix clears before it sets its own home variable."""
    return _ordered_union(
        [credential_scrub(drivers), NETWORK_OVERRIDE_ENV, home_env_names(drivers)]
    )


def credential_dir_overlap_error(
    spec: DriverSpec, config_dir: str, cwd: str, read_dirs: Sequence[str] = ()
) -> str | None:
    """Refuse lane roots that overlap the directory holding a key file."""
    if spec.lane is None or spec.lane.credential_file is None:
        return None
    config = Path(config_dir).expanduser().resolve()
    for label, raw in (("worktree", cwd), *(("read directory", d) for d in read_dirs)):
        path = Path(raw).expanduser().resolve()
        try:
            path.relative_to(config)
            return f"{spec.id} {label} overlaps account config_dir"
        except ValueError:
            pass
        try:
            config.relative_to(path)
            return f"{spec.id} account config_dir overlaps {label}"
        except ValueError:
            pass
    return None


def is_home_default_dir(spec: DriverSpec, config_dir: str) -> bool:
    """True when ``config_dir`` is the driver CLI's own default directory."""
    if spec.home_default_dir is None:
        return False
    try:
        return (
            Path(config_dir).expanduser().resolve()
            == Path(spec.home_default_dir).expanduser().resolve()
        )
    except OSError:
        return False
