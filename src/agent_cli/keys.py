"""Key identity, client side: key files, the pinned hub key and signed requests.

The wire format is the hub's key protocol: binary values are base64url without
padding, hashes are SHA-256 as 64 lowercase hex characters, times are UTC in
whole seconds (``YYYY-MM-DDTHH:MM:SSZ``), and a signed text is UTF-8 with its
lines joined by one line feed and no trailing line feed.

A private key never appears in an output line or in an error message.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
TIME_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]*")
ERROR_VALUE_PATTERN = re.compile(r"[a-z_]{1,64}")
TIME_WINDOW_SECONDS = 120
MAX_ANSWER_BYTES = 1024 * 1024
NONCE_BYTES = 16
SEED_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64
HUB_METHOD = "GET"
WHOAMI_TARGET = "/api/key/whoami"
RELEASE_PATH = "/api/key/release"
RELEASE_FIELDS = frozenset({"repository", "pull_request", "released", "failed", "hub_key"})
RELEASE_FAILURES = ("task_missing", "task_not_ready", "question_unanswered")
MAX_PULL_REQUEST = 2147483647
# Repository rule of the keys file: owner in the form of a handle, name of
# lowercase letters, digits, '.', '_' and '-'.
OWNER_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}")
REPOSITORY_NAME_PATTERN = re.compile(r"[a-z0-9._-]{1,100}")
PULL_REQUEST_PATTERN = re.compile(r"[1-9][0-9]{0,9}")
REPOSITORY_RULE = (
    "OWNER/NAME in lowercase: OWNER 1 to 39 letters, digits or '-', not starting or ending "
    "with '-' and without '--'; NAME 1 to 100 letters, digits, '.', '_' or '-', not '.' or '..'"
)
PULL_REQUEST_RULE = f"a number from 1 to {MAX_PULL_REQUEST} without sign or leading zero"
HUB_HEADERS = ("Agent-Hub-Key", "Agent-Hub-Time", "Agent-Hub-Signature")
NOT_VERIFIED = "hub answer not verified"
NOT_PINNED = "hub key is not pinned"
UNEXPECTED_FORM = "hub answer has an unexpected form"
HUB_KEY_FILE = "hub-key.json"
KEYS_DIR = "keys"
KEY_SUFFIX = ".key"
ID_RULE = "3 to 63 lowercase letters, digits or '-', starting and ending with a letter or digit"

_DEFAULT_PORTS = {"http": 80, "https": 443}
# Ed25519 public key checks (RFC 8032): the y value must be below the field
# prime, and the y values of the small-order points are refused with either
# sign bit. The order-8 y value is the one listed in common small-order
# blocklists; its negation is the other order-8 y value.
_FIELD_PRIME = 2**255 - 19
_ORDER_EIGHT_Y = 2707385501144840649318225287225658788936804267575313519463743609750303402022
_SMALL_ORDER_Y = frozenset(
    {0, 1, _FIELD_PRIME - 1, _ORDER_EIGHT_Y, _FIELD_PRIME - _ORDER_EIGHT_Y}
)
_Y_MASK = (1 << 255) - 1


class KeyProtocolError(Exception):
    """A refusal of a key command. Its message never holds a private key."""


@dataclass(frozen=True)
class HubPin:
    """The hub key id and public key this device pinned with ``agent key pin-hub``."""

    key_id: str
    public_key: Ed25519PublicKey


@dataclass(frozen=True)
class HubOrigin:
    """Where signed requests go: the base URL to send to and the signed host line."""

    base_url: str
    host: str


# Encodings


def valid_id(value: str) -> bool:
    """True when ``value`` follows the id rule of the keys file."""
    return ID_PATTERN.fullmatch(value) is not None


def _require_id(value: str, what: str, writes: bool = True) -> None:
    if not valid_id(value):
        suffix = "; nothing was written" if writes else ""
        raise KeyProtocolError(f"{what} must be {ID_RULE}{suffix}")


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str, length: int) -> bytes:
    """Decode canonical base64url without padding of exactly ``length`` bytes.

    Raises ValueError otherwise. The message never repeats the value.
    """
    if BASE64URL_PATTERN.fullmatch(text) is None or len(text) % 4 == 1:
        raise ValueError("not base64url without padding")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url_encode(raw) != text:
        raise ValueError("not canonical base64url")
    if len(raw) != length:
        raise ValueError(f"not {length} bytes")
    return raw


def sha256_hex(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize().hex()


def parse_time(text: str) -> datetime:
    """Parse ``YYYY-MM-DDTHH:MM:SSZ`` strictly; ValueError for any other form or an impossible date."""
    if TIME_PATTERN.fullmatch(text) is None:
        raise ValueError("not YYYY-MM-DDTHH:MM:SSZ")
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


def ed25519_public_key(raw: bytes) -> Ed25519PublicKey:
    """Load a raw Ed25519 public key; refuse a non-canonical y and the small-order encodings."""
    if len(raw) != PUBLIC_KEY_BYTES:
        raise ValueError(f"not {PUBLIC_KEY_BYTES} bytes")
    y = int.from_bytes(raw, "little") & _Y_MASK
    if y >= _FIELD_PRIME:
        raise ValueError("not a canonical Ed25519 public key")
    if y in _SMALL_ORDER_Y:
        raise ValueError("a small-order Ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(raw)


def decode_public_key(text: str) -> Ed25519PublicKey:
    """An Ed25519 public key written as base64url without padding."""
    return ed25519_public_key(b64url_decode(text, PUBLIC_KEY_BYTES))


def public_key_text(public_key: Ed25519PublicKey) -> str:
    return b64url_encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


# Signed texts


def request_text(
    key_id: str,
    method: str,
    host: str,
    target: str,
    time_text: str,
    nonce: str,
    body_hash: str,
) -> str:
    """The eight lines a caller signs for one request."""
    return "\n".join(
        ("agent-request-v1", key_id, method, host, target, time_text, nonce, body_hash)
    )


def response_text(
    key_id: str,
    nonce: str,
    time_text: str,
    status: int,
    hub_time: str,
    body_hash: str,
) -> str:
    """The seven lines the hub signs for its answer to one request."""
    return "\n".join(
        ("agent-response-v1", key_id, nonce, time_text, str(status), hub_time, body_hash)
    )


# Files


def _write_new_file(path: Path, data: bytes) -> None:
    """Create ``path`` with mode 0600 and write ``data``; FileExistsError when it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    written = False
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
        written = True
    finally:
        if not written:
            path.unlink()


