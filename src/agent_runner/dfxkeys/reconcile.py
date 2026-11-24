"""Reconcile step `dfx_keys` (order 47: after the logs and the state, before the sessions step 50, so a session that
starts in the same run already finds its organization's DFX key; ADR-080 section 7). It acts only on the state of a
hub with `model_fallback.api >= 1` (an older hub has no DFX keys).

- Org device: `model.dfxkey.accept` verifies and stores the envelopes of `dfx_key_held`.
- Platform device: the issuer takes `dfx_keys` of the state and announces itself (`issuer.Issuer.observe`); the work
  with the provider runs in the task `dfx_keys`, so a slow provider never holds up the reconcile run.
"""

from __future__ import annotations

from agent_runner.dfxkeys.issuer import fallback_api, issuer_for
from agent_runner.model import dfxkey
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    if not fallback_api(run.desired):
        return
    assert run.desired is not None
    # the engine runs only on an enrolled device, where only an org device has no issuer
    issuer = issuer_for(run.context)
    if issuer is None:
        await dfxkey.accept(run.context, run.desired)
    else:
        await issuer.observe(run.desired)


STEPS = [ReconcileStep(47, "dfx_keys", step)]
