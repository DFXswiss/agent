"""U-HUB-passkeys (management): `POST /v1/passkeys/register/begin {add:true}` and `DELETE /v1/passkeys/{id}`
(03-api.md section 4.1, 04-crypto.md section 5.6).

A signed-in user adds a passkey (ceremony of kind `add_passkey`, finished by HUB-03's `register/finish`), logs in with
it and uploads its slot; a passkey is removed with its slot and its sessions; the last one is refused
(`last_passkey`); foreign, unknown and removed passkeys are `not_found`. Audit `auth.passkey_add|remove`.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.auth import passkeys_manage
from agent_hub.errors import HubError
from tests.platform.hub.auth.authenticator import Account, FakeBrowser, seed_invite
from tests.platform.hub.conftest import HubHarness, SignedClient

NO_CONTRACT = {"contract": False}


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


def add_passkey(browser: FakeBrowser, client: SignedClient, account: Account) -> Account:
    """04-crypto.md 5.6: begin (signed), create, finish; the account with the new credential."""
    begin = client.post("/v1/passkeys/register/begin", json={"add": True})
    assert begin.status_code == 200, begin.text
    started = begin.json()
    assert started["user_id"] == account.user_id
    credential = browser.authenticator.create(started["options"])
    finish = client.post(
        "/v1/passkeys/register/finish", json={"ceremony_id": started["ceremony_id"], "credential": credential}
    )
    assert finish.status_code == 200, finish.text
    assert finish.json() == {"user_id": account.user_id, "credential_id": credential["id"]}
    raw = b64url_decode(credential["id"]) or b""
    return Account(account.user_id, raw, account.identity, account.recovery_code, account.cred_key)


@pytest.fixture
def account(browser: FakeBrowser) -> Account:
    return browser.register(seed_invite(browser.hub))


@pytest.fixture
def client(browser: FakeBrowser, account: Account) -> SignedClient:
    return browser.client(browser.login(account))


def active(hub: HubHarness, account: Account) -> list[str]:
    found = rows(
        hub,
        "SELECT credential_id FROM hub.passkey WHERE user_id = %s AND removed_at IS NULL ORDER BY created_at",
        account.user_id,
    )
    return [b64url_encode(bytes(row[0])) for row in found]


def test_add_a_passkey_and_use_it(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    hub = browser.hub
    begin = client.post("/v1/passkeys/register/begin", json={"add": True})
    options = begin.json()["options"]
    assert [item["id"] for item in options["excludeCredentials"]] == [b64url_encode(account.credential_id)]
    assert rows(hub, "SELECT kind, user_id::text FROM hub.ceremony WHERE id = %s", begin.json()["ceremony_id"]) == [
        ("add_passkey", account.user_id)
    ]
    hub.clock.advance(1)
    second = add_passkey(browser, client, account)
    assert active(hub, account) == [b64url_encode(account.credential_id), b64url_encode(second.credential_id)]
    session = browser.login(second)
    other = browser.client(session)
    slot = "passkey:" + b64url_encode(second.credential_id)
    put = other.put(f"/v1/me/bundles/{slot}", json={"bundle_ct": browser.passkey_bundle(second)})
    assert put.status_code == 200, put.text
    me = other.get("/v1/me").json()
    assert [item["credential_id"] for item in me["passkeys"]] == active(hub, account)[::-1]
    audit = rows(hub, "SELECT actor_id::text, target_id FROM hub.audit WHERE action = 'auth.passkey_add'")
    assert audit == [(account.user_id, b64url_encode(second.credential_id))]


def test_add_ceremony_belongs_to_its_user(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    started = client.post("/v1/passkeys/register/begin", json={"add": True}).json()
    credential = browser.authenticator.create(started["options"])
    stranger = browser.hub.browser()
    response = stranger.post(
        "/v1/passkeys/register/finish", json={"ceremony_id": started["ceremony_id"], "credential": credential}
    )
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


def test_remove_a_passkey(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    hub = browser.hub
    hub.clock.advance(1)
    second = add_passkey(browser, client, account)
    other = browser.client(browser.login(second))
    put = client.put(
        f"/v1/me/bundles/passkey:{b64url_encode(account.credential_id)}",
        json={"bundle_ct": browser.passkey_bundle(account)},
    )
    assert put.status_code == 200
    hub.clock.advance(1)
    response = other.delete(f"/v1/passkeys/{b64url_encode(account.credential_id)}")
    assert (response.status_code, response.json()) == (200, {})
    assert active(hub, account) == [b64url_encode(second.credential_id)]
    assert rows(hub, "SELECT slot FROM hub.identity_bundle WHERE user_id = %s ORDER BY slot", account.user_id) == [
        ("recovery",)
    ]
    # the session certified by the removed passkey has ended
    refused = client.get("/v1/me")
    assert (refused.status_code, refused.json()) == (401, {"error": "key_revoked"})
    ended = rows(hub, "SELECT ended_at FROM hub.browser_session WHERE credential_id = %s", account.credential_id)
    assert ended == [(hub.clock(),)]
    audit = rows(
        hub,
        "SELECT actor_id::text, target_kind, target_id, detail FROM hub.audit WHERE action = %s",
        "auth.passkey_remove",
    )
    reference = b64url_encode(account.credential_id)
    assert audit == [(account.user_id, "passkey", reference, {"passkey_id": reference})]
    # the removed passkey can no longer log in, nor be removed again
    begin = browser.login_begin()
    certificate, _key, _statement = browser.certificate(b64url_decode(begin["hub_value"]) or b"", account.credential_id)
    login = hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": begin["ceremony_id"], "certificate": certificate}
    )
    assert (login.status_code, login.json()) == (400, {"error": "credential_invalid"})
    again = other.delete(f"/v1/passkeys/{b64url_encode(account.credential_id)}")
    assert (again.status_code, again.json()) == (404, {"error": "not_found"})


def test_the_last_passkey_stays(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    response = client.delete(f"/v1/passkeys/{b64url_encode(account.credential_id)}")
    assert (response.status_code, response.json()) == (403, {"error": "last_passkey"})
    assert active(browser.hub, account) == [b64url_encode(account.credential_id)]


def test_removing_the_own_session_passkey_ends_that_session(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    browser.hub.clock.advance(1)
    add_passkey(browser, client, account)
    response = client.delete(f"/v1/passkeys/{b64url_encode(account.credential_id)}")
    assert response.status_code == 200
    assert client.get("/v1/me").json() == {"error": "key_revoked"}


def test_foreign_unknown_and_malformed_passkeys_are_not_found(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    other = browser.register(seed_invite(browser.hub))
    browser.hub.clock.advance(1)
    add_passkey(browser, client, account)
    for target in (b64url_encode(other.credential_id), b64url_encode(b"\x01" * 16)):
        response = client.delete(f"/v1/passkeys/{target}")
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    response = client.delete("/v1/passkeys/AB", extensions=NO_CONTRACT)  # not canonical b64u
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert len(active(browser.hub, other)) == 1


def test_anonymous_removal_is_refused(browser: FakeBrowser, account: Account) -> None:
    response = browser.hub.anon.delete(f"/v1/passkeys/{b64url_encode(account.credential_id)}")
    assert (response.status_code, response.json()["error"]) == (401, "signature_missing")


def test_parallel_removals_keep_one_passkey(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    hub = browser.hub
    hub.clock.advance(1)
    second = add_passkey(browser, client, account)
    context = hub.app.state.hub
    principal = client.key.principal if client.key is not None else None
    assert principal is not None
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def remove(credential: bytes) -> None:
        barrier.wait()
        try:
            passkeys_manage.remove(context, principal, b64url_encode(credential))
            outcomes.append("removed")
        except HubError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=remove, args=(raw,)) for raw in (account.credential_id, second.credential_id)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["last_passkey", "removed"]
    assert len(active(hub, account)) == 1