def _ensure_home(agent_home: Path) -> None:
    """Create ``$AGENT_HOME`` with mode 0700 when it does not exist; an existing home is left as it is."""
    if agent_home.is_dir():
        return
    agent_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent_home, 0o700)


def keys_directory(agent_home: Path) -> Path:
    return agent_home / KEYS_DIR


def _key_path(agent_home: Path, key_id: str) -> Path:
    return keys_directory(agent_home) / f"{key_id}{KEY_SUFFIX}"


def _load_private_key(path: Path, key_id: str) -> Ed25519PrivateKey:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise KeyProtocolError(
            f"no key {key_id} at {path}; run agent key create --id {key_id}"
        ) from None
    except OSError as exc:
        raise KeyProtocolError(f"cannot read key file {path}: {exc.strerror}") from None
    try:
        text = raw.decode("ascii")
        seed = b64url_decode(text.removesuffix("\n"), SEED_BYTES)
    except ValueError:
        raise KeyProtocolError(
            f"key file {path} does not hold a {SEED_BYTES}-byte seed as base64url"
        ) from None
    return Ed25519PrivateKey.from_private_bytes(seed)


def create_key(agent_home: Path, key_id: str) -> str:
    """Create an Ed25519 key in ``$AGENT_HOME/keys/ID.key``; return its public key value.

    The directory gets mode 0700 and the file mode 0600. An existing file is
    never overwritten, and an id outside the rule writes nothing.
    """
    _require_id(key_id, "ID")
    directory = keys_directory(agent_home)
    path = _key_path(agent_home, key_id)
    if path.exists() or path.is_symlink():
        raise KeyProtocolError(f"key {key_id} already exists at {path}; nothing was written")
    if directory.is_symlink():
        raise KeyProtocolError(f"{directory} is a symbolic link; nothing was written")
    _ensure_home(agent_home)
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        _write_new_file(path, (b64url_encode(seed) + "\n").encode("ascii"))
    except FileExistsError:
        raise KeyProtocolError(f"key {key_id} already exists at {path}; nothing was written") from None
    return public_key_text(private.public_key())


def list_keys(agent_home: Path) -> list[tuple[str, str]]:
    """The id and public key value of every key file under ``$AGENT_HOME/keys``, sorted by id."""
    directory = keys_directory(agent_home)
    if not directory.exists():
        return []
    found: list[tuple[str, str]] = []
    for path in sorted(directory.iterdir()):
        if not path.name.endswith(KEY_SUFFIX):
            continue
        key_id = path.name[: -len(KEY_SUFFIX)]
        if not valid_id(key_id):
            raise KeyProtocolError(f"key file {path} has a name outside the id rule")
        found.append((key_id, public_key_text(_load_private_key(path, key_id).public_key())))
    return found


