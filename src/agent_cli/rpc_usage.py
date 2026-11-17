"""Record usage windows that a driver's own local JSON-RPC server reports.

An ``rpc`` usage adapter in ``drivers.json`` names a program (``argv``) that
speaks JSON-RPC 2.0 on stdio, one JSON message per line. Agent starts it with
the account's configuration directory in the driver's ``home_env`` (left unset
for the driver's ``home_default_dir``), sends ``initialize`` with the configured
``initialize_params``, the ``initialized`` notification and the configured
``method``, and maps the limits object of the answer to a ``usage.snapshot``
shaped like a ``windows`` snapshot. Agent never reads the login tokens: the
program authenticates itself. The identity comes from the adapter's
``account_file``, optionally from one claim of an ID token's payload.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any

from .drivers import DriverSpec, RpcUsageSpec, is_home_default_dir, role_clear_env
from .store import Store, StoreError, utcnow
from .usage import (
    AccountUsageError,
    AuthStale,
    _finite,
    _record_account_snapshot,
    _record_usage_check,
    _text,
    account_file_path,
    read_account_identity,
)

# How long a terminated server may take to exit before it is killed.
_STOP_SECONDS = 5.0
_INITIALIZE_ID = 1
_REQUEST_ID = 2
_PRINTABLE = re.compile(r"[^\x20-\x7e]+")
_MAX_ERROR_DETAIL = 200

RpcRead = Callable[[DriverSpec, str], dict[str, Any]]


def rpc_spec(driver: DriverSpec) -> RpcUsageSpec:
    """The driver's rpc usage adapter; ``StoreError`` for any other driver."""
    usage = driver.usage
    if not isinstance(usage, RpcUsageSpec):
        raise StoreError(f"{driver.id} has no rpc usage section")
    return usage


