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

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
# A browser variable would let a driver tool open pages for the run.
_EXTRA_STRIP_ENV = ("BROWSER",)


@dataclass(frozen=True)
class KeepaliveResult:
    account: str
    action: str  # "fresh" | "refreshed" | "error" | "skipped"
    expires_at: float | None  # epoch seconds after the action, never the token
    message: str


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
        return (
            f"{OUTPUT_PREFIX} {result.action} {result.account} "
            f"until {format_until(result.expires_at)}"
        )
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
    *,
    drivers: Mapping[str, DriverSpec],
    run: Callable[..., Any],
    clock: Callable[[], float],
    read_keychain: KeychainReader | None,
    environ: Mapping[str, str],
) -> KeepaliveResult:
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
    if expires_at is not None and expires_at - clock() > margin:
        return KeepaliveResult(account_name, "refreshed", expires_at, "")
    reason = "no login after the refresh run" if expires_at is None else "token not renewed"
    message = f"{reason} (exit {completed.returncode})"
    excerpt = _stderr_excerpt(completed.stderr)
    if excerpt:
        message += f": {excerpt}"
    return KeepaliveResult(account_name, "error", expires_at, message)


def keep_alive(
    accounts: AIAccounts,
    *,
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
    command against the subscription.
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
        try:
            expires_at = token_expiry(spec, account.config_dir, read_keychain=read_keychain)
            if expires_at is None:
                results.append(
                    KeepaliveResult(account_name, "skipped", None, NO_LOGIN_MESSAGE)
                )
                continue
            if expires_at - clock() > margin:
                results.append(KeepaliveResult(account_name, "fresh", expires_at, ""))
                continue
            tool = keepalive.argv[0]
            binary = which(tool)
            if binary is None:
                results.append(
                    KeepaliveResult(
                        account_name, "error", expires_at, f"{tool} executable not found on PATH"
                    )
                )
                continue
            results.append(
                _refresh(
                    account_name,
                    spec,
                    [binary, *keepalive.argv[1:]],
                    account.config_dir,
                    margin,
                    drivers=drivers,
                    run=run,
                    clock=clock,
                    read_keychain=read_keychain,
                    environ=base,
                )
            )
        except StoreError as exc:
            results.append(KeepaliveResult(account_name, "error", None, str(exc)))
        except OSError as exc:
            results.append(KeepaliveResult(account_name, "error", None, str(exc)))
    return results
