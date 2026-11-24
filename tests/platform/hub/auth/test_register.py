"""U-HUB-passkeys (registration): `POST /v1/passkeys/register/begin|finish` (03-api.md section 4.1, 04-crypto.md
section 5.1, ADR-026).

Registration only through an open user invitation (R-1.1: no self-registration), options exactly as specified,
WebAuthn verification (origin, RP ID, UV, UP, ES256 only, challenge, type), the first passkey's identity, recovery
slot and self-wrap of `CK_1`, the binding of the invitation and the one-time use of every ceremony.
"""

from __future__ import annotations

import importlib.machinery
import secrets
import sys
import threading
import types
import uuid
from datetime import timedelta
from typing import Any

import pytest

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.auth import register
from agent_hub.auth.ceremonies import creation_options
from agent_hub.auth.keys import ANONYMOUS, Principal
from agent_hub.crypto.kdf import prf_input
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.recovery import recovery_id
from agent_hub.crypto.wrap import sign_wrap
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from tests.platform.hub.auth.authenticator import (
    EDDSA,
    FLAG_AT,
    FLAG_BS,
    FLAG_UP,
    FLAG_UV,
    Account,
    FakeBrowser,
    seed_invite,
)
from tests.platform.hub.conftest import HubHarness

NO_CONTRACT = {"contract": False}


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


def count(hub: HubHarness, table: str) -> int:
    return int(rows(hub, f"SELECT count(*) FROM hub.{table}")[0][0])


def audit_actions(hub: HubHarness) -> list[tuple[Any, ...]]:
    return rows(hub, "SELECT action, result, actor_kind, actor_id, target_kind, target_id, detail FROM hub.audit")


def begin(browser: FakeBrowser, invite: str) -> dict[str, Any]:
    response = browser.register_begin(invite)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def finish(browser: FakeBrowser, body: dict[str, Any], **request: Any) -> Any:
    return browser.hub.anon.post("/v1/passkeys/register/finish", json=body, **request)


def first_body(browser: FakeBrowser, invite: str, **create: Any) -> dict[str, Any]:
    started = begin(browser, invite)
    credential = browser.authenticator.create(started["options"], **create)
    body, *_ = browser.registration_body(started, credential)
    return body


# --- begin ------------------------------------------------------------------------------------------------------------


def test_begin_returns_the_specified_creation_options(browser: FakeBrowser) -> None:
    invite = seed_invite(browser.hub)
    started = begin(browser, invite)
    user_id = uuid.UUID(started["user_id"])
    assert user_id.version == 7
    options = started["options"]
    assert options["rp"] == {"id": "localhost", "name": "agent.dfx.swiss"}
    assert options["user"] == {
        "id": b64url_encode(user_id.bytes),
        "name": started["user_id"],
        "displayName": started["user_id"],
    }
    assert options["pubKeyCredParams"] == [{"type": "public-key", "alg": -7}]
    assert options["authenticatorSelection"] == {
        "residentKey": "required",
        "userVerification": "required",
        "requireResidentKey": True,
    }
    assert options["extensions"] == {"prf": {"eval": {"first": b64url_encode(prf_input())}}}
    assert options["timeout"] == 300000
    assert options["attestation"] == "none"
    assert options["excludeCredentials"] == []
    challenge = b64url_decode(options["challenge"])
    [(kind, stored, user, bound, created, expires, used)] = rows(
        browser.hub,
        "SELECT kind, challenge, user_id, invite_id, created_at, expires_at, used_at FROM hub.ceremony WHERE id = %s",
        uuid.UUID(started["ceremony_id"]),
    )
    assert (kind, bytes(stored), user, bound, used) == ("register", challenge, user_id, invite, None)
    assert expires - created == timedelta(seconds=300)


def test_begin_lists_existing_credentials_to_exclude(browser: FakeBrowser) -> None:
    user = uuid.UUID(int=7 << 76 | 2 << 62)
    options = creation_options(browser.hub.config, user, bytes(32), [b"\x01" * 16])
    assert options["excludeCredentials"] == [{"type": "public-key", "id": b64url_encode(b"\x01" * 16)}]


