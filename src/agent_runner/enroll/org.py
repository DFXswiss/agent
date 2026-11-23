"""Org device enrollment `agent-runner enroll --hub URL --code CODE` (05-runner.md section 2.1, 04-crypto.md sections
3, 4.6, 4.7, 5.8 and 6.1, 03-api.md sections 4.1 and 6.1, ADR-005, ADR-007).

1. Refuse an existing identity (`already_enrolled`; a half identity is `identity_incomplete`).
2. Parse the code (`invite_code_invalid` / `invite_code_length`) and derive `invite_id`, `package_key` and `sk_inv`.
3. `POST /v1/invites/lookup` (anonymous, not response-signed): an unknown, used, revoked or expired invitation is
   `invite_unknown` (one code for all, ADR-005); a user or platform invitation is `invite_unknown` too (named
   `user`/`platform`: a platform device enrolls with `--platform`).
4. Open the package with the code (`invite_code_invalid`); it names the org, the founder key, the log head right
   after the `invite` entry, the keys `OK`/`WK` and the hub key.
5. Verify the membership log of the lookup answer (the device is no member yet and cannot read the org log):
   from `seq` 0 against the package's founder (L1-L8), it must contain the package head (`log_rollback`), end at the
   served head (`log_chain`), name the package's org (`log_genesis`) and hold this invitation (`invite` of kind
   `device` with the code's `pk_inv`, not revoked, not accepted: `invite_not_in_log`, `invite_revoked`,
   `invite_consumed`). A log that assigns platform devices (`device_assign` with `platform_seq`) needs the verified
   platform log, which only an active device can read: before the `accept` such a log is anchored without
   signatures (gapless hash chain through the package head to the served head, genesis of the package's founder and
   org, the `invite` entry at or before the package head; the package head pins the hash of that history), and it
   is verified completely right after the enrollment, before anything is stored (step 8). Same rule as the web
   client's `org_verify` (`web/src/core/account/join.ts`).
6. Generate the identity (X25519 + Ed25519) and a UUIDv7 device ID; build `accept{invite_id, subject, kind:"device",
   pub}` at `seq` = served head + 1 with `sig` (device key) and `inv_sig` (`sk_inv`).
7. Write `keys/device.key` (last but one), then `POST /v1/device/enroll` signed with the new key and verified with the
   **hub key of the package** (pinned; TOFU is not used). The answer must be `{state:"active", org_id:<package org>}`.
8. Fetch the platform log (`GET /v1/device/platform-log`, P1-P4 against `AGENT_RUNNER_PLATFORM_ROOT_KEY`) and verify
   the org log with the `accept` appended completely. Store, in one transaction of the runner store (started for the
   command when it does not run, `vm.cli.store`): the verified platform log, the verified org log and its head, and
   the package keys the device is entitled to after its `accept` (`scope_key`, source `package`, sealed with the
   storage key). Then write `device.json` (`kind:"org"`, hub key of the package) last.

Any failure after the key file was written removes both identity files and rolls the store transaction back: no
half identity remains. A failure after the hub accepted the enrollment leaves the device enrolled at the hub only;
an admin removes it (`remove`), the operator enrolls again with a new code. The code is never printed. Output:
`runner.enroll state=active kind=org device_id=<id> org_id=<org> keys=<n>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform as host_platform
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import psycopg

from agent_cli import pg
from agent_runner import __version__
from agent_runner.config import Config
from agent_runner.crypto.encoding import canonical
from agent_runner.crypto.errors import CryptoError, fail
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.invite import InviteSecrets, invite_secrets, open_package, parse_code
from agent_runner.crypto.log.entries import Head, check_extends, check_log_entry, entry_hash, sign_log_entry
from agent_runner.crypto.log.platform import PlatformState, VerifiedPlatformLog, verify_platform_log
from agent_runner.crypto.log.state import DEVICE
from agent_runner.crypto.log.verify import VerifiedLog, verify_log
from agent_runner.crypto.storage import scope_key_context, seal_local, storage_key
from agent_runner.encoding import b64u_decode
from agent_runner.errors import RunnerError
from agent_runner.identity import (
    Device,
    DeviceKeys,
    generate_keys,
    load_identity,
    remove_identity,
    write_device,
    write_keys,
)
from agent_runner.sync.codes import HUB_ERROR_CODES
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError, hub_origin
from agent_runner.vm import cli as store_cli
from agent_runner.vm.capacity import compute, physical_memory_mib

LOOKUP_TARGET = "/v1/invites/lookup"
ENROLL_TARGET = "/v1/device/enroll"
PLATFORM_TARGET = "/v1/device/platform-log"
ACTIVE = "active"
MAX_LOOKUP_BYTES = 64 * 1024 * 1024
TIMEOUT_S = 30.0
PAGE_LIMIT = 1000
MAX_PAGES = 1000
_PLATFORM_TEXT_RE = re.compile(r"[a-z0-9_]{1,32}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class Invitation:
    """What the code and the lookup answer yield, verified (step 5)."""

    secrets: InviteSecrets
    org: str
    founder: str
    package_head: Head
    entries: list[dict[str, object]]
    head: Head
    keys: list[dict[str, Any]]
    hub_key: str
    anchored: bool
    """True when the log needs the platform log and is verified completely only after the enrollment."""


@dataclass(frozen=True)
class Enrolled:
    device: Device
    org: str
    keys: int


@dataclass
class EnrollOptions:
    """Injectable parts (tests): HTTP transport, wall clock, key generation, device ID, host facts, store probe."""

    transport: httpx.AsyncBaseTransport | None = None
    wall: Callable[[], float] = time.time
    generate: Callable[[], DeviceKeys] = generate_keys
    new_id: Callable[[], str] = uuid7
    os_name: str = "darwin"
    arch: str = field(default_factory=lambda: host_platform.machine().lower())
    physical_mib: Callable[[], int] = physical_memory_mib
    store_running: Callable[[Path], bool] = pg.cluster_running


# --- lookup --------------------------------------------------------------------------------------------------------


def _refusal(status: int, body: bytes) -> RunnerError:
    """The closed code of an unsigned error answer (`response_invalid` for anything else)."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        data = None
    code = data.get("error") if isinstance(data, dict) else None
    if status >= 400 and isinstance(code, str) and _CODE_RE.fullmatch(code) and code in HUB_ERROR_CODES:
        return RunnerError(code)
    return RunnerError("response_invalid")


