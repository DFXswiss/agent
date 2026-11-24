"""U-HUB-passkeys (identity): `GET /v1/me`, `GET /v1/me/bundles/recovery`, `PUT /v1/me/bundles/{slot}` and
`PUT /v1/me/settings` (03-api.md section 4.1, 02-data-model.md section 2.1, 04-crypto.md sections 4.1, 5.4, 5.5).

Also checks the zero-knowledge rule of 04-crypto.md section 7 for everything registration and login store: no
private key, recovery code or PRF output reaches the database.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

import pytest

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.crypto.bundle import RECOVERY_SLOT, open_bundle, passkey_slot, seal_bundle
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.kdf import kek_from_recovery_code
from agent_hub.crypto.keys import ed25519_seed, x25519_private_raw
from agent_hub.crypto.recovery import generate_recovery_code, recovery_id
from tests.platform.hub.auth.authenticator import Account, FakeBrowser, seed_invite
from tests.platform.hub.conftest import HubHarness, SignedClient, uuid7

NO_CONTRACT = {"contract": False}

DEFAULT_FALLBACK = {"own_to_dfx": False, "operator_to_platform": True}


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


@pytest.fixture
def account(browser: FakeBrowser) -> Account:
    return browser.register(seed_invite(browser.hub))


@pytest.fixture
def client(browser: FakeBrowser, account: Account) -> SignedClient:
    return browser.client(browser.login(account))


# --- GET /v1/me -------------------------------------------------------------------------------------------------------


def test_me_of_a_new_user(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    response = client.get("/v1/me")
    assert response.status_code == 200
    now = format_time(browser.hub.clock())
    assert response.json() == {
        "user_id": account.user_id,
        "identity": {
            "kem_pub": b64url_encode(account.identity.kem_public()),
            "sig_pub": b64url_encode(account.identity.sig_public()),
        },
        "settings": {"model_access": "auto", "model_id": None, "locale": "en", "fallback": DEFAULT_FALLBACK},
        "orgs": [],
        "platform_roles": [],
        "unread_notifications": 0,
        "passkeys": [
            {
                "credential_id": b64url_encode(account.credential_id),
                "created_at": now,
                "last_used_at": now,
                "prf_seen": False,
            }
        ],
        "cred_holders": {"epoch": 1, "devices": []},
    }


def seed_org(hub: HubHarness, user: uuid.UUID, *, role: str, removed: bool, offset: int) -> uuid.UUID:
    org, wg = uuid7(hub.clock()), uuid7(hub.clock())
    head = b64url_encode(secrets.token_bytes(32))
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.org (id, private, default_wg, created_by, created_at, log_head_seq, log_head_hash,"
            " rotation_pending) VALUES (%s, %s, %s, %s, %s, 3, %s, '[]')",
            (org, role == "admin", wg, user, hub.clock() + timedelta(seconds=offset), head),
        )
        connection.execute(
            "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq, removed_seq)"
            " VALUES (%s, %s, 'user', %s, 1, %s)",
            (org, user, role, 2 if removed else None),
        )
    return org


def test_me_lists_orgs_roles_notifications_and_passkeys(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    hub = browser.hub
    user = uuid.UUID(account.user_id)
    older = seed_org(hub, user, role="admin", removed=False, offset=0)
    newer = seed_org(hub, user, role="member", removed=False, offset=1)
    seed_org(hub, user, role="member", removed=True, offset=2)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.platform_role (user_id, role, granted_seq) VALUES (%s, 'inviter', 1)", (user,)
        )
        for read in (None, None, hub.clock()):
            connection.execute(
                "INSERT INTO hub.notification (id, recipient_id, kind, org_id, created_at, read_at)"
                " VALUES (%s, %s, 'task.idle', %s, %s, %s)",
                (uuid.uuid4(), user, older, hub.clock(), read),
            )
        connection.execute(
            "INSERT INTO hub.passkey (credential_id, user_id, public_key, created_at, last_used_at, removed_at)"
            " SELECT %s, user_id, public_key, created_at, last_used_at, %s FROM hub.passkey",
            (b"\x09" * 16, hub.clock()),
        )
    hub.clock.advance(5)
    second = browser.authenticator.create(
        {"user": {"id": b64url_encode(user.bytes)}, "challenge": b64url_encode(bytes(32))}
    )
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.passkey (credential_id, user_id, public_key, created_at, last_used_at, prf_seen)"
            " SELECT %s, user_id, public_key, %s, %s, true FROM hub.passkey WHERE credential_id = %s",
            (b64url_decode(second["id"]), hub.clock(), hub.clock(), account.credential_id),
        )
    body = client.get("/v1/me").json()
    assert [org["org_id"] for org in body["orgs"]] == [str(newer), str(older)]
    assert body["orgs"][1]["role"] == "admin"
    assert body["orgs"][1]["private"] is True
    assert body["orgs"][1]["log_head"]["seq"] == 3
    assert body["platform_roles"] == ["inviter"]
    assert body["unread_notifications"] == 2
    assert [key["credential_id"] for key in body["passkeys"]] == [
        second["id"],
        b64url_encode(account.credential_id),
    ]
    assert body["passkeys"][0]["prf_seen"] is True


def test_me_of_a_session_without_identity_is_not_found(browser: FakeBrowser) -> None:
    response = browser.hub.browser().get("/v1/me")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_reads_are_logged(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    browser.hub.flush_read_log()  # the windows of the reads before (ADR-063 section 3)
    assert client.get("/v1/me").status_code == 200
    assert client.get("/v1/me/bundles/recovery").status_code == 200
    # recorded before the answers left the hub, no flush needed
    logged = rows(
        browser.hub,
        "SELECT action, actor_id, result FROM hub.audit WHERE action LIKE 'read.%%' ORDER BY seq DESC LIMIT 2",
    )[::-1]
    user = uuid.UUID(account.user_id)
    assert logged == [("read.me", user, "ok"), ("read.me.bundles.recovery", user, "ok")]


def test_me_needs_a_session(browser: FakeBrowser) -> None:
    assert browser.hub.anon.get("/v1/me").json() == {"error": "signature_missing"}
    assert browser.hub.device().get("/v1/me").json() == {"error": "forbidden"}


# --- bundles ----------------------------------------------------------------------------------------------------------


def test_the_recovery_bundle_unlocks_the_identity(account: Account, client: SignedClient) -> None:
    bundle = client.get("/v1/me/bundles/recovery").json()["bundle_ct"]
    identity = open_bundle(kek_from_recovery_code(account.recovery_code), account.user_id, RECOVERY_SLOT, bundle)
    assert x25519_private_raw(identity.kem) == x25519_private_raw(account.identity.kem)


def test_the_recovery_bundle_of_a_user_without_one_is_not_found(browser: FakeBrowser) -> None:
    response = browser.hub.browser().get("/v1/me/bundles/recovery")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_the_own_passkey_slot_is_stored_and_marks_prf(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    slot = passkey_slot(account.credential_id)
    bundle = browser.passkey_bundle(account)
    assert client.put(f"/v1/me/bundles/{slot}", json={"bundle_ct": bundle}).json() == {}
    browser.hub.clock.advance(1)
    replaced = browser.passkey_bundle(account)
    assert client.put(f"/v1/me/bundles/{slot}", json={"bundle_ct": replaced}).status_code == 200
    stored = rows(browser.hub, "SELECT ct, updated_at FROM hub.identity_bundle WHERE slot = %s", slot)
    assert [(bytes(ct), at) for ct, at in stored] == [(b64url_decode(replaced), browser.hub.clock())]
    assert rows(browser.hub, "SELECT prf_seen FROM hub.passkey") == [(True,)]
    # both writes are audited with the passkey as target (ADR-063 section 2)
    audited = rows(
        browser.hub, "SELECT actor_id::text, target_kind, target_id FROM hub.audit WHERE action = 'auth.passkey_bundle'"
    )
    reference = b64url_encode(account.credential_id)
    assert audited == [(account.user_id, "passkey", reference)] * 2


def test_a_foreign_or_removed_passkey_slot_is_forbidden(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    other = browser.register(seed_invite(browser.hub))
    bundle = browser.passkey_bundle(account)
    for credential in (other.credential_id, secrets.token_bytes(16)):
        response = client.put(f"/v1/me/bundles/{passkey_slot(credential)}", json={"bundle_ct": bundle})
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    with browser.hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.passkey SET removed_at = %s WHERE credential_id = %s",
            (browser.hub.clock(), account.credential_id),
        )
    second = browser.login(other)
    # The session of `account` is revoked now; the slot of its removed passkey cannot be written by anybody.
    response = browser.client(second).put(
        f"/v1/me/bundles/{passkey_slot(account.credential_id)}", json={"bundle_ct": bundle}
    )
    assert response.json() == {"error": "forbidden"}
    assert rows(browser.hub, "SELECT count(*) FROM hub.identity_bundle WHERE slot LIKE 'passkey:%%'") == [(0,)]


def test_a_passkey_slot_takes_no_recovery_id(account: Account, client: SignedClient) -> None:
    slot = passkey_slot(account.credential_id)
    body = {"bundle_ct": b64url_encode(bytes(40)), "new_recovery_id": recovery_id(generate_recovery_code())}
    response = client.put(f"/v1/me/bundles/{slot}", json=body)
    assert (response.status_code, response.json()) == (400, {"error": "request_invalid"})


@pytest.mark.parametrize("slot", ["passkey:AB", "other", "passkey:"], ids=["non-canonical", "name", "empty"])
def test_a_malformed_slot_is_refused(client: SignedClient, slot: str) -> None:
    response = client.put(
        f"/v1/me/bundles/{slot}", json={"bundle_ct": b64url_encode(bytes(40))}, extensions=NO_CONTRACT
    )
    assert (response.status_code, response.json()) == (400, {"error": "request_invalid"})


def finished_recovery(hub: HubHarness, account: Account, *, passkey: bool = True, proof: bool = False) -> None:
    """A recovery ceremony of the account, used one second later than the recovery slot was written, and the passkey
    its finish added (HUB-11 creates it in `recovery/finish` and `register/finish` uses it). `passkey=False`: a finish
    that added nothing; `proof=True`: the proof ceremony of `recovery/begin` (with `hub_value`) instead."""
    hub.clock.advance(1)
    now = hub.clock()
    user = uuid.UUID(account.user_id)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.ceremony (id, kind, challenge, user_id, hub_value, created_at, expires_at, used_at)"
            " VALUES (%s, 'recovery', %s, %s, %s, %s, %s, %s)",
            (uuid7(now), bytes(32), user, bytes(32) if proof else None, now, now + timedelta(seconds=300), now),
        )
        if passkey:
            connection.execute(
                "INSERT INTO hub.passkey (credential_id, user_id, public_key, sign_count, transports, created_at,"
                " last_used_at) VALUES (%s, %s, %s, 0, '{}', %s, %s)",
                (secrets.token_bytes(16), user, b"\x04" + bytes(64), now, now),
            )
    hub.clock.advance(1)


def test_the_recovery_slot_is_replaced_once_after_a_recovery(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    hub = browser.hub
    code = generate_recovery_code()
    bundle = seal_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, account.identity)
    body = {"bundle_ct": bundle, "new_recovery_id": recovery_id(code)}
    finished_recovery(hub, account)
    response = client.put("/v1/me/bundles/recovery", json=body)
    assert (response.status_code, response.json()) == (200, {})
    assert rows(hub, "SELECT recovery_id FROM hub.user_identity") == [(recovery_id(code),)]
    assert client.get("/v1/me/bundles/recovery").json() == {"bundle_ct": bundle}
    # One replacement per finished recovery.
    again = client.put("/v1/me/bundles/recovery", json=body)
    assert (again.status_code, again.json()) == (403, {"error": "forbidden"})
    # A further recovery allows the next one; the own current recovery ID is no conflict.
    finished_recovery(hub, account)
    assert client.put("/v1/me/bundles/recovery", json=body).status_code == 200


@pytest.mark.parametrize("ceremony", [{"passkey": False}, {"proof": True}], ids=["nothing-added", "proof-ceremony"])
def test_only_a_finished_recovery_allows_the_replacement(
    browser: FakeBrowser, account: Account, client: SignedClient, ceremony: dict[str, bool]
) -> None:
    """KOR-01 (review of A-14): a recovery ceremony whose finish added no passkey, or the proof ceremony, is no
    finished recovery."""
    finished_recovery(browser.hub, account, **ceremony)
    body = {"bundle_ct": b64url_encode(bytes(40)), "new_recovery_id": recovery_id(generate_recovery_code())}
    response = client.put("/v1/me/bundles/recovery", json=body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


def test_the_recovery_slot_is_not_replaced_without_a_recovery(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    # A stolen session cannot plant a recovery code of its own or destroy the recovery bundle.
    body = {"bundle_ct": b64url_encode(bytes(40)), "new_recovery_id": recovery_id(generate_recovery_code())}
    response = client.put("/v1/me/bundles/recovery", json=body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert rows(browser.hub, "SELECT recovery_id FROM hub.user_identity") == [(recovery_id(account.recovery_code),)]
    assert client.get("/v1/me/bundles/recovery").status_code == 200


def test_the_recovery_slot_needs_a_new_recovery_id(client: SignedClient) -> None:
    response = client.put("/v1/me/bundles/recovery", json={"bundle_ct": b64url_encode(bytes(40))})
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/new_recovery_id"]}},
    )


def test_a_recovery_id_of_another_user_is_a_conflict(
    browser: FakeBrowser, account: Account, client: SignedClient
) -> None:
    other = browser.register(seed_invite(browser.hub))
    finished_recovery(browser.hub, account)
    body = {"bundle_ct": b64url_encode(bytes(40)), "new_recovery_id": recovery_id(other.recovery_code)}
    response = client.put("/v1/me/bundles/recovery", json=body)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert rows(
        browser.hub, "SELECT recovery_id FROM hub.user_identity WHERE user_id = %s", uuid.UUID(account.user_id)
    ) == [(recovery_id(account.recovery_code),)]


# --- settings ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("access", "model"),
    [
        ("auto", None),
        ("auto", "claude-opus-5-5"),
        ("auto", "deepseek/deepseek-v4-pro"),
        ("platform", "deepseek/deepseek-v4.1-flash-1017"),
        ("api_key", "claude-sonnet-5-5"),
        ("subscription", "claude-haiku-5-5"),
        ("subscription", None),
    ],
)
def test_settings_are_stored(browser: FakeBrowser, client: SignedClient, access: str, model: str | None) -> None:
    settings = {"model_access": access, "model_id": model, "locale": "de"}
    response = client.put("/v1/me/settings", json=settings)
    stored = {**settings, "fallback": DEFAULT_FALLBACK}
    assert (response.status_code, response.json()) == (200, stored)
    assert client.get("/v1/me").json()["settings"] == stored
    assert rows(browser.hub, "SELECT updated_at FROM hub.user_settings") == [(browser.hub.clock(),)]
    # the new values are audited (metadata the hub stores in clear, ADR-063 section 2)
    audited = {**settings, **DEFAULT_FALLBACK}
    assert rows(browser.hub, "SELECT detail FROM hub.audit WHERE action = 'settings.put'") == [(audited,)]


def test_the_fallback_settings_are_stored_and_kept(browser: FakeBrowser, client: SignedClient) -> None:
    """KOR-50, ADR-080 section 1: `fallback` is optional; without it the stored values stay."""
    chosen = {"own_to_dfx": True, "operator_to_platform": False}
    settings = {"model_access": "auto", "model_id": None, "locale": "de", "fallback": chosen}
    assert client.put("/v1/me/settings", json=settings).json() == settings
    assert client.get("/v1/me").json()["settings"]["fallback"] == chosen
    later = {"model_access": "subscription", "model_id": None, "locale": "en"}
    assert client.put("/v1/me/settings", json=later).json() == {**later, "fallback": chosen}
    details = rows(browser.hub, "SELECT detail FROM hub.audit WHERE action = 'settings.put' ORDER BY seq")
    assert [detail[0]["own_to_dfx"] for detail in details] == [True, True]
    back = {**later, "fallback": DEFAULT_FALLBACK}
    assert client.put("/v1/me/settings", json=back).json() == back
    # a new row takes the given values
    fresh = {"model_access": "platform", "model_id": None, "locale": "en", "fallback": chosen}
    assert browser.hub.browser().put("/v1/me/settings", json=fresh).json() == fresh


@pytest.mark.parametrize(
    ("access", "model"),
    [("platform", "claude-opus-5-5"), ("api_key", "deepseek/deepseek-v4-pro"), ("auto", "unknown-model")],
)
def test_a_model_outside_the_catalog_is_refused(client: SignedClient, access: str, model: str) -> None:
    response = client.put("/v1/me/settings", json={"model_access": access, "model_id": model, "locale": "en"})
    assert (response.status_code, response.json()) == (
        400,
        {"error": "params_invalid", "detail": {"pointers": ["/model_id"]}},
    )


@pytest.mark.parametrize(
    "body",
    [
        {"model_access": "auto", "model_id": None, "locale": "fr"},
        {"model_access": "other", "model_id": None, "locale": "en"},
        {"model_access": "auto", "locale": "en"},
        {"model_access": "auto", "model_id": " bad", "locale": "en"},
        {"model_access": "auto", "model_id": None, "locale": "en", "theme": "dark"},
        {"model_access": "auto", "model_id": None, "locale": "en", "fallback": {"own_to_dfx": True}},
        {"model_access": "auto", "model_id": None, "locale": "en", "fallback": None},
    ],
    ids=["locale", "access", "missing", "model-form", "unknown", "fallback-partial", "fallback-null"],
)
def test_malformed_settings_are_refused(client: SignedClient, body: dict[str, Any]) -> None:
    response = client.put("/v1/me/settings", json=body, extensions=NO_CONTRACT)
    assert response.status_code == 400
    assert response.json()["error"] == "request_invalid"


def test_settings_of_a_user_without_a_settings_row_are_created(browser: FakeBrowser) -> None:
    settings = {"model_access": "platform", "model_id": None, "locale": "en"}
    assert browser.hub.browser().put("/v1/me/settings", json=settings).json() == {
        **settings,
        "fallback": DEFAULT_FALLBACK,
    }


# --- zero knowledge ---------------------------------------------------------------------------------------------------


def test_no_secret_reaches_the_database(browser: FakeBrowser, account: Account, client: SignedClient) -> None:
    slot = passkey_slot(account.credential_id)
    assert client.put(f"/v1/me/bundles/{slot}", json={"bundle_ct": browser.passkey_bundle(account)}).status_code == 200
    browser.login(account)
    secrets_of_the_user = {
        "kem": x25519_private_raw(account.identity.kem),
        "sig": ed25519_seed(account.identity.sig),
        "recovery code": account.recovery_code,
        "credential key": account.cred_key,
        "prf": browser.authenticator.prf(account.credential_id),
    }
    with browser.hub.pool.connection() as connection:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'hub'"
            ).fetchall()
        ]
        dump = b"".join(
            str(row).encode("utf-8") + b"".join(bytes(value) for value in row if isinstance(value, bytes | memoryview))
            for table in tables
            for row in connection.execute(f"SELECT * FROM hub.{table}").fetchall()
        )
    for name, secret in secrets_of_the_user.items():
        assert secret not in dump, name
        assert b64url_encode(secret).encode("ascii") not in dump, name
