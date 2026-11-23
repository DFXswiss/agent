"""KOR-01 / AUD-01 A-02: an open invitation can only be registered through by the holder of its code (ADR-043).

The `invite_id` of a platform user invitation stands in the platform log, which every signed-in user may read
(`GET /v1/platform/log`, right `user`); an org invitation's stands in the membership log, inviters see the IDs of all
platform invitations. Before ADR-043 `register/begin|finish` was bound to the `invite_id` alone, so any of those
readers could take an open invitation (and with a platform invitation found a private org). Now `register/finish`
needs `inv_sig`, the signature of `sk_inv` of the code over the ceremony challenge.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.crypto import invite as invite_codes
from agent_hub.platform import log as platform_log
from tests.platform.hub.auth.authenticator import INVITES, FakeBrowser, new_invite, seed_invite
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.platform.conftest import Platform


@pytest.fixture(autouse=True)
def fresh_platform_cache() -> Any:
    platform_log.CACHE.clear()
    yield
    platform_log.CACHE.clear()


def registered_user(hub: HubHarness, invite_id: str) -> str | None:
    with hub.pool.connection() as connection:
        row = connection.execute("SELECT registered_user FROM hub.invite WHERE id = %s", (invite_id,)).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


def foreign_finish(browser: FakeBrowser, invite_id: str, **body: Any) -> Any:
    """`register/begin` + `register/finish` by someone who only knows the `invite_id`."""
    started = browser.register_begin(invite_id)
    assert started.status_code == 200, started.text
    begun = started.json()
    credential = browser.authenticator.create(begun["options"])
    finish, *_ = browser.registration_body(begun, credential, invite=new_invite())
    finish.update(body)
    complete = all(value is not None for value in finish.values())
    finish = {key: value for key, value in finish.items() if value is not None}
    request: dict[str, Any] = {} if complete else {"extensions": {"contract": False}}
    return browser.hub.anon.post("/v1/passkeys/register/finish", json=finish, **request)


def test_a_member_who_reads_the_platform_log_cannot_take_an_open_platform_invitation(hub: HubHarness) -> None:
    plat = Platform(hub)
    invite_id = plat.invite_user()
    # Mallory is a signed-in user: she reads the platform log and finds the open invitation
    mallory = FakeBrowser(hub)
    session = mallory.login(mallory.register(seed_invite(hub)))
    page = mallory.client(session).get("/v1/platform/log")
    assert page.status_code == 200, page.text
    [found] = [
        item["entry"]["body"] for item in page.json()["entries"] if item["entry"]["body"]["type"] == "user_invite"
    ]
    assert found["invite_id"] == invite_id
    assert found["pk_inv"] == b64url_encode(b64url_decode(found["pk_inv"]) or b"")
    # without the proof, and with a proof of another code, the registration is refused
    without = foreign_finish(mallory, invite_id, inv_sig=None)
    assert (without.status_code, without.json()["error"]) == (400, "request_invalid")
    other = foreign_finish(mallory, invite_id)
    assert (other.status_code, other.json()) == (401, {"error": "signature_invalid"})
    assert registered_user(hub, invite_id) is None
    # the invitee, who holds the code, still registers
    account = FakeBrowser(hub).register(invite_id)
    assert registered_user(hub, invite_id) == account.user_id


def test_an_inviter_cannot_take_another_inviters_platform_invitation(hub: HubHarness) -> None:
    """Inviters list every platform invitation with its ID; the key comes from the creating inviter's request."""
    plat = Platform(hub)
    inviter = FakeBrowser(hub)
    account = inviter.register(seed_invite(hub))
    plat.append("role_grant", subject=account.user_id, role="inviter")
    client = inviter.client(inviter.login(account))
    code = new_invite()
    created = client.post(
        "/v1/platform/invites",
        json={"invite_id": code.invite_id, "pk_inv": code.pk_inv, "kind": "user", "expires_at": plat.expires(24)},
    )
    assert created.status_code == 200, created.text
    listed = client.get("/v1/platform/invites").json()["invites"]
    assert code.invite_id in [row["invite_id"] for row in listed]
    other = foreign_finish(FakeBrowser(hub), code.invite_id)
    assert (other.status_code, other.json()) == (401, {"error": "signature_invalid"})
    assert registered_user(hub, code.invite_id) is None
    registered = FakeBrowser(hub).register(code.invite_id)
    assert registered_user(hub, code.invite_id) == registered.user_id


def test_a_refused_proof_consumes_the_ceremony(browser: FakeBrowser) -> None:
    invite_id = seed_invite(browser.hub)
    started = browser.register_begin(invite_id).json()
    credential = browser.authenticator.create(started["options"])
    body, *_ = browser.registration_body(started, credential, invite=new_invite())
    refused = browser.hub.anon.post("/v1/passkeys/register/finish", json=body)
    assert (refused.status_code, refused.json()) == (401, {"error": "signature_invalid"})
    right, *_ = browser.registration_body(started, credential)
    again = browser.hub.anon.post("/v1/passkeys/register/finish", json=right)
    assert (again.status_code, again.json()) == (400, {"error": "ceremony_invalid"})
    assert registered_user(browser.hub, invite_id) is None


def test_the_proof_binds_the_user_id_of_the_ceremony(browser: FakeBrowser) -> None:
    invite_id = seed_invite(browser.hub)
    started = browser.register_begin(invite_id).json()
    credential = browser.authenticator.create(started["options"])
    body, *_ = browser.registration_body(started, credential)
    challenge = b64url_decode(started["options"]["challenge"]) or b""
    body["inv_sig"] = invite_codes.sign_register(INVITES[invite_id], str(uuid.uuid4()), challenge)
    refused = browser.hub.anon.post("/v1/passkeys/register/finish", json=body)
    assert (refused.status_code, refused.json()) == (401, {"error": "signature_invalid"})


def test_a_platform_invitation_without_a_key_cannot_be_registered_through(browser: FakeBrowser) -> None:
    """A row from before ADR-043 (no `pk_inv`) is closed: `invite_unknown` at begin, like any invitation that is not
    open, and at finish if the key is gone since the begin."""
    keyless = seed_invite(browser.hub, b64url_encode(bytes(range(16))))
    refused = browser.register_begin(keyless)
    assert (refused.status_code, refused.json()) == (404, {"error": "invite_unknown"})
    invite_id = seed_invite(browser.hub)
    started = browser.register_begin(invite_id).json()
    with browser.hub.pool.connection() as connection:
        connection.execute("UPDATE hub.invite SET pk_inv = NULL WHERE id = %s", (invite_id,))
    credential = browser.authenticator.create(started["options"])
    body, *_ = browser.registration_body(started, credential)
    late = browser.hub.anon.post("/v1/passkeys/register/finish", json=body)
    assert (late.status_code, late.json()) == (404, {"error": "invite_unknown"})
