"""Keep the login of polled window-usage accounts fresh.

Some driver tools renew their access token only while they run. An idle
profile's token expires and the window usage reader then skips the account.
When the operator enables ``token_keepalive`` in ``ai-accounts.json``, this
module runs the driver's configured ``usage.keepalive.argv`` once for each
window usage account whose login is close to expiry, so the tool renews the
login itself. Agent never calls a token endpoint, never uses the refresh token
and never reads, prints or stores a token.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from .ai_accounts import AIAccounts
from .drivers import (
    DRIVERS_FILE,
    DriverSpec,
    UsageCredential,
    is_home_default_dir,
    role_clear_env,
)
from .store import StoreError
from .usage import (
    KeychainReader,
    _windows_spec,
    keychain_service_name,
    keychain_user,
    read_keychain_item,
    usage_poll_due,
)

KEEPALIVE_CHECK_SECONDS = 5 * 60
REFRESH_TIMEOUT_SECONDS = 180
STDERR_LIMIT = 200
OUTPUT_PREFIX = "token.keepalive"
DEFAULT_PROFILE_MESSAGE = "default profile is not kept alive"
NO_LOGIN_MESSAGE = "no login; run agent account-login"
NO_KEEPALIVE_MESSAGE = (
    "driver {driver} has no usage.keepalive in "
    f"{DRIVERS_FILE}; it cannot be kept alive"
)
REFRESH_IN_PROGRESS_MESSAGE = "refresh in progress"
SHORT_LIFETIME_MESSAGE = "lifetime shorter than refresh_before_minutes"
# After a renewal whose lifetime is within the margin, the account waits until
# this long before the new expiry instead of running again on every check.
SHORT_LIFETIME_LEAD_SECONDS = 2 * KEEPALIVE_CHECK_SECONDS
# Growing wait after an error: 10 minutes, doubled per further error, at most 6 hours.
ERROR_BACKOFF_SECONDS = 2 * KEEPALIVE_CHECK_SECONDS
ERROR_BACKOFF_MAX_SECONDS = 6 * 3600
# One lock file per profile directory, shared with ``agent account-login``.
PROFILE_LOCK_DIR = "token-keepalive"
# A browser variable would let a driver tool open pages for the run.
_EXTRA_STRIP_ENV = ("BROWSER",)


@dataclass(frozen=True)
class KeepaliveResult:
    account: str
    action: str  # "fresh" | "refreshed" | "error" | "skipped"
    expires_at: float | None  # epoch seconds after the action, never the token
    message: str


@dataclass
class KeepalivePace:
    """The daemon's per-account not-before times, kept across keep-alive passes.

    An account whose not-before time lies in the future is not examined. An
    error sets it to a growing wait (``error_backoff``); a fresh or renewed
    login clears the error count. A renewal whose new lifetime is within the
    margin defers the account until shortly before the new expiry.
    """

    not_before: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    def waiting(self, account: str, now: float) -> bool:
        return now < self.not_before.get(account, -math.inf)

    def record(self, result: KeepaliveResult, now: float, margin: float) -> None:
        account = result.account
        if result.action == "error":
            count = self.failures.get(account, 0) + 1
            self.failures[account] = count
            self.not_before[account] = now + error_backoff(count)
            return
        if result.action not in ("fresh", "refreshed"):
            return
        self.failures.pop(account, None)
        expires_at = result.expires_at
        if expires_at is None:
            raise StoreError(f"keep-alive result {result.action} has no expiry")
        if expires_at - now <= margin:
            self.not_before[account] = max(now, expires_at - SHORT_LIFETIME_LEAD_SECONDS)
        else:
            self.not_before.pop(account, None)


def error_backoff(failures: int) -> float:
    """Seconds to wait after the ``failures``-th error in a row (``failures`` >= 1)."""
    if failures < 1:
        raise StoreError("keep-alive backoff needs at least one failure")
    exponent = min(failures - 1, 16)
    return float(min(ERROR_BACKOFF_MAX_SECONDS, ERROR_BACKOFF_SECONDS * 2**exponent))


def profile_lock_path(agent_home: Path, config_dir: str) -> Path:
    """``$AGENT_HOME/token-keepalive/<hash of the absolute profile dir>.lock``."""
    absolute = os.path.abspath(os.path.expanduser(config_dir))
    digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:32]
    return agent_home / PROFILE_LOCK_DIR / f"{digest}.lock"


def _open_profile_lock(agent_home: Path, config_dir: str) -> IO[str]:
    path = profile_lock_path(agent_home, config_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        os.chmod(handle.name, 0o600)
    except OSError:
        handle.close()
        raise
    return handle


@contextmanager
def try_profile_lock(agent_home: Path, config_dir: str) -> Iterator[bool]:
    """Hold the profile's lock without waiting; yields ``False`` when it is taken.

    The lock is an exclusive ``flock`` across processes: a keep-alive pass of
    the daemon, ``agent watch token-keepalive`` and ``agent account-login`` never
    renew or sign in the same profile at the same time.
    """
    handle = _open_profile_lock(agent_home, config_dir)
    try:
        acquired = True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        handle.close()


@contextmanager
def hold_profile_lock(
    agent_home: Path, config_dir: str, *, on_wait: Callable[[], None]
) -> Iterator[None]:
    """Hold the profile's lock, waiting for it; ``on_wait`` runs before a wait."""
    handle = _open_profile_lock(agent_home, config_dir)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            on_wait()
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _expiry_from_credentials(
    credential: UsageCredential, raw: str, source: str
) -> float | None:
    """The login's expiry in epoch seconds; ``None`` without a login object."""
    label = credential.label
    object_key = credential.object_key
    expires_field = credential.expires_at
    if label is None or object_key is None or expires_field is None:
        raise StoreError("windows credential section is incomplete")
    data: Any = None
    valid = True
    try:
        data = json.loads(raw)
    except ValueError:
        # Raised below: the decode error holds the credential text in ``doc``.
        valid = False
    if not valid:
        raise StoreError(f"{label} credentials in {source} are not valid JSON")
    if not isinstance(data, dict):
        raise StoreError(f"{label} credentials in {source} are not an object")
    login = data.get(object_key)
    if login is None:
        return None
    if not isinstance(login, dict):
        raise StoreError(f"{label} credentials in {source}: {object_key} must be an object")
    unit_error = f"{label} credentials in {source}: {expires_field} must be epoch milliseconds"
    if credential.expires_unit != "epoch-ms":
        raise StoreError(unit_error)
    expires_at = login.get(expires_field)
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise StoreError(unit_error)
    try:
        seconds = float(expires_at) / 1000.0
    except OverflowError:
        seconds = math.inf
    usable = math.isfinite(seconds)
    if usable:
        try:
            datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            usable = False
    if not usable:
        raise StoreError(unit_error)
    return seconds


