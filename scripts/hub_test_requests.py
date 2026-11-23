"""Device-request answers of the device double of the web suite (WEB-09).

The runner (RUN-09: request service, GitHub device flow, credential cache) is not part of the web Playwright suite.
`scripts/hub_test_director.py` plays the addressed device through the hub's device API, signed with the device key:

- `POST /requests/answer {"device", "steps": [{"body", "final"?, "store"?}]}`: the newest open request of the device
  (`pending`/`delivered`) is fetched (`GET /v1/device/requests/{id}`), its envelope verified and opened with the
  device's X25519 key (so the browser's sealing is proven), then every step is answered in order
  (`POST /v1/device/requests/{id}/responses`, sealed to the user's identity, signed by the device, `seq` from 1).
  A step with `"store": {"login", "source"?, "scopes"?}` first writes the `github` credential as this device
  (`PUT /v1/device/credentials/{owner}/github`, CK-encrypted with the user's `CK` the browser wrapped for the device,
  token not real) - what RUN-09 does before answering `done`; `"store": {"kind": "claude_subscription", "email"}`
  writes the subscription credential of a Claude login the same way (RUN-12/13, WEB-10). Answers
  `{"request_id", "kind", "body"}` (the opened request body).

Frames reach the browsers as the hub publishes them (`device_response`). Only loopback, only for tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.crypto import credential as credential_format
from agent_hub.crypto import request as request_format
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.wrap import unwrap_key
from agent_hub.wraps.rules import wraps_for

Json = dict[str, Any]
Pool = ConnectionPool[psycopg.Connection[Any]]
Signed = Callable[[str, str, str, Json | None], httpx.Response]
"""`(device_id, method, path, body)` → the hub's answer to a device-signed request."""
ENC_ALG = "HPKE-X25519-A256GCM"


def now() -> datetime:
    return datetime.now(UTC)


def _open_request(pool: Pool, device_id: str) -> tuple[str, str, str]:
    """`(request_id, user_id, kind)` of the device's newest open request."""
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT id, user_id, kind FROM hub.device_request"
            " WHERE device_id = %s AND state IN ('pending', 'delivered') ORDER BY created_at DESC, id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("the device has no open request")
    return str(row[0]), str(row[1]), str(row[2])


def _user_keys(pool: Pool, user_id: str) -> tuple[bytes, bytes]:
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT kem_pub, sig_pub FROM hub.user_identity WHERE user_id = %s", (user_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("unknown user")
    return bytes(row[0]), bytes(row[1])


def _body(store: Json) -> tuple[str, Json]:
    """The kind and body of a `store` step (module docstring); tokens are not real."""
    if store.get("kind") == "claude_subscription":
        return "claude_subscription", {
            "token": "setup-token-not-real",
            "email": store.get("email"),
            "kind": "setup_token",
        }
    body: Json = {
        "token": "gho_not_a_real_token",
        "login": str(store["login"]),
        "user_id": 4242,
        "scopes": [str(scope) for scope in store.get("scopes", [])],
        "created_at": format_time(now()),
    }
    if "source" in store:
        body["source"] = str(store["source"])
    return "github", body


def _store_credential(
    pool: Pool, signed: Signed, identity: Identity, *, device: str, user: str, request_id: str, store: Json
) -> None:
    """Writes the credential of a `store` step for `user` as `device` (module docstring)."""
    kind, body = _body(store)
    with pool.connection() as connection:
        wraps = wraps_for(connection, device, scope="cred", scope_id=user)
        found = connection.execute(
            "SELECT version, header_text FROM hub.credential WHERE owner_id = %s AND kind = %s", (user, kind)
        ).fetchone()
    if not wraps:
        raise RuntimeError("the device holds no CK of the user")
    newest = max(wraps, key=lambda wrap: int(wrap["epoch"]))
    epoch = int(newest["epoch"])
    key = unwrap_key(identity.kem, scope="cred", scope_id=user, epoch=epoch, recipient=device, wrap=newest["wrap"])
    version = 0 if found is None else int(found[0])
    header = credential_format.new_header(
        id=str(uuid7()) if found is None else str(json.loads(found[1])["id"]),
        owner=user,
        kind=kind,
        version=version + 1,
        epoch=epoch,
        author=device,
        ts=format_time(now()),
    )
    record = credential_format.seal_credential(
        key, header, json.dumps(body, separators=(",", ":")).encode(), identity.sig
    )
    answer = signed(
        device,
        "PUT",
        f"/v1/device/credentials/{user}/{kind}",
        {"record": record, "expected_version": version, "request_id": request_id},
    )
    if answer.status_code != 200:
        raise RuntimeError(f"credential refused: {answer.text}")


def answer(
    pool: Pool, signed: Signed, identity_of: Callable[[str], Identity], device: str, steps: Sequence[Json]
) -> Json:
    """Answers the device's newest open request with `steps` (module docstring)."""
    identity = identity_of(device)
    request_id, user, kind = _open_request(pool, device)
    fetched = signed(device, "GET", f"/v1/device/requests/{request_id}", None)
    if fetched.status_code != 200:
        raise RuntimeError(f"request fetch refused: {fetched.text}")
    kem_pub, sig_pub = _user_keys(pool, user)
    envelope = fetched.json()["envelope"]
    opened = json.loads(request_format.open_request(identity.kem, envelope, sig_pub))
    with pool.connection() as connection:
        last = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM hub.device_response WHERE request_id = %s", (request_id,)
        ).fetchone()
    seq = int(last[0]) if last is not None else 0
    for step in steps:
        if "store" in step:
            _store_credential(
                pool, signed, identity, device=device, user=user, request_id=request_id, store=dict(step["store"])
            )
        seq += 1
        header = {
            "v": 1,
            "id": str(uuid7()),
            "user": user,
            "device": device,
            "kind": kind,
            "in_reply_to": request_id,
            "seq": seq,
            "final": bool(step.get("final", False)),
            "ts": format_time(now()),
            "enc_alg": ENC_ALG,
        }
        plaintext = json.dumps(step["body"], separators=(",", ":")).encode("utf-8")
        sealed = request_format.seal_response(header, kem_pub, plaintext, identity.sig)
        posted = signed(device, "POST", f"/v1/device/requests/{request_id}/responses", {"envelope": sealed})
        if posted.status_code != 200:
            raise RuntimeError(f"response refused: {posted.text}")
    return {"request_id": request_id, "kind": kind, "body": opened}
