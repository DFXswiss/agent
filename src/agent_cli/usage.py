"""Record configured provider usage as usage.snapshot activities."""

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
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_VENDOR = "openrouter"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_VENDOR = "claude"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_KEYCHAIN_FALLBACK_USER = "claude-code-user"
SECURITY_BIN = "/usr/bin/security"
# The Claude usage endpoint answers per-minute polling with HTTP 429 and a
# Retry-After header, so the poll reads each Claude account at most this often.
CLAUDE_USAGE_POLL_SECONDS = 15 * 60
# ``security`` exit status for an item that does not exist.
_SECURITY_ITEM_NOT_FOUND = 44
_KEYCHAIN_USER = re.compile(r"[a-zA-Z0-9._-]+")
_MAX_MULTIPLIER = re.compile(r"(?:^|_)max_(\d+x)$", re.IGNORECASE)
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


class OpenRouterUsageError(AccountUsageError):
    """One or more configured OpenRouter accounts could not be scanned."""


class ClaudeUsageError(AccountUsageError):
    """One or more configured Claude accounts could not be scanned."""


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
    if auth_path is None:
        raise AuthStale("Grok authentication path must be explicitly configured")
    path = auth_path
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


def pick_session_id(store: Store, provider: str = "grok") -> str:
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
    store: Store,
    match: Callable[[dict[str, Any]], bool] | None = None,
    *,
    vendor: str = VENDOR,
) -> dict[str, Any] | None:
    """Return the newest owned usage payload accepted by ``match``."""
    if match is None:
        def matches(payload: dict[str, Any]) -> bool:
            return payload.get("vendor") == vendor
    else:
        matches = match
    event = store.last_own_activity_insert(
        "usage.snapshot",
        predicate=lambda row: isinstance(row.get("payload"), dict)
        and matches(row["payload"]),
    )
    if event is None:
        return None
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else None


def scan_usage(
    store: Store,
    *,
    fetch: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
    auth_path: Path | None = None,
    now: Callable[[], str] | None = None,
) -> str | None:
    """
    Resolve an explicit usage binding (or injected auth_path), fetch and record.
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
    session_id = None
    if auth_path is None:
        from .ai_accounts import load_ai_accounts
        accounts = load_ai_accounts(store.home)
        session_id = accounts.usage_session
        if session_id is None:
            raise AuthStale("AI usage monitoring is not configured")
        row = store.row("session", session_id)
        if row is None or row.get("_origin_device_id") != store.device_id() or row.get("status") != "active":
            raise StoreError("AI usage session must be owned and active")
        selected = accounts.for_session(session_id)
        auth_path = Path(selected.account.config_dir) / "auth.json"
    token, account_email = load_grok_bearer(auth_path)
    credits, settings = (fetch or fetch_credits_and_settings)(token)
    fetched_at = (now or utcnow)()
    snapshot = snapshot_from_payloads(credits, settings, fetched_at, account_email)
    # Explicit auth_path is an injection hook for callers supplying their own
    # account path; automatic polling always uses the configured usage session.
    if session_id is None:
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


def _get_bearer_json(
    url: str,
    headers: dict[str, str],
    name: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET ``url`` with a bearer header; error text never carries the token."""
    try:
        if client is None:
            # The bearer must never follow an ambient proxy or trust an ambient
            # CA override; the owned client ignores the process environment.
            with httpx.Client(
                timeout=8.0, follow_redirects=False, trust_env=False
            ) as owned:
                response = owned.get(url, headers=headers)
        else:
            response = client.get(url, headers=headers)
    except httpx.HTTPError:
        raise StoreError(f"{name} request failed") from None
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