def token_expiry(
    spec: DriverSpec,
    config_dir: str,
    *,
    read_keychain: KeychainReader | None = None,
) -> float | None:
    """Epoch seconds of the login's configured expiry; ``None`` without a login.

    Same source order as ``usage.load_login_credentials``: on the platform of a
    configured ``credential.keychain`` the secret-store item of the directory
    first, then ``<config_dir>/<credential.file>``. An injected reader replaces
    the configured keychain on every platform; without a configured keychain
    only the file is read. A keychain read error falls back to the file;
    without a file that error is raised. Malformed data is ``StoreError``.
    Unlike the usage reader, an expired login still returns its expiry. Never
    returns or logs the token.
    """
    usage = _windows_spec(spec)
    credential = usage.credential
    label = credential.label
    keychain = credential.keychain
    keychain_error: StoreError | None = None
    reader = read_keychain if keychain is not None else None
    if reader is None and keychain is not None and sys.platform == keychain.platform:

        def reader(service: str, user: str) -> str | None:
            return read_keychain_item(keychain, service, user, label=str(label))

    if reader is not None and keychain is not None:
        try:
            raw = reader(keychain_service_name(spec, config_dir), keychain_user(keychain))
        except StoreError as exc:
            keychain_error = exc
        else:
            if raw is not None:
                return _expiry_from_credentials(credential, raw, "the keychain")
    path = Path(config_dir) / credential.file
    raw_file: str | None = None
    readable = True
    try:
        if path.is_file():
            raw_file = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        # Raised below so that the original stays out of ``__context__``.
        readable = False
    if not readable:
        raise StoreError(f"{label} credentials file is not readable")
    if raw_file is None:
        if keychain_error is not None:
            raise keychain_error
        return None
    return _expiry_from_credentials(credential, raw_file, credential.file)


