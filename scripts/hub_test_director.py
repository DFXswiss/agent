"""Device double of the web Playwright suite (WEB-04): platform execution devices and scripted session transitions.

The runner (and its outbox, HUB-10) is not part of the web suite, so this process plays the device side next to the
test hub (`scripts/hub_test_server.py` starts it on `--port` with the hub's writer database and origin):

- `POST /devices {"online": bool, "pool_ready": int, "free": int}`: a new platform device with real keys -
  `device_invite` in the platform log (root-signed requests with the public test root key, decision WEB-01 12), the
  enrollment signed by the device (`POST /v1/device/enroll`), `device_certify` (with retries on `seq_conflict`) - so
  that the browser verifies its certification against the compiled root key. Every device created before is set
  offline and gets no free slot, so the new device is the first candidate (with `free` 0 it has no free slot either:
  `capacity_exhausted`). `online` keeps a fresh presence row (heartbeat every few seconds, as the device socket of
  HUB-07 stores it); `pool_ready` warm VMs give an estimate of 3 s, none 8 s (03-api.md section 4.6.2).
- `POST /devices/<id>/online {"online": bool}`: presence on or off; on also re-runs the scheduler for waiting sessions
  (what the hub does when a device connects).
- `GET /health`: `{"ok": true}` once the server listens.
- `GET /tasks/<task id>/session`: the task's main session `{session_id, device_id, state}`.
- `POST /sessions/<id>/transitions {"states": [...], "reason": str | null, "interval_ms": int}`: the transitions as
  the outbox applies them (`agent_hub.sessions.transitions.apply`, reported by the session's device, device time =
  hub time - 40 ms), one every `interval_ms` in a background thread (`reason` for a final `failed`).
- `POST /sessions/<id>/events {"events": [{"kind", "body", "ts_offset_ms"?}]}`: device-authored session events sealed
  and stored like the runner's outbox would (`scripts/hub_test_events.py`, WEB-05); answers `{"scope_seqs": [...]}`.
- `POST /sessions/<id>/children {"state": str, "model_id": str | null}`: a subagent session of the session (WEB-05);
  answers `{"session_id"}`.
- `POST /sessions/<id>/operations {"type", "params", "outcome"?, "dedupe"?}`: an operation registered and finished
  through the hub's device API (`scripts/hub_test_operations.py`, WEB-09).
- `POST /requests/answer {"device", "steps"}`: answers the device's newest open device request
  (`scripts/hub_test_requests.py`, WEB-09).
- `POST /devices/<id>/{vms,metrics,usage,rollups}`: VMs, metric samples, usage and rollups of the monitoring screen
  (`scripts/hub_test_metrics.py`, WEB-12).
- `POST|GET /volume/seed`, `POST /volume/{invite,joined,live}`: the data set of the volume gate
  (`scripts/hub_test_volume.py`, KOR-35), cached in a lane of the volume gate (`scripts/hub_test_volume_cache.py`,
  HOT-21).
- `POST /sessions/<id>/orchestrator {"pairing_id", "pub", "pop", "fp", "forged"?}`: the keys of a main orchestrator
  session registered by the session's device (`scripts/hub_test_orchestrator.py`, ADR-082 section 6); `GET` of the
  same path: the device's view with the stored handover. Both answer `{status, body}` of the hub.
- `POST /orgs/<id>/people {"wg", "count"}`: `count` further people of the org (KOR-52, the situation view by person),
  written directly into the hub's tables like the hub's volume tests do: a user principal, an org membership and read
  access to `wg` (sequence 0, no log entry: people that never sign in) and one open task in `wg` each (numbered by the
  hub's trigger, no sealed events, no session); answers `{"owners": [...]}` in creation order.

Frames and hints of these changes reach the hub's browsers over the PostgreSQL bus (`hub_stream`), exactly like the
frames of another hub process (`agent_hub.stream.bus`). Only loopback, only for tests.

    python scripts/hub_test_director.py --port 20264 --hub http://localhost:20262 (AGENT_HUB_DATABASE set)
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_core.signing import request_text
from agent_hub import signals
from agent_hub.crypto import enroll as enroll_text
from agent_hub.crypto import invite as invite_format
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.keys import Identity, ed25519_private, ed25519_sign
from agent_hub.device.keys import key_id_of
from agent_hub.log.entries import entry_hash, sign_platform_entry
from agent_hub.scheduler.reschedule import reschedule
from agent_hub.sessions.transitions import apply
from agent_hub.stream.bus import STREAM_CHANNEL, encode
from agent_hub.stream.publish import Published, set_publisher


def _sibling(name: str) -> ModuleType:
    """A module next to this script (scripts/ is no package)."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module  # dataclasses of the module look themselves up there
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


