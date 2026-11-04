"""Two logical model slots for the lane chain: ``first`` and ``second``.

Which provider fills each slot is device-local configuration
(``ai-accounts.json`` → ``models``). The stored identifiers of the ledger
(gate stages ``grok-pr`` / ``codex-pr``, checklist keys ``grok_pr_*`` /
``codex_pr_*``) are historical slot names and stay stable; they no longer
imply a provider.

Pure module: no Postgres, no network, no subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SUPPORTED_PROVIDERS = frozenset({"grok", "codex", "claude", "openrouter"})
PROVIDER_CHOICES = "|".join(sorted(SUPPORTED_PROVIDERS))

MODEL_SLOTS = ("first", "second")
# Historical ledger identifiers per slot. Stored rows keep these names.
SLOT_STAGES = {"first": "grok-pr", "second": "codex-pr"}
STAGE_SLOTS = {stage: slot for slot, stage in SLOT_STAGES.items()}
SLOT_KEY_PREFIXES = {"first": "grok_pr_", "second": "codex_pr_"}
STAGE_CHOICES = "|".join(SLOT_STAGES[slot] for slot in MODEL_SLOTS)


class ModelSlotsError(ValueError):
    """The models configuration does not describe valid first/second slots."""


@dataclass(frozen=True)
class ModelSlots:
    first: str
    second: str | None

    def for_slot(self, slot: str) -> str | None:
        if slot == "first":
            return self.first
        if slot == "second":
            return self.second
        raise ModelSlotsError(f"unknown model slot {slot}")

    def for_stage(self, stage: str) -> str | None:
        slot = STAGE_SLOTS.get(stage)
        if slot is None:
            raise ModelSlotsError(f"unknown gate stage {stage}")
        return self.for_slot(slot)

    def slot_of(self, vendor: str) -> str | None:
        if vendor == self.first:
            return "first"
        if self.second is not None and vendor == self.second:
            return "second"
        return None

    def as_dict(self) -> dict[str, str | None]:
        return {"first": self.first, "second": self.second}


# Historical order used when a device or a task has no ``models``.
DEFAULT_MODEL_SLOTS = ModelSlots("grok", "codex")


def parse_model_slots(raw: Any) -> ModelSlots:
    """Validate the slots; ``second: null`` selects an external second review."""
    if raw is None:
        return DEFAULT_MODEL_SLOTS
    if not isinstance(raw, Mapping) or set(raw) != set(MODEL_SLOTS):
        raise ModelSlotsError("models must be an object with exactly first and second")
    first = raw.get("first")
    second = raw.get("second")
    if not isinstance(first, str) or first not in SUPPORTED_PROVIDERS:
        raise ModelSlotsError(f"models.first must be one of {PROVIDER_CHOICES}")
    if second is not None and (
        not isinstance(second, str) or second not in SUPPORTED_PROVIDERS
    ):
        raise ModelSlotsError(
            f"models.second must be null or one of {PROVIDER_CHOICES}"
        )
    if first == second:
        raise ModelSlotsError("models.first and models.second must be different providers")
    return ModelSlots(first, second)


def models_from_snapshot(snapshot: Mapping[str, Any] | None) -> ModelSlots:
    """Read ``models`` from a task row or snapshot; absent means the historical order.

    A task pins the device's ``models`` when it is created, so a later change of
    the device configuration never re-labels the gates and agents a task already
    recorded. Tasks created before model slots existed carry no ``models`` and
    keep the historical order ``grok``, then ``codex``.
    """
    raw = (snapshot or {}).get("models")
    if raw is None:
        return DEFAULT_MODEL_SLOTS
    return parse_model_slots(raw)
