"""Fail-closed configuration of the runner (docs/platform/spec/05-runner.md section 1.3, ADR-027 section 7).

Every setting is an environment variable `AGENT_RUNNER_<NAME>`, read once at start. A missing required or
invalid value raises `RunnerError("config_invalid", "<NAME>")`; the value itself is never echoed. The runner
supports macOS only (R-3.6): any other platform raises `RunnerError("unsupported_os")` before a variable is read.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from agent_runner.encoding import b64u_decode
from agent_runner.errors import RunnerError

PREFIX = "AGENT_RUNNER_"
SUPPORTED_PLATFORM = "darwin"
DEFAULT_HOME_RELATIVE = ".local/share/agent-runner"
DEFAULT_HOME = "~/" + DEFAULT_HOME_RELATIVE
DEFAULT_LOGIN_ARGV = ("claude", "setup-token")
OPERATOR_GITHUB_ONE_USER = "OPERATOR_GITHUB_USERS:one_user_only"
"""The error name of an `OPERATOR_GITHUB_USERS` with more than one user (`doctor` and the daemon refuse it)."""
DeviceKind = Literal["org", "platform"]

# Every name of section 1.3 plus VM_PREFIX (ADR-027 section 7). A variable with the prefix that is not listed is
# refused, so a typo never silently falls back to a default.
NAMES = (
    "HOME",
    "HUB_URL",
    "PLATFORM_ROOT_KEY",
    "VM_CPUS",
    "VM_MEMORY_MIB",
    "VM_DISK_GIB",
    "POOL_SIZE",
    "MEMORY_RESERVE_MIB",
    "SESSIONS_PER_VM",
    "MAX_VMS",
    "SESSION_IDLE_SECONDS",
    "VM_IDLE_SECONDS",
    "TEXTFILE_DIR",
    "MODEL_CATALOG",
    "OPENROUTER_KEY_FILE",
    "GITHUB_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_APP_SLUG",
    "GITHUB_API_URL",
    "GITHUB_WEB_URL",
    "MODEL_UPSTREAM_ANTHROPIC",
    "MODEL_UPSTREAM_OPENROUTER",
    "ANTHROPIC_USAGE_URL",
    "LOGIN_ARGV",
    "LOGIN_AUTOMATION",
    "GOLDEN_TEMPLATE",
    "TEST_MODE",
    "VM_PREFIX",
    "OPERATOR_SUBSCRIPTION_DIR",
    "OPERATOR_SUBSCRIPTION_ORGS",
    "OPERATOR_GITHUB_TOKEN_FILE",
    "OPERATOR_GITHUB_USERS",
    # Work VMs (docs/platform/spec/13-work-vm.md, ADR-059)
    "WORKVM_SLOTS",
    "WORKVM_SLOTS_SELF",
    "WORKVM_MAX_SIZE",
    "WORKVM_IDLE_SECONDS",
    "WORKVM_POOL_SIZE",
    "WORKVM_DISK_QUOTA_GIB",
    "WORKVM_QUEUE_SECONDS",
    "WORKVM_MAX_PER_TASK",
    "WORKVM_MAX_PER_ORG",
    "WORKVM_MAX_PER_OWNER",
    "WORKVM_SSH_PORT_BASE",
    "WORKVM_TEMPLATE",
    "CONTEXT_INTERVAL_SECONDS",
    "CONTEXT_CACHE_MIB",
    "CONTEXT_WORKSPACE_MIB",
    "CONTEXT_TRANSCRIPT_MIB",
    "CONTEXT_WAIT_SECONDS",
)
# Required for the daemon (`run`, `service install`); commands that never talk to the hub load without them.
HUB_NAMES = frozenset({"HUB_URL", "PLATFORM_ROOT_KEY"})

# name: (default, minimum, maximum)
_INTEGERS: dict[str, tuple[int, int, int]] = {
    "VM_CPUS": (4, 1, 256),
    "VM_MEMORY_MIB": (8192, 1024, 4_194_304),
    "VM_DISK_GIB": (20, 5, 65_536),
    "POOL_SIZE": (2, 0, 64),
    "MEMORY_RESERVE_MIB": (32768, 0, 4_194_304),
    # ADR-075 section 1: occupying sessions per VM and VMs of the device that occupy a slot; 0 = no such limit
    "SESSIONS_PER_VM": (16, 0, 1_000_000),
    "MAX_VMS": (0, 0, 1_000_000),
    "SESSION_IDLE_SECONDS": (1800, 60, 604_800),
    "VM_IDLE_SECONDS": (1800, 60, 604_800),
    "WORKVM_SLOTS": (0, 0, 100),
    "WORKVM_IDLE_SECONDS": (1800, 60, 604_800),
    "WORKVM_POOL_SIZE": (1, 0, 16),
    "WORKVM_DISK_QUOTA_GIB": (0, 0, 1_000_000),
    "WORKVM_QUEUE_SECONDS": (900, 0, 86_400),
    "WORKVM_MAX_PER_TASK": (4, 1, 100),
    "WORKVM_MAX_PER_ORG": (16, 1, 100),
    "WORKVM_MAX_PER_OWNER": (8, 1, 100),
    "WORKVM_SSH_PORT_BASE": (47200, 1024, 65_435),
    # KOR-33 context checkpoints (ADR-072 sections 4 and 6): generous defaults, never a precondition.
    "CONTEXT_INTERVAL_SECONDS": (600, 30, 86_400),
    "CONTEXT_CACHE_MIB": (20_480, 0, 999_999_999),
    "CONTEXT_WORKSPACE_MIB": (4096, 0, 999_999_999),
    "CONTEXT_TRANSCRIPT_MIB": (4096, 1, 999_999_999),
    "CONTEXT_WAIT_SECONDS": (180, 0, 3600),
}
WORKVM_SIZES = ("standard", "large", "xlarge")
_URL_DEFAULTS = {
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_WEB_URL": "https://github.com",
    "MODEL_UPSTREAM_ANTHROPIC": "https://api.anthropic.com",
    "MODEL_UPSTREAM_OPENROUTER": "https://openrouter.ai/api",
    "ANTHROPIC_USAGE_URL": "https://api.anthropic.com/api/oauth/usage",
}
_DIGITS_RE = re.compile(r"^[0-9]{1,9}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
# Lima instance names start with a letter and use [A-Za-z0-9._-]; the prefix is lower case and ends the
# instance name's first segment, e.g. `wp-run01-`.
_VM_PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31})?$")
_LOOPBACK_NAMES = frozenset({"localhost"})
_ED25519_PUBLIC_LENGTH = 32


@dataclass(frozen=True)
class Config:
    """The validated runner configuration. `None` means "not set" (or "packaged default" for files)."""

    home: Path
    hub_url: str | None
    platform_root_key: str | None
    vm_cpus: int
    vm_memory_mib: int
    vm_disk_gib: int
    pool_size: int
    memory_reserve_mib: int
    session_idle_seconds: int
    vm_idle_seconds: int
    textfile_dir: Path | None
    model_catalog: Path | None
    openrouter_key_file: Path | None
    github_client_id: str | None
    github_api_url: str
    github_web_url: str
    model_upstream_anthropic: str
    model_upstream_openrouter: str
    anthropic_usage_url: str
    login_argv: tuple[str, ...]
    login_automation: bool
    # True when LOGIN_AUTOMATION was set explicitly (otherwise the default follows the device kind).
    login_automation_set: bool
    golden_template: Path | None
    test_mode: bool
    vm_prefix: str
    operator_subscription_dir: Path | None = None
    """Directory of the operator's subscription token files (platform devices only; KOR-23, ADR-052)."""
    operator_subscription_orgs: frozenset[str] = frozenset()
    """Organizations whose sessions may use the operator's subscriptions; only together with the directory."""
    operator_github_token_file: Path | None = None
    """The operator's GitHub token file (platform devices only; KOR-27, ADR-053)."""
    operator_github_users: frozenset[str] = frozenset()
    """Users whose `github.connect` uses the operator's token; only together with the file."""
    context_interval_seconds: int = 600
    """Checkpoint interval of a working main session (KOR-33, ADR-072 section 4)."""
    context_cache_mib: int = 20_480
    """Size cap of the local encrypted checkpoint cache `<home>/contexts/`."""
    context_workspace_mib: int = 4096
    """A workspace above this size is left out of a checkpoint (the transcript is still saved)."""
    context_transcript_mib: int = 4096
    """A transcript above this size cannot be checkpointed (audited, never silent)."""
    context_wait_seconds: int = 180
    """How long a restore waits for the final checkpoint of the session it continues and retries the hub."""
    sessions_per_vm: int = 16
    """Occupying sessions per VM, 0 = no limit (KOR-41, ADR-075 section 1)."""
    max_vms: int = 0
    """VMs of the device that occupy a slot, 0 = only the memory budget decides (KOR-41, ADR-075 section 1)."""
    github_oauth_client_id: str | None = None
    """Public client ID of the OAuth App whose device flow "Connect GitHub" prefers (KOR-65, ADR-015 addendum)."""
    github_app_slug: str | None = None
    """URL name of the GitHub App (`https://github.com/apps/<slug>`), for the install link of a next step (KOR-65)."""

    @property
    def pg_dir(self) -> Path:
        """Directory of the runner's own PostgreSQL cluster (section 1.4)."""
        return self.home / "pg"

    @property
    def log_dir(self) -> Path:
        return self.home / "log"

    @property
    def tmp_dir(self) -> Path:
        return self.home / "tmp"

    workvm_slots: int = 0
    workvm_slots_self: bool = False
    workvm_max_size: str = "large"
    workvm_idle_seconds: int = 1800
    workvm_pool_size: int = 1
    workvm_disk_quota_gib: int = 0
    workvm_queue_seconds: int = 900
    workvm_max_per_task: int = 4
    workvm_max_per_org: int = 16
    workvm_max_per_owner: int = 8
    workvm_ssh_port_base: int = 47200
    workvm_template: Path | None = None

    @property
    def contexts_dir(self) -> Path:
        """The encrypted checkpoint cache (KOR-33)."""
        return self.home / "contexts"