hub_test_events = _sibling("hub_test_events")
hub_test_requests = _sibling("hub_test_requests")
hub_test_operations = _sibling("hub_test_operations")
hub_test_metrics = _sibling("hub_test_metrics")
hub_test_volume = _sibling("hub_test_volume")
hub_test_orchestrator = _sibling("hub_test_orchestrator")

ROOT_SEED = hashlib.sha256(b"agent.dfx.swiss test platform root key").digest()
HEARTBEAT_SECONDS = 5.0
ONLINE_SECONDS = int(os.environ.get("AGENT_HUB_DEVICE_ONLINE_SECONDS") or 30)
"""The hub's online window (same variable and default as the hub configuration)."""
SLOTS = 8
VM_MIB = 8192
Json = dict[str, Any]
THROWAWAY_PREFIX = "t_web_"
"""Name prefix of the databases `scripts/hub_test_server.py` creates (and drops) for the suite."""


def now() -> datetime:
    return datetime.now(UTC)


def signed_headers(key_id: str, sign: Callable[[bytes], bytes], method: str, url: str, body: bytes) -> dict[str, str]:
    """`agent-request-v1` headers (03-api.md section 1)."""
    parsed = httpx.URL(url)
    target = parsed.raw_path.decode("ascii")
    agent_time = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = b64u_encode(secrets.token_bytes(16))
    text = request_text(
        key_id, method, parsed.netloc.decode("ascii"), target, agent_time, nonce, hashlib.sha256(body).hexdigest()
    )
    headers = {"agent-key": key_id, "agent-time": agent_time, "agent-nonce": nonce}
    headers["agent-signature"] = b64u_encode(sign(text))
    if body:
        headers["content-type"] = "application/json"
    return headers


FEATURES = ("log.admit", "log.org_assign", "log.policy", "log.support", "vm.choice", "github.identities")


