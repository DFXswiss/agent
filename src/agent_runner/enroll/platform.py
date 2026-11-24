"""Platform device enrollment `agent-runner enroll --platform --hub URL --code CODE` (05-runner.md section 2.1,
04-crypto.md sections 3, 4.8 and 5.9, 03-api.md sections 4.1 and 6.1, ADR-023).

1. Refuse an existing identity (`already_enrolled`; a half identity is `identity_incomplete`, removed with
   `agent-runner uninstall`).
2. Parse the code (`invite_code_invalid` / `invite_code_length`) and derive `invite_id` and `sk_inv`.
3. `GET /v1/config` (anonymous, unsigned): its `platform_root_key` must equal the configured
   `AGENT_RUNNER_PLATFORM_ROOT_KEY` (`platform_root_mismatch`, fatal); its `hub_key` is pinned. Platform devices have
   no invitation package, so this is the only source of the hub key; everything the hub says later is verified
   against it, and the platform log against the root key before the device acts (reconcile step 2).
4. Generate the identity (X25519 + Ed25519) and a UUIDv7 device ID; build the enrollment body
   `{device_id, kind:"platform", invite_id, pub, agent_version, os, arch, capacity}` and sign it: `enroll_sig` by the
   device key and `inv_sig` by `sk_inv`, both over `P || "enroll\\0" || canonical(body without both)`.
5. Write `keys/device.key` (last but one), then `POST /v1/device/enroll` signed with the new device key (proof of
   possession) and verified with the pinned hub key; the answer must be `pending_certification`. Then write
   `device.json` (last). Any failure after the key file was written removes both files: no half identity remains.

`agent-runner run` then polls `GET /v1/device/state` every 30 s until the operator certified the device
(`agent-hub platform certify-pending`).
"""

from __future__ import annotations

import asyncio
import json
import platform as host_platform
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from agent_runner import __version__
from agent_runner.config import Config
from agent_runner.crypto.enroll import sign_enrollment
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.invite import invite_secrets, parse_code
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
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError, hub_origin
from agent_runner.vm.capacity import compute, physical_memory_mib

CONFIG_TARGET = "/v1/config"
ENROLL_TARGET = "/v1/device/enroll"
MAX_CONFIG_BYTES = 1024 * 1024
TIMEOUT_S = 30.0
PENDING = "pending_certification"
_HUB_KEY_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
_PLATFORM_TEXT_RE = re.compile(r"[a-z0-9_]{1,32}")
_ED25519_PUBLIC_BYTES = 32


@dataclass(frozen=True)
class HubConfig:
    """What enrollment takes from `GET /v1/config`."""

    platform_root_key: str
    hub_key_id: str
    hub_key: str


@dataclass
class EnrollOptions:
    """Injectable parts (tests): HTTP transport, wall clock, key generation, device ID, host facts."""

    transport: httpx.AsyncBaseTransport | None = None
    wall: Callable[[], float] = time.time
    generate: Callable[[], DeviceKeys] = generate_keys
    new_id: Callable[[], str] = uuid7
    os_name: str = "darwin"
    arch: str = field(default_factory=lambda: host_platform.machine().lower())
    physical_mib: Callable[[], int] = physical_memory_mib


def _public_key(value: object, code: str) -> str:
    try:
        raw = b64u_decode(value) if isinstance(value, str) else b""
    except ValueError:
        raw = b""
    if len(raw) != _ED25519_PUBLIC_BYTES:
        raise RunnerError(code)
    return str(value)


def parse_hub_config(data: object) -> HubConfig:
    """`platform_root_key` and `hub_key {id, public_key}` of a `/v1/config` body (`response_invalid`)."""
    if not isinstance(data, dict) or not isinstance(data.get("hub_key"), dict):
        raise RunnerError("response_invalid")
    hub_key = data["hub_key"]
    key_id = hub_key.get("id")
    if not isinstance(key_id, str) or not _HUB_KEY_ID_RE.fullmatch(key_id):
        raise RunnerError("response_invalid")
    return HubConfig(
        platform_root_key=_public_key(data.get("platform_root_key"), "response_invalid"),
        hub_key_id=key_id,
        hub_key=_public_key(hub_key.get("public_key"), "response_invalid"),
    )