def pin_hub_key(agent_home: Path, hub_id: str, public_key: str, replace: bool) -> None:
    """Store the hub key id and public key in ``$AGENT_HOME/hub-key.json`` (mode 0600).

    Both values are checked before anything is written. A pinned key is replaced
    only when ``replace`` is set.
    """
    _require_id(hub_id, "HUB_ID")
    try:
        decode_public_key(public_key)
    except ValueError as exc:
        raise KeyProtocolError(
            f"--public-key must be an Ed25519 public key as base64url without padding ({exc}); "
            "nothing was written"
        ) from None
    path = agent_home / HUB_KEY_FILE
    payload = (json.dumps({"id": hub_id, "public_key": public_key}, indent=2) + "\n").encode("utf-8")
    _ensure_home(agent_home)
    if not replace:
        try:
            _write_new_file(path, payload)
        except FileExistsError:
            raise KeyProtocolError(
                f"a hub key is already pinned in {path}; use --replace to replace it"
            ) from None
        return
    tmp = path.with_name(f".{HUB_KEY_FILE}.tmp.{os.getpid()}")
    _write_new_file(tmp, payload)
    replaced = False
    try:
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink()


def load_hub_pin(agent_home: Path) -> HubPin:
    """Read ``$AGENT_HOME/hub-key.json``; stop with ``hub key is not pinned`` when it is absent."""
    path = agent_home / HUB_KEY_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyProtocolError(NOT_PINNED) from None
    except (OSError, ValueError) as exc:
        raise KeyProtocolError(f"cannot read {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise KeyProtocolError(f"{path} is not valid JSON") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"id", "public_key"}
        or not isinstance(data["id"], str)
        or not isinstance(data["public_key"], str)
    ):
        raise KeyProtocolError(f"{path} must hold exactly the strings id and public_key")
    if not valid_id(data["id"]):
        raise KeyProtocolError(f"{path} holds a hub key id outside the id rule")
    try:
        public_key = decode_public_key(data["public_key"])
    except ValueError as exc:
        raise KeyProtocolError(f"{path} holds an invalid hub public key ({exc})") from None
    return HubPin(key_id=data["id"], public_key=public_key)


# The hub


def hub_origin(hub_url: str) -> HubOrigin:
    """The base URL and signed host line for the configured hub URL.

    The host is lowercase, an international name in its ASCII form, an IPv6
    address in brackets, followed by ``:`` and the port only when the port is
    not the default of the scheme. A hub URL with user information, a path, a
    query or a fragment is refused: the hub's public URL has none of them.
    """
    if "?" in hub_url or "#" in hub_url:
        raise KeyProtocolError("the configured hub URL must not carry a query or fragment")
    try:
        parts = urlsplit(hub_url)
    except ValueError as exc:
        raise KeyProtocolError(f"the configured hub URL is invalid: {exc}") from None
    scheme = parts.scheme
    if scheme not in _DEFAULT_PORTS:
        raise KeyProtocolError("the configured hub URL must use http or https")
    if "@" in parts.netloc:
        raise KeyProtocolError("the configured hub URL must not carry user information")
    if parts.path not in ("", "/"):
        raise KeyProtocolError("the configured hub URL must not carry a path")
    try:
        url = httpx.URL(f"{scheme}://{parts.netloc}/")
    except httpx.InvalidURL as exc:
        raise KeyProtocolError(f"the configured hub URL is invalid: {exc}") from None
    host = url.raw_host.decode("ascii").lower()
    if host == "":
        raise KeyProtocolError("the configured hub URL has no host")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = url.port
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    return HubOrigin(base_url=f"{scheme}://{host}", host=host)


def _clock_time(clock: Callable[[], str]) -> str:
    value = clock()
    try:
        parse_time(value)
    except ValueError:
        raise KeyProtocolError("the local clock did not give a time as YYYY-MM-DDTHH:MM:SSZ") from None
    return value


def _not_verified(reason: str) -> KeyProtocolError:
    return KeyProtocolError(f"{NOT_VERIFIED} ({reason})")


