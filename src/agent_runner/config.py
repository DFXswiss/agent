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
DEFAULT_SUBSCRIPTION_LOGIN_ARGV = ("claude", "auth", "login", "--claudeai")
"""The full login of a further subscription (ADR-092 section 1); `--email <address>` is appended when known."""
DEFAULT_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
DEFAULT_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
"""Token endpoint and client ID the pinned Claude Code renews its login with (ADR-092 section 2)."""
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
    "VM_LOAD_MAX_PERCENT",
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
    "SUBSCRIPTION_LOGIN_ARGV",
    "CLAUDE_TOKEN_URL",
    "CLAUDE_OAUTH_CLIENT_ID",
    "LOGIN_AUTOMATION",
    "GOLDEN_TEMPLATE",
    "TEST_MODE",
    "VM_PREFIX",
    "OPERATOR_SUBSCRIPTION_DIR",
    "OPERATOR_SUBSCRIPTION_ORGS",
    "OPERATOR_GITHUB_TOKEN_FILE",
    "OPERATOR_GITHUB_USERS",
    "OPERATOR_GITHUB_ORG",
    # Work VMs (docs/platform/spec/13-work-vm.md, ADR-059)
    "WORKVM_SLOTS_SELF",
    "WORKVM_MAX_SIZE",
    "WORKVM_IDLE_SECONDS",
    "WORKVM_KEEP_STOPPED_SECONDS",
    "CPU_LOAD_MAX_PERCENT",
    "WORKVM_POOL_SIZE",
    "WORKVM_DISK_QUOTA_GIB",
    "WORKVM_QUEUE_SECONDS",
    "WORKVM_SSH_PORT_BASE",
    "WORKVM_TEMPLATE",
    "LOG_MAX_MIB",
    "CONTEXT_INTERVAL_SECONDS",
    "CONTEXT_CACHE_MIB",
    "CONTEXT_WORKSPACE_MIB",
    "CONTEXT_TRANSCRIPT_MIB",
    "CONTEXT_WAIT_SECONDS",
    # DFX model access on own hardware (ADR-081 section 7)
    "OPENROUTER_MANAGEMENT_KEY_FILE",
    "DFX_KEY_MAX_HEADROOM_USD",
    "DFX_KEY_MAX_PER_ORG",
    # The platform browser (KOR-44, ADR-085; docs/platform/spec/13-work-vm.md section 13)
    "BROWSER_IDLE_SECONDS",
    "BROWSER_LIVE_MAX_KBPS",
    "BROWSER_SEARCH_URL",
)
RETIRED_NAMES = frozenset(
    {"WORKVM_SLOTS", "WORKVM_MAX_PER_TASK", "WORKVM_MAX_PER_ORG", "WORKVM_MAX_PER_OWNER", "SESSIONS_PER_VM", "MAX_VMS"}
)
"""Fixed capacity limits that no longer exist (the free resources decide, ADR-075 addendum 2): set in an older
`runner.env` they are ignored - no `config_invalid` - and named once in the log at the start (`Config.retired`)."""
# Required for the daemon (`run`, `service install`); commands that never talk to the hub load without them.
HUB_NAMES = frozenset({"HUB_URL", "PLATFORM_ROOT_KEY"})

