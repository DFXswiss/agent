"""Fail-closed configuration of the hub process (03-api.md section 2, `AGENT_HUB_*`).

Missing, empty or malformed values stop the start with a `ConfigError` whose message names the variable and the
rule, never the value (pattern of the legacy `agent_core.config`). `AGENT_HUB_OWNER_DATABASE` belongs to
`agent-hub migrate` only; the hub process refuses to start while it is set, so the owner credentials never live in the
long-running process. The public-URL checks are adapted from `agent_core.config.public_url_host` and
`refuse_relying_party_host` (copied: that module names the legacy variables in its messages).
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from agent_core.record.connection import ConnectionString, ConnectionStringError, parse_connection_string
from agent_core.signing import SEED_RE, HubSigner, b64url_decode, ed25519_public_key_valid, valid_key_id
from agent_hub.http.forwarded import HEADERS as FORWARDING_HEADERS
from agent_hub.http.forwarded import X_FORWARDED_FOR, Network

PREFIX = "AGENT_HUB_"
PUBLIC_URL = "AGENT_HUB_PUBLIC_URL"
DATABASE = "AGENT_HUB_DATABASE"
OWNER_DATABASE = "AGENT_HUB_OWNER_DATABASE"
SIGNING_KEY = "AGENT_HUB_SIGNING_KEY"
HUB_KEY_ID = "AGENT_HUB_HUB_KEY_ID"
PLATFORM_ROOT_KEY = "AGENT_HUB_PLATFORM_ROOT_KEY"
HOST = "AGENT_HUB_HOST"
PORT = "AGENT_HUB_PORT"
STATIC_DIR = "AGENT_HUB_STATIC_DIR"
DOWNLOADS_DIR = "AGENT_HUB_DOWNLOADS_DIR"
DEVICE_ONLINE_SECONDS = "AGENT_HUB_DEVICE_ONLINE_SECONDS"
PLATFORM_BUDGET = "AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY"
START_CREDIT = "AGENT_HUB_BILLING_START_CREDIT_USD"
MODEL_CATALOG = "AGENT_HUB_MODEL_CATALOG"
TRUSTED_PROXIES = "AGENT_HUB_TRUSTED_PROXIES"
IPV4_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")
FORWARDED_HEADER = "AGENT_HUB_FORWARDED_HEADER"
CONTEXT_MAX_OBJECT_BYTES = "AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES"
CONTEXT_KEEP = "AGENT_HUB_CONTEXT_KEEP"
CONTEXT_ORG_BYTES = "AGENT_HUB_CONTEXT_ORG_BYTES"
LOG_KEEP = "AGENT_HUB_LOG_KEEP"
LOG_ORG_BYTES = "AGENT_HUB_LOG_ORG_BYTES"
VMS_PER_WG = "AGENT_HUB_VMS_PER_WG"
CAPACITY_REFUSALS = "AGENT_HUB_CAPACITY_REFUSALS"
REFUSAL_HOLD_SECONDS = "AGENT_HUB_REFUSAL_HOLD_SECONDS"

TEST_PREFIX = "AGENT_HUB_TEST_"
KNOWN_VARIABLES = frozenset(
    {
        PUBLIC_URL,
        DATABASE,
        SIGNING_KEY,
        HUB_KEY_ID,
        PLATFORM_ROOT_KEY,
        HOST,
        PORT,
        STATIC_DIR,
        DOWNLOADS_DIR,
        DEVICE_ONLINE_SECONDS,
        PLATFORM_BUDGET,
        START_CREDIT,
        MODEL_CATALOG,
        TRUSTED_PROXIES,
        FORWARDED_HEADER,
        CONTEXT_MAX_OBJECT_BYTES,
        CONTEXT_KEEP,
        CONTEXT_ORG_BYTES,
        LOG_KEEP,
        LOG_ORG_BYTES,
        VMS_PER_WG,
        CAPACITY_REFUSALS,
        REFUSAL_HOLD_SECONDS,
    }
)

DEFAULT_PORT = 8788
DEFAULT_DEVICE_ONLINE_SECONDS = 30
MAX_DEVICE_ONLINE_SECONDS = 3600
DEFAULT_VMS_PER_WG = 100
"""Live VMs of one workgroup over all devices (ADR-075 section 1; 0 = no limit)."""
MAX_VMS_PER_WG = 1_000_000
DEFAULT_CAPACITY_REFUSALS = 3
"""Refusals in a row after which the next attempt is held back (ADR-075 section 7, ADR-049)."""
MAX_CAPACITY_REFUSALS = 1000
DEFAULT_REFUSAL_HOLD_SECONDS = 60
MAX_REFUSAL_HOLD_SECONDS = 86400
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_CATALOG_BYTES = 1024 * 1024
GIB = 1024**3
DEFAULT_CONTEXT_MAX_OBJECT_BYTES = 8 * GIB
DEFAULT_CONTEXT_KEEP = 3
MAX_CONTEXT_KEEP = 1000
DEFAULT_CONTEXT_ORG_BYTES = 200 * GIB
DEFAULT_LOG_KEEP = 500
"""Complete logs kept per task (ADR-079 section 3)."""
MAX_LOG_KEEP = 999_999
DEFAULT_LOG_ORG_BYTES = 100 * GIB
MAX_CONTEXT_BYTES = 2**53 - 1

# Limits of 03-api.md section 12 and section 1 that the hub enforces and `/v1/config` publishes.
LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "request_body_bytes": 1024 * 1024,
        "event_batch_bytes": 4 * 1024 * 1024,
        "events_per_request": 100,
        "event_ct_bytes": 256 * 1024,
        "terminal_ct_bytes": 64 * 1024,
        "whiteboard_text_bytes": 64 * 1024,
        "session_certificate_seconds": 43080,
        "invite_user_seconds": 72 * 3600,
        "invite_device_seconds": 24 * 3600,
        "invite_max_seconds": 30 * 24 * 3600,
        "device_request_seconds": 15 * 60,
        "subagent_max_depth": 2,
        "subagent_max_running": 3,
        "subagent_max_total": 10,
    }
)

_HUB_KEY_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0x.*")
_PORT_RE = re.compile(r"[0-9]{1,5}")
_SECONDS_RE = re.compile(r"[0-9]{1,6}")
_BYTES_RE = re.compile(r"[0-9]{1,16}")
_BUDGET_RE = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_LABEL_TEXT_RE = re.compile(r"[ -~]{1,64}")
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_HOST_NAME = 253
ACCESS_KINDS = ("platform", "api_key", "subscription")
MAX_LIST = 64
MAX_TOKENS = 100_000_000
PRICE_KEYS = frozenset({"input", "output", "cache_read", "cache_write"})


class ConfigError(Exception):
    """The configuration is incomplete or malformed; the message names the variable and the rule, never the value."""


class CatalogInvalid(ValueError):
    """The model catalog breaks `contracts/model-catalog.json` or its rules; the message names the place only."""


@dataclass(frozen=True)
class PublicOrigin:
    """The parts of `AGENT_HUB_PUBLIC_URL`.

    `origin` is `scheme://host[:port]` without a default port (WebAuthn origin, `/v1/config.origin`), `rp_id` the host
    name (WebAuthn relying-party ID) and `host` the host line of every signed request (`agent-request-v1`).
    """

    origin: str
    rp_id: str
    host: str


@dataclass(frozen=True)
class ModelCatalog:
    """The model allow-lists per access kind and the platform default (source of `/v1/config.models`)."""

    platform_default: str
    allowed: Mapping[str, tuple[str, ...]]
    document: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class HubConfig:
    """The validated configuration of one hub process."""

    public: PublicOrigin
    database: ConnectionString = field(repr=False)
    signing_key: str = field(repr=False)
    hub_key_id: str
    platform_root_key: bytes
    bind_host: str
    bind_port: int
    static_dir: Path
    device_online_seconds: int
    platform_budget_usd_per_org_day: Decimal | None
    model_catalog: ModelCatalog
    billing_start_credit_usd: Decimal = Decimal(0)
    """`AGENT_HUB_BILLING_START_CREDIT_USD`: the balance of a new credit account (KOR-31, ADR-062; default 0)."""
    trusted_proxies: tuple[Network, ...] = ()
    forwarded_header: str = X_FORWARDED_FOR
    context_max_object_bytes: int = DEFAULT_CONTEXT_MAX_OBJECT_BYTES
    """`AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES`: the sealed bytes of one context checkpoint (ADR-072 section 6)."""
    context_keep: int = DEFAULT_CONTEXT_KEEP
    """`AGENT_HUB_CONTEXT_KEEP`: complete checkpoints kept per task; older ones are deleted on a commit."""
    context_org_bytes: int = DEFAULT_CONTEXT_ORG_BYTES
    """`AGENT_HUB_CONTEXT_ORG_BYTES`: the sealed checkpoint bytes of all tasks of one organisation."""
    log_keep: int = DEFAULT_LOG_KEEP
    """`AGENT_HUB_LOG_KEEP`: complete logs kept per task; older ones are deleted on a commit (ADR-079 section 3)."""
    log_org_bytes: int = DEFAULT_LOG_ORG_BYTES
    """`AGENT_HUB_LOG_ORG_BYTES`: the sealed log bytes of all tasks of one organisation (apart from the checkpoints)."""
    downloads_dir: Path | None = None
    """`AGENT_HUB_DOWNLOADS_DIR`: the download area of an own device (ADR-074 section 7); None = no downloads."""
    vms_per_wg: int = DEFAULT_VMS_PER_WG
    capacity_refusals: int = DEFAULT_CAPACITY_REFUSALS
    refusal_hold_seconds: int = DEFAULT_REFUSAL_HOLD_SECONDS

    def signer(self) -> HubSigner:
        """The hub's response and checkpoint signer (reused `agent_core.signing.HubSigner`)."""
        return HubSigner.from_seed_hex(self.hub_key_id, self.signing_key)


