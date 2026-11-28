"""Run configuration: options, ports, prefixes, checkout paths and the allow-list environment of every process.

Ports: a run uses only `WP_PORT_BASE+4 … +19` (ADR-027 section 7; `+0…+3` stay free for the package's unit-test
cluster and the web suite). The hub is reached by everybody under one public origin `http://localhost:<public>`
(WebAuthn RP ID, signed host line): the runners through the runner relay listening on `<public>` on their device,
the browser through the browser relay that Chromium's host resolver maps `localhost:<public>` to.

Environment (07-testing.md section 5.2): every process gets an allow-list environment with `HTTP(S)_PROXY` pointing
to the closed port 9 of the loopback, so any unexpected egress fails at once, and `NO_PROXY` naming only the
addresses of the doubles and the hub.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PORT_OFFSETS = {
    "control": 4,
    "postgres": 5,
    "hub": 6,
    "public": 7,
    "browser": 8,
    "model": 9,
    "model-relay": 10,
    "github": 13,
    "github-relay": 14,
    "site": 18,
}
LAST_OFFSET = 19
DEFAULT_PORT_BASE = 20000
DEAD_PROXY = "http://127.0.0.1:9"
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
PASSED_THROUGH = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH", "UV_CACHE_DIR")
TOPOLOGIES = ("t1", "t2")
PROJECTS = ("desktop", "mobile")
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_ONLY_RE = re.compile(r"^(?:E2E|PERF|LIVE)-\d{2}$")
_TARGET_RE = re.compile(r"^(?:[a-z_][a-z0-9_.-]{0,31}@)?[A-Za-z0-9.-]{1,253}$")


class ConfigError(Exception):
    """An option or environment value is invalid; the run does not start."""


def port(base: int, name: str) -> int:
    return base + PORT_OFFSETS[name]


@dataclass(frozen=True)
class RunConfig:
    topology: str
    core_src: Path
    agent_src: Path
    artifacts: Path
    port_base: int
    vm_prefix: str
    runner_target: str | None = None
    remote_agent_src: str | None = None
    remote_core_src: str | None = None
    only: tuple[str, ...] = ()
    repeat: int = 1
    live: bool = False
    project: str = "desktop"
    runners: int = 1
    pool_size: int = 0
    vm_memory_mib: int = 8192
    memory_budget_mib: int | None = None
    path_prefix: str = "/opt/homebrew/bin"
    # E2E_WEB_MINIFY=1: the minified production bundle instead of the unminified one (browser coverage then maps onto
    # nothing; for a smoke of the release-like build, AUD-01 B-26)
    web_minify: bool = False
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def suite(self) -> Path:
        return self.core_src / "e2e" / "platform"

    @property
    def run_prefix(self) -> str:
        """Prefix of every Lima instance of this run (cleanup deletes all of them)."""
        return f"{self.vm_prefix}e2e-"

    def runner_prefix(self, index: int) -> str:
        return f"{self.run_prefix}r{index}-"

    def port(self, name: str) -> int:
        return port(self.port_base, name)

    @property
    def public_origin(self) -> str:
        return f"http://localhost:{self.port('public')}"


def home_relative(path: Path, home: Path) -> str:
    """`~/<rel>` for a path below `home` (the same layout on the runner device), else the path itself."""
    try:
        return "~/" + path.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return str(path)


def build_config(  # noqa: PLR0912 - one validation per option
    *,
    topology: str,
    environ: Mapping[str, str],
    core_src: Path,
    runner: str | None = None,
    only: str | None = None,
    repeat: int = 1,
    live: bool = False,
    project: str = "desktop",
    artifacts: str | None = None,
    home: Path | None = None,
) -> RunConfig:
    """Validate the options of `run.sh` and the environment (`WP_PORT_BASE`, `AGENT_RUNNER_VM_PREFIX`, …)."""
    if topology not in TOPOLOGIES:
        raise ConfigError(f"--topology must be one of {', '.join(TOPOLOGIES)}")
    if (topology == "t2") != (runner is not None):
        raise ConfigError("--runner <ssh target> is required for t2 and not allowed for t1")
    if runner is not None and not _TARGET_RE.fullmatch(runner):
        raise ConfigError("--runner must be an SSH target [user@]host")
    if project not in PROJECTS:
        raise ConfigError(f"--project must be one of {', '.join(PROJECTS)}")
    if not 1 <= repeat <= 20:
        raise ConfigError("--repeat must be 1..20")
    selected = tuple(item.strip() for item in only.split(",") if item.strip()) if only else ()
    bad = [item for item in selected if not _ONLY_RE.fullmatch(item)]
    if bad:
        raise ConfigError(f"--only takes test IDs like E2E-06 (got {', '.join(bad)})")
    base_text = environ.get("WP_PORT_BASE", str(DEFAULT_PORT_BASE))
    if not base_text.isdigit() or not 1024 <= int(base_text) <= 65535 - LAST_OFFSET:
        raise ConfigError("WP_PORT_BASE must be a port number with 19 free ports above it")
    prefix = environ.get("AGENT_RUNNER_VM_PREFIX") or environ.get("WP_VM_PREFIX") or ""
    if not prefix:
        raise ConfigError("AGENT_RUNNER_VM_PREFIX (or WP_VM_PREFIX) must be set: every VM of a run carries it")
    if not _PREFIX_RE.fullmatch(prefix) or not prefix.endswith("-"):
        raise ConfigError("AGENT_RUNNER_VM_PREFIX must be lower case, start with a letter and end with '-'")
    # One repository since INF-06: runner and doubles come from this checkout unless AGENT_RUNNER_SRC names another.
    agent_text = environ.get("AGENT_RUNNER_SRC")
    agent_src = Path(agent_text).expanduser() if agent_text else core_src
    if not (agent_src / "src" / "agent_runner").is_dir() and topology == "t1":
        raise ConfigError(f"AGENT_RUNNER_SRC={agent_src} is not an agent checkout")
    numbers = {}
    for name, default in (("E2E_RUNNERS", 1), ("E2E_POOL_SIZE", 0), ("E2E_VM_MEMORY_MIB", 8192)):
        text = environ.get(name, str(default))
        if not text.isdigit():
            raise ConfigError(f"{name} must be a non-negative integer")
        numbers[name] = int(text)
    if not 1 <= numbers["E2E_RUNNERS"] <= 4:
        raise ConfigError("E2E_RUNNERS must be 1..4")
    if numbers["E2E_POOL_SIZE"] > 1:
        # The runner's warm pool is fixed at 1 (ADR-009 addendum); 0 holds it empty (AGENT_RUNNER_TEST_POOL_EMPTY).
        raise ConfigError("E2E_POOL_SIZE must be 0 (pool held empty) or 1 (the runner's fixed pool)")
    budget_text = environ.get("E2E_VM_MEMORY_BUDGET_MIB")
    if budget_text is not None and not budget_text.isdigit():
        raise ConfigError("E2E_VM_MEMORY_BUDGET_MIB must be a number of MiB")
    minify_text = environ.get("E2E_WEB_MINIFY", "0")
    if minify_text not in ("0", "1"):
        raise ConfigError("E2E_WEB_MINIFY must be 0 or 1")
    home = home or Path.home()
    artifacts_path = Path(artifacts or environ.get("E2E_ARTIFACTS_DIR") or core_src / "e2e" / "platform" / "artifacts")
    return RunConfig(
        topology=topology,
        core_src=core_src,
        agent_src=agent_src,
        artifacts=artifacts_path.expanduser(),
        port_base=int(base_text),
        vm_prefix=prefix,
        runner_target=runner,
        remote_agent_src=environ.get("E2E_REMOTE_AGENT_SRC") or home_relative(agent_src, home),
        remote_core_src=environ.get("E2E_REMOTE_CORE_SRC") or home_relative(core_src, home),
        only=selected,
        repeat=repeat,
        live=live,
        project=project,
        runners=numbers["E2E_RUNNERS"],
        pool_size=numbers["E2E_POOL_SIZE"],
        vm_memory_mib=numbers["E2E_VM_MEMORY_MIB"],
        memory_budget_mib=int(budget_text) if budget_text is not None else None,
        path_prefix=environ.get("E2E_REMOTE_PATH_PREFIX", "/opt/homebrew/bin"),
        web_minify=minify_text == "1",
    )


def clean_env(environ: Mapping[str, str], extra: Mapping[str, str], no_proxy: tuple[str, ...] = ()) -> dict[str, str]:
    """Allow-list environment: a few pass-through variables, the dead proxy, `NO_PROXY` and `extra`."""
    env = {name: environ[name] for name in PASSED_THROUGH if name in environ}
    env.setdefault("LANG", "C.UTF-8")
    hosts = ",".join(dict.fromkeys((*LOOPBACK_NAMES, *no_proxy)))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env[name] = DEAD_PROXY
    env["NO_PROXY"] = env["no_proxy"] = hosts
    env.update(extra)
    return env
