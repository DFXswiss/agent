"""Adding and removing passkeys of the signed-in user (03-api.md section 4.1, 04-crypto.md section 5.6).

- `POST /v1/passkeys/register/begin {add: true}` (signed): `agent_hub.auth.register.begin` hands the request to
  `begin_add`, which opens an `add_passkey` ceremony for the caller and answers `{ceremony_id, user_id, options}`
  (the creation options exclude the user's active passkeys). `register/finish` adds the passkey (HUB-03, audited
  `auth.passkey_add`); the browser then uploads the new passkey slot (`PUT /v1/me/bundles/passkey:<id>`).
- `DELETE /v1/passkeys/{credential_id}` (signed): removes an active passkey of the caller (`removed_at`), deletes its
  identity-bundle slot and ends the browser sessions certified by it; the caller's last active passkey is refused
  (`403 last_passkey`), an unknown, foreign or removed passkey is `404 not_found`. The user's active passkeys are
  locked first, so two parallel removals can never remove the last one. Audit `auth.passkey_remove` (the passkey is
  named in `target_id`, as HUB-03 does for `auth.passkey_add`).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.ceremonies import create_ceremony, creation_options, credential_reference
from agent_hub.auth.keys import Principal
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext


def _user(principal: Principal) -> uuid.UUID:
    return cast(uuid.UUID, principal.principal_id)


def active_passkeys(connection: Any, user_id: uuid.UUID, *, lock: bool = False) -> list[bytes]:
    """The credential IDs of the user's active passkeys, oldest first (locked with `lock`)."""
    suffix = " FOR UPDATE" if lock else ""
    rows = connection.execute(
        "SELECT credential_id FROM hub.passkey WHERE user_id = %s AND removed_at IS NULL"
        f" ORDER BY created_at, credential_id{suffix}",
        (user_id,),
    ).fetchall()
    return [bytes(row[0]) for row in rows]


def begin_add(context: HubContext, principal: Principal) -> dict[str, Any]:
    """`register/begin {add: true}` of a signed-in user."""
    user_id = _user(principal)
    now = context.clock()
    with context.pool.connection() as connection:
        exclude = active_passkeys(connection, user_id)
    ceremony = create_ceremony(context.pool, kind="add_passkey", now=now, user_id=user_id)
    return {
        "ceremony_id": str(ceremony.id),
        "user_id": str(user_id),
        "options": creation_options(context.config, user_id, ceremony.challenge, exclude),
    }


def remove(context: HubContext, principal: Principal, credential_text: str) -> dict[str, Any]:
    """`DELETE /v1/passkeys/{credential_id}`."""
    user_id = _user(principal)
    credential_id = b64url_decode(credential_text)
    if credential_id is None or b64url_encode(credential_id) != credential_text:
        raise HubError("not_found")
    now = context.clock()
    with transaction(context.pool) as connection:
        active = active_passkeys(connection, user_id, lock=True)
        if credential_id not in active:
            raise HubError("not_found")
        if len(active) == 1:
            raise HubError("last_passkey")
        connection.execute("UPDATE hub.passkey SET removed_at = %s WHERE credential_id = %s", (now, credential_id))
        connection.execute(
            "DELETE FROM hub.identity_bundle WHERE user_id = %s AND slot = %s",
            (user_id, "passkey:" + credential_text),
        )
        connection.execute(
            "UPDATE hub.browser_session SET ended_at = %s WHERE credential_id = %s AND ended_at IS NULL",
            (now, credential_id),
        )
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=user_id,
                action="auth.passkey_remove",
                result="ok",
                target_kind="passkey",
                target_id=credential_reference(credential_id),
                detail={"passkey_id": credential_reference(credential_id)},
            ),
        )
    return {}
