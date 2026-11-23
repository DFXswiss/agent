"""The base handlers of the outbox item kinds (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030).

`usage` and `quota` have their base handlers in the usage area (its `BASE_KINDS`, ADR-037). Other
areas extend an item kind by registering a handler for it in their own `outbox.py`; it runs after the base
handler of this area (`agent_hub.outbox.chains`).
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_hub.outbox import (
    handlers_audit,
    handlers_event,
    handlers_metrics,
    handlers_operation,
    handlers_session,
    handlers_vm,
    handlers_wrap,
)
from agent_hub.outbox.base import Handler

HANDLERS: Mapping[str, Handler] = {
    "event": handlers_event.handle,
    "session_transition": handlers_session.handle,
    "vm_state": handlers_vm.handle,
    "operation_transition": handlers_operation.handle,
    "metrics": handlers_metrics.handle,
    "audit": handlers_audit.handle,
    "wrap": handlers_wrap.handle,
}