class BusPublisher:
    """Publishes every frame and hint of this process on the hub's bus channel (like another hub process)."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
        self.pool = pool
        self.instance = "web-test-director-" + secrets.token_hex(4)

    def publish(self, item: Published) -> None:
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("SELECT pg_notify(%s, %s)", (STREAM_CHANNEL, encode(self.instance, item)))


class Director:
    """The devices and sessions of the suite (module docstring)."""

    def __init__(self, hub: str, pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
        self.hub = hub.rstrip("/")
        self.pool = pool
        self.root = ed25519_private(ROOT_SEED)
        self.lock = threading.Lock()
        self.online: set[str] = set()
        self.devices: list[str] = []
        self.identities: dict[str, Identity] = {}
        self.certified: dict[str, int] = {}
        self.http = httpx.Client(timeout=30)
        self.context = SimpleNamespace(
            pool=pool, clock=now, config=SimpleNamespace(device_online_seconds=ONLINE_SECONDS)
        )
        self.metrics = hub_test_metrics.MetricsDouble(self, signed_headers)
        self.volume = hub_test_volume.Seeder(self)

    # --- platform log ---

    def _root_request(self, method: str, path: str, body: Json | None = None) -> httpx.Response:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        url = self.hub + path
        headers = signed_headers("platform-root", lambda text: ed25519_sign(self.root, text), method, url, raw)
        return self.http.request(method, url, content=raw, headers=headers)

    def append(self, *bodies: Json) -> int:
        """Appends root-signed entries after the current head; the `seq` of the last one."""
        for _attempt in range(30):
            log = list(self._root_request("GET", "/v1/platform/enrollments").json()["log"])
            planned = list(bodies)
            if not log:
                planned.insert(0, {"type": "root", "pub": b64u_encode(self.root.public_key().public_bytes_raw())})
            prev = entry_hash(log[-1]) if log else ""
            entries = []
            for offset, body in enumerate(planned):
                full = {"seq": len(log) + offset, "prev": prev, "by": "platform-root", "ts": format_time(now()), "v": 1}
                entry = sign_platform_entry({**full, **body}, self.root)
                entries.append(entry)
                prev = entry_hash(entry)
            answer = self._root_request("POST", "/v1/platform/log", {"expected_seq": len(log), "entries": entries})
            if answer.status_code == 200:
                return len(log) + len(planned) - 1
            if answer.json().get("error") != "seq_conflict":
                raise RuntimeError(f"platform log append refused: {answer.text}")
            time.sleep(0.05)
        raise RuntimeError("platform log append kept conflicting")

    # --- devices ---

    def capacity(self, free: int, pool_ready: int) -> Json:
        return {
            "vm_memory_budget_mib": SLOTS * VM_MIB,
            "vm_memory_used_mib": (SLOTS - free) * VM_MIB,
            "vm_slots_total": SLOTS,
            "vm_slots_free": free,
            "pool_ready": pool_ready,
        }

    def create_device(self, online: bool, pool_ready: int, free: int = SLOTS, exclusive: bool = True) -> str:
        """A certified platform device; `exclusive` (the default) makes it the first candidate (module docstring)."""
        identity = Identity.generate()
        device_id = str(uuid7())
        found = invite_format.invite_secrets(invite_format.generate_code())
        expires = format_time(now() + timedelta(hours=24))
        self.append({"type": "device_invite", "invite_id": found.invite_id, "pk_inv": found.pk_inv, "expires": expires})
        body = {
            "device_id": device_id,
            "kind": "platform",
            "invite_id": found.invite_id,
            "pub": identity.public(),
            "agent_version": "0.1.0",
            "os": "darwin",
            "arch": "arm64",
            "capacity": self.capacity(SLOTS, pool_ready),
        }
        signed = enroll_text.sign_enrollment(body, identity.sig, found.signing)
        raw = json.dumps(signed).encode("utf-8")
        url = self.hub + "/v1/device/enroll"
        headers = signed_headers(key_id_of(device_id), identity.sig.sign, "POST", url, raw)
        answer = self.http.post(url, content=raw, headers=headers)
        if answer.status_code != 200:
            raise RuntimeError(f"enrollment refused: {answer.text}")
        fields = {name: signed[name] for name in ("pub", "invite_id", "enroll_sig", "inv_sig", "agent_version")}
        fields.update({name: signed[name] for name in ("os", "arch", "capacity")})
        seq = self.append({"type": "device_certify", "device": device_id, **fields})
        with self.lock:
            earlier = list(self.devices) if exclusive else []
            self.devices.append(device_id)
            self.identities[device_id] = identity
            self.certified[device_id] = seq
        for other in earlier:
            self.set_online(other, False)
            self.set_capacity(other, self.capacity(0, 0))
        self.set_capacity(device_id, self.capacity(free, pool_ready))
        # what a current runner announces on its live channel (`agent_runner.sync.ws.FEATURES`): the hub holds back
        # platform-log entries an older runner could not verify (ADR-060, ADR-079, ADR-096)
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("UPDATE hub.device SET features = %s WHERE id = %s", (list(FEATURES), device_id))
        self.set_online(device_id, online)
        return device_id

    def certified_seq(self, device_id: str) -> int:
        """The platform-log `seq` of the device's `device_certify`."""
        return self.certified[device_id]

    def hub_key(self) -> Json:
        """The hub's signing key as `GET /v1/config` publishes it (invitation packages name it)."""
        return dict(self.http.get(self.hub + "/v1/config").json()["hub_key"])

    def set_capacity(self, device_id: str, capacity: Json) -> None:
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("UPDATE hub.device SET capacity = %s WHERE id = %s", (Jsonb(capacity), device_id))

    def heartbeat(self, device_id: str) -> None:
        moment = now()
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.device_presence (device_id, hub_instance, connected_at, last_heartbeat_at)"
                " VALUES (%s, 'web-test-director', %s, %s) ON CONFLICT (device_id) DO UPDATE SET"
                " last_heartbeat_at = EXCLUDED.last_heartbeat_at",
                (device_id, moment, moment),
            )

    def set_online(self, device_id: str, online: bool) -> None:
        with self.lock:
            if online:
                self.online.add(device_id)
            else:
                self.online.discard(device_id)
        if online:
            self.heartbeat(device_id)
            with self.pool.connection() as connection:
                wgs = [
                    row[0]
                    for row in connection.execute(
                        "SELECT wg_id FROM hub.wg_device WHERE device_id = %s AND unassigned_seq IS NULL", (device_id,)
                    )
                ]
            reschedule(self.context, wgs)
        else:
            with self.pool.connection() as connection, connection.transaction():
                connection.execute("DELETE FROM hub.device_presence WHERE device_id = %s", (device_id,))

    def beat_forever(self) -> None:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            with self.lock:
                online = sorted(self.online)
            for device_id in online:
                self.heartbeat(device_id)

    def signed(self, device_id: str, method: str, path: str, body: Json | None) -> httpx.Response:
        """A request to the hub signed by the device `device_id` (its device API)."""
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        url = self.hub + path
        headers = signed_headers(key_id_of(device_id), self.identities[device_id].sig.sign, method, url, raw)
        return self.http.request(method, url, content=raw, headers=headers)

    def orchestrator(self, session_id: str, body: Json | None) -> Json:
        """The session's device in a main orchestrator pairing: registers the keys of `body`, or (None) reads the
        device's view (`scripts/hub_test_orchestrator.py`)."""
        if body is None:
            return cast(Json, hub_test_orchestrator.view(self.pool, self.signed, session_id))
        return cast(
            Json, hub_test_orchestrator.register(self.pool, self.signed, self.identities.__getitem__, session_id, body)
        )

    # --- sessions ---

    def main_session(self, task_id: str) -> Json | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT id, device_id, state FROM hub.session WHERE task_id = %s AND depth = 0"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {"session_id": str(row[0]), "device_id": None if row[1] is None else str(row[1]), "state": row[2]}

    def transition(self, session_id: str, state: str, reason: str | None) -> None:
        with self.pool.connection() as connection, connection.transaction():
            device = connection.execute("SELECT device_id FROM hub.session WHERE id = %s", (session_id,)).fetchone()
            if device is None or device[0] is None:
                raise RuntimeError("the session has no device")
            moment = now()
            applied = apply(
                connection,
                session_id,
                state,
                reason,
                moment - timedelta(milliseconds=40),
                received_at=moment,
                reported_by=str(device[0]),
            )
        applied.effects.flush()

    def walk(self, session_id: str, states: Sequence[str], reason: str | None, interval_ms: int) -> None:
        for index, state in enumerate(states):
            if index > 0:
                time.sleep(interval_ms / 1000)
            self.transition(session_id, state, reason if index == len(states) - 1 else None)

    # --- people (KOR-52) ---

    def seed_people(self, org: str, wg: str, count: int) -> list[str]:
        """`count` people of `org` with read access to `wg` and one open task there each (module docstring)."""
        owners = [str(uuid7()) for _ in range(count)]
        moment = now()
        with self.pool.connection() as connection, connection.transaction():
            for owner in owners:
                connection.execute(
                    "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (owner, moment)
                )
                connection.execute(
                    "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq)"
                    " VALUES (%s, %s, 'user', 'member', 0)",
                    (org, owner),
                )
                connection.execute(
                    "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq) VALUES (%s, %s, 'read', 0)",
                    (wg, owner),
                )
                connection.execute(
                    "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
                    " VALUES (%s, %s, %s, %s, 'idle', %s, %s, %s)",
                    (str(uuid7()), org, wg, owner, moment, moment, moment),
                )
        return owners

    def walk_logged(self, session_id: str, states: Sequence[str], reason: str | None, interval_ms: int) -> None:
        """`walk` in a background thread: a refused transition is printed (the spec then times out on its assertion)."""
        try:
            self.walk(session_id, states, reason, interval_ms)
        except Exception as error:
            print(f"director: transitions {list(states)} of {session_id} failed: {error}", file=sys.stderr, flush=True)


