"""Fail-closed configuration. Missing or empty values abort."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(SystemExit):
    pass


def require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"{name} is not set")
    return value


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
    security_tracker_url: str = ""
    security_tracker_token: str = ""
    security_tracker_link: str = ""

    @property
    def security_tracker_enabled(self) -> bool:
        return self.security_tracker_url != ""

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
        tracker = _security_tracker_from_env()
        return Config(
            public_url=require("AGENT_CORE_PUBLIC_URL").rstrip("/"),
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
            security_tracker_url=tracker[0],
            security_tracker_token=tracker[1],
            security_tracker_link=tracker[2],
        )


SECURITY_TRACKER_KEYS = (
    "AGENT_CORE_SECURITY_TRACKER_URL",
    "AGENT_CORE_SECURITY_TRACKER_TOKEN",
    "AGENT_CORE_SECURITY_TRACKER_LINK",
)


def _security_tracker_from_env() -> tuple[str, str, str]:
    """All three keys or none. A partial set aborts instead of silently disabling the counter."""
    values = [os.environ.get(k) or "" for k in SECURITY_TRACKER_KEYS]
    if all(v == "" for v in values):
        return ("", "", "")
    missing = [k for k, v in zip(SECURITY_TRACKER_KEYS, values) if v == ""]
    if missing:
        raise ConfigError(", ".join(missing) + " is not set; set all security tracker keys or none")
    url, token, link = values
    for key, value in (("AGENT_CORE_SECURITY_TRACKER_URL", url), ("AGENT_CORE_SECURITY_TRACKER_LINK", link)):
        if not value.startswith("https://"):
            raise ConfigError(f"{key} must start with https://")
    return (url.rstrip("/"), token, link)
