"""Hook addon: the ping notice on the next tool or prompt answer (05-runner.md section 9, 03-api.md section 8.2).

For ``PreToolUse``, ``PostToolUse`` and an accepted ``UserPromptSubmit`` (``call.decorate``) the addon asks the
control process for the pending batch (api.sock op ``ping.notice``); a batch is returned once, marked noticed and
journaled as ``ping.delivered {via:"hook"}``, and its fixed notice becomes ``additionalContext``. ``Stop`` is never
listed (a ``Stop`` answer with context would make the session continue), and a blocked prompt consumes nothing
(GST-03: only consume what can be delivered). A failure of the op is counted by
``hooks.py`` as ``guest.error hook_handler_failed``; the hook decision is unchanged.
"""

from __future__ import annotations

from agent_guest.hooks import HookAddon, HookCall
from agent_guest.server import ApiContext

EVENTS = frozenset({"PreToolUse", "PostToolUse", "UserPromptSubmit"})


def handle(context: ApiContext, call: HookCall) -> str | None:
    if not call.decorate:
        return None
    notice = context.control.call("ping.notice", {"session_id": call.session_id})["notice"]
    return notice if isinstance(notice, str) else None


ADDON = HookAddon(events=EVENTS, handle=handle)