@pytest.mark.parametrize(
    "state",
    [
        {"used": True},
        {"revoked": True},
        {"expires_in": -1},
        {"expires_in": 0},
        {"kind": "device"},
        {"registered_user": uuid.UUID("01900000-0000-7000-8000-000000000001")},
    ],
    ids=["used", "revoked", "expired", "expires-now", "device", "registered"],
)
def test_begin_refuses_an_invitation_that_is_not_open(browser: FakeBrowser, state: dict[str, Any]) -> None:
    invite = seed_invite(browser.hub, **state)
    response = browser.register_begin(invite)
    assert (response.status_code, response.json()) == (404, {"error": "invite_unknown"})
    assert count(browser.hub, "ceremony") == 0


def test_begin_refuses_an_unknown_invitation(browser: FakeBrowser) -> None:
    response = browser.register_begin(b64url_encode(secrets.token_bytes(16)))
    assert (response.status_code, response.json()) == (404, {"error": "invite_unknown"})


def test_begin_accepts_an_org_invitation(browser: FakeBrowser) -> None:
    assert browser.register_begin(seed_invite(browser.hub, scope="org")).status_code == 200


@pytest.mark.parametrize("body", [{}, {"invite_id": "A" * 21 + "A", "add": True}], ids=["empty", "both"])
def test_begin_needs_exactly_one_of_invitation_and_add(browser: FakeBrowser, body: dict[str, Any]) -> None:
    response = browser.hub.anon.post("/v1/passkeys/register/begin", json=body, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (400, {"error": "request_invalid"})


@pytest.mark.parametrize(
    "body",
    [{"invite_id": "short"}, {"add": False}, {"invite_id": "A" * 22, "other": 1}, {"invite_id": 5}],
    ids=["pattern", "add-false", "unknown-field", "type"],
)
def test_begin_refuses_malformed_bodies(browser: FakeBrowser, body: dict[str, Any]) -> None:
    response = browser.hub.anon.post("/v1/passkeys/register/begin", json=body, extensions=NO_CONTRACT)
    assert response.status_code == 400
    assert response.json()["error"] == "request_invalid"


def test_without_invitation_and_without_session_nobody_registers(browser: FakeBrowser) -> None:
    # R-1.1: `{add: true}` is the only form without an invitation and needs a signed-in user.
    response = browser.hub.anon.post("/v1/passkeys/register/begin", json={"add": True})
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})


def test_add_with_a_session_is_refused_without_passkey_management(
    browser: FakeBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HUB-11 created `auth/passkeys_manage.py`; a hub without the module refuses `{add: true}`.
    monkeypatch.setattr(register, "ADD_MODULE", "agent_hub.auth.no_such_module")
    response = browser.hub.browser().post("/v1/passkeys/register/begin", json={"add": True})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


def test_add_is_handed_to_passkey_management_when_it_exists(
    browser: FakeBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Principal] = []

    def begin_add(context: HubContext, principal: Principal) -> dict[str, Any]:
        seen.append(principal)
        user = uuid.UUID(str(principal.principal_id))
        return {
            "ceremony_id": str(user),
            "user_id": str(user),
            "options": creation_options(context.config, user, bytes(32)),
        }

    module = types.ModuleType(register.ADD_MODULE)
    module.__spec__ = importlib.machinery.ModuleSpec(register.ADD_MODULE, None)
    module.begin_add = begin_add  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, register.ADD_MODULE, module)
    client = browser.hub.browser()
    response = client.post("/v1/passkeys/register/begin", json={"add": True})
    assert response.status_code == 200, response.text
    assert client.key is not None
    assert [principal.key_id for principal in seen] == [client.key.key_id]


def test_a_device_cannot_register(browser: FakeBrowser) -> None:
    invite = seed_invite(browser.hub)
    response = browser.hub.device().post("/v1/passkeys/register/begin", json={"invite_id": invite})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


