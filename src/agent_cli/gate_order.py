"""Durable ordering for every reader of review-gate history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from .model_slots import canonical_stage
from .timestamps import parse_utc_timestamp


class GateStore(Protocol):
    def rows(self, table: str) -> list[dict[str, Any]]: ...

    def row_insert_sequences(self, table: str) -> dict[str, tuple[str, int]]: ...


GateKey = tuple[str, str]


def _unavailable_gate(gate: dict[str, Any], key: GateKey) -> dict[str, Any]:
    unavailable = dict(gate)
    unavailable["stage"] = key[0]
    unavailable["dimension"] = key[1]
    unavailable["verdict"] = "unavailable"
    unavailable["head_sha"] = ""
    return unavailable


def ordered_gate_rows(
    rows: Sequence[dict[str, Any]],
    *,
    insert_sequences: Mapping[str, tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Order gate histories and synthesize unavailable for ambiguous conflicts."""
    sequences = insert_sequences or {}
    grouped: dict[GateKey, list[dict[str, Any]]] = {}
    for gate in rows:
        key = (
            canonical_stage(str(gate.get("stage"))),
            str(gate.get("dimension") or ""),
        )
        grouped.setdefault(key, []).append(gate)

    ordered: list[dict[str, Any]] = []
    for key in sorted(grouped):
        gates = grouped[key]
        durable: list[tuple[int, dict[str, Any]]] = []
        origins: set[str] = set()
        for gate in gates:
            row_id = str(gate.get("id") or "")
            sequence = sequences.get(row_id)
            if sequence is None or sequence[0] != str(
                gate.get("_origin_device_id") or ""
            ):
                durable = []
                break
            origins.add(sequence[0])
            durable.append((sequence[1], gate))
        if (
            len(durable) == len(gates)
            and len(origins) == 1
            and len({sequence for sequence, _gate in durable}) == len(gates)
        ):
            ordered.extend(
                gate for _sequence, gate in sorted(durable, key=lambda item: item[0])
            )
            continue

        by_time: dict[datetime, list[dict[str, Any]]] = {}
        unordered: list[dict[str, Any]] = []
        for gate in gates:
            recorded_at = parse_utc_timestamp(gate.get("recorded_at"))
            if recorded_at is None:
                unordered.append(gate)
                continue
            by_time.setdefault(recorded_at, []).append(gate)
        for recorded_at in sorted(by_time):
            tied = sorted(
                by_time[recorded_at], key=lambda gate: str(gate.get("id") or "")
            )
            ordered.extend(tied)
            outcomes = {
                (
                    str(gate.get("vendor") or ""),
                    str(gate.get("verdict") or ""),
                    str(gate.get("head_sha") or ""),
                )
                for gate in tied
            }
            if len(outcomes) > 1:
                ordered.append(_unavailable_gate(tied[-1], key))
        if unordered:
            unordered.sort(key=lambda gate: str(gate.get("id") or ""))
            ordered.extend(unordered)
            if len(gates) > 1:
                ordered.append(_unavailable_gate(unordered[-1], key))
    return ordered


def latest_ordered_gate_rows(
    rows: Sequence[dict[str, Any]],
) -> dict[GateKey, dict[str, Any]]:
    """Collapse rows already ordered and fail-closed by ``ordered_gate_rows``."""
    latest: dict[GateKey, dict[str, Any]] = {}
    for gate in rows:
        key = (
            canonical_stage(str(gate.get("stage"))),
            str(gate.get("dimension") or ""),
        )
        latest[key] = gate
    return latest


def ordered_snapshot_gates(store: GateStore, task_id: str) -> list[dict[str, Any]]:
    rows = [
        gate
        for gate in store.rows("review_gate")
        if gate.get("task_id") == task_id
    ]
    return ordered_gate_rows(
        rows,
        insert_sequences=store.row_insert_sequences("review_gate"),
    )


def latest_gates(store: GateStore, task_id: str) -> dict[GateKey, dict[str, Any]]:
    return latest_ordered_gate_rows(ordered_snapshot_gates(store, task_id))