def _error_value(body: bytes) -> str | None:
    """The ``error`` value of a ``{"error": "..."}`` body when it is a plain refusal word."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"error"}:
        return None
    value = data["error"]
    if not isinstance(value, str) or ERROR_VALUE_PATTERN.fullmatch(value) is None:
        return None
    return value


def _read_answer(response: httpx.Response) -> bytes:
    """The answer bytes, at most ``MAX_ANSWER_BYTES``.

    An answer with a ``Content-Encoding`` is refused before its body is read, so
    the bytes read are the body as sent.
    """
    if "content-encoding" in response.headers:
        raise _not_verified("the answer carries a Content-Encoding")
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_ANSWER_BYTES:
                raise _not_verified("the answer is larger than 1 MiB")
            chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise KeyProtocolError(f"hub request failed: {exc}") from None
    return b"".join(chunks)


def verify_answer(
    pin: HubPin,
    *,
    key_id: str,
    nonce: str,
    request_time: str,
    status: int,
    headers: httpx.Headers,
    body: bytes,
    now: str,
) -> None:
    """Accept an answer only when the pinned hub signed it for this request within the time window."""
    if "content-encoding" in headers:
        raise _not_verified("the answer carries a Content-Encoding")
    present = [name for name in HUB_HEADERS if headers.get_list(name)]
    if not present:
        detail = f"HTTP {status}"
        error = _error_value(body) if status >= 400 else None
        if error is not None:
            detail = f"{detail}, error {error}"
        raise _not_verified(f"unsigned answer; unverified: {detail}")
    values: dict[str, str] = {}
    for name in HUB_HEADERS:
        found = headers.get_list(name)
        if len(found) != 1:
            raise _not_verified(f"{name} is missing or repeated")
        values[name] = found[0]
    if values["Agent-Hub-Key"] != pin.key_id:
        raise _not_verified("Agent-Hub-Key is not the pinned hub key id")
    hub_time = values["Agent-Hub-Time"]
    try:
        hub_instant = parse_time(hub_time)
    except ValueError:
        raise _not_verified("Agent-Hub-Time is not YYYY-MM-DDTHH:MM:SSZ") from None
    try:
        signature = b64url_decode(values["Agent-Hub-Signature"], SIGNATURE_BYTES)
    except ValueError:
        raise _not_verified("Agent-Hub-Signature is not a 64-byte base64url value") from None
    text = response_text(key_id, nonce, request_time, status, hub_time, sha256_hex(body))
    try:
        pin.public_key.verify(signature, text.encode("utf-8"))
    except InvalidSignature:
        raise _not_verified("the signature does not verify with the pinned hub key") from None
    skew = abs((hub_instant - parse_time(now)).total_seconds())
    if skew > TIME_WINDOW_SECONDS:
        raise _not_verified(
            f"Agent-Hub-Time is more than {TIME_WINDOW_SECONDS} seconds from this device's clock"
        )


def _signed_get(
    agent_home: Path,
    key_id: str,
    pin: HubPin,
    hub_url: str,
    client: httpx.Client,
    clock: Callable[[], str],
    target: str,
) -> tuple[int, bytes]:
    """Send a signed ``GET`` of ``target`` to the configured hub; return the status and body of the verified answer.

    ``target`` is the raw request target the signature covers. ``client``
    carries the HTTP transport and ``clock`` gives the device's time as
    ``YYYY-MM-DDTHH:MM:SSZ``. A redirect is never followed, and an answer that
    does not verify raises before its body is returned.
    """
    _require_id(key_id, "ID", writes=False)
    origin = hub_origin(hub_url)
    private = _load_private_key(_key_path(agent_home, key_id), key_id)
    request_time = _clock_time(clock)
    nonce = b64url_encode(secrets.token_bytes(NONCE_BYTES))
    text = request_text(
        key_id,
        HUB_METHOD,
        origin.host,
        target,
        request_time,
        nonce,
        sha256_hex(b""),
    )
    signature = b64url_encode(private.sign(text.encode("utf-8")))
    headers = {
        "Agent-Key": key_id,
        "Agent-Time": request_time,
        "Agent-Nonce": nonce,
        "Agent-Signature": signature,
        "Accept-Encoding": "identity",
    }
    request = client.build_request(HUB_METHOD, origin.base_url + target, headers=headers)
    try:
        response = client.send(request, stream=True, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise KeyProtocolError(f"hub request failed: {exc}") from None
    try:
        status = response.status_code
        answer_headers = response.headers
        body = _read_answer(response)
    finally:
        response.close()
    verify_answer(
        pin,
        key_id=key_id,
        nonce=nonce,
        request_time=request_time,
        status=status,
        headers=answer_headers,
        body=body,
        now=_clock_time(clock),
    )
    return status, body


def _hub_refusal(target: str, status: int, body: bytes) -> KeyProtocolError:
    """The report of a verified answer that is not 200, with its plain ``error`` value when it has one."""
    error = _error_value(body)
    detail = "" if error is None else error
    return KeyProtocolError(f"hub {HUB_METHOD} {target} → HTTP {status}: {detail}")


def whoami(
    agent_home: Path,
    key_id: str,
    pin: HubPin,
    hub_url: str,
    client: httpx.Client,
    clock: Callable[[], str],
) -> dict[str, Any]:
    """Send a signed ``GET /api/key/whoami`` to the configured hub and return the verified answer.

    ``client`` carries the HTTP transport and ``clock`` gives the device's time
    as ``YYYY-MM-DDTHH:MM:SSZ``. A redirect is never followed, and the body of
    an answer that does not verify is never used.
    """
    status, body = _signed_get(agent_home, key_id, pin, hub_url, client, clock, WHOAMI_TARGET)
    if status != 200:
        raise _hub_refusal(WHOAMI_TARGET, status, body)
    try:
        answer = json.loads(body)
    except (ValueError, RecursionError):
        raise KeyProtocolError("hub answer is not JSON") from None
    if not isinstance(answer, dict):
        raise KeyProtocolError("hub answer is not a JSON object")
    return answer


def valid_repository(value: str) -> bool:
    """True when ``value`` is ``owner/name`` by the repository rule of the keys file."""
    owner, slash, name = value.partition("/")
    return (
        slash == "/"
        and OWNER_PATTERN.fullmatch(owner) is not None
        and REPOSITORY_NAME_PATTERN.fullmatch(name) is not None
        and name not in (".", "..")
    )


def check_release_args(repository: str, pull_request: str) -> int:
    """Check ``--repo`` and ``--pr`` by the hub's rules before anything is sent; return the number."""
    if not valid_repository(repository):
        raise KeyProtocolError(f"--repo must be {REPOSITORY_RULE}; nothing was sent")
    if PULL_REQUEST_PATTERN.fullmatch(pull_request) is None or int(pull_request) > MAX_PULL_REQUEST:
        raise KeyProtocolError(f"--pr must be {PULL_REQUEST_RULE}; nothing was sent")
    return int(pull_request)


