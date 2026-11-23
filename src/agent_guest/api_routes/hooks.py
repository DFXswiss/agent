"""``POST /hooks/<Event>`` for the 18 Claude Code hook events (05-runner.md section 5.2, 03-api.md section 8.2).

The handlers live in ``agent_guest.hooks``; the hook addons of ``hook_addons/`` are discovered once when the API
process loads its routes (a malformed addon refuses the start).
"""

from __future__ import annotations

from agent_guest import hooks

ROUTES = hooks.routes(hooks.load_addons())
