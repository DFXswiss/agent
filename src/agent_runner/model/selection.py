"""Model access selection of a session (docs/platform/spec/05-runner.md sections 10.1, 10.3 and 10.5; ADR-010).

`select(request, catalog=..., strategies=...)` decides the access kind and the model of one session from the owner's
preference (`model_access_pref`, `model_id_pref` of `GET /v1/device/state`) and what this device can offer:

- `auto` tries the kinds in the order `subscription` -> `api_key` -> `platform` and takes the first available one;
- an explicit preference that is not available fails with `model_access_unavailable` (no silent fallback).

Each kind is a `Strategy` that answers `None` (not available on this device) or a `Choice`:

- `subscription` / `api_key`: the owner's `claude_subscription` / `anthropic_api_key` credential opens on this
  device (`Request.credentials` names the access kinds whose credential does; `model.access` finds out);
- `platform`: a usable `OPENROUTER_KEY_FILE` on a platform device, or the limited DFX key of an org device (ADR-081
  section 7; `SessionEnv.platform_key` answers None without one).

The access kinds are a closed list (`contracts/common.json#/$defs/model_access`) and all three are built in; the
extension point `<area>/model_access.py:STRATEGIES` that RUN-04 left for them is therefore removed
(decisions/RUN-12.md).

Model choice: the owner's `model_id_pref` when the catalog allows it for the access kind, else the kind's catalog
default (`platform`: `deepseek/deepseek-v4.1-flash-1017`; API key and subscription: Claude Code's default, no
`--model`). The model environment of the platform kind (section 10.3) sets every Claude Code model slot to the chosen
model and the context and output limits from the catalog when they are below Claude Code's own assumptions; the other
kinds need no model environment (the guest passes a chosen model as `--model`).

The catalog lives in `model.catalog` (re-exported here for the callers of RUN-04 and RUN-07).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.model.catalog import (
    ACCESS_KINDS,
    AccessPolicy,
    Catalog,
    CatalogInvalid,
    ModelInfo,
    load_catalog,
    parse_catalog,
)

__all__ = ["AccessPolicy", "Catalog", "CatalogInvalid", "ModelInfo", "load_catalog", "parse_catalog"]

PREFERENCES = ("auto", *ACCESS_KINDS)
AUTO_ORDER = ACCESS_KINDS
UNAVAILABLE = "model_access_unavailable"
# Claude Code assumes these limits for unknown models (05-runner.md section 10.3).
CONTEXT_ASSUMED = 1_000_000
OUTPUT_ASSUMED = 32_000
MODEL_SLOTS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


@dataclass(frozen=True)
class Request:
    """What the selection needs to know about the session and the device."""

    preference: str
    model_pref: str | None
    device_kind: str
    owner_id: str
    platform_key_available: bool = False
    credentials: frozenset[str] = frozenset()
    """Access kinds (`subscription`, `api_key`) whose owner credential opens on this device."""


@dataclass(frozen=True)
class Choice:
    """The access kind and model of a session; `env` is the model environment of section 10.3 for the guest."""

    access: str
    model_id: str | None
    env: Mapping[str, str] = field(default_factory=dict)


Strategy = Callable[[Request, Catalog], Choice | None]


def choose_model(catalog: Catalog, kind: str, preferred: str | None) -> str | None:
    """The owner's model when allowed for `kind`, else the kind's catalog default."""
    policy = catalog.policy(kind)
    return preferred if preferred is not None and preferred in policy.allowed else policy.default


def platform_env(catalog: Catalog, model_id: str) -> dict[str, str]:
    """Model environment of the platform kind (05-runner.md section 10.3)."""
    env = dict.fromkeys(MODEL_SLOTS, model_id)
    info = catalog.models[model_id]
    if info.context_window < CONTEXT_ASSUMED:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(info.context_window)
    if info.max_output < OUTPUT_ASSUMED:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(info.max_output)
    return env


def platform_strategy(request: Request, catalog: Catalog) -> Choice | None:
    """DFX model access through OpenRouter: on any device that has a key for it (the platform key of a platform device,
    the limited DFX key of an org device, ADR-081 section 7)."""
    if not request.platform_key_available:
        return None
    model = choose_model(catalog, "platform", request.model_pref)
    assert model is not None  # parse_catalog requires a platform default
    return Choice(access="platform", model_id=model, env=platform_env(catalog, model))


def credential_strategy(kind: str) -> Strategy:
    """The owner's own credential of `kind` (`subscription`, `api_key`), on any device that can open it."""

    def strategy(request: Request, catalog: Catalog) -> Choice | None:
        if kind not in request.credentials:
            return None
        return Choice(access=kind, model_id=choose_model(catalog, kind, request.model_pref))

    return strategy


subscription_strategy = credential_strategy("subscription")
api_key_strategy = credential_strategy("api_key")

BUILTIN: dict[str, Strategy] = {
    "subscription": subscription_strategy,
    "api_key": api_key_strategy,
    "platform": platform_strategy,
}


def order(preference: str) -> tuple[str, ...]:
    """The access kinds a preference may use, in the order they are tried (`model_access_unavailable` for an unknown
    preference)."""
    if preference not in PREFERENCES:
        raise RunnerError(UNAVAILABLE, "preference")
    return AUTO_ORDER if preference == "auto" else (preference,)


def strategies(_package: str = registry.ROOT_PACKAGE) -> dict[str, Strategy]:
    """The strategies of the session area's environment: the built-in kinds (the closed list of access kinds is
    complete since RUN-12, so the discovered extension point of RUN-04 is gone)."""
    return dict(BUILTIN)


def select(request: Request, *, catalog: Catalog, strategies: Mapping[str, Strategy] = BUILTIN) -> Choice:
    """The access of the session, or `RunnerError("model_access_unavailable")` (see the module docstring)."""
    kinds: Sequence[str] = order(request.preference)
    for kind in kinds:
        strategy = strategies.get(kind)
        choice = strategy(request, catalog) if strategy is not None else None
        if choice is not None:
            return choice
    raise RunnerError(UNAVAILABLE, request.preference)
