"""The delegation of the user's rights to an agent with its own identity, made with the user's identity (ADR-082
sections 2, 5, 7 and 9; twin of the web client's confirmation).

Only an account that holds the user's identity (mode "user") delegates: the registering agent of an account of an
agent alone when it confirms a successor (`agents confirm`, `alone.py`) and an agent paired before ADR-082 when it
converts itself (`convert.py`). `delegation` builds what `POST /v1/me/agents` (new form) and `POST /v1/agent/identity`
carry:

- `cert`: the user's signature over the certificate text of the agent's ID, keys and generation;
- `user_ct`: the user's public keys and the certificate sealed to the agent under the handover secret `r`;
- `batches`: per organization of `GET /v1/me` one append batch `agent_add{by: user}` with the wraps the batch builder
  makes (`batch.build_batch`): every epoch the user holds of every scope it reads, to the agent; a previous agent of
  the user with a lower generation is replaced by the entry and its R is rotated in the same batch;
- `cred_wraps`: every epoch of the user's `CK` the account holds, wrapped to the agent.

`submit` sends the body; after `seq_conflict` (a log moved on meanwhile) the logs are read again and everything is
built once more (ADR-082 section 9.12), at most `ATTEMPTS` times.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_platform.account import Account
from agent_platform.batch import build_batch, make_wrap
from agent_platform.errors import PlatformError
from agent_platform.membership import platform_for
from agent_platform.orgs import ATTEMPTS, crypto_refusal, now_text
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log.wraps import CRED, WrapSlot

Body = dict[str, Any]


def holder(account: Account) -> Identity:
    """The user's identity the account holds (`not_own_account` for an agent with its own identity: only the user's
    clients sign a certificate)."""
    if account.identity is None:
        raise PlatformError("not_own_account", reason="own_identity")
    return account.identity


def _orgs(account: Account) -> list[str]:
    listed = account.me().get("orgs", [])
    return sorted(str(item["org_id"]) for item in listed if isinstance(item, dict) and "org_id" in item)


def _batch(account: Account, org: str, add: dict[str, object]) -> Body:
    identity = holder(account)
    log = account.org_log(org)
    try:
        built = build_batch(
            log,
            org=org,
            author=account.user_id,
            identity=identity,
            fields=[add],
            ts=now_text(),
            held=account.keys(org),
            platform=platform_for(account, log.state, []),
            held_only=True,
            agent_history=True,
        )
    except CryptoError as error:
        raise crypto_refusal(error) from None
    return {"org": org, "expected_seq": built.expected_seq, "entries": built.entries, "wraps": built.wraps}


def delegation(
    account: Account, *, agent_id: str, pub: dict[str, str], gen: int, user_ct: str | None = None, secret: bytes
) -> Body:
    """`{gen, cert, user_ct, batches, cred_wraps}` for the agent `agent_id` with the keys `pub` (module docstring);
    `user_ct` given keeps a sealed anchor made before (a repeat must send the same one)."""
    identity, user = holder(account), account.user_id
    cert = agent_crypto.sign_cert(identity.sig, agent_id, pub, user, gen)
    kem = b64u_decode(pub["kem"], 32)
    if user_ct is None:
        user_ct = agent_crypto.seal_user(kem, secret, agent_id, user, user_pub=identity.public(), cert=cert)
    add = {"type": "agent_add", "subject": agent_id, "user": user, "pub": pub, "gen": gen, "cert": cert}
    batches = [_batch(account, org, add) for org in _orgs(account)]
    cred = [
        make_wrap(identity, WrapSlot(CRED, user, epoch, agent_id), kem, key)
        for epoch, key in sorted(account.cred_keys().items())
    ]
    return {"gen": gen, "cert": cert, "user_ct": user_ct, "batches": batches, "cred_wraps": cred}


def submit(account: Account, send: Callable[[], Body]) -> Body:
    """`send()` again on freshly read logs after `seq_conflict` (module docstring)."""
    number = 1
    while True:
        try:
            return send()
        except PlatformError as error:
            if error.fields.get("hub_error") != "seq_conflict" or number == ATTEMPTS:
                raise
        for org in _orgs(account):
            account.forget(org)
        number += 1
