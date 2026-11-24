"""Hook addon: the session notice on the first decorated answer after a resume or restore (KOR-33, ADR-072 section 7).

Claude Code keeps the system prompt of a conversation in its transcript and ignores ``--append-system-prompt-file``
on ``--resume``, and 2.1.330 sends no ``SessionStart`` hook (ADR-029). What changed - the runner's sentence of a
restore (``restore.note``) or the guest's sentence of a resume - and the session facts the conversation does not know
yet therefore reach it through the hook path Claude Code does add to the conversation: for ``PreToolUse``,
``PostToolUse`` and an accepted ``UserPromptSubmit`` (``call.decorate``; normally the resumed prompt itself) the
addon asks the control process for the pending notice (api.sock op ``session.notice``), which is handed out once and
becomes ``additionalContext`` (``contracts/mcp/objects.json#/$defs/session_notice``). It runs before the ping addon
(module order), so a ping notice follows it after a blank line. ``Stop`` is never listed and a blocked prompt
consumes nothing (GST-03: only consume what can be delivered). A failure of the op is counted by ``hooks.py`` as
``guest.error hook_handler_failed``; the notice then stays pending for the next answer.
"""

from __future__ import annotations

from agent_guest.hooks import HookAddon, HookCall
from agent_guest.server import ApiContext

EVENTS = frozenset({"PreToolUse", "PostToolUse", "UserPromptSubmit"})


def handle(context: ApiContext, call: HookCall) -> str | None:
    if not call.decorate:
        return None
    notice = context.control.call("session.notice", {"session_id": call.session_id})["notice"]
    return notice if isinstance(notice, str) else None


ADDON = HookAddon(events=EVENTS, handle=handle)