def driver_env(
    spec: DriverSpec,
    drivers: Mapping[str, DriverSpec],
    config_dir: str,
    base: Mapping[str, str],
) -> dict[str, str]:
    """Environment for running the driver's own tool for one profile.

    A copy of ``base`` without every variable a role prefix clears (all
    configured credential and home variables, the network and CA overrides)
    and without ``BROWSER``. The driver's ``home_env`` is then set to
    ``config_dir`` unless that is the driver's ``home_default_dir``: some tools
    keep the login of their default profile outside that directory, so pointing
    the variable there would find no login.
    """
    home_env = spec.home_env
    if home_env is None:
        raise StoreError(f"driver {spec.id} has no home_env in {DRIVERS_FILE}")
    strip = {*role_clear_env(drivers), home_env, *_EXTRA_STRIP_ENV}
    env = {key: value for key, value in base.items() if key not in strip}
    if not is_home_default_dir(spec, config_dir):
        env[home_env] = config_dir
    return env


def keepalive_due(last: float | None, now: float) -> bool:
    """True when the daemon's keep-alive pass is due (at most every 5 minutes)."""
    return usage_poll_due(last, now, KEEPALIVE_CHECK_SECONDS)


def format_until(expires_at: float) -> str:
    """UTC ISO-8601 to the minute, for example ``2026-11-17T18:05Z``."""
    return datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def describe(result: KeepaliveResult) -> str:
    """The output line for one result (without the stream choice)."""
    if result.action in ("fresh", "refreshed"):
        if result.expires_at is None:
            raise StoreError(f"keep-alive result {result.action} has no expiry")
        line = (
            f"{OUTPUT_PREFIX} {result.action} {result.account} "
            f"until {format_until(result.expires_at)}"
        )
        if result.message:
            line += f" ({result.message})"
        return line
    if result.action == "error":
        return f"{OUTPUT_PREFIX} error: {result.account}: {result.message}"
    if result.action == "skipped":
        return f"{OUTPUT_PREFIX} skipped {result.account}: {result.message}"
    raise StoreError(f"unknown keep-alive action {result.action}")


def _stderr_excerpt(stderr: Any) -> str:
    """At most ``STDERR_LIMIT`` characters of redacted, single-line stderr."""
    if not isinstance(stderr, str) or stderr.strip() == "":
        return ""
    from .coordinator_common import redact

    flat = " ".join(redact(" ".join(stderr.split()), limit=None).split())
    return flat[:STDERR_LIMIT]