async def lookup(hub_url: str, invite_id: str, transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    """`POST /v1/invites/lookup` without signatures (anonymous); no redirects, no proxy, bounded size."""
    origin = hub_origin(hub_url)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, timeout=TIMEOUT_S, trust_env=False
    ) as client:
        try:
            async with client.stream(
                "POST",
                origin.base_url + LOOKUP_TARGET,
                content=canonical({"invite_id": invite_id}),
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_LOOKUP_BYTES:
                        raise RunnerError("response_invalid")
                status = response.status_code
        except httpx.HTTPError:
            raise RunnerError("hub_unreachable") from None
    if status != 200:
        raise _refusal(status, bytes(body))
    try:
        data = json.loads(bytes(body))
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise RunnerError("response_invalid")
    return cast(dict[str, Any], data)


def _head(value: object) -> Head:
    if (
        not isinstance(value, dict)
        or set(value) != {"seq", "hash"}
        or not isinstance(value["seq"], int)
        or isinstance(value["seq"], bool)
        or not isinstance(value["hash"], str)
    ):
        raise RunnerError("response_invalid")
    return Head(value["seq"], value["hash"])


# --- invitation and log -------------------------------------------------------------------------------------------


def _needs_platform(entries: Sequence[object]) -> bool:
    """A `device_assign` of a platform device (verifiable only with the platform log)."""
    for entry in entries:
        body = entry.get("body") if isinstance(entry, dict) else None
        if isinstance(body, dict) and body.get("type") == "device_assign" and "platform_seq" in body:
            return True
    return False


def check_log(invitation: Invitation, platform: PlatformState | None) -> VerifiedLog:
    """Step 5 with the full verification (L1-L8 against the package's founder)."""
    check_extends(invitation.package_head, invitation.entries)
    log = verify_log(invitation.entries, invitation.founder, platform=platform)
    if log.state.org != invitation.org:
        fail("log_genesis")
    if log.head != invitation.head:
        fail("log_chain")
    found = log.state.invites.get(invitation.secrets.invite_id)
    if found is None or found.pk_inv != invitation.secrets.pk_inv or found.kind != DEVICE:
        fail("invite_not_in_log")
    if found.revoked_seq is not None:
        fail("invite_revoked")
    if found.accepted_seq is not None:
        fail("invite_consumed")
    return log


def anchor_log(invitation: Invitation) -> None:
    """Step 5 without signatures (a log with platform devices before the enrollment): see the module docstring.

    A later `invite_revoke` or `accept` of this invitation is refused structurally (`invite_revoked`,
    `invite_consumed`); the signatures are checked by the full verification after the enrollment."""
    entries = invitation.entries
    check_extends(invitation.package_head, entries)
    found: dict[str, object] | None = None
    seen: set[object] = set()
    previous = ""
    for seq, entry in enumerate(entries):
        body = check_log_entry(entry)
        if body["seq"] != seq or body["prev"] != previous:
            fail("log_chain")
        previous = entry_hash(entry)
        if body["type"] in ("invite", "invite_revoke", "accept") and body["invite_id"] == invitation.secrets.invite_id:
            seen.add(body["type"])
            if body["type"] == "invite" and seq <= invitation.package_head.seq:
                found = body
    genesis = check_log_entry(entries[0])
    pub = genesis.get("pub")
    if genesis["type"] != "genesis" or not isinstance(pub, dict) or pub.get("sig") != invitation.founder:
        fail("log_genesis")
    if genesis["org"] != invitation.org:
        fail("log_genesis")
    if invitation.head != Head(len(entries) - 1, previous):
        fail("log_chain")
    if found is None or found["pk_inv"] != invitation.secrets.pk_inv or found["kind"] != DEVICE:
        fail("invite_not_in_log")
    if "invite_revoke" in seen:
        fail("invite_revoked")
    if "accept" in seen:
        fail("invite_consumed")


def open_invitation(secrets: InviteSecrets, answer: Mapping[str, Any]) -> Invitation:
    """Steps 3-5 on the lookup answer (`invite_unknown`, `invite_code_invalid`, `log_*`, `invite_*`)."""
    if answer.get("scope") != "org" or answer.get("kind") != DEVICE:
        kind = answer.get("scope") if answer.get("scope") == "platform" else answer.get("kind")
        raise RunnerError("invite_unknown", kind if kind in ("platform", "user") else None)
    sealed = answer.get("package_ct")
    entries = answer.get("log")
    if not isinstance(sealed, str) or not isinstance(entries, list) or not entries:
        raise RunnerError("response_invalid")
    head = _head(answer.get("log_head"))
    package = open_package(secrets, sealed)
    if package["org"] != answer.get("org_id"):
        fail("log_genesis")
    package_head = cast(dict[str, Any], package["head"])
    hub_key = cast(dict[str, str], package["hub_key"])
    invitation = Invitation(
        secrets=secrets,
        org=cast(str, package["org"]),
        founder=cast(str, package["founder"]),
        package_head=Head(package_head["seq"], package_head["hash"]),
        entries=cast(list[dict[str, object]], entries),
        head=head,
        keys=cast(list[dict[str, Any]], package["keys"]),
        hub_key=hub_key["public_key"],
        anchored=_needs_platform(entries),
    )
    if invitation.anchored:
        anchor_log(invitation)
    else:
        check_log(invitation, None)
    return invitation


# --- enrollment ---------------------------------------------------------------------------------------------------


def _time_ms(seconds: float) -> str:
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    return f"{whole}.{int(seconds * 1000) % 1000:03d}Z"


def accept_entry(invitation: Invitation, device_id: str, keys: DeviceKeys, ts: str) -> dict[str, object]:
    """The `accept` entry of this device at the served head (step 6)."""
    body = {
        "org": invitation.org,
        "seq": invitation.head.seq + 1,
        "prev": invitation.head.hash,
        "type": "accept",
        "by": device_id,
        "ts": ts,
        "v": 1,
        "invite_id": invitation.secrets.invite_id,
        "subject": device_id,
        "kind": DEVICE,
        "pub": {"kem": keys.kem_public, "sig": keys.sig_public},
    }
    return sign_log_entry(body, keys.signing_key(), invitation.secrets.signing)


def enrollment_body(
    *,
    device_id: str,
    invitation: Invitation,
    keys: DeviceKeys,
    entry: dict[str, object],
    config: Config,
    options: EnrollOptions,
) -> dict[str, object]:
    arch = options.arch if _PLATFORM_TEXT_RE.fullmatch(options.arch) else "unknown"
    capacity = compute(
        physical_mib=options.physical_mib(),
        reserve_mib=config.memory_reserve_mib,
        vm_memory_mib=config.vm_memory_mib,
        vms=[],
    )
    return {
        "device_id": device_id,
        "kind": "org",
        "invite_id": invitation.secrets.invite_id,
        "pub": {"kem": keys.kem_public, "sig": keys.sig_public},
        "agent_version": __version__,
        "os": options.os_name,
        "arch": arch,
        "capacity": capacity.heartbeat(),
        "entry": entry,
    }


async def _platform_log(client: HubClient) -> list[object]:
    """Every platform-log entry, page by page."""
    entries: list[object] = []
    after: int | None = None
    for _ in range(MAX_PAGES):
        position = "" if after is None else f"&after={after}"
        answer = await client.request("GET", f"{PLATFORM_TARGET}?limit={PAGE_LIMIT}{position}")
        page = answer.get("entries")
        if not isinstance(page, list):
            raise RunnerError("response_invalid")
        for item in page:
            if not isinstance(item, dict) or "entry" not in item:
                raise RunnerError("response_invalid")
            entries.append(item["entry"])
        next_after = answer.get("next_after")
        if not isinstance(next_after, int) or isinstance(next_after, bool) or not page:
            break
        if after is not None and next_after <= after:
            break
        after = next_after
    return entries


async def _register(
    config: Config,
    invitation: Invitation,
    *,
    device_id: str,
    keys: DeviceKeys,
    body: dict[str, object],
    options: EnrollOptions,
) -> VerifiedPlatformLog:
    """Step 7 and the platform log of step 8 (signed requests verified with the package's hub key)."""
    assert config.hub_url is not None
    assert config.platform_root_key is not None
    client = HubClient(
        hub_url=config.hub_url,
        key_id="device." + uuid.UUID(device_id).hex,
        signing_key=keys.signing_key(),
        hub_key=b64u_decode(invitation.hub_key),
        transport=options.transport,
        wall=options.wall,
    )
    try:
        answer = await client.request("POST", ENROLL_TARGET, body)
        if answer != {"state": ACTIVE, "org_id": invitation.org}:
            raise RunnerError("response_invalid")
        entries = await _platform_log(client)
    except HubRefusal as refusal:
        raise RunnerError(refusal.code) from None
    except HubTransportError as error:
        raise RunnerError(error.reason) from None
    finally:
        await client.aclose()
    try:
        return verify_platform_log(entries, config.platform_root_key)
    except CryptoError as refused:
        raise RunnerError(refused.code) from None


def entitled_keys(
    log: VerifiedLog, device_id: str, keys: Sequence[Mapping[str, Any]]
) -> list[tuple[str, str, int, bytes]]:
    """The package keys this device may hold after its `accept` (read right, existing epoch), sorted."""
    state = log.state
    chosen = {
        (str(item["scope"]), str(item["scope_id"]), int(item["epoch"])): b64u_decode(str(item["key"]))
        for item in keys
        if state.can_read(device_id, str(item["scope"]), str(item["scope_id"]))
        and 1 <= int(item["epoch"]) <= state.epoch(str(item["scope"]), str(item["scope_id"]))
    }
    return [(*slot, key) for slot, key in sorted(chosen.items())]


def _entry_text(entry: Mapping[str, object]) -> str:
    return canonical(dict(entry)).decode("ascii")


def store_enrollment(
    dsn: str,
    *,
    keys: DeviceKeys,
    platform: VerifiedPlatformLog,
    log: VerifiedLog,
    scope_keys: Sequence[tuple[str, str, int, bytes]],
) -> None:
    """Step 8: the verified copies and the package keys in one transaction (a store with data is refused)."""
    sealing = storage_key(keys.sig)
    try:
        with psycopg.connect(dsn) as conn, conn.transaction():
            row = conn.execute(
                "SELECT (SELECT count(*) FROM runner.log_entry) + (SELECT count(*) FROM runner.platform_log_entry)"
                " + (SELECT count(*) FROM runner.scope_key) + (SELECT count(*) FROM runner.sync_state)"
                " + (SELECT count(*) FROM runner.outbox) + (SELECT count(*) FROM runner.session)"
                " + (SELECT count(*) FROM runner.vm WHERE wg_id IS NOT NULL AND state <> 'deleted')"
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise RunnerError("store_not_empty")
            for state, entry in zip(platform.states, platform.entries, strict=True):
                conn.execute(
                    "INSERT INTO runner.platform_log_entry (seq, entry_text, entry_hash) VALUES (%s, %s, %s)",
                    (state.seq, _entry_text(entry), state.head),
                )
            for org_state, entry in zip(log.states, log.entries, strict=True):
                conn.execute(
                    "INSERT INTO runner.log_entry (org_id, seq, entry_text, entry_hash) VALUES (%s, %s, %s, %s)",
                    (org_state.org, org_state.seq, _entry_text(entry), org_state.head),
                )
            conn.execute(
                "INSERT INTO runner.log_head (org_id, seq, hash) VALUES (%s, %s, %s)",
                (log.state.org, log.state.seq, log.state.head),
            )
            for scope, scope_id, epoch, key in scope_keys:
                blob = seal_local(sealing, "scope_key", scope_key_context(scope, scope_id, epoch), key)
                conn.execute(
                    "INSERT INTO runner.scope_key (scope, scope_id, epoch, key_ct, source)"
                    " VALUES (%s, %s, %s, %s, 'package')",
                    (scope, scope_id, epoch, blob),
                )
    except psycopg.Error:
        raise RunnerError("store_unavailable") from None


def _complete(
    invitation: Invitation, platform: VerifiedPlatformLog, device_id: str, entry: dict[str, object]
) -> VerifiedLog:
    """Step 8: the whole log with the platform log, then with the device's `accept` appended."""
    try:
        check_log(invitation, platform.state)
        return verify_log([*invitation.entries, entry], invitation.founder, platform=platform.state)
    except CryptoError as refused:
        raise RunnerError(refused.code) from None


async def prepare(config: Config, code_text: str, options: EnrollOptions) -> Invitation:
    """Steps 2-5 (nothing is written)."""
    assert config.hub_url is not None
    try:
        secrets = invite_secrets(parse_code(code_text))
    except CryptoError as refused:
        raise RunnerError(refused.code) from None
    answer = await lookup(config.hub_url, secrets.invite_id, options.transport)
    try:
        return open_invitation(secrets, answer)
    except CryptoError as refused:
        raise RunnerError(refused.code) from None


def _check_config(config: Config) -> str:
    if config.hub_url is None or config.platform_root_key is None:
        raise RunnerError("config_invalid", "HUB_URL" if config.hub_url is None else "PLATFORM_ROOT_KEY")
    try:
        hub_origin(config.hub_url)
    except ValueError:
        raise RunnerError("config_invalid", "HUB_URL") from None
    if load_identity(config.home) is not None:
        raise RunnerError("already_enrolled")
    return config.hub_url


@contextlib.contextmanager
def _store(config: Config, host: Any, options: EnrollOptions) -> Iterator[str]:
    with store_cli.store(config, SimpleNamespace(host=host), running=options.store_running) as dsn:
        yield dsn


def run_enroll(config: Config, code_text: str, host: Any, options: EnrollOptions | None = None) -> Enrolled:
    """Enroll this host as an org device at `config.hub_url` (synchronous; see the module docstring)."""
    options = options or EnrollOptions()
    hub_url = _check_config(config)
    invitation = asyncio.run(prepare(config, code_text, options))
    keys = options.generate()
    device_id = options.new_id()
    entry = accept_entry(invitation, device_id, keys, _time_ms(options.wall()))
    body = enrollment_body(
        device_id=device_id, invitation=invitation, keys=keys, entry=entry, config=config, options=options
    )
    device = Device(
        device_id=device_id,
        kind="org",
        hub_url=hub_url,
        hub_key=invitation.hub_key,
        enrolled_at=_time_ms(options.wall()),
    )
    with _store(config, host, options) as dsn:
        try:
            write_keys(config.home, keys)
            platform = asyncio.run(
                _register(config, invitation, device_id=device_id, keys=keys, body=body, options=options)
            )
            log = _complete(invitation, platform, device_id, entry)
            chosen = entitled_keys(log, device_id, invitation.keys)
            store_enrollment(dsn, keys=keys, platform=platform, log=log, scope_keys=chosen)
            write_device(config.home, device)
        except BaseException:
            remove_identity(config.home)
            raise
    return Enrolled(device=device, org=invitation.org, keys=len(chosen))


def default_options() -> EnrollOptions:
    """The options of a real enrollment (tests replace this function to inject a transport)."""
    return EnrollOptions()


def enroll(config: Config, code_text: str, out: Any) -> int:
    """CLI entry (`enroll.cli`): enroll and print one fact."""
    done = run_enroll(config, code_text, out.host, default_options())
    out.fact(
        "enroll",
        {"state": ACTIVE, "kind": "org", "device_id": done.device.device_id, "org_id": done.org, "keys": done.keys},
    )
    return 0
