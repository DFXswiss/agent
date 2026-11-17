"""Two logical model slots for the lane chain: ``first`` and ``second``.

The device ``ai-accounts.json`` supplies each slot's default driver when a
task is created; the task pin governs and can be changed with
``agent task models``. Which drivers exist comes from ``drivers.json``: a
device selection must name configured drivers, while a stored task pin is
checked for its shape only, so a task recorded on another device stays
readable. Ledger stages, checklist keys and coordinator phases use
vendor-neutral slot names.

Earlier versions named the first and second review stage after the driver that
ran it, and wrote those names into stages, checklist keys and coordinator
phases. ``recorded_slots`` in ``drivers.json`` names those two drivers; the
process installs it once (``install_recorded_names``), and the ``canonical_*``
readers map every such stored name to its neutral name. The same entry supplies
the slots of a task created before tasks pinned model slots, only to read or
display its recorded rows. It is never a device assignment and must not be used
to start a lane or write a new record. Without the entry, those stored names
stay unknown and an unpinned task cannot be read.

Pure module: no Postgres, no network, no subprocess.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any, Mapping

from .drivers import (
    DRIVERS_DOC,
    DRIVERS_FILE,
    DRIVERS_HINT,
    RecordedNames,
    choice_text,
    valid_driver_id,
)

MODEL_SLOTS = ("first", "second")
# Canonical ledger identifiers per slot.
SLOT_STAGES = {"first": "review-1", "second": "review-2"}
STAGE_SLOTS = {stage: slot for slot, stage in SLOT_STAGES.items()}
SLOT_KEY_PREFIXES = {"first": "review_1_", "second": "review_2_"}
# Coordinator phase that runs each slot's PR review pair.
SLOT_PHASES = {slot: f"pr_gates_{slot}" for slot in MODEL_SLOTS}
STAGE_CHOICES = "|".join(SLOT_STAGES[slot] for slot in MODEL_SLOTS)
EXTERNAL_SECOND_TASK_CREATION_ERROR = (
    "models.second = null is allowed only for coordinator A38 task creation"
)
UNRECORDED_HINT = (
    f"written by an earlier version? configure recorded_slots in {DRIVERS_FILE}, "
    f"{DRIVERS_DOC}"
)
# The shapes of the stage, checklist key and phase names an earlier version
# wrote (see ``RecordedNames``); ``driver`` must also be a valid driver id.
_RECORDED_NAME_SHAPES = (
    re.compile(r"(?P<driver>.+)-pr"),
    re.compile(r"(?P<driver>.+)_pr_(?:quality|logic)"),
    re.compile(r"pr_gates_(?P<driver>.+)"),
)


@dataclass(frozen=True)
class _Recorded:
    """The installed ``recorded_slots`` and the stored names derived from it."""

    names: RecordedNames | None = None
    stages: Mapping[str, str] = field(default_factory=dict)
    keys: Mapping[str, str] = field(default_factory=dict)
    phases: Mapping[str, str] = field(default_factory=dict)


# One object, replaced as a whole, so a concurrent reader never sees a mix.
_recorded = _Recorded()


def install_recorded_names(names: RecordedNames | None) -> None:
    """Install this process's ``recorded_slots``; ``None`` removes every alias."""
    global _recorded
    if names is None:
        _recorded = _Recorded()
        return
    _recorded = _Recorded(
        names=names,
        stages={
            stored: SLOT_STAGES[slot] for stored, slot in names.stage_slots().items()
        },
        keys={
            stored: f"{SLOT_KEY_PREFIXES[slot]}{dimension}"
            for stored, (slot, dimension) in names.key_slots().items()
        },
        phases={
            stored: SLOT_PHASES[slot] for stored, slot in names.phase_slots().items()
        },
    )


def recorded_names() -> RecordedNames | None:
    """The installed ``recorded_slots``, or ``None`` when none is configured."""
    return _recorded.names


def unrecorded_name_hint(name: str) -> str:
    """Refusal suffix for an unknown name of the shape an earlier version wrote.

    The shapes are those of ``RecordedNames``: stage ``<driver>-pr``, checklist
    key ``<driver>_pr_quality`` or ``<driver>_pr_logic`` and phase
    ``pr_gates_<driver>``, where ``<driver>`` is a valid driver id. Any other
    name, or any name while ``recorded_slots`` is installed, yields "".
    """
    if _recorded.names is not None:
        return ""
    for shape in _RECORDED_NAME_SHAPES:
        match = shape.fullmatch(name)
        if match is not None and valid_driver_id(match["driver"]):
            return f" ({UNRECORDED_HINT})"
    return ""


