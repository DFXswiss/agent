"""A Python fake authenticator and a fake browser for the hub's passkey tests (HUB-03, no real browser).

`FakeAuthenticator` creates real ES256 (P-256) credentials and answers `create()` and `get()` with the fields a browser
hands to the page: `clientDataJSON`, the CBOR attestation object (`fmt: "none"`) and assertions over
`authenticator_data ‖ SHA-256(clientDataJSON)`. The PRF output is simulated as HMAC-SHA-256 of a per-credential
secret over the PRF input (32 stable bytes per credential, like the Chromium virtual authenticator). Every value the
hub checks can be bent by a keyword: origin, RP ID, type, challenge, flags (UP, UV, BE/BS), the counter, the
algorithm of the credential public key and the credential ID.

`FakeBrowser` drives the flows of 04-crypto.md sections 5.1 and 5.4 against the test hub: registration through an
invitation (identity, recovery bundle, self-wrap of `CK_1`, the invitation proof `inv_sig` of ADR-043), login with a
P-256 session key and the statement `agent-session-v1`, and the PRF-based passkey slot.

The browser knows the code of every invitation the test support created (`remember_invite`; `seed_invite` and the
org-invitation support register theirs), as the invitee's browser knows the code from the link.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_core.passkey.certificate import build_statement, statement_challenge
from agent_core.passkey.encoding import session_key_id
from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.auth.keys import Principal
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.bundle import RECOVERY_SLOT, passkey_slot, seal_bundle
from agent_hub.crypto.kdf import kek_from_prf, kek_from_recovery_code, prf_input
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.recovery import generate_recovery_code, recovery_id
from agent_hub.crypto.wrap import sign_wrap, wrap_key
from tests.platform.hub.conftest import FixedClock, HubHarness, SignedClient, SigningKey, p256_raw_signature

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40
FLAG_ED = 0x80
ES256 = -7
EDDSA = -8
SESSION_ISSUE_SECONDS = 43080

# The codes of the invitations the test support created, by invite_id (what the invitee's browser knows).
INVITES: dict[str, invite_codes.InviteSecrets] = {}


def remember_invite(found: invite_codes.InviteSecrets) -> invite_codes.InviteSecrets:
    """Let `FakeBrowser.register` sign for this invitation."""
    INVITES[found.invite_id] = found
    return found


def new_invite() -> invite_codes.InviteSecrets:
    """The secrets of a new random code (remembered)."""
    return remember_invite(invite_codes.invite_secrets(invite_codes.generate_code()))


# --- a minimal CBOR encoder (integers, bytes, text, maps, booleans) ---


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([major << 5 | value])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if value < 1 << (8 * width):
            return bytes([major << 5 | info]) + value.to_bytes(width, "big")
    raise ValueError("integer too large for CBOR")


def cbor(value: object) -> bytes:
    """The CBOR encoding of `value` (maps in the given key order)."""
    if isinstance(value, bool):
        return b"\xf5" if value else b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(cbor(key) + cbor(item) for key, item in value.items())
    raise TypeError("unsupported CBOR value")


def point_of(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


@dataclass
class FakeCredential:
    """One resident credential of the fake authenticator."""

    credential_id: bytes
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    user_handle: bytes
    prf_secret: bytes = field(repr=False)
    counter: int = 0


class FakeAuthenticator:
    """A software platform authenticator with resident ES256 credentials, UV and simulated PRF."""

    def __init__(self, origin: str, rp_id: str, *, counting: bool = True) -> None:
        self.origin = origin
        self.rp_id = rp_id
        self.counting = counting
        self.credentials: dict[bytes, FakeCredential] = {}

    def _client_data(self, kind: str, challenge: bytes, origin: str | None, extra: dict[str, Any]) -> bytes:
        data: dict[str, Any] = {
            "type": kind,
            "challenge": b64url_encode(challenge),
            "origin": self.origin if origin is None else origin,
            "crossOrigin": False,
        }
        data.update(extra)
        return json.dumps(data, separators=(",", ":")).encode("utf-8")

    def create(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        kind: str = "webauthn.create",
        challenge: bytes | None = None,
        flags: int = FLAG_UP | FLAG_UV | FLAG_AT,
        alg: int = ES256,
        credential_id: bytes | None = None,
        attested_id: bytes | None = None,
        client_extra: dict[str, Any] | None = None,
        transports: list[str] | None = None,
    ) -> dict[str, Any]:
        """`navigator.credentials.create()` for the hub's creation options; the JSON the browser sends."""
        key = ec.generate_private_key(ec.SECP256R1())
        raw_id = secrets.token_bytes(32) if credential_id is None else credential_id
        handle = b64url_decode(options["user"]["id"]) or b""
        credential = FakeCredential(raw_id, key, handle, secrets.token_bytes(32))
        self.credentials[raw_id] = credential
        point = point_of(key)
        cose: dict[int, object] = {1: 2, 3: alg, -1: 1, -2: point[1:33], -3: point[33:]}
        attested = raw_id if attested_id is None else attested_id
        auth_data = (
            hashlib.sha256((self.rp_id if rp_id is None else rp_id).encode("utf-8")).digest()
            + bytes([flags])
            + credential.counter.to_bytes(4, "big")
            + bytes(16)
            + len(attested).to_bytes(2, "big")
            + attested
            + cbor(cose)
        )
        attestation = cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        wanted = (b64url_decode(options["challenge"]) or b"") if challenge is None else challenge
        client_data = self._client_data(kind, wanted, origin, client_extra or {})
        return {
            "id": b64url_encode(raw_id),
            "clientDataJSON": b64url_encode(client_data),
            "attestationObject": b64url_encode(attestation),
            "transports": ["internal"] if transports is None else transports,
        }

    def get(
        self,
        credential_id: bytes,
        challenge: bytes,
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        kind: str = "webauthn.get",
        flags: int = FLAG_UP | FLAG_UV,
        counter: int | None = None,
        corrupt: bool = False,
    ) -> dict[str, bytes]:
        """`navigator.credentials.get()`: authenticator data, client data and the DER signature."""
        credential = self.credentials[credential_id]
        if counter is None:
            if self.counting:
                credential.counter += 1
            counter = credential.counter
        auth_data = (
            hashlib.sha256((self.rp_id if rp_id is None else rp_id).encode("utf-8")).digest()
            + bytes([flags])
            + counter.to_bytes(4, "big")
        )
        client_data = self._client_data(kind, challenge, origin, {})
        signature = credential.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        if corrupt:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        return {"authenticator_data": auth_data, "client_data_json": client_data, "signature": signature}

    def prf(self, credential_id: bytes) -> bytes:
        """The simulated PRF output for the platform's PRF input (32 bytes)."""
        return hmac.new(self.credentials[credential_id].prf_secret, prf_input(), hashlib.sha256).digest()


