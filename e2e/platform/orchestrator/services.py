"""The services of a run: PostgreSQL 16 cluster, hub, doubles and runner instances (07-testing.md section 5.1).

- Cluster: `initdb` (scram passwords, loopback TCP only, no unix socket) and `postgres` as a managed process;
  database `agent_hub` with owner, writer and reader roles (passwords are random per run and listed as secrets for the
  artefact scan).
- Hub: the web build is made first (unminified, so browser coverage maps onto the sources), then
  `python -m agent_hub.db migrate` with the owner URL, then `python -m agent_hub serve` with the writer URL. The public
  URL is `http://localhost:<public>`; the hub itself listens on the hub port.
- Doubles: `python -m agent_doubles.<name>` from the agent checkout, port files in the run directory, journals next
  to them; each double is reached through a relay (logged, can be cut). The model double serves both model upstreams,
  the GitHub double the GitHub API and web hosts of the runners.
- Runners: `python -m agent_runner run` with an own `AGENT_RUNNER_HOME`, `AGENT_RUNNER_VM_PREFIX`, test mode, the
  public hub URL and a platform OpenRouter key file (a run secret; the model double gets it too and answers its key
  document `GET /v1/key` only for that key); ready when
  the daemon reports `enrolled=` (`no`: it waits for enrollment). One-shot runner commands (`enroll`, `image adopt`,
  `pool …`) run with the same environment.
- Operator key: the test platform root key is public (seed SHA-256 of `TEST_ROOT_SEED_TEXT`, WEB-01 decision 12); the
  operator CLI gets it as a key file (`write_operator_key`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.config import RunConfig, clean_env
from orchestrator.node import Node, NodeError

PG_BIN_DIRS = ("/opt/homebrew/opt/postgresql@16/bin", "/usr/local/opt/postgresql@16/bin", "/usr/lib/postgresql/16/bin")
HUB_DATABASE = "agent_hub"
READY_TIMEOUT_S = 60.0
TEST_ROOT_SEED_TEXT = "agent.dfx.swiss test platform root key"
# The GitHub double's default App client ID (`agent_doubles.github.deviceflow.DEFAULT_CLIENT_ID`) and the https site
# its answers name in web links (`--html-url`): the runner accepts only https result URLs, as GitHub sends them.
GITHUB_CLIENT_ID = "Iv1.0000double0000"
GITHUB_HTML_URL = "https://github.example"
_ROOT_KEY_RE = re.compile(r"TEST_ROOT_KEY\s*=\s*'([A-Za-z0-9_-]{43})'")


class ServiceError(Exception):
    """A service did not start or answered wrongly."""


@dataclass
class Secrets:
    """Values generated for a run; the artefact scan fails if any of them appears in an artefact."""

    values: list[str] = field(default_factory=list)

    def new(self, nbytes: int = 32) -> str:
        value = secrets.token_hex(nbytes)
        self.values.append(value)
        return value


def find_pg_bin(node: Node, candidates: tuple[str, ...] = PG_BIN_DIRS) -> str:
    for directory in candidates:
        if node.run(["test", "-x", f"{directory}/initdb"])["rc"] == 0:
            return directory
    raise ServiceError(f"PostgreSQL 16 not found (looked in {', '.join(candidates)})")


@dataclass
class Cluster:
    """PostgreSQL 16 on 127.0.0.1 with password authentication (scram) and no unix socket.

    Roles: the superuser `postgres` (setup and dump), `hub_owner`/`hub_writer` (the hub) and `e2e_reader`
    (`pg_read_all_data`, used by the read-only queries of the control API). All passwords are run secrets."""

    node: Node
    bin_dir: str
    data_dir: str
    port: int
    env: Mapping[str, str]
    password: str = ""
    reader_password: str = ""

    def start(self, pwfile: str) -> None:
        """`pwfile` (on the hub device) briefly holds the superuser password for `initdb` and is deleted after."""
        Path(pwfile).write_text(self.password + "\n", encoding="utf-8")
        try:
            argv = [f"{self.bin_dir}/initdb", "-D", self.data_dir, "-U", "postgres", "--auth=scram-sha-256"]
            init = self.node.run(
                [*argv, f"--pwfile={pwfile}", "--no-sync", "-E", "UTF8"],
                env=dict(self.env, LC_ALL="C"),
            )
        finally:
            Path(pwfile).unlink(missing_ok=True)
        if init["rc"] != 0:
            raise ServiceError(f"initdb failed: {init['stderr'].strip()[-300:]}")
        options = ["-c", "listen_addresses=127.0.0.1", "-c", "unix_socket_directories="]
        self.node.spawn(
            "postgres",
            [f"{self.bin_dir}/postgres", "-D", self.data_dir, "-p", str(self.port), *options],
            dict(self.env),
        )
        self.node.wait_line("postgres", "ready to accept connections", READY_TIMEOUT_S)

    def _client_env(self, password: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(self.env, PGPASSWORD=password, **(extra or {}))

    def psql(self, sql: str, database: str = "postgres", *, read_only: bool = False) -> str:
        """One statement as `postgres`, or as `e2e_reader` in a read-only session."""
        user, password = ("e2e_reader", self.reader_password) if read_only else ("postgres", self.password)
        extra = {"PGOPTIONS": "-c default_transaction_read_only=on"} if read_only else None
        argv = [f"{self.bin_dir}/psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
        argv += ["-h", "127.0.0.1", "-p", str(self.port), "-U", user, "-d", database, "-c", sql]
        done = self.node.run(argv, env=self._client_env(password, extra))
        if done["rc"] != 0:
            raise ServiceError(f"psql failed: {done['stderr'].strip()[-300:]}")
        return str(done["stdout"])

    def create_hub_database(self, owner_password: str, writer_password: str) -> tuple[str, str]:
        """Roles and database `agent_hub`; returns (owner URL, writer URL)."""
        self.psql(
            f"CREATE ROLE hub_owner LOGIN PASSWORD '{owner_password}';"
            f"CREATE ROLE hub_writer LOGIN PASSWORD '{writer_password}';"
            f"CREATE ROLE e2e_reader LOGIN PASSWORD '{self.reader_password}' IN ROLE pg_read_all_data;"
        )
        self.psql(f"CREATE DATABASE {HUB_DATABASE} OWNER hub_owner")
        return self.url("hub_owner", owner_password), self.url("hub_writer", writer_password)

    def grant_reader(self) -> None:
        """After `agent-hub migrate` (which revokes CONNECT from PUBLIC): the reader role may connect again."""
        self.psql(f"GRANT CONNECT ON DATABASE {HUB_DATABASE} TO e2e_reader")

    def url(self, role: str, password: str) -> str:
        return f"postgresql://{role}:{password}@127.0.0.1:{self.port}/{HUB_DATABASE}?sslmode=disable"

    def dump(self, path: str) -> None:
        argv = [f"{self.bin_dir}/pg_dump", "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres", "-f", path]
        done = self.node.run([*argv, HUB_DATABASE], env=self._client_env(self.password))
        if done["rc"] != 0:
            raise ServiceError(f"pg_dump failed: {done['stderr'].strip()[-300:]}")


def test_root_key(core_src: Path) -> str:
    """The test platform root key compiled into the web build without `VITE_PLATFORM_ROOT_KEY` (WEB-01)."""
    match = _ROOT_KEY_RE.search((core_src / "web" / "build" / "manifest.ts").read_text(encoding="utf-8"))
    if match is None:
        raise ServiceError("web/build/manifest.ts names no TEST_ROOT_KEY")
    return match.group(1)


def write_operator_key(path: Path) -> Path:
    """The operator CLI's key file (`agent-hub platform --key-file`, format of `agent_hub.platform.cli`) holding the
    public test root seed, mode 0600; an existing file is kept (the CLI's operator record lies next to it)."""
    if not path.exists():
        seed = hashlib.sha256(TEST_ROOT_SEED_TEXT.encode()).digest()
        root = base64.urlsafe_b64encode(seed).rstrip(b"=").decode("ascii")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(json.dumps({"v": 1, "root": root}) + "\n")
    return path


def build_web(node: Node, config: RunConfig, environ: Mapping[str, str], root_key: str) -> None:
    """The web build into the hub's static directory with the run's root key: unminified (WEB-01 decision 6), so that
    browser coverage maps onto the sources, or minified like the release build with `E2E_WEB_MINIFY=1`."""
    minify = "true" if config.web_minify else "false"
    script = f"import('./scripts/build.mjs').then((m) => m.buildWeb({{ minify: {minify} }}))"
    extra = {"VITE_PLATFORM_ROOT_KEY": root_key}
    if node.run(["git", "-C", str(config.core_src), "rev-parse", "HEAD"])["rc"] != 0:
        # A synchronised copy without .git (tools/wp/remote.sh): the manifest gets a placeholder commit and time.
        extra.update({"BUILD_COMMIT": "0" * 40, "SOURCE_DATE_EPOCH": "0"})
    env = clean_env(environ, extra)
    done = node.run(["node", "-e", script], env=env, cwd=str(config.core_src / "web"), timeout=300)
    if done["rc"] != 0:
        raise ServiceError(f"web build failed: {done['stderr'].strip()[-500:]}")


@dataclass
class Hub:
    node: Node
    python: str
    env: dict[str, str]
    bind: str
    port: int
    sleep: Callable[[float], None] = time.sleep

    def migrate(self, owner_url: str) -> str:
        env = {k: v for k, v in self.env.items() if not k.startswith("AGENT_HUB_")}
        env.update({"AGENT_HUB_OWNER_DATABASE": owner_url, "AGENT_HUB_DATABASE": self.env["AGENT_HUB_DATABASE"]})
        done = self.node.run([self.python, "-m", "agent_hub.db", "migrate"], env=env)
        if done["rc"] != 0:
            raise ServiceError(f"agent-hub migrate failed: {(done['stdout'] + done['stderr']).strip()[-500:]}")
        return str(done["stdout"]).strip()

    def start(self) -> None:
        self.node.spawn("hub", [self.python, "-m", "agent_hub", "serve"], self.env)
        url = f"http://{self.bind}:{self.port}/v1/health"
        deadline_tries = int(READY_TIMEOUT_S / 0.5)
        for _ in range(deadline_tries):
            if not self.node.alive("hub"):
                raise ServiceError(f"hub exited:\n{self.node.log_tail('hub')}")
            try:
                if self.node.http("GET", url, timeout=2)["status"] == 200:
                    return
            except NodeError:
                pass
            self.sleep(0.5)
        raise ServiceError(f"hub not healthy within {READY_TIMEOUT_S:.0f} s:\n{self.node.log_tail('hub')}")

    def has_command(self, *words: str) -> bool:
        """True when `agent-hub <words> --help` exists (operator CLI, audit verify of later packages)."""
        return bool(self.node.run([self.python, "-m", "agent_hub", *words, "--help"], env=self.env)["rc"] == 0)


def hub_env(
    environ: Mapping[str, str], config: RunConfig, *, writer_url: str, signing_key: str, root_key: str, bind: str
) -> dict[str, str]:
    return clean_env(
        environ,
        {
            "AGENT_HUB_PUBLIC_URL": config.public_origin,
            "AGENT_HUB_DATABASE": writer_url,
            "AGENT_HUB_SIGNING_KEY": signing_key,
            "AGENT_HUB_HUB_KEY_ID": "hub-e2e",
            "AGENT_HUB_PLATFORM_ROOT_KEY": root_key,
            "AGENT_HUB_HOST": bind,
            "AGENT_HUB_PORT": str(config.port("hub")),
            "AGENT_HUB_MODEL_CATALOG": str(config.core_src / "contracts" / "model-catalog.example.json"),
        },
    )


@dataclass
class Double:
    """A double of the agent checkout reached through its relay."""

    node: Node
    name: str
    raw_port: int
    relay_port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.relay_port}"

    @property
    def raw_url(self) -> str:
        return f"http://127.0.0.1:{self.raw_port}"

    def state(self) -> dict[str, object]:
        answer = self.node.http("GET", f"{self.raw_url}/_double/state")
        if answer["status"] != 200 or not isinstance(answer["body"], dict):
            raise ServiceError(f"{self.name} double: state answered {answer['status']}")
        return answer["body"]


