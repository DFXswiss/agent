"""Checks of `agent-runner doctor` (05-runner.md section 1.2; 03-api.md sections 1 and 6.1).

Every check yields one `Check(name, result, detail, value)`: result `ok`, `warn` or `fail`, a closed `detail` code
and an optional value (versions, counts, seconds; never a secret). The order is fixed:

- `os`: ok for `darwin` (the only supported OS, KONZEPT section 3); fail `unsupported` otherwise.
- `lima`: ok for `limactl` >= 2.3.0; fail `lima_missing` / `lima_version`.
- `vz`: ok when `limactl info` lists the `vz` VM type; fail `vz_unavailable`.
- `postgresql`: ok when the runner store answers (value `auth=trust_loopback`, 05 section 1.4); warn `store_missing`
  before the first `run`/`enroll`; fail `store_unavailable`.
- `golden`: ok with a `ready` golden image; fail `no_golden`.
- `landlock`: ok for Landlock ABI >= 4 in the golden's build record; warn `record_missing`; fail for ABI < 4.
- `login_cli`: the host program of the subscription login (`LOGIN_ARGV`, default `claude setup-token`, 05 section
  11.2), looked up with the login's own `relay.resolve` in the daemon's `PATH` (the `PATH` of the installed
  LaunchDaemon plist; without a plist the caller's); fail `login_cli_missing` when it is missing or cannot be
  executed. For the default command `claude --version` runs with a time limit in a temporary directory with the
  login's allow-list environment (`relay.login_env`; no profile of an account is read): fail `login_cli_missing` when
  it cannot start or exits with a failure; ok with the version when it equals the Claude Code pin of the golden build
  (`images/versions.json` through `golden.load_source`); warn `version_mismatch` (value: the version found),
  `version_unknown` (no answer within the limit, or no version in the output) or `pin_missing` (the pin cannot be
  read; value: the version found). A different `LOGIN_ARGV` (a test double) is only checked for being executable: ok
  `executable`.
- `hub`: ok when a signed `GET /v1/device/state` verifies with the pinned hub key (value `active` or
  `pending_certification`); not enrolled: warn `not_enrolled` when `GET /v1/config` answers; fail with the closed code
  otherwise (`hub_unreachable`, `response_unsigned`, `key_revoked`, ...).
- `clock`: ok for a skew against the hub's `hub_time` below 5 s; warn `no_hub_time`; fail `skew_seconds`.
- `textfile`: ok when the textfile directory is writable or the sink is off; fail `textfile_unwritable`.
- `operator_subscriptions` (KOR-23, ADR-052): ok `off` when `OPERATOR_SUBSCRIPTION_DIR` is not set; otherwise the
  directory is scanned like the daemon does (`model.operator.Pool`): ok `usable` with the number of usable token files
  when every `*.token` file is usable; warn `files_ignored` (value: the number of usable files) when some are
  ignored (rights, owner, symbolic link, format), warn `none_usable` (value: the number of ignored files) when none
  is usable and warn `dir_unusable` (value: the reason) when the directory cannot be used. No token, file name or
  organization is shown.
- `test_mode`: ok for `AGENT_RUNNER_TEST_MODE=0`; warn `test_mode_on` (production MUST run with 0).
- `sync`: ok when nothing waits for an operator; one fail per marker: `device_revoked`, `sync_fatal`
  (`origin_seq_conflict`, ...), `platform_stopped`, `org_stopped`.

The `sync` failures are the conditions that need an operator (05 section 2.3, decisions/RUN-03.md item 11):
`agent-runner doctor --clear <name>` removes the persisted marker after the cause was dealt with; the running daemon
keeps its in-memory state, so it takes effect at the next daemon start.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg

from agent_runner.config import DEFAULT_LOGIN_ARGV, Config
from agent_runner.encoding import b64u_decode
from agent_runner.enroll.platform import fetch_hub_config
from agent_runner.errors import RunnerError
from agent_runner.identity import Identity
from agent_runner.login.relay import LoginFailed, login_env, resolve
from agent_runner.model.operator import IGNORED, USABLE, Pool, configured
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError
from agent_runner.vm import golden as golden_module
from agent_runner.vm import lima as lima_module

OK = "ok"
WARN = "warn"
FAIL = "fail"
MAX_SKEW_S = 5
MIN_LANDLOCK_ABI = 4
STATE_TARGET = "/v1/device/state"
OPERATOR_KEYS = ("sync_fatal", "platform_stopped")
ORG_STOPPED = "org_stopped_"
REVOKED = "device_revoked"
LOGIN_CLI = "login_cli"
OPERATOR_SUBSCRIPTIONS = "operator_subscriptions"
LOGIN_VERSION_TIMEOUT_S = 10.0
_CLI_VERSION_RE = re.compile(r"(?<![\w.])(\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)(?![\w.-])")
"""The first `X.Y.Z[-pre]` of `claude --version` (`2.1.330 (Claude Code)`)."""
FAILED_EXIT = "failed"
"""`login_cli_version`: the command ended with a failure exit."""
_MS_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z")


@dataclass(frozen=True)
class Check:
    name: str
    result: str
    detail: str
    value: str | int | None = None


@dataclass
class DoctorOptions:
    """Injectable parts (tests): hub transport, wall clock, `limactl` runner (default: the one on `PATH`), time limit
    of the login command's version query."""

    transport: httpx.AsyncBaseTransport | None = None
    wall: Callable[[], float] = time.time
    limactl: lima_module.LimactlRun | None = None
    login_timeout: float = LOGIN_VERSION_TIMEOUT_S


