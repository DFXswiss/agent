"""The hub, coexistence with GitHub sign-in and the storage (PASSKEY-PROTOCOL.md sections 11, 12 and
13.6, without the cases that concern pages). The configuration cases of section 13.6 are in
`tests/test_config.py`."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from agent_core.db import Store
from tests.conftest import sign_in as github_sign_in
from tests.passkey_hub import (
    BEGIN,
    COMMANDS,
    FINISH,
    INVITES,
    NOW,
    OPEN,
    REGISTER,
    SESSION_END,
    WHOAMI,
    FailingGitHub,
    HubFactory,
    PasskeyHub,
    b64,
    command_text,
    fmt,
    hubs,  # noqa: F401
    private_key_texts,
    session_signature,
)
from tests.webauthn_authenticator import SoftAuthenticator

PASSKEY_TABLES = (
    "passkey_invite",
    "passkey_registration",
    "passkey_sign_in",
    "passkey_session",
    "passkey_counter",
    "command_sequence",
)


def scenario(hub: PasskeyHub, hubs: HubFactory) -> tuple[list[tuple[str, int, Any]], dict[str, Any]]:  # noqa: F811
    """Every route of this document once, in the order a person uses them. Returns each route with
    its status and the parts of its body that do not depend on randomness, and what was made."""
    seen: list[tuple[str, int, Any]] = []
    admin = hub.sign_in(hubs.passkeys["admin"])
    seen.append((FINISH, 200, admin.answer["passkey"]))
    whoami = hub.send(hub.session_wire(admin))
    seen.append((WHOAMI, whoami.status, whoami.json()["kind"]))
    invite = hub.create_invite(admin)
    seen.append((INVITES, 201, invite["handle"]))
    opened = hub.open(invite["link"])
    seen.append((OPEN, opened.status, opened.json()["rp_id"]))
    passkey = SoftAuthenticator()
    registered = hub.register(passkey, opened)
    seen.append((REGISTER, registered.status, registered.json()["handle"]))
    listed = hub.invites(admin)
    seen.append(("GET " + INVITES, 200, listed[0]["status"]))
    staff = hub.sign_in(hubs.passkeys["staff"])
    text = command_text(staff.id, "example-staff-laptop-1", "noop", 1, fmt(NOW))
    body = {
        "command": {"target": "example-staff-laptop-1", "action": "noop", "sequence": 1, "time": fmt(NOW), "content_hash": "-"},
        "signature": b64(session_signature(staff.private, text)),
    }
    sent = hub.send(hub.session_wire(staff, method="POST", target=COMMANDS, body=json.dumps(body).encode()))
    seen.append((COMMANDS, sent.status, sent.json()["delivered"]))
    ended = hub.send(hub.session_wire(staff, method="POST", target=SESSION_END))
    seen.append((SESSION_END, ended.status, ended.json()["ended"]))
    refused = hub.post(BEGIN, {"session_public_key": b64(staff.public)})
    seen.append((BEGIN, refused.status, refused.json()))
    return seen, {"admin": admin, "staff": staff, "invite": invite, "passkey": passkey}


def test_every_passkey_route_answers_the_same_with_a_github_fake_that_fails_on_every_call(tmp_path: Any) -> None:
    answers = []
    failing = FailingGitHub()
    for index, github in enumerate((None, failing)):
        (tmp_path / str(index)).mkdir()
        factory = HubFactory(tmp_path / str(index))
        try:
            hub = factory.build(github=github)
            answers.append(scenario(hub, factory)[0])
        finally:
            factory.close()
    assert answers[0] == answers[1]
    assert [status for _, status, _ in answers[1]] == [200, 200, 201, 200, 200, 200, 200, 200, 409]
    assert failing.calls == []


def test_a_github_cookie_session_gets_signature_missing_on_every_passkey_route(hubs: HubFactory) -> None:  # noqa: F811
    hub = hubs.build()
    with TestClient(hub.app) as client:
        github_sign_in(client, "code-example-staff")
        assert client.get("/auth/me").status_code == 200
        for method, path in (
            ("GET", WHOAMI),
            ("GET", INVITES),
            ("POST", INVITES),
            ("POST", SESSION_END),
            ("POST", COMMANDS),
        ):
            answer = client.request(method, path, json={})
            assert answer.status_code == 401
            assert answer.json() == {"error": "signature_missing"}


def test_a_passkey_session_gets_401_on_the_cookie_routes(hubs: HubFactory) -> None:  # noqa: F811
    hub = hubs.build()
    session = hub.sign_in(hubs.passkeys["staff"])
    for method, target, body in (
        ("GET", "/auth/me", b""),
        ("GET", "/api/state", b""),
        ("POST", "/api/session/control?id=example-session", b'{"action":"stop"}'),
    ):
        # The control request carries the query `id` and a JSON object body, because without them
        # the application answers 422 before the sign-in check (section 13.6).
        wire = hub.session_wire(
            session, method=method, target=target, body=body, extra_headers=((b"content-type", b"application/json"),)
        )
        answer = hub.send(wire)
        assert answer.status == 401, (target, answer.body)
        assert answer.header("agent-hub-signature") is None


def test_no_passkey_route_reads_or_writes_the_cookie_session(hubs: HubFactory) -> None:  # noqa: F811
    hub = hubs.build()
    with TestClient(hub.app) as client:
        github_sign_in(client, "code-example-staff")
        assert client.cookies.get("session")
        with_cookie = [client.post(path, content=b"{}").json() for path in (OPEN, REGISTER, BEGIN, FINISH)]
        assert client.get("/auth/me").json()["login"] == "example-staff"
        client.cookies.clear()
        without_cookie = [client.post(path, content=b"{}").json() for path in (OPEN, REGISTER, BEGIN, FINISH)]
        assert client.get("/auth/me").status_code == 401
    assert with_cookie == without_cookie == [{"error": "request_invalid"}] * 4
    session = hub.sign_in(hubs.passkeys["staff"])
    assert session.answer["handle"] == "example-staff"


def test_the_tables_of_section_12_are_created_at_start(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "start.sqlite"))
    try:
        names = {row["name"] for row in store.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        store.close()
    assert set(PASSKEY_TABLES) <= names
    columns = {
        table: [row[1] for row in sqlite3.connect(str(tmp_path / "start.sqlite")).execute(f"PRAGMA table_info({table})")]
        for table in PASSKEY_TABLES
    }
    assert columns == {
        "passkey_invite": [
            "id", "token_hash", "handle", "team", "created", "expires", "created_by", "bootstrap",
            "creation_request", "opened", "received", "credential_id", "public_key", "proposed_id",
        ],
        "passkey_registration": ["id", "invite_id", "challenge", "user_id", "created", "finished"],
        "passkey_sign_in": ["id", "statement", "session_public_key", "created"],
        "passkey_session": [
            "id", "passkey_id", "handle", "credential_id", "passkey_public_key", "session_public_key",
            "statement", "authenticator_data", "client_data_json", "signature", "created", "expires", "ended",
        ],
        "passkey_counter": ["passkey_id", "sign_count"],
        "command_sequence": ["sender", "last_sequence"],
    }


def test_after_a_full_invite_and_sign_in_no_row_holds_the_invite_token_or_a_private_key(hubs: HubFactory) -> None:  # noqa: F811
    hub = hubs.build()
    _, made = scenario(hub, hubs)
    token = made["invite"]["link"].split("#", 1)[1]
    forbidden = [token, token.encode().hex()]
    for session in (made["admin"], made["staff"]):
        forbidden += private_key_texts(session.private)
    for passkey in (made["passkey"], hubs.passkeys["admin"], hubs.passkeys["staff"]):
        forbidden += private_key_texts(passkey.credentials[0].private_key)
    tables = [row["name"] for row in hub.store.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
    dump = json.dumps({table: hub.rows(f"SELECT * FROM {table}") for table in tables})
    for value in forbidden:
        assert value not in dump
    # The search reaches the session rows and the creation request.
    assert made["staff"].id in dump and made["admin"].id in dump