SESSION_RE = re.compile(r"^/tasks/([0-9a-f-]{36})/session$")
TRANSITIONS_RE = re.compile(r"^/sessions/([0-9a-f-]{36})/transitions$")
EVENTS_RE = re.compile(r"^/sessions/([0-9a-f-]{36})/events$")
CHILDREN_RE = re.compile(r"^/sessions/([0-9a-f-]{36})/children$")
OPERATIONS_RE = re.compile(r"^/sessions/([0-9a-f-]{36})/operations$")
ORCHESTRATOR_RE = re.compile(r"^/sessions/([0-9a-f-]{36})/orchestrator$")
ONLINE_RE = re.compile(r"^/devices/([0-9a-f-]{36})/online$")
PEOPLE_RE = re.compile(r"^/orgs/([0-9a-f-]{36})/people$")


def handler(director: Director) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, body: object) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> Json:
            length = int(self.headers.get("content-length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/volume/seed":
                return self._send(200, director.volume.status())
            if self.path == "/volume/live":
                return self._send(200, director.volume.live_count())
            if (orchestrator := ORCHESTRATOR_RE.match(self.path)) is not None:
                return self._send(200, director.orchestrator(orchestrator.group(1), None))
            match = SESSION_RE.match(self.path)
            found = None if match is None else director.main_session(match.group(1))
            self._send(404, {"error": "not_found"}) if found is None else self._send(200, found)

        def _session_post(self, body: Json) -> bool:
            """The routes of WEB-05 (events, children), WEB-09 (operations, requests), WEB-12 (monitoring) and KOR-52
            (people)."""
            if (match := EVENTS_RE.match(self.path)) is not None:
                seqs = hub_test_events.session_events(
                    director.pool,
                    director.identities.__getitem__,
                    director.root.public_key().public_bytes_raw(),
                    match.group(1),
                    list(body.get("events", [])),
                )
                self._send(200, {"scope_seqs": seqs})
                return True
            if (match := CHILDREN_RE.match(self.path)) is not None:
                child = hub_test_events.child_session(
                    director.pool, match.group(1), str(body.get("state", "working")), body.get("model_id")
                )
                self._send(200, {"session_id": child})
                return True
            if (match := OPERATIONS_RE.match(self.path)) is not None:
                found = hub_test_operations.operation(
                    director.pool, director.signed, director.identities.__getitem__, match.group(1), body
                )
                self._send(200, found)
                return True
            if self.path == "/requests/answer":
                answered = hub_test_requests.answer(
                    director.pool,
                    director.signed,
                    director.identities.__getitem__,
                    str(body["device"]),
                    list(body.get("steps", [])),
                )
                self._send(200, answered)
                return True
            answer = director.metrics.route(self.path, body)
            if answer is None:
                answer = self._volume_post(body)
            if answer is None and (match := ORCHESTRATOR_RE.match(self.path)) is not None:
                answer = director.orchestrator(match.group(1), body)
            if answer is None and (match := PEOPLE_RE.match(self.path)) is not None:
                answer = {"owners": director.seed_people(match.group(1), str(body["wg"]), int(body["count"]))}
            if answer is not None:
                self._send(200, answer)
            return answer is not None

        def _volume_post(self, body: Json) -> Json | None:
            """The routes of the volume gate (KOR-35, `scripts/hub_test_volume.py`)."""
            volume = director.volume
            if self.path == "/volume/seed":
                return dict(volume.start(dict(body.get("counts", {})), director.root.public_key().public_bytes_raw()))
            if self.path == "/volume/invite":
                return dict(volume.admin_invite())
            if self.path == "/volume/joined":
                return dict(volume.joined(str(body["user_id"])))
            if self.path == "/volume/live":
                return dict(volume.live_load(float(body.get("seconds", 10)), int(body.get("rate", 200))))
            return None

        def do_POST(self) -> None:
            try:
                body = self._body()
                if self.path == "/devices":
                    device = director.create_device(
                        bool(body.get("online", True)), int(body.get("pool_ready", 0)), int(body.get("free", SLOTS))
                    )
                    return self._send(200, {"device_id": device})
                if (match := ONLINE_RE.match(self.path)) is not None:
                    director.set_online(match.group(1), bool(body.get("online")))
                    return self._send(200, {})
                if (match := TRANSITIONS_RE.match(self.path)) is not None:
                    states = [str(state) for state in body.get("states", [])]
                    reason = body.get("reason")
                    interval = int(body.get("interval_ms", 0))
                    first, rest = states[:1], states[1:]
                    director.walk(match.group(1), first, reason if not rest else None, 0)
                    if rest:
                        threading.Thread(
                            target=director.walk_logged,
                            args=(match.group(1), rest, reason, interval),
                            kwargs={},
                            daemon=True,
                        ).start()
                    return self._send(200, {})
                if self._session_post(body):
                    return None
                return self._send(404, {"error": "not_found"})
            except Exception as error:  # the test reads the message
                return self._send(500, {"error": str(error)})

    return Handler


def volume_lane(director: Director, environ: Mapping[str, str]) -> None:
    """A lane of the volume gate (`scripts/hub_test_server.py --volume-cache`): take over the cached data set's
    state, or - holding the lane's seeding lock - seed at once (once the hub answers), store the data set in the cache
    and release the lock, done or failed (`scripts/hub_test_volume_cache.py`)."""
    if "WEB_E2E_VOLUME_STATE" in environ:
        director.volume.restore(json.loads(environ["WEB_E2E_VOLUME_STATE"]))
        return
    if "WEB_E2E_VOLUME_SNAPSHOT" not in environ:
        return
    target, lock = Path(environ["WEB_E2E_VOLUME_SNAPSHOT"]), Path(environ["WEB_E2E_VOLUME_LOCK"])
    admin_url, signing_key = environ["WEB_E2E_VOLUME_ADMIN_URL"], environ["WEB_E2E_HUB_SIGNING_KEY"]
    cache = _sibling("hub_test_volume_cache")
    holder = os.getppid()  # the hub test server took the lock

    def snapshot(state: Json) -> None:
        cache.store(target.parent, target.name, admin_url, state, signing_key)

    def finished() -> None:
        cache.release(lock, holder)

    director.volume.snapshot = snapshot
    director.volume.finished = finished

    def seed() -> None:
        try:
            wait_for_hub(director)
        except RuntimeError as error:
            print(f"volume: {error}", flush=True)
            finished()
            return
        director.volume.start({}, director.root.public_key().public_bytes_raw())

    threading.Thread(target=seed, daemon=True).start()


def wait_for_hub(director: Director, seconds: float = 120.0) -> None:
    """Waits until the hub answers `GET /v1/health`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if director.http.get(director.hub + "/v1/health").status_code == 200:
                return
        time.sleep(0.2)
    raise RuntimeError(f"the hub did not answer within {seconds:.0f} s")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--hub", required=True)
    args = parser.parse_args(argv)
    database = os.environ.get("AGENT_HUB_DATABASE")
    if not database:
        raise SystemExit("AGENT_HUB_DATABASE is not set")
    name = str(conninfo_to_dict(database).get("dbname") or "")
    if not name.startswith(THROWAWAY_PREFIX):
        # the director writes rows directly (sessions, tasks, the volume data set): only into the suite's own database
        raise SystemExit(f"AGENT_HUB_DATABASE must name a throwaway database of the suite ({THROWAWAY_PREFIX}*)")
    signals.configure({})
    pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(database, min_size=1, max_size=4, open=True)
    set_publisher(BusPublisher(pool))
    director = Director(args.hub, pool)
    volume_lane(director, os.environ)
    threading.Thread(target=director.beat_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(director))
    try:
        server.serve_forever()
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