def check_platform(platform: str) -> None:
    """Refuse every host except macOS (R-3.6)."""
    if platform != SUPPORTED_PLATFORM:
        raise RunnerError("unsupported_os")


def load_config(
    env: Mapping[str, str],
    *,
    platform: str,
    device_kind: DeviceKind | None = None,
    require: frozenset[str] = HUB_NAMES,
    expand_user: Callable[[str], str] = os.path.expanduser,
) -> Config:
    """Validate `AGENT_RUNNER_*` from `env`.

    `device_kind` is the kind recorded in the device identity (None before enrollment); it selects the
    `LOGIN_AUTOMATION` default and refuses `OPENROUTER_KEY_FILE` on org devices. `require` names the variables
    that must be set (default: the hub settings needed by the daemon).
    """
    check_platform(platform)
    for key in sorted(env):
        if key.startswith(PREFIX) and key[len(PREFIX) :] not in NAMES:
            raise RunnerError("config_invalid", key[len(PREFIX) :])

    def raw(name: str) -> str:
        return env.get(PREFIX + name, "")

    test_mode = _flag(raw("TEST_MODE"), "TEST_MODE")
    hub_url = _optional(raw("HUB_URL"), "HUB_URL", require, lambda v: _url(v, "HUB_URL", test_mode))
    root_key = _optional(raw("PLATFORM_ROOT_KEY"), "PLATFORM_ROOT_KEY", require, _root_key)
    integers = {name: _integer(raw(name), name, *limits) for name, limits in _INTEGERS.items()}
    urls = {name: _url(raw(name) or default, name, test_mode) for name, default in _URL_DEFAULTS.items()}
    openrouter = _optional(raw("OPENROUTER_KEY_FILE"), "OPENROUTER_KEY_FILE", require, _key_file)
    if openrouter is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPENROUTER_KEY_FILE")
    operator_dir, operator_orgs = _operator(raw, require, device_kind)
    github_file, github_users = _operator_github(raw, require, device_kind)
    automation_raw = raw("LOGIN_AUTOMATION") or ("on" if device_kind == "platform" else "off")
    if automation_raw not in ("on", "off"):
        raise RunnerError("config_invalid", "LOGIN_AUTOMATION")
    return Config(
        home=_home(raw("HOME") or DEFAULT_HOME, expand_user),
        hub_url=hub_url,
        platform_root_key=root_key,
        vm_cpus=integers["VM_CPUS"],
        vm_memory_mib=integers["VM_MEMORY_MIB"],
        vm_disk_gib=integers["VM_DISK_GIB"],
        pool_size=integers["POOL_SIZE"],
        memory_reserve_mib=integers["MEMORY_RESERVE_MIB"],
        sessions_per_vm=integers["SESSIONS_PER_VM"],
        max_vms=integers["MAX_VMS"],
        session_idle_seconds=integers["SESSION_IDLE_SECONDS"],
        vm_idle_seconds=integers["VM_IDLE_SECONDS"],
        textfile_dir=_optional(raw("TEXTFILE_DIR"), "TEXTFILE_DIR", require, lambda v: _absolute(v, "TEXTFILE_DIR")),
        model_catalog=_optional(raw("MODEL_CATALOG"), "MODEL_CATALOG", require, lambda v: _file(v, "MODEL_CATALOG")),
        openrouter_key_file=openrouter,
        github_client_id=_optional(raw("GITHUB_CLIENT_ID"), "GITHUB_CLIENT_ID", require, _client_id),
        github_oauth_client_id=_optional(
            raw("GITHUB_OAUTH_CLIENT_ID"),
            "GITHUB_OAUTH_CLIENT_ID",
            require,
            lambda v: _client_id(v, "GITHUB_OAUTH_CLIENT_ID"),
        ),
        github_app_slug=_optional(raw("GITHUB_APP_SLUG"), "GITHUB_APP_SLUG", require, _app_slug),
        github_api_url=urls["GITHUB_API_URL"],
        github_web_url=urls["GITHUB_WEB_URL"],
        model_upstream_anthropic=urls["MODEL_UPSTREAM_ANTHROPIC"],
        model_upstream_openrouter=urls["MODEL_UPSTREAM_OPENROUTER"],
        anthropic_usage_url=urls["ANTHROPIC_USAGE_URL"],
        login_argv=_login_argv(raw("LOGIN_ARGV")),
        login_automation=automation_raw == "on",
        login_automation_set=raw("LOGIN_AUTOMATION") != "",
        golden_template=_optional(
            raw("GOLDEN_TEMPLATE"), "GOLDEN_TEMPLATE", require, lambda v: _file(v, "GOLDEN_TEMPLATE")
        ),
        test_mode=test_mode,
        vm_prefix=_vm_prefix(raw("VM_PREFIX")),
        operator_subscription_dir=operator_dir,
        operator_subscription_orgs=operator_orgs,
        operator_github_token_file=github_file,
        operator_github_users=github_users,
        **_workvm(raw, require, integers, test_mode),
        context_interval_seconds=integers["CONTEXT_INTERVAL_SECONDS"],
        context_cache_mib=integers["CONTEXT_CACHE_MIB"],
        context_workspace_mib=integers["CONTEXT_WORKSPACE_MIB"],
        context_transcript_mib=integers["CONTEXT_TRANSCRIPT_MIB"],
        context_wait_seconds=integers["CONTEXT_WAIT_SECONDS"],
    )


