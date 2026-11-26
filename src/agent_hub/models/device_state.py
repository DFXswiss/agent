"""The sections `model_fallback`, `dfx_keys` and `dfx_key_held` of `GET /v1/device/state` (ADR-081, KOR-50; ADR-027
section 4 `<area>/device_state.py:SECTION`).

- `model_fallback: {api: 2}` for every active device: this hub accepts the items `operator_quota`, `dfx_key_*`, the
  field `limited` of `quota`, the wait of `session_transition` and the audit action `session.model_switched` (1), with
  the cause `renewed` (2, HOT-26); a runner sends them only when it sees the field and version (the per-session
  `fallback` and `model_consent` come with `sessions`).
- `dfx_keys` only for the elected issuer (`agent_hub.models.dfx.issuer_state`, headroom capped by
  `AGENT_HUB_DFX_KEY_HEADROOM_USD`).
- `dfx_key_held` for every org device: the sealed key of each organization it holds one for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import OMIT, Section, StateRequest
from agent_hub.models import dfx

API = 2


def provide(request: StateRequest) -> Mapping[str, Any]:
    connection, device, now = request.connection, request.device_id, request.now
    elected = dfx.issuer(connection, now, request.config.device_online_seconds)
    keys = dfx.issuer_state(connection, now, request.config.dfx_key_headroom_usd) if elected == device else OMIT
    held = OMIT if dfx.platform_device(connection, device) else dfx.held(connection, device)
    return {"model_fallback": {"api": API}, "dfx_keys": keys, "dfx_key_held": held}


SECTION = Section(("model_fallback", "dfx_keys", "dfx_key_held"), provide)