@dataclass
class StoreFacts:
    """What the checks read from the store."""

    exists: bool = True
    """False when the runner has no store yet (before the first `run` or `enroll`)."""
    reachable: bool = False
    golden: str | None = None
    state: dict[str, str] = field(default_factory=dict)


def check_os(platform: str) -> Check:
    return (
        Check("os", OK, "supported", "darwin") if platform == "darwin" else Check("os", FAIL, "unsupported", platform)
    )


def check_lima(limactl: lima_module.LimactlRun) -> Check:
    client = lima_module.Lima(limactl)
    try:
        found = client.check_version()
    except lima_module.LimaVersionError:
        return Check("lima", FAIL, "lima_version")
    except lima_module.LimaError:
        return Check("lima", FAIL, "lima_missing")
    return Check("lima", OK, "version", ".".join(str(part) for part in found))


def check_vz(limactl: lima_module.LimactlRun) -> Check:
    done = limactl(["info"], 30.0)
    try:
        info = json.loads(done.stdout) if done.returncode == 0 else None
    except ValueError:
        info = None
    types = info.get("vmTypes") if isinstance(info, dict) else None
    if isinstance(types, list) and "vz" in types:
        return Check("vz", OK, "available")
    return Check("vz", FAIL, "vz_unavailable")


def read_store(dsn: str | None) -> StoreFacts:
    if dsn is None:
        return StoreFacts(exists=False)
    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT version FROM runner.golden WHERE state = 'ready' ORDER BY built_at DESC NULLS LAST, version"
                " DESC LIMIT 1"
            ).fetchone()
            state = {str(key): str(value) for key, value in conn.execute("SELECT key, value FROM runner.sync_state")}
    except psycopg.Error:
        return StoreFacts()
    return StoreFacts(reachable=True, golden=None if row is None else str(row[0]), state=state)


def check_store(facts: StoreFacts) -> Check:
    if not facts.exists:
        return Check("postgresql", WARN, "store_missing")
    if not facts.reachable:
        return Check("postgresql", FAIL, "store_unavailable")
    return Check("postgresql", OK, "reachable", "auth=trust_loopback")