def _refresh(
    account_name: str,
    spec: DriverSpec,
    argv: list[str],
    config_dir: str,
    margin: float,
    old_expiry: float,
    *,
    drivers: Mapping[str, DriverSpec],
    run: Callable[..., Any],
    clock: Callable[[], float],
    read_keychain: KeychainReader | None,
    environ: Mapping[str, str],
) -> KeepaliveResult:
    """Run the refresh command once and judge it by the gain over ``old_expiry``.

    A renewal counts when the new expiry is later than the old one and still in
    the future. A renewed lifetime within ``margin`` is reported with
    ``SHORT_LIFETIME_MESSAGE`` so that the caller can defer the account.
    """
    failure: str | None = None
    completed: Any = None
    env = driver_env(spec, drivers, config_dir, environ)
    with tempfile.TemporaryDirectory(prefix="agent-token-keepalive-") as workdir:
        try:
            completed = run(
                argv,
                env=env,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=REFRESH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # The exception carries the model's output; it is not kept.
            failure = f"refresh run timed out after {REFRESH_TIMEOUT_SECONDS} s"
        except OSError as exc:
            failure = f"cannot run {argv[0]}: {exc}"
    if failure is not None:
        return KeepaliveResult(account_name, "error", None, failure)
    expires_at = token_expiry(spec, config_dir, read_keychain=read_keychain)
    now = clock()
    if expires_at is not None and expires_at > old_expiry and expires_at > now:
        if expires_at - now > margin:
            return KeepaliveResult(account_name, "refreshed", expires_at, "")
        return KeepaliveResult(account_name, "refreshed", expires_at, SHORT_LIFETIME_MESSAGE)
    reason = "no login after the refresh run" if expires_at is None else "token not renewed"
    message = f"{reason} (exit {completed.returncode})"
    excerpt = _stderr_excerpt(completed.stderr)
    if excerpt:
        message += f": {excerpt}"
    return KeepaliveResult(account_name, "error", expires_at, message)


def _keep_alive_account(
    account_name: str,
    spec: DriverSpec,
    keepalive_argv: tuple[str, ...],
    config_dir: str,
    margin: float,
    *,
    agent_home: Path,
    drivers: Mapping[str, DriverSpec],
    run: Callable[..., Any],
    clock: Callable[[], float],
    read_keychain: KeychainReader | None,
    which: Callable[[str], str | None],
    environ: Mapping[str, str],
) -> KeepaliveResult:
    expires_at = token_expiry(spec, config_dir, read_keychain=read_keychain)
    if expires_at is None:
        return KeepaliveResult(account_name, "skipped", None, NO_LOGIN_MESSAGE)
    if expires_at - clock() > margin:
        return KeepaliveResult(account_name, "fresh", expires_at, "")
    tool = keepalive_argv[0]
    binary = which(tool)
    if binary is None:
        return KeepaliveResult(
            account_name, "error", expires_at, f"{tool} executable not found on PATH"
        )
    with try_profile_lock(agent_home, config_dir) as acquired:
        if not acquired:
            return KeepaliveResult(account_name, "skipped", None, REFRESH_IN_PROGRESS_MESSAGE)
        # Another process may have renewed or replaced the login before the lock.
        expires_at = token_expiry(spec, config_dir, read_keychain=read_keychain)
        if expires_at is None:
            return KeepaliveResult(account_name, "skipped", None, NO_LOGIN_MESSAGE)
        if expires_at - clock() > margin:
            return KeepaliveResult(account_name, "fresh", expires_at, "")
        return _refresh(
            account_name,
            spec,
            [binary, *keepalive_argv[1:]],
            config_dir,
            margin,
            expires_at,
            drivers=drivers,
            run=run,
            clock=clock,
            read_keychain=read_keychain,
            environ=environ,
        )


def keep_alive(
    accounts: AIAccounts,
    *,
    agent_home: Path,
    pace: KeepalivePace | None,
    run: Callable[..., Any] = subprocess.run,
    clock: Callable[[], float] = time.time,
    read_keychain: KeychainReader | None = None,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> list[KeepaliveResult]:
    """Renew the login of every window usage account that is close to expiry.

    Off (``token_keepalive_seconds`` is ``None``) returns ``[]`` without any
    work. Only ``usage_accounts`` whose driver has a ``windows`` usage adapter
    are considered. The driver's default profile is never kept alive: a refresh
    run from the GUI session may move a file login into the secret store and
    delete the credentials file, so tools using the same profile over SSH
    (locked secret store) would lose the login. A driver without
    ``usage.keepalive`` cannot be kept alive and is skipped. One account's
    failure never stops the others. Each refresh is one run of the configured
    command against the subscription, under the profile's lock in
    ``agent_home``; a taken lock skips the account. With ``pace`` (the
    daemon), an account still waiting after an error or a short renewal is
    left out without a result, and every result updates ``pace``.
    """
    margin = accounts.token_keepalive_seconds
    if margin is None:
        return []
    base = os.environ if environ is None else environ
    drivers = accounts.drivers
    results: list[KeepaliveResult] = []
    for account_name in accounts.usage_accounts:
        account = accounts.accounts[account_name]
        spec = drivers.get(account.provider)
        if spec is None or spec.usage is None or spec.usage.snapshot != "windows":
            continue
        if pace is not None and pace.waiting(account_name, clock()):
            continue
        if is_home_default_dir(spec, account.config_dir):
            results.append(
                KeepaliveResult(account_name, "skipped", None, DEFAULT_PROFILE_MESSAGE)
            )
            continue
        keepalive = spec.usage.keepalive
        if keepalive is None:
            results.append(
                KeepaliveResult(
                    account_name,
                    "skipped",
                    None,
                    NO_KEEPALIVE_MESSAGE.format(driver=spec.id),
                )
            )
            continue
        result: KeepaliveResult
        try:
            result = _keep_alive_account(
                account_name,
                spec,
                keepalive.argv,
                account.config_dir,
                margin,
                agent_home=agent_home,
                drivers=drivers,
                run=run,
                clock=clock,
                read_keychain=read_keychain,
                which=which,
                environ=base,
            )
        except StoreError as exc:
            result = KeepaliveResult(account_name, "error", None, str(exc))
        except OSError as exc:
            result = KeepaliveResult(account_name, "error", None, str(exc))
        results.append(result)
        if pace is not None:
            pace.record(result, clock(), margin)
    return results
