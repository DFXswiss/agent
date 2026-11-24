"""The conversion of an agent that still holds its user's identity into an agent with its own identity (ADR-082
sections 5 and 9.10), done before every command of such an account (`ensure`, from `Context.account`).

`GET /v1/agent/me` decides:

- `identity: "own"`: the hub converted the agent already (an earlier run stored it and was interrupted before it
  rewrote its state, or the hub answered after the tool stopped): the state takes the hub's `user_ct` and `gen`, after
  they open with this agent's key and `r` and the certificate verifies under the user's keys for this agent
  (`account.open_anchor`); the sealed identity is deleted from the state;
- `identity: "user"` with `migrate` (the user has a passkey): the conversion with the generation the hub names
  (`migrate_gen`). The certificate is signed with the identity the agent holds, the user's public keys are sealed to
  the agent under its own `r` (kept in the state as `converting` before it is sent, so a repeat after a crash sends
  the same `user_ct`, which the hub answers unchanged), every organization gets `agent_add{by: user}` with the wraps of
  every epoch held and the `CK` wraps (`delegate.delegation`), all in one `POST /v1/agent/identity`; then the state is
  rewritten as above;
- otherwise (the registering agent of an account of an agent alone): nothing.

The opened account is replaced by the converted one: from then on the agent signs as itself.
"""

from __future__ import annotations

from typing import Any

from agent_platform import delegate
from agent_platform.account import OWN, Account, open_anchor, unverified
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError

CONVERTING = "converting"


def _adopt(account: Account, user_ct: object, gen: object) -> Account:
    """Rewrite the state to the own identity (`user_ct`, `gen`) once they verify; the re-opened account."""
    state = {key: value for key, value in account.state.items() if key not in ("bundle_ct", CONVERTING)}
    state.update(user_ct=user_ct, gen=gen)
    try:
        anchor = open_anchor(account.agent, state)
    except (CryptoError, KeyError, TypeError, ValueError):
        raise unverified("identity_mismatch") from None
    if {"kem": anchor["kem"], "sig": anchor["sig"]} != account.user_pub:
        raise unverified("identity_mismatch")
    account.home.save(state)
    account.close()
    return Account.open(account.home, account.platform_root)


def _convert(account: Account, gen: int) -> Account:
    pending = account.state.get(CONVERTING)
    kept = pending.get("user_ct") if isinstance(pending, dict) and pending.get("gen") == gen else None
    secret = b64u_decode(account.state["secret"], 32)
    pub = account.agent.public()
    first = delegate.delegation(account, agent_id=account.agent_id, pub=pub, gen=gen, secret=secret, user_ct=kept)
    account.state[CONVERTING] = {"gen": gen, "user_ct": first["user_ct"]}
    account.home.save(account.state)

    built = [first]

    def send() -> dict[str, Any]:
        body = (
            built.pop()
            if built
            else delegate.delegation(
                account, agent_id=account.agent_id, pub=pub, gen=gen, secret=secret, user_ct=first["user_ct"]
            )
        )
        return account.call("POST", "/v1/agent/identity", body)

    delegate.submit(account, send)
    return _adopt(account, first["user_ct"], gen)


def ensure(account: Account) -> Account:
    """The account converted if the hub asks for it (module docstring); a failure closes the account's client (its
    event loop and the loop's resolver threads) before it is passed on."""
    try:
        return _ensure(account)
    except BaseException:
        account.close()
        raise


def _ensure(account: Account) -> Account:
    if account.mode == OWN:
        return account
    me = account.call("GET", "/v1/agent/me")
    if me.get("identity") == OWN:
        return _adopt(account, me.get("user_ct"), me.get("gen"))
    gen = me.get("migrate_gen")
    if me.get("migrate") is not True or not isinstance(gen, int) or isinstance(gen, bool):
        return account
    return _convert(account, gen)