class _WorkVmSettings(TypedDict):
    workvm_slots: int
    workvm_slots_self: bool
    workvm_max_size: str
    workvm_idle_seconds: int
    workvm_pool_size: int
    workvm_disk_quota_gib: int
    workvm_queue_seconds: int
    workvm_max_per_task: int
    workvm_max_per_org: int
    workvm_max_per_owner: int
    workvm_ssh_port_base: int
    workvm_template: Path | None


def _workvm(
    raw: Callable[[str], str], require: frozenset[str], integers: Mapping[str, int], test_mode: bool
) -> _WorkVmSettings:
    """The work VM settings (spec 11 sections 6 and 7). `WORKVM_SLOTS_SELF` (every slot is the runner's own account,
    no host filter) exists only in `TEST_MODE`;
    egress goes through the device's own Cloudflare client (section 6.3), so there is no egress setting."""
    slots_self = _flag(raw("WORKVM_SLOTS_SELF"), "WORKVM_SLOTS_SELF")
    if slots_self and not test_mode:
        raise RunnerError("config_invalid", "WORKVM_SLOTS_SELF")
    max_size = raw("WORKVM_MAX_SIZE") or "large"
    if max_size not in WORKVM_SIZES:
        raise RunnerError("config_invalid", "WORKVM_MAX_SIZE")
    template = _optional(raw("WORKVM_TEMPLATE"), "WORKVM_TEMPLATE", require, lambda v: _file(v, "WORKVM_TEMPLATE"))
    return {
        "workvm_slots": integers["WORKVM_SLOTS"],
        "workvm_slots_self": slots_self,
        "workvm_max_size": max_size,
        "workvm_idle_seconds": integers["WORKVM_IDLE_SECONDS"],
        "workvm_pool_size": integers["WORKVM_POOL_SIZE"],
        "workvm_disk_quota_gib": integers["WORKVM_DISK_QUOTA_GIB"],
        "workvm_queue_seconds": integers["WORKVM_QUEUE_SECONDS"],
        "workvm_max_per_task": integers["WORKVM_MAX_PER_TASK"],
        "workvm_max_per_org": integers["WORKVM_MAX_PER_ORG"],
        "workvm_max_per_owner": integers["WORKVM_MAX_PER_OWNER"],
        "workvm_ssh_port_base": integers["WORKVM_SSH_PORT_BASE"],
        "workvm_template": template,
    }