# name: (default, minimum, maximum)
_INTEGERS: dict[str, tuple[int, int, int]] = {
    "VM_CPUS": (4, 1, 256),
    "VM_MEMORY_MIB": (8192, 1024, 4_194_304),
    "VM_DISK_GIB": (20, 5, 65_536),
    "POOL_SIZE": (2, 0, 64),
    # a running workgroup VM takes one more session while its load per vCPU is at most this (`vm.room`)
    "VM_LOAD_MAX_PERCENT": (85, 10, 100),
    "SESSION_IDLE_SECONDS": (1800, 60, 604_800),
    "VM_IDLE_SECONDS": (1800, 60, 604_800),
    # ADR-059 addendum 4: an idle work VM is stopped (slot and RAM free, disk kept), a stopped one deleted later.
    "WORKVM_IDLE_SECONDS": (600, 60, 604_800),
    "WORKVM_KEEP_STOPPED_SECONDS": (86_400, 600, 2_592_000),
    "WORKVM_POOL_SIZE": (1, 0, 16),
    "WORKVM_DISK_QUOTA_GIB": (0, 0, 1_000_000),
    "WORKVM_QUEUE_SECONDS": (900, 0, 86_400),
    "WORKVM_SSH_PORT_BASE": (47200, 1024, 65_435),
    # KOR-43 logs of test runs and long work VM jobs (ADR-080 section 3): the kept bytes of one log (its last part).
    "LOG_MAX_MIB": (256, 1, 1_000_000),
    # KOR-33 context checkpoints (ADR-072 sections 4 and 6): generous defaults, never a precondition.
    "CONTEXT_INTERVAL_SECONDS": (600, 30, 86_400),
    "CONTEXT_CACHE_MIB": (20_480, 0, 999_999_999),
    "CONTEXT_WORKSPACE_MIB": (4096, 0, 999_999_999),
    "CONTEXT_TRANSCRIPT_MIB": (4096, 1, 999_999_999),
    "CONTEXT_WAIT_SECONDS": (180, 0, 3600),
    # live DFX keys one issuer creates per organization (ADR-081 section 7): a compromised hub cannot ask for more
    "DFX_KEY_MAX_PER_ORG": (50, 1, 100_000),
    # KOR-44 platform browser (ADR-085 sections 2 and 6): idle teardown and the cap of the live picture (kbit/s).
    "BROWSER_IDLE_SECONDS": (600, 60, 3600),
    "BROWSER_LIVE_MAX_KBPS": (3000, 100, 100_000),
}
# name: (minimum, maximum) of a setting that is empty by default; empty = the rule of every device (05-runner.md 1.3)
_OPTIONAL_INTEGERS: dict[str, tuple[int, int]] = {
    # the host reserve: empty = a quarter of the memory, at least 4 GiB, at most 32 GiB (`vm.capacity.host_reserve_mib`)
    "MEMORY_RESERVE_MIB": (0, 4_194_304),
    # the device's one admission (`agent_runner.admission`): a new VM of any kind only while the host's CPU load (1 min,
    # per core) is at most this; empty = about 1-2 cores stay free (`vm.capacity.host_cpu_ratio`)
    "CPU_LOAD_MAX_PERCENT": (10, 100),
}
WORKVM_SIZES = ("standard", "large", "xlarge")
_URL_DEFAULTS = {
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_WEB_URL": "https://github.com",
    "MODEL_UPSTREAM_ANTHROPIC": "https://api.anthropic.com",
    "MODEL_UPSTREAM_OPENROUTER": "https://openrouter.ai/api",
    "ANTHROPIC_USAGE_URL": "https://api.anthropic.com/api/oauth/usage",
    "CLAUDE_TOKEN_URL": DEFAULT_CLAUDE_TOKEN_URL,
}
_DIGITS_RE = re.compile(r"^[0-9]{1,9}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
# Lima instance names start with a letter and use [A-Za-z0-9._-]; the prefix is lower case and ends the
# instance name's first segment, e.g. `wp-run01-`.
_VM_PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31})?$")
_LOOPBACK_NAMES = frozenset({"localhost"})
_ED25519_PUBLIC_LENGTH = 32
DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD = 5_000_000
_MICRO = 1_000_000
_MAX_DFX_KEY_HEADROOM_MICRO_USD = 10_000 * _MICRO
# a USD amount with at most six decimals (micro-USD precision), e.g. `5`, `2.5`, `0.000001`
_USD_RE = re.compile(r"^[0-9]{1,5}(?:\.[0-9]{1,6})?$")


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
    memory_reserve_mib: int | None
    """The host reserve when set; None: a quarter of the memory, 4-32 GiB, on every device (`vm.capacity`)."""
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
    vm_load_max_percent: int = 85
    """A running workgroup VM takes one more session while its load per vCPU is at most this (`vm.room`)."""
    cpu_load_max_percent: int | None = None
    """The device's one admission: a new VM of any kind only while the host's load per core is at most this; None:
    about 1-2 cores stay free (`vm.capacity.host_cpu_ratio`)."""
    retired: tuple[str, ...] = ()
    """Names of `RETIRED_NAMES` set in the environment: ignored, named once in the log at the start."""
    github_oauth_client_id: str | None = None
    """Public client ID of the OAuth App whose device flow "Connect GitHub" prefers (KOR-65, ADR-015 addendum)."""
    github_app_slug: str | None = None
    """URL name of the GitHub App (`https://github.com/apps/<slug>`), for the install link of a next step (KOR-65)."""
    operator_github_org: str | None = None
    """The organization that owns the operator's GitHub account (platform devices only, only together with the file;
    KOR-47, ADR-078 section 7): an admin's company `github.connect` for it answers with the operator's token."""
    openrouter_management_key_file: Path | None = None
    """The OpenRouter management key file of a DFX key issuer (platform devices only, 0600, never logged or sent;
    ADR-081 section 7). It can create, read, change and delete **every** key of the provider account (the platform key
    included), which is why it never leaves the issuing device; the issuer touches only keys named `dfx-...`."""
    dfx_key_max_headroom_micro_usd: int = DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD
    """The issuer's own cap of a key's headroom above its spend (`DFX_KEY_MAX_HEADROOM_USD`, default 5 USD), so a
    compromised hub cannot raise a limit beyond it (ADR-081 section 7)."""
    dfx_key_max_per_org: int = 50
    """Live keys the issuer keeps per organization at most (`DFX_KEY_MAX_PER_ORG`); further requests are refused."""

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

    workvm_slots_self: bool = False
    workvm_max_size: str = "large"
    workvm_idle_seconds: int = 600
    workvm_keep_stopped_seconds: int = 86_400
    workvm_pool_size: int = 1
    workvm_disk_quota_gib: int = 0
    workvm_queue_seconds: int = 900
    log_max_mib: int = 256
    workvm_ssh_port_base: int = 47200
    workvm_template: Path | None = None

    browser_idle_seconds: int = 600
    """Idle teardown of a platform browser session without its own `idle_seconds` (KOR-44, ADR-085 section 2)."""
    browser_live_max_kbps: int = 3000
    """Cap of the live picture's bandwidth in kbit/s (ADR-085 section 6)."""
    browser_search_url: str = ""
    """URL template with `{q}` that `browser_navigate {query}` opens; empty = automated search is off (F9)."""
    subscription_login_argv: tuple[str, ...] = DEFAULT_SUBSCRIPTION_LOGIN_ARGV
    """The full login of a further subscription (ADR-092 section 1)."""
    claude_token_url: str = DEFAULT_CLAUDE_TOKEN_URL
    """The token endpoint the measuring device renews further subscriptions at (ADR-092 section 2)."""
    claude_oauth_client_id: str = DEFAULT_CLAUDE_OAUTH_CLIENT_ID
    """The client ID of that renewal (the pinned Claude Code's)."""

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
    `LOGIN_AUTOMATION` default and refuses `OPENROUTER_KEY_FILE` and `OPENROUTER_MANAGEMENT_KEY_FILE` on org
    devices. `require` names the variables that must be set (default: the hub settings needed by the daemon).
    """
    check_platform(platform)
    for key in sorted(env):
        if key.startswith(PREFIX) and key[len(PREFIX) :] not in (*NAMES, *RETIRED_NAMES):
            raise RunnerError("config_invalid", key[len(PREFIX) :])
    retired = tuple(sorted(name for name in RETIRED_NAMES if PREFIX + name in env))

    def raw(name: str) -> str:
        return env.get(PREFIX + name, "")

    test_mode = _flag(raw("TEST_MODE"), "TEST_MODE")
    hub_url = _optional(raw("HUB_URL"), "HUB_URL", require, lambda v: _url(v, "HUB_URL", test_mode))
    root_key = _optional(raw("PLATFORM_ROOT_KEY"), "PLATFORM_ROOT_KEY", require, _root_key)
    integers = {name: _integer(raw(name), name, *limits) for name, limits in _INTEGERS.items()}
    chosen = {
        name: None if raw(name) == "" else _integer(raw(name), name, low, low, high)
        for name, (low, high) in _OPTIONAL_INTEGERS.items()
    }
    urls = {name: _url(raw(name) or default, name, test_mode) for name, default in _URL_DEFAULTS.items()}
    openrouter = _optional(raw("OPENROUTER_KEY_FILE"), "OPENROUTER_KEY_FILE", require, _key_file)
    if openrouter is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPENROUTER_KEY_FILE")
    management = _optional(
        raw("OPENROUTER_MANAGEMENT_KEY_FILE"),
        "OPENROUTER_MANAGEMENT_KEY_FILE",
        require,
        lambda v: _key_file(v, "OPENROUTER_MANAGEMENT_KEY_FILE"),
    )
    if management is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPENROUTER_MANAGEMENT_KEY_FILE")
    operator_dir, operator_orgs = _operator(raw, require, device_kind)
    github_file, github_users, github_org = _operator_github(raw, require, device_kind)
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
        memory_reserve_mib=chosen["MEMORY_RESERVE_MIB"],
        vm_load_max_percent=integers["VM_LOAD_MAX_PERCENT"],
        cpu_load_max_percent=chosen["CPU_LOAD_MAX_PERCENT"],
        retired=retired,
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
        subscription_login_argv=_login_argv(
            raw("SUBSCRIPTION_LOGIN_ARGV"), "SUBSCRIPTION_LOGIN_ARGV", DEFAULT_SUBSCRIPTION_LOGIN_ARGV
        ),
        claude_token_url=urls["CLAUDE_TOKEN_URL"],
        claude_oauth_client_id=_client_id(
            raw("CLAUDE_OAUTH_CLIENT_ID") or DEFAULT_CLAUDE_OAUTH_CLIENT_ID, "CLAUDE_OAUTH_CLIENT_ID"
        ),
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
        operator_github_org=github_org,
        openrouter_management_key_file=management,
        dfx_key_max_headroom_micro_usd=_usd_micro(raw("DFX_KEY_MAX_HEADROOM_USD"), "DFX_KEY_MAX_HEADROOM_USD"),
        dfx_key_max_per_org=integers["DFX_KEY_MAX_PER_ORG"],
        **_workvm(raw, require, integers, test_mode),
        context_interval_seconds=integers["CONTEXT_INTERVAL_SECONDS"],
        context_cache_mib=integers["CONTEXT_CACHE_MIB"],
        context_workspace_mib=integers["CONTEXT_WORKSPACE_MIB"],
        context_transcript_mib=integers["CONTEXT_TRANSCRIPT_MIB"],
        context_wait_seconds=integers["CONTEXT_WAIT_SECONDS"],
        browser_idle_seconds=integers["BROWSER_IDLE_SECONDS"],
        browser_live_max_kbps=integers["BROWSER_LIVE_MAX_KBPS"],
        browser_search_url=_search_url(raw("BROWSER_SEARCH_URL")),
    )


class _WorkVmSettings(TypedDict):
    workvm_slots_self: bool
    workvm_max_size: str
    workvm_idle_seconds: int
    workvm_keep_stopped_seconds: int
    workvm_pool_size: int
    workvm_disk_quota_gib: int
    workvm_queue_seconds: int
    workvm_ssh_port_base: int
    workvm_template: Path | None
    log_max_mib: int


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
        "workvm_slots_self": slots_self,
        "workvm_max_size": max_size,
        "workvm_idle_seconds": integers["WORKVM_IDLE_SECONDS"],
        "workvm_keep_stopped_seconds": integers["WORKVM_KEEP_STOPPED_SECONDS"],
        "workvm_pool_size": integers["WORKVM_POOL_SIZE"],
        "workvm_disk_quota_gib": integers["WORKVM_DISK_QUOTA_GIB"],
        "workvm_queue_seconds": integers["WORKVM_QUEUE_SECONDS"],
        "log_max_mib": integers["LOG_MAX_MIB"],
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
        "OPENROUTER_MANAGEMENT_KEY_FILE": config.openrouter_management_key_file,
        "DFX_KEY_MAX_HEADROOM_USD": (
            None
            if config.dfx_key_max_headroom_micro_usd == DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD
            else usd_text(config.dfx_key_max_headroom_micro_usd)
        ),
        "GITHUB_CLIENT_ID": config.github_client_id,
        "GITHUB_OAUTH_CLIENT_ID": config.github_oauth_client_id,
        "GITHUB_APP_SLUG": config.github_app_slug,
        "GOLDEN_TEMPLATE": config.golden_template,
        "VM_PREFIX": config.vm_prefix or None,
        "OPERATOR_SUBSCRIPTION_DIR": config.operator_subscription_dir,
        "OPERATOR_SUBSCRIPTION_ORGS": ",".join(sorted(config.operator_subscription_orgs)) or None,
        "OPERATOR_GITHUB_TOKEN_FILE": config.operator_github_token_file,
        "OPERATOR_GITHUB_USERS": ",".join(sorted(config.operator_github_users)) or None,
        "OPERATOR_GITHUB_ORG": config.operator_github_org,
        "WORKVM_MAX_SIZE": None if config.workvm_max_size == "large" else config.workvm_max_size,
        "WORKVM_TEMPLATE": config.workvm_template,
        "BROWSER_SEARCH_URL": config.browser_search_url or None,
    }
    for name, value in optional_text.items():
        if value is not None:
            out[PREFIX + name] = str(value)
    defaults: dict[str, int | None] = {name: default for name, (default, _, _) in _INTEGERS.items()}
    for name, default in {**defaults, **dict.fromkeys(_OPTIONAL_INTEGERS)}.items():
        value_int = getattr(config, name.lower())
        if value_int is not None and value_int != default:
            out[PREFIX + name] = str(value_int)
    for name, default_url in _URL_DEFAULTS.items():
        url = getattr(config, name.lower())
        if url != default_url:
            out[PREFIX + name] = url
    if config.login_argv != DEFAULT_LOGIN_ARGV:
        out[PREFIX + "LOGIN_ARGV"] = json.dumps(list(config.login_argv))
    if config.subscription_login_argv != DEFAULT_SUBSCRIPTION_LOGIN_ARGV:
        out[PREFIX + "SUBSCRIPTION_LOGIN_ARGV"] = json.dumps(list(config.subscription_login_argv))
    if config.claude_oauth_client_id != DEFAULT_CLAUDE_OAUTH_CLIENT_ID:
        out[PREFIX + "CLAUDE_OAUTH_CLIENT_ID"] = config.claude_oauth_client_id
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


def _search_url(value: str) -> str:
    """`BROWSER_SEARCH_URL`: empty (off) or an http(s) URL template with a host and the placeholder `{q}`."""
    if value == "":
        return value
    try:
        parts = urlsplit(value.replace("{q}", "q"))
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise RunnerError("config_invalid", "BROWSER_SEARCH_URL") from None
    if (
        "{q}" not in value
        or parts.scheme not in ("http", "https")
        or not host
        or port == 0
        or parts.username is not None
        or parts.password is not None
        or len(value) > 2048
        or any(ch.isspace() or not ch.isprintable() for ch in value)
    ):
        raise RunnerError("config_invalid", "BROWSER_SEARCH_URL")
    return value


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


def _key_file(value: str, name: str = "OPENROUTER_KEY_FILE") -> Path:
    """A regular file (not a symlink) readable only by its owner (0600 or stricter)."""
    path = _absolute(value, name)
    try:
        info = path.lstat()
    except OSError:
        raise RunnerError("config_invalid", name) from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise RunnerError("config_invalid", name)
    return path


def _usd_micro(value: str, name: str) -> int:
    """A USD amount (at most six decimals, at most 10000) in micro-USD; empty is the default of 5 USD."""
    if value == "":
        return DEFAULT_DFX_KEY_MAX_HEADROOM_MICRO_USD
    if not _USD_RE.fullmatch(value):
        raise RunnerError("config_invalid", name)
    whole, _, fraction = value.partition(".")
    micro = int(whole) * _MICRO + int(fraction.ljust(6, "0"))
    if micro > _MAX_DFX_KEY_HEADROOM_MICRO_USD:
        raise RunnerError("config_invalid", name)
    return micro


def usd_text(micro: int) -> str:
    """The shortest decimal text of a micro-USD amount (`5`, `2.5`, `0.000001`); the inverse of `_usd_micro`."""
    whole, fraction = divmod(micro, _MICRO)
    return f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")


def _operator(
    raw: Callable[[str], str], require: frozenset[str], device_kind: DeviceKind | None
) -> tuple[Path | None, frozenset[str]]:
    """`OPERATOR_SUBSCRIPTION_DIR` and `OPERATOR_SUBSCRIPTION_ORGS` (KOR-23): platform devices only; the list needs the
    directory, the directory alone is valid (the organizations come from the platform log, ADR-079)."""
    directory = _optional(raw("OPERATOR_SUBSCRIPTION_DIR"), "OPERATOR_SUBSCRIPTION_DIR", require, _operator_dir)
    orgs = _org_ids(raw("OPERATOR_SUBSCRIPTION_ORGS"))
    if directory is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_SUBSCRIPTION_DIR")
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
) -> tuple[Path | None, frozenset[str], str | None]:
    """`OPERATOR_GITHUB_TOKEN_FILE`, `OPERATOR_GITHUB_USERS` (KOR-27) and `OPERATOR_GITHUB_ORG` (KOR-47), platform
    devices only: the file needs users or the organization (an organization ID, normalized to lower case), and users
    or the organization need the file. The token is the operator's own GitHub connection, so the user list names at
    most one user - the operator; more are `config_invalid` `OPERATOR_GITHUB_USERS:one_user_only` (a personal
    connection is never shared, HOT-14, ADR-053); the organization's members use it as a company account (ADR-078).

    Only the path is checked here: the file is replaced atomically while the daemon runs, so its type, owner, mode
    and content are checked at each read (`github.operator.read_token`)."""
    path = _optional(
        raw("OPERATOR_GITHUB_TOKEN_FILE"),
        "OPERATOR_GITHUB_TOKEN_FILE",
        require,
        lambda v: _absolute(v, "OPERATOR_GITHUB_TOKEN_FILE"),
    )
    users = _user_ids(raw("OPERATOR_GITHUB_USERS"))
    org = _org_id(raw("OPERATOR_GITHUB_ORG"))
    if path is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_TOKEN_FILE")
    if users and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_USERS")
    if org is not None and device_kind == "org":
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_ORG")
    if path is not None and not users and org is None:
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_USERS")
    if (users or org is not None) and path is None:
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_TOKEN_FILE")
    if len(users) > 1:
        raise RunnerError("config_invalid", OPERATOR_GITHUB_ONE_USER)
    return path, users, org


def _org_id(value: str) -> str | None:
    """`OPERATOR_GITHUB_ORG`: one organization ID (a UUID, normalized to lower case); empty = none."""
    if not value.strip():
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise RunnerError("config_invalid", "OPERATOR_GITHUB_ORG") from None


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


def _login_argv(value: str, name: str = "LOGIN_ARGV", default: tuple[str, ...] = DEFAULT_LOGIN_ARGV) -> tuple[str, ...]:
    if value == "":
        return default
    try:
        parsed = json.loads(value)
    except ValueError:
        raise RunnerError("config_invalid", name) from None
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(item, str) and item and "\x00" not in item for item in parsed)
    ):
        raise RunnerError("config_invalid", name)
    return tuple(parsed)


def _vm_prefix(value: str) -> str:
    if not _VM_PREFIX_RE.fullmatch(value):
        raise RunnerError("config_invalid", "VM_PREFIX")
    return value
