"""Opening stored, verified workgroup events into memory (04-crypto.md sections 4.3 and 5.12).

The operation area keeps `settings.workgroup` and `operation.result` events exactly as the inbox verified them
(ciphertext, signature) together with the author's signing key from the verified log at the event's `lh`. `open_stored`
opens one again with the workgroup key of the header's epoch: the signature under the stored key and the decryption
must both succeed and the body must be a JSON object, else None.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.orgs.keys import load_scope_key


async def open_stored(
    conn: AsyncConnection[Any], storage_key: bytes, wg_id: str, sealed: object, author_sig: str
) -> dict[str, Any] | None:
    header = sealed.get("header") if isinstance(sealed, Mapping) else None
    epoch = header.get("epoch") if isinstance(header, Mapping) else None
    if type(epoch) is not int:
        return None
    assert isinstance(sealed, Mapping)
    try:
        key = await load_scope_key(conn, storage_key, "wg", wg_id, epoch)
        if key is None:
            return None
        body = parse_json(open_event(key, sealed, b64u_decode(author_sig)))
    except (CryptoError, KeyError, TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None
