"""Credential provisioning of the guest model gateway (docs/platform/spec/05-runner.md sections 10.1-10.3, 2.6;
03-api.md section 7.1 `PUT /v1/sessions/{id}/credential`; ADR-010).

The provider credential never enters the session sandbox: the runner hands it to the gateway of the session's VM,
which keeps it in memory only. This module builds that request for every access kind:

- `platform` (DFX OpenRouter key, platform devices only): upstream `MODEL_UPSTREAM_OPENROUTER`, header
  `Authorization: Bearer <key>`, the catalog's platform models, the provider allow-list and the daily budget;
- `api_key` (the owner's `anthropic_api_key`): upstream `MODEL_UPSTREAM_ANTHROPIC`, header `x-api-key: <key>`, the
  catalog's API-key models;
- `subscription` (the owner's `claude_subscription`): upstream `MODEL_UPSTREAM_ANTHROPIC`, header `Authorization:
  Bearer <token>` (the gateway adds `oauth-2025-04-20` to `anthropic-beta`), the catalog's subscription models.

- `read_platform_key(path)`: the key in `AGENT_RUNNER_OPENROUTER_KEY_FILE`. The file must be a regular file of this
  user, not a symbolic link, readable by nobody else (0600 or stricter) and contain one printable token; otherwise
  `None` (the session then fails with `model_access_unavailable`). The value is read for each provisioning and kept
  only in memory; it is never logged, never written to the store and never part of an error.
- `platform_credential(...)`: `{upstream_base_url, auth_header:"authorization", auth_value:"Bearer <key>",
  extra_headers:[], provider_allowlist, access:"platform", models_allowed, budget?}` with the catalog's platform
  allow-list and context windows and the organization's daily platform budget from `GET /v1/device/state`.
- `owner_credential(kind, secret, upstream, catalog)`: the body of `api_key` or `subscription` from the owner's
  decrypted credential (`anthropic_api_key.key`, `claude_subscription.token`).
- `withdrawn_credential(kind, upstream)`: the body that takes the access away from a running session (05 section 2.6:
  the credential was deleted): no secret and an empty model allow-list, so every further request of the session is
  refused by the gateway (`model_not_allowed`) until the runner has stopped it.
- `budget_exhausted(budget)`: the organization has spent its daily platform budget (new platform sessions and
  subagents are refused with `budget_exceeded`).
"""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runner.model.catalog import Catalog

MAX_KEY_BYTES = 4096
WITHDRAWN = "withdrawn"
# the credential kind of the owner behind an access kind
CREDENTIAL_KINDS = {"api_key": "anthropic_api_key", "subscription": "claude_subscription"}
SECRET_FIELDS = {"anthropic_api_key": "key", "claude_subscription": "token"}
_TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")


def read_platform_key(path: Path | None) -> str | None:
    """The platform key, or None when it is not configured or not usable (see the module docstring)."""
    if path is None:
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            return None
        data = os.read(descriptor, MAX_KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return text if len(data) <= MAX_KEY_BYTES and _TOKEN_RE.fullmatch(text) else None


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def platform_budget(desired: Mapping[str, Any] | None, org_id: str) -> dict[str, float] | None:
    """`{usd_per_day, spent_usd}` of the org from `platform_budget` of the desired state.

    `null` (or no desired state) is unlimited (None). A budget that is present but malformed fails closed: a missing
    or invalid limit is 0, an invalid spent amount counts as the whole limit, so the gateway refuses with
    `budget_exceeded` instead of spending without limit. An org without an entry has spent nothing today."""
    budget = None if desired is None else desired.get("platform_budget")
    if budget is None:
        return None
    if not isinstance(budget, dict):
        return {"usd_per_day": 0.0, "spent_usd": 0.0}
    limit = _amount(budget.get("usd_per_org_day"))
    if limit is None:
        return {"usd_per_day": 0.0, "spent_usd": 0.0}
    spent_by_org = budget.get("spent_today_by_org")
    if not isinstance(spent_by_org, dict):
        return {"usd_per_day": limit, "spent_usd": limit}
    spent = _amount(spent_by_org.get(org_id, 0))
    return {"usd_per_day": limit, "spent_usd": limit if spent is None else spent}


def platform_credential(
    *, key: str, upstream: str, catalog: Catalog, budget: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The `PUT /v1/sessions/{id}/credential` body of the platform access kind."""
    policy = catalog.policy("platform")
    body: dict[str, Any] = {
        "upstream_base_url": upstream.rstrip("/"),
        "auth_header": "authorization",
        "auth_value": f"Bearer {key}",
        "extra_headers": [],
        "provider_allowlist": list(policy.providers_allowed),
        "access": "platform",
        "models_allowed": _models(catalog, "platform"),
    }
    if budget is not None:
        body["budget"] = dict(budget)
    return body


def _models(catalog: Catalog, kind: str) -> list[dict[str, Any]]:
    return [{"model_id": model, "context_window": window} for model, window in catalog.allowed_models(kind)]


def owner_secret(credential_kind: str, body: Mapping[str, Any]) -> str | None:
    """The secret of a decrypted owner credential, or None when the body does not carry a usable one."""
    value = body.get(SECRET_FIELDS[credential_kind])
    return value if isinstance(value, str) and _TOKEN_RE.fullmatch(value) else None


def owner_credential(*, kind: str, secret: str, upstream: str, catalog: Catalog) -> dict[str, Any]:
    """The `PUT /v1/sessions/{id}/credential` body of the `api_key` or `subscription` access kind."""
    api_key = kind == "api_key"
    return {
        "upstream_base_url": upstream.rstrip("/"),
        "auth_header": "x-api-key" if api_key else "authorization",
        "auth_value": secret if api_key else f"Bearer {secret}",
        "extra_headers": [],
        "access": kind,
        "models_allowed": _models(catalog, kind),
    }


def withdrawn_credential(*, kind: str, upstream: str) -> dict[str, Any]:
    """A provisioning without secret and without allowed models (the access was taken away)."""
    api_key = kind == "api_key"
    return {
        "upstream_base_url": upstream.rstrip("/"),
        "auth_header": "x-api-key" if api_key else "authorization",
        "auth_value": WITHDRAWN if api_key else f"Bearer {WITHDRAWN}",
        "extra_headers": [],
        "access": kind,
        "models_allowed": [],
    }


def budget_exhausted(budget: Mapping[str, Any] | None) -> bool:
    """True when a daily platform budget exists and is spent (the gateway's own rule, 05 section 10.2)."""
    return budget is not None and budget["spent_usd"] >= budget["usd_per_day"]
