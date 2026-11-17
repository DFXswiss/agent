"""Placeholder drivers for tests.

``driver-a``: spec as a file argument, event stream on stdout, an interactive
section and a period usage adapter. ``driver-b``: spec on stdin, final message
in an output file, may run commands. ``driver-c``: spec on stdin, event stream,
extra read directories and a judgment template. ``driver-d``: like ``driver-c``
plus a key file, a fixed child environment and a spend usage adapter.
``driver-e``: record-only (no lane, no account). ``driver-w`` and ``driver-r``
(added by the tests that need them): a windows and an rpc usage adapter.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from agent_cli.drivers import (
    DriverSpec,
    PaneMarkers,
    judgment_argv,
    lane_argv,
    lane_scrub_env,
    parse_driver_document,
    parse_recorded_slots,
)
from agent_cli.model_slots import install_recorded_names

_C_ARGV_HEAD = ["tool-c", "--print", "--model", "{model}"]

DRIVER_DOCUMENT: dict[str, Any] = {
    "drivers": {
        "driver-a": {
            "display_name": "Vendor A",
            "home_env": "TOOL_A_HOME",
            "credential_env": ["TOOL_A_API_KEY"],
            "lane": {
                "spec": "file-arg",
                "output": "event-stream",
                "may_run_commands": False,
                "audit_stderr_when_empty": False,
                "argv": {
                    "read": [
                        "tool-a", "--prompt-file", "{spec_file}", "-m", "{model}",
                        "--read-only", "--cwd", "{cwd}",
                    ],
                    "write": [
                        "tool-a", "--prompt-file", "{spec_file}", "-m", "{model}",
                        "--edit", "--cwd", "{cwd}",
                    ],
                    "judgment": [
                        "tool-a", "--prompt-file", "{spec_file}", "-m", "{model}",
                        "--no-tools", "--cwd", "{cwd}",
                    ],
                },
                "write_hardening": [["--no-shell"], ["--deny", "Shell"]],
            },
            "interactive": {
                "argv_new": [
                    "tool-a", "--session-id", "{session_id}", "--model", "{model}",
                ],
                "argv_resume": [
                    "tool-a", "--resume", "{session_id}", "--model", "{model}",
                ],
                "session_id": "uuid",
                "read_only_args": ["--deny", "Edit"],
                "hardening_args": ["--deny", "Shell"],
                "session_env": "TOOL_A_SESSION_ID",
                "pane": {
                    "working": ["(?i)working\\.\\.\\.", "\\[stop\\]"],
                    "queued": [["queued", "send now"]],
                    "idle": ["> $"],
                    "permission": ["(?i)approve\\?"],
                    "permission_confirm": ["Tab:next"],
                    "chrome": ["Status bar"],
                },
            },
            "usage": {
                "snapshot": "period",
                "credential": {
                    "file": "auth.json",
                    "format": "json-entry",
                    "entry_prefix": "https://auth.example.test::",
                    "token": "key",
                    "expires_at": "expires_at",
                    "account": "email",
                },
                "proxy_from_environment": False,
                "requests": [
                    {
                        "name": "billing",
                        "url": "https://usage.example.test/v1/billing",
                        "headers": {"x-client": "agent-tests"},
                    },
                    {"name": "plan", "url": "https://usage.example.test/v1/plan"},
                ],
                "fields": {
                    "tier": "/plan/tier",
                    "used_percent": "/billing/used",
                    "period_type": "/billing/period/type",
                    "period_start": "/billing/period/start",
                    "period_end": "/billing/period/end",
                    "products": {
                        "list": "/billing/products",
                        "product": "/name",
                        "used_percent": "/used",
                    },
                    "prepaid_val": "/billing/prepaid/val",
                    "on_demand_cap_val": "/billing/cap/val",
                    "on_demand_used_val": "/billing/on_demand/val",
                },
            },
        },
        "driver-b": {
            "display_name": "Vendor B",
            "home_env": "TOOL_B_HOME",
            "credential_env": ["TOOL_B_API_KEY"],
            "lane": {
                "spec": "stdin",
                "output": "last-message-file",
                "may_run_commands": True,
                "audit_stderr_when_empty": False,
                "argv": {
                    "read": [
                        "tool-b", "exec", "--model", "{model}", "--sandbox",
                        "read-only", "--cd", "{cwd}", "--last-message",
                        "{output_file}", "-",
                    ],
                    "write": [
                        "tool-b", "exec", "--model", "{model}", "--sandbox",
                        "workspace-write", "--cd", "{cwd}", "--last-message",
                        "{output_file}", "-",
                    ],
                },
            },
        },
        "driver-c": {
            "display_name": "Vendor C",
            "home_env": "TOOL_C_CONFIG",
            "home_default_dir": "~/.tool-c",
            "credential_env": ["TOOL_C_API_KEY", "TOOL_C_TOKEN"],
            "lane": {
                "spec": "stdin",
                "output": "event-stream",
                "may_run_commands": False,
                "audit_stderr_when_empty": True,
                "argv": {
                    "read": [*_C_ARGV_HEAD, "--tools", "Read", "--mode", "default"],
                    "write": [*_C_ARGV_HEAD, "--tools", "Read,Edit", "--mode", "edit"],
                    "judgment": [*_C_ARGV_HEAD, "--tools", "", "--mode", "default"],
                },
                "read_dir_args": ["--add-dir", "{dir}"],
            },
        },
        "driver-d": {
            "display_name": "Vendor D",
            "home_env": "TOOL_C_CONFIG",
            "credential_env": ["TOOL_D_API_KEY", "TOOL_C_API_KEY", "TOOL_C_TOKEN"],
            "lane": {
                "spec": "stdin",
                "output": "event-stream",
                "may_run_commands": False,
                "audit_stderr_when_empty": True,
                "argv": {
                    "read": [*_C_ARGV_HEAD, "--tools", "Read", "--mode", "default"],
                    "write": [*_C_ARGV_HEAD, "--tools", "Read,Edit", "--mode", "edit"],
                    "judgment": [*_C_ARGV_HEAD, "--tools", "", "--mode", "default"],
                },
                "read_dir_args": ["--add-dir", "{dir}"],
                "env": {
                    "TOOL_C_BASE_URL": "https://gateway.example.test/api",
                    "TOOL_C_API_KEY": "",
                },
                "credential_file": {"file": "tool-d-key", "env": "TOOL_C_TOKEN"},
            },
            "usage": {
                "snapshot": "spend",
                "credential": {"file": "tool-d-key", "format": "text"},
                "proxy_from_environment": False,
                "requests": [
                    {"name": "key", "url": "https://gateway.example.test/api/v1/key"}
                ],
                "fields": {
                    "usage_usd": "/key/data/usage",
                    "usage_daily_usd": "/key/data/usage_daily",
                    "usage_weekly_usd": "/key/data/usage_weekly",
                    "usage_monthly_usd": "/key/data/usage_monthly",
                    "limit_usd": "/key/data/limit",
                    "limit_remaining_usd": "/key/data/limit_remaining",
                    "limit_reset": "/key/data/limit_reset",
                },
            },
        },
        "driver-e": {},
    },
    "recorded_slots": {"first": "driver-a", "second": "driver-b"},
    "pane_driver": "driver-a",
}

LANE_DRIVERS = ("driver-a", "driver-b", "driver-c", "driver-d")
DECLARED_DRIVERS = (*LANE_DRIVERS, "driver-e")
CONFIG_DIRS = {
    "driver-a": "/operator/path/tool-a",
    "driver-b": "/operator/path/tool-b",
    "driver-c": "/operator/path/tool-c",
    "driver-d": "/operator/path/tool-d",
}


def driver_document() -> dict[str, Any]:
    """A fresh copy of the placeholder ``drivers.json`` document."""
    return copy.deepcopy(DRIVER_DOCUMENT)


def driver_specs() -> dict[str, DriverSpec]:
    """The validated placeholder drivers."""
    return parse_driver_document(driver_document()).drivers


def provider_argv(
    driver: str,
    *,
    cwd: str,
    write: bool,
    model: str,
    spec_file: str = "",
    output_file: str | None = None,
    read_dirs: tuple[str, ...] = (),
) -> list[str]:
    """The lane argv of one placeholder driver, scrubbed as a launch scrubs it."""
    drivers = driver_specs()
    return lane_argv(
        drivers[driver],
        "write" if write else "read",
        scrub=lane_scrub_env(drivers),
        spec_file=spec_file,
        cwd=cwd,
        model=model,
        output_file=output_file,
        read_dirs=read_dirs,
    )


def provider_judgment_argv(
    driver: str,
    *,
    spec_file: str,
    cwd: str,
    model: str,
    output_file: str | None = None,
) -> list[str]:
    """The judgment argv of one placeholder driver."""
    drivers = driver_specs()
    return judgment_argv(
        drivers[driver],
        scrub=lane_scrub_env(drivers),
        spec_file=spec_file,
        cwd=cwd,
        model=model,
        output_file=output_file,
    )


def install_recorded_slots() -> None:
    """Install the placeholder ``recorded_slots``, as ``main.open_store`` does."""
    install_recorded_names(parse_recorded_slots(DRIVER_DOCUMENT["recorded_slots"]))


def pane_markers(driver: str = "driver-a") -> PaneMarkers:
    """The pane markers of a placeholder driver with an interactive section."""
    interactive = driver_specs()[driver].interactive
    assert interactive is not None
    return interactive.pane


# Panes painted the way the ``driver-a`` markers describe.
PANE_WORKING = "  working... 4.3s                                   [stop]\n"
PANE_IDLE = "  done in 1.8s\n  Status bar: model-a\n  > \n"
PANE_PERMISSION = "  approve? edit notes.txt\n  Enter:yes  Tab:next  Esc:no\n"


def write_drivers(home: Path, document: dict[str, Any] | None = None) -> Path:
    path = home / "drivers.json"
    path.write_text(
        json.dumps(driver_document() if document is None else document),
        encoding="utf-8",
    )
    return path


def accounts_document() -> dict[str, Any]:
    """An ``ai-accounts.json`` that uses the placeholder drivers."""
    accounts = {
        f"{driver}-account": {"provider": driver, "config_dir": config_dir}
        for driver, config_dir in CONFIG_DIRS.items()
    }
    roles: dict[str, Any] = {}
    lanes: dict[str, str] = {}
    for driver in CONFIG_DIRS:
        roles[f"{driver}-writer"] = {
            "account": f"{driver}-account",
            "model": f"{driver}-model",
            "access": "workspace-write",
        }
        roles[f"{driver}-reader"] = {
            "account": f"{driver}-account",
            "model": f"{driver}-model",
            "access": "read-only",
        }
        lanes[f"{driver}:implementer"] = f"{driver}-writer"
        for kind in ("reviewer", "pr-reviewer-quality", "pr-reviewer-logic", "check"):
            lanes[f"{driver}:{kind}"] = f"{driver}-reader"
    return {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "driver-a", "second": "driver-b"},
        "accounts": accounts,
        "roles": roles,
        "lanes": lanes,
        "sessions": {"session-a": {"interactive": "driver-a-writer"}},
    }


WINDOWS_DRIVER_ID = "driver-w"
WINDOWS_POLL_SECONDS = 900
WINDOWS_LABEL = "Login usage"
WINDOWS_CREDENTIAL_LABEL = "login"
WINDOWS_URL = "https://usage.example/oauth/usage"
WINDOWS_SERVICE = "login-credentials"
WINDOWS_ACCOUNT_FILE = ".login.json"
WINDOWS_CREDENTIAL_FILE = ".credentials.json"
WINDOWS_HOME_DIR = "~/.login-tool"
WINDOWS_OBJECT = "loginOauth"
WINDOWS_TOKEN_FIELD = "accessToken"
WINDOWS_TOKEN_NAME = "access token"


def windows_usage() -> dict[str, Any]:
    """A valid ``windows`` usage section. Field names are placeholders."""
    return {
        "snapshot": "windows",
        "label": WINDOWS_LABEL,
        "poll_seconds": WINDOWS_POLL_SECONDS,
        "credential": {
            "file": WINDOWS_CREDENTIAL_FILE,
            "format": "oauth-file",
            "label": WINDOWS_CREDENTIAL_LABEL,
            "object": WINDOWS_OBJECT,
            "token": WINDOWS_TOKEN_FIELD,
            "token_name": WINDOWS_TOKEN_NAME,
            "expires_at": "expiresAt",
            "expires_unit": "epoch-ms",
            "subscription": "subscriptionType",
            "login_name": "Login",
            "renew": "login-tool",
            "multiplier": {
                "plan": "max",
                "pattern": r"(?:^|_)max_(\d+x)$",
                "field": "rateLimitTier",
            },
            "keychain": {
                "argv": [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    "{service}",
                    "-a",
                    "{user}",
                    "-w",
                ],
                "service": WINDOWS_SERVICE,
                "user_env": "USER",
                "user_pattern": r"[a-zA-Z0-9._-]+",
                "fallback_user": "login-user",
                "missing_status": 44,
                "platform": "darwin",
                "service_suffix": "nfc-sha256-8",
            },
        },
        "account_file": {
            "file": WINDOWS_ACCOUNT_FILE,
            "object": "loginAccount",
            "field": "emailAddress",
            "default_name": WINDOWS_ACCOUNT_FILE,
        },
        "proxy_from_environment": False,
        "requests": [
            {
                "name": "usage",
                "url": WINDOWS_URL,
                "headers": {
                    "X-Api-Beta": "oauth-2025-04-20",
                    "User-Agent": "dfx-agent",
                },
            }
        ],
        "fields": {
            "five_hour": "/usage/five_hour",
            "weekly": "/usage/seven_day",
            "limits": "/usage/limits",
        },
        "window": {
            "used_percent": "utilization",
            "resets_at": "resets_at",
            "round": "minute",
        },
        "limits": {
            "kind": "kind",
            "weekly_all": "weekly_all",
            "weekly_model": "weekly_scoped",
            "used_percent": "percent",
            "resets_at": "resets_at",
            "scope": "scope",
            "model": "model",
            "model_names": ["display_name", "id"],
        },
    }


def windows_driver() -> dict[str, Any]:
    """A record-only driver whose usage section is a windows adapter."""
    return {
        "display_name": "Login",
        "home_env": "LOGIN_CONFIG_DIR",
        "home_default_dir": WINDOWS_HOME_DIR,
        "usage": windows_usage(),
    }


def windows_driver_spec() -> DriverSpec:
    config = parse_driver_document({"drivers": {WINDOWS_DRIVER_ID: windows_driver()}})
    return config.drivers[WINDOWS_DRIVER_ID]


RPC_DRIVER_ID = "driver-r"
RPC_LABEL = "Seat usage"
RPC_HOME_ENV = "TOOL_R_HOME"
RPC_HOME_DIR = "~/.tool-r"
RPC_ACCOUNT_FILE = "auth.json"
RPC_METHOD = "limits/read"
RPC_POLL_SECONDS = 900
RPC_TIMEOUT_SECONDS = 30


def rpc_usage() -> dict[str, Any]:
    """A valid ``rpc`` usage section. Method and field names are placeholders."""
    return {
        "snapshot": "rpc",
        "label": RPC_LABEL,
        "poll_seconds": RPC_POLL_SECONDS,
        "timeout_seconds": RPC_TIMEOUT_SECONDS,
        "argv": ["tool-r", "serve-rpc"],
        "initialize_params": {"clientInfo": {"name": "agent-tests", "version": "1"}},
        "method": RPC_METHOD,
        "params": {},
        "result_pointer": "/limits",
        "windows_by_minutes": {"300": "five_hour", "10080": "weekly"},
        "fields": {
            "windows": ["/primary", "/secondary"],
            "used_percent": "usedPercent",
            "window_minutes": "windowMinutes",
            "resets_at": "resetsAt",
            "plan": "/plan",
            "limit_reached": "/reachedType",
            "reset_credits_count": "/resetCredits/availableCount",
        },
        "account_file": {
            "file": RPC_ACCOUNT_FILE,
            "object": "tokens",
            "jwt_field": "id_token",
            "jwt_claim": "email",
        },
    }


def rpc_driver() -> dict[str, Any]:
    """A record-only driver whose usage section is an rpc adapter."""
    return {
        "display_name": "Vendor R",
        "home_env": RPC_HOME_ENV,
        "home_default_dir": RPC_HOME_DIR,
        "credential_env": ["TOOL_R_API_KEY"],
        "usage": rpc_usage(),
    }


def rpc_driver_spec() -> DriverSpec:
    config = parse_driver_document({"drivers": {RPC_DRIVER_ID: rpc_driver()}})
    return config.drivers[RPC_DRIVER_ID]


def write_accounts(home: Path, document: dict[str, Any] | None = None) -> Path:
    path = home / "ai-accounts.json"
    path.write_text(
        json.dumps(accounts_document() if document is None else document),
        encoding="utf-8",
    )
    return path
