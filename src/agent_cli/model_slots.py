"""Two logical model slots for the lane chain: ``first`` and ``second``.

Which provider fills each slot is device-local configuration
(``ai-accounts.json`` → ``models``). Ledger stages and checklist keys use
vendor-neutral slot names. Historical vendor-bound names remain readable and
are accepted as deprecated CLI aliases.

``LEGACY_RECORDED_SLOTS`` exists only to read or display agent and gate rows
recorded before tasks pinned model slots. It is never a device assignment and
must not be used to start a lane or write a new record.

Pure module: no Postgres, no network, no subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SUPPORTED_PROVIDERS = frozenset({"grok", "codex", "claude", "openrouter"})
PROVIDER_CHOICES = "|".join(sorted(SUPPORTED_PROVIDERS))
IMPLEMENTER_PROVIDERS = SUPPORTED_PROVIDERS | frozenset({"deepseek"})
IMPLEMENTER_PROVIDER_CHOICES = "|".join(sorted(IMPLEMENTER_PROVIDERS))

MODEL_SLOTS = ("first", "second")
# Canonical ledger identifiers per slot.
SLOT_STAGES = {"first": "review-1", "second": "review-2"}
STAGE_SLOTS = {stage: slot for slot, stage in SLOT_STAGES.items()}
SLOT_KEY_PREFIXES = {"first": "review_1_", "second": "review_2_"}
STAGE_CHOICES = "|".join(SLOT_STAGES[slot] for slot in MODEL_SLOTS)
EXTERNAL_SECOND_TASK_CREATION_ERROR = (
    "models.second = null is allowed only for coordinator A38 task creation"
)

LEGACY_STAGE_ALIASES = {"grok-pr": "review-1", "codex-pr": "review-2"}
LEGACY_KEY_ALIASES = {
    "grok_pr_quality": "review_1_quality",
    "grok_pr_logic": "review_1_logic",
    "codex_pr_quality": "review_2_quality",
    "codex_pr_logic": "review_2_logic",
}


def canonical_stage(stage: str) -> str:
    """Return the canonical stage name, preserving unknown values for validation."""
    return LEGACY_STAGE_ALIASES.get(stage, stage)


def canonical_key(key: str) -> str:
    """Return the canonical checklist key, preserving unknown values for validation."""
    return LEGACY_KEY_ALIASES.get(key, key)


def canonical_checklist(values: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize stored checklist keys, preferring an explicit canonical row."""
    result = {
        canonical_key(str(key)): value
        for key, value in values.items()
        if str(key) in LEGACY_KEY_ALIASES
    }
    result.update(
        {
            canonical_key(str(key)): value
            for key, value in values.items()
            if str(key) not in LEGACY_KEY_ALIASES
        }
    )
    return result


class ModelSlotsError(ValueError):
    """The model-slot or implementer selection is invalid."""


class PinnedModelSlotsError(ModelSlotsError):
    """An existing task contains an invalid model or implementer pin."""


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

    def for_stage(self, stage: str) -> str:
        slot = STAGE_SLOTS.get(canonical_stage(stage))
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


MODELS_NOT_CONFIGURED = (
    "models is not configured: the device's ai-accounts.json must name "
    "models.first and models.second (set by the local harness)"
)
TASK_MODELS_NOT_CONFIGURED = (
    "task models is not configured: the task must be recreated with the device's "
    "configured models.first and models.second"
)

# This is not a device default or a selection for new work.
LEGACY_RECORDED_SLOTS = ModelSlots("grok", "codex")


def parse_model_slots(raw: Any) -> ModelSlots:
    """Validate the slots; ``second: null`` selects an external second review."""
    if raw is None:
        raise ModelSlotsError(MODELS_NOT_CONFIGURED)
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


def parse_implementer(raw: Any, models: ModelSlots) -> str:
    """Validate the optional implementer provider; absent means model slot ``first``."""
    if raw is None:
        return models.first
    if not isinstance(raw, str) or raw not in IMPLEMENTER_PROVIDERS:
        raise ModelSlotsError(
            f"implementer must be one of {IMPLEMENTER_PROVIDER_CHOICES}"
        )
    if raw == models.second:
        raise ModelSlotsError("implementer must differ from models.second")
    return raw


def models_from_snapshot(snapshot: Mapping[str, Any] | None) -> ModelSlots:
    """Read recorded rows, using the historical mapping for a task without a pin.

    A task pins the device's ``models`` when it is created, so a later change of
    the device configuration never re-labels the gates and agents a task already
    recorded. Tasks created before model slots existed carry no ``models`` and
    keep the historical mapping only when existing agent and gate rows are read
    or displayed. Do not use this resolver to start a lane or write a new row.
    """
    raw = (snapshot or {}).get("models")
    if raw is None:
        return LEGACY_RECORDED_SLOTS
    try:
        return parse_model_slots(raw)
    except ModelSlotsError as exc:
        raise PinnedModelSlotsError(str(exc)) from exc


def models_for_new_record(snapshot: Mapping[str, Any] | None) -> ModelSlots:
    """Resolve a task pin for a new lane, agent row, or gate record; never fall back."""
    raw = (snapshot or {}).get("models")
    if raw is None:
        raise ModelSlotsError(TASK_MODELS_NOT_CONFIGURED)
    try:
        return parse_model_slots(raw)
    except ModelSlotsError as exc:
        raise PinnedModelSlotsError(str(exc)) from exc


def implementer_from_snapshot(snapshot: Mapping[str, Any] | None) -> str:
    """Read a recorded task's implementer pin, including the historical mapping."""
    snap = snapshot or {}
    try:
        return parse_implementer(snap.get("implementer"), models_from_snapshot(snap))
    except PinnedModelSlotsError:
        raise
    except ModelSlotsError as exc:
        raise PinnedModelSlotsError(str(exc)) from exc


def implementer_for_new_record(snapshot: Mapping[str, Any] | None) -> str:
    """Resolve the implementer for a new lane or agent row; require pinned models."""
    snap = snapshot or {}
    try:
        return parse_implementer(snap.get("implementer"), models_for_new_record(snap))
    except PinnedModelSlotsError:
        raise
    except ModelSlotsError as exc:
        # A missing task model selection is a task-state error, not a malformed pin.
        if str(exc) == TASK_MODELS_NOT_CONFIGURED:
            raise
        raise PinnedModelSlotsError(str(exc)) from exc


def model_pins(models: ModelSlots, implementer: str) -> dict[str, Any]:
    """Task fields for a device selection, omitting a redundant implementer pin."""
    pins: dict[str, Any] = {"models": models.as_dict()}
    if implementer != models.first:
        pins["implementer"] = implementer
    return pins
