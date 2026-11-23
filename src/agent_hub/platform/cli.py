"""Operator CLI of the platform log: `agent-hub platform …` (ADR-023, 04-crypto.md sections 4.8 and 5.9).

The platform root key is created offline and kept by the operator outside the hub (`init`, key file 0600). Every
other command talks to the hub with root-signed requests (`agent-request-v1`, key ID `platform-root`):

1. `GET /v1/config`: the hub's `platform_root_key` must be the operator's key (`platform_root_mismatch`); the hub key
   named there is pinned on first use in the operator record `<key file>.state` (a changed key: `hub_key_changed`) and
   verifies every further response (`agent-response-v1`); `--hub` must be https (http only for localhost);
2. `GET /v1/platform/enrollments`: the platform log, verified here with P1-P4 against the operator's own key (a hub
   cannot make the CLI extend a log the root did not sign) and required to extend the head recorded in the operator
   record (`log_rollback`: a hub cannot make the root sign a fork of a shortened log), and the pending enrollments;
3. the new entries are built on the verified head, checked locally with the same rules as the hub, signed with the
   root key and sent with `POST /v1/platform/log` (a `root` entry first while the log is empty). A concurrent append
   (`seq_conflict`) is retried on the new head.

Commands (entries they append):

- `init --key-file F [--hub URL]`: creates the key (never overwrites one) and prints its public key; with `--hub` it
  appends `root`;
- `invite-user [--expires-hours H]`: `user_invite` with `pk_inv` (ADR-043); prints the code (the hub sees only
  `invite_id` and `pk_inv`);
- `invite-device [--expires-hours H]`: `device_invite` with `pk_inv`; prints the code;
- `certify-pending [--device ID ...]`: one `device_certify` per verified pending enrollment (or the named devices);
- `grant-org-access --org ID [--revoke]`: `org_platform_access` (ADR-026);
- `grant-role inviter --user ID`, `revoke-role inviter --user ID`: `role_grant`, `role_revoke`;
- `decertify --device ID`: `device_decertify`.

Output never contains the root key or a hub secret; the invitation code is printed because the operator hands it on.
Exit codes: 0 done, 1 refused (by the hub or by a local check; the line names the code), 2 usage or key-file error.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO, cast

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_core.signing import (
    HEADER_HUB_KEY,
    HEADER_HUB_SIGNATURE,
    HEADER_HUB_TIME,
    format_time,
    request_text,
    response_text,
    sha256_hex,
)
from agent_hub.clock import Clock, epoch_seconds, system_clock
from agent_hub.config import LIMITS
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.encoding import format_time as entry_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.ids import is_uuid7
from agent_hub.crypto.keys import ed25519_private, ed25519_public_raw, ed25519_seed, ed25519_verify
from agent_hub.log.entries import PLATFORM_ROOT, VERSION, Head, check_extends, entry_hash, sign_platform_entry
from agent_hub.log.platform import VerifiedPlatformLog, append_platform, verify_platform_log

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
KEY_FILE_VERSION = 1
KEY_FILE_KEYS = frozenset({"v", "root"})
MAX_ATTEMPTS = 3
HOURS = 3600


class Refused(Exception):
    """A refusal with a code (from the hub or a local check)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Appended:
    """What an append did: whether it wrote the `root` entry first, and the `seq` of each requested entry."""

    root: bool
    seqs: list[int]


Builder = Callable[["VerifiedPlatformLog | None", list[dict[str, Any]]], list[dict[str, Any]]]


class UsageError(Exception):
    """A usage error (exit code 2); the message never contains key material."""


class KeyFileError(UsageError):
    """The key file is missing, unreadable, too open or malformed."""


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def state_path(key_file: Path) -> Path:
    """The operator's record next to the key file: per hub the pinned hub key and the last verified log head."""
    return key_file.with_name(key_file.name + ".state")


def load_state(path: Path) -> dict[str, Any]:
    """The operator record (empty when missing); a damaged record is a usage error, never silently replaced."""
    if not path.exists():
        return {"v": 1, "hubs": {}}
    try:
        data = json.loads(path.read_text(encoding="ascii"))
        if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("hubs"), dict):
            raise ValueError
    except (OSError, ValueError) as error:
        raise UsageError(f"{path} is not an operator record") from error
    return data


def save_state(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True, indent=1) + "\n", encoding="ascii")
    temporary.chmod(0o600)
    temporary.replace(path)


def create_key_file(path: Path) -> Ed25519PrivateKey:
    """Create a new root key in `path` (mode 0600); refuses an existing file."""
    key = ed25519_private(secrets.token_bytes(32))
    text = json.dumps({"v": KEY_FILE_VERSION, "root": b64u_encode(ed25519_seed(key))}) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise KeyFileError(f"{path} exists; refusing to overwrite a root key") from error
    except OSError as error:
        raise KeyFileError(f"{path} cannot be created") from error
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(text)
    return key