def unrecorded_rows_hint(names: Iterable[object]) -> str:
    """``unrecorded_name_hint()`` of the first stored stage or key that has one."""
    for name in names:
        hint = unrecorded_name_hint(str(name))
        if hint:
            return hint
    return ""


def canonical_stage(stage: str) -> str:
    """Return the canonical stage name, preserving unknown values for validation."""
    return _recorded.stages.get(stage, stage)


def canonical_key(key: str) -> str:
    """Return the canonical checklist key, preserving unknown values for validation."""
    return _recorded.keys.get(key, key)


def canonical_phase(phase: str) -> str:
    """Return the canonical coordinator phase, preserving unknown values."""
    return _recorded.phases.get(phase, phase)


def canonical_checklist(values: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize stored checklist keys, preferring an explicit canonical row."""
    aliases = _recorded.keys
    result = {
        aliases[str(key)]: value for key, value in values.items() if str(key) in aliases
    }
    result.update(
        {str(key): value for key, value in values.items() if str(key) not in aliases}
    )
    return result


class ModelSlotsError(ValueError):
    """The model-slot or implementer selection is invalid.

    The ``models`` configuration does not describe valid first/second slots, or
    the optional ``implementer`` selection is invalid.
    """


class PinnedModelSlotsError(ModelSlotsError):
    """An existing task contains an invalid model or implementer pin."""


class UnrecordedSlotsError(PinnedModelSlotsError):
    """A task without model pins was read while no ``recorded_slots`` is installed."""


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
        slot = STAGE_SLOTS.get(canonical_stage(stage))
        if slot is None:
            raise ModelSlotsError(
                f"unknown gate stage {stage}{unrecorded_name_hint(stage)}"
            )
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


def _driver_field(
    value: Any, field: str, drivers: Collection[str] | None, *, nullable: bool = False
) -> None:
    """Check one selected driver: configured membership, or only its shape."""
    prefix = f"{field} must be null or" if nullable else f"{field} must be"
    if drivers is None:
        if not valid_driver_id(value):
            raise ModelSlotsError(f"{prefix} a driver id")
    elif not isinstance(value, str) or value not in drivers:
        raise ModelSlotsError(
            f"{prefix} one of {choice_text(sorted(drivers))} ({DRIVERS_HINT})"
        )


def parse_model_slots(
    raw: Any, lane_drivers: Collection[str] | None = None
) -> ModelSlots:
    """Validate the slots; ``second: null`` selects an external second review.

    ``lane_drivers`` are the configured drivers with a lane section; a device
    selection must name them. ``None`` checks a stored pin's shape only.
    """
    if raw is None:
        raise ModelSlotsError(MODELS_NOT_CONFIGURED)
    if not isinstance(raw, Mapping) or set(raw) != set(MODEL_SLOTS):
        raise ModelSlotsError("models must be an object with exactly first and second")
    first = raw.get("first")
    second = raw.get("second")
    _driver_field(first, "models.first", lane_drivers)
    if second is not None:
        _driver_field(second, "models.second", lane_drivers, nullable=True)
    if first == second:
        raise ModelSlotsError("models.first and models.second must be different providers")
    return ModelSlots(first, second)


def parse_implementer(
    raw: Any, models: ModelSlots, drivers: Collection[str] | None = None
) -> str:
    """Validate the optional implementer driver; absent means model slot ``first``.

    ``drivers`` are every configured driver, including record-only ones; a
    device selection must name one. ``None`` checks a stored pin's shape only.
    """
    if raw is None:
        return models.first
    _driver_field(raw, "implementer", drivers)
    if raw == models.second:
        raise ModelSlotsError("implementer must differ from models.second")
    return raw


def recorded_model_slots(names: RecordedNames) -> ModelSlots:
    """The slots an earlier version used before tasks pinned model slots."""
    return ModelSlots(names.first, names.second)


def models_from_snapshot(snapshot: Mapping[str, Any] | None) -> ModelSlots:
    """Read recorded rows, using ``recorded_slots`` for a task without a pin.

    A task pins the device's ``models`` when it is created, so a later change of
    the device configuration never re-labels the gates and agents a task already
    recorded. Tasks created before model slots existed carry no ``models`` and
    take the installed ``recorded_slots`` only when existing agent and gate rows
    are read or displayed. Do not use this resolver to start a lane or write a
    new row.
    """
    raw = (snapshot or {}).get("models")
    if raw is None:
        names = _recorded.names
        if names is None:
            task = (snapshot or {}).get("id")
            subject = f"task {task}" if task else "the task"
            raise UnrecordedSlotsError(
                f"{subject} predates model pins; configure recorded_slots in "
                f"{DRIVERS_FILE} to read it ({DRIVERS_DOC})"
            )
        return recorded_model_slots(names)
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
    """Read a recorded task's implementer pin, including ``recorded_slots``."""
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