@dataclass
class Account:
    """A user registered through the fake browser: identity, recovery code, credential key and the passkey."""

    user_id: str
    credential_id: bytes
    identity: Identity = field(repr=False)
    recovery_code: bytes = field(repr=False)
    cred_key: bytes = field(repr=False)


@dataclass
class Session:
    """A logged-in browser session: key ID, private key and the login response."""

    key_id: str
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    response: dict[str, Any]
    user_id: str


class FakeBrowser:
    """The browser side of registration and login against the test hub."""

    def __init__(self, hub: HubHarness, authenticator: FakeAuthenticator | None = None) -> None:
        self.hub = hub
        public = hub.config.public
        self.authenticator = authenticator or FakeAuthenticator(public.origin, public.rp_id)
        self.ceremonies: dict[str, str] = {}

    @property
    def clock(self) -> FixedClock:
        return self.hub.clock

    def register_begin(self, invite_id: str) -> httpx.Response:
        """`register/begin`; the browser remembers which invitation each ceremony belongs to."""
        response = self.hub.anon.post("/v1/passkeys/register/begin", json={"invite_id": invite_id})
        if response.status_code == 200:
            self.ceremonies[response.json()["ceremony_id"]] = invite_id
        return response

    def registration_body(
        self,
        begin: dict[str, Any],
        credential: dict[str, Any],
        *,
        identity: Identity | None = None,
        code: bytes | None = None,
        cred_key: bytes | None = None,
        invite: invite_codes.InviteSecrets | None = None,
    ) -> tuple[dict[str, Any], Identity, bytes, bytes]:
        """The `register/finish` body of a first passkey (identity, recovery bundle, self-wrap of `CK_1`, `inv_sig`
        by the invitation the ceremony is bound to, or by `invite`)."""
        user_id = begin["user_id"]
        signer = invite or INVITES[self.ceremonies[begin["ceremony_id"]]]
        challenge = b64url_decode(begin["options"]["challenge"]) or b""
        identity = identity or Identity.generate()
        code = code or generate_recovery_code()
        cred_key = cred_key or secrets.token_bytes(32)
        bundle = seal_bundle(kek_from_recovery_code(code), user_id, RECOVERY_SLOT, identity)
        wrap = wrap_key(identity.kem_public(), scope="cred", scope_id=user_id, epoch=1, recipient=user_id, key=cred_key)
        sig = sign_wrap(identity.sig, scope="cred", scope_id=user_id, epoch=1, recipient=user_id, wrap=wrap)
        body = {
            "ceremony_id": begin["ceremony_id"],
            "credential": credential,
            "identity": {
                "kem_pub": b64url_encode(identity.kem_public()),
                "sig_pub": b64url_encode(identity.sig_public()),
            },
            "recovery": {"recovery_id": recovery_id(code), "bundle_ct": bundle},
            "cred_wrap": {"epoch": 1, "wrap": wrap, "sig": sig},
            "inv_sig": invite_codes.sign_register(signer, user_id, challenge),
        }
        return body, identity, code, cred_key

    def register(self, invite_id: str) -> Account:
        """Register a new user through `invite_id` (gesture 1 of 04-crypto.md section 5.1)."""
        begin = self.register_begin(invite_id)
        assert begin.status_code == 200, begin.text
        options = begin.json()
        credential = self.authenticator.create(options["options"])
        body, identity, code, cred_key = self.registration_body(options, credential)
        finish = self.hub.anon.post("/v1/passkeys/register/finish", json=body)
        assert finish.status_code == 200, finish.text
        raw_id = b64url_decode(credential["id"]) or b""
        return Account(options["user_id"], raw_id, identity, code, cred_key)

    def certificate(
        self,
        hub_value: bytes,
        credential_id: bytes,
        *,
        expires_in: int = SESSION_ISSUE_SECONDS,
        origin: str | None = None,
        **get_options: Any,
    ) -> tuple[dict[str, str], ec.EllipticCurvePrivateKey, bytes]:
        """A session key, its statement and the assertion over SHA-256(statement)."""
        session_key = ec.generate_private_key(ec.SECP256R1())
        expires = int(self.clock().timestamp()) + expires_in
        statement = build_statement(
            origin=self.hub.config.public.origin if origin is None else origin,
            session_public_key=point_of(session_key),
            hub_value=hub_value,
            expires=expires,
        )
        assertion = self.authenticator.get(credential_id, statement_challenge(statement), **get_options)
        certificate = {
            "statement": b64url_encode(statement),
            "credential_id": b64url_encode(credential_id),
            "authenticator_data": b64url_encode(assertion["authenticator_data"]),
            "client_data_json": b64url_encode(assertion["client_data_json"]),
            "signature": b64url_encode(assertion["signature"]),
        }
        return certificate, session_key, statement

    def login_begin(self) -> dict[str, Any]:
        response = self.hub.anon.post("/v1/passkeys/login/begin", json={})
        assert response.status_code == 200, response.text
        begin: dict[str, Any] = response.json()
        return begin

    def login(self, account: Account, **certificate_options: Any) -> Session:
        """Log in with the account's passkey (04-crypto.md section 5.4)."""
        begin = self.login_begin()
        hub_value = b64url_decode(begin["hub_value"]) or b""
        certificate, private, statement = self.certificate(hub_value, account.credential_id, **certificate_options)
        response = self.hub.anon.post(
            "/v1/passkeys/login/finish", json={"ceremony_id": begin["ceremony_id"], "certificate": certificate}
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["key_id"] == session_key_id(statement)
        return Session(body["key_id"], private, body, account.user_id)

    def client(self, session: Session) -> SignedClient:
        """A signed client of the session key."""
        principal = Principal(
            kind="session", key_id=session.key_id, principal_id=uuid.UUID(session.user_id), actor_kind="user"
        )
        private = session.private_key
        key = SigningKey(
            key_id=session.key_id, sign=lambda text: p256_raw_signature(private, text), principal=principal
        )
        return self.hub.client(key)

    def passkey_bundle(self, account: Account) -> str:
        """The passkey slot of the account's identity, sealed with `KEK_i` from the simulated PRF."""
        kek = kek_from_prf(self.authenticator.prf(account.credential_id), account.credential_id)
        return seal_bundle(kek, account.user_id, passkey_slot(account.credential_id), account.identity)


def seed_invite(
    hub: HubHarness,
    invite_id: str | None = None,
    *,
    scope: str = "platform",
    kind: str = "user",
    expires_in: float = 72 * 3600,
    used: bool = False,
    revoked: bool = False,
    registered_user: uuid.UUID | None = None,
) -> str:
    """Insert an invitation row directly (HUB-04/HUB-11 create them through their APIs): a new remembered code with
    its `pk_inv`, or the given `invite_id` without a key."""
    found = None if invite_id is not None else new_invite()
    invite = invite_id or cast(invite_codes.InviteSecrets, found).invite_id
    key = None if found is None else b64url_decode(found.pk_inv)
    now = hub.clock()
    org = uuid.UUID(int=uuid.uuid4().int) if scope == "org" else None
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.invite (id, scope, kind, org_id, created_at, expires_at, registered_user, registered_at,"
            " used_at, revoked_at, pk_inv) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                invite,
                scope,
                kind,
                org,
                min(now - timedelta(seconds=10), now + timedelta(seconds=expires_in - 1)),
                now + timedelta(seconds=expires_in),
                registered_user,
                now if registered_user else None,
                now if used else None,
                now if revoked else None,
                key,
            ),
        )
    return invite