def environment(config: Config) -> dict[str, str]:
    """The `AGENT_RUNNER_*` variables that reproduce `config` (used for the LaunchDaemon environment).

    Only settings that differ from their default are written, plus `HOME`. None of them is a secret: URLs, the
    platform root public key, paths, numbers and switches.
    """
    out = {PREFIX + "HOME": str(config.home)}
    optional_text: dict[str, str | Path | None] = {
        "HUB_URL": config.hub_url,
        "PLATFORM_ROOT_KEY": config.platform_root_key,
        "TEXTFILE_DIR": config.textfile_dir,
        "MODEL_CATALOG": config.model_catalog,
        "OPENROUTER_KEY_FILE": config.openrouter_key_file,
        "GITHUB_CLIENT_ID": config.github_client_id,
        "GITHUB_OAUTH_CLIENT_ID": config.github_oauth_client_id,
        "GITHUB_APP_SLUG": config.github_app_slug,
        "GOLDEN_TEMPLATE": config.golden_template,
        "VM_PREFIX": config.vm_prefix or None,
        "OPERATOR_SUBSCRIPTION_DIR": config.operator_subscription_dir,
        "OPERATOR_SUBSCRIPTION_ORGS": ",".join(sorted(config.operator_subscription_orgs)) or None,
        "OPERATOR_GITHUB_TOKEN_FILE": config.operator_github_token_file,
        "OPERATOR_GITHUB_USERS": ",".join(sorted(config.operator_github_users)) or None,
        "WORKVM_MAX_SIZE": None if config.workvm_max_size == "large" else config.workvm_max_size,
        "WORKVM_TEMPLATE": config.workvm_template,
    }
    for name, value in optional_text.items():
        if value is not None:
            out[PREFIX + name] = str(value)
    for name, (default, _, _) in _INTEGERS.items():
        value_int = getattr(config, name.lower())
        if value_int != default:
            out[PREFIX + name] = str(value_int)
    for name, default_url in _URL_DEFAULTS.items():
        url = getattr(config, name.lower())
        if url != default_url:
            out[PREFIX + name] = url
    if config.login_argv != DEFAULT_LOGIN_ARGV:
        out[PREFIX + "LOGIN_ARGV"] = json.dumps(list(config.login_argv))
    if config.login_automation_set:
        out[PREFIX + "LOGIN_AUTOMATION"] = "on" if config.login_automation else "off"
    if config.test_mode:
        out[PREFIX + "TEST_MODE"] = "1"
    if config.workvm_slots_self:
        out[PREFIX + "WORKVM_SLOTS_SELF"] = "1"
    return out


