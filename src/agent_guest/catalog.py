"""The model policy of one session: the slice of the runner's model catalog the gateway enforces (05-runner.md §10.2).

The runner owns ``model-catalog.json`` (§10.5); the guest never reads it. With every credential provisioning
(``PUT /v1/sessions/{id}/credential``, ``contracts/changes/GST-04.md``) the runner sends the part that applies to the
session's access kind, and this module turns it into a ``ModelPolicy``:

* ``access`` - ``subscription``, ``api_key`` or ``platform`` (selects the credential injection)
* ``context_windows`` - allowed model ID -> catalog ``context_window`` (a model outside is ``model_not_allowed``)
* ``providers_allowed`` - OpenRouter provider allow-list; empty means any provider (never blocks, only flags)
* ``budget`` - daily platform budget ``{usd_per_day, spent_usd, journal_through?}`` or ``None`` (unlimited);
  ``journal_through`` is the last journal record whose cost the runner took into ``spent_usd`` (KOR-10)

Pure module: no clock, no network, no files. The body is validated against the contract before it gets here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ACCESS_KINDS = ("subscription", "api_key", "platform")


@dataclass(frozen=True)
class Budget:
    usd_per_day: float
    spent_usd: float
    journal_through: int | None = None


@dataclass(frozen=True)
class ModelPolicy:
    access: str
    context_windows: Mapping[str, int]
    providers_allowed: frozenset[str]
    budget: Budget | None

    def context_window(self, model: str) -> int | None:
        """The catalog context window of ``model``; ``None`` when the model is not allowed."""
        return self.context_windows.get(model)

    def provider_outside(self, provider: str | None) -> bool:
        """True when ``provider`` is known, an allow-list exists and the provider is not on it."""
        return provider is not None and bool(self.providers_allowed) and provider not in self.providers_allowed

    def within_budget(self, observed_usd: float) -> bool:
        """False once the runner's known spending plus the cost observed since the provisioning reaches the limit."""
        return self.budget is None or self.budget.spent_usd + observed_usd < self.budget.usd_per_day


def policy_from(body: Mapping[str, Any]) -> ModelPolicy:
    """The policy of a provisioning body that already matched ``guest/session-credential.request.json``."""
    budget = body.get("budget")
    return ModelPolicy(
        access=str(body["access"]),
        context_windows={str(item["model_id"]): int(item["context_window"]) for item in body["models_allowed"]},
        providers_allowed=frozenset(str(name) for name in body.get("provider_allowlist", ())),
        budget=None
        if budget is None
        else Budget(float(budget["usd_per_day"]), float(budget["spent_usd"]), budget.get("journal_through")),
    )
