"""What an agent may call: everything its user may (11-agent-access.md section 6, ADR-056 section 4).

Decision of the owner (2026-11-23): an AI agent that connects with its own key may do everything a person who
connects with a passkey may do. The request pipeline therefore admits an `active` agent on **every** route a browser
session of its user is admitted to (authentication classes `browser`, `anon_or_browser` and `anon`, and requests
that match no route, which get the same `404` as a browser's), with the same rights and without any filter on
body or query; the user's own rights apply in every handler, so an agent is never more and never less than its user.
Equality is the default: a browser route added later is open to agents without any change here.

The only exceptions are the WebAuthn ceremonies themselves, which cannot be performed without an authenticator, and
the confirmation of a main orchestrator, whose essence is the user's passkey approval (concept idea 34: one main
orchestrator per user, approved with a passkey; an agent never pairs or replaces an agent); each entry of
`EXCEPTIONS` names its reason. Where the web client needs a passkey gesture (adding a passkey), the agent
tool opens the page in the user's browser. A `confirmed` agent (paired, not yet claimed) may only claim and fetch
the handover of a remote pairing (`CONFIRMED_ROUTES`, ADR-062); agents
also have their own routes (authentication class `agent`). `tests/platform/hub/agents/test_scope.py` checks every
registered route against this rule.
"""

from __future__ import annotations

from collections.abc import Mapping

ACTIVE = "active"
CONFIRMED = "confirmed"
CLAIM = ("POST", "/v1/agent/claim")
HANDOVER = ("GET", "/v1/agent/handover")
CONFIRMED_ROUTES = frozenset({CLAIM, HANDOVER})
# The authentication classes a signed browser session passes, plus the agent's own class (decisions KOR-24 item 1).
# `device_enroll` is signed by the new device's key, which the enrolment route checks itself.
BROWSER_ADMITTED = frozenset({"browser", "anon_or_browser", "anon", "device_enroll", "agent"})

EXCEPTIONS: Mapping[tuple[str, str], str] = {
    ("POST", "/v1/passkeys/register/begin"): "creates a passkey: a WebAuthn create() ceremony needs an authenticator",
    ("POST", "/v1/passkeys/register/finish"): "finishes the create() ceremony of a new passkey (authenticator)",
    ("POST", "/v1/me/recovery/begin"): "starts a WebAuthn get() ceremony for a passkey assertion (authenticator); "
    "an agent replaces the recovery code with its signed request instead",
    ("POST", "/v1/me/agents"): "confirms (and so replaces) the user's main orchestrator: the concept requires the "
    "user's passkey approval for it (idea 34); the agent tool opens the pairing page in the user's browser",
}


def agent_allowed(method: str, path: str, auth: str, state: object) -> bool:
    """Whether an agent in `state` may call the route `method path` (path template as declared) of the
    authentication class `auth`."""
    if (method, path) in CONFIRMED_ROUTES:
        return state in (CONFIRMED, ACTIVE)
    if state != ACTIVE or (method, path) in EXCEPTIONS:
        return False
    return auth in BROWSER_ADMITTED