def start_double(
    node: Node,
    *,
    name: str,
    python: str,
    env: dict[str, str],
    run_dir: str,
    raw_port: int,
    relay_port: int,
    args: list[str],
) -> Double:
    argv = [
        python,
        "-m",
        f"agent_doubles.{name}",
        "--port",
        str(raw_port),
        "--journal",
        f"{run_dir}/{name}-journal.jsonl",
    ]
    node.spawn(f"double-{name}", [*argv, *args], env)
    node.wait_line(f"double-{name}", r"^http://", READY_TIMEOUT_S)
    node.relay_start(name, ("127.0.0.1", relay_port), ("127.0.0.1", raw_port))
    return Double(node, name, raw_port, relay_port)


@dataclass
class Runner:
    node: Node
    name: str
    python: str
    env: dict[str, str]
    stopped_on_request: bool = False

    def start(self) -> None:
        self.stopped_on_request = False
        self.node.spawn(self.name, [self.python, "-m", "agent_runner", "run"], self.env)
        self.node.wait_line(self.name, r"^runner\.run .*enrolled=", READY_TIMEOUT_S)

    def state(self) -> str:
        if not self.node.alive(self.name):
            return "stopped"
        tail = self.node.log_tail(self.name, 200)
        return "waiting_for_enrollment" if "enrolled=no" in tail else "running"

    def stop(self) -> int:
        self.stopped_on_request = True
        return self.node.stop(self.name)

    def command(self, *args: str, timeout: float = 120.0) -> dict[str, Any]:
        """`python -m agent_runner <args>` once, with the runner's environment (same home and store)."""
        return self.node.run([self.python, "-m", "agent_runner", *args], env=self.env, timeout=timeout)

    def adopt(self, golden: str) -> None:
        """Register the shared golden cache as the runner's ready golden (`image adopt`), so the pool clones it."""
        done = self.command("image", "adopt", golden, timeout=300)
        if done["rc"] != 0:
            raise ServiceError(f"{self.name}: image adopt failed: {(done['stdout'] + done['stderr']).strip()[-300:]}")


