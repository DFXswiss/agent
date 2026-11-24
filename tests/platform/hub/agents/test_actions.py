"""U-HUB-agents (an agent acts with its user's rights, 11-agent-access.md sections 6 and 8, ADR-056 sections 4 and 5):
through the real pipeline an active agent reads and changes its user's account, credentials of every kind, orgs,
invitations, tasks and device requests - never more than its user - and every account-changing action of an agent
notifies the user (`agent.account_change`, `ref` = `<action>:<agent>:<target>`), published after the commit."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import replace

from agent_core.signing import b64url_encode
from agent_hub.auth.ceremonies import credential_reference
from agent_hub.crypto.bundle import RECOVERY_SLOT, passkey_slot, seal_bundle
from agent_hub.crypto.kdf import kek_from_recovery_code
from agent_hub.crypto.recovery import generate_recovery_code, recovery_id
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.agents.support import TestAgent, agent_row, rows, stored_agent
from tests.platform.hub.auth.authenticator import Account, FakeBrowser, new_invite, seed_invite
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.credentials.support import Desk as CredentialDesk
from tests.platform.hub.invites.support import create_invitation
from tests.platform.hub.orgs.support import Actor, World
from tests.platform.hub.platform.conftest import Platform
from tests.platform.hub.tasks.support import Desk as TaskDesk

KINDS = ("anthropic_api_key", "claude_subscription", "github", "mailbox")


def account_agent(browser: FakeBrowser) -> tuple[Account, TestAgent]:
    account = browser.register(seed_invite(browser.hub))
    return account, stored_agent(browser.hub, uuid.UUID(account.user_id))


def notices(hub: HubHarness, user: uuid.UUID | str) -> list[str]:
    found = rows(
        hub, "SELECT ref FROM hub.notification WHERE recipient_id = %s AND org_id IS NULL ORDER BY id", str(user)
    )
    return sorted(ref for (ref,) in found)


def live(published: MemoryPublisher) -> list[str]:
    return [item.data["ref"] for item in published.items() if item.frame == "notification"]


def as_agent(actor: Actor, agent: TestAgent, hub: HubHarness) -> Actor:
    """The actor whose requests are signed by its agent."""
    return replace(actor, client=agent.client(hub))


# --- the account ------------------------------------------------------------------------------------------------------


def test_the_agent_reads_and_changes_the_account_like_its_user(browser: FakeBrowser) -> None:
    hub = browser.hub
    account, agent = account_agent(browser)
    session = browser.client(browser.login(account))
    client = agent.client(hub)
    assert client.get("/v1/me").json() == session.get("/v1/me").json()
    settings = {"model_access": "auto", "model_id": None, "locale": "de"}
    stored = {**settings, "fallback": {"own_to_dfx": False, "operator_to_platform": True}}
    assert client.put("/v1/me/settings", json=settings).json() == stored
    assert session.get("/v1/me").json()["settings"] == stored
    # settings are no account change: nothing is notified
    assert notices(hub, account.user_id) == []


def test_the_agent_removes_a_passkey_and_the_user_is_notified(browser: FakeBrowser, published: MemoryPublisher) -> None:
    hub = browser.hub
    account, agent = account_agent(browser)
    second = secrets.token_bytes(16)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.passkey (credential_id, user_id, public_key, created_at, last_used_at)"
            " SELECT %s, user_id, public_key, created_at, last_used_at FROM hub.passkey WHERE credential_id = %s",
            (second, account.credential_id),
        )
    client = agent.client(hub)
    response = client.delete(f"/v1/passkeys/{b64url_encode(second)}")
    assert (response.status_code, response.json()) == (200, {})
    ref = f"passkey_removed:{agent.agent_id}:{credential_reference(second)}"
    assert notices(hub, account.user_id) == [ref]
    assert live(published) == [ref]
    assert rows(hub, "SELECT agent_id FROM hub.audit WHERE action = 'auth.passkey_remove'") == [(agent.agent_id,)]
    # the user's last passkey stays, for an agent as for a browser
    last = client.delete(f"/v1/passkeys/{b64url_encode(account.credential_id)}")
    assert (last.status_code, last.json()) == (403, {"error": "last_passkey"})
    # a browser session removing a passkey notifies nobody
    browser_removal = browser.client(browser.login(account))
    third = secrets.token_bytes(16)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.passkey (credential_id, user_id, public_key, created_at, last_used_at)"
            " SELECT %s, user_id, public_key, created_at, last_used_at FROM hub.passkey WHERE credential_id = %s",
            (third, account.credential_id),
        )
    assert browser_removal.delete(f"/v1/passkeys/{b64url_encode(third)}").status_code == 200
    assert notices(hub, account.user_id) == [ref]


def test_the_agent_replaces_the_recovery_code_with_its_signed_request(
    browser: FakeBrowser, published: MemoryPublisher
) -> None:
    hub = browser.hub
    account, agent = account_agent(browser)
    code = generate_recovery_code()
    bundle = seal_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, account.identity)
    body = {"bundle_ct": bundle, "new_recovery_id": recovery_id(code)}
    response = agent.client(hub).put("/v1/me/bundles/recovery", json=body)
    assert (response.status_code, response.json()) == (200, {})
    assert rows(hub, "SELECT recovery_id FROM hub.user_identity") == [(recovery_id(code),)]
    audit = rows(hub, "SELECT target_kind, target_id, agent_id FROM hub.audit WHERE action = 'auth.recovery'")
    assert audit == [("agent", str(agent.agent_id), agent.agent_id)]
    ref = f"recovery_replaced:{agent.agent_id}:"
    assert notices(hub, account.user_id) == [ref]
    assert live(published) == [ref]
    # a browser session still needs a passkey assertion or a finished recovery
    session = browser.client(browser.login(account))
    refused = session.put("/v1/me/bundles/recovery", json=body)
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})


def test_overwriting_a_passkey_slot_notifies_writing_it_first_does_not(browser: FakeBrowser) -> None:
    hub = browser.hub
    account, agent = account_agent(browser)
    with hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.identity_bundle WHERE slot LIKE 'passkey:%%'")
    slot = passkey_slot(account.credential_id)
    url = f"/v1/me/bundles/{slot}"
    client = agent.client(hub)
    assert client.put(url, json={"bundle_ct": browser.passkey_bundle(account)}).status_code == 200
    assert notices(hub, account.user_id) == []
    assert client.put(url, json={"bundle_ct": browser.passkey_bundle(account)}).status_code == 200
    assert notices(hub, account.user_id) == [
        f"bundle_replaced:{agent.agent_id}:{credential_reference(account.credential_id)}"
    ]


def test_the_agents_session_end_disconnects_it(browser: FakeBrowser, published: MemoryPublisher) -> None:
    hub = browser.hub
    account, agent = account_agent(browser)
    client = agent.client(hub)
    response = client.post("/v1/session/end", json={})
    assert (response.status_code, response.json()) == (200, {})
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["bundle_ct"]) == ("revoked", None)
    assert client.get("/v1/me").json() == {"error": "key_revoked"}
    assert notices(hub, account.user_id) == [f"agent_revoked:{agent.agent_id}:{agent.agent_id}"]
    assert [item.frame for item in published.items()] == ["notification", "close_keys"]
    # a browser session's end still works as before and ends its streams
    published.clear()
    session = browser.login(account)
    assert browser.client(session).post("/v1/session/end", json={}).status_code == 200
    assert [(item.frame, item.data) for item in published.items()] == [("close_keys", {"key_ids": [session.key_id]})]


def test_marking_notifications_read_by_an_agent_is_audited(browser: FakeBrowser) -> None:
    hub = browser.hub
    account, agent = account_agent(browser)
    user = uuid.UUID(account.user_id)
    org = uuid7(hub.clock())
    with hub.pool.connection() as connection:
        for org_id, kind in ((org, "task.idle"), (None, "agent.account_change")):
            connection.execute(
                "INSERT INTO hub.notification (id, recipient_id, kind, org_id, ref, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (uuid7(hub.clock()), user, kind, org_id, None if org_id else "agent_revoked::", hub.clock()),
            )
    session = browser.client(browser.login(account))
    assert session.get("/v1/me").json()["unread_notifications"] == 1
    # every marking is audited (ADR-063); `all` without `account` leaves the account notification unread
    assert session.post("/v1/notifications/read", json={"all": True}).status_code == 200
    unread = rows(hub, "SELECT kind FROM hub.notification WHERE read_at IS NULL")
    assert unread == [("agent.account_change",)]
    client = agent.client(hub)
    assert client.post("/v1/notifications/read", json={"all": True, "account": True}).json() == {}
    assert rows(hub, "SELECT count(*) FROM hub.notification WHERE read_at IS NULL") == [(0,)]
    (found,) = rows(hub, "SELECT id FROM hub.notification WHERE org_id IS NULL")
    assert client.post("/v1/notifications/read", json={"ids": [str(found[0])]}).json() == {}
    assert client.post("/v1/notifications/read", json={"all": True}).json() == {}
    audit = rows(
        hub, "SELECT actor_id, detail, agent_id FROM hub.audit WHERE action = 'notification.read' ORDER BY seq"
    )
    assert audit == [
        (user, {"marked": 1, "scope": "all"}, None),
        (user, {"marked": 1, "scope": "all_account"}, agent.agent_id),
        (user, {"marked": 0, "scope": "ids"}, agent.agent_id),
        (user, {"marked": 0, "scope": "all"}, agent.agent_id),
    ]
    listed = session.get("/v1/notifications", params={"account": "1"}).json()["notifications"]
    assert sorted(entry["kind"] for entry in listed) == ["agent.account_change", "task.idle"]
    refused = session.get("/v1/notifications", params={"account": "yes"}, extensions={"contract": False})
    assert (refused.status_code, refused.json()["error"]) == (400, "request_invalid")


# --- credentials and device requests ----------------------------------------------------------------------------------


def test_the_agent_writes_credentials_of_every_kind_and_device_requests(
    hub: HubHarness, published: MemoryPublisher
) -> None:
    desk = CredentialDesk.build(World(hub))
    published.clear()
    writer = desk.scene.writer
    agent = stored_agent(hub, uuid.UUID(writer.id))
    acting = as_agent(writer, agent, hub)
    for kind in KINDS:
        response = desk.put(acting, desk.record(writer, kind), 0)
        assert (response.status_code, response.json()) == (200, {"version": 1}), kind
    assert sorted(kind for (kind,) in rows(hub, "SELECT kind FROM hub.credential WHERE owner_id = %s", writer.id)) == [
        *KINDS
    ]
    assert acting.browser.get("/v1/me/credentials/github").status_code == 200
    assert acting.browser.delete("/v1/me/credentials/mailbox").json() == {}
    expected = [f"credential_changed:{agent.agent_id}:{kind}" for kind in (*KINDS, "mailbox")]
    assert notices(hub, writer.id) == sorted(expected)
    assert live(published) == expected
    audit = rows(hub, "SELECT DISTINCT agent_id FROM hub.audit WHERE action LIKE 'credential.%%'")
    assert audit == [(agent.agent_id,)]
    # another user's record is refused like the user's own browser would be refused
    stranger = desk.record(desk.scene.stranger, "github")
    refused = desk.put(acting, stranger, 0)
    assert refused.status_code == desk.put(writer, stranger, 0).status_code
    assert refused.status_code >= 400
    # a device request
    envelope = desk.request(writer, desk.scene.device)
    response = acting.browser.post("/v1/device-requests", json={"envelope": envelope})
    assert response.status_code == 200, response.text
    assert f"device_request:{agent.agent_id}:{desk.scene.device.id}" in notices(hub, writer.id)
    again = acting.browser.post("/v1/device-requests", json={"envelope": envelope})
    assert again.json()["replayed"] is True
    assert len(notices(hub, writer.id)) == len(expected) + 1


# --- orgs, invitations, tasks ----------------------------------------------------------------------------------------


def test_the_agent_creates_an_org_changes_roles_and_invites_an_admin(
    hub: HubHarness, published: MemoryPublisher
) -> None:
    world = World(hub)
    founder = world.user()
    agent = stored_agent(hub, uuid.UUID(founder.id))
    acting = as_agent(founder, agent, hub)
    draft = world.org(acting)
    response = draft.create()
    assert response.status_code == 200, response.text
    member = world.user()
    world.join(draft, member)
    published.clear()
    draft.add("role", acting, subject=member.id, role="admin")
    response = draft.post(acting)
    assert response.status_code == 200, response.text
    role = f"role_changed:{agent.agent_id}:{draft.id}"
    assert notices(hub, founder.id) == [role]
    assert live(published) == [role]
    # an org invitation with role admin, and one with role member (no notification)
    invitation = create_invitation(draft, by=acting, role="admin")
    assert invitation.response.status_code == 200, invitation.response.text
    assert create_invitation(draft, by=acting).response.status_code == 200
    assert notices(hub, founder.id) == sorted([role, f"invite_created:{agent.agent_id}:{draft.id}"])
    # the agent of a member who is not an admin is refused like the member's browser
    member_agent = as_agent(member, stored_agent(hub, uuid.UUID(member.id)), hub)
    stranger = world.user()
    stranger_agent = as_agent(stranger, stored_agent(hub, uuid.UUID(stranger.id)), hub)
    for path in (f"/v1/orgs/{draft.id}", f"/v1/orgs/{draft.id}/audit", f"/v1/orgs/{draft.id}/invites"):
        for actor, agent_actor in ((member, member_agent), (stranger, stranger_agent)):
            expected = actor.browser.get(path)
            found = agent_actor.browser.get(path)
            assert (found.status_code, found.json().get("error")) == (
                expected.status_code,
                expected.json().get("error"),
            )
    assert stranger_agent.browser.get(f"/v1/orgs/{draft.id}").status_code == 404


def test_the_agent_creates_platform_invitations_as_an_inviter(hub: HubHarness) -> None:
    platform = Platform(hub)
    agent = stored_agent(hub)
    user = agent.user_id
    assert user is not None
    client = agent.client(hub)
    found = new_invite()
    body = {
        "invite_id": found.invite_id,
        "pk_inv": found.pk_inv,
        "kind": "user",
        "expires_at": "2026-11-22T12:00:00.000Z",
    }
    refused = client.post("/v1/platform/invites", json=body)
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    platform.append("role_grant", subject=str(user), role="inviter")
    assert client.post("/v1/platform/invites", json=body).json() == {}
    assert client.post("/v1/platform/invites", json=body).json() == {}  # the repeat notifies nobody
    assert notices(hub, user) == [f"invite_created:{agent.agent_id}:platform"]


def test_the_agent_creates_tasks_and_prompts(hub: HubHarness, published: MemoryPublisher) -> None:
    desk = TaskDesk.build(World(hub))
    desk.online(desk.scene.device)
    writer, reader = desk.scene.writer, desk.scene.reader
    acting = as_agent(writer, stored_agent(hub, uuid.UUID(writer.id)), hub)
    response = desk.create(acting)
    assert response.status_code == 200, response.text
    task = response.json()["task"]["task_id"]
    assert desk.prompt(acting, task).status_code == 200
    # the reader's agent cannot write, like the reader
    reading = as_agent(reader, stored_agent(hub, uuid.UUID(reader.id)), hub)
    expected = desk.create(reader)
    found = desk.create(reading)
    assert (found.status_code, found.json()) == (expected.status_code, expected.json())
    assert found.status_code == 403