# --- finish: first passkey --------------------------------------------------------------------------------------------


def test_first_passkey_stores_identity_recovery_wrap_and_binds_the_invitation(browser: FakeBrowser) -> None:
    hub = browser.hub
    invite = seed_invite(hub)
    started = begin(browser, invite)
    credential = browser.authenticator.create(started["options"], transports=["internal", "hybrid"])
    body, identity, code, _ = browser.registration_body(started, credential)
    response = finish(browser, body)
    assert response.status_code == 200, response.text
    user_id = uuid.UUID(started["user_id"])
    assert response.json() == {"user_id": started["user_id"], "credential_id": credential["id"]}
    now = hub.clock()
    assert rows(hub, "SELECT id, kind, created_at, disabled_at FROM hub.principal") == [(user_id, "user", now, None)]
    [(kem, sig, rid)] = rows(hub, "SELECT kem_pub, sig_pub, recovery_id FROM hub.user_identity")
    assert (bytes(kem), bytes(sig), rid) == (identity.kem_public(), identity.sig_public(), recovery_id(code))
    assert rows(hub, "SELECT model_access, model_id, locale FROM hub.user_settings") == [("auto", None, "en")]
    [(slot, ct)] = rows(hub, "SELECT slot, ct FROM hub.identity_bundle WHERE user_id = %s", user_id)
    assert (slot, bytes(ct)) == ("recovery", b64url_decode(body["recovery"]["bundle_ct"]))
    [(raw_id, owner, point, counter, transports, prf_seen, removed)] = rows(
        hub, "SELECT credential_id, user_id, public_key, sign_count, transports, prf_seen, removed_at FROM hub.passkey"
    )
    assert (bytes(raw_id), owner, len(point), counter, transports, prf_seen, removed) == (
        b64url_decode(credential["id"]),
        user_id,
        65,
        0,
        ["internal", "hybrid"],
        False,
        None,
    )
    assert rows(hub, "SELECT scope, scope_id, epoch FROM hub.scope_epoch") == [("cred", user_id, 1)]
    [(scope, scope_id, epoch, recipient, wrap, wrapped_by, wrap_sig)] = rows(
        hub, "SELECT scope, scope_id, epoch, recipient_id, wrap, wrapped_by, sig FROM hub.key_wrap"
    )
    assert (scope, scope_id, epoch, recipient, wrapped_by) == ("cred", user_id, 1, user_id, user_id)
    assert bytes(wrap) == b64url_decode(body["cred_wrap"]["wrap"])
    assert bytes(wrap_sig) == b64url_decode(body["cred_wrap"]["sig"])
    assert rows(hub, "SELECT registered_user, registered_at, used_at FROM hub.invite") == [(user_id, now, None)]
    assert rows(hub, "SELECT used_at FROM hub.ceremony") == [(now,)]
    reference = credential["id"]
    begun = {"method": "POST", "route": "/v1/passkeys/register/begin", "status": 200, "code": "ok"}
    assert audit_actions(hub) == [
        ("request.write", "ok", "anonymous", None, None, None, begun),  # the ceremony's generic row (ADR-063)
        ("auth.register", "ok", "user", user_id, "passkey", reference, {"passkey_id": reference, "invite_id": invite}),
        ("invite.register", "ok", "user", user_id, "invite", invite, {"invite_id": invite, "kind": "user"}),
    ]


def test_a_long_credential_id_is_named_by_its_digest_in_the_audit(browser: FakeBrowser) -> None:
    raw_id = secrets.token_bytes(200)
    response = finish(browser, first_body(browser, seed_invite(browser.hub), credential_id=raw_id))
    assert response.status_code == 200, response.text
    [(target,)] = rows(browser.hub, "SELECT target_id FROM hub.audit WHERE action = 'auth.register'")
    assert target.startswith("sha256:")
    assert len(target) == 7 + 32


