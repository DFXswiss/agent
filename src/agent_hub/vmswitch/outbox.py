"""Outbox handler extension of the switch area (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-093 section 3): after
the base handler of `vm_state` stored the VM row (and refused a VM of another device), the switch status and the
successor of a switched VM are stored (`service.apply_report`)."""

from __future__ import annotations

from collections.abc import Mapping

from agent_hub.outbox.base import Handler, ItemContext
from agent_hub.outbox.bodies import VmStateBody
from agent_hub.vmswitch.service import apply_report


def vm_state(item: ItemContext, body: VmStateBody) -> None:
    apply_report(item.connection, body)


HANDLERS: Mapping[str, Handler] = {"vm_state": vm_state}
