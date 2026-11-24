"""The agent adds itself where its user is and it is missing (ADR-082 sections 1 and 9.1; owner rule E-04), an upkeep
step before every writing command (`upkeep.prepare`).

An agent with its own identity is a sub-principal of its user in every log of the user. A log can lack it: the user
was admitted by somebody else (`admit`), accepted an invitation in a browser, or founded an organization there. For
every organization of `GET /v1/me` whose verified log has the user as an active member and has no active agent of the
user, the tool appends `agent_add{by: itself}` with its certificate and generation (signed with its own key, L5). The
agent holds no key of that organization yet: the keys follow by catch-up from their holders; the batch wraps only
what the agent already holds (`agent_history`, the keys it learned). A log in which another active agent of the user
is, is left alone; a refusal (for example a generation that log does not allow any more) is reported per org.
"""

from __future__ import annotations

from typing import Any

from agent_platform.account import OWN, Account
from agent_platform.batch import build_batch
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.membership import platform_for
from agent_platform.orgs import crypto_refusal, now_text, require_head, with_retry
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.verify import VerifiedLog


def missing(account: Account, log: VerifiedLog) -> bool:
    """The user is an active member and has no active agent in the log."""
    state = log.state
    return state.member(account.user_id) is not None and state.agent_of(account.user_id) is None


def add(account: Account, org: str) -> dict[str, Any] | None:
    """`agent_add{by: itself}` in `org` while it is missing there; None when nothing was appended."""
    entry = {
        "type": "agent_add",
        "subject": account.agent_id,
        "user": account.user_id,
        "pub": account.agent.public(),
        "gen": int(account.state["gen"]),
        "cert": account.cert,
    }

    def attempt() -> dict[str, Any] | None:
        log = account.org_log(org)
        if not missing(account, log):
            return None
        try:
            built = build_batch(
                log,
                org=org,
                author=account.agent_id,
                identity=account.agent,
                fields=[entry],
                ts=now_text(),
                held=account.keys(org),
                platform=platform_for(account, log.state, []),
                held_only=True,
                agent_history=True,
            )
        except CryptoError as error:
            raise crypto_refusal(error) from None
        answer = account.call("POST", f"/v1/orgs/{segment(org)}/log", built.body([]))
        require_head(answer, built.log)
        account.remember(org, built.log)
        return {"org_id": org, "seq": built.log.head.seq}

    return with_retry(account, org, attempt)


def add_everywhere(account: Account) -> tuple[list[dict[str, Any]], list[tuple[str, PlatformError]]]:
    """The organizations the agent added itself to, and the refusals per organization (module docstring)."""
    if account.mode != OWN:
        return [], []
    added: list[dict[str, Any]] = []
    failed: list[tuple[str, PlatformError]] = []
    try:
        listed = account.me().get("orgs", [])
    except PlatformError:
        return [], []  # the org list is reported by the other steps; the next run adds
    for item in listed:
        org = str(item.get("org_id")) if isinstance(item, dict) else ""
        try:
            done = add(account, org)
        except PlatformError as error:
            failed.append((org, error))
            continue
        if done is not None:
            added.append(done)
    return added, failed
