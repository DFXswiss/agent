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
        "The pairing expired. Run the agent on the computer where the browser runs, or forward the port (for example "
        "`ssh -L`), or pair from another computer with `agent-platform pair --hub <url> --remote`; then pair again."
    ),
    "hub_too_old": "The hub does not support pairing from another computer yet; pair on the user's computer instead.",
    "pairing_pending": (
        "A pairing from another computer is still pending: finish it with `agent-platform pair --wait`, or start "
        "a new one with `--replace-pending`."
    ),
    "no_pending_pairing": "No pairing is pending; start one with `agent-platform pair --hub <url> --remote`.",
    "pairing_busy": "Another `agent-platform pair --wait` is running for this pairing; wait for it to finish.",
    "pairing_failed": (
        "Pair again; the confirmation did not match this agent (or the code from the page was wrong). Ask the user "
        "for a new connection."
    ),
    "claude_login_missing": "Claude Code is not logged in on this computer; run `agent-platform claude setup-token`.",
    "claude_login_expired": (
        "Use Claude Code once, then run `agent-platform claude sync`, or run `agent-platform claude setup-token`."
    ),
    "claude_cli_missing": "Install Claude Code (the `claude` program) or set AGENT_PLATFORM_CLAUDE.",
    "setup_token_failed": "Run `agent-platform claude setup-token` again and ask the user to authorize in the browser.",
    "no_org": "Create an organization (`agent-platform org create`) or name an existing one.",
    "no_device": "No execution device is available for this workgroup; try again later.",
    "not_admin": "Only an admin of the organization can do this; ask an admin.",
    "no_write_access": "The user has no write access to this workgroup; ask an admin of the organization for it.",
    "not_inviter": "Only a user with the platform role `inviter` can do this.",
    "device_request_failed": "The device did not finish the request; see `failure`, then start it again.",
    "rotation_pending": (
        "A member left and the key of this organization or workgroup must change before anything new is written "
        "there (see `scope`, `scope_id`); this user may not change it. Ask an admin (or a writer of the workgroup) to "
        "open the web client or to let their agent run `agent-platform status`; both change it on their own."
    ),
    "last_admin": "The organization needs another admin first: make a member admin (`agent-platform member role`).",
    "not_found": (
        "The member, workgroup, device, invitation or whiteboard entry is not known; check the ID with `member list`, "
        "`wg list`, `device list`, `org show` or `whiteboard list`."
    ),
    "credentials_blocked": (
        "A credential record cannot be opened (see `kinds`); replace or delete it, then run "
        "`agent-platform credentials rotate` again."
    ),
    "invalid_argument": "Check the command line (`agent-platform <command> --help`).",
    "state_damaged": ("The state directory has a wrong mode, owner or a symlink; fix it or remove it and pair again."),
    "internal": "An unexpected error happened; run the command again.",
}
CODES = frozenset(NEXT)
# A different next step for one reason of a code (the texts are fixed here as well).
NEXT_FOR_REASON: dict[tuple[str, str], str] = {
    ("pairing_failed", "claim_lost"): (
        "The pairing may have completed at the hub while its answer was lost, so the hub may list this agent as "
        "connected although it kept nothing. Ask your user to disconnect it in the settings (Connected agents), "
        "then pair again."
    ),
}


class PlatformError(Exception):
    """A failure with a code of `CODES` and extra fields (codes of closed lists only)."""

    def __init__(self, code: str, **fields: object) -> None:
        if code not in CODES:
            raise ValueError(f"unknown error code {code}")
        self.code = code
        self.fields = fields
        super().__init__(code)

    def to_json(self) -> dict[str, object]:
        step = NEXT_FOR_REASON.get((self.code, str(self.fields.get("reason"))), NEXT[self.code])
        return {"ok": False, "error": self.code, "next": step, **self.fields}
