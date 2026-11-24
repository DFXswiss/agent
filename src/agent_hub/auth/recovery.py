"""Recovery with the recovery code (03-api.md section 4.1, 04-crypto.md section 5.5).

1. `POST /v1/recovery/begin {recovery_id}` (anonymous): the hub finds the active user whose `user_identity.recovery_id`
   it is (`404 not_found` otherwise) and answers `{user_id, bundle_ct, challenge}` - the recovery slot of the identity
   bundle and a fresh 32-byte challenge. The challenge is stored as a `recovery` ceremony that carries the challenge
   also as `hub_value` (the mark of a proof ceremony; 300 s like every ceremony). Rate limits: 5 begins per hour and
   source address (every attempt counts, also unknown IDs; per hub process, like the limits of
   `agent_hub.http.limits`) and 20 per day and recovery ID (counted from the proof ceremonies of the user in the
   database, which ceremonies keep for one day); beyond, `429 rate_limited` with `Retry-After`. Audit
   `auth.recovery {step: "begin"}`.
2. `POST /v1/recovery/finish {user_id, challenge, proof}` (anonymous): the proof ceremony of that user and challenge
   (unused, not expired; `400 ceremony_invalid` otherwise) gets one attempt: the proof
   `Ed25519_identity(P || "recovery-proof\\0" || user_id || "\\0" || challenge)` must verify with the registered
   identity (`agent_hub.crypto.recovery.verify_proof`). Afterwards the challenge of the proof ceremony is replaced by
   random bytes, so it can never be presented again. A wrong proof is `401 signature_invalid` (audited
   `auth.recovery {step: "finish"}`, result `denied`). A valid proof opens the registration of a new passkey: a
   `recovery` ceremony without `hub_value` and the creation options (excluding the user's active passkeys), finished by
   `POST /v1/passkeys/register/finish` (`agent_hub.auth.register`, audited there as `auth.recovery {step: "finish"}`).

The used registration ceremony is what then allows the browser to replace the recovery slot with a new recovery code
once (`PUT /v1/me/bundles/recovery {bundle_ct, new_recovery_id}`, `agent_hub.me.routes`). Proof ceremonies are never
marked used, so a refused proof never allows that replacement.
"""

from __future__ import annotations

import math
import secrets
import threading
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from typing import Annotated, Any, cast

from pydantic import StringConstraints

from agent_core.signing import b64url_encode
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.ceremonies import (
    CEREMONY_SECONDS,
    CHALLENGE_BYTES,
    create_ceremony,
    creation_options,
    new_id,
)
from agent_hub.auth.fields import B64u16Id, B64u32, B64u64, Strict, decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.recovery import verify_proof
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext

PER_SOURCE = 5
SOURCE_WINDOW_SECONDS = 3600.0
PER_RECOVERY_ID = 20
RECOVERY_ID_WINDOW_SECONDS = 24 * 3600
MAX_SOURCES = 100_000

Uuid = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
]


class RecoveryBegin(Strict):
    recovery_id: B64u16Id


class RecoveryFinish(Strict):
    user_id: Uuid
    challenge: B64u32
    proof: B64u64


class SourceLimit:
    """At most `limit` begins per `window` seconds and source address (sliding window, per hub process; the least
    recently used sources are dropped beyond `max_sources`)."""

    def __init__(
        self, limit: int = PER_SOURCE, window: float = SOURCE_WINDOW_SECONDS, max_sources: int = MAX_SOURCES
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_sources = max_sources
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, deque[float]] = OrderedDict()

    def hit(self, source: str, now: datetime) -> int | None:
        """Count one attempt; None when allowed, else the `Retry-After` seconds."""
        moment = now.timestamp()
        with self._lock:
            times = self._seen.pop(source, None) or deque()
            while times and times[0] <= moment - self.window:
                times.popleft()
            self._seen[source] = times
            while len(self._seen) > self.max_sources:
                self._seen.popitem(last=False)
            if len(times) >= self.limit:
                return max(1, math.ceil(times[0] + self.window - moment))
            times.append(moment)
            return None


def _limited(seconds: int) -> HubError:
    return HubError("rate_limited", headers={"Retry-After": str(seconds)})