def check_golden(facts: StoreFacts) -> Check:
    if facts.golden is None:
        return Check("golden", FAIL, "no_golden")
    return Check("golden", OK, "ready", facts.golden)


def check_landlock(facts: StoreFacts, records: Path) -> Check:
    if facts.golden is None:
        return Check("landlock", FAIL, "no_golden")
    try:
        document = json.loads((records / f"{facts.golden}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Check("landlock", WARN, "record_missing")
    build = document.get("build") if isinstance(document, dict) else None
    abi = build.get("landlock_abi") if isinstance(build, dict) else None
    if not isinstance(abi, int) or isinstance(abi, bool):
        return Check("landlock", WARN, "record_missing")
    return Check("landlock", OK if abi >= MIN_LANDLOCK_ABI else FAIL, "abi", abi)


def login_cli_version(program: str, timeout: float) -> str | None:
    """The version `program --version` reports, run like the login: in a new temporary directory (0700, removed
    afterwards) with `relay.login_env` as the complete environment and its own process group. `FAILED_EXIT` for a
    failure exit; None when it does not end within `timeout` seconds (the group is killed; a descendant that left the
    group and keeps the output open is waited for once more as long, then left) or prints no version; `OSError` when
    it cannot be started."""
    with tempfile.TemporaryDirectory(prefix="agent-runner-doctor-") as temporary:
        workdir = Path(temporary)
        for sub in ("home", "config", "tmp"):
            (workdir / sub).mkdir(mode=0o700)
        with subprocess.Popen(
            [program, "--version"],
            cwd=workdir,
            env=login_env(workdir, program),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.communicate(timeout=timeout)
                return None
    if process.returncode != 0:
        return FAILED_EXIT
    match = _CLI_VERSION_RE.search(output.decode("utf-8", "replace"))
    return None if match is None else match.group(1)


def golden_pin(template: Path | None) -> str | None:
    """The Claude Code version the golden build pins (the same source as `image build`); None when unreadable."""
    try:
        return golden_module.load_source(template).claude_version
    except RunnerError:
        return None


def check_login_cli(config: Config, search_path: str, timeout: float) -> Check:
    try:
        program = resolve(config.login_argv, search_path)[0]
    except LoginFailed:
        return Check(LOGIN_CLI, FAIL, "login_cli_missing")
    if config.login_argv != DEFAULT_LOGIN_ARGV:
        return Check(LOGIN_CLI, OK, "executable")
    try:
        found = login_cli_version(program, timeout)
    except OSError:
        found = FAILED_EXIT
    if found == FAILED_EXIT:
        return Check(LOGIN_CLI, FAIL, "login_cli_missing")
    if found is None:
        return Check(LOGIN_CLI, WARN, "version_unknown")
    pin = golden_pin(config.golden_template)
    if pin is None:
        return Check(LOGIN_CLI, WARN, "pin_missing", found)
    return Check(LOGIN_CLI, OK, "version", found) if found == pin else Check(LOGIN_CLI, WARN, "version_mismatch", found)


def _ms_time(text: object) -> float | None:
    match = _MS_TIME_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    year, month, day, hour, minute, second, milli = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp() + milli / 1000
    except ValueError:
        return None


async def _state(identity: Identity, options: DoctorOptions) -> Mapping[str, Any]:
    client = HubClient(
        hub_url=identity.device.hub_url,
        key_id="device." + uuid.UUID(identity.device.device_id).hex,
        signing_key=identity.keys.signing_key(),
        hub_key=b64u_decode(identity.device.hub_key),
        transport=options.transport,
        wall=options.wall,
    )
    try:
        return await client.request("GET", STATE_TARGET)
    finally:
        await client.aclose()


def _anonymous(config: Config, options: DoctorOptions) -> Check:
    """Not enrolled: is the configured hub reachable at all?"""
    if config.hub_url is not None:
        try:
            asyncio.run(fetch_hub_config(config.hub_url, options.transport))
        except RunnerError as error:
            return Check("hub", FAIL, error.code)
    return Check("hub", WARN, "not_enrolled")


def check_hub(config: Config, identity: Identity | None, options: DoctorOptions) -> tuple[Check, Check]:
    """The hub check and the clock check (from the same answer)."""
    if identity is None:
        return _anonymous(config, options), Check("clock", WARN, "no_hub_time")
    try:
        answer = asyncio.run(_state(identity, options))
    except HubRefusal as refusal:
        return Check("hub", FAIL, refusal.code), Check("clock", WARN, "no_hub_time")
    except HubTransportError as error:
        # an answer outside the signature window says the clocks differ by more than 120 s
        clock = Check("clock", FAIL, "skew_seconds", ">120") if error.reason == "response_time_skew" else None
        return Check("hub", FAIL, error.reason), clock or Check("clock", WARN, "no_hub_time")
    state = answer.get("state")
    hub = Check("hub", OK, "pinned_key", state if state in ("active", "pending_certification") else "unknown")
    hub_time = _ms_time(answer.get("hub_time"))
    if hub_time is None:
        return hub, Check("clock", WARN, "no_hub_time")
    skew = options.wall() - hub_time
    return hub, Check("clock", OK if abs(skew) < MAX_SKEW_S else FAIL, "skew_seconds", round(skew))


def check_textfile(directory: Path | None) -> Check:
    if directory is None:
        return Check("textfile", OK, "off")
    probe = directory / f".doctor-{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        probe.unlink()
    except OSError:
        return Check("textfile", FAIL, "textfile_unwritable")
    return Check("textfile", OK, "writable")


def check_operator_subscriptions(config: Config) -> Check:
    settings = configured(config)
    if settings is None:
        return Check(OPERATOR_SUBSCRIPTIONS, OK, "off")
    pool = Pool(directory=settings[0], orgs=settings[1])
    pool.scan()
    if pool.problem is not None:
        return Check(OPERATOR_SUBSCRIPTIONS, WARN, "dir_unusable", pool.problem)
    states = pool.summary().states
    if states[USABLE] == 0:
        return Check(OPERATOR_SUBSCRIPTIONS, WARN, "none_usable", states[IGNORED])
    if states[IGNORED]:
        return Check(OPERATOR_SUBSCRIPTIONS, WARN, "files_ignored", states[USABLE])
    return Check(OPERATOR_SUBSCRIPTIONS, OK, "usable", states[USABLE])


def check_test_mode(test_mode: bool) -> Check:
    return Check("test_mode", WARN, "test_mode_on") if test_mode else Check("test_mode", OK, "off")


def check_sync(facts: StoreFacts) -> list[Check]:
    """One failure per marker that waits for an operator (none: one `ok`)."""
    found: list[Check] = []
    state = facts.state
    if REVOKED in state:
        found.append(Check("sync", FAIL, REVOKED, state[REVOKED]))
    for key in OPERATOR_KEYS:
        if key in state:
            found.append(Check("sync", FAIL, key, state[key]))
    for key in sorted(state):
        if key.startswith(ORG_STOPPED):
            found.append(Check("sync", FAIL, "org_stopped", f"{key[len(ORG_STOPPED) :]}:{state[key]}"))
    return found or [Check("sync", OK, "clear")]


def clearable(name: str) -> bool:
    """A marker `doctor --clear` may remove (never `device_revoked`: a revoked device enrolls again)."""
    return name in OPERATOR_KEYS or bool(re.fullmatch(ORG_STOPPED + "[0-9a-f]{32}", name))


def clear_marker(dsn: str, name: str) -> bool:
    try:
        with psycopg.connect(dsn) as conn:
            cur = conn.execute("DELETE FROM runner.sync_state WHERE key = %s", (name,))
            return cur.rowcount == 1
    except psycopg.Error:
        raise RunnerError("store_unavailable") from None