def load_key_file(path: Path) -> Ed25519PrivateKey:
    """The root key of `path`; the file must belong to nobody but its owner (no group or other permissions)."""
    try:
        mode = path.stat().st_mode
        raw = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise KeyFileError(f"{path} cannot be read") from error
    if stat.S_IMODE(mode) & 0o077:
        raise KeyFileError(f"{path} must not be accessible to group or others (chmod 600)")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != KEY_FILE_KEYS or data["v"] != KEY_FILE_VERSION:
            raise ValueError
        return ed25519_private(b64u_decode(data["root"], 32))
    except (ValueError, CryptoError) as error:
        raise KeyFileError(f"{path} is not a platform root key file") from error


def public_text(key: Ed25519PrivateKey) -> str:
    """b64u of the root public key (the value of `AGENT_HUB_PLATFORM_ROOT_KEY`)."""
    return b64u_encode(ed25519_public_raw(key))


@dataclass
class Operator:
    """Root-signed requests to one hub."""

    http: httpx.Client
    key: Ed25519PrivateKey
    clock: Clock
    record: Path | None = None
    hub_key: bytes | None = None
    hub_key_id: str = ""

    @property
    def origin(self) -> str:
        return str(self.http.base_url).rstrip("/")

    def _record(self) -> dict[str, Any]:
        if self.record is None:
            return {}
        hubs: dict[str, Any] = load_state(self.record)["hubs"]
        return cast(dict[str, Any], hubs.get(self.origin, {}))

    def _remember(self, **values: Any) -> None:
        if self.record is None:
            return
        data = load_state(self.record)
        data["hubs"][self.origin] = {**data["hubs"].get(self.origin, {}), **values}
        save_state(self.record, data)

    def connect(self) -> None:
        """Read `/v1/config`: the hub must use this root key; its hub key is pinned on first use (a changed hub key
        is refused) and verifies every further response."""
        response = self.http.get("/v1/config")
        if response.status_code != 200:
            raise Refused("unavailable")
        config = response.json()
        if config.get("platform_root_key") != public_text(self.key):
            raise Refused("platform_root_mismatch")
        hub_key = {"id": str(config["hub_key"]["id"]), "public_key": str(config["hub_key"]["public_key"])}
        pinned = self._record().get("hub_key")
        if pinned is not None and pinned != hub_key:
            raise Refused("hub_key_changed")
        self.hub_key = b64u_decode(hub_key["public_key"], 32)
        self.hub_key_id = hub_key["id"]
        if pinned is None:
            self._remember(hub_key=hub_key)

    def request(self, method: str, target: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One signed request; the JSON answer of a 2xx response with a valid hub signature, else `Refused`."""
        content = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("ascii")
        agent_time = format_time(epoch_seconds(self.clock))
        nonce = b64u_encode(secrets.token_bytes(16))
        host = self.http.base_url.netloc.decode("ascii")
        text = request_text(PLATFORM_ROOT, method, host, target, agent_time, nonce, sha256_hex(content))
        headers = {
            "Agent-Key": PLATFORM_ROOT,
            "Agent-Time": agent_time,
            "Agent-Nonce": nonce,
            "Agent-Signature": b64u_encode(self.key.sign(text)),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.http.request(method, target, content=content, headers=headers)
        self._check_signature(response, nonce, agent_time)
        data = response.json()
        if response.status_code >= 300:
            raise Refused(str(data.get("error", "unavailable")) if isinstance(data, dict) else "unavailable")
        return cast(dict[str, Any], data)

    def _check_signature(self, response: httpx.Response, nonce: str, agent_time: str) -> None:
        hub_time = response.headers.get(HEADER_HUB_TIME, "")
        text = response_text(
            PLATFORM_ROOT, nonce, agent_time, response.status_code, hub_time, sha256_hex(response.content)
        )
        try:
            if response.headers.get(HEADER_HUB_KEY) != self.hub_key_id or self.hub_key is None:
                raise ValueError
            ed25519_verify(self.hub_key, b64u_decode(response.headers.get(HEADER_HUB_SIGNATURE, ""), 64), text)
        except Exception as error:
            raise Refused("signature_invalid") from error

    def snapshot(self) -> tuple[VerifiedPlatformLog | None, list[dict[str, Any]]]:
        """The verified platform log (None while empty) and the pending enrollments."""
        data = self.request("GET", "/v1/platform/enrollments")
        entries = data["log"]
        stored = self._record().get("head")
        try:
            log = verify_platform_log(entries, public_text(self.key)) if entries else None
            check_extends(None if stored is None else Head(int(stored["seq"]), str(stored["hash"])), entries)
        except CryptoError as error:
            raise Refused(error.code) from error
        if log is not None:
            self._remember(head=log.state.head_value().to_json())
        return log, list(data["enrollments"])

    def append(self, build: Builder) -> Appended:
        """Build bodies on the verified head, sign and append them (with `root` first while the log is empty)."""
        attempt = 1
        while True:
            log, pending = self.snapshot()
            bodies = build(log, pending)
            planned = ([{"type": "root", "pub": public_text(self.key)}] if log is None else []) + bodies
            if not planned:
                return Appended(root=False, seqs=[])
            entries = self._sign(log, planned)
            try:
                append_platform(log, entries, public_text(self.key))
            except CryptoError as error:
                raise Refused(error.code) from error
            expected = 0 if log is None else log.state.seq + 1
            try:
                answer = self.request("POST", "/v1/platform/log", {"expected_seq": expected, "entries": entries})
            except Refused as refusal:
                if refusal.code != "seq_conflict" or attempt >= MAX_ATTEMPTS:
                    raise
                attempt += 1
                continue
            self._remember(head=answer["head"])
            first = expected + len(planned) - len(bodies)
            return Appended(root=log is None, seqs=list(range(first, expected + len(planned))))

    def _sign(self, log: VerifiedPlatformLog | None, planned: list[dict[str, Any]]) -> list[dict[str, object]]:
        ts = entry_time(self.clock())
        seq, prev = (0, "") if log is None else (log.state.seq + 1, log.state.head)
        entries: list[dict[str, object]] = []
        for fields in planned:
            body = {"seq": seq, "prev": prev, "by": PLATFORM_ROOT, "ts": ts, "v": VERSION, **fields}
            entry = sign_platform_entry(body, self.key)
            entries.append(entry)
            prev = entry_hash(entry)
            seq += 1
        return entries


def uuid_text(value: str) -> str:
    """argparse type: a UUIDv7 in lower-case text form (the ID form of the logs)."""
    if not is_uuid7(value):
        raise argparse.ArgumentTypeError("expected a UUIDv7 in lower-case text form")
    return value


def hours(value: str) -> int:
    """argparse type: whole hours from 1 to the invitation maximum of 30 days (ADR-005)."""
    if not value.isdigit() or not 1 <= int(value) <= LIMITS["invite_max_seconds"] // HOURS:
        raise argparse.ArgumentTypeError("expected whole hours from 1 to 720")
    return int(value)


def _expires(operator: Operator, hours_ahead: int) -> str:
    return entry_time(operator.clock() + timedelta(hours=hours_ahead))


def invite(operator: Operator, kind: str, hours_ahead: int, out: TextIO) -> None:
    """`user_invite` or `device_invite` for a new code; prints the code, the invitation ID and the expiry."""
    code = invite_codes.generate_code()
    derived = invite_codes.invite_secrets(code)
    expires = _expires(operator, hours_ahead)
    body: dict[str, Any] = {
        "type": f"{kind}_invite",
        "invite_id": derived.invite_id,
        "pk_inv": derived.pk_inv,
        "expires": expires,
    }
    done = operator.append(lambda _log, _pending: [body])
    print(f"invitation code {invite_codes.format_code(code)}", file=out)
    print(f"invite_id {derived.invite_id} expires {expires} (seq {done.seqs[0]})", file=out)


def certify_bodies(pending: list[dict[str, Any]], devices: Sequence[str]) -> list[dict[str, Any]]:
    """`device_certify` bodies for the pending enrollments (all, or the named devices, which must all be pending)."""
    chosen = [item for item in pending if not devices or item["device_id"] in devices]
    missing = set(devices) - {item["device_id"] for item in chosen}
    if missing:
        raise Refused("not_found")
    return [
        {
            "type": "device_certify",
            "device": item["device_id"],
            "pub": item["pub"],
            "invite_id": item["invite_id"],
            "enroll_sig": item["enroll_sig"],
            "inv_sig": item["inv_sig"],
            "agent_version": item["agent_version"],
            "os": item["os"],
            "arch": item["arch"],
            "capacity": item["capacity"],
        }
        for item in chosen
    ]


def certify_pending(operator: Operator, devices: Sequence[str], out: TextIO) -> None:
    """Certify pending enrollments; the enrollment signatures are verified locally before signing."""
    built: list[dict[str, Any]] = []

    def build(_log: VerifiedPlatformLog | None, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        built[:] = certify_bodies(pending, devices)
        return list(built)

    done = operator.append(build)
    if not built:
        print("no pending enrollments", file=out)
    for body, seq in zip(built, done.seqs, strict=True):
        print(f"certified {body['device']} (seq {seq})", file=out)


def simple(operator: Operator, body: dict[str, Any], out: TextIO) -> None:
    """Append one entry and print its type and `seq`."""
    done = operator.append(lambda _log, _pending: [body])
    print(f"appended {body['type']} (seq {done.seqs[0]})", file=out)


def init(args: argparse.Namespace, http: httpx.Client | None, clock: Clock, out: TextIO) -> int:
    path = Path(args.key_file)
    if args.hub is None or not path.exists():
        key = create_key_file(path)
        print(f"root public key {public_text(key)}", file=out)
        print(f"key file {path} (mode 0600); keep it offline", file=out)
    if args.hub is None:
        return EXIT_OK
    operator = open_operator(args, http, clock)
    done = operator.append(lambda _log, _pending: [])
    print("root entry appended (seq 0)" if done.root else "root entry present", file=out)
    return EXIT_OK


def open_operator(args: argparse.Namespace, http: httpx.Client | None, clock: Clock) -> Operator:
    path = Path(args.key_file)
    key = load_key_file(path)
    url = httpx.URL(args.hub)
    if url.scheme != "https" and not (url.scheme == "http" and url.host in LOCAL_HOSTS):
        raise UsageError("--hub must be an https URL (http only for localhost)")
    client = http if http is not None else httpx.Client(base_url=args.hub, timeout=30.0)
    operator = Operator(http=client, key=key, clock=clock, record=state_path(path))
    operator.connect()
    return operator


def dispatch(args: argparse.Namespace, http: httpx.Client | None, clock: Clock, out: TextIO) -> int:
    """Run one command (the parsed arguments of `register`)."""
    if args.platform_command == "init":
        return init(args, http, clock, out)
    operator = open_operator(args, http, clock)
    command = args.platform_command
    if command in ("invite-user", "invite-device"):
        invite(operator, command.removeprefix("invite-"), args.expires_hours, out)
    elif command == "certify-pending":
        certify_pending(operator, args.device or [], out)
    elif command == "grant-org-access":
        simple(operator, {"type": "org_platform_access", "org": args.org, "granted": not args.revoke}, out)
    elif command in ("grant-role", "revoke-role"):
        kind = "role_grant" if command == "grant-role" else "role_revoke"
        simple(operator, {"type": kind, "subject": args.user, "role": args.role}, out)
    else:
        simple(operator, {"type": "device_decertify", "device": args.device}, out)
    return EXIT_OK


def run(
    args: argparse.Namespace,
    *,
    http: httpx.Client | None = None,
    clock: Clock = system_clock,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """The handler of `agent-hub platform …`; `http` and `clock` are injected by tests."""
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    try:
        return dispatch(args, http, clock, stdout)
    except UsageError as error:
        print(f"agent-hub platform: {error}", file=stderr)
        return EXIT_USAGE
    except Refused as refusal:
        print(f"agent-hub platform: refused: {refusal.code}", file=stderr)
        return EXIT_REFUSED
    except httpx.HTTPError:
        print("agent-hub platform: the hub cannot be reached", file=stderr)
        return EXIT_REFUSED
    except (ValueError, KeyError, TypeError):
        print("agent-hub platform: refused: unavailable (the hub's answer is malformed)", file=stderr)
        return EXIT_REFUSED


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add `agent-hub platform <command>`."""
    parser = subparsers.add_parser("platform", help="operator commands of the platform log (root key)")
    commands = parser.add_subparsers(dest="platform_command", metavar="command", required=True)

    def command(name: str, help_text: str, *, hub: bool = True) -> argparse.ArgumentParser:
        sub: argparse.ArgumentParser = commands.add_parser(name, help=help_text)
        sub.add_argument("--key-file", required=True, help="the platform root key file (0600)")
        sub.add_argument("--hub", required=hub, default=None, help="hub URL, e.g. https://agent.example")
        return sub

    command("init", "create the root key offline; with --hub also append the root entry", hub=False)
    user = command("invite-user", "append a user_invite and print the code")
    user.add_argument("--expires-hours", type=hours, default=LIMITS["invite_user_seconds"] // HOURS)
    device = command("invite-device", "append a device_invite and print the code")
    device.add_argument("--expires-hours", type=hours, default=LIMITS["invite_device_seconds"] // HOURS)
    certify = command("certify-pending", "certify pending platform-device enrollments")
    certify.add_argument("--device", type=uuid_text, action="append", help="only this device (repeatable)")
    access = command("grant-org-access", "grant (or with --revoke take back) platform access of an org")
    access.add_argument("--org", type=uuid_text, required=True)
    access.add_argument("--revoke", action="store_true")
    for name in ("grant-role", "revoke-role"):
        role = command(name, f"{name.split('-')[0]} a platform role")
        role.add_argument("role", choices=["inviter"])
        role.add_argument("--user", type=uuid_text, required=True)
    decertify = command("decertify", "decertify a platform device")
    decertify.add_argument("--device", type=uuid_text, required=True)
    parser.set_defaults(handler=run)