def begin(context: HubContext, body: RecoveryBegin, source: str, limit: SourceLimit) -> dict[str, Any]:
    """`POST /v1/recovery/begin`."""
    now = context.clock()
    retry = limit.hit(source, now)
    if retry is not None:
        raise _limited(retry)
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    with transaction(context.pool) as connection:
        found = connection.execute(
            "SELECT i.user_id, b.ct FROM hub.user_identity i JOIN hub.principal p ON p.id = i.user_id"
            " AND p.disabled_at IS NULL JOIN hub.identity_bundle b ON b.user_id = i.user_id AND b.slot = 'recovery'"
            " WHERE i.recovery_id = %s FOR UPDATE OF i",
            (body.recovery_id,),
        ).fetchone()
        if found is None:
            raise HubError("not_found")
        user_id, bundle = cast(uuid.UUID, found[0]), bytes(found[1])
        since = now - timedelta(seconds=RECOVERY_ID_WINDOW_SECONDS)
        count, oldest = cast(
            tuple[int, datetime | None],
            connection.execute(
                "SELECT count(*), min(created_at) FROM hub.ceremony WHERE kind = 'recovery' AND user_id = %s"
                " AND hub_value IS NOT NULL AND created_at > %s",
                (user_id, since),
            ).fetchone(),
        )
        if count >= PER_RECOVERY_ID:
            waited = (cast(datetime, oldest) - since).total_seconds()
            raise _limited(max(1, math.ceil(waited)))
        connection.execute(
            "INSERT INTO hub.ceremony (id, kind, challenge, user_id, hub_value, created_at, expires_at)"
            " VALUES (%s, 'recovery', %s, %s, %s, %s, %s)",
            (new_id(now), challenge, user_id, challenge, now, now + timedelta(seconds=CEREMONY_SECONDS)),
        )
        _audit(connection, user_id, "begin", "ok", now)
    return {"user_id": str(user_id), "bundle_ct": b64url_encode(bundle), "challenge": b64url_encode(challenge)}


def _audit(connection: Any, user_id: uuid.UUID, step: str, result: str, now: datetime) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="anonymous",
            action="auth.recovery",
            result=result,
            target_kind="user",
            target_id=str(user_id),
            detail={"step": step},
        ),
    )


def finish(context: HubContext, body: RecoveryFinish) -> dict[str, Any]:
    """`POST /v1/recovery/finish`."""
    now = context.clock()
    user_id = uuid.UUID(body.user_id)
    challenge = decode(body.challenge)
    with transaction(context.pool) as connection:
        found = connection.execute(
            "SELECT c.id, i.sig_pub FROM hub.ceremony c JOIN hub.user_identity i ON i.user_id = c.user_id"
            " JOIN hub.principal p ON p.id = c.user_id AND p.disabled_at IS NULL"
            " WHERE c.kind = 'recovery' AND c.user_id = %s AND c.challenge = %s AND c.hub_value IS NOT NULL"
            " AND c.used_at IS NULL AND c.expires_at > %s FOR UPDATE OF c",
            (user_id, challenge, now),
        ).fetchone()
        if found is None:
            raise HubError("ceremony_invalid")
        ceremony_id, sig_pub = found
        try:
            verify_proof(bytes(sig_pub), body.user_id, challenge, body.proof)
            valid = True
        except CryptoError:
            valid = False
        connection.execute(
            "UPDATE hub.ceremony SET challenge = %s WHERE id = %s", (secrets.token_bytes(CHALLENGE_BYTES), ceremony_id)
        )
        if not valid:
            _audit(connection, user_id, "finish", "denied", now)
        exclude = [
            bytes(row[0])
            for row in connection.execute(
                "SELECT credential_id FROM hub.passkey WHERE user_id = %s AND removed_at IS NULL"
                " ORDER BY created_at, credential_id",
                (user_id,),
            )
        ]
    if not valid:
        raise HubError("signature_invalid")
    ceremony = create_ceremony(context.pool, kind="recovery", now=now, user_id=user_id)
    return {
        "ceremony_id": str(ceremony.id),
        "options": creation_options(context.config, user_id, ceremony.challenge, exclude),
    }
