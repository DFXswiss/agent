"""Whose debug sign-in a `dfx.debug` operation may use (ADR-102 section 3).

The sign-in is a person's own access to production data, so it is used only where its owner switched it on:

- the operation belongs to a task of the owner (`session.owner_id`, the task owner) and the person whose instruction
  started the turn is that owner (`operation.actor_id`, `credentials.acting`: a shared session's other writers, a turn
  of unknown origin and the platform's own continuation use nobody's sign-in) - `rights_denied`;
- the owner holds a `dfx_debug` record that verifies and opens under the owner's `CK` on this device
  (`credentials.cache`) - `not_found` otherwise;
- its **sealed** release `use.wgs` names the session's workgroup and the owner has explicit `read` or `write` access
  to that workgroup on the verified membership log now - `rights_denied` otherwise.

`signin_for(context, operation, session)` returns the `SignIn` for one use or raises `CheckFailed` with the remedy
(E-05). The opened body lives only in the returned object, is never logged and never leaves the device; a hub that
cannot be reached is `hub_unreachable`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_runner.credentials import cache
from agent_runner.credentials.cache import CredentialUnavailable
from agent_runner.operations.extensions import CheckFailed
from agent_runner.orgs.book import book_for
from agent_runner.orgs.db import session as store_session
from agent_runner.sync.http import HubRefusal, HubTransportError

KIND = "dfx_debug"
PRINTABLE_RE = re.compile(r"[!-~]{1,4096}")
API_URL_RE = re.compile(r"https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~-]{1,64}){0,8}")
STORE = "agent-platform credentials set-dfx-debug"
NOT_OWNER = (
    "Only the task owner's own instructions may use the task owner's DFX debug sign-in. Ask the task owner to give"
    " this instruction."
)
MISSING = (
    f"The task owner has no DFX debug sign-in on the platform. The owner stores it with {STORE} (signature on standard"
    " input) and releases it to this workgroup with --release."
)
NOT_RELEASED = (
    f"The task owner's DFX debug sign-in is not released to this workgroup. The owner releases it with {STORE}"
    " --release <workgroup id> (or agent-platform credentials release-dfx-debug)."
)
UNREACHABLE = "The platform could not read the DFX debug sign-in; try again."


@dataclass(frozen=True)
class SignIn:
    """One opened debug sign-in (never printed: the address and the signature are left out of `repr`)."""

    record_id: str
    owner_id: str
    api_url: str
    address: str = field(repr=False)
    signature: str = field(repr=False)


def _sign_in(record_id: str, owner: str, body: dict[str, Any], wg_id: str) -> SignIn | None:
    """The sign-in of a body released to `wg_id`, None for any other body."""
    use = body.get("use")
    wgs = use.get("wgs") if isinstance(use, dict) else None
    if not isinstance(wgs, list) or wg_id not in wgs:
        return None
    address, signature, api_url = body.get("address"), body.get("signature"), body.get("api_url")
    if not all(isinstance(value, str) and PRINTABLE_RE.fullmatch(value) for value in (address, signature)):
        return None
    if not isinstance(api_url, str) or API_URL_RE.fullmatch(api_url) is None:
        return None
    return SignIn(record_id, owner, api_url.rstrip("/"), str(address), str(signature))


async def signin_for(context: Any, operation: Any, session: Any) -> SignIn:
    """The sign-in this operation may use (module docstring); `CheckFailed` otherwise."""
    owner = session.owner_id
    if owner is None or operation.actor_id != owner:
        raise CheckFailed("rights_denied", message=NOT_OWNER)
    try:
        async with store_session(context.pool) as conn:
            cached = await cache.cached(conn, owner, KIND)
        record = cached[1] if cached is not None else await cache.fetch(context, owner, KIND)
        if record is None:
            raise CredentialUnavailable("missing")
        body = await cache._open(context, owner, KIND, record)
    except (CredentialUnavailable, cache.RecordRefused):
        raise CheckFailed("not_found", message=MISSING) from None
    except (HubRefusal, HubTransportError):
        raise CheckFailed("hub_unreachable", message=UNREACHABLE) from None
    found = _sign_in(str(record["header"]["id"]), owner, body, session.wg_id)
    body.clear()
    log = book_for(context).log(session.org_id)
    if found is None or log is None or log.state.explicit_access(owner, session.wg_id) is None:
        raise CheckFailed("rights_denied", message=NOT_RELEASED)
    return found
