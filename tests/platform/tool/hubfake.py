"""An in-process hub for the tests of the agent tool (11-agent-access.md sections 4 and 6; 03-api.md section 4).

HTTP on 127.0.0.1 (an OS-drawn port) in a thread. It checks every signed request like the hub (`agent-request-v1`,
key ID `agent.<32 hex>`, the agent's state) and signs every answer to a signed request with `agent-response-v1`
(`agent_cli.keys.response_text`, the text the runner's client verifies). Appends run the reference rules of the copy
(`check_batch`, `check_cred_wrap`), so a batch the tool builds wrongly is refused as by the real hub.

The fake also plays the user's browser where the hub needs one (`confirm`: the confirmation of a pairing with the
sealed identity, label and claim hash) and seeds accounts (`add_user`: identity, `CK_1` with its self-wrap, optionally
a private organization with profile events). `hooks` replace the answer of a route (`(method, path)` -> callable
returning `(status, body)` or None to go on), to simulate refusals, faults and a malicious hub.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_cli.keys import request_text, response_text
from agent_platform.batch import build_batch, make_wrap
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import enroll
from agent_runner.crypto import identity as identity_format
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.credential import verify_credential
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, body_json, format_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import Identity, ed25519_public_raw, ed25519_verify
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.entries import Head, entry_hash, sign_platform_entry
from agent_runner.crypto.log.platform import PlatformState, verify_platform_log
from agent_runner.crypto.log.state import ORG
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import CRED, WrapSlot, check_cred_wrap
from agent_runner.crypto.wrap import unwrap_key
from agent_runner.sync.http import format_time as request_time

from .fakes import INSTALLERS

HUB_KEY_ID = "hub-test"
ANONYMOUS = ("/v1/config", "/v1/agent-pairings", "/v1/agent/claim", "/v1/invites/lookup")
CONFIRMED_ROUTES = (("POST", "/v1/agent/claim"), ("GET", "/v1/agent/handover"))
Json = dict[str, Any]
Hook = Callable[[Json | None, "Request"], tuple[Any, ...] | None]


def now_ms_text(offset: timedelta = timedelta()) -> str:
    return format_time(datetime.now(UTC) + offset)


@dataclass
class User:
    id: str
    identity: Identity
    settings: Json = field(default_factory=lambda: {"model_access": "auto", "model_id": None, "locale": "en"})
    cred_epoch: int = 1
    cred_keys: dict[int, bytes] = field(default_factory=dict)
    disabled: bool = False
    anchor: Json | None = None
    """The identity answer of the hub (`platform_seq`, `id_sig`; ADR-060) once the user is anchored."""


@dataclass
class AgentRow:
    id: str
    kem: bytes
    sig: bytes
    state: str = "open"
    user: str | None = None
    bundle_ct: str | None = None
    label_ct: str | None = None
    claim_hash: bytes | None = None
    misses: int = 0
    expires: float = 0.0
    remote: bool = False
    nonce: bytes | None = None
    handover_ct: str | None = None


@dataclass
class Org:
    id: str
    log: VerifiedLog
    private: bool
    platform_access: bool


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    user: str | None
    agent: str | None
    raw: bytes


class HubFake:
    def __init__(self) -> None:
        self.hub_key = Ed25519PrivateKey.generate()
        self.root = Ed25519PrivateKey.generate()
        self.root_text = b64u_encode(ed25519_public_raw(self.root))
        self.users: dict[str, User] = {}
        self.agents: dict[str, AgentRow] = {}
        self.orgs: dict[str, Org] = {}
        self.wraps: list[Json] = []
        self.events: dict[str, list[Json]] = {}
        self.credentials: dict[tuple[str, str], Json] = {}
        self.invites: dict[str, Json] = {}
        self.devices: dict[str, list[Any]] = {}
        self.candidates: dict[str, list[Any]] = {}
        self.placements: dict[str, Any] = {}
        self.no_device: set[str] = set()
        self.tasks: dict[str, Json] = {}
        self.attention: dict[str, Json] = {}
        """Attention records by task (KOR-39): the hub derives them; tests set them."""
        self.hooks: dict[tuple[str, str], Hook] = {}
        self.requests: list[Request] = []
        self.bodies: list[bytes] = []
        self.platform_entries: list[Json] = []
        self.platform_devices: dict[str, Identity] = {}
        self.sign_answers = True
        self.old_hub = False  # refuses `remote` like a hub of before KOR-42
        self.routes: list[tuple[str, str, Callable[..., tuple[int, Any]]]] = []
        self.lock = threading.RLock()
        body = {"seq": 0, "prev": "", "type": "root", "by": "platform-root", "ts": now_ms_text(), "v": 1}
        self._platform_append({**body, "pub": self.root_text})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- lifecycle ---

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> HubFake:
        for name in INSTALLERS:
            importlib.import_module(f".fakes.{name}", __package__).install(self)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- seeding ---

    def add_user(
        self,
        *,
        private_org: bool = True,
        display_name: str = "Ada",
        cred: bool = True,
        anchor: str | None = None,
    ) -> User:
        """A user; `anchor` anchors its identity in the platform log (`certify`: `user_certify`, `invite`: registered
        through a `user_invite` with the identity proof and the derived user ID; None: not anchored)."""
        identity = Identity.generate()
        if anchor == "invite":
            user = User("", identity)
            user.id, user.anchor = self.invite_identity(identity)
        else:
            user = User(uuid7(), identity)
            if anchor == "certify":
                self.anchor_user(user)
        self.users[user.id] = user
        if cred:
            key = secrets.token_bytes(32)
            user.cred_keys[1] = key
            self.add_wrap(user, WrapSlot(CRED, user.id, 1, user.id), user.identity.kem_public(), key)
        if private_org:
            self.create_org(user, private=True, name="Private", display_name=display_name)
        return user

    def add_wrap(self, wrapper: User, slot: WrapSlot, kem: bytes, key: bytes) -> Json:
        record = {**make_wrap(wrapper.identity, slot, kem, key), "wrapped_by": wrapper.id, "created_at": now_ms_text()}
        self.wraps.append(record)
        return record

    def org_key(self, org: str, user: User, scope: str = ORG, scope_id: str | None = None) -> bytes:
        scope_id = org if scope_id is None else scope_id
        epoch = self.orgs[org].log.state.epoch(scope, scope_id)
        for wrap in self.wraps:
            if (wrap["scope"], wrap["scope_id"], wrap["epoch"], wrap["recipient_id"]) == (
                scope,
                scope_id,
                epoch,
                user.id,
            ):
                return unwrap_key(
                    user.identity.kem, scope=scope, scope_id=scope_id, epoch=epoch, recipient=user.id, wrap=wrap["wrap"]
                )
        raise KeyError(scope_id)

    def create_org(self, user: User, *, private: bool, name: str, display_name: str) -> str:
        org, wg = uuid7(), uuid7()
        genesis = {
            "type": "genesis",
            "subject": user.id,
            "pub": user.identity.public(),
            "default_wg": wg,
            "private": private,
        }
        built = build_batch(
            None, org=org, author=user.id, identity=user.identity, fields=[genesis], ts=now_ms_text(), held={}
        )
        self.orgs[org] = Org(org, built.log, private, private)
        for record in built.wraps:
            self.wraps.append({**record, "wrapped_by": user.id, "created_at": now_ms_text()})
        key = built.new_keys[(ORG, org, 1)]
        self.add_event(org, user, key, "org.profile", {"name": name}, lh=0)
        self.add_event(org, user, key, "wg.profile", {"name": "Default"}, lh=0, wg=wg)
        if display_name:
            self.add_event(org, user, key, "member.profile", {"display_name": display_name}, lh=0, ref=user.id)
        return org

    def add_event(self, org: str, user: User, key: bytes, kind: str, body: Json, *, lh: int, **extra: str) -> Json:
        epoch = self.orgs[org].log.state.epoch(ORG, org)
        header = new_header(
            id=uuid7(),
            org=org,
            scope=ORG,
            scope_id=org,
            epoch=epoch,
            author=user.id,
            kind=kind,
            ts=now_ms_text(),
            lh=lh,
            **extra,
        )
        event = seal_event(key, header, body_json(body), user.identity.sig)
        self.store_event(org, event)
        return event

    def store_event(self, scope: str, event: Json) -> None:
        found = self.events.setdefault(scope, [])
        found.append({"scope_seq": len(found) + 1, "received_at": now_ms_text(), "event": event})

    def _platform_append(self, body: Json) -> Json:
        entry = sign_platform_entry(body, self.root)
        self.platform_entries.append(entry)
        return entry

    def platform_state(self) -> PlatformState:
        return verify_platform_log(self.platform_entries, self.root_text).state

    def _platform_base(self) -> Json:
        head = self.platform_entries[-1]
        return {
            "seq": len(self.platform_entries),
            "prev": entry_hash(head),
            "by": "platform-root",
            "ts": now_ms_text(),
            "v": 1,
        }

    def anchor_user(self, user: User) -> None:
        """A `user_certify` of the user's identity keys."""
        body = {**self._platform_base(), "type": "user_certify", "user": user.id, "pub": user.identity.public()}
        user.anchor = {"platform_seq": int(self._platform_append(body)["body"]["seq"]), "id_sig": ""}

    def invite_identity(self, identity: Identity) -> tuple[str, Json]:
        """A `user_invite` and the identity proof of the account registered with its code: the derived user ID and the
        anchor (`platform_seq` of the invitation, `id_sig`)."""
        inv = invite_format.invite_secrets(invite_format.generate_code())
        body = {
            **self._platform_base(),
            "type": "user_invite",
            "invite_id": inv.invite_id,
            "pk_inv": inv.pk_inv,
            "expires": now_ms_text(timedelta(days=1)),
        }
        entry = self._platform_append(body)
        user = identity_format.invited_user_id(inv.invite_id, body["ts"])
        id_sig = identity_format.sign_identity(inv.signing, user, identity.public())
        return user, {"platform_seq": int(entry["body"]["seq"]), "id_sig": id_sig}

    def identity_answer(self, user: User) -> Json:
        """`GET /v1/users/{user}/identity` (ADR-060)."""
        return {
            "user_id": user.id,
            "pub": user.identity.public(),
            **cast(Json, user.anchor),
            "account_code": identity_format.account_code(user.id, user.identity.public()),
        }

    def append(self, org: str, user: User, fields: list[Json]) -> None:
        """Entries the user appends from another device (e.g. `wg_archive`, which the tool has no command for)."""
        found = self.orgs[org]
        built = build_batch(
            found.log,
            org=org,
            author=user.id,
            identity=user.identity,
            fields=fields,
            ts=now_ms_text(),
            held={},
            platform=self.platform_state(),
        )
        found.log = built.log

    def certify_device(self) -> tuple[str, int]:
        """A platform device certified in the platform log (`device_invite` + `device_certify`)."""
        device, ident = uuid7(), Identity.generate()
        code = invite_format.generate_code()
        inv = invite_format.invite_secrets(code)
        head = self.platform_entries[-1]
        seq = len(self.platform_entries)
        expires = now_ms_text(timedelta(days=1))
        base = {"by": "platform-root", "ts": now_ms_text(), "v": 1}
        self._platform_append(
            {
                **base,
                "seq": seq,
                "prev": entry_hash(head),
                "type": "device_invite",
                "invite_id": inv.invite_id,
                "pk_inv": inv.pk_inv,
                "expires": expires,
            }
        )
        capacity = {
            "vm_memory_budget_mib": 8192,
            "vm_memory_used_mib": 0,
            "vm_slots_total": 4,
            "vm_slots_free": 4,
            "pool_ready": 1,
        }
        extra = {"agent_version": "1.0.0", "os": "linux", "arch": "arm64", "capacity": capacity}
        unsigned = {"device_id": device, "kind": "platform", "invite_id": inv.invite_id, "pub": ident.public(), **extra}
        enrollment = enroll.sign_enrollment(unsigned, ident.sig, inv.signing)
        self._platform_append(
            {
                **base,
                "seq": seq + 1,
                "prev": entry_hash(self.platform_entries[-1]),
                "type": "device_certify",
                "device": device,
                "pub": ident.public(),
                "invite_id": inv.invite_id,
                **extra,
                "enroll_sig": enrollment["enroll_sig"],
                "inv_sig": enrollment["inv_sig"],
            }
        )
        self.platform_devices[device] = ident
        return device, seq + 1

    # --- the browser side of a pairing ---

    def confirm(self, pairing_id: str, user: User, secret: bytes, label: Json | None = None) -> None:
        row = self.agents[pairing_id]
        claim = agent_crypto.claim(secret, pairing_id, user.id)
        row.bundle_ct = agent_crypto.seal_identity(row.kem, secret, pairing_id, user.id, user.identity)
        row.label_ct = agent_crypto.seal_label(
            user.identity.kem_public(), pairing_id, user.id, label or {"name": "Claude Code", "host": "mac"}
        )
        row.claim_hash = agent_crypto.claim_hash(claim)
        row.user = user.id
        row.state = "confirmed"

    def confirm_remote(self, pairing_id: str, user: User, secret: bytes | None = None, root: str | None = None) -> str:
        """The browser of a remote pairing (ADR-067): confirm, seal the handover to the agent; the return code."""
        secret = secrets.token_bytes(32) if secret is None else secret
        self.confirm(pairing_id, user, secret)
        row = self.agents[pairing_id]
        row.handover_ct = agent_crypto.seal_handover(row.kem, pairing_id, user.id, secret, root or self.root_text)
        row.expires = time.time() + 600
        return agent_crypto.format_code(agent_crypto.return_code(secret, pairing_id, user.id))

    def override(self, name: str, handler: Callable[..., tuple[int, Any]]) -> None:
        """Replace a route handler (`r_*`) of this instance, e.g. with a forging wrapper of the original."""
        setattr(self, name, handler)

    def restore(self, name: str) -> None:
        delattr(self, name)

    def revoke(self, agent_id: str) -> None:
        self.agents[agent_id].state = "revoked"
        self.agents[agent_id].bundle_ct = None

    # --- HTTP ---

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, body, signed, *extra = hub.handle(self.command, self.path, dict(self.headers.items()), raw)
                data = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in (extra[0] if extra else {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                if signed is not None and hub.sign_answers:
                    key_id, nonce, agent_time = signed
                    hub_time = request_time(time.time())
                    text = response_text(key_id, nonce, agent_time, status, hub_time, hashlib.sha256(data).hexdigest())
                    self.send_header("Agent-Hub-Key", HUB_KEY_ID)
                    self.send_header("Agent-Hub-Time", hub_time)
                    self.send_header("Agent-Hub-Signature", b64u_encode(hub.hub_key.sign(text.encode())))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        return Handler

    def handle(self, method: str, target: str, headers: dict[str, str], raw: bytes) -> tuple[Any, ...]:
        """`(status, body, signed)`, and as a fourth element the extra answer headers of a hook that returned
        `(status, body, headers)` (e.g. `Retry-After`)."""
        with self.lock:
            self.bodies.append(raw)
            parts = urlsplit(target)
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            signed = None
            user = agent = None
            if "Agent-Key" in headers:
                key_id, nonce, agent_time = headers["Agent-Key"], headers["Agent-Nonce"], headers["Agent-Time"]
                signed = (key_id, nonce, agent_time)
                found = self._authenticate(method, target, headers, raw)
                if isinstance(found, tuple):
                    return found[0], found[1], signed
                agent = found
                row = self.agents[found]
                user = row.user
                if row.state == "confirmed" and (method, parts.path) not in CONFIRMED_ROUTES:
                    return 403, {"error": "forbidden"}, signed
            body = json.loads(raw) if raw else None
            request = Request(method, parts.path, query, user, agent, raw)
            self.requests.append(request)
            hook = self.hooks.get((method, parts.path))
            if hook is not None:
                answer = hook(body, request)
                if answer is not None:
                    return answer[0], answer[1], signed, *answer[2:]
            try:
                status, result = self.route(request, body)
            except CryptoError as error:
                status, result = 400, {"error": error.code if error.code != "header_invalid" else "request_invalid"}
            return status, result, signed

    def _authenticate(self, method: str, target: str, headers: dict[str, str], raw: bytes) -> str | tuple[int, Json]:
        key_id = headers["Agent-Key"]
        match = re.fullmatch(r"agent\.([0-9a-f]{32})", key_id)
        row = None
        if match is not None:
            hexed = match.group(1)
            row = next((r for r in self.agents.values() if r.id.replace("-", "") == hexed), None)
        if row is None or row.state == "open":
            return 401, {"error": "key_unknown"}
        text = request_text(
            key_id,
            method,
            headers["Host"],
            target,
            headers["Agent-Time"],
            headers["Agent-Nonce"],
            hashlib.sha256(raw).hexdigest(),
        )
        try:
            ed25519_verify(row.sig, b64u_decode(headers["Agent-Signature"]), text.encode())
        except (CryptoError, ValueError):
            return 401, {"error": "signature_invalid"}
        if row.state == "revoked" or (row.user is not None and self.users[row.user].disabled):
            return 401, {"error": "key_revoked"}
        return row.id

    # --- routes ---

    def route(self, request: Request, body: Any) -> tuple[int, Any]:
        method, path = request.method, request.path
        table: list[tuple[str, str, Callable[..., tuple[int, Any]]]] = [
            ("GET", r"/v1/config", self.r_config),
            ("POST", r"/v1/agent-pairings", self.r_pairing_create),
            ("GET", r"/v1/agent-pairings/(?P<pairing>[0-9a-f-]+)", self.r_pairing_get),
            ("GET", r"/v1/agent/handover", self.r_handover),
            ("POST", r"/v1/agent/claim", self.r_claim),
            ("GET", r"/v1/agent/me", self.r_agent_me),
            ("GET", r"/v1/me", self.r_me),
            ("PUT", r"/v1/me/settings", self.r_settings),
            ("GET", r"/v1/me/credentials", self.r_credentials),
            ("GET", r"/v1/me/credentials/(?P<kind>[a-z_]+)", self.r_credential_get),
            ("PUT", r"/v1/me/credentials/(?P<kind>[a-z_]+)", self.r_credential_put),
            ("GET", r"/v1/me/devices", self.r_devices),
            ("GET", r"/v1/wraps", self.r_wraps),
            ("POST", r"/v1/wraps", self.r_wraps_post),
            ("POST", r"/v1/orgs", self.r_org_create),
            ("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/log", self.r_log),
            ("POST", r"/v1/orgs/(?P<org>[0-9a-f-]+)/log", self.r_log_append),
            ("POST", r"/v1/orgs/(?P<org>[0-9a-f-]+)/invites", self.r_invite),
            ("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/invites", self.r_invites),
            ("POST", r"/v1/invites/lookup", self.r_lookup),
            ("GET", r"/v1/platform/log", self.r_platform_log),
            ("GET", r"/v1/users/lookup", self.r_user_lookup),
            ("GET", r"/v1/users/(?P<user>[0-9a-f-]+)/identity", self.r_user_identity),
            ("GET", r"/v1/scopes/(?P<scope>[0-9a-f-]+)/events", self.r_events),
            ("GET", r"/v1/workgroups/(?P<wg>[0-9a-f-]+)/device-candidates", self.r_candidates),
            ("GET", r"/v1/workgroups/(?P<wg>[0-9a-f-]+)/placement", self.r_placement_get),
            ("PUT", r"/v1/workgroups/(?P<wg>[0-9a-f-]+)/placement", self.r_placement_put),
            ("GET", r"/v1/tasks/(?P<task>[0-9a-f-]+)", self.r_task),
            ("POST", r"/v1/tasks", self.r_task_create),
            ("POST", r"/v1/tasks/(?P<task>[0-9a-f-]+)/prompts", self.r_task_prompt),
            ("GET", r"/v1/workgroups/(?P<wg>[0-9a-f-]+)/tasks", self.r_wg_tasks),
            ("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/attention", self.r_attention),
            ("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/attention/summary", self.r_attention_summary),
            *self.routes,
        ]
        for route_method, pattern, handler in table:
            match = re.fullmatch(pattern, path)
            if match is not None and route_method == method:
                if not path.startswith(ANONYMOUS) and request.user is None:
                    return 401, {"error": "unauthorized"}
                return handler(request, body, **match.groupdict())
        return 404, {"error": "not_found"}

    def add_route(self, method: str, pattern: str, handler: Callable[..., tuple[int, Any]]) -> None:
        """A further route (`fakes/*.py`); a signed request is needed on the paths of `route`'s list, as there."""
        self.routes.append((method, pattern, handler))

    def r_config(self, request: Request, body: Any) -> tuple[int, Any]:
        public = b64u_encode(ed25519_public_raw(self.hub_key))
        return 200, {
            "origin": self.origin,
            "platform_root_key": self.root_text,
            "hub_key": {"id": HUB_KEY_ID, "public_key": public},
        }

    def r_pairing_create(self, request: Request, body: Json) -> tuple[int, Any]:
        kem, sig = b64u_decode(body["pub"]["kem"], 32), b64u_decode(body["pub"]["sig"], 32)
        try:
            agent_crypto.verify_pop(body["pairing_id"], kem, sig, body["pop"])
        except CryptoError:
            return 401, {"error": "signature_invalid"}
        remote = body.get("remote") is True
        if "remote" in body and self.old_hub:
            return 400, {"error": "request_invalid"}
        seconds = 300 if remote else 600
        row = AgentRow(body["pairing_id"], kem, sig, expires=time.time() + seconds, remote=remote)
        answer: Json = {"pairing_id": row.id, "expires_at": now_ms_text(timedelta(seconds=seconds))}
        if remote:
            row.nonce = secrets.token_bytes(16)
            answer.update(remote=True, nonce=b64u_encode(row.nonce))
        self.agents[row.id] = row
        return 200, answer

    def r_pairing_get(self, request: Request, body: Any, pairing: str) -> tuple[int, Any]:
        row = self.agents.get(pairing)
        if row is None or row.state not in ("open", "confirmed") or row.expires <= time.time():
            return 404, {"error": "pairing_unknown"}
        answer: Json = {
            "pairing_id": row.id,
            "pub": {"kem": b64u_encode(row.kem), "sig": b64u_encode(row.sig)},
            "state": row.state,
            "expires_at": format_time(datetime.fromtimestamp(row.expires, UTC)),
        }
        if row.remote and row.nonce is not None:
            answer.update(remote=True, nonce=b64u_encode(row.nonce))
        return 200, answer

    def r_handover(self, request: Request, body: Any) -> tuple[int, Any]:
        row = self.agents[cast(str, request.agent)]
        if row.state != "confirmed" or row.handover_ct is None or row.expires <= time.time():
            return 404, {"error": "pairing_unknown"}
        return 200, {"agent_id": row.id, "user_id": row.user, "handover_ct": row.handover_ct}

    def _agent_me(self, row: AgentRow) -> Json:
        return {
            "agent_id": row.id,
            "user_id": row.user,
            "bundle_ct": row.bundle_ct,
            "label_ct": row.label_ct,
            "created_at": now_ms_text(),
            "claimed_at": now_ms_text(),
        }

    def r_claim(self, request: Request, body: Json) -> tuple[int, Any]:
        row = self.agents[cast(str, request.agent)]
        claim = b64u_decode(body["claim"], 32)
        if agent_crypto.claim_hash(claim) != row.claim_hash:
            row.misses += 1
            return 401, {"error": "claim_invalid"}
        row.state = "active"
        row.handover_ct = None
        return 200, self._agent_me(row)

    def r_agent_me(self, request: Request, body: Any) -> tuple[int, Any]:
        return 200, self._agent_me(self.agents[cast(str, request.agent)])

    def r_me(self, request: Request, body: Any) -> tuple[int, Any]:
        user = self.users[cast(str, request.user)]
        orgs = []
        for org in self.orgs.values():
            member = org.log.state.member(user.id)
            if member is not None:
                orgs.append(
                    {
                        "org_id": org.id,
                        "role": member.role,
                        "private": org.private,
                        "default_wg": org.log.state.default_wg,
                        "log_head": org.log.head.to_json(),
                    }
                )
        return 200, {
            "user_id": user.id,
            "identity": {"kem_pub": user.identity.public()["kem"], "sig_pub": user.identity.public()["sig"]},
            "settings": user.settings,
            "orgs": orgs,
            "platform_roles": [],
            "unread_notifications": 0,
            "passkeys": [],
            "cred_holders": {"epoch": user.cred_epoch, "devices": []},
        }

    def r_settings(self, request: Request, body: Json) -> tuple[int, Any]:
        if set(body) != {"model_access", "model_id", "locale"}:
            return 400, {"error": "request_invalid"}
        self.users[cast(str, request.user)].settings = dict(body)
        return 200, dict(body)

    def r_credentials(self, request: Request, body: Any) -> tuple[int, Any]:
        found = [
            {"kind": kind, "version": row["version"], "epoch": row["record"]["header"]["epoch"], "updated_at": "x"}
            for (owner, kind), row in self.credentials.items()
            if owner == request.user
        ]
        return 200, {"credentials": found}

    def r_credential_get(self, request: Request, body: Any, kind: str) -> tuple[int, Any]:
        row = self.credentials.get((cast(str, request.user), kind))
        return (404, {"error": "not_found"}) if row is None else (200, {"record": row["record"]})

    def r_credential_put(self, request: Request, body: Json, kind: str) -> tuple[int, Any]:
        user = self.users[cast(str, request.user)]
        record = body["record"]
        header = verify_credential(record, user.identity.sig_public())
        stored = self.credentials.get((user.id, kind))
        current = 0 if stored is None else stored["version"]
        if header["owner"] != user.id or header["kind"] != kind or header["epoch"] != user.cred_epoch:
            return 400, {"error": "request_invalid"}
        if header["version"] != body["expected_version"] + 1:
            return 400, {"error": "request_invalid"}
        if body["expected_version"] != current:
            return 409, {"error": "version_conflict", "detail": {"expected": current}}
        if stored is not None and stored["record"]["header"]["id"] != header["id"]:
            return 400, {"error": "request_invalid"}
        self.credentials[(user.id, kind)] = {"record": record, "version": header["version"]}
        return 200, {"version": header["version"]}

    def r_devices(self, request: Request, body: Any) -> tuple[int, Any]:
        return 200, {"devices": self.devices.get(cast(str, request.user), [])}

    def r_wraps(self, request: Request, body: Any) -> tuple[int, Any]:
        return 200, {"wraps": [wrap for wrap in self.wraps if wrap["recipient_id"] == request.user]}

    def r_wraps_post(self, request: Request, body: Json) -> tuple[int, Any]:
        user = cast(str, request.user)
        logs = [org.log for org in self.orgs.values() if org.log.state.member(user) is not None]
        for wrap in body["wraps"]:
            check_cred_wrap(wrap, user, logs, self.users[user].cred_epoch, require_active=True)
            self.wraps.append({**wrap, "wrapped_by": user, "created_at": now_ms_text()})
        return 200, {"stored": len(body["wraps"]), "ignored": 0}

    def r_org_create(self, request: Request, body: Json) -> tuple[int, Any]:
        user = self.users[cast(str, request.user)]
        genesis = body["genesis"]
        result = check_batch(None, [genesis], body["wraps"], wrapped_by=user.id, founder=user.identity.public()["sig"])
        org = result.log.state.org
        if result.log.state.private:
            return 403, {"error": "platform_invite_required"}
        self.orgs[org] = Org(org, result.log, False, False)
        for wrap in body["wraps"]:
            self.wraps.append({**wrap, "wrapped_by": user.id, "created_at": now_ms_text()})
        for event in body["events"]:
            self.store_event(org, event)
        return 200, {"org_id": org, "default_wg": result.log.state.default_wg}

    def _member(self, request: Request, org: str) -> Org | None:
        found = self.orgs.get(org)
        if found is None or found.log.state.member(cast(str, request.user)) is None:
            return None
        return found

    def r_log(self, request: Request, body: Any, org: str) -> tuple[int, Any]:
        found = self._member(request, org)
        if found is None:
            return 404, {"error": "not_found"}
        after = int(request.query.get("after", "-1"))
        limit = int(request.query.get("limit", "200"))
        entries = list(found.log.entries)[after + 1 : after + 1 + limit]
        items = [{"seq": cast(Json, entry["body"])["seq"], "entry": entry} for entry in entries]
        more = after + 1 + limit < len(found.log.entries)
        return 200, {
            "entries": items,
            "head": found.log.head.to_json(),
            "next_after": items[-1]["seq"] if more else None,
        }

    def r_log_append(self, request: Request, body: Json, org: str) -> tuple[int, Any]:
        found = self.orgs.get(org)
        if found is None:
            return 404, {"error": "not_found"}
        if body["expected_seq"] != found.log.head.seq + 1:
            return 409, {"error": "seq_conflict", "detail": {"expected_seq": found.log.head.seq + 1}}
        result = check_batch(
            found.log,
            body["entries"],
            body["wraps"],
            wrapped_by=cast(str, request.user),
            platform=self.platform_state(),
            now=datetime.now(UTC),
            platform_access=found.platform_access,
        )
        found.log = result.log
        for wrap in body["wraps"]:
            self.wraps.append({**wrap, "wrapped_by": request.user, "created_at": now_ms_text()})
        for event in body["events"]:
            self.store_event(org, event)
        return 200, {"head": result.log.head.to_json()}

    def r_invite(self, request: Request, body: Json, org: str) -> tuple[int, Any]:
        found = self.orgs[org]
        if not found.log.state.is_admin(cast(str, request.user)):
            return 403, {"error": "forbidden"}
        result = check_batch(found.log, [body["entry"]], [], wrapped_by=cast(str, request.user))
        found.log = result.log
        invite_id = body["entry"]["body"]["invite_id"]
        self.invites[invite_id] = {"org": org, "package_ct": body["package_ct"], "registered": False, "opened_at": None}
        return 200, {"invite_id": invite_id, "seq": result.log.head.seq}

    def r_invites(self, request: Request, body: Any, org: str) -> tuple[int, Any]:
        """The hub's list (only the fields the tool reads besides the ID; `opened_at` from the first lookup)."""
        if not self.orgs[org].log.state.is_admin(cast(str, request.user)):
            return 403, {"error": "forbidden"}
        rows = [
            {
                "invite_id": invite_id,
                "state": found.get("state", "open"),
                "registered": found["registered"],
                "registered_user": found.get("registered_user"),
                "opened_at": found["opened_at"],
            }
            for invite_id, found in self.invites.items()
            if found["org"] == org
        ]
        return 200, {"invites": rows}

    def r_lookup(self, request: Request, body: Json) -> tuple[int, Any]:
        found = self.invites.get(body["invite_id"])
        if found is None:
            return 404, {"error": "invite_unknown"}
        if found.get("scope") == "platform":
            expires = now_ms_text(timedelta(hours=1))
            return 200, {"kind": "user", "scope": "platform", "expires_at": expires, "registered": found["registered"]}
        org = self.orgs[found["org"]]
        found["opened_at"] = found.get("opened_at") or now_ms_text()
        return 200, {
            "kind": "user",
            "scope": "org",
            "org_id": org.id,
            "expires_at": now_ms_text(timedelta(hours=1)),
            "registered": False,
            "package_ct": found["package_ct"],
            "log_head": org.log.head.to_json(),
            "log": list(org.log.entries),
        }

    def r_platform_log(self, request: Request, body: Any) -> tuple[int, Any]:
        after = int(request.query.get("after", "-1"))
        limit = int(request.query.get("limit", "200"))
        entries = self.platform_entries[after + 1 : after + 1 + limit]
        items = [{"seq": entry["body"]["seq"], "entry": entry} for entry in entries]
        more = after + 1 + limit < len(self.platform_entries)
        last = self.platform_entries[-1]
        head = {"seq": last["body"]["seq"], "hash": entry_hash(last)}
        return 200, {"entries": items, "head": head, "next_after": items[-1]["seq"] if more else None}

    def org_of_wg(self, wg: str) -> str | None:
        for org in self.orgs.values():
            if wg in org.log.state.workgroups:
                return org.id
        return None

    def r_user_identity(self, request: Request, body: Any, user: str) -> tuple[int, Any]:
        found = self.users.get(user)
        if found is None or found.anchor is None:
            return 404, {"error": "identity_unproven"}
        return 200, self.identity_answer(found)

    def r_user_lookup(self, request: Request, body: Any) -> tuple[int, Any]:
        code = identity_format.parse_account_code(request.query.get("code"))
        for user in self.users.values():
            if user.anchor is not None and identity_format.account_code(user.id, user.identity.public()) == code:
                return 200, self.identity_answer(user)
        return 404, {"error": "identity_unproven"}

    def r_events(self, request: Request, body: Any, scope: str) -> tuple[int, Any]:
        if self._member(request, self.org_of_wg(scope) or scope) is None:
            return 404, {"error": "not_found"}
        kinds = set(request.query.get("kinds", "").split(","))
        after = int(request.query.get("after", "0"))
        task, ref = request.query.get("task"), request.query.get("ref")
        found = [
            item
            for item in self.events.get(scope, [])
            if item["scope_seq"] > after
            and item["event"]["header"]["kind"] in kinds
            and (task is None or item["event"]["header"]["task"] == task)
            and (ref is None or item["event"]["header"]["ref"] == ref)
        ]
        return 200, {"events": found, "next_after": None, "head": len(self.events.get(scope, []))}

    def r_candidates(self, request: Request, body: Any, wg: str) -> tuple[int, Any]:
        return 200, {"candidates": self.candidates.get(wg, [])}

    def r_placement_get(self, request: Request, body: Any, wg: str) -> tuple[int, Any]:
        return 200, {"placement": self.placements.get(wg)}

    def r_placement_put(self, request: Request, body: Json, wg: str) -> tuple[int, Any]:
        if set(body) != {"device_id", "vm_id"} or (body["device_id"] is None and body["vm_id"] is not None):
            return 400, {"error": "request_invalid"}
        self.placements[wg] = None if body["device_id"] is None else dict(body)
        return 200, {"placement": self.placements[wg]}

    def r_task(self, request: Request, body: Any, task: str) -> tuple[int, Any]:
        found = self.tasks.get(task)
        return (404, {"error": "not_found"}) if found is None else (200, found)

    # --- tasks (the checks of agent_hub/tasks/service.py that the tool's tests rely on) ---

    def _task_events(self, wg: str, task: str, events: list[Json], kinds: list[str]) -> bool:
        headers = [event["header"] for event in events]
        return [h["kind"] for h in headers] == kinds and all(
            (h["scope"], h["scope_id"], h["task"]) == ("wg", wg, task) for h in headers
        )

    def r_task_create(self, request: Request, body: Json) -> tuple[int, Any]:
        wg, task = body["wg_id"], body["task_id"]
        org = self.org_of_wg(wg)
        if org is None or self._member(request, org) is None:
            return 404, {"error": "not_found"}
        if task in self.tasks:
            return 200, {**self.tasks[task]["created"], "replayed": True}
        if not self._task_events(wg, task, body["events"], ["task.meta", "prompt"]):
            return 400, {"error": "invalid"}
        if wg in self.no_device:
            return 409, {"error": "no_device"}
        for event in body["events"]:
            self.store_event(wg, event)
        row = {"task_id": task, "org_id": org, "wg_id": wg, "owner_id": request.user, "state": "queued"}
        row |= {"state_reason": None, "created_at": now_ms_text(), "last_activity_at": now_ms_text(), "closed_at": None}
        placement = body.get("placement")
        row["placement"] = None if placement is None else {"vm_id": None, **placement}
        created = {"task": row, "session": {"session_id": uuid7()}, "events": []}
        self.tasks[task] = {"task": row, "sessions": [], "transitions": [], "created": created}
        return 200, created

    def r_task_prompt(self, request: Request, body: Json, task: str) -> tuple[int, Any]:
        found = self.tasks.get(task)
        if found is None:
            return 404, {"error": "not_found"}
        wg = found["task"]["wg_id"]
        if not self._task_events(wg, task, [body["event"]], ["prompt"]):
            return 400, {"error": "invalid"}
        self.store_event(wg, body["event"])
        return 200, {"scope_seq": len(self.events[wg]), "session_id": found["created"]["session"]["session_id"]}

    def r_wg_tasks(self, request: Request, body: Any, wg: str) -> tuple[int, Any]:
        org = self.org_of_wg(wg)
        if org is None or self._member(request, org) is None:
            return 404, {"error": "not_found"}
        state = request.query.get("state")
        rows = [t["task"] for t in self.tasks.values() if t["task"]["wg_id"] == wg]
        return 200, {"tasks": [r for r in rows if state is None or r["state"] == state], "next_before": None}

    # --- the situation view (the shapes of 03-api.md section 4.6 and agent_hub's attention endpoints) ---

    def _attention_rows(self, request: Request, org: str) -> list[tuple[Json, Json]]:
        rows = [(self.tasks[task]["task"], row) for task, row in self.attention.items() if task in self.tasks]
        return [(task, row) for task, row in rows if task["org_id"] == org]

    def r_attention(self, request: Request, body: Any, org: str) -> tuple[int, Any]:
        if self._member(request, org) is None:
            return 404, {"error": "not_found"}
        category = request.query.get("category")
        if category not in ("needs_decision", "blocked", "working", "done"):
            return 400, {"error": "invalid"}
        rows = [(t, a) for t, a in self._attention_rows(request, org) if a["category"] == category]
        if request.query.get("mine") == "1":
            rows = [(t, a) for t, a in rows if t["owner_id"] == request.user]
        ascending = category == "needs_decision"
        rows.sort(key=lambda pair: (pair[1]["since"], pair[1]["task_id"]), reverse=not ascending)
        before = request.query.get("before")
        if before is not None:
            since, _, task = before.partition("|")
            rows = [
                (t, a)
                for t, a in rows
                if (
                    (a["since"], a["task_id"]) > (since, task)
                    if ascending
                    else (a["since"], a["task_id"]) < (since, task)
                )
            ]
        limit = min(int(request.query.get("limit", "50")), 100)
        page = rows[:limit]
        following = f"{page[-1][1]['since']}|{page[-1][1]['task_id']}" if len(rows) > limit else None
        return 200, {"items": [{"task": t, "attention": a} for t, a in page], "next_before": following}

    def r_attention_summary(self, request: Request, body: Any, org: str) -> tuple[int, Any]:
        if self._member(request, org) is None:
            return 404, {"error": "not_found"}
        rows = self._attention_rows(request, org)

        def count(category: str, mine: bool = False) -> int:
            return sum(1 for t, a in rows if a["category"] == category and (not mine or t["owner_id"] == request.user))

        return 200, {
            "needs_decision": count("needs_decision"),
            "needs_decision_mine": count("needs_decision", mine=True),
            "blocked": count("blocked"),
            "working": count("working"),
            "done_since": count("done"),
        }


def head_of(log: VerifiedLog) -> Head:
    return log.head
