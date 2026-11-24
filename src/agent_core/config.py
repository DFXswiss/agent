"""Fail-closed configuration. Missing or empty values abort."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .signing import SEED_RE, valid_key_id

DEFAULT_PORTS = {"http": 80, "https": 443}
# PASSKEY-PROTOCOL.md section 3: the last label of a host name that a browser reads as an IPv4
# address, such as `127.1` or `0x7f000001`.
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0x.*")


class ConfigError(SystemExit):
    pass


def require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"{name} is not set")
    return value


def public_url_host(url: str) -> str:
    """The host line of a signed request (KEY-PROTOCOL.md section 5) for the hub's public URL.

    Refuses a URL with user information, a path, a query or a fragment; a single trailing `/`
    is not a path.
    """
    if "?" in url:
        raise ConfigError("AGENT_CORE_PUBLIC_URL must not have a query")
    if "#" in url:
        raise ConfigError("AGENT_CORE_PUBLIC_URL must not have a fragment")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ConfigError("AGENT_CORE_PUBLIC_URL is not a valid URL") from exc
    if parts.scheme not in DEFAULT_PORTS:
        raise ConfigError("AGENT_CORE_PUBLIC_URL must be an http or https URL")
    if "@" in parts.netloc:
        raise ConfigError("AGENT_CORE_PUBLIC_URL must not have user information")
    if parts.path not in ("", "/"):
        raise ConfigError("AGENT_CORE_PUBLIC_URL must not have a path")
    hostname = parts.hostname
    if hostname is None or hostname == "":
        raise ConfigError("AGENT_CORE_PUBLIC_URL has no host")
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        if not hostname.isascii():
            raise ConfigError(
                "AGENT_CORE_PUBLIC_URL must carry an international host name in its ASCII form"
            )
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ConfigError("AGENT_CORE_PUBLIC_URL has an invalid host name") from exc
    if port is not None and port != DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{port}"
    return host


def _is_ip_address(host_name: str) -> bool:
    try:
        ipaddress.ip_address(host_name)
    except ValueError:
        return False
    return True


def refuse_relying_party_host(public_url: str) -> None:
    """PASSKEY-PROTOCOL.md section 3: the host name of the public URL is the relying-party id, so
    it neither ends with a dot nor is an IP address. Tests the host name without port, brackets
    and case (`urlsplit(...).hostname`), never the host line; runs after `public_url_host`."""
    host_name = str(urlsplit(public_url).hostname)
    if host_name.endswith("."):
        raise ConfigError("AGENT_CORE_PUBLIC_URL must not have a host name ending in a dot")
    if (
        ":" in host_name
        or _is_ip_address(host_name)
        or _NUMERIC_LABEL_RE.fullmatch(host_name.rsplit(".", 1)[-1]) is not None
    ):
        raise ConfigError("AGENT_CORE_PUBLIC_URL must have a host name, not an IP address")


@dataclass(frozen=True)
class Config:
    public_url: str
    session_secret: str
    github_client_id: str
    github_client_secret: str
    github_authorize_url: str
    github_token_url: str
    github_user_url: str
    database: str
    teams_path: Path
    host: str
    port: int
    keys_path: Path
    signing_key: str = field(repr=False)
    hub_key_id: str

    @staticmethod
    def from_env() -> "Config":
        secret = require("AGENT_CORE_SESSION_SECRET")
        if len(secret) < 32:
            raise ConfigError("AGENT_CORE_SESSION_SECRET must be at least 32 characters")
        port_raw = require("AGENT_CORE_PORT")
        if not port_raw.isdigit():
            raise ConfigError("AGENT_CORE_PORT must be an integer")
        teams = Path(require("AGENT_CORE_TEAMS"))
        if not teams.is_file():
            raise ConfigError(f"AGENT_CORE_TEAMS is not a file: {teams}")
        keys = Path(require("AGENT_CORE_KEYS"))
        if not keys.is_file():
            raise ConfigError(f"AGENT_CORE_KEYS is not a file: {keys}")
        signing_key = require("AGENT_CORE_SIGNING_KEY")
        if SEED_RE.fullmatch(signing_key) is None:
            raise ConfigError("AGENT_CORE_SIGNING_KEY must be 64 lowercase hex characters")
        hub_key_id = require("AGENT_CORE_HUB_KEY_ID")
        if not valid_key_id(hub_key_id):
            raise ConfigError(
                "AGENT_CORE_HUB_KEY_ID must be 3 to 63 lowercase letters, digits and '-', "
                "starting and ending with a letter or digit"
            )
        public_url = require("AGENT_CORE_PUBLIC_URL")
        public_url_host(public_url)
        refuse_relying_party_host(public_url)
        if public_url.endswith("/"):
            public_url = public_url[:-1]
        return Config(
            public_url=public_url,
            session_secret=secret,
            github_client_id=require("AGENT_CORE_GITHUB_CLIENT_ID"),
            github_client_secret=require("AGENT_CORE_GITHUB_CLIENT_SECRET"),
            github_authorize_url=require("AGENT_CORE_GITHUB_AUTHORIZE_URL"),
            github_token_url=require("AGENT_CORE_GITHUB_TOKEN_URL"),
            github_user_url=require("AGENT_CORE_GITHUB_USER_URL"),
            database=require("AGENT_CORE_DATABASE"),
            teams_path=teams,
            host=require("AGENT_CORE_HOST"),
            port=int(port_raw),
            keys_path=keys,
            signing_key=signing_key,
            hub_key_id=hub_key_id,
        )