def _optional[T](value: str, name: str, require: frozenset[str], parse: Callable[[str], T]) -> T | None:
    if value == "":
        if name in require:
            raise RunnerError("config_invalid", name)
        return None
    return parse(value)


def _flag(value: str, name: str) -> bool:
    if value not in ("", "0", "1"):
        raise RunnerError("config_invalid", name)
    return value == "1"


def _integer(value: str, name: str, default: int, minimum: int, maximum: int) -> int:
    if value == "":
        return default
    if not _DIGITS_RE.fullmatch(value):
        raise RunnerError("config_invalid", name)
    number = int(value)
    if not minimum <= number <= maximum:
        raise RunnerError("config_invalid", name)
    return number


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url(value: str, name: str, test_mode: bool) -> str:
    """An `https://` URL without credentials, query or fragment; loopback `http://` only in test mode."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise RunnerError("config_invalid", name) from None
    if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise RunnerError("config_invalid", name)
    if any(ch.isspace() for ch in value) or port == 0:
        raise RunnerError("config_invalid", name)
    if parts.scheme == "https":
        return value.rstrip("/")
    if parts.scheme == "http" and test_mode and _is_loopback(host):
        return value.rstrip("/")
    raise RunnerError("config_invalid", name)


def _root_key(value: str) -> str:
    try:
        raw_key = b64u_decode(value)
    except ValueError:
        raise RunnerError("config_invalid", "PLATFORM_ROOT_KEY") from None
    if len(raw_key) != _ED25519_PUBLIC_LENGTH:
        raise RunnerError("config_invalid", "PLATFORM_ROOT_KEY")
    return value


def _home(value: str, expand_user: Callable[[str], str]) -> Path:
    """Absolute home; when it exists it must be a real directory with mode 0700 (no group or other bits)."""
    return check_home(Path(expand_user(value)))


def check_home(path: Path) -> Path:
    """Refuse a relative home and an existing home that is not a real 0700-style directory."""
    if not path.is_absolute():
        raise RunnerError("config_invalid", "HOME")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    except OSError:
        raise RunnerError("config_invalid", "HOME") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise RunnerError("config_invalid", "HOME")
    return path


def _absolute(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RunnerError("config_invalid", name)
    return path


def _file(value: str, name: str) -> Path:
    path = _absolute(value, name)
    if not path.is_file():
        raise RunnerError("config_invalid", name)
    return path


def _key_file(value: str) -> Path:
    """A regular file (not a symlink) readable only by its owner (0600 or stricter)."""
    path = _absolute(value, "OPENROUTER_KEY_FILE")
    try:
        info = path.lstat()
    except OSError:
        raise RunnerError("config_invalid", "OPENROUTER_KEY_FILE") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise RunnerError("config_invalid", "OPENROUTER_KEY_FILE")
    return path


def _operator(
    raw: Callable[[str], str], require: frozenset[str], device_kind: DeviceKind | None
) -> tuple[Path | None, frozenset[str]]:
    """`OPERATOR_SUBSCRIPTION_DIR` and `OPERATOR_SUBSCRIPTION_ORGS` (KOR-23): both or neither, platform devices only."""
    directory = _optional(raw("OPERATOR_SUBSCRIPTION_DIR"), "OPERATOR_SUBSCRIPTION_DIR", require, _operator_dir)
    orgs = _org_ids(raw("OPERATOR_SUBSCRIPTION_ORGS"))
    if directory is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_DIR")
    if directory is not None and not orgs:
        raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_ORGS")
    if orgs and directory is None:
        raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_DIR")
    return directory, orgs


def _operator_dir(value: str) -> Path:
    """A directory (not a symlink) with no rights for group and others (0700); its owner is checked at each scan."""
    path = _absolute(value, "OPERATOR_SUBSCRIPTION_DIR")
    try:
        info = path.lstat()
    except OSError:
        raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_DIR") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_DIR")
    return path


def _org_ids(value: str) -> frozenset[str]:
    """Comma-separated organization IDs (UUIDs, normalized to lower case); empty = nobody."""
    if not value.strip():
        return frozenset()
    found: set[str] = set()
    for part in value.split(","):
        try:
            parsed = uuid.UUID(part.strip())
        except ValueError:
            raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_ORGS") from None
        found.add(str(parsed))
    return frozenset(found)


def _operator_github(
    raw: Callable[[str], str], require: frozenset[str], device_kind: DeviceKind | None
) -> tuple[Path | None, frozenset[str]]:
    """`OPERATOR_GITHUB_TOKEN_FILE` and `OPERATOR_GITHUB_USERS` (KOR-27): both or neither, platform devices only.
    The token is the operator's own GitHub connection, so the list names exactly one user - the operator; more are
    `config_invalid` `OPERATOR_GITHUB_USERS:one_user_only` (a personal connection is never shared, HOT-14, ADR-053).

    Only the path is checked here: the file is replaced atomically while the daemon runs, so its type, owner, mode
    and content are checked at each read (`github.operator.read_token`)."""
    path = _optional(
        raw("OPERATOR_GITHUB_TOKEN_FILE"),
        "OPERATOR_GITHUB_TOKEN_FILE",
        require,
        lambda v: _absolute(v, "OPERATOR_GITHUB_TOKEN_FILE"),
    )
    users = _user_ids(raw("OPERATOR_GITHUB_USERS"))
    if path is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_TOKEN_FILE")
    if users and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_USERS")
    if path is not None and not users:
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_USERS")
    if users and path is None:
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_TOKEN_FILE")
    if len(users) > 1:
        raise RunnerError("config_invalid", OPERATOR_GITHUB_ONE_USER)
    return path, users


def _user_ids(value: str) -> frozenset[str]:
    """Comma-separated user IDs (UUIDs, normalized to lower case); empty = nobody."""
    if not value.strip():
        return frozenset()
    found: set[str] = set()
    for part in value.split(","):
        try:
            found.add(str(uuid.UUID(part.strip())))
        except ValueError:
            raise RunnerError("config_invalid", "OPERATOR_GITHUB_USERS") from None
    return frozenset(found)


def _client_id(value: str, name: str = "GITHUB_CLIENT_ID") -> str:
    if not _CLIENT_ID_RE.fullmatch(value):
        raise RunnerError("config_invalid", name)
    return value


def _app_slug(value: str) -> str:
    if not _APP_SLUG_RE.fullmatch(value):
        raise RunnerError("config_invalid", "GITHUB_APP_SLUG")
    return value


def _login_argv(value: str) -> tuple[str, ...]:
    if value == "":
        return DEFAULT_LOGIN_ARGV
    try:
        parsed = json.loads(value)
    except ValueError:
        raise RunnerError("config_invalid", "LOGIN_ARGV") from None
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(item, str) and item and "\x00" not in item for item in parsed)
    ):
        raise RunnerError("config_invalid", "LOGIN_ARGV")
    return tuple(parsed)


def _vm_prefix(value: str) -> str:
    if not _VM_PREFIX_RE.fullmatch(value):
        raise RunnerError("config_invalid", "VM_PREFIX")
    return value