async def fetch_hub_config(hub_url: str, transport: httpx.AsyncBaseTransport | None) -> HubConfig:
    """`GET /v1/config` without signatures (anonymous); no redirects, no proxy, at most 1 MiB."""
    origin = hub_origin(hub_url)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, timeout=TIMEOUT_S, trust_env=False
    ) as client:
        try:
            async with client.stream(
                "GET", origin.base_url + CONFIG_TARGET, headers={"Accept-Encoding": "identity"}
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_CONFIG_BYTES:
                        raise RunnerError("response_invalid")
                status = response.status_code
        except httpx.HTTPError:
            raise RunnerError("hub_unreachable") from None
    if status != 200:
        raise RunnerError("response_invalid")
    try:
        data = json.loads(bytes(body))
    except (ValueError, RecursionError):
        data = None
    return parse_hub_config(data)


def _time_ms(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(seconds * 1000) % 1000:03d}Z"


def enrollment_body(
    *, device_id: str, invite_id: str, keys: DeviceKeys, config: Config, options: EnrollOptions
) -> dict[str, object]:
    """The unsigned body of `POST /v1/device/enroll` for a platform device."""
    arch = options.arch if _PLATFORM_TEXT_RE.fullmatch(options.arch) else "unknown"
    capacity = compute(
        physical_mib=options.physical_mib(),
        reserve_mib=config.memory_reserve_mib,
        vm_memory_mib=config.vm_memory_mib,
        vms=[],
        max_vms=config.max_vms,
    )
    return {
        "device_id": device_id,
        "kind": "platform",
        "invite_id": invite_id,
        "pub": {"kem": keys.kem_public, "sig": keys.sig_public},
        "agent_version": __version__,
        "os": options.os_name,
        "arch": arch,
        "capacity": capacity.heartbeat(),
    }


async def _post(
    hub_url: str, hub: HubConfig, *, device_id: str, keys: DeviceKeys, body: dict[str, object], options: EnrollOptions
) -> None:
    client = HubClient(
        hub_url=hub_url,
        key_id="device." + uuid.UUID(device_id).hex,
        signing_key=keys.signing_key(),
        hub_key=b64u_decode(hub.hub_key),
        transport=options.transport,
        wall=options.wall,
    )
    try:
        answer = await client.request("POST", ENROLL_TARGET, body)
    except HubRefusal as refusal:
        raise RunnerError(refusal.code) from None
    except HubTransportError as error:
        raise RunnerError(error.reason) from None
    finally:
        await client.aclose()
    if answer != {"state": PENDING}:
        raise RunnerError("response_invalid")


async def enroll_platform(config: Config, code_text: str, options: EnrollOptions | None = None) -> Device:
    """Enroll this host as a platform device at `config.hub_url`; returns the written `device.json` content."""
    options = options or EnrollOptions()
    if config.hub_url is None or config.platform_root_key is None:
        raise RunnerError("config_invalid", "HUB_URL" if config.hub_url is None else "PLATFORM_ROOT_KEY")
    hub_url = config.hub_url
    try:
        hub_origin(hub_url)
    except ValueError:
        raise RunnerError("config_invalid", "HUB_URL") from None
    if load_identity(config.home) is not None:
        raise RunnerError("already_enrolled")
    try:
        secrets = invite_secrets(parse_code(code_text))
    except CryptoError as refused:
        raise RunnerError(refused.code) from None
    hub = await fetch_hub_config(hub_url, options.transport)
    if hub.platform_root_key != config.platform_root_key:
        raise RunnerError("platform_root_mismatch")
    keys = options.generate()
    device_id = options.new_id()
    body = sign_enrollment(
        enrollment_body(device_id=device_id, invite_id=secrets.invite_id, keys=keys, config=config, options=options),
        keys.signing_key(),
        secrets.signing,
    )
    device = Device(
        device_id=device_id,
        kind="platform",
        hub_url=hub_url,
        hub_key=hub.hub_key,
        enrolled_at=_time_ms(options.wall()),
    )
    write_keys(config.home, keys)
    try:
        await _post(hub_url, hub, device_id=device_id, keys=keys, body=body, options=options)
        write_device(config.home, device)
    except BaseException:
        remove_identity(config.home)
        raise
    return device


def run_enroll(config: Config, code_text: str, options: EnrollOptions | None = None) -> Device:
    """Synchronous entry point of the CLI."""
    return asyncio.run(enroll_platform(config, code_text, options))


def default_options() -> EnrollOptions:
    """The options of a real enrollment (tests replace this function to inject a transport)."""
    return EnrollOptions()
