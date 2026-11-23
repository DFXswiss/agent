"""A hub stand-in that decides duplicates the way the hub does (03-api.md section 8.3 H5/H6; KOR-28).

`SlotHub.install(kit)` replaces the scripted answers of the kit's `OpsHub`: a registration with `local_check:
"rejected"` is `rejected` with its code; one whose `{epoch, tag}` slot is held is `duplicate` of the holder; any other
is `accepted` and takes its slot. Releasing a slot (`release`) stands for the `failed`/`expired` transition of the
holder. Every decision is recorded in `audit` (the hub writes an audit row for every outcome).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kit import OKit, OpsHub

Json = dict[str, Any]


@dataclass
class SlotHub:
    slots: dict[tuple[int, str], str] = field(default_factory=dict)
    audit: list[tuple[str, str, str | None]] = field(default_factory=list)
    """`(operation_id, state, error_code or duplicate_of)` of every decision."""

    def install(self, kit: OKit) -> None:
        hub: OpsHub = kit.hub
        check = kit.env.hub.check

        def handle(body: Json) -> tuple[int, Json]:
            check("api/device-operations.request.json", body)
            hub.requests.append(body)
            answer = self.decide(body)
            check("api/device-operations.response.json", answer)
            return 200, answer

        hub.handle = handle  # type: ignore[method-assign]

    def decide(self, body: Json) -> Json:
        op_id = body["operation_id"]
        if body["local_check"] == "rejected":
            self.audit.append((op_id, "rejected", body["error_code"]))
            return {"state": "rejected", "error_code": body["error_code"]}
        slot = body["dedupe"]
        key = None if slot is None else (slot["epoch"], slot["tag"])
        holder = None if key is None else self.slots.get(key)
        if holder is not None:
            self.audit.append((op_id, "duplicate", holder))
            return {"state": "duplicate", "duplicate_of": holder, "original": {"state": "accepted"}}
        if key is not None:
            self.slots[key] = op_id
        self.audit.append((op_id, "accepted", None))
        return {"state": "accepted"}

    def release(self, op_id: str) -> None:
        for key, holder in list(self.slots.items()):
            if holder == op_id:
                del self.slots[key]
