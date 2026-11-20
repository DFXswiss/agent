"""The HTTP routes of the passkey step on the hub (PASSKEY-PROTOCOL.md sections 5 to 8).

Routes that carry no registered key (decision 28), each answering JSON:

- `POST /auth/invite/open` (section 5.4) and `POST /auth/invite/finish` (section 5.5)
- `POST /auth/passkey/begin` (section 6.1) and `POST /auth/passkey/finish` (section 6.3)

Routes under `/api/key/`, which run after checks 1 to 10 of section 7.1 and whose answers the key
service signs:

- `POST /api/key/invites` and `GET /api/key/invites` (sections 5.1 and 5.2)
- `POST /api/key/session/end` (section 7.3)
- `POST /api/key/commands` (section 8.2)

`GET /api/key/whoami` stays in `agent_core.app`; its answer for a session key is
`session_whoami_body` (section 7.2).

None of these routes reads or writes the cookie session, reads the OAuth token, the `device` table
or `teams.yaml` logins as identity, or calls GitHub (section 11): the identity is the keys-file
entry alone. No route logs a token, a challenge, a signature, a body or a public key.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ..keys import KeyEntry, KeyRing, device_key
from ..signing import (
    LOG,
    Clock,
    KeyRequest,
    KeyRoutes,
    SessionKey,
    b64url_decode,
    b64url_encode,
    format_time,
    sha256_hex,
)
from ..teams import Roster
from .certificate import Certificate, build_statement, statement_challenge, verify_certificate
from .command import (
    check_command_time,
    command_text,
    parse_command_body,
    verify_command_signature,
)
from .encoding import (
    ANONYMOUS_BODY_LIMIT,
    CEREMONY_ID_BYTES,
    CEREMONY_SECONDS,
    HUB_VALUE_BYTES,
    INVITE_ID_RANDOM_BYTES,
    INVITE_TOKEN_BYTES,
    REGISTRATION_CHALLENGE_BYTES,
    SESSION_KEY_ISSUE_SECONDS,
    SESSION_KEY_MAX_SECONDS,
    USER_ID_BYTES,
    JsonInvalid,
    ceremony_id,
    fingerprint,
    invite_id,
    invite_token,
    p256_point_valid,
    parse_json,
)
from .errors import (
    COMMAND_INVALID,
    COMMAND_SIGNATURE_INVALID,
    COMMAND_TIME_SKEW,
    PasskeyRefusal,
)
from .invites import invite_entry, invite_link, parse_invite_body, token_hash
from .relying_party import RelyingParty
from .storage import CREATED, SESSION_KEY_KNOWN, NewInvite, NewSession, PasskeyStore
from .webauthn import verify_registration

PATH_INVITE_OPEN = "/auth/invite/open"
PATH_INVITE_FINISH = "/auth/invite/finish"
PATH_PASSKEY_BEGIN = "/auth/passkey/begin"
PATH_PASSKEY_FINISH = "/auth/passkey/finish"
PATH_INVITES = "/api/key/invites"
PATH_SESSION_END = "/api/key/session/end"
PATH_COMMANDS = "/api/key/commands"

INVITE_SCOPE = "keys:invite"
COMMAND_SCOPE = "record:write"
RP_NAME = "agent-core"
TIMEOUT_MS = CEREMONY_SECONDS * 1000

# The failure values of the routes of this module (the values of the library are in `errors`).
REQUEST_INVALID = "request_invalid"
REGISTRATION_UNKNOWN = "registration_unknown"
REGISTRATION_EXPIRED = "registration_expired"
CREDENTIAL_KNOWN = "credential_known"
CHALLENGE_UNKNOWN = "challenge_unknown"
CHALLENGE_EXPIRED = "challenge_expired"
CREDENTIAL_UNKNOWN = "credential_unknown"
KEY_REVOKED = "key_revoked"
INVITE_INVALID = "invite_invalid"
INVITE_LIMIT = "invite_limit"
SESSION_KEY_REQUIRED = "session_key_required"
SENDER_NOT_ALLOWED = "sender_not_allowed"
TARGET_NOT_ALLOWED = "target_not_allowed"
SEQUENCE_NOT_HIGHER = "sequence_not_higher"
INTERNAL = "internal"

OPEN_FIELDS = frozenset({"token"})
REGISTRATION_FIELDS = frozenset({"registration", "credential_id", "client_data_json", "attestation_object"})
BEGIN_FIELDS = frozenset({"session_public_key"})
ASSERTION_FIELDS = frozenset({"sign_in", "credential_id", "authenticator_data", "client_data_json", "signature"})
SIGN_IN_FIELDS = ASSERTION_FIELDS | {"user_handle"}
# The headers of a signed request that the stored creation request of an invite keeps (section 5.1).
SIGNED_HEADERS = ("Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature")
# C2 to C4 of section 8.2.
COMMAND_STATUS = {COMMAND_INVALID: 400, COMMAND_TIME_SKEW: 400, COMMAND_SIGNATURE_INVALID: 403}

Answer = tuple[int, dict[str, Any]]


def _error(status: int, error: str) -> Answer:
    return status, {"error": error}


def _json(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


async def _anonymous_body(request: Request) -> bytes | None:
    """The body of a route without a registered key: no `Content-Encoding` header with any value,
    also `identity` or an empty one (decision 47), and at most `ANONYMOUS_BODY_LIMIT` bytes; reading
    stops as soon as the limit is passed. None for `request_invalid`."""
    if "content-encoding" in request.headers:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > ANONYMOUS_BODY_LIMIT:
            return None
    return bytes(body)


async def _fields(request: Request, names: frozenset[str]) -> dict[str, Any] | None:
    """The body as one JSON object (section 2) with exactly the given members; None otherwise."""
    raw = await _anonymous_body(request)
    if raw is None:
        return None
    try:
        data = parse_json(raw)
    except JsonInvalid:
        return None
    if type(data) is not dict or set(data) != names:
        return None
    return data


def _decoded(fields: dict[str, Any], names: frozenset[str]) -> dict[str, bytes] | None:
    """The named members, each canonical base64url text, decoded; None when one is not."""
    decoded = {name: b64url_decode(fields[name]) for name in names}
    if any(value is None for value in decoded.values()):
        return None
    return decoded  # type: ignore[return-value]


def session_whoami_body(entry: KeyEntry, session: SessionKey, hub_key: str) -> dict[str, Any]:
    """The answer of `GET /api/key/whoami` for a session key (section 7.2), fields in order."""
    return {
        "key": session.id,
        "kind": "session",
        "principal": entry.principal,
        "handle": entry.handle,
        "team": entry.team,
        "scopes": list(entry.scopes),
        "hub_key": hub_key,
        "passkey": entry.id,
        "expires": format_time(session.expires),
    }


class PasskeyRoutes:
    """The routes of this module for one hub."""

    def __init__(
        self,
        *,
        store: PasskeyStore,
        keys: KeyRing,
        roster: Roster,
        relying_party: RelyingParty,
        host: str,
        hub_key: str,
        clock: Clock,
    ) -> None:
        self.store = store
        self.keys = keys
        self.roster = roster
        self.relying_party = relying_party
        self.host = host
        self.hub_key = hub_key
        self.clock = clock

    def register(self, app: FastAPI, routes: KeyRoutes) -> None:
        for path, handler in (
            (PATH_INVITE_OPEN, self.invite_open),
            (PATH_INVITE_FINISH, self.invite_finish),
            (PATH_PASSKEY_BEGIN, self.passkey_begin),
            (PATH_PASSKEY_FINISH, self.passkey_finish),
        ):
            app.add_api_route(path, _anonymous_route(path, handler), methods=["POST"])
        routes.add("POST", PATH_INVITES, self.create_invite, scope=INVITE_SCOPE, subject=None)
        routes.add("GET", PATH_INVITES, self.list_invites, scope=INVITE_SCOPE, subject=None)
        routes.add("POST", PATH_SESSION_END, self.end_session, scope=None, subject=None)
        routes.add("POST", PATH_COMMANDS, self.command, scope=COMMAND_SCOPE, subject=None)

    # --- invites (section 5) ---------------------------------------------------------------------

    async def invite_open(self, request: Request) -> Answer:
        """O1 to O4 of section 5.4, then the registration."""
        fields = await _fields(request, OPEN_FIELDS)
        token = None if fields is None else b64url_decode(fields["token"])
        if token is None or len(token) != INVITE_TOKEN_BYTES:
            return _error(400, REQUEST_INVALID)
        challenge = secrets.token_bytes(REGISTRATION_CHALLENGE_BYTES)
        user_id = secrets.token_bytes(USER_ID_BYTES)
        registration = ceremony_id(secrets.token_bytes(CEREMONY_ID_BYTES))
        outcome = await asyncio.to_thread(
            self.store.open_invite,
            token_hash(token),
            self.clock,
            registration_id=registration,
            challenge=b64url_encode(challenge),
            user_id=b64url_encode(user_id),
        )
        if isinstance(outcome, str):
            return _error(401, outcome)
        if outcome.bootstrap:
            LOG.info("invite bootstrap opened %s %s", outcome.id, outcome.handle)
        excluded = [
            b64url_encode(entry.credential_id)
            for entry in self.keys.entries.values()
            if entry.handle == outcome.handle and entry.credential_id is not None
        ]
        return 200, {
            "registration": registration,
            "handle": outcome.handle,
            "rp_id": self.relying_party.rp_id,
            "rp_name": RP_NAME,
            "user_id": b64url_encode(user_id),
            "challenge": b64url_encode(challenge),
            "exclude_credentials": excluded,
            "timeout_ms": TIMEOUT_MS,
        }

    async def invite_finish(self, request: Request) -> Answer:
        """R1 to R14 of section 5.5 in this order."""
        fields = await _fields(request, REGISTRATION_FIELDS)
        decoded = None if fields is None else _decoded(fields, REGISTRATION_FIELDS)
        if fields is None or decoded is None:
            return _error(400, REQUEST_INVALID)
        registration = await asyncio.to_thread(
            self.store.consume_registration, fields["registration"], self.clock()
        )
        if registration is None:
            return _error(401, REGISTRATION_UNKNOWN)
        # R3 and the later checks read the clock after R2, so that the wait for the store lies
        # before them (section 12, "Clock").
        now = self.clock()
        if now - registration.opened > CEREMONY_SECONDS or now >= registration.expires:
            return _error(401, REGISTRATION_EXPIRED)
        try:
            received = verify_registration(
                credential_id=decoded["credential_id"],
                client_data_json=decoded["client_data_json"],
                attestation_object=decoded["attestation_object"],
                challenge=registration.challenge,
                relying_party=self.relying_party,
            )
        except PasskeyRefusal as refusal:
            return _error(401, refusal.error)
        proposed = await asyncio.to_thread(
            self.store.receive_invite,
            registration,
            credential_id=received.credential_id,
            public_key=received.public_key,
            keys=self.keys,
            now=now,
        )
        if proposed is None:
            return _error(409, CREDENTIAL_KNOWN)
        fingerprint_text = fingerprint(received.public_key)
        if registration.bootstrap:
            LOG.info(
                "invite bootstrap received %s %s %s",
                registration.invite_id,
                registration.handle,
                fingerprint_text,
            )
        return 200, {"handle": registration.handle, "fingerprint": fingerprint_text}

    async def create_invite(self, request: KeyRequest) -> Answer:
        """I1 and I2 of section 5.1, after checks 9 to 10."""
        fields = parse_invite_body(request.body, self.roster)
        if fields is None:
            return _error(400, INVITE_INVALID)
        now = self.clock()
        token = secrets.token_bytes(INVITE_TOKEN_BYTES)
        invite = NewInvite(
            id=invite_id(secrets.token_bytes(INVITE_ID_RANDOM_BYTES)),
            token_hash=token_hash(token),
            handle=fields.handle,
            team=fields.team,
            created=now,
            expires=now + fields.lifetime_seconds,
            created_by=request.key_id,
            bootstrap=False,
            creation_request=self._creation_request(request),
        )
        outcome = await asyncio.to_thread(self.store.create_invite, invite, now)
        if outcome != CREATED:
            return _error(409, INVITE_LIMIT)
        return 201, {
            "invite": invite.id,
            "handle": invite.handle,
            "team": invite.team,
            "created": format_time(invite.created),
            "expires": format_time(invite.expires),
            "link": invite_link(self.relying_party.origin, invite_token(token)),
            "hub_key": self.hub_key,
        }

    def _creation_request(self, request: KeyRequest) -> str:
        """The complete signed creation request (section 5.1, decision 51): the four `Agent-*`
        headers, the method, the request target, the host line of the signed text and the body
        after removing any `Content-Encoding`, which the signed body hash covers."""
        target = request.raw_path + ("?" + request.query if request.query else "")
        stored = {
            "method": request.method,
            "target": target,
            "host": self.host,
            "headers": {
                name: request.header_values(name.lower())[0].decode("ascii") for name in SIGNED_HEADERS
            },
            "body": b64url_encode(request.body),
        }
        return json.dumps(stored, separators=(",", ":"))

    async def list_invites(self, request: KeyRequest) -> Answer:
        """Section 5.2; the query is ignored."""
        now = self.clock()
        invites = await asyncio.to_thread(self.store.invites)
        return 200, {"invites": [invite_entry(invite, now) for invite in invites], "hub_key": self.hub_key}

    # --- sign-in (section 6) ---------------------------------------------------------------------

    async def passkey_begin(self, request: Request) -> Answer:
        """B1 to B3 of section 6.1, then the statement of section 6.2."""
        fields = await _fields(request, BEGIN_FIELDS)
        session_public_key = None if fields is None else b64url_decode(fields["session_public_key"])
        if session_public_key is None or not p256_point_valid(session_public_key):
            return _error(400, REQUEST_INVALID)
        now = self.clock()
        statement = build_statement(
            origin=self.relying_party.origin,
            session_public_key=session_public_key,
            hub_value=secrets.token_bytes(HUB_VALUE_BYTES),
            expires=now + SESSION_KEY_ISSUE_SECONDS,
        )
        sign_in = ceremony_id(secrets.token_bytes(CEREMONY_ID_BYTES))
        refusal = await asyncio.to_thread(
            self.store.begin_sign_in,
            sign_in,
            statement=b64url_encode(statement),
            session_public_key=b64url_encode(session_public_key),
            now=now,
        )
        if refusal is not None:
            return _error(409 if refusal == SESSION_KEY_KNOWN else 429, refusal)
        return 200, {
            "sign_in": sign_in,
            "statement": statement.decode("utf-8"),
            "challenge": b64url_encode(statement_challenge(statement)),
            "rp_id": self.relying_party.rp_id,
            "timeout_ms": TIMEOUT_MS,
        }

    def _staff_entry(self, credential_id: bytes) -> KeyEntry | None:
        """S4: the `staff` entry of the keys file with this credential id. The keys file is the
        one the hub loaded at its start; a key received through an invite is not in it."""
        for entry in self.keys.entries.values():
            if entry.principal == "staff" and entry.credential_id == credential_id:
                return entry
        return None

    async def passkey_finish(self, request: Request) -> Answer:
        """S1 to S7 of section 6.3 in this order, then the session."""
        fields = await _fields(request, SIGN_IN_FIELDS)
        decoded = None if fields is None else _decoded(fields, ASSERTION_FIELDS)
        if (
            fields is None
            or decoded is None
            or (fields["user_handle"] is not None and b64url_decode(fields["user_handle"]) is None)
        ):
            return _error(400, REQUEST_INVALID)
        pending = await asyncio.to_thread(self.store.take_sign_in, fields["sign_in"])
        if pending is None:
            return _error(401, CHALLENGE_UNKNOWN)
        # S3 and the later checks read the clock after S2, so that the wait for the store lies
        # before them (section 12, "Clock").
        now = self.clock()
        if now - pending.created > CEREMONY_SECONDS:
            return _error(401, CHALLENGE_EXPIRED)
        entry = self._staff_entry(decoded["credential_id"])
        if entry is None:
            return _error(401, CREDENTIAL_UNKNOWN)
        if entry.revoked_at(now):
            return _error(401, KEY_REVOKED)
        try:
            certified = verify_certificate(
                Certificate(
                    statement=pending.statement,
                    credential_id=decoded["credential_id"],
                    authenticator_data=decoded["authenticator_data"],
                    client_data_json=decoded["client_data_json"],
                    signature=decoded["signature"],
                ),
                passkey_public_key=entry.public_key,
                relying_party=self.relying_party,
                now=now,
                max_lifetime=SESSION_KEY_MAX_SECONDS,
            )
            session = NewSession(
                id=certified.session_key_id,
                passkey_id=entry.id,
                handle=entry.handle,
                credential_id=fields["credential_id"],
                passkey_public_key=b64url_encode(entry.public_key),
                session_public_key=b64url_encode(certified.statement.session_public_key),
                statement=b64url_encode(pending.statement),
                authenticator_data=fields["authenticator_data"],
                client_data_json=fields["client_data_json"],
                signature=fields["signature"],
                created=now,
                expires=certified.statement.expires,
            )
            await asyncio.to_thread(self.store.create_session, session, certified.sign_count)
        except PasskeyRefusal as refusal:
            return _error(401, refusal.error)
        return 200, {
            "session_key": session.id,
            "passkey": entry.id,
            "handle": entry.handle,
            "expires": format_time(session.expires),
        }

    # --- sessions and commands (sections 7.3 and 8.2) --------------------------------------------

    async def end_session(self, request: KeyRequest) -> Answer:
        """Section 7.3: a keys-file key gets `session_key_required`; the body is ignored."""
        if request.session is None:
            return _error(403, SESSION_KEY_REQUIRED)
        await asyncio.to_thread(self.store.end_session, request.session.id, self.clock())
        return 200, {"ended": True, "hub_key": self.hub_key}

    async def command(self, request: KeyRequest) -> Answer:
        """C1 to C6 of section 8.2 in this order, after checks 9 to 10."""
        session = request.session
        if session is None:
            return _error(403, SENDER_NOT_ALLOWED)
        now = self.clock()
        try:
            parsed = parse_command_body(request.body, session.id)
            check_command_time(parsed, now)
            verify_command_signature(parsed.command, parsed.signature, session.session_public_key)
        except PasskeyRefusal as refusal:
            return _error(COMMAND_STATUS[refusal.error], refusal.error)
        command = parsed.command
        target = device_key(self.keys, command.target, now)
        if target is None or target.handle != session.handle:
            return _error(403, TARGET_NOT_ALLOWED)
        if not await asyncio.to_thread(self.store.advance_sequence, session.id, command.sequence):
            return _error(409, SEQUENCE_NOT_HIGHER)
        return 200, {
            "command": sha256_hex(command_text(command)),
            "target": command.target,
            "action": command.action,
            "sequence": command.sequence,
            "delivered": False,
            "hub_key": self.hub_key,
        }


def _anonymous_route(
    path: str, handler: Callable[[Request], Awaitable[Answer]]
) -> Callable[[Request], Awaitable[Response]]:
    """A route without a registered key: its JSON answer, and `500` `{"error":"internal"}` when it
    fails. The log line names the route and the type of the failure, never its text, which could
    carry a value of the request."""

    async def endpoint(request: Request) -> Response:
        try:
            status, body = await handler(request)
        except Exception as exc:
            LOG.error("passkey request %s failed: %s", path, type(exc).__name__)
            status, body = _error(500, INTERNAL)
        return Response(_json(body), status_code=status, media_type="application/json")

    return endpoint