def rpc_child_env(
    driver: DriverSpec,
    config_dir: str,
    clear: Collection[str],
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The server's environment: no credential, home, proxy or CA variables.

    ``clear`` holds every configured credential and home variable and the
    network overrides. The driver's ``home_env`` then names ``config_dir``,
    except for the driver's ``home_default_dir``, which the driver selects
    when the variable is unset.
    """
    if driver.home_env is None:
        raise StoreError(f"{driver.id} rpc usage requires home_env")
    source = os.environ if environ is None else environ
    removed = set(clear)
    env = {key: value for key, value in source.items() if key not in removed}
    if not is_home_default_dir(driver, config_dir):
        env[driver.home_env] = config_dir
    return env


def _requests(spec: RpcUsageSpec) -> str:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": _INITIALIZE_ID,
            "method": "initialize",
            "params": json.loads(spec.initialize_params),
        },
        {"jsonrpc": "2.0", "method": "initialized"},
        {
            "jsonrpc": "2.0",
            "id": _REQUEST_ID,
            "method": spec.method,
            "params": json.loads(spec.params),
        },
    ]
    return "".join(json.dumps(message) + "\n" for message in messages)


def _pump(stream: Any, lines: queue.Queue[Any], label: str) -> None:
    """Forward stdout lines; ``None`` marks the end, a ``StoreError`` a read failure."""
    try:
        for line in iter(stream.readline, ""):
            lines.put(line)
    except UnicodeError:
        lines.put(StoreError(f"{label} output is not valid UTF-8"))
    except (OSError, ValueError):
        lines.put(StoreError(f"{label} output could not be read"))
    lines.put(None)


def _stop(process: Any) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _close(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except BrokenPipeError:
        # The child has exited; input it never read no longer matters.
        pass


def _error_detail(error: Any) -> str:
    """A short, single-line ``: <code> <message>`` from a JSON-RPC error, or ``""``."""
    if not isinstance(error, dict):
        return ""
    parts: list[str] = []
    code = error.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(str(code))
    message = error.get("message")
    if isinstance(message, str):
        text = _PRINTABLE.sub(" ", message).strip()[:_MAX_ERROR_DETAIL]
        if text:
            parts.append(text)
    return ": " + " ".join(parts) if parts else ""


def read_rpc_result(
    driver: DriverSpec,
    config_dir: str,
    *,
    clear_env: Collection[str],
    popen: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The ``result`` of the adapter's ``method`` from the driver's local server.

    Sends ``initialize``, ``initialized`` and the request, then reads stdout
    lines until the answer with id 2; notifications and other ids are
    ignored. The process is terminated afterwards in every case, and the whole
    exchange is bounded by ``timeout_seconds``. An ``error`` answer, a line
    that is not JSON, early end of output and the time-out are ``StoreError``.
    """
    spec = rpc_spec(driver)
    label = spec.label
    deadline = clock() + spec.timeout_seconds
    env = rpc_child_env(driver, config_dir, clear_env)
    failure: str | None = None
    process: Any = None
    try:
        process = popen(
            list(spec.argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        failure = f"{label} program not found: {spec.argv[0]}"
    except (OSError, ValueError):
        failure = f"{label} could not be started"
    if failure is not None:
        raise StoreError(failure)
    lines: queue.Queue[Any] = queue.Queue()
    reader = threading.Thread(
        target=_pump,
        args=(process.stdout, lines, label),
        name=f"{driver.id}-rpc-usage-stdout",
        daemon=True,
    )
    reader.start()
    try:
        written = True
        try:
            process.stdin.write(_requests(spec))
            process.stdin.flush()
        except (OSError, ValueError):
            written = False
        if not written:
            raise StoreError(f"{label} closed its input")
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise StoreError(f"{label} timed out after {spec.timeout_seconds} s")
            try:
                item = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if item is None:
                code = process.poll()
                suffix = "" if code is None else f" (exit {code})"
                raise StoreError(f"{label} ended before answering{suffix}")
            if isinstance(item, StoreError):
                raise item
            text = item.strip()
            if text == "":
                continue
            message: Any = None
            parsed = True
            try:
                message = json.loads(text)
            except ValueError:
                # Raised below: the decode error holds the line in ``doc``.
                parsed = False
            if not parsed:
                raise StoreError(f"{label} wrote a line that is not JSON")
            if not isinstance(message, dict):
                raise StoreError(f"{label} wrote a message that is not an object")
            request_id = message.get("id")
            if isinstance(request_id, bool) or request_id != _REQUEST_ID:
                continue
            if "error" in message:
                raise StoreError(
                    f"{label} {spec.method} failed{_error_detail(message.get('error'))}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise StoreError(f"{label} {spec.method} result is not an object")
            return result
    finally:
        _stop(process)
        reader.join(timeout=_STOP_SECONDS)
        _close(process.stdin)
        _close(process.stdout)


def _segments(pointer: str) -> list[str]:
    return [
        segment.replace("~1", "/").replace("~0", "~")
        for segment in pointer.split("/")[1:]
    ]


def _lookup(root: Any, pointer: str, label: str, base: str) -> Any:
    """Follow ``pointer`` through objects; ``None`` when a step is null or absent.

    A step that is neither an object nor null is a ``StoreError``, so a changed
    answer is reported instead of read as "no value".
    """
    current = root
    walked: list[str] = []
    for segment in _segments(pointer):
        if current is None:
            return None
        if not isinstance(current, dict):
            name = ".".join([base, *walked]) if walked else base
            raise StoreError(f"{label} {name} must be an object or null")
        walked.append(segment)
        current = current.get(segment)
    return current


def _name(pointer: str, base: str) -> str:
    """Readable name of a pointer for error text, such as ``limits.primary``."""
    return ".".join([base, *_segments(pointer)])


def _rpc_reset(value: Any, field: str, label: str) -> str | None:
    """Epoch seconds as UTC ISO-8601 rounded to the nearest minute, or ``None``.

    Rounding to the nearest full minute (30 seconds and more round up) matches
    the windows adapter, so both compare reset times at the same precision.
    """
    if value is None:
        return None
    seconds = float(_finite(f"{label} {field}", value))
    failure = False
    text = ""
    try:
        minute = math.floor((seconds + 30.0) / 60.0) * 60
        text = datetime.fromtimestamp(minute, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        failure = True
    if failure:
        raise StoreError(f"{label} {field} is not a valid time")
    return text


def _rpc_window(
    raw: Any, name: str, spec: RpcUsageSpec
) -> tuple[int, dict[str, Any]] | None:
    """``(window_minutes, window)`` for a reported window, ``None`` for null."""
    label = spec.label
    fields = spec.fields
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StoreError(f"{label} {name} must be an object or null")
    minutes = raw.get(fields.window_minutes)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise StoreError(
            f"{label} {name}.{fields.window_minutes} must be a positive integer"
        )
    return minutes, {
        "used_percent": float(
            _finite(f"{label} {name}.{fields.used_percent}", raw.get(fields.used_percent))
        ),
        "resets_at": _rpc_reset(
            raw.get(fields.resets_at), f"{name}.{fields.resets_at}", label
        ),
    }


def rpc_snapshot(
    driver: str,
    spec: RpcUsageSpec,
    result: Mapping[str, Any],
    account_email: str,
    fetched_at: str,
) -> dict[str, Any]:
    """Map the method's result to a ``usage.snapshot`` payload.

    The payload has the shape of a ``windows`` snapshot plus ``limit_reached``
    and ``reset_credits_available``. Each configured window goes to the
    snapshot window its length maps to in ``windows_by_minutes``; an unmapped
    length, or a second window of a length already taken, goes to
    ``windows.other`` with ``window_minutes``. A window the result does not
    report stays ``null``, never 0 %. ``limit_reached`` is true for any value
    other than null or false. Nothing else of the result is stored.
    """
    label = spec.label
    fields = spec.fields
    if not isinstance(result, dict):
        raise StoreError(f"{label} result is not an object")
    limits_name = _name(spec.result_pointer, "result")
    limits = _lookup(result, spec.result_pointer, label, "result")
    if not isinstance(limits, dict):
        raise StoreError(f"{label} {limits_name} must be an object")
    tier = _lookup(limits, fields.plan, label, limits_name)
    if tier is not None and not isinstance(tier, str):
        raise StoreError(
            f"{label} {_name(fields.plan, limits_name)} must be a string or null"
        )
    if isinstance(tier, str) and tier.strip() == "":
        tier = None
    windows: dict[str, Any] = {
        "five_hour": None,
        "weekly": None,
        "weekly_models": [],
        "other": [],
    }
    for pointer in fields.windows:
        name = _name(pointer, limits_name)
        reported = _rpc_window(_lookup(limits, pointer, label, limits_name), name, spec)
        if reported is None:
            continue
        minutes, window = reported
        slot = spec.window_slot(minutes)
        if slot is not None and windows[slot] is None:
            windows[slot] = window
        else:
            windows["other"].append({"window_minutes": minutes, **window})
    reached = _lookup(limits, fields.limit_reached, label, limits_name)
    count = _lookup(result, fields.reset_credits_count, label, "result")
    if count is None:
        count = 0
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise StoreError(
            f"{label} {_name(fields.reset_credits_count, 'result')} "
            "must be a non-negative integer"
        )
    return {
        "vendor": driver,
        "provider": driver,
        "account_email": _text("account_email", account_email),
        "tier": tier,
        "windows": windows,
        "limit_reached": reached is not None and reached is not False,
        "reset_credits_available": count,
        "fetched_at": fetched_at,
    }


def load_rpc_account(driver: DriverSpec, config_dir: str) -> str:
    """The login identity of ``config_dir`` from the adapter's account file."""
    spec = rpc_spec(driver)
    return read_account_identity(
        spec.label,
        spec.account_file,
        account_file_path(driver, spec.account_file, config_dir),
        f"log in with {spec.argv[0]} first",
    )


def scan_rpc_usage(
    store: Store,
    *,
    read: RpcRead | None = None,
    now: Callable[[], str] | None = None,
    skipped: list[str] | None = None,
    pace: dict[str, float] | None = None,
    clock: Callable[[], float] | None = None,
) -> list[str]:
    """Record changed rpc usage for the configured ``usage_accounts``.

    ``read`` takes the driver and the account's ``config_dir`` and returns the
    method's result; by default the driver's local server is asked. An
    account without a login is skipped quietly; its reason is appended to
    ``skipped`` when the caller passes a list. ``pace`` maps an account name to
    the ``clock`` time before which it is not read again; an attempted account
    waits its adapter's ``poll_seconds``. Without ``pace`` every account is
    read on every call. Every successful read records the account's
    ``usage.checked.*`` time, also when the snapshot did not change. Failures
    of single accounts are raised together as ``AccountUsageError`` after
    every account was tried.
    """
    tick = clock or time.monotonic
    from .ai_accounts import load_ai_accounts

    accounts = load_ai_accounts(store.home)
    clear = role_clear_env(accounts.drivers)
    created: list[str] = []
    errors: list[str] = []
    for account_name in accounts.usage_accounts:
        account = accounts.accounts[account_name]
        driver_spec = accounts.drivers.get(account.provider)
        if driver_spec is None or not isinstance(driver_spec.usage, RpcUsageSpec):
            continue
        spec = driver_spec.usage
        if pace is not None:
            started = tick()
            if started < pace.get(account_name, -math.inf):
                continue
            pace[account_name] = started + spec.poll_seconds
        try:
            try:
                email = load_rpc_account(driver_spec, account.config_dir)
            except AuthStale as exc:
                if skipped is not None:
                    skipped.append(f"{account_name}: {exc}")
                continue
            if read is not None:
                result = read(driver_spec, account.config_dir)
            else:
                # Resolved at call time so a test can replace the reader.
                result = read_rpc_result(driver_spec, account.config_dir, clear_env=clear)
            driver = account.provider
            snapshot = rpc_snapshot(driver, spec, result, email, (now or utcnow)())
            key = email.casefold()

            def match(payload: dict[str, Any], key: str = key, vendor: str = driver) -> bool:
                value = payload.get("account_email")
                return (
                    payload.get("vendor") == vendor
                    and isinstance(value, str)
                    and value.casefold() == key
                )

            activity_id = _record_account_snapshot(
                store,
                snapshot,
                match,
                provider=driver,
                lock_key=f"usage.snapshot:{driver}:{key}",
            )
            # Also when the snapshot did not change: the read itself succeeded.
            _record_usage_check(store, driver, account_name, key, snapshot["fetched_at"])
            if activity_id is not None:
                created.append(activity_id)
        except StoreError as exc:
            errors.append(f"{account_name}: {exc}")
    if errors:
        raise AccountUsageError(errors, created)
    return created