def _value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    return None if value is None or value == "" else value


def _require(environ: Mapping[str, str], name: str) -> str:
    value = _value(environ, name)
    if value is None:
        raise ConfigError(f"{name} is not set")
    return value


def parse_public_url(url: str, name: str = PUBLIC_URL) -> PublicOrigin:
    """Check the public URL: http(s), a DNS host name (it is the relying-party ID), no user information, path, query
    or fragment; one trailing `/` is allowed."""
    if "?" in url or "#" in url:
        raise ConfigError(f"{name} must not have a query or a fragment")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ConfigError(f"{name} is not a valid URL") from None
    if parts.scheme not in DEFAULT_PORTS:
        raise ConfigError(f"{name} must be an http or https URL")
    if "@" in parts.netloc:
        raise ConfigError(f"{name} must not have user information")
    if parts.path not in ("", "/"):
        raise ConfigError(f"{name} must not have a path")
    hostname = parts.hostname or ""
    if (
        len(hostname) > MAX_HOST_NAME
        or _HOST_NAME_RE.fullmatch(hostname) is None
        or _is_ip_address(hostname)
        or _NUMERIC_LABEL_RE.fullmatch(hostname.rsplit(".", 1)[-1]) is not None
    ):
        raise ConfigError(f"{name} must have a lower-case DNS host name, not an IP address")
    host = hostname if port is None or port == DEFAULT_PORTS[parts.scheme] else f"{hostname}:{port}"
    return PublicOrigin(origin=f"{parts.scheme}://{host}", rp_id=hostname, host=host)


