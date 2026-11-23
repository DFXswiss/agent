"""The device resolver of the signature gate and the device rights `device:any` / `device` (03-api.md sections 1, 3).

A device signs every request with its Ed25519 identity under the key ID `device.<32 hex>` (the device UUID without
dashes). `resolve_device` finds the `hub.device` row of that ID (registered as `RESOLVERS["device."]`, ADR-027
section 4) and answers

- an unknown device: None (the gate answers `401 key_unknown`);
- a decertified or removed device, a disabled principal and an org device that is no longer an active member of its
  org (removed by a `remove` entry): `401 key_revoked`;
- otherwise the principal (kind `device`, `attributes["state"]` = `active` or `pending_certification`) with the
  verifier of its registered signing key.

The gate admits a pending device everywhere a device key is accepted; the routes ask for the right themselves:
`AnyDevice` (`device:any`, only `GET /v1/device/state`) and `ActiveDevice` (`device`, every other device route,
also those of later packages): a pending device gets `403 forbidden` there.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends

from agent_hub.auth.keys import DEVICE_PREFIX, KeyContext, KeyResolver, Principal, ResolvedKey, ed25519_verifier
from agent_hub.device import signals
from agent_hub.errors import HubError
from agent_hub.http.context import CurrentPrincipal

ACTIVE = "active"
PENDING = "pending_certification"
REVOKED_STATES = frozenset({"decertified", "removed"})


def key_id_of(device_id: UUID | str) -> str:
    """The key ID of a device: `device.` and the UUID without dashes."""
    return DEVICE_PREFIX + UUID(str(device_id)).hex


def resolve_device(context: KeyContext, key_id: str) -> ResolvedKey | None:
    """The principal and verifier of a device key (module docstring)."""
    device_id = UUID(hex=key_id[len(DEVICE_PREFIX) :])
    signals.bind(context)
    with context.pool.connection() as connection:
        row = connection.execute(
            "SELECT d.state, d.sig_pub, p.disabled_at IS NOT NULL,"
            " d.kind = 'org' AND NOT EXISTS (SELECT 1 FROM hub.org_member m WHERE m.org_id = d.owner_org"
            " AND m.principal_id = d.id AND m.removed_seq IS NULL)"
            " FROM hub.device d JOIN hub.principal p ON p.id = d.id WHERE d.id = %s",
            (device_id,),
        ).fetchone()
    if row is None:
        return None
    state, sig_pub, disabled, left_org = row
    revoked = state in REVOKED_STATES or disabled or left_org
    principal = Principal(
        kind="device",
        key_id=key_id,
        principal_id=device_id,
        actor_kind="device",
        attributes=MappingProxyType({"state": state}),
    )
    return ResolvedKey(
        principal=principal, verify=ed25519_verifier(bytes(sig_pub)), refusal="key_revoked" if revoked else None
    )


def any_device(principal: CurrentPrincipal) -> Principal:
    """`device:any`: an active or a pending device (the pipeline admits only device keys to device routes)."""
    if principal.kind != "device" or principal.attributes.get("state") not in (ACTIVE, PENDING):
        raise HubError("forbidden")
    return principal


def active_device(principal: CurrentPrincipal) -> Principal:
    """`device`: an active device; a device waiting for its certification is refused (`403 forbidden`)."""
    if principal.kind != "device" or principal.attributes.get("state") != ACTIVE:
        raise HubError("forbidden")
    return principal


def device_id(principal: Principal) -> UUID:
    """The device ID of a device principal (`active_device`/`any_device` passed)."""
    return cast(UUID, principal.principal_id)


AnyDevice = Annotated[Principal, Depends(any_device)]
ActiveDevice = Annotated[Principal, Depends(active_device)]

RESOLVERS: Mapping[str, KeyResolver] = {DEVICE_PREFIX: resolve_device}
