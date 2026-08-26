"""Record SuperGrok weekly quota snapshots as usage.snapshot activities."""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .store import Store, StoreError, utcnow

# format=credits is required: the weekly SuperGrok pool lives only on that view;
# the default monthly billing body has no currentPeriod.
CREDITS_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
USAGE_POLL_SECONDS = 60
VENDOR = "grok"

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


class AuthStale(StoreError):
    """Grok login missing or expired. Not a runtime failure of this scan."""


def grok_auth_path() -> Path:
    """$GROK_HOME/auth.json if GROK_HOME is set and non-empty, else ~/.grok/auth.json."""
    home = os.environ.get("GROK_HOME")
    if isinstance(home, str) and home != "":
        return Path(home) / "auth.json"
    return Path.home() / ".grok" / "auth.json"


def load_grok_bearer(auth_path: Path | None = None) -> tuple[str, str]:
    """
    Read the SuperGrok OIDC entry.
    Prefer the first top-level key that starts with 'https://auth.x.ai::'.
    Required fields: key (non-empty str), expires_at (non-empty str), email (non-empty str).
    expires_at must parse as aware UTC; if now >= expiry → AuthStale('grok auth token expired; run grok login').
    Parse ISO-8601; replace trailing Z with +00:00. No other date libraries.
    Missing file or no auth.x.ai entry → AuthStale. Invalid JSON, missing key/expires_at/email → StoreError.
    Return (token, account_email). Never log the token.
    Do not refresh tokens. Do not read refresh_token except to ignore it.
    """
    path = auth_path if auth_path is not None else grok_auth_path()
    if not path.is_file():
        raise AuthStale(f"grok auth file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("grok auth file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise StoreError("grok auth file is not valid JSON")
    entry: dict[str, Any] | None = None
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("https://auth.x.ai::"):
            if not isinstance(value, dict):
                raise StoreError("grok auth entry field key is missing or invalid")
            entry = value
            break
    if entry is None:
        raise AuthStale("grok auth file has no auth.x.ai entry")
    token = entry.get("key")
    if not isinstance(token, str) or token == "":
        raise StoreError("grok auth entry field key is missing or invalid")
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, str) or expires_at == "":
        raise StoreError("grok auth entry field expires_at is missing or invalid")
    parsed = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        when = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise StoreError("grok auth entry field expires_at is invalid") from exc
    if when.tzinfo is None:
        raise StoreError("grok auth entry field expires_at is invalid")
    if datetime.now(timezone.utc) >= when:
        raise AuthStale("grok auth token expired; run grok login")
    account_email = entry.get("email")
    if not isinstance(account_email, str) or account_email == "":
        raise StoreError("grok auth entry field email is missing or invalid")
    return token, account_email