def _is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _bind_host(value: str) -> str:
    if _is_ip_address(value) or (len(value) <= MAX_HOST_NAME and _HOST_NAME_RE.fullmatch(value) is not None):
        return value
    raise ConfigError(f"{HOST} must be an IP address or a lower-case host name")


def _bounded_int(environ: Mapping[str, str], name: str, default: int, pattern: re.Pattern[str], maximum: int) -> int:
    value = _value(environ, name)
    if value is None:
        return default
    if pattern.fullmatch(value) is None or not 1 <= int(value) <= maximum:
        raise ConfigError(f"{name} must be an integer from 1 to {maximum}")
    return int(value)


def _setting(environ: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    """A count setting of 0 to `maximum` (0 = no limit or no hold, ADR-075 section 1)."""
    value = _value(environ, name)
    if value is None:
        return default
    if _SECONDS_RE.fullmatch(value) is None or int(value) > maximum:
        raise ConfigError(f"{name} must be an integer from 0 to {maximum}")
    return int(value)


def _budget(environ: Mapping[str, str]) -> Decimal | None:
    value = _value(environ, PLATFORM_BUDGET)
    if value is None:
        return None
    if _BUDGET_RE.fullmatch(value) is None:
        raise ConfigError(f"{PLATFORM_BUDGET} must be empty or a non-negative amount with at most 6 decimals")
    return Decimal(value)


def _start_credit(environ: Mapping[str, str]) -> Decimal:
    value = _value(environ, START_CREDIT)
    if value is None:
        return Decimal(0)
    if _BUDGET_RE.fullmatch(value) is None:
        raise ConfigError(f"{START_CREDIT} must be empty or a non-negative amount with at most 6 decimals")
    return Decimal(value)


def _trusted_proxies(environ: Mapping[str, str]) -> tuple[Network, ...]:
    """The reverse proxies whose forwarding header names the client (`agent_hub.http.forwarded`); empty = none."""
    value = _value(environ, TRUSTED_PROXIES)
    if value is None:
        return ()
    try:
        networks = tuple(ipaddress.ip_network(item.strip()) for item in value.split(","))
    except ValueError:
        raise ConfigError(
            f"{TRUSTED_PROXIES} must be a comma-separated list of IP addresses or networks (address/prefix length)"
        ) from None
    if any(network.prefixlen == 0 for network in networks):
        raise ConfigError(f"{TRUSTED_PROXIES} must not trust every address (prefix length 0)")
    if any(network.subnet_of(IPV4_MAPPED) for network in networks if network.version == 6):
        # forwarding entries and peers in that range are compared as IPv4, so such a network would never match
        raise ConfigError(f"{TRUSTED_PROXIES} must name IPv4-mapped addresses (::ffff:a.b.c.d) as IPv4")
    return networks


def _forwarded_header(environ: Mapping[str, str], trusted: tuple[Network, ...]) -> str:
    value = _value(environ, FORWARDED_HEADER)
    if value is None:
        return X_FORWARDED_FOR
    if not trusted:
        raise ConfigError(f"{FORWARDED_HEADER} needs {TRUSTED_PROXIES}")
    if value.lower() not in FORWARDING_HEADERS:
        raise ConfigError(f"{FORWARDED_HEADER} must be {' or '.join(FORWARDING_HEADERS)}")
    return value.lower()


def _static_dir(environ: Mapping[str, str]) -> Path:
    value = _value(environ, STATIC_DIR)
    path = DEFAULT_STATIC_DIR if value is None else Path(value)
    if not path.is_dir():
        raise ConfigError(f"{STATIC_DIR} is not a directory")
    return path.resolve()


def _downloads_dir(environ: Mapping[str, str]) -> Path | None:
    value = _value(environ, DOWNLOADS_DIR)
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ConfigError(f"{DOWNLOADS_DIR} must be the absolute path of an existing directory")
    return path.resolve()


def _object_without_repeated_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CatalogInvalid("the catalog repeats a key")
        out[key] = value
    return out


def _refuse_constant(_name: str) -> Any:
    raise CatalogInvalid("the catalog contains NaN or Infinity")


def _object(value: object, where: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CatalogInvalid(f"{where} must be an object with exactly the keys {', '.join(sorted(keys))}")
    return value


def _model_id(value: object, where: str) -> str:
    if not isinstance(value, str) or _MODEL_ID_RE.fullmatch(value) is None:
        raise CatalogInvalid(f"{where} must be a model ID")
    return value


def _string_list(value: object, where: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise CatalogInvalid(f"{where} must be a list of at most {MAX_LIST} entries")
    for item in value:
        if not isinstance(item, str) or pattern.fullmatch(item) is None:
            raise CatalogInvalid(f"{where} has an entry of the wrong form")
    return tuple(value)


def _token_count(value: object, where: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_TOKENS:
        raise CatalogInvalid(f"{where} must be an integer from 1 to {MAX_TOKENS}")


def _price(value: object, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise CatalogInvalid(f"{where} must be a number of at least 0")


def parse_model_catalog(document: object) -> ModelCatalog:
    """Validate a decoded catalog against `contracts/model-catalog.json` and the catalog rules (every default and
    allowed model is listed under `models`; the platform default is allowed for platform access)."""
    top = _object(document, "the catalog", frozenset({"version", "access", "models"}))
    if type(top["version"]) is not int or top["version"] != 1:
        raise CatalogInvalid("version must be the integer 1")
    access = _object(top["access"], "access", frozenset(ACCESS_KINDS))
    platform = _object(access["platform"], "access.platform", frozenset({"default", "allowed", "providers_allowed"}))
    platform_default = _model_id(platform["default"], "access.platform.default")
    _string_list(platform["providers_allowed"], "access.platform.providers_allowed", _LABEL_TEXT_RE)
    allowed = {"platform": _string_list(platform["allowed"], "access.platform.allowed", _MODEL_ID_RE)}
    defaults = [platform_default]
    for kind in ACCESS_KINDS[1:]:
        entry = _object(access[kind], f"access.{kind}", frozenset({"default", "allowed"}))
        if entry["default"] is not None:
            defaults.append(_model_id(entry["default"], f"access.{kind}.default"))
        allowed[kind] = _string_list(entry["allowed"], f"access.{kind}.allowed", _MODEL_ID_RE)
    models = top["models"]
    if not isinstance(models, dict):
        raise CatalogInvalid("models must be an object")
    for spec in models.values():
        # Keys are not checked beyond the schema (it has no `propertyNames`): a key that is no model ID can never be
        # named by a default or an allowed list.
        entry = _object(spec, "a model entry", frozenset({"context_window", "max_output", "price_per_mtok"}))
        _token_count(entry["context_window"], "context_window")
        _token_count(entry["max_output"], "max_output")
        prices = _object(entry["price_per_mtok"], "price_per_mtok", PRICE_KEYS)
        for key in sorted(PRICE_KEYS):
            _price(prices[key], f"price_per_mtok.{key}")
    listed = set(defaults).union(*allowed.values())
    if not listed <= set(models):
        raise CatalogInvalid("every default and allowed model must be listed under models")
    if platform_default not in allowed["platform"]:
        raise CatalogInvalid("the platform default must be an allowed platform model")
    return ModelCatalog(
        platform_default=platform_default,
        allowed=MappingProxyType(allowed),
        document=MappingProxyType(top),
    )


def load_model_catalog(path: Path) -> ModelCatalog:
    """Read and validate the catalog file (strict JSON: no repeated keys, no NaN or Infinity, at most 1 MiB)."""
    if not path.is_file():
        raise ConfigError(f"{MODEL_CATALOG} is not a file")
    raw = path.read_bytes()
    if len(raw) > MAX_CATALOG_BYTES:
        raise ConfigError(f"{MODEL_CATALOG} is larger than 1 MiB")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_repeated_key,
            parse_constant=_refuse_constant,
        )
        return parse_model_catalog(document)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError(f"{MODEL_CATALOG} is not a UTF-8 JSON file") from None
    except CatalogInvalid as error:
        raise ConfigError(f"{MODEL_CATALOG} is not a valid model catalog: {error}") from None


def load_config(environ: Mapping[str, str]) -> HubConfig:
    """The configuration of the hub process from the environment; raises `ConfigError` on the first problem.

    An unknown `AGENT_HUB_*` variable is refused (a misspelt optional setting must not silently fall back to its
    default); `AGENT_HUB_TEST_*` belongs to the test suites and is ignored."""
    if OWNER_DATABASE in environ:
        raise ConfigError(f"{OWNER_DATABASE} must not be set in the hub process (it belongs to agent-hub migrate)")
    unknown = sorted(
        name
        for name in environ
        if name.startswith(PREFIX) and name not in KNOWN_VARIABLES and not name.startswith(TEST_PREFIX)
    )
    if unknown:
        raise ConfigError(f"{unknown[0]} is not a hub setting")
    public = parse_public_url(_require(environ, PUBLIC_URL))
    try:
        database = parse_connection_string(_require(environ, DATABASE))
    except ConnectionStringError:
        raise ConfigError(
            f"{DATABASE} must have the form postgresql://<role>:<password>@<host>[:<port>]/<database>?sslmode=<mode>"
        ) from None
    signing_key = _require(environ, SIGNING_KEY)
    if SEED_RE.fullmatch(signing_key) is None:
        raise ConfigError(f"{SIGNING_KEY} must be 64 lower-case hex characters (an Ed25519 seed)")
    hub_key_id = _require(environ, HUB_KEY_ID)
    if _HUB_KEY_ID_RE.fullmatch(hub_key_id) is None or not valid_key_id(hub_key_id):
        raise ConfigError(
            f"{HUB_KEY_ID} must be 3 to 63 of a-z, 0-9 and '-', starting and ending with a letter or digit"
        )
    root = b64url_decode(_require(environ, PLATFORM_ROOT_KEY))
    if root is None or not ed25519_public_key_valid(root):
        raise ConfigError(f"{PLATFORM_ROOT_KEY} must be the b64u text of a valid Ed25519 public key")
    trusted = _trusted_proxies(environ)
    return HubConfig(
        public=public,
        database=database,
        signing_key=signing_key,
        hub_key_id=hub_key_id,
        platform_root_key=root,
        bind_host=_bind_host(_require(environ, HOST)),
        bind_port=_bounded_int(environ, PORT, DEFAULT_PORT, _PORT_RE, 65535),
        static_dir=_static_dir(environ),
        device_online_seconds=_bounded_int(
            environ, DEVICE_ONLINE_SECONDS, DEFAULT_DEVICE_ONLINE_SECONDS, _SECONDS_RE, MAX_DEVICE_ONLINE_SECONDS
        ),
        platform_budget_usd_per_org_day=_budget(environ),
        model_catalog=load_model_catalog(Path(_require(environ, MODEL_CATALOG))),
        billing_start_credit_usd=_start_credit(environ),
        trusted_proxies=trusted,
        forwarded_header=_forwarded_header(environ, trusted),
        context_max_object_bytes=_bounded_int(
            environ, CONTEXT_MAX_OBJECT_BYTES, DEFAULT_CONTEXT_MAX_OBJECT_BYTES, _BYTES_RE, MAX_CONTEXT_BYTES
        ),
        context_keep=_bounded_int(environ, CONTEXT_KEEP, DEFAULT_CONTEXT_KEEP, _SECONDS_RE, MAX_CONTEXT_KEEP),
        context_org_bytes=_bounded_int(
            environ, CONTEXT_ORG_BYTES, DEFAULT_CONTEXT_ORG_BYTES, _BYTES_RE, MAX_CONTEXT_BYTES
        ),
        log_keep=_bounded_int(environ, LOG_KEEP, DEFAULT_LOG_KEEP, _SECONDS_RE, MAX_LOG_KEEP),
        log_org_bytes=_bounded_int(environ, LOG_ORG_BYTES, DEFAULT_LOG_ORG_BYTES, _BYTES_RE, MAX_CONTEXT_BYTES),
        downloads_dir=_downloads_dir(environ),
        vms_per_wg=_setting(environ, VMS_PER_WG, DEFAULT_VMS_PER_WG, MAX_VMS_PER_WG),
        capacity_refusals=_bounded_int(
            environ, CAPACITY_REFUSALS, DEFAULT_CAPACITY_REFUSALS, _SECONDS_RE, MAX_CAPACITY_REFUSALS
        ),
        refusal_hold_seconds=_setting(
            environ, REFUSAL_HOLD_SECONDS, DEFAULT_REFUSAL_HOLD_SECONDS, MAX_REFUSAL_HOLD_SECONDS
        ),
    )