def test_the_ceremony_is_used_once(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    assert finish(browser, body).status_code == 200
    again = finish(browser, body)
    assert (again.status_code, again.json()) == (400, {"error": "ceremony_invalid"})


def test_a_failed_verification_consumes_the_ceremony(browser: FakeBrowser) -> None:
    invite = seed_invite(browser.hub)
    started = begin(browser, invite)
    bad, *_ = browser.registration_body(
        started, browser.authenticator.create(started["options"], origin="https://evil.example")
    )
    assert finish(browser, bad).json() == {"error": "origin_mismatch"}
    good, *_ = browser.registration_body(started, browser.authenticator.create(started["options"]))
    assert finish(browser, good).json() == {"error": "ceremony_invalid"}
    assert count(browser.hub, "principal") == 0


def test_an_expired_ceremony_is_refused(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    browser.hub.clock.advance(300)
    response = finish(browser, body)
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})


def test_an_unknown_ceremony_is_refused(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    body["ceremony_id"] = str(uuid.UUID(int=uuid.UUID(body["ceremony_id"]).int ^ 1))
    assert finish(browser, body).json() == {"error": "ceremony_invalid"}


def test_a_login_ceremony_cannot_finish_a_registration(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    body["ceremony_id"] = browser.login_begin()["ceremony_id"]
    assert finish(browser, body).json() == {"error": "ceremony_invalid"}


@pytest.mark.parametrize(
    ("create", "code"),
    [
        ({"origin": "https://evil.example"}, "origin_mismatch"),
        ({"origin": "http://localhost:8789"}, "origin_mismatch"),
        ({"client_extra": {"crossOrigin": True}}, "origin_mismatch"),
        ({"client_extra": {"topOrigin": "http://localhost:8788"}}, "origin_mismatch"),
        ({"rp_id": "evil.example"}, "rp_id_mismatch"),
        ({"flags": FLAG_UP | FLAG_AT}, "user_verification_missing"),
        ({"flags": FLAG_UV | FLAG_AT}, "user_presence_missing"),
        ({"flags": FLAG_UP | FLAG_UV | FLAG_AT | FLAG_BS}, "authenticator_data_invalid"),
        ({"alg": EDDSA}, "credential_invalid"),
        ({"alg": -257}, "credential_invalid"),
        ({"kind": "webauthn.get"}, "client_data_invalid"),
        ({"challenge": bytes(32)}, "challenge_mismatch"),
        ({"attested_id": b"\x02" * 32}, "credential_invalid"),
        ({"credential_id": b"\x03" * 8}, "credential_invalid"),
    ],
    ids=[
        "origin",
        "origin-port",
        "cross-origin",
        "top-origin",
        "rp-id",
        "no-uv",
        "no-up",
        "backup-state",
        "eddsa",
        "rs256",
        "type",
        "challenge",
        "attested-id",
        "short-id",
    ],
)
def test_webauthn_refusals(browser: FakeBrowser, create: dict[str, Any], code: str) -> None:
    response = finish(browser, first_body(browser, seed_invite(browser.hub), **create))
    assert (response.status_code, response.json()) == (400, {"error": code})
    assert count(browser.hub, "principal") == 0
    assert rows(browser.hub, "SELECT registered_user FROM hub.invite") == [(None,)]


def test_a_malformed_attestation_object_is_refused(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    body["credential"]["attestationObject"] = b64url_encode(b"\xa0")
    assert finish(browser, body).json() == {"error": "attestation_invalid"}


@pytest.mark.parametrize("state", ["used_at", "revoked_at"])
def test_the_invitation_is_checked_again_at_finish(browser: FakeBrowser, state: str) -> None:
    invite = seed_invite(browser.hub)
    body = first_body(browser, invite)
    with browser.hub.pool.connection() as connection:
        connection.execute(f"UPDATE hub.invite SET {state} = %s", (browser.hub.clock(),))
    response = finish(browser, body)
    assert (response.status_code, response.json()) == (404, {"error": "invite_unknown"})
    assert count(browser.hub, "principal") == 0


def test_an_invitation_that_expired_after_begin_is_refused(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub, expires_in=100))
    browser.hub.clock.advance(100)
    assert finish(browser, body).json() == {"error": "invite_unknown"}


def test_one_invitation_registers_one_user(browser: FakeBrowser) -> None:
    invite = seed_invite(browser.hub)
    first = first_body(browser, invite)
    second = first_body(browser, invite)
    assert finish(browser, first).status_code == 200
    response = finish(browser, second)
    assert (response.status_code, response.json()) == (404, {"error": "invite_unknown"})
    assert count(browser.hub, "principal") == 1
    assert browser.register_begin(invite).json() == {"error": "invite_unknown"}


def test_an_invalid_identity_signature_key_is_refused(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    body["identity"]["sig_pub"] = b64url_encode(bytes([0xFF] * 32))
    response = finish(browser, body)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "params_invalid", "detail": {"pointers": ["/identity/sig_pub"]}},
    )


def test_a_wrap_not_signed_by_the_new_identity_is_refused(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    user_id = rows_user(browser, body)
    body["cred_wrap"]["sig"] = sign_wrap(
        Identity.generate().sig,
        scope="cred",
        scope_id=str(user_id),
        epoch=1,
        recipient=str(user_id),
        wrap=body["cred_wrap"]["wrap"],
    )
    response = finish(browser, body)
    assert (response.status_code, response.json()) == (400, {"error": "wrap_unauthorized"})
    assert count(browser.hub, "key_wrap") == 0


def rows_user(browser: FakeBrowser, body: dict[str, Any]) -> uuid.UUID:
    [(user,)] = rows(browser.hub, "SELECT user_id FROM hub.ceremony WHERE id = %s", uuid.UUID(body["ceremony_id"]))
    return uuid.UUID(str(user))


def test_a_wrap_for_another_recipient_is_refused(browser: FakeBrowser) -> None:
    invite = seed_invite(browser.hub)
    started = begin(browser, invite)
    body, identity, _, _ = browser.registration_body(started, browser.authenticator.create(started["options"]))
    other = str(uuid.UUID(int=uuid.UUID(started["user_id"]).int ^ 1))
    body["cred_wrap"]["sig"] = sign_wrap(
        identity.sig, scope="cred", scope_id=other, epoch=1, recipient=other, wrap=body["cred_wrap"]["wrap"]
    )
    assert finish(browser, body).json() == {"error": "wrap_unauthorized"}


def test_a_recovery_id_in_use_is_a_conflict(browser: FakeBrowser) -> None:
    account = browser.register(seed_invite(browser.hub))
    invite = seed_invite(browser.hub)
    started = begin(browser, invite)
    credential = browser.authenticator.create(started["options"])
    body, *_ = browser.registration_body(started, credential, code=account.recovery_code)
    response = finish(browser, body)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert count(browser.hub, "principal") == 1
    assert rows(browser.hub, "SELECT registered_user FROM hub.invite WHERE id = %s", invite) == [(None,)]


def test_a_credential_registered_before_is_refused(browser: FakeBrowser) -> None:
    account = browser.register(seed_invite(browser.hub))
    response = finish(browser, first_body(browser, seed_invite(browser.hub), credential_id=account.credential_id))
    assert (response.status_code, response.json()) == (400, {"error": "credential_invalid"})
    assert count(browser.hub, "principal") == 1


@pytest.mark.parametrize("missing", ["identity", "recovery", "cred_wrap"])
def test_first_passkey_parts_come_together(browser: FakeBrowser, missing: str) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    del body[missing]
    response = finish(browser, body, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (400, {"error": "request_invalid"})


def test_a_registration_without_identity_is_refused_and_keeps_the_ceremony(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    bare = {"ceremony_id": body["ceremony_id"], "credential": body["credential"]}
    assert finish(browser, bare).json() == {"error": "request_invalid"}
    assert finish(browser, body).status_code == 200


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("credential", "transports"), ["bluetooth"]),
        (("credential", "id"), "AB"),
        (("cred_wrap", "epoch"), 2),
        (("recovery", "bundle_ct"), "AAAA"),
        (("identity", "kem_pub"), "A" * 42 + "B"),
    ],
    ids=["transport", "id", "epoch", "bundle", "kem"],
)
def test_finish_refuses_malformed_fields(browser: FakeBrowser, path: tuple[str, str], value: object) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    body[path[0]][path[1]] = value
    response = finish(browser, body, extensions=NO_CONTRACT)
    assert response.status_code == 400
    assert response.json()["error"] == "request_invalid"


def test_a_signed_in_browser_may_register_through_an_invitation(browser: FakeBrowser) -> None:
    body = first_body(browser, seed_invite(browser.hub))
    assert browser.hub.browser().post("/v1/passkeys/register/finish", json=body).status_code == 200


# --- finish: further passkeys (ceremonies of HUB-11) ------------------------------------------------------------------


def further_ceremony(browser: FakeBrowser, account: Account, kind: str) -> dict[str, Any]:
    """A ceremony of `kind` for the account, as HUB-11 creates it; the finish body for a new credential."""
    hub = browser.hub
    ceremony = uuid.UUID(int=uuid.UUID(account.user_id).int ^ 0xFF)
    challenge = secrets.token_bytes(32)
    now = hub.clock()
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.ceremony (id, kind, challenge, user_id, created_at, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (ceremony, kind, challenge, uuid.UUID(account.user_id), now, now + timedelta(seconds=300)),
        )
    user = uuid.UUID(account.user_id)
    options = creation_options(hub.config, user, challenge)
    return {"ceremony_id": str(ceremony), "credential": browser.authenticator.create(options)}


def test_a_further_passkey_is_added_to_the_signed_in_user(browser: FakeBrowser) -> None:
    hub = browser.hub
    account = browser.register(seed_invite(hub))
    client = browser.client(browser.login(account))
    body = further_ceremony(browser, account, "add_passkey")
    response = client.post("/v1/passkeys/register/finish", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"user_id": account.user_id, "credential_id": body["credential"]["id"]}
    assert count(hub, "passkey") == 2
    assert count(hub, "user_identity") == 1
    [(actor, target)] = rows(hub, "SELECT actor_id, target_id FROM hub.audit WHERE action = 'auth.passkey_add'")
    assert (str(actor), target) == (account.user_id, body["credential"]["id"])


@pytest.mark.parametrize("who", ["anonymous", "other"])
def test_only_the_owner_finishes_an_add_passkey_ceremony(browser: FakeBrowser, who: str) -> None:
    account = browser.register(seed_invite(browser.hub))
    body = further_ceremony(browser, account, "add_passkey")
    client = browser.hub.anon if who == "anonymous" else browser.hub.browser()
    response = client.post("/v1/passkeys/register/finish", json=body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    owned = rows(browser.hub, "SELECT count(*) FROM hub.passkey WHERE user_id = %s", uuid.UUID(account.user_id))
    assert owned == [(1,)]


def test_a_recovery_ceremony_adds_a_passkey_without_a_session(browser: FakeBrowser) -> None:
    account = browser.register(seed_invite(browser.hub))
    body = further_ceremony(browser, account, "recovery")
    response = finish(browser, body)
    assert response.status_code == 200, response.text
    assert count(browser.hub, "passkey") == 2
    [(actor, target, detail)] = rows(
        browser.hub, "SELECT actor_id, target_id, detail FROM hub.audit WHERE action = 'auth.recovery'"
    )
    assert (str(actor), target, detail) == (account.user_id, body["credential"]["id"], {"step": "finish"})


def test_a_further_passkey_for_a_disabled_user_is_refused(browser: FakeBrowser) -> None:
    account = browser.register(seed_invite(browser.hub))
    body = further_ceremony(browser, account, "recovery")
    with browser.hub.pool.connection() as connection:
        connection.execute("UPDATE hub.principal SET disabled_at = %s", (browser.hub.clock(),))
    assert finish(browser, body).json() == {"error": "ceremony_invalid"}
    assert count(browser.hub, "passkey") == 1


def test_a_further_passkey_carries_no_identity(browser: FakeBrowser) -> None:
    account = browser.register(seed_invite(browser.hub))
    body = further_ceremony(browser, account, "recovery")
    full = first_body(browser, seed_invite(browser.hub))
    body.update({key: full[key] for key in ("identity", "recovery", "cred_wrap", "inv_sig")})
    assert finish(browser, body).json() == {"error": "request_invalid"}


def test_a_further_passkey_with_a_known_credential_is_refused(browser: FakeBrowser) -> None:
    account = browser.register(seed_invite(browser.hub))
    body = further_ceremony(browser, account, "recovery")
    body["credential"] = browser.authenticator.create(
        creation_options(browser.hub.config, uuid.UUID(account.user_id), challenge_of(browser, body)),
        credential_id=account.credential_id,
    )
    assert finish(browser, body).json() == {"error": "credential_invalid"}


def challenge_of(browser: FakeBrowser, body: dict[str, Any]) -> bytes:
    [(challenge,)] = rows(
        browser.hub, "SELECT challenge FROM hub.ceremony WHERE id = %s", uuid.UUID(body["ceremony_id"])
    )
    return bytes(challenge)


def test_a_recovery_proof_ceremony_never_registers_a_passkey(browser: FakeBrowser) -> None:
    """KOR-01 / AUD-01 A-14: the ceremony of `recovery/begin` (it carries the challenge as `hub_value`) is a proof
    ceremony, not a registration; `register/finish` refuses it, keeps it open for its proof and adds no passkey, so it
    can never open the replacement of the recovery slot either."""
    hub = browser.hub
    account = browser.register(seed_invite(hub))
    begun = hub.anon.post("/v1/recovery/begin", json={"recovery_id": recovery_id(account.recovery_code)})
    assert begun.status_code == 200, begun.text
    challenge = b64url_decode(begun.json()["challenge"]) or b""
    [(ceremony,)] = rows(hub, "SELECT id FROM hub.ceremony WHERE kind = 'recovery' AND hub_value IS NOT NULL")
    options = creation_options(hub.config, uuid.UUID(account.user_id), challenge)
    body = {"ceremony_id": str(ceremony), "credential": browser.authenticator.create(options)}
    response = finish(browser, body)
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})
    assert count(hub, "passkey") == 1
    assert rows(hub, "SELECT used_at FROM hub.ceremony WHERE id = %s", ceremony) == [(None,)]


