"""Key resolver of the platform root (03-api.md section 1: root-signed operator requests are verified against
`AGENT_HUB_PLATFORM_ROOT_KEY`); discovered as `config_api/keys.py:RESOLVERS`."""

from __future__ import annotations

from collections.abc import Mapping

from agent_hub.auth.keys import ROOT_KEY_ID, KeyContext, KeyResolver, Principal, ResolvedKey, ed25519_verifier


def resolve_platform_root(context: KeyContext, key_id: str) -> ResolvedKey | None:
    """The platform root key of the configuration (`AGENT_HUB_PLATFORM_ROOT_KEY`, operator CLI requests)."""
    if key_id != ROOT_KEY_ID:
        return None
    principal = Principal(kind="root", key_id=ROOT_KEY_ID, principal_id=None, actor_kind="operator")
    return ResolvedKey(principal=principal, verify=ed25519_verifier(context.config.platform_root_key))


RESOLVERS: Mapping[str, KeyResolver] = {ROOT_KEY_ID: resolve_platform_root}
