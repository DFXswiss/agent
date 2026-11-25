"""The runner's part of a main orchestrator session for the web Playwright suite (ADR-082 section 6, 05-runner.md
section 17; used by `scripts/hub_test_director.py`).

The test draws the session's keys itself (the agent side, as the tool in the VM would); this module plays the
session's device:

- `register`: the device's signature over the fingerprint (`orchestrator_key_text`) and the device-signed
  `POST /v1/device/orchestrator-pairings`; answers `{status, body}` of the hub. With `forged` the pairing is written
  straight into the hub's table with the signature of another key - what a hub that swapped the device key would
  show the browser (the hub's own check would refuse it).
- `view`: the device-signed `GET /v1/device/sessions/{id}/orchestrator` (the stored handover `{ct, sig}`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.keys import Identity

Json = dict[str, Any]
Signed = Callable[[str, str, str, Json | None], httpx.Response]
PAIRING_SECONDS = 600


def _device(pool: ConnectionPool[psycopg.Connection[Any]], session_id: str) -> str:
    with pool.connection() as connection:
        row = connection.execute("SELECT device_id FROM hub.session WHERE id = %s", (session_id,)).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("the session has no device")
    return str(row[0])


def _answer(response: httpx.Response) -> Json:
    return {"status": response.status_code, "body": response.json()}


def register(
    pool: ConnectionPool[psycopg.Connection[Any]],
    signed: Signed,
    identity: Callable[[str], Identity],
    session_id: str,
    body: Json,
) -> Json:
    """The session's keys as the runner registers them (module docstring)."""
    device = _device(pool, session_id)
    pairing_id, fp = str(body["pairing_id"]), str(body["fp"])
    text = agent_format.orchestrator_key_text(fp, pairing_id, session_id)
    if not body.get("forged"):
        device_sig = b64u_encode(identity(device).sig.sign(text))
        fields = {key: body[key] for key in ("pairing_id", "pub", "pop", "fp")}
        request = {"session_id": session_id, **fields, "device_sig": device_sig}
        return _answer(signed(device, "POST", "/v1/device/orchestrator-pairings", request))
    other = Identity.generate()
    now = datetime.now(UTC)
    with pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, created_at, expires_at, session_id, device_id,"
            " device_sig) VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s)",
            (
                pairing_id,
                b64u_decode(body["pub"]["kem"]),
                b64u_decode(body["pub"]["sig"]),
                now,
                now + timedelta(seconds=PAIRING_SECONDS),
                session_id,
                device,
                other.sig.sign(text),
            ),
        )
    return {"status": 200, "body": {"pairing_id": pairing_id}}


def view(pool: ConnectionPool[psycopg.Connection[Any]], signed: Signed, session_id: str) -> Json:
    """The device's view of the session's orchestrator pairing (module docstring)."""
    device = _device(pool, session_id)
    return _answer(signed(device, "GET", f"/v1/device/sessions/{session_id}/orchestrator", None))
