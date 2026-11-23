"""The closed list of error codes of the agent tool and their next steps (11-agent-access.md sections 8.1, 8.4).

Every failure ends as `{"ok": false, "error": <code>, "next": <text>}`. The texts come from this module only, never
from the hub. `PlatformError` may carry extra fields whose values are themselves codes of closed lists (a hub error
code of `agent_runner.sync.codes`, a crypto code of `agent_runner.crypto.errors`), never free text.
"""

from __future__ import annotations

NEXT: dict[str, str] = {
    "not_paired": "Run `agent-platform pair --hub <url>` on the computer where the user's browser runs.",
    "agent_revoked": (
        "The user disconnected this agent or approved another agent as main orchestrator. Pair again only if the "
        "user asks for it."
    ),
    "hub_unreachable": "Check the network connection and the hub address, then run the command again.",
    "hub_refused": "The hub refused the request or sent an answer that does not verify; see `hub_error`/`reason`.",
    "pairing_timeout": (
        "Run the agent on the computer where the browser runs, or forward the port (for example `ssh -L`), then "
        "pair again."
    ),
    "pairing_failed": "Pair again; the confirmation did not match this agent.",
    "claude_login_missing": "Claude Code is not logged in on this computer; run `agent-platform claude setup-token`.",
    "claude_login_expired": (
        "Use Claude Code once, then run `agent-platform claude sync`, or run `agent-platform claude setup-token`."
    ),
    "claude_cli_missing": "Install Claude Code (the `claude` program) or set AGENT_PLATFORM_CLAUDE.",
    "setup_token_failed": "Run `agent-platform claude setup-token` again and ask the user to authorize in the browser.",
    "no_org": "Create an organization (`agent-platform org create`) or name an existing one.",
    "no_device": "No execution device is available for this workgroup; try again later.",
    "not_admin": "Only an admin of the organization can do this; ask an admin.",
    "invalid_argument": "Check the command line (`agent-platform <command> --help`).",
    "state_damaged": ("The state directory has a wrong mode, owner or a symlink; fix it or remove it and pair again."),
    "internal": "An unexpected error happened; run the command again.",
}
CODES = frozenset(NEXT)


class PlatformError(Exception):
    """A failure with a code of `CODES` and extra fields (codes of closed lists only)."""

    def __init__(self, code: str, **fields: object) -> None:
        if code not in CODES:
            raise ValueError(f"unknown error code {code}")
        self.code = code
        self.fields = fields
        super().__init__(code)

    def to_json(self) -> dict[str, object]:
        return {"ok": False, "error": self.code, "next": NEXT[self.code], **self.fields}