def release_target(repository: str, number: int) -> str:
    """The signed request target of the release query: ``/`` of the repository as ``%2F``."""
    return f"{RELEASE_PATH}?repository={repository.replace('/', '%2F')}&pull_request={number}"


def _release_answer(body: bytes, repository: str, number: int, pin: HubPin) -> dict[str, Any]:
    """The verified 200 answer when it has exactly the form of the hub's release answer for this query."""
    try:
        answer = json.loads(body)
    except (ValueError, RecursionError):
        raise KeyProtocolError(UNEXPECTED_FORM) from None
    if not isinstance(answer, dict) or set(answer) != RELEASE_FIELDS:
        raise KeyProtocolError(UNEXPECTED_FORM)
    released = answer["released"]
    failed = answer["failed"]
    if (
        answer["repository"] != repository
        or type(answer["pull_request"]) is not int
        or answer["pull_request"] != number
        or not isinstance(released, bool)
        or answer["hub_key"] != pin.key_id
    ):
        raise KeyProtocolError(UNEXPECTED_FORM)
    if released:
        form_ok = failed is None
    else:
        form_ok = isinstance(failed, str) and failed in RELEASE_FAILURES
    if not form_ok:
        raise KeyProtocolError(UNEXPECTED_FORM)
    return answer


def release_query(
    agent_home: Path,
    key_id: str,
    pin: HubPin,
    hub_url: str,
    client: httpx.Client,
    clock: Callable[[], str],
    repository: str,
    pull_request: str,
) -> dict[str, Any]:
    """Ask the configured hub with a signed ``GET /api/key/release`` whether a pull request may leave draft.

    ``repository`` and ``pull_request`` are checked before anything is sent.
    The verified answer is returned only when it has exactly the release
    answer's form for this query and the pinned hub key id; a signed but
    malformed answer is refused and never counts as a release.
    """
    number = check_release_args(repository, pull_request)
    target = release_target(repository, number)
    status, body = _signed_get(agent_home, key_id, pin, hub_url, client, clock, target)
    if status != 200:
        raise _hub_refusal(target, status, body)
    return _release_answer(body, repository, number, pin)
