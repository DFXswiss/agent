"""Record configured driver usage as usage.snapshot activities.

A driver's ``usage`` section in ``drivers.json`` is its usage adapter: the
credential file in the account's configuration directory, the HTTPS requests to
make and JSON pointers (RFC 6901) into their responses. The first segment of a
field pointer names a request. A ``period`` adapter records the usage of a
subscription period for the configured usage session; a ``spend`` adapter
records key spend in US dollars for each configured usage account. A
``windows`` adapter records the login's usage windows (a short window, the
week, and the week per model) for each configured usage account.

The request semantics stay here: a bearer token, ``Accept: application/json``,
an 8 second timeout and no redirects. Proxy and certificate settings come from
the process environment only when the adapter sets ``proxy_from_environment``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .drivers import (
    SPEND_FIELDS,
    DriverSpec,
    UsageCredential,
    UsageKeychain,
    UsageRequest,
    UsageSpec,
    expand_template,
    is_home_default_dir,
)
from .store import Store, StoreError, utcnow

USAGE_POLL_SECONDS = 60
USAGE_TIMEOUT_SECONDS = 8.0
# Bound of one secret-store read. The program and its arguments come from the adapter.
KEYCHAIN_READ_TIMEOUT_SECONDS = 10
# A bearer token must be visible ASCII: no whitespace, control or non-ASCII characters.
_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]+")
_COMPARE_KEYS = (
    "vendor",
    "provider",
    "account_email",
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
# Spend fields that may be null, or absent from an existing object, when the
# key has no limit. Every other spend field is a required number.
_NULLABLE_SPEND_FIELDS = frozenset({"limit_usd", "limit_remaining_usd", "limit_reset"})
_PERIOD_TEXT_FIELDS = ("tier", "period_type", "period_start", "period_end")
_PERIOD_INT_FIELDS = ("prepaid_val", "on_demand_cap_val", "on_demand_used_val")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_MISSING = object()

Fetch = Callable[[str, UsageSpec, str], Mapping[str, Any]]


class AuthStale(StoreError):
    """The usage credential is missing or expired. Not a runtime failure of this scan."""


class AccountUsageError(StoreError):
    """One or more configured usage accounts could not be scanned."""

    def __init__(self, errors: list[str], activity_ids: list[str]):
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)
        self.activity_ids = tuple(activity_ids)


class UsageRateLimited(StoreError):
    """The provider answered HTTP 429; ``retry_after`` is its wait in seconds."""

    def __init__(self, message: str, retry_after: float | None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after_seconds(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` value (delay or HTTP date).

    ``None`` when the value is missing or is not a finite number of seconds,
    so an unusable header is treated like a missing one.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds) if math.isfinite(seconds) else None


def is_spend_snapshot(payload: Mapping[str, Any]) -> bool:
    """A spend snapshot names its usage account; a period snapshot an email."""
    return "account" in payload


def load_usage_credential(
    driver: str, credential: UsageCredential, path: Path
) -> tuple[str, str]:
    """Read a ``json-entry`` credential and return ``(token, account)``.

    The first top-level key that starts with ``entry_prefix`` holds the entry.
    Its ``token``, ``expires_at`` and ``account`` fields must be non-empty
    strings. ``expires_at`` is ISO-8601 with an offset; a trailing ``Z`` means
    UTC. A missing file, a missing entry or an expired token is AuthStale.
    Invalid JSON or a missing or invalid field is StoreError. The token is
    never logged, and nothing is refreshed.
    """
    prefix = credential.entry_prefix
    token_field = credential.token
    expires_field = credential.expires_at
    account_field = credential.account
    if prefix is None or token_field is None or expires_field is None or account_field is None:
        raise StoreError(f"{driver} usage credential is not a json-entry credential")
    if not path.is_file():
        raise AuthStale(f"{driver} credential file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"{driver} credential file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise StoreError(f"{driver} credential file is not valid JSON")
    entry: dict[str, Any] | None = None
    for key, value in data.items():
        if isinstance(key, str) and key.startswith(prefix):
            if not isinstance(value, dict):
                raise StoreError(_entry_field_error(driver, token_field))
            entry = value
            break
    if entry is None:
        raise AuthStale(f"{driver} credential file has no entry that starts with {prefix}")
    token = entry.get(token_field)
    if not isinstance(token, str) or token == "":
        raise StoreError(_entry_field_error(driver, token_field))
    expires_at = entry.get(expires_field)
    if not isinstance(expires_at, str) or expires_at == "":
        raise StoreError(_entry_field_error(driver, expires_field))
    parsed = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        when = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise StoreError(f"{driver} credential entry field {expires_field} is invalid") from exc
    if when.tzinfo is None:
        raise StoreError(f"{driver} credential entry field {expires_field} is invalid")
    if datetime.now(timezone.utc) >= when:
        raise AuthStale(f"{driver} credential expired; log in again")
    account = entry.get(account_field)
    if not isinstance(account, str) or account == "":
        raise StoreError(_entry_field_error(driver, account_field))
    return token, account


def _entry_field_error(driver: str, field: str) -> str:
    return f"{driver} credential entry field {field} is missing or invalid"


def load_key_credential(driver: str, path: Path) -> str:
    """Read a ``text`` credential: the key file's content without surrounding space."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise StoreError(f"{driver} key missing") from exc
    if token == "":
        raise StoreError(f"{driver} key missing")
    return token