def _get_json(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    name: str,
) -> dict[str, Any]:
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise StoreError(f"{name} request failed: {exc}") from exc
    if response.status_code != 200:
        raise StoreError(f"{name} returned HTTP {response.status_code}")
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise StoreError(f"{name} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise StoreError(f"{name} response is not an object")
    return data


def fetch_credits_and_settings(
    token: str, client: httpx.Client | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    GET CREDITS_URL and SETTINGS_URL with httpx.Client(timeout=8.0, follow_redirects=False).
    Headers: Authorization Bearer, Accept application/json, x-xai-token-auth: xai-grok-cli.
    Non-200, HTTPError, invalid JSON, non-object body → StoreError.
    Return (credits_object, settings_object).
    Optional client is caller-owned and is not closed.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "x-xai-token-auth": "xai-grok-cli",
    }
    if client is None:
        with httpx.Client(timeout=8.0, follow_redirects=False) as owned:
            credits = _get_json(owned, CREDITS_URL, headers, "credits")
            settings = _get_json(owned, SETTINGS_URL, headers, "settings")
        return credits, settings
    credits = _get_json(client, CREDITS_URL, headers, "credits")
    settings = _get_json(client, SETTINGS_URL, headers, "settings")
    return credits, settings


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise StoreError(f"{field} must be a non-empty string")
    return value


def _require_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(f"{field} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise StoreError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise StoreError(f"{field} must be a finite number")
    return number


def _require_val_int(obj: Any, field: str) -> int:
    if not isinstance(obj, dict):
        raise StoreError(f"{field} must be an object")
    val = obj.get("val")
    if isinstance(val, bool) or not isinstance(val, int):
        raise StoreError(f"{field}.val must be an int")
    return val


def snapshot_from_payloads(
    credits: dict[str, Any],
    settings: dict[str, Any],
    fetched_at: str,
    account_email: str,
) -> dict[str, Any]:
    """
    Return:
    {
      "vendor": "grok",
      "provider": "grok",
      "account_email": <account_email str>,
      "tier": <settings.subscription_tier_display str>,
      "used_percent": <float of config.creditUsagePercent>,
      "period_type": <config.currentPeriod.type str>,
      "period_start": <config.currentPeriod.start str>,
      "period_end": <config.currentPeriod.end str>,
      "products": [{"product": str, "used_percent": float}, ...],  # from config.productUsage; empty list allowed
      "prepaid_val": <int config.prepaidBalance.val>,
      "on_demand_cap_val": <int config.onDemandCap.val>,
      "on_demand_used_val": <int config.onDemandUsed.val>,
      "fetched_at": fetched_at,
    }
    creditUsagePercent and usagePercent: int or float (not bool). Store as float.
    val fields: int (not bool, not float).
    productUsage missing → StoreError (do not default to []).
    productUsage must be a list; each item a dict with product str non-empty and usagePercent number.
    account_email must be a non-empty str.
    Any missing/wrong type → StoreError naming the field. No defaults, no coercion of the monthly shape.
    """
    if not isinstance(credits, dict):
        raise StoreError("credits must be an object")
    config = credits.get("config")
    if not isinstance(config, dict):
        raise StoreError("config must be an object")
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        raise StoreError("currentPeriod must be an object")
    period_type = _require_nonempty_str(period.get("type"), "currentPeriod.type")
    period_start = _require_nonempty_str(period.get("start"), "currentPeriod.start")
    period_end = _require_nonempty_str(period.get("end"), "currentPeriod.end")
    used_percent = _require_float(config.get("creditUsagePercent"), "creditUsagePercent")
    if "productUsage" not in config:
        raise StoreError("productUsage is missing")
    product_usage = config.get("productUsage")
    if not isinstance(product_usage, list):
        raise StoreError("productUsage must be a list")
    products: list[dict[str, Any]] = []
    for item in product_usage:
        if not isinstance(item, dict):
            raise StoreError("productUsage item must be an object")
        product = _require_nonempty_str(item.get("product"), "productUsage.product")
        item_used = _require_float(item.get("usagePercent"), "productUsage.usagePercent")
        products.append({"product": product, "used_percent": item_used})
    prepaid_val = _require_val_int(config.get("prepaidBalance"), "prepaidBalance")
    on_demand_cap_val = _require_val_int(config.get("onDemandCap"), "onDemandCap")
    on_demand_used_val = _require_val_int(config.get("onDemandUsed"), "onDemandUsed")
    if not isinstance(settings, dict):
        raise StoreError("settings must be an object")
    tier = settings.get("subscription_tier_display")
    if not isinstance(tier, str) or tier == "":
        raise StoreError("subscription_tier_display must be a non-empty string")
    account_email = _require_nonempty_str(account_email, "account_email")
    return {
        "vendor": VENDOR,
        "provider": VENDOR,
        "account_email": account_email,
        "tier": tier,
        "used_percent": used_percent,
        "period_type": period_type,
        "period_start": period_start,
        "period_end": period_end,
        "products": products,
        "prepaid_val": prepaid_val,
        "on_demand_cap_val": on_demand_cap_val,
        "on_demand_used_val": on_demand_used_val,
        "fetched_at": fetched_at,
    }


def pick_session_id(store: Store) -> str:
    """
    Among rows from store.rows('session') whose _origin_device_id == store.device_id():
    1. first (rows is newest-first) with status=='active' and runtime dict provider=='grok'
    2. else first with status=='active'
    3. else first owned session of any status
    Else StoreError('no owned session to record grok usage').
    """
    origin = store.device_id()
    owned = [row for row in store.rows("session") if row.get("_origin_device_id") == origin]
    for row in owned:
        if row.get("status") != "active":
            continue
        runtime = row.get("runtime")
        if isinstance(runtime, dict) and runtime.get("provider") == "grok":
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
    raise StoreError("no owned session to record grok usage")


def usage_unchanged(previous: dict[str, Any] | None, snapshot: dict[str, Any]) -> bool:
    """Compare vendor, provider, account_email, tier, used_percent, period_type, period_start, period_end, products, prepaid_val, on_demand_cap_val, on_demand_used_val. Ignore fetched_at and any other keys."""
    if previous is None:
        return False
    for key in _COMPARE_KEYS:
        if previous.get(key) != snapshot.get(key):
            return False
    return True


def last_usage_snapshot(store: Store, vendor: str = VENDOR) -> dict[str, Any] | None:
    """Newest owned activity with type=='usage.snapshot' and payload.vendor==vendor; return its payload dict or None."""
    event = store.last_own_activity_insert("usage.snapshot")
    if event is None:
        return None
    payload = event.get("payload")
    if isinstance(payload, dict) and payload.get("vendor") == vendor:
        return payload
    return None


def scan_usage(
    store: Store,
    *,
    fetch: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
    auth_path: Path | None = None,
    now: Callable[[], str] | None = None,
) -> str | None:
    """
    Load bearer, call fetch or fetch_credits_and_settings, parse, pick session.
    If usage_unchanged(last, snapshot): return None.
    Else insert activity via store.write_with_advisory:
      lock_key='usage.snapshot:grok'
      skip=lambda: usage_unchanged(last_usage_snapshot(store), snapshot)
      payload:
        id, session_id, type='usage.snapshot',
        payload=snapshot, execution_status='done'
    Return new activity id or None.
    Raise StoreError on any failure. Never invent 0% on error.
    fetched_at = (now or utcnow)().
    """
    token, account_email = load_grok_bearer(auth_path)
    credits, settings = (fetch or fetch_credits_and_settings)(token)
    fetched_at = (now or utcnow)()
    snapshot = snapshot_from_payloads(credits, settings, fetched_at, account_email)
    session_id = pick_session_id(store)
    if usage_unchanged(last_usage_snapshot(store), snapshot):
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
        lock_key="usage.snapshot:grok",
        skip=lambda: usage_unchanged(last_usage_snapshot(store), snapshot),
    )
    return None if event is None else activity_id


def usage_poll_due(last: float | None, now: float, interval: float = USAGE_POLL_SECONDS) -> bool:
    """True if last is None or now - last >= interval."""
    return last is None or now - last >= interval
