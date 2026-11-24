"""Ticker of the hook summaries (ADR-086 section 3, ``hook_summary.py``).

``STARTUP`` starts the thread ``agent-guest-hook-summary``: every ``TICK_SECONDS`` it writes the summaries of the
sessions whose first counted hook record is ``HookLimits.summary_seconds`` old (``Journal.flush_summaries``), so the
counts of a session that went quiet reach the runner within that time plus a tick. The thread ends when the journal is
closed; a failing round is logged and the next one runs. The module has no routes (``ROUTES`` empty).
"""

from __future__ import annotations

import sys
import threading
import traceback

from agent_guest.control import ControlContext
from agent_guest.journal import Journal, JournalError
from agent_guest.routing import Route

TICK_SECONDS = 1.0


def tick(journal: Journal, seconds: float = TICK_SECONDS) -> None:
    while not journal.wait_closed(seconds):
        try:
            journal.flush_summaries()
        except JournalError as error:
            if error.code == "unavailable":
                return
            sys.stderr.write(f"agent-guest-control: hook summaries failed: {error.code}\n")
        except Exception:
            sys.stderr.write("agent-guest-control: hook summaries failed\n")
            traceback.print_exc(file=sys.stderr)


def startup(context: ControlContext) -> None:
    thread = threading.Thread(target=tick, args=(context.journal,), name="agent-guest-hook-summary", daemon=True)
    thread.start()


STARTUP = startup
ROUTES: list[Route] = []