@pytest.mark.parametrize("same_credential", [True, False], ids=["same-credential", "two-credentials"])
def test_parallel_finishes_of_one_ceremony_register_exactly_once(browser: FakeBrowser, same_credential: bool) -> None:
    """KOR-01 / AUD-01 B-21: two `register/finish` of the same ceremony at the same moment (a double click, a retry
    racing the first request): `take_ceremony` locks and consumes the ceremony, so exactly one succeeds and the other
    is `ceremony_invalid`; one user, one passkey, one binding of the invitation."""
    hub = browser.hub
    context: HubContext = hub.app.state.hub
    invite = seed_invite(hub)
    started = begin(browser, invite)
    first = browser.authenticator.create(started["options"])
    second = first if same_credential else browser.authenticator.create(started["options"])
    bodies = [browser.registration_body(started, credential)[0] for credential in (first, second)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run(body: dict[str, Any]) -> None:
        request = register.RegisterFinish.model_validate(body)
        barrier.wait()
        try:
            register.finish(context, request, ANONYMOUS)
            outcomes.append("ok")
        except HubError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=run, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ceremony_invalid", "ok"], outcomes
    assert (count(hub, "principal"), count(hub, "passkey"), count(hub, "user_identity")) == (1, 1, 1)
    [(registered,)] = rows(hub, "SELECT registered_user FROM hub.invite WHERE id = %s", invite)
    assert str(registered) == started["user_id"]