def write_platform_key(node: Node, path: str, key: str) -> None:
    """The runner's platform OpenRouter key file (0600, only the device user may read it)."""
    script = 'umask 077 && mkdir -p "$(dirname "$1")" && printf %s "$E2E_KEY" > "$1"'
    done = node.run(["/bin/sh", "-c", script, "sh", path], env={"E2E_KEY": key, "PATH": "/usr/bin:/bin"})
    if done["rc"] != 0:
        raise ServiceError(f"platform key file not written: {done['stderr'].strip()[-300:]}")


def runner_env(
    environ: Mapping[str, str],
    config: RunConfig,
    *,
    index: int,
    home: str,
    root_key: str,
    model_url: str,
    github_url: str,
    key_file: str | None = None,
) -> dict[str, str]:
    return clean_env(
        environ,
        {
            "AGENT_RUNNER_HOME": home,
            "AGENT_RUNNER_HUB_URL": config.public_origin,
            "AGENT_RUNNER_PLATFORM_ROOT_KEY": root_key,
            "AGENT_RUNNER_TEST_MODE": "1",
            "AGENT_RUNNER_VM_PREFIX": config.runner_prefix(index),
            "AGENT_RUNNER_POOL_SIZE": str(config.pool_size),
            "AGENT_RUNNER_VM_MEMORY_MIB": str(config.vm_memory_mib),
            "AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC": model_url,
            "AGENT_RUNNER_MODEL_UPSTREAM_OPENROUTER": model_url,
            "AGENT_RUNNER_GITHUB_API_URL": github_url,
            "AGENT_RUNNER_GITHUB_WEB_URL": github_url,
            "AGENT_RUNNER_GITHUB_CLIENT_ID": GITHUB_CLIENT_ID,
            **({"AGENT_RUNNER_OPENROUTER_KEY_FILE": key_file} if key_file else {}),
        },
    )
