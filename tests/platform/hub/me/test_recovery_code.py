"""KOR-01 / AUD-01 B-06: a signed-in user replaces the recovery code at any time with a fresh passkey assertion
(06-web.md section 3.2 "generate a new recovery code"; `POST /v1/me/recovery/begin`, `PUT /v1/me/bundles/recovery`
with `assertion`). The old code no longer finds the account, the new one recovers it; a session alone, a foreign or
used ceremony, a foreign or removed passkey and a broken assertion are refused; the hub never sees the code.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.auth.ceremonies import creation_options
from agent_hub.crypto.bundle import RECOVERY_SLOT, open_bundle, seal_bundle
from agent_hub.crypto.kdf import kek_from_recovery_code
from agent_hub.crypto.recovery import format_recovery_code, generate_recovery_code, recovery_id
from tests.platform.hub.auth.authenticator import FLAG_UP, Account, FakeBrowser, seed_invite
from tests.platform.hub.auth.test_passkeys_manage import add_passkey
from tests.platform.hub.conftest import HubHarness, SignedClient

NO_CONTRACT = {"contract": False}


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


@pytest.fixture
def account(browser: FakeBrowser) -> Account:
    return browser.register(seed_invite(browser.hub))


@pytest.fixture
def client(browser: FakeBrowser, account: Account) -> SignedClient:
    return browser.client(browser.login(account))


def begin(client: SignedClient) -> dict[str, Any]:
    response = client.post("/v1/me/recovery/begin", json={})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def assertion(browser: FakeBrowser, credential_id: bytes, started: dict[str, Any], **get: Any) -> dict[str, str]:
    """`get()` over the ceremony challenge with the passkey `credential_id`."""
    challenge = b64url_decode(started["challenge"]) or b""
    signed = browser.authenticator.get(credential_id, challenge, **get)
    return {
        "ceremony_id": started["ceremony_id"],
        "credential_id": b64url_encode(credential_id),
        "authenticator_data": b64url_encode(signed["authenticator_data"]),
        "client_data_json": b64url_encode(signed["client_data_json"]),
        "signature": b64url_encode(signed["signature"]),
    }


def new_slot(account: Account) -> tuple[bytes, dict[str, str]]:
    code = generate_recovery_code()
    bundle = seal_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, account.identity)
    return code, {"bundle_ct": bundle, "new_recovery_id": recovery_id(code)}


def put(client: SignedClient, body: dict[str, Any], **request: Any) -> Any:
    return client.put("/v1/me/bundles/recovery", json=body, **request)


def recovery_begin(hub: HubHarness, code: bytes) -> Any:
    return hub.anon.post("/v1/recovery/begin", json={"recovery_id": recovery_id(code)})


def test_a_new_recovery_code_replaces_the_old_one(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    hub = browser.hub
    started = begin(client)
    options = started["options"]
    assert (options["allowCredentials"], options["userVerification"], options["timeout"]) == ([], "required", 300000)
    assert options["rpId"] == hub.config.public.rp_id
    [(kind, user, challenge)] = rows(
        hub, "SELECT kind, user_id, challenge FROM hub.ceremony WHERE id = %s", uuid.UUID(started["ceremony_id"])
    )
    assert (kind, str(user), bytes(challenge)) == (
        "recovery_rotate",
        account.user_id,
        b64url_decode(started["challenge"]),
    )
    code, body = new_slot(account)
    response = put(client, {**body, "assertion": assertion(browser, account.credential_id, started)})
    assert (response.status_code, response.json()) == (200, {})
    # the old code no longer finds the account; the new code recovers it
    assert recovery_begin(hub, account.recovery_code).status_code == 404
    found = recovery_begin(hub, code)
    assert found.status_code == 200, found.text
    opened = open_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, found.json()["bundle_ct"])
    assert opened.sig_public() == account.identity.sig_public()
    # the counter of the assertion is kept; the replacement is audited; the code never reached the hub
    [(count, used)] = rows(
        hub,
        "SELECT p.sign_count, c.used_at FROM hub.passkey p, hub.ceremony c WHERE c.id = %s",
        uuid.UUID(started["ceremony_id"]),
    )
    assert (count, used is not None) == (browser.authenticator.credentials[account.credential_id].counter, True)
    audit = rows(
        hub,
        "SELECT actor_kind, actor_id, target_kind, detail FROM hub.audit WHERE action = 'auth.recovery'"
        " AND actor_kind = 'user'",
    )
    assert audit == [("user", uuid.UUID(account.user_id), "passkey", {"step": "rotate"})]
    text = format_recovery_code(code)
    dump = str(rows(hub, "SELECT * FROM hub.identity_bundle")) + str(rows(hub, "SELECT * FROM hub.user_identity"))
    assert text not in dump
    assert code.hex() not in dump


def test_the_assertion_is_used_once(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    started = begin(client)
    signed = assertion(browser, account.credential_id, started)
    _code, body = new_slot(account)
    assert put(client, {**body, "assertion": signed}).status_code == 200
    _other, again = new_slot(account)
    response = put(client, {**again, "assertion": signed})
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})


def test_a_session_alone_cannot_replace_the_code(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    _code, body = new_slot(account)
    response = put(client, body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert rows(browser.hub, "SELECT recovery_id FROM hub.user_identity") == [(recovery_id(account.recovery_code),)]


def test_the_ceremony_of_another_user_or_kind_is_refused(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    hub = browser.hub
    other_browser = FakeBrowser(hub, browser.authenticator)
    other = other_browser.register(seed_invite(hub))
    foreign = begin(other_browser.client(other_browser.login(other)))
    _code, body = new_slot(account)
    response = put(client, {**body, "assertion": assertion(browser, account.credential_id, foreign)})
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})
    # the foreign ceremony stays open for its owner
    assert rows(hub, "SELECT used_at FROM hub.ceremony WHERE id = %s", uuid.UUID(foreign["ceremony_id"])) == [(None,)]
    login = hub.anon.post("/v1/passkeys/login/begin", json={}).json()
    as_login = {"ceremony_id": login["ceremony_id"], "challenge": login["hub_value"]}
    response = put(client, {**body, "assertion": assertion(browser, account.credential_id, as_login)})
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})


def test_a_foreign_or_removed_passkey_is_refused(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    hub = browser.hub
    other = FakeBrowser(hub, browser.authenticator).register(seed_invite(hub))
    _code, body = new_slot(account)
    response = put(client, {**body, "assertion": assertion(browser, other.credential_id, begin(client))})
    assert (response.status_code, response.json()) == (400, {"error": "credential_invalid"})
    # a second passkey of the user, removed meanwhile (the session of the first one stays valid)
    second = add_passkey(browser, client, account)
    assert client.delete("/v1/passkeys/" + b64url_encode(second.credential_id)).status_code == 200
    response = put(client, {**body, "assertion": assertion(browser, second.credential_id, begin(client))})
    assert (response.status_code, response.json()) == (400, {"error": "credential_invalid"})


@pytest.mark.parametrize(
    ("bend", "code"),
    [
        ({"corrupt": True}, "signature_invalid"),
        ({"flags": FLAG_UP}, "user_verification_missing"),
        ({"kind": "webauthn.create"}, "client_data_invalid"),
        ({"origin": "https://evil.example"}, "origin_mismatch"),
        ({"rp_id": "evil.example"}, "rp_id_mismatch"),
        ({"counter": 0}, "counter_regressed"),
    ],
    ids=["signature", "no-uv", "type", "origin", "rp-id", "counter"],
)
def test_a_broken_assertion_is_refused(
    browser: FakeBrowser, account: Account, client: SignedClient, bend: dict[str, Any], code: str
) -> None:
    _new, body = new_slot(account)
    response = put(client, {**body, "assertion": assertion(browser, account.credential_id, begin(client), **bend)})
    assert response.json() == {"error": code}
    assert rows(browser.hub, "SELECT recovery_id FROM hub.user_identity") == [(recovery_id(account.recovery_code),)]


def test_an_assertion_over_another_challenge_is_refused(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    started = begin(client)
    other = {**started, "challenge": b64url_encode(bytes(32))}
    _code, body = new_slot(account)
    signed = assertion(browser, account.credential_id, other)
    response = put(client, {**body, "assertion": signed})
    assert response.json() == {"error": "challenge_mismatch"}


def test_a_passkey_slot_takes_no_assertion(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    slot = "passkey:" + b64url_encode(account.credential_id)
    body = {
        "bundle_ct": browser.passkey_bundle(account),
        "assertion": assertion(browser, account.credential_id, begin(client)),
    }
    response = client.put(f"/v1/me/bundles/{slot}", json=body, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (400, {"error": "request_invalid"})


def test_a_recovery_id_of_another_user_is_a_conflict(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    other = FakeBrowser(browser.hub, browser.authenticator).register(seed_invite(browser.hub))
    body = {"bundle_ct": b64url_encode(bytes(40)), "new_recovery_id": recovery_id(other.recovery_code)}
    response = put(client, {**body, "assertion": assertion(browser, account.credential_id, begin(client))})
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})


def test_begin_needs_a_session(browser: FakeBrowser) -> None:
    response = browser.hub.anon.post("/v1/me/recovery/begin", json={})
    assert response.status_code == 401


def test_an_expired_ceremony_is_refused(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    started = begin(client)
    browser.hub.clock.advance(301)
    _code, body = new_slot(account)
    response = put(client, {**body, "assertion": assertion(browser, account.credential_id, started)})
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})


def test_the_rotate_ceremony_serves_no_other_purpose(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    """Neither a sign-in nor a registration can finish a `recovery_rotate` ceremony (separate ceremonies, KOR-01)."""
    hub = browser.hub
    started = begin(client)
    challenge = b64url_decode(started["challenge"]) or b""
    certificate, _key, _statement = browser.certificate(challenge, account.credential_id)
    login = hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": started["ceremony_id"], "certificate": certificate}
    )
    assert (login.status_code, login.json()) == (400, {"error": "ceremony_invalid"})
    options = creation_options(hub.config, uuid.UUID(account.user_id), challenge)
    register = client.post(
        "/v1/passkeys/register/finish",
        json={"ceremony_id": started["ceremony_id"], "credential": browser.authenticator.create(options)},
    )
    assert (register.status_code, register.json()) == (400, {"error": "ceremony_invalid"})
    # still open for its purpose
    _code, body = new_slot(account)
    assert put(client, {**body, "assertion": assertion(browser, account.credential_id, started)}).status_code == 200
