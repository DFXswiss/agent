"""Revoke at GitHub what "GitHub trennen" deletes (KOR-40; 03-api.md section 4.8, ADR-015).

When the user disconnects GitHub, the hub deletes the `github` credential record and the devices purge their cached
copy (`credentials.cache.fetch`: `404`). Just before the purge this module gets the decrypted body
(`cache.subscribe_before_purge`) and revokes its token at GitHub with the unauthenticated credential revocation
(`POST /credentials/revoke {"credentials": [token]}`, answered `202`) - but only a token this device minted itself
through the device flow (`remember`, table `runner.github_minted`: owner and SHA-256 of the token, never the token),
and only while this device is still an eligible reader of the owner's credential key in the verified logs (a device
that lost its eligibility also gets `404` and purges, but the user did not disconnect). Never revoked:

* the operator's token (ADR-053), which approved users share: it is stored by the operator path, which remembers
  nothing; on a platform device a token equal to the operator's current token is skipped as well;
* a token the user transferred from elsewhere (for example the GitHub CLI's token): revoking it would sign the user
  out there.

The outcome is a fact line `github_revoke outcome=<revoked|skipped|failed> reason=<...>`; the token is never logged.
A failure changes nothing else: the record is purged anyway, and the remembered hash is forgotten.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_runner.credentials import cache
from agent_runner.github import operator
from agent_runner.github.client import GitHubError, settings_for
from agent_runner.orgs.db import session

PATH = "/credentials/revoke"
KIND = "github"
ATTRIBUTE = "github_revoke_subscribed"


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.github {text}\n")
    context.log.flush()


def _operator_token(context: Any) -> str | None:
    path = context.config.operator_github_token_file
    if path is None:
        return None
    try:
        return operator.read_token(path, operator.state_for(context), lambda text: _line(context, text))
    except operator.Unusable:
        return None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def remember(context: Any, owner: str, token: str) -> None:
    """Note that this device minted `token` for `owner` through the device flow."""
    async with session(context.pool) as conn:
        await conn.execute(
            "INSERT INTO runner.github_minted (owner_id, token_sha256) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (owner, _digest(token)),
        )


async def _forget(context: Any, owner: str, token: str) -> bool:
    """Forget a minted token; True when this device had minted it."""
    async with session(context.pool) as conn:
        cur = await conn.execute(
            "DELETE FROM runner.github_minted WHERE owner_id = %s AND token_sha256 = %s", (owner, _digest(token))
        )
    return cur.rowcount > 0


async def revoke(context: Any, owner: str, kind: str, body: dict[str, Any]) -> str:
    """Revoke the token of a purged `github` record (module docstring); the outcome."""
    if kind != KIND:
        return "ignored"
    token = body.get("token")
    if not isinstance(token, str) or not await _forget(context, owner, token):
        _line(context, f"github_revoke outcome=skipped reason=not_minted owner={owner}")
        return "skipped"
    if not cache.eligible(context, owner):
        _line(context, f"github_revoke outcome=skipped reason=not_eligible owner={owner}")
        return "skipped"
    if token == _operator_token(context):
        _line(context, f"github_revoke outcome=skipped reason=operator owner={owner}")
        return "skipped"
    try:
        async with settings_for(context).client(None) as client:
            await client.request("POST", PATH, body={"credentials": [token]})
    except GitHubError as error:
        _line(context, f"github_revoke outcome=failed reason={error.kind} owner={owner}")
        return "failed"
    _line(context, f"github_revoke outcome=revoked owner={owner}")
    return "revoked"


def subscribe(context: Any) -> None:
    """Register the revocation once per daemon."""
    if getattr(context, ATTRIBUTE, False):
        return

    async def listener(owner: str, kind: str, body: dict[str, Any]) -> None:
        await revoke(context, owner, kind, body)

    cache.subscribe_before_purge(context, frozenset({KIND}), listener)
    setattr(context, ATTRIBUTE, True)
