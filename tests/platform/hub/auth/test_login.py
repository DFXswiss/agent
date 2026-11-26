"""U-HUB-passkeys (login) and U-HUB-auth (sessions): `POST /v1/passkeys/login/begin|finish`, the session-key
resolver of the signature gate, `POST /v1/session/end` and `POST /v1/session/renew` (03-api.md sections 1, 4.1, 12;
04-crypto.md sections 4.9, 5.4; ADR-099).

The certificate is verified against the stored passkey (V1-V11), bound to the ceremony's hub value, the counter
must not regress, the session lasts at most 43080 s, and the session key then signs every request: expired →
`session_expired`, ended or passkey removed or user disabled → `key_revoked`. A still valid session renews itself to
30 days from the hub's clock (rule E-40), the same key and row.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

import pytest

from agent_core.passkey.certificate import statement_challenge
from agent_core.passkey.encoding import session_key_id
from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.auth.keys import RESOLVERS, SESSION_PREFIX, Principal, resolve_session
from agent_hub.auth.sessions import SESSION_RENEW_SECONDS, renew_session
from agent_hub.crypto.bundle import open_bundle, passkey_slot
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.kdf import kek_from_prf, prf_input
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from tests.platform.hub.auth.authenticator import (
    FLAG_UP,
    FLAG_UV,
    SESSION_ISSUE_SECONDS,
    Account,
    FakeAuthenticator,
    FakeBrowser,
    seed_invite,
)
from tests.platform.hub.conftest import HubHarness

NO_CONTRACT = {"contract": False}


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


@pytest.fixture
def account(browser: FakeBrowser) -> Account:
    return browser.register(seed_invite(browser.hub))


def attempt(browser: FakeBrowser, account: Account, **options: Any) -> Any:
    """One login with a fresh ceremony; `options` bend the certificate."""
    begin = browser.login_begin()
    hub_value = b64url_decode(begin["hub_value"]) or b""
    certificate, _, _ = browser.certificate(hub_value, account.credential_id, **options)
    return browser.hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": begin["ceremony_id"], "certificate": certificate}
    )


def refusals(hub: HubHarness) -> list[tuple[Any, ...]]:
    return rows(hub, "SELECT result, actor_kind, detail FROM hub.audit WHERE action = 'auth.refused' ORDER BY seq")


# --- begin ------------------------------------------------------------------------------------------------------------


def test_begin_returns_hub_value_and_request_options(browser: FakeBrowser) -> None:
    begin = browser.login_begin()
    assert begin["options"] == {
        "rpId": "localhost",
        "allowCredentials": [],
        "userVerification": "required",
        "extensions": {"prf": {"eval": {"first": b64url_encode(prf_input())}}},
        "timeout": 300000,
    }
    [(kind, challenge, hub_value, user, invite)] = rows(
        browser.hub,
        "SELECT kind, challenge, hub_value, user_id, invite_id FROM hub.ceremony WHERE id = %s",
        uuid.UUID(begin["ceremony_id"]),
    )
    assert (kind, user, invite) == ("login", None, None)
    assert bytes(hub_value) == bytes(challenge) == b64url_decode(begin["hub_value"])


def test_begin_refuses_a_non_empty_body(browser: FakeBrowser) -> None:
    response = browser.hub.anon.post("/v1/passkeys/login/begin", json={"user": "x"}, extensions=NO_CONTRACT)
    assert response.status_code == 400


# --- finish -----------------------------------------------------------------------------------------------------------


def test_login_creates_a_session_of_at_most_43080_seconds(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    session = browser.login(account)
    now = hub.clock()
    answer = session.response
    assert answer == {
        "user_id": account.user_id,
        "key_id": session.key_id,
        "expires_at": answer["expires_at"],
        "prf_expected": False,
    }
    [(user, credential, point, statement, created, expires, ended)] = rows(
        hub,
        "SELECT user_id, credential_id, public_key, statement, created_at, expires_at, ended_at"
        " FROM hub.browser_session WHERE key_id = %s",
        session.key_id,
    )
    assert (str(user), bytes(credential), created, ended) == (account.user_id, account.credential_id, now, None)
    assert len(bytes(point)) == 65
    assert session_key_id(statement.encode("ascii")) == session.key_id
    assert expires - now <= timedelta(seconds=SESSION_ISSUE_SECONDS)
    assert answer["expires_at"] == format_time(expires)
    [(counter, last_used)] = rows(hub, "SELECT sign_count, last_used_at FROM hub.passkey")
    assert (counter, last_used) == (1, now)
    [(actor, target)] = rows(hub, "SELECT actor_id, target_id FROM hub.audit WHERE action = 'auth.login'")
    assert (str(actor), target) == (account.user_id, b64url_encode(account.credential_id))


def test_a_browser_clock_ahead_is_capped_at_43080_seconds(browser: FakeBrowser, account: Account) -> None:
    session = browser.login(account, expires_in=SESSION_ISSUE_SECONDS + 100)
    [(expires,)] = rows(browser.hub, "SELECT expires_at FROM hub.browser_session WHERE key_id = %s", session.key_id)
    assert expires == browser.hub.clock() + timedelta(seconds=SESSION_ISSUE_SECONDS)


def test_a_shorter_session_keeps_the_statement_expiry(browser: FakeBrowser, account: Account) -> None:
    session = browser.login(account, expires_in=600)
    [(expires,)] = rows(browser.hub, "SELECT expires_at FROM hub.browser_session WHERE key_id = %s", session.key_id)
    assert expires.timestamp() == int(browser.hub.clock().timestamp()) + 600


def test_the_passkey_slot_is_returned_once_uploaded(browser: FakeBrowser, account: Account) -> None:
    client = browser.client(browser.login(account))
    slot = passkey_slot(account.credential_id)
    assert client.put(f"/v1/me/bundles/{slot}", json={"bundle_ct": browser.passkey_bundle(account)}).json() == {}
    answer = browser.login(account).response
    assert answer["prf_expected"] is True
    kek = kek_from_prf(browser.authenticator.prf(account.credential_id), account.credential_id)
    identity = open_bundle(kek, account.user_id, slot, answer["bundle_ct"])
    assert identity.sig_public() == account.identity.sig_public()


@pytest.mark.parametrize(
    ("options", "code", "status"),
    [
        ({"expires_in": 43201}, "certificate_expiry_invalid", 400),
        ({"expires_in": 0}, "certificate_expiry_invalid", 400),
        ({"origin": "https://evil.example"}, "statement_invalid", 400),
        ({"flags": FLAG_UP}, "user_verification_missing", 400),
        ({"flags": FLAG_UV}, "user_presence_missing", 400),
        ({"rp_id": "evil.example"}, "rp_id_mismatch", 400),
        ({"kind": "webauthn.create"}, "client_data_invalid", 400),
        ({"corrupt": True}, "signature_invalid", 401),
    ],
    ids=["too-long", "expired", "statement-origin", "no-uv", "no-up", "rp-id", "type", "signature"],
)
def test_certificate_refusals(
    browser: FakeBrowser, account: Account, options: dict[str, Any], code: str, status: int
) -> None:
    response = attempt(browser, account, **options)
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert rows(browser.hub, "SELECT count(*) FROM hub.browser_session") == [(0,)]
    [(result, actor_kind, detail)] = refusals(browser.hub)
    assert (result, actor_kind, detail["code"]) == ("denied", "anonymous", code)
    assert detail["key_id"].startswith(SESSION_PREFIX)


def test_client_data_from_another_origin_is_refused(browser: FakeBrowser, account: Account) -> None:
    begin = browser.login_begin()
    hub_value = b64url_decode(begin["hub_value"]) or b""
    certificate, _, _ = browser.certificate(hub_value, account.credential_id)
    statement = b64url_decode(certificate["statement"]) or b""
    assertion = browser.authenticator.get(
        account.credential_id, statement_challenge(statement), origin="https://evil.example"
    )
    certificate.update(
        {
            "authenticator_data": b64url_encode(assertion["authenticator_data"]),
            "client_data_json": b64url_encode(assertion["client_data_json"]),
            "signature": b64url_encode(assertion["signature"]),
        }
    )
    response = browser.hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": begin["ceremony_id"], "certificate": certificate}
    )
    assert response.json() == {"error": "origin_mismatch"}


def test_the_hub_value_of_another_ceremony_is_refused(browser: FakeBrowser, account: Account) -> None:
    first = browser.login_begin()
    second = browser.login_begin()
    certificate, _, _ = browser.certificate(b64url_decode(first["hub_value"]) or b"", account.credential_id)
    response = browser.hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": second["ceremony_id"], "certificate": certificate}
    )
    assert (response.status_code, response.json()) == (400, {"error": "challenge_mismatch"})


def test_a_counter_regression_is_refused(browser: FakeBrowser, account: Account) -> None:
    browser.login(account)
    response = attempt(browser, account, counter=1)
    assert (response.status_code, response.json()) == (400, {"error": "counter_regressed"})
    assert rows(browser.hub, "SELECT sign_count FROM hub.passkey") == [(1,)]
    assert refusals(browser.hub)[0][2]["code"] == "counter_regressed"


def test_an_authenticator_without_counter_logs_in_repeatedly(browser: FakeBrowser, hub: HubHarness) -> None:
    public = hub.config.public
    browser = FakeBrowser(hub, FakeAuthenticator(public.origin, public.rp_id, counting=False))
    account = browser.register(seed_invite(hub))
    browser.login(account)
    browser.login(account)
    assert rows(hub, "SELECT sign_count FROM hub.passkey") == [(0,)]


def test_an_unknown_credential_is_refused(browser: FakeBrowser, account: Account) -> None:
    other = secrets.token_bytes(32)
    browser.authenticator.credentials[other] = browser.authenticator.credentials[account.credential_id]
    response = attempt(browser, Account(account.user_id, other, account.identity, b"", b""))
    assert (response.status_code, response.json()) == (400, {"error": "credential_invalid"})


@pytest.mark.parametrize(
    "change", ["UPDATE hub.passkey SET removed_at = %s", "UPDATE hub.principal SET disabled_at = %s"]
)
def test_a_removed_passkey_or_disabled_user_cannot_log_in(browser: FakeBrowser, account: Account, change: str) -> None:
    with browser.hub.pool.connection() as connection:
        connection.execute(change, (browser.hub.clock(),))
    assert attempt(browser, account).json() == {"error": "credential_invalid"}


def test_a_login_ceremony_is_used_once(browser: FakeBrowser, account: Account) -> None:
    begin = browser.login_begin()
    hub_value = b64url_decode(begin["hub_value"]) or b""
    certificate, _, _ = browser.certificate(hub_value, account.credential_id)
    body = {"ceremony_id": begin["ceremony_id"], "certificate": certificate}
    assert browser.hub.anon.post("/v1/passkeys/login/finish", json=body).status_code == 200
    again = browser.hub.anon.post("/v1/passkeys/login/finish", json=body)
    assert (again.status_code, again.json()) == (400, {"error": "ceremony_invalid"})


def test_an_expired_login_ceremony_is_refused(browser: FakeBrowser, account: Account) -> None:
    begin = browser.login_begin()
    browser.hub.clock.advance(300)
    certificate, _, _ = browser.certificate(b64url_decode(begin["hub_value"]) or b"", account.credential_id)
    response = browser.hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": begin["ceremony_id"], "certificate": certificate}
    )
    assert response.json() == {"error": "ceremony_invalid"}
    # Refusals before the ceremony was taken leave no audit row (random IDs cannot flood the chain).
    assert refusals(browser.hub) == []


def test_a_registration_ceremony_cannot_finish_a_login(browser: FakeBrowser, account: Account) -> None:
    started = browser.register_begin(seed_invite(browser.hub)).json()
    certificate, _, _ = browser.certificate(bytes(32), account.credential_id)
    response = browser.hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": started["ceremony_id"], "certificate": certificate}
    )
    assert response.json() == {"error": "ceremony_invalid"}


def test_login_refuses_malformed_certificates(browser: FakeBrowser) -> None:
    begin = browser.login_begin()
    certificate = dict.fromkeys(("statement", "authenticator_data", "client_data_json", "signature"), "AAAA")
    certificate["credential_id"] = "AAAA"
    certificate["signature"] = "A" * 400
    response = browser.hub.anon.post(
        "/v1/passkeys/login/finish",
        json={"ceremony_id": begin["ceremony_id"], "certificate": certificate},
        extensions=NO_CONTRACT,
    )
    assert response.status_code == 400
    assert response.json()["error"] == "request_invalid"


# --- the session key at the gate --------------------------------------------------------------------------------------


def test_the_session_key_signs_requests(browser: FakeBrowser, account: Account) -> None:
    client = browser.client(browser.login(account))
    response = client.get("/v1/me")
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True
    assert response.json()["user_id"] == account.user_id


def test_an_expired_session_is_refused(browser: FakeBrowser, account: Account) -> None:
    client = browser.client(browser.login(account))
    browser.hub.clock.advance(SESSION_ISSUE_SECONDS)
    response = client.get("/v1/me")
    assert (response.status_code, response.json()) == (401, {"error": "session_expired"})


def test_ending_the_session_revokes_its_key(browser: FakeBrowser, account: Account) -> None:
    session = browser.login(account)
    client = browser.client(session)
    response = client.post("/v1/session/end", json={})
    assert (response.status_code, response.json()) == (200, {})
    assert response.extensions["hub_signature"] is True
    assert rows(browser.hub, "SELECT ended_at FROM hub.browser_session") == [(browser.hub.clock(),)]
    [(actor, target, detail)] = rows(
        browser.hub, "SELECT actor_id, target_id, detail FROM hub.audit WHERE action = 'auth.session_end'"
    )
    assert (str(actor), target, detail) == (account.user_id, session.key_id, {"key_id": session.key_id})
    again = client.get("/v1/me")
    assert (again.status_code, again.json()) == (401, {"error": "key_revoked"})


def test_session_end_needs_a_session(browser: FakeBrowser) -> None:
    response = browser.hub.anon.post("/v1/session/end", json={})
    assert (response.status_code, response.json()) == (401, {"error": "signature_missing"})


# --- renewal (rule E-40, ADR-099) -------------------------------------------------------------------------------------


def test_renewal_extends_the_same_session(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    session = browser.login(account)
    client = browser.client(session)
    [(created_before, statement_before)] = rows(
        hub, "SELECT created_at, statement FROM hub.browser_session WHERE key_id = %s", session.key_id
    )
    hub.clock.advance(3600)
    response = client.post("/v1/session/renew", json={})
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True
    renewed = hub.clock() + timedelta(seconds=SESSION_RENEW_SECONDS)
    assert response.json() == {"key_id": session.key_id, "expires_at": format_time(renewed)}
    [(created, statement, expires, ended)] = rows(
        hub,
        "SELECT created_at, statement, expires_at, ended_at FROM hub.browser_session WHERE key_id = %s",
        session.key_id,
    )
    assert (created, statement, expires, ended) == (created_before, statement_before, renewed, None)
    # the same key keeps signing after the 12 h of the login
    hub.clock.advance(SESSION_ISSUE_SECONDS)
    assert client.get("/v1/me").status_code == 200
    [(actor, target, detail)] = rows(
        hub, "SELECT actor_id, target_id, detail FROM hub.audit WHERE action = 'auth.session_renew'"
    )
    assert (str(actor), target, detail) == (account.user_id, session.key_id, {"key_id": session.key_id})


def test_renewal_never_shortens_a_session(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    session = browser.login(account)
    later = hub.clock() + timedelta(seconds=SESSION_RENEW_SECONDS + 600)
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.browser_session SET expires_at = %s", (later,))
    response = browser.client(session).post("/v1/session/renew", json={})
    assert response.json() == {"key_id": session.key_id, "expires_at": format_time(later)}


def test_an_expired_session_cannot_renew(browser: FakeBrowser, account: Account) -> None:
    client = browser.client(browser.login(account))
    browser.hub.clock.advance(SESSION_ISSUE_SECONDS)
    response = client.post("/v1/session/renew", json={})
    assert (response.status_code, response.json()) == (401, {"error": "session_expired"})


def test_an_ended_session_cannot_renew(browser: FakeBrowser, account: Account) -> None:
    client = browser.client(browser.login(account))
    assert client.post("/v1/session/end", json={}).status_code == 200
    response = client.post("/v1/session/renew", json={})
    assert (response.status_code, response.json()) == (401, {"error": "key_revoked"})


def test_renewal_needs_a_session(browser: FakeBrowser) -> None:
    response = browser.hub.anon.post("/v1/session/renew", json={})
    assert (response.status_code, response.json()) == (401, {"error": "signature_missing"})


def test_renewal_refuses_a_session_ended_after_the_gate_and_leaves_an_agent_unchanged(
    browser: FakeBrowser, account: Account
) -> None:
    hub = browser.hub
    session = browser.login(account)
    context: HubContext = hub.app.state.hub
    user = uuid.UUID(account.user_id)
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.browser_session SET ended_at = %s", (hub.clock(),))
    principal = Principal(kind="session", key_id=session.key_id, principal_id=user, actor_kind="user")
    with pytest.raises(HubError, match="key_revoked"):
        renew_session(context, principal)
    # an agent's key does not expire: its key ID and no end, nothing written
    agent = Principal(kind="agent", key_id="agent." + "0" * 32, principal_id=user, actor_kind="user")
    assert renew_session(context, agent) == {"key_id": "agent." + "0" * 32, "expires_at": None}
    assert rows(hub, "SELECT count(*) FROM hub.audit WHERE action = 'auth.session_renew'") == [(0,)]


@pytest.mark.parametrize(
    "change", ["UPDATE hub.passkey SET removed_at = %s", "UPDATE hub.principal SET disabled_at = %s"]
)
def test_a_removed_passkey_or_disabled_user_revokes_the_session(
    browser: FakeBrowser, account: Account, change: str
) -> None:
    client = browser.client(browser.login(account))
    with browser.hub.pool.connection() as connection:
        connection.execute(change, (browser.hub.clock(),))
    assert client.get("/v1/me").json() == {"error": "key_revoked"}


def test_a_tampered_request_is_refused(browser: FakeBrowser, account: Account) -> None:
    client = browser.client(browser.login(account))
    response = client.put(
        "/v1/me/settings",
        json={"model_access": "auto", "model_id": None, "locale": "de"},
        headers={"Agent-Signature": b64url_encode(bytes(64))},
    )
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})


def test_the_session_resolver_is_registered_and_resolves(browser: FakeBrowser, account: Account) -> None:
    assert {SESSION_PREFIX: resolve_session} == RESOLVERS
    session = browser.login(account)
    context: HubContext = browser.hub.app.state.hub
    resolved = resolve_session(context, session.key_id)
    assert resolved is not None
    assert (resolved.principal.kind, resolved.principal.key_id, str(resolved.principal.principal_id)) == (
        "session",
        session.key_id,
        account.user_id,
    )
    assert resolved.principal.actor_kind == "user"
    assert resolve_session(context, SESSION_PREFIX + "0" * 32) is None
    browser.hub.clock.advance(SESSION_ISSUE_SECONDS)
    with pytest.raises(HubError, match="session_expired"):
        resolve_session(context, session.key_id)