def _get_document(
    client: httpx.Client, driver: str, request: UsageRequest, token: str
) -> dict[str, Any]:
    label = f"{driver} usage request {request.name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        **dict(request.headers),
    }
    try:
        response = client.get(request.url, headers=headers)
    except httpx.HTTPError as exc:
        # The exception text may repeat request details; only its kind is shown.
        raise StoreError(f"{label} failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise StoreError(f"{label} returned HTTP {response.status_code}")
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise StoreError(f"{label} returned invalid JSON") from None
    if not isinstance(data, dict):
        raise StoreError(f"{label} response is not an object")
    return data


def fetch_usage_documents(
    driver: str,
    spec: UsageSpec,
    token: str,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """GET every configured request in order; return the JSON objects by name.

    The owned client has an 8 second timeout, follows no redirect, and reads
    proxy and certificate settings from the environment only when the adapter
    sets ``proxy_from_environment``. An optional client is caller-owned and is
    not closed. A transport error, a status other than 200, invalid JSON or a
    body that is not an object is a StoreError that never contains the token.
    """
    if client is None:
        with httpx.Client(
            timeout=USAGE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=spec.proxy_from_environment,
        ) as owned:
            return fetch_usage_documents(driver, spec, token, owned)
    return {
        request.name: _get_document(client, driver, request, token)
        for request in spec.requests
    }


def _resolve(root: Any, pointer: str, *, absent_leaf: Any = _MISSING) -> Any:
    """Follow a JSON pointer from ``root``; ``_MISSING`` when it leads nowhere.

    ``absent_leaf`` stands in for a last segment that an existing object lacks.
    """
    segments = [
        segment.replace("~1", "/").replace("~0", "~")
        for segment in pointer.split("/")[1:]
    ]
    current = root
    for position, segment in enumerate(segments):
        if isinstance(current, dict):
            if segment not in current:
                return absent_leaf if position == len(segments) - 1 else _MISSING
            current = current[segment]
        elif isinstance(current, list) and _ARRAY_INDEX.fullmatch(segment):
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _field(
    driver: str, name: str, pointer: str, root: Any, *, absent_leaf: Any = _MISSING
) -> tuple[str, Any]:
    label = f"{driver} usage field {name} at {pointer}"
    value = _resolve(root, pointer, absent_leaf=absent_leaf)
    if value is _MISSING:
        raise StoreError(f"{label} is missing")
    return label, value


def _text(label: str, value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise StoreError(f"{label} must be a non-empty string")
    return value


def _finite(label: str, value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(f"{label} must be a number")
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise StoreError(f"{label} must be a finite number")
    return value


def _integer(label: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(f"{label} must be an integer")
    return value


def period_snapshot(
    driver: str,
    spec: UsageSpec,
    documents: Mapping[str, Any],
    fetched_at: str,
    account_email: str,
) -> dict[str, Any]:
    """Map the period adapter's responses to a period snapshot.

    Text fields are non-empty strings; ``used_percent`` and each product's
    ``used_percent`` are finite numbers stored as floats; the three ``*_val``
    fields are integers. The product list must exist and may be empty. A
    missing field or a wrong type is a StoreError that names the field and its
    pointer; nothing is defaulted or coerced.
    """
    values: dict[str, Any] = {}
    for name in _PERIOD_TEXT_FIELDS:
        values[name] = _text(*_field(driver, name, spec.pointer(name), documents))
    values["used_percent"] = float(
        _finite(*_field(driver, "used_percent", spec.pointer("used_percent"), documents))
    )
    products = spec.products
    if products is None:
        raise StoreError(f"{driver} usage adapter has no products fields")
    label, items = _field(driver, "products", products.list, documents)
    if not isinstance(items, list):
        raise StoreError(f"{label} must be a list")
    product_rows: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        item_name = f"products[{index}]"
        if not isinstance(item, dict):
            raise StoreError(f"{label} item {index} must be an object")
        product = _text(*_field(driver, f"{item_name}.product", products.product, item))
        used = _finite(*_field(driver, f"{item_name}.used_percent", products.used_percent, item))
        product_rows.append({"product": product, "used_percent": float(used)})
    for name in _PERIOD_INT_FIELDS:
        values[name] = _integer(*_field(driver, name, spec.pointer(name), documents))
    account_email = _text(f"{driver} usage account", account_email)
    return {
        "vendor": driver,
        "provider": driver,
        "account_email": account_email,
        "tier": values["tier"],
        "used_percent": values["used_percent"],
        "period_type": values["period_type"],
        "period_start": values["period_start"],
        "period_end": values["period_end"],
        "products": product_rows,
        "prepaid_val": values["prepaid_val"],
        "on_demand_cap_val": values["on_demand_cap_val"],
        "on_demand_used_val": values["on_demand_used_val"],
        "fetched_at": fetched_at,
    }


def spend_snapshot(
    driver: str,
    spec: UsageSpec,
    account: str,
    documents: Mapping[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    """Map the spend adapter's responses to a spend snapshot of one account.

    Amounts are finite numbers, stored as received. ``limit_usd`` and
    ``limit_remaining_usd`` may be null, and ``limit_reset`` may be any scalar;
    those three read an absent last segment as null. Anything else missing or
    of the wrong type is a StoreError that names the field and its pointer.
    """
    snapshot: dict[str, Any] = {"vendor": driver, "provider": driver, "account": account}
    for name in SPEND_FIELDS:
        nullable = name in _NULLABLE_SPEND_FIELDS
        label, value = _field(
            driver,
            name,
            spec.pointer(name),
            documents,
            absent_leaf=None if nullable else _MISSING,
        )
        if name == "limit_reset":
            if isinstance(value, (dict, list, bool)):
                raise StoreError(f"{label} has an invalid type")
        elif not (nullable and value is None):
            value = _finite(label, value)
        snapshot[name] = value
    snapshot["fetched_at"] = fetched_at
    return snapshot


def pick_session_id(store: Store, provider: str) -> str:
    """
    Among rows from store.rows('session') whose _origin_device_id == store.device_id():
    1. first (rows is newest-first) with status=='active' and runtime dict provider==provider
    2. else first with status=='active'
    3. else first owned session of any status
    Else StoreError(f'no owned session to record {provider} usage').
    """
    origin = store.device_id()
    owned = [row for row in store.rows("session") if row.get("_origin_device_id") == origin]
    for row in owned:
        if row.get("status") != "active":
            continue
        runtime = row.get("runtime")
        if isinstance(runtime, dict) and runtime.get("provider") == provider:
            sid = row.get("id")
            if isinstance(sid, str) and sid != "":
                return sid
    for row in owned:
        if row.get("status") != "active":
            continue
        sid = row.get("id")
        if isinstance(sid, str) and sid != "":
            return sid
    for row in owned:
        sid = row.get("id")
        if isinstance(sid, str) and sid != "":
            return sid
    raise StoreError(f"no owned session to record {provider} usage")


def usage_unchanged(
    previous: dict[str, Any] | None,
    snapshot: dict[str, Any],
    compare_keys: Collection[str] = _COMPARE_KEYS,
) -> bool:
    """Compare only ``compare_keys``; ignore fetched_at and any other keys."""
    if previous is None:
        return False
    for key in compare_keys:
        if previous.get(key) != snapshot.get(key):
            return False
    return True


def last_usage_snapshot(
    store: Store, match: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    """Return the newest owned usage payload accepted by ``match``."""
    event = store.last_own_activity_insert(
        "usage.snapshot",
        predicate=lambda row: isinstance(row.get("payload"), dict)
        and match(row["payload"]),
    )
    if event is None:
        return None
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else None


def scan_session_usage(
    store: Store,
    *,
    fetch: Fetch | None = None,
    now: Callable[[], str] | None = None,
) -> str | None:
    """Record the configured usage session's period usage when it changed.

    The usage session's interactive role names the account; its driver's
    period adapter names the credential file in the account's configuration
    directory. An unchanged snapshot records nothing and returns None. A new
    snapshot is inserted through ``store.write_with_advisory`` under the lock
    key ``usage.snapshot:<driver>`` and its activity id is returned. Any
    failure raises; usage is never invented on error.
    """
    from .ai_accounts import load_ai_accounts

    accounts = load_ai_accounts(store.home)
    session_id = accounts.usage_session
    if session_id is None:
        raise AuthStale("AI usage monitoring is not configured")
    row = store.row("session", session_id)
    if row is None or row.get("_origin_device_id") != store.device_id() or row.get("status") != "active":
        raise StoreError("AI usage session must be owned and active")
    account = accounts.for_session(session_id).account
    driver = account.provider
    spec = accounts.usage_adapter(driver, "period", "usage_session")
    token, account_email = load_usage_credential(
        driver, spec.credential, Path(account.config_dir) / spec.credential.file
    )
    documents = (fetch or fetch_usage_documents)(driver, spec, token)
    snapshot = period_snapshot(driver, spec, documents, (now or utcnow)(), account_email)

    def match(payload: dict[str, Any]) -> bool:
        return payload.get("vendor") == driver

    if usage_unchanged(last_usage_snapshot(store, match), snapshot):
        return None
    activity_id = str(uuid.uuid4())
    event = store.write_with_advisory(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "usage.snapshot",
            "payload": snapshot,
            "execution_status": "done",
        },
        lock_key=f"usage.snapshot:{driver}",
        skip=lambda: usage_unchanged(last_usage_snapshot(store, match), snapshot),
    )
    return None if event is None else activity_id


def usage_poll_due(last: float | None, now: float, interval: float = USAGE_POLL_SECONDS) -> bool:
    """True if last is None or now - last >= interval."""
    return last is None or now - last >= interval


def _get_bearer_json(
    url: str,
    headers: dict[str, str],
    name: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET ``url`` with a bearer header; error text never carries the token."""
    response: httpx.Response | None = None
    try:
        if client is None:
            # The bearer must never follow an ambient proxy or trust an ambient
            # CA override; the owned client ignores the process environment.
            with httpx.Client(
                timeout=USAGE_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as owned:
                response = owned.get(url, headers=headers)
        else:
            response = client.get(url, headers=headers)
    except (httpx.HTTPError, UnicodeError):
        # A header value httpx cannot encode raises UnicodeEncodeError, whose
        # ``object`` holds the header with the token. Raising outside this
        # block keeps the original out of both __cause__ and __context__.
        pass
    if response is None:
        raise StoreError(f"{name} request failed")
    if response.status_code == 429:
        raise UsageRateLimited(
            f"{name} returned HTTP 429",
            _retry_after_seconds(response.headers.get("Retry-After")),
        )
    if response.status_code != 200:
        raise StoreError(f"{name} returned HTTP {response.status_code}")
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise StoreError(f"{name} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise StoreError(f"{name} response is not an object")
    return body


def _record_account_snapshot(
    store: Store,
    snapshot: dict[str, Any],
    match: Callable[[dict[str, Any]], bool],
    *,
    provider: str,
    lock_key: str,
) -> str | None:
    """Insert ``snapshot`` unless the newest matching snapshot equals it."""
    compare_keys = tuple(key for key in snapshot if key != "fetched_at")
    if usage_unchanged(last_usage_snapshot(store, match), snapshot, compare_keys):
        return None
    session_id = pick_session_id(store, provider)
    activity_id = str(uuid.uuid4())
    event = store.write_with_advisory(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "usage.snapshot",
            "payload": snapshot,
            "execution_status": "done",
        },
        lock_key=lock_key,
        skip=lambda: usage_unchanged(
            last_usage_snapshot(store, match), snapshot, compare_keys
        ),
    )
    return None if event is None else activity_id


def _record_usage_check(
    store: Store, driver: str, account_name: str, checked_at: str
) -> None:
    """Remember when the configured account ``account_name`` was last read successfully.

    A snapshot is stored only on a change, so its ``fetched_at`` is the last
    change; this meta value is the last successful check. The key names the
    configured account, never a token or an e-mail address.
    """
    store.set_meta(f"usage.checked.{driver}.{account_name}", checked_at)


def scan_account_usage(
    store: Store,
    *,
    fetch: Fetch | None = None,
    now: Callable[[], str] | None = None,
) -> list[str]:
    """Record changed spend snapshots for the configured usage accounts.

    Each account's driver has a spend adapter whose key file lies in the
    account's configuration directory. A failing account does not stop the
    others; the failures are raised together as AccountUsageError, which also
    carries the activities recorded before it.
    """
    from .ai_accounts import load_ai_accounts

    accounts = load_ai_accounts(store.home)
    created: list[str] = []
    errors: list[str] = []
    for account_name in accounts.usage_accounts:
        account = accounts.accounts[account_name]
        # Window accounts are read by scan_window_usage.
        configured = accounts.drivers.get(account.provider)
        if (
            configured is not None
            and configured.usage is not None
            and configured.usage.snapshot == "windows"
        ):
            continue
        driver = account.provider
        try:
            spec = accounts.usage_adapter(
                driver, "spend", f"usage_accounts account {account_name}"
            )
            token = load_key_credential(
                driver, Path(account.config_dir) / spec.credential.file
            )
            documents = (fetch or fetch_usage_documents)(driver, spec, token)
            snapshot = spend_snapshot(
                driver, spec, account_name, documents, (now or utcnow)()
            )

            def match(
                payload: dict[str, Any], name: str = account_name, vendor: str = driver
            ) -> bool:
                return payload.get("vendor") == vendor and payload.get("account") == name

            activity_id = _record_account_snapshot(
                store,
                snapshot,
                match,
                provider=driver,
                lock_key=f"usage.snapshot:{driver}:{account_name}",
            )
            _record_usage_check(store, driver, account_name, snapshot["fetched_at"])
            if activity_id is not None:
                created.append(activity_id)
        except StoreError as exc:
            errors.append(f"{account_name}: {exc}")
    if errors:
        raise AccountUsageError(errors, created)
    return created


KeychainReader = Callable[[str, str], str | None]
WindowFetch = Callable[[str], dict[str, Any]]


def _windows_spec(spec: DriverSpec) -> UsageSpec:
    usage = spec.usage
    if usage is None or usage.snapshot != "windows" or usage.label is None:
        raise StoreError(f"{spec.id} has no windows usage section")
    if usage.poll_seconds is None or usage.account_file is None:
        raise StoreError(f"{spec.id} windows usage section is incomplete")
    if usage.window is None or usage.limits is None:
        raise StoreError(f"{spec.id} windows usage section is incomplete")
    credential = usage.credential
    if (
        credential.label is None
        or credential.object_key is None
        or credential.token is None
        or credential.token_name is None
        or credential.expires_at is None
        or credential.subscription is None
        or credential.login_name is None
        or credential.renew is None
    ):
        raise StoreError(f"{spec.id} windows credential section is incomplete")
    return usage


def keychain_service_name(spec: DriverSpec, config_dir: str) -> str:
    """Secret-store service for the login of ``config_dir``.

    With ``service_suffix`` ``nfc-sha256-8``, the driver's default profile keeps
    the unsuffixed service. Every other directory, and every directory when the
    driver has no ``home_default_dir``, adds the first 8 hex characters of the
    SHA-256 of the NFC form of ``config_dir`` exactly as given.
    """
    usage = _windows_spec(spec)
    keychain = usage.credential.keychain
    if keychain is None:
        raise StoreError(f"{usage.credential.label} keychain is not configured")
    service = keychain.service
    if keychain.service_suffix is None:
        return service
    if spec.home_default_dir is not None and is_home_default_dir(spec, config_dir):
        return service
    if keychain.service_suffix != "nfc-sha256-8":
        raise StoreError(
            f"{usage.credential.label} keychain suffix {keychain.service_suffix} "
            "is not supported"
        )
    digest = hashlib.sha256(
        unicodedata.normalize("NFC", config_dir).encode("utf-8")
    ).hexdigest()[:8]
    return f"{service}-{digest}"


def keychain_user(keychain: UsageKeychain, environ: Mapping[str, str] | None = None) -> str:
    """The secret-store account: the named variable when it matches, else the fallback."""
    source = os.environ if environ is None else environ
    user = source.get(keychain.user_env, "")
    if keychain.user_pattern.fullmatch(user):
        return user
    return keychain.fallback_user


def read_keychain_item(
    keychain: UsageKeychain, service: str, user: str, *, label: str
) -> str | None:
    """Run the configured secret-store argv; ``None`` when the item does not exist.

    The output is the credential itself and is never echoed in an error.
    """
    argv = expand_template(keychain.argv, {"service": service, "user": user})
    # Errors are raised after each ``except`` block: the original exception
    # carries the captured output (the credential), so it must not stay in
    # ``__context__``.
    failure: str | None = None
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        failure = f"{label} keychain read timed out"
    except (OSError, ValueError):
        failure = f"{label} keychain read failed"
    if failure is not None:
        raise StoreError(failure)
    if completed.returncode == keychain.missing_status:
        return None
    if completed.returncode != 0:
        raise StoreError(f"{label} keychain read failed (exit {completed.returncode})")
    text = completed.stdout.strip()
    return text or None


def _login_tier(credential: UsageCredential, subscription: Any, rate_value: Any) -> str | None:
    """Plan name; a configured plan adds the multiplier its pattern captures."""
    plan = subscription.strip() if isinstance(subscription, str) else ""
    if plan == "":
        return None
    multiplier = credential.multiplier
    if (
        multiplier is not None
        and plan.lower() == multiplier.plan.lower()
        and isinstance(rate_value, str)
    ):
        found = multiplier.pattern.search(rate_value.strip())
        if found is not None:
            return f"{multiplier.plan.lower()}_{found.group(1).lower()}"
    return plan


def _parse_login_credentials(
    credential: UsageCredential, raw: str, source: str
) -> tuple[str, str | None]:
    label = credential.label
    object_key = credential.object_key
    token_field = credential.token
    token_name = credential.token_name
    expires_field = credential.expires_at
    if (
        label is None
        or object_key is None
        or token_field is None
        or token_name is None
        or expires_field is None
        or credential.login_name is None
        or credential.renew is None
        or credential.subscription is None
    ):
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
    oauth = data.get(object_key)
    if oauth is None:
        raise AuthStale(f"{label} credentials in {source} hold no {credential.login_name}")
    if not isinstance(oauth, dict):
        raise StoreError(f"{label} credentials in {source}: {object_key} must be an object")
    token = oauth.get(token_field)
    if token is None:
        raise AuthStale(f"{label} credentials in {source} hold no {token_name}")
    if not isinstance(token, str) or token == "":
        raise StoreError(
            f"{label} credentials in {source}: {token_field} must be a non-empty string"
        )
    if not _VISIBLE_ASCII.fullmatch(token):
        raise StoreError(
            f"{label} credentials in {source}: {token_field} must be visible ASCII characters"
        )
    expires_at = oauth.get(expires_field)
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise StoreError(
            f"{label} credentials in {source}: {expires_field} must be epoch milliseconds"
        )
    if credential.expires_unit != "epoch-ms":
        raise StoreError(
            f"{label} credentials in {source}: {expires_field} must be epoch milliseconds"
        )
    try:
        expires_seconds = float(expires_at) / 1000.0
    except OverflowError:
        expires_seconds = math.inf
    if not math.isfinite(expires_seconds):
        raise StoreError(
            f"{label} credentials in {source}: {expires_field} must be epoch milliseconds"
        )
    if datetime.now(timezone.utc).timestamp() >= expires_seconds:
        raise AuthStale(
            f"{label} login token expired; start {credential.renew} to renew it"
        )
    rate_field = credential.multiplier.field if credential.multiplier is not None else None
    rate_value = oauth.get(rate_field) if rate_field is not None else None
    return token, _login_tier(credential, oauth.get(credential.subscription), rate_value)


def load_login_credentials(
    spec: DriverSpec,
    config_dir: str,
    *,
    read_keychain: KeychainReader | None = None,
) -> tuple[str, str | None]:
    """Return ``(access_token, tier)`` for the windows login of ``config_dir``.

    On the keychain's platform the secret-store item of that directory is read
    first, then the credential file; elsewhere only the file. An injected
    reader is used on every platform. A missing login or an expired token is
    ``AuthStale``; malformed data is ``StoreError``. Never refreshes and never
    reads a refresh token. Never log the token.
    """
    usage = _windows_spec(spec)
    credential = usage.credential
    label = credential.label
    keychain = credential.keychain
    keychain_error: StoreError | None = None
    reader = read_keychain
    if reader is None and keychain is not None and sys.platform == keychain.platform:
        def reader(service: str, user: str) -> str | None:
            return read_keychain_item(keychain, service, user, label=label)

    if reader is not None:
        if keychain is None:
            raise StoreError(f"{label} keychain is not configured")
        try:
            raw = reader(keychain_service_name(spec, config_dir), keychain_user(keychain))
        except StoreError as exc:
            keychain_error = exc
        else:
            if raw is not None:
                return _parse_login_credentials(credential, raw, "the keychain")
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
        raise AuthStale(
            f"{label} login not found for {config_dir}; run {credential.renew} auth login"
        )
    return _parse_login_credentials(credential, raw_file, credential.file)


def _login_account_file(spec: DriverSpec, config_dir: str) -> Path:
    usage = _windows_spec(spec)
    account_file = usage.account_file
    if account_file is None:
        raise StoreError(f"{spec.id} windows usage section is incomplete")
    if account_file.default_name is not None and is_home_default_dir(spec, config_dir):
        return Path.home() / account_file.default_name
    return Path(config_dir) / account_file.file


def load_login_account(spec: DriverSpec, config_dir: str) -> str:
    """The login's identity string from the configured account file."""
    usage = _windows_spec(spec)
    credential = usage.credential
    account_file = usage.account_file
    if account_file is None or credential.label is None or credential.renew is None:
        raise StoreError(f"{spec.id} windows usage section is incomplete")
    label = credential.label
    path = _login_account_file(spec, config_dir)
    # Errors are raised after each ``except`` block, never chained.
    readable = True
    exists = False
    try:
        exists = path.is_file()
    except OSError:
        readable = False
    if not readable:
        raise StoreError(f"{label} account file is not readable")
    if not exists:
        raise AuthStale(f"{label} account file not found: {path}")
    data: Any = None
    failure: str | None = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        failure = f"{label} account file is not readable"
    except (UnicodeError, ValueError):
        failure = f"{label} account file is not valid JSON"
    if failure is not None:
        raise StoreError(failure)
    if not isinstance(data, dict):
        raise StoreError(f"{label} account file is not an object")
    account = data.get(account_file.object_key)
    if account is None:
        raise AuthStale(
            f"{label} account file has no {account_file.object_key}; "
            f"run {credential.renew} auth login"
        )
    if not isinstance(account, dict):
        raise StoreError(
            f"{label} account file field {account_file.object_key} must be an object"
        )
    email = account.get(account_file.field)
    if not isinstance(email, str) or email.strip() == "":
        raise StoreError(
            f"{label} account file field {account_file.object_key}.{account_file.field} "
            "is missing or invalid"
        )
    return email


def fetch_window_documents(
    spec: UsageSpec,
    token: str,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """GET every windows request; return the JSON objects by request name.

    The owned client has an 8 second timeout, follows no redirect, and reads
    proxy settings from the environment only when the adapter sets
    ``proxy_from_environment``. Error text uses the adapter's ``label`` and
    never contains the token. HTTP 429 is ``UsageRateLimited``.
    """
    if spec.label is None:
        raise StoreError("windows usage adapter has no label")
    if client is None:
        with httpx.Client(
            timeout=USAGE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=spec.proxy_from_environment,
        ) as owned:
            return fetch_window_documents(spec, token, owned)
    documents: dict[str, dict[str, Any]] = {}
    for request in spec.requests:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **dict(request.headers),
        }
        documents[request.name] = _get_bearer_json(request.url, headers, spec.label, client)
    return documents


def _pointer_leaf(pointer: str) -> str:
    return pointer.split("/")[-1].replace("~1", "/").replace("~0", "~")


def _window_reset(value: Any, field: str, label: str) -> str | None:
    """Reset time as UTC ISO-8601 rounded to the nearest minute, or ``None``.

    The same reset is reported with about a second of jitter between calls.
    Rounding to the nearest full minute (30 seconds and more round up) keeps an
    unchanged window equal, so no new snapshot is written for jitter alone.
    """
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise StoreError(f"{label} {field} must be a timestamp string or null")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise StoreError(f"{label} {field} is not a valid timestamp") from None
    if when.tzinfo is None:
        raise StoreError(f"{label} {field} has no time zone")
    try:
        when = when.astimezone(timezone.utc)
        minute = when.replace(second=0, microsecond=0)
        if when - minute >= timedelta(seconds=30):
            minute += timedelta(minutes=1)
    except OverflowError:
        raise StoreError(f"{label} {field} is not a valid timestamp") from None
    return minute.isoformat()


def _usage_window(
    raw: Any, field: str, spec: UsageSpec
) -> dict[str, Any] | None:
    """A reported window, or ``None`` when the response has no value for it."""
    window = spec.window
    label = spec.label
    if window is None or label is None:
        raise StoreError("windows usage adapter has no window fields")
    if window.round != "minute":
        raise StoreError(f"{label} window round {window.round} is not supported")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StoreError(f"{label} {field} must be an object or null")
    used = raw.get(window.used_percent)
    if used is None:
        return None
    return {
        "used_percent": float(_finite(f"{label} {field}.{window.used_percent}", used)),
        "resets_at": _window_reset(
            raw.get(window.resets_at), f"{field}.{window.resets_at}", label
        ),
    }


def _optional_field(documents: Mapping[str, Any], pointer: str) -> Any:
    value = _resolve(documents, pointer, absent_leaf=None)
    return None if value is _MISSING else value


def windows_snapshot(
    driver: str,
    spec: UsageSpec,
    account_email: str,
    tier: str | None,
    documents: Mapping[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    """Map the windows adapter's responses to a ``usage.snapshot`` payload.

    ``windows.five_hour`` and ``windows.weekly`` are ``None`` when the response
    reports no value; a missing window is never shown as 0 %. The weekly window
    is the configured weekly pointer, or the first all-models limit when that
    pointer has no value. ``windows.weekly_models`` lists the limits scoped to
    one model.
    """
    label = spec.label
    limits_spec = spec.limits
    if label is None or limits_spec is None or spec.window is None:
        raise StoreError(f"{driver} windows usage adapter is incomplete")
    five_name = _pointer_leaf(spec.pointer("five_hour"))
    weekly_name = _pointer_leaf(spec.pointer("weekly"))
    limits_name = _pointer_leaf(spec.pointer("limits"))
    weekly_all: dict[str, Any] | None = None
    weekly_models: list[dict[str, Any]] = []
    limits = _optional_field(documents, spec.pointer("limits"))
    if limits is None:
        limits = []
    if not isinstance(limits, list):
        raise StoreError(f"{label} {limits_name} must be a list or null")
    for index, item in enumerate(limits):
        field = f"{limits_name}[{index}]"
        if not isinstance(item, dict):
            raise StoreError(f"{label} {field} must be an object")
        kind = item.get(limits_spec.kind)
        if (
            kind not in (limits_spec.weekly_all, limits_spec.weekly_model)
            or item.get(limits_spec.used_percent) is None
        ):
            continue
        window = {
            "used_percent": float(
                _finite(
                    f"{label} {field}.{limits_spec.used_percent}",
                    item.get(limits_spec.used_percent),
                )
            ),
            "resets_at": _window_reset(
                item.get(limits_spec.resets_at),
                f"{field}.{limits_spec.resets_at}",
                label,
            ),
        }
        if kind == limits_spec.weekly_all:
            if weekly_all is None:
                weekly_all = window
            continue
        scope = item.get(limits_spec.scope)
        model = scope.get(limits_spec.model) if isinstance(scope, dict) else None
        name: str | None = None
        if isinstance(model, dict):
            for key in limits_spec.model_names:
                value = model.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        weekly_models.append({"model": name, **window})
    weekly = (
        _usage_window(_optional_field(documents, spec.pointer("weekly")), weekly_name, spec)
        or weekly_all
    )
    return {
        "vendor": driver,
        "provider": driver,
        "account_email": _text("account_email", account_email),
        "tier": tier,
        "windows": {
            "five_hour": _usage_window(
                _optional_field(documents, spec.pointer("five_hour")), five_name, spec
            ),
            "weekly": weekly,
            "weekly_models": weekly_models,
        },
        "fetched_at": fetched_at,
    }


def scan_window_usage(
    store: Store,
    *,
    fetch: WindowFetch | None = None,
    read_keychain: KeychainReader | None = None,
    now: Callable[[], str] | None = None,
    skipped: list[str] | None = None,
    pace: dict[str, float] | None = None,
    clock: Callable[[], float] | None = None,
) -> list[str]:
    """Record changed window usage for configured ``usage_accounts``.

    An account without a usable login (missing or expired) is skipped quietly;
    its reason is appended to ``skipped`` when the caller passes a list.

    ``pace`` maps an account name to the ``clock`` time before which it is not
    read again. An attempted account waits its adapter's ``poll_seconds``, or
    the server's ``Retry-After`` when that is longer. Without ``pace`` every
    account is read on every call.
    """
    tick = clock or time.monotonic
    from .ai_accounts import load_ai_accounts

    accounts = load_ai_accounts(store.home)
    created: list[str] = []
    errors: list[str] = []
    for account_name in accounts.usage_accounts:
        account = accounts.accounts[account_name]
        driver_spec = accounts.drivers.get(account.provider)
        if (
            driver_spec is None
            or driver_spec.usage is None
            or driver_spec.usage.snapshot != "windows"
        ):
            continue
        spec = _windows_spec(driver_spec)
        poll_seconds = spec.poll_seconds
        if poll_seconds is None:
            raise StoreError(f"{account.provider} windows usage has no poll_seconds")
        if pace is not None:
            started = tick()
            if started < pace.get(account_name, -math.inf):
                continue
            pace[account_name] = started + poll_seconds
        try:
            try:
                email = load_login_account(driver_spec, account.config_dir)
                token, tier = load_login_credentials(
                    driver_spec, account.config_dir, read_keychain=read_keychain
                )
            except AuthStale as exc:
                if skipped is not None:
                    skipped.append(f"{account_name}: {exc}")
                continue
            if fetch is not None:
                body = fetch(token)
                if not isinstance(body, dict):
                    raise StoreError(f"{spec.label} response is not an object")
                if len(spec.requests) != 1:
                    raise StoreError(f"{spec.label} fetch requires exactly one request")
                documents: Mapping[str, Any] = {spec.requests[0].name: body}
            else:
                documents = fetch_window_documents(spec, token)
            snapshot = windows_snapshot(
                account.provider, spec, email, tier, documents, (now or utcnow)()
            )
            key = email.casefold()
            driver = account.provider

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
            _record_usage_check(store, driver, account_name, snapshot["fetched_at"])
            if activity_id is not None:
                created.append(activity_id)
        except UsageRateLimited as exc:
            message = f"{account_name}: {exc}"
            if exc.retry_after is not None:
                message += f"; retry after {math.ceil(exc.retry_after)} s"
                if pace is not None and exc.retry_after > poll_seconds:
                    pace[account_name] = tick() + exc.retry_after
            errors.append(message)
        except StoreError as exc:
            errors.append(f"{account_name}: {exc}")
    if errors:
        raise AccountUsageError(errors, created)
    return created