def _fetch_openrouter_key(
    token: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return _get_bearer_json(OPENROUTER_KEY_URL, headers, "OpenRouter usage", client)


def _openrouter_number(value: Any, field: str, *, nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(f"OpenRouter {field} must be a number")
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise StoreError(f"OpenRouter {field} must be a finite number")
    return value


def _openrouter_snapshot(
    account: str, body: dict[str, Any], fetched_at: str
) -> dict[str, Any]:
    data = body.get("data")
    if not isinstance(data, dict):
        raise StoreError("OpenRouter usage data is not an object")
    limit_reset = data.get("limit_reset")
    if isinstance(limit_reset, (dict, list, bool)):
        raise StoreError("OpenRouter limit_reset has an invalid type")
    return {
        "vendor": OPENROUTER_VENDOR,
        "provider": OPENROUTER_VENDOR,
        "account": account,
        "usage_usd": _openrouter_number(data.get("usage"), "usage"),
        "usage_daily_usd": _openrouter_number(data.get("usage_daily"), "usage_daily"),
        "usage_weekly_usd": _openrouter_number(data.get("usage_weekly"), "usage_weekly"),
        "usage_monthly_usd": _openrouter_number(data.get("usage_monthly"), "usage_monthly"),
        "limit_usd": _openrouter_number(data.get("limit"), "limit", nullable=True),
        "limit_remaining_usd": _openrouter_number(
            data.get("limit_remaining"), "limit_remaining", nullable=True
        ),
        "limit_reset": limit_reset,
        "fetched_at": fetched_at,
    }


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


def scan_openrouter_usage(
    store: Store,
    *,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    now: Callable[[], str] | None = None,
) -> list[str]:
    """Record changed OpenRouter key-usage snapshots for configured accounts."""
    from .ai_accounts import load_ai_accounts

    accounts = load_ai_accounts(store.home)
    created: list[str] = []
    errors: list[str] = []
    for account_name in accounts.usage_accounts:
        account = accounts.accounts[account_name]
        if account.provider != OPENROUTER_VENDOR:
            continue
        key_path = Path(account.config_dir) / "openrouter-api-key"
        try:
            try:
                token = key_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise StoreError("openrouter key missing") from exc
            if token == "":
                raise StoreError("openrouter key missing")
            body = (fetch or _fetch_openrouter_key)(token)
            if not isinstance(body, dict):
                raise StoreError("OpenRouter usage response is not an object")
            snapshot = _openrouter_snapshot(
                account_name, body, (now or utcnow)()
            )

            def match(payload: dict[str, Any], name: str = account_name) -> bool:
                return (
                    payload.get("vendor") == OPENROUTER_VENDOR
                    and payload.get("account") == name
                )

            activity_id = _record_account_snapshot(
                store,
                snapshot,
                match,
                provider=OPENROUTER_VENDOR,
                lock_key=f"usage.snapshot:openrouter:{account_name}",
            )
            if activity_id is not None:
                created.append(activity_id)
        except StoreError as exc:
            errors.append(f"{account_name}: {exc}")
    if errors:
        raise OpenRouterUsageError(errors, created)
    return created


KeychainReader = Callable[[str, str], str | None]


def claude_keychain_service(config_dir: str) -> str:
    """Claude Code's Keychain service name for the login of ``config_dir``.

    The default profile (``~/.claude``) uses the unsuffixed service; any other
    directory adds the first 8 hex characters of the SHA-256 of the NFC form of
    the directory exactly as ``CLAUDE_CONFIG_DIR`` names it.
    """
    from .ai_accounts import _is_default_claude_dir

    if _is_default_claude_dir(config_dir):
        return CLAUDE_KEYCHAIN_SERVICE
    digest = hashlib.sha256(
        unicodedata.normalize("NFC", config_dir).encode("utf-8")
    ).hexdigest()[:8]
    return f"{CLAUDE_KEYCHAIN_SERVICE}-{digest}"


def claude_keychain_user(environ: dict[str, str] | None = None) -> str:
    """The Keychain account Claude Code uses: ``$USER`` when it is a plain name."""
    user = (os.environ if environ is None else environ).get("USER", "")
    return user if _KEYCHAIN_USER.fullmatch(user) else CLAUDE_KEYCHAIN_FALLBACK_USER


def read_claude_keychain(service: str, user: str) -> str | None:
    """Read one generic password; ``None`` when the item does not exist.

    The output is the credential itself and is never echoed in an error.
    """
    try:
        completed = subprocess.run(
            [SECURITY_BIN, "find-generic-password", "-s", service, "-a", user, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise StoreError("claude keychain read timed out") from None
    except (OSError, ValueError):
        raise StoreError("claude keychain read failed") from None
    if completed.returncode == _SECURITY_ITEM_NOT_FOUND:
        return None
    if completed.returncode != 0:
        raise StoreError(f"claude keychain read failed (exit {completed.returncode})")
    text = completed.stdout.strip()
    return text or None


def _keychain_supported() -> bool:
    return sys.platform == "darwin"


def _claude_tier(subscription: Any, rate_limit_tier: Any) -> str | None:
    """Plan name; a Max plan adds its multiplier (``default_claude_max_20x`` → ``max_20x``)."""
    plan = subscription.strip() if isinstance(subscription, str) else ""
    if plan == "":
        return None
    if plan.lower() == "max" and isinstance(rate_limit_tier, str):
        multiplier = _MAX_MULTIPLIER.search(rate_limit_tier.strip())
        if multiplier is not None:
            return f"max_{multiplier.group(1).lower()}"
    return plan


def _parse_claude_credentials(raw: str, source: str) -> tuple[str, str | None]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise StoreError(f"claude credentials in {source} are not valid JSON") from None
    if not isinstance(data, dict):
        raise StoreError(f"claude credentials in {source} are not an object")
    oauth = data.get("claudeAiOauth")
    if oauth is None:
        raise AuthStale(f"claude credentials in {source} hold no Claude login")
    if not isinstance(oauth, dict):
        raise StoreError(f"claude credentials in {source}: claudeAiOauth must be an object")
    token = oauth.get("accessToken")
    if token is None:
        raise AuthStale(f"claude credentials in {source} hold no access token")
    if not isinstance(token, str) or token == "":
        raise StoreError(
            f"claude credentials in {source}: accessToken must be a non-empty string"
        )
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise StoreError(
            f"claude credentials in {source}: expiresAt must be epoch milliseconds"
        )
    try:
        expires_seconds = float(expires_at) / 1000.0
    except OverflowError:
        expires_seconds = math.inf
    if not math.isfinite(expires_seconds):
        raise StoreError(
            f"claude credentials in {source}: expiresAt must be epoch milliseconds"
        )
    if datetime.now(timezone.utc).timestamp() >= expires_seconds:
        raise AuthStale("claude login token expired; start claude to renew it")
    return token, _claude_tier(oauth.get("subscriptionType"), oauth.get("rateLimitTier"))


def load_claude_credentials(
    config_dir: str,
    *,
    read_keychain: KeychainReader | None = None,
) -> tuple[str, str | None]:
    """Return ``(access_token, tier)`` for the Claude login of ``config_dir``.

    macOS: the Keychain item of that directory first, then
    ``<config_dir>/.credentials.json``; elsewhere only the file. A missing login
    or an expired token is ``AuthStale``; malformed data is ``StoreError``.
    Never refreshes and never reads the refresh token. Never log the token.
    """
    keychain_error: StoreError | None = None
    reader = read_keychain
    if reader is None and _keychain_supported():
        reader = read_claude_keychain
    if reader is not None:
        try:
            raw = reader(claude_keychain_service(config_dir), claude_keychain_user())
        except StoreError as exc:
            keychain_error = exc
        else:
            if raw is not None:
                return _parse_claude_credentials(raw, "the keychain")
    path = Path(config_dir) / ".credentials.json"
    if not path.is_file():
        if keychain_error is not None:
            raise keychain_error
        raise AuthStale(f"claude login not found for {config_dir}; run claude auth login")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise StoreError("claude credentials file is not readable") from None
    return _parse_claude_credentials(raw, ".credentials.json")


def _claude_account_file(config_dir: str) -> Path:
    from .ai_accounts import _is_default_claude_dir

    if _is_default_claude_dir(config_dir):
        return Path.home() / ".claude.json"
    return Path(config_dir) / ".claude.json"


def load_claude_account_email(config_dir: str) -> str:
    """The e-mail address of the Claude login, from ``oauthAccount.emailAddress``."""
    path = _claude_account_file(config_dir)
    if not path.is_file():
        raise AuthStale(f"claude account file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise StoreError("claude account file is not valid JSON") from None
    if not isinstance(data, dict):
        raise StoreError("claude account file is not an object")
    account = data.get("oauthAccount")
    if account is None:
        raise AuthStale("claude account file has no oauthAccount; run claude auth login")
    if not isinstance(account, dict):
        raise StoreError("claude account file field oauthAccount must be an object")
    email = account.get("emailAddress")
    if not isinstance(email, str) or email.strip() == "":
        raise StoreError(
            "claude account file field oauthAccount.emailAddress is missing or invalid"
        )
    return email


def _fetch_claude_usage(
    token: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "dfx-agent",
    }
    return _get_bearer_json(CLAUDE_USAGE_URL, headers, "Claude usage", client)


def _claude_reset(value: Any, field: str) -> str | None:
    """Reset time as UTC ISO-8601 rounded to the nearest minute, or ``None``.

    The endpoint reports the same reset with about a second of jitter between
    calls (for example ``09:59:59.66`` and ``10:00:00.41``). Rounding to the
    nearest full minute (30 seconds and more round up) keeps an unchanged
    window equal, so no new snapshot is written for jitter alone.
    """
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise StoreError(f"Claude usage {field} must be a timestamp string or null")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise StoreError(f"Claude usage {field} is not a valid timestamp") from None
    if when.tzinfo is None:
        raise StoreError(f"Claude usage {field} has no time zone")
    when = when.astimezone(timezone.utc)
    minute = when.replace(second=0, microsecond=0)
    if when - minute >= timedelta(seconds=30):
        minute += timedelta(minutes=1)
    return minute.isoformat()


def _claude_window(raw: Any, field: str) -> dict[str, Any] | None:
    """A reported window, or ``None`` when the response has no value for it."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StoreError(f"Claude usage {field} must be an object or null")
    used = raw.get("utilization")
    if used is None:
        return None
    return {
        "used_percent": _require_float(used, f"Claude usage {field}.utilization"),
        "resets_at": _claude_reset(raw.get("resets_at"), f"{field}.resets_at"),
    }


def claude_snapshot(
    account_email: str,
    tier: str | None,
    body: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    """Map the OAuth usage response to a ``usage.snapshot`` payload.

    ``windows.five_hour`` and ``windows.weekly`` are ``None`` when the response
    reports no value; a missing window is never shown as 0 %.
    ``windows.weekly_models`` lists the weekly limits scoped to one model.
    """
    if not isinstance(body, dict):
        raise StoreError("Claude usage response is not an object")
    weekly_all: dict[str, Any] | None = None
    weekly_models: list[dict[str, Any]] = []
    limits = body.get("limits")
    if limits is None:
        limits = []
    if not isinstance(limits, list):
        raise StoreError("Claude usage limits must be a list or null")
    for index, item in enumerate(limits):
        field = f"limits[{index}]"
        if not isinstance(item, dict):
            raise StoreError(f"Claude usage {field} must be an object")
        kind = item.get("kind")
        if kind not in ("weekly_all", "weekly_scoped") or item.get("percent") is None:
            continue
        window = {
            "used_percent": _require_float(
                item.get("percent"), f"Claude usage {field}.percent"
            ),
            "resets_at": _claude_reset(item.get("resets_at"), f"{field}.resets_at"),
        }
        if kind == "weekly_all":
            if weekly_all is None:
                weekly_all = window
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name: str | None = None
        if isinstance(model, dict):
            for key in ("display_name", "id"):
                value = model.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        weekly_models.append({"model": name, **window})
    weekly = _claude_window(body.get("seven_day"), "seven_day") or weekly_all
    return {
        "vendor": CLAUDE_VENDOR,
        "provider": CLAUDE_VENDOR,
        "account_email": _require_nonempty_str(account_email, "account_email"),
        "tier": tier,
        "windows": {
            "five_hour": _claude_window(body.get("five_hour"), "five_hour"),
            "weekly": weekly,
            "weekly_models": weekly_models,
        },
        "fetched_at": fetched_at,
    }


def scan_claude_usage(
    store: Store,
    *,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    read_keychain: KeychainReader | None = None,
    now: Callable[[], str] | None = None,
    skipped: list[str] | None = None,
    pace: dict[str, float] | None = None,
    clock: Callable[[], float] | None = None,
) -> list[str]:
    """Record changed Claude subscription usage for configured ``usage_accounts``.

    An account without a usable login (missing or expired) is skipped quietly;
    its reason is appended to ``skipped`` when the caller passes a list.

    ``pace`` maps an account name to the ``clock`` time before which it is not
    read again. An attempted account waits ``CLAUDE_USAGE_POLL_SECONDS``, or the
    server's ``Retry-After`` when that is longer. Without ``pace`` every account
    is read on every call.
    """
    tick = clock or time.monotonic
    from .ai_accounts import load_ai_accounts

    accounts = load_ai_accounts(store.home)
    created: list[str] = []
    errors: list[str] = []
    for account_name in accounts.usage_accounts:
        account = accounts.accounts[account_name]
        if account.provider != CLAUDE_VENDOR:
            continue
        if pace is not None:
            started = tick()
            if started < pace.get(account_name, -math.inf):
                continue
            pace[account_name] = started + CLAUDE_USAGE_POLL_SECONDS
        try:
            try:
                email = load_claude_account_email(account.config_dir)
                token, tier = load_claude_credentials(
                    account.config_dir, read_keychain=read_keychain
                )
            except AuthStale as exc:
                if skipped is not None:
                    skipped.append(f"{account_name}: {exc}")
                continue
            body = (fetch or _fetch_claude_usage)(token)
            snapshot = claude_snapshot(email, tier, body, (now or utcnow)())
            key = email.casefold()

            def match(payload: dict[str, Any], key: str = key) -> bool:
                value = payload.get("account_email")
                return (
                    payload.get("vendor") == CLAUDE_VENDOR
                    and isinstance(value, str)
                    and value.casefold() == key
                )

            activity_id = _record_account_snapshot(
                store,
                snapshot,
                match,
                provider=CLAUDE_VENDOR,
                lock_key=f"usage.snapshot:claude:{key}",
            )
            if activity_id is not None:
                created.append(activity_id)
        except UsageRateLimited as exc:
            message = f"{account_name}: {exc}"
            if exc.retry_after is not None:
                message += f"; retry after {math.ceil(exc.retry_after)} s"
                if pace is not None and exc.retry_after > CLAUDE_USAGE_POLL_SECONDS:
                    pace[account_name] = tick() + exc.retry_after
            errors.append(message)
        except StoreError as exc:
            errors.append(f"{account_name}: {exc}")
    if errors:
        raise ClaudeUsageError(errors, created)
    return created
