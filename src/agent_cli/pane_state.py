"""Is the driver's terminal user interface in this tmux pane currently working?

The interactive driver paints that state itself; its configured pane markers
(``interactive.pane`` in ``drivers.json``) say how. Pane flicker, process lists,
and log mtimes are not this question.
"""

from __future__ import annotations

from .drivers import PaneMarkers
from .text_sanitize import sanitize_terminal_text


def strip_ansi(text: str) -> str:
    return sanitize_terminal_text(text)


def pane_is_working(pane: str, markers: PaneMarkers) -> bool:
    """True only when the pane shows an in-flight turn (thinking, tool, stop)."""
    plain = strip_ansi(pane)
    lowered = plain.lower()
    # Queued follow-up: the current turn has not returned to the idle prompt.
    for group in markers.queued:
        if all(part.lower() in lowered for part in group):
            return True
    return any(pattern.search(plain) is not None for pattern in markers.working)


def pane_permission_prompt(pane: str, markers: PaneMarkers) -> bool:
    """True when the driver is blocked on a tool-approval modal.

    A permission marker alone may be quoted text; one confirmation substring
    must be present as well.
    """
    plain = strip_ansi(pane)
    if not any(pattern.search(plain) is not None for pattern in markers.permission):
        return False
    lowered = plain.lower()
    return any(part.lower() in lowered for part in markers.permission_confirm)


def pane_is_idle(pane: str, markers: PaneMarkers) -> bool:
    """True when the composer is waiting for a prompt."""
    if pane_is_working(pane, markers) or pane_permission_prompt(pane, markers):
        return False
    plain = strip_ansi(pane)
    return any(pattern.search(plain) is not None for pattern in markers.idle)
