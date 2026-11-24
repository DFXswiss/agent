"""After-commit effects of task and session changes: live frames, device hints and internal signals.

A change inside a transaction records what it must announce in an `Effects`; the caller runs `flush()` once the
transaction has committed (never on a rollback). Frames and hints go out through `agent_hub.stream.publish` only and
signals through `agent_hub.signals` (ADR-027 section 4); a failing publisher is logged and never turns a committed
change into an error.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agent_hub import signals
from agent_hub.stream.publish import Audience, publish

LOG = logging.getLogger("agent_hub.sessions")


@dataclass
class Effects:
    """Frames, hints and signals to send after the commit, in the order they were recorded."""

    frames: list[tuple[Audience, str, dict[str, Any], UUID | None]] = field(default_factory=list)
    signals: list[tuple[str, dict[str, UUID | int | str]]] = field(default_factory=list)

    def org_frame(
        self, org_id: UUID | str, frame: str, data: Mapping[str, Any], *, wg_id: UUID | str | None = None
    ) -> None:
        """A frame for the browsers of the org (`task`, `session`); `wg_id` restricts it to the workgroup's readers
        (`attention`, whose row names no workgroup)."""
        wg = None if wg_id is None else UUID(str(wg_id))
        self.frames.append((Audience("org", UUID(str(org_id))), frame, dict(data), wg))

    def hint(self, device_id: UUID | str, hint: str, data: Mapping[str, Any]) -> None:
        """A content-free hint for one device (`session.start`, `session.control`, `prompt`)."""
        self.frames.append((Audience("device", UUID(str(device_id))), hint, dict(data), None))

    def signal(self, name: str, **ids: UUID | int | str) -> None:
        """An internal signal (`task.state`)."""
        self.signals.append((name, ids))

    def extend(self, other: Effects) -> None:
        """Append the effects of `other`."""
        self.frames.extend(other.frames)
        self.signals.extend(other.signals)

    def flush(self) -> None:
        """Send everything (after the commit) and forget it."""
        frames, self.frames = self.frames, []
        pending, self.signals = self.signals, []
        for audience, frame, data, wg_id in frames:
            try:
                publish(audience, frame, data, wg_id=wg_id)
            except Exception:
                LOG.error("a %s frame could not be published", frame, exc_info=True)
        for name, ids in pending:
            signals.emit(name, **ids)
