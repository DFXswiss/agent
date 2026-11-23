"""Test support of the org-invitation suites: invitations created through the API with a real sealed package
(04-crypto.md section 4.6), the browser side of registration and login (`FakeBrowser` of the auth suites) and actors
that are registered users with a real browser session."""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from agent_hub.crypto import invite as invite_format
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.log.entries import entry_hash
from tests.platform.hub.auth.authenticator import Account, FakeBrowser, remember_invite
from tests.platform.hub.orgs.support import Actor, OrgDraft, World

Json = dict[str, Any]
HOUR = 3600


@dataclass
class Invitation:
    """An invitation created by an admin: its code secrets, the `invite` entry and the package text."""

    secrets: invite_format.InviteSecrets
    entry: Json
    package_ct: str
    response: httpx.Response = field(repr=False)

    @property
    def invite_id(self) -> str:
        return self.secrets.invite_id


def package_of(draft: OrgDraft, found: invite_format.InviteSecrets, wgs: list[str]) -> str:
    """A sealed package for the invitee: OK and the WK of the given workgroups, the head after the `invite`."""
    hub_key = draft.world.hub.config.signer()
    package = {
        "org": draft.id,
        "org_name": "Grüße AG",
        "wg_names": dict.fromkeys(wgs, "Default"),
        "founder": draft.founder.pub["sig"],
        "head": {"seq": len(draft.entries) - 1, "hash": entry_hash(draft.entries[-1])},
        "keys": [{"scope": "org", "scope_id": draft.id, "epoch": 1, "key": b64u_encode(draft.key("org", draft.id, 1))}]
        + [{"scope": "wg", "scope_id": wg, "epoch": 1, "key": b64u_encode(draft.key("wg", wg, 1))} for wg in wgs],
        "hub_key": {"id": hub_key.key_id, "public_key": b64u_encode(hub_key.public_key)},
        "v": 1,
    }
    return invite_format.seal_package(found, package)


def expires_in(world: World, seconds: float) -> str:
    return format_time(world.hub.clock() + timedelta(seconds=seconds))


def create_invitation(
    draft: OrgDraft,
    *,
    by: Actor | None = None,
    kind: str = "user",
    role: str = "member",
    wgs: dict[str, str] | None = None,
    seconds: float = 72 * HOUR,
    package_ct: str | None = None,
    **request: Any,
) -> Invitation:
    """Append an `invite` entry with its package through `POST /v1/orgs/{org}/invites`; on success the draft counts
    the entry as submitted, otherwise it is dropped again."""
    admin = by or draft.founder
    # users get the default workgroup implicitly (04-crypto.md 4.7), devices are assigned to it explicitly
    chosen = ({draft.default_wg: "device"} if kind == "device" else {}) if wgs is None else wgs
    found = remember_invite(
        draft.invite(admin, kind=kind, role=role, wgs=chosen, expires=expires_in(draft.world, seconds))
    )
    entry = draft.entries[-1]
    sealed = package_ct or package_of(draft, found, sorted({draft.default_wg, *chosen}))
    response = admin.browser.post(
        f"/v1/orgs/{draft.id}/invites", json={"entry": entry, "package_ct": sealed}, **request
    )
    if response.status_code == 200:
        draft.submitted = len(draft.entries)
    else:
        draft.drop_pending()
    return Invitation(found, entry, sealed, response)


def revoke_invitation(draft: OrgDraft, invitation: Invitation, *, by: Actor | None = None) -> httpx.Response:
    admin = by or draft.founder
    entry = draft.add("invite_revoke", admin, invite_id=invitation.invite_id)
    response = admin.browser.post(f"/v1/orgs/{draft.id}/invites/{invitation.invite_id}/revoke", json={"entry": entry})
    if response.status_code == 200:
        draft.submitted = len(draft.entries)
    else:
        draft.drop_pending()
    return response


def accept(draft: OrgDraft, actor: Actor, invitation: Invitation, **identity: Any) -> httpx.Response:
    """The invitee appends its `accept` through `POST /v1/orgs/{org}/log` (dropped again on a refusal)."""
    draft.accept(actor, invitation.secrets, **identity)
    response = draft.post(actor)
    if response.status_code != 200:
        draft.drop_pending()
    return response


def registered_actor(browser: FakeBrowser, invite_id: str) -> tuple[Actor, Account]:
    """Register a new user through the invitation and log in: an actor with a real browser session."""
    account = browser.register(invite_id)
    session = browser.login(account)
    actor = Actor(account.user_id, "user", account.identity, browser.client(session))
    return actor, account


def random_invite_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


def uuid_of(actor: Actor) -> uuid.UUID:
    return uuid.UUID(actor.id)
