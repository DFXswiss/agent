"""U-HUB-passkeys (recovery): `POST /v1/recovery/begin|finish` (03-api.md section 4.1, 04-crypto.md section 5.5).

The recovery code unlocks the recovery bundle, the identity proves itself over the 32-byte challenge, a new passkey is
registered through the ceremony of HUB-03 and the recovery slot is replaced with a new code exactly once. Wrong proofs,
expired and reused challenges are refused; rate limits 5 per hour and source, 20 per day and recovery ID.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.auth import recovery
from agent_hub.crypto.bundle import RECOVERY_SLOT, open_bundle, seal_bundle
from agent_hub.crypto.kdf import kek_from_recovery_code
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.recovery import generate_recovery_code, recovery_id, sign_proof
from tests.platform.hub.auth.authenticator import Account, FakeBrowser, seed_invite
from tests.platform.hub.conftest import HubHarness

NO_CONTRACT = {"contract": False}


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


def recovery_audit(hub: HubHarness) -> list[tuple[Any, ...]]:
    return rows(
        hub,
        "SELECT result, actor_kind, target_kind, target_id, detail FROM hub.audit WHERE action = 'auth.recovery'"
        " ORDER BY seq",
    )


@pytest.fixture
def account(browser: FakeBrowser) -> Account:
    return browser.register(seed_invite(browser.hub))


@pytest.fixture
def generous(hub: HubHarness) -> None:
    """A per-source limit high enough for the per-recovery-ID tests (the test client has one source address)."""
    hub.app.state.recovery_limit = recovery.SourceLimit(limit=1000)


def begin(hub: HubHarness, code: bytes) -> Any:
    return hub.anon.post("/v1/recovery/begin", json={"recovery_id": recovery_id(code)})


def proof_body(account: Account, started: dict[str, Any], identity: Identity | None = None) -> dict[str, Any]:
    challenge = b64url_decode(started["challenge"]) or b""
    signer = (identity or account.identity).sig
    return {
        "user_id": started["user_id"],
        "challenge": started["challenge"],
        "proof": sign_proof(signer, started["user_id"], challenge),
    }


def test_recovery_registers_a_new_passkey_and_replaces_the_code(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    hub.clock.advance(60)  # the recovery happens after the registration wrote the recovery slot
    started = begin(hub, account.recovery_code)
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["user_id"] == account.user_id
    assert len(b64url_decode(body["challenge"]) or b"") == 32
    # the browser opens the recovery bundle with KEK_rec
    identity = open_bundle(
        kek_from_recovery_code(account.recovery_code), account.user_id, RECOVERY_SLOT, body["bundle_ct"]
    )
    assert identity.sig_public() == account.identity.sig_public()
    finished = hub.anon.post("/v1/recovery/finish", json=proof_body(account, body, identity))
    assert finished.status_code == 200, finished.text
    options = finished.json()["options"]
    assert (b64url_decode(options["user"]["id"]) or b"") == uuid.UUID(account.user_id).bytes
    assert [item["id"] for item in options["excludeCredentials"]] == [b64url_encode(account.credential_id)]
    # a new passkey through register/finish (second form)
    credential = browser.authenticator.create(options)
    registered = hub.anon.post(
        "/v1/passkeys/register/finish", json={"ceremony_id": finished.json()["ceremony_id"], "credential": credential}
    )
    assert registered.status_code == 200, registered.text
    new_account = Account(
        account.user_id, b64url_decode(credential["id"]) or b"", identity, account.recovery_code, account.cred_key
    )
    session = browser.login(new_account)
    client = browser.client(session)
    # a new recovery code replaces the slot, once
    code = generate_recovery_code()
    bundle = seal_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, identity)
    put = client.put("/v1/me/bundles/recovery", json={"bundle_ct": bundle, "new_recovery_id": recovery_id(code)})
    assert put.status_code == 200, put.text
    again = client.put("/v1/me/bundles/recovery", json={"bundle_ct": bundle, "new_recovery_id": recovery_id(code)})
    assert (again.status_code, again.json()) == (403, {"error": "forbidden"})
    assert begin(hub, account.recovery_code).status_code == 404
    assert begin(hub, code).status_code == 200
    audit = recovery_audit(hub)
    assert audit[0] == ("ok", "anonymous", "user", account.user_id, {"step": "begin"})
    assert ("ok", "user", "passkey", credential["id"], {"step": "finish"}) in audit


def test_wrong_proof_is_refused_once_and_allows_no_replacement(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    hub.clock.advance(60)
    started = begin(hub, account.recovery_code).json()
    wrong = proof_body(account, started, Identity.generate())
    response = hub.anon.post("/v1/recovery/finish", json=wrong)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    assert recovery_audit(hub)[-1] == ("denied", "anonymous", "user", account.user_id, {"step": "finish"})
    # the challenge had its one attempt
    right = hub.anon.post("/v1/recovery/finish", json=proof_body(account, started))
    assert (right.status_code, right.json()) == (400, {"error": "ceremony_invalid"})
    # no used recovery ceremony: a session of the user cannot replace the recovery code
    client = browser.client(browser.login(account))
    code = generate_recovery_code()
    bundle = seal_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, account.identity)
    put = client.put("/v1/me/bundles/recovery", json={"bundle_ct": bundle, "new_recovery_id": recovery_id(code)})
    assert (put.status_code, put.json()) == (403, {"error": "forbidden"})


def test_a_challenge_is_used_once(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    started = begin(hub, account.recovery_code).json()
    body = proof_body(account, started)
    assert hub.anon.post("/v1/recovery/finish", json=body).status_code == 200
    again = hub.anon.post("/v1/recovery/finish", json=body)
    assert (again.status_code, again.json()) == (400, {"error": "ceremony_invalid"})


def test_expired_challenge_and_other_user_are_refused(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    started = begin(hub, account.recovery_code).json()
    other = {**proof_body(account, started), "user_id": str(uuid.UUID(int=uuid.UUID(account.user_id).int ^ 1))}
    response = hub.anon.post("/v1/recovery/finish", json=other)
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})
    hub.clock.advance(300)
    response = hub.anon.post("/v1/recovery/finish", json=proof_body(account, started))
    assert (response.status_code, response.json()) == (400, {"error": "ceremony_invalid"})


def test_unknown_or_disabled_recovery_id_is_not_found(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    response = begin(hub, generate_recovery_code())
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", (hub.clock(), account.user_id))
    response = begin(hub, account.recovery_code)
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_begin_is_limited_per_source(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    assert begin(hub, generate_recovery_code()).status_code == 404  # unknown IDs count as well
    for _ in range(4):
        assert begin(hub, account.recovery_code).status_code == 200
    limited = begin(hub, account.recovery_code)
    assert (limited.status_code, limited.json()) == (429, {"error": "rate_limited"})
    assert limited.headers["Retry-After"] == "3600"
    hub.clock.advance(1800)
    assert begin(hub, account.recovery_code).headers["Retry-After"] == "1800"
    hub.clock.advance(1800)
    assert begin(hub, account.recovery_code).status_code == 200


@pytest.mark.usefixtures("generous")
def test_begin_is_limited_per_recovery_id(browser: FakeBrowser, account: Account) -> None:
    hub = browser.hub
    other = browser.register(seed_invite(hub))
    for _ in range(20):
        assert begin(hub, account.recovery_code).status_code == 200
        hub.clock.advance(60)
    limited = begin(hub, account.recovery_code)
    assert (limited.status_code, limited.json()) == (429, {"error": "rate_limited"})
    assert limited.headers["Retry-After"] == str(24 * 3600 - 20 * 60)
    assert begin(hub, other.recovery_code).status_code == 200
    hub.clock.advance(24 * 3600 - 20 * 60)
    assert begin(hub, account.recovery_code).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"recovery_id": "short"},
        {"recovery_id": "A" * 22, "extra": 1},
    ],
)
def test_begin_refuses_malformed_bodies(hub: HubHarness, body: dict[str, Any]) -> None:
    response = hub.anon.post("/v1/recovery/begin", json=body, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_finish_refuses_malformed_bodies(hub: HubHarness) -> None:
    body = {"user_id": "not-a-uuid", "challenge": "A" * 43, "proof": "A" * 86}
    response = hub.anon.post("/v1/recovery/finish", json=body, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_source_limit_window_and_eviction() -> None:
    limit = recovery.SourceLimit(limit=2, window=10, max_sources=2)
    start = datetime(2026, 11, 21, tzinfo=UTC)
    assert limit.hit("a", start) is None
    assert limit.hit("a", start + timedelta(seconds=1)) is None
    assert limit.hit("a", start + timedelta(seconds=2)) == 8
    assert limit.hit("a", start + timedelta(seconds=10)) is None  # the first attempt left the window
    assert limit.hit("b", start) is None
    assert limit.hit("c", start) is None  # evicts "a", the least recently used source
    assert limit.hit("a", start + timedelta(seconds=10)) is None
    assert limit.hit("a", start + timedelta(seconds=10)) is None
    assert limit.hit("a", start + timedelta(seconds=10)) == 10
