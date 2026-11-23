"""Configuration of both guest processes from ``AGENT_GUEST_*`` (fail-closed, 05-runner.md section 5.1).

Every value has the production default of the golden image; tests override the paths (no root needed). A variable
with the prefix that is not listed here is refused (``config_invalid <NAME>``), so a typo never falls back to a
default silently.

| Variable | Default | Rule |
|---|---|---|
| ``AGENT_GUEST_CONTROL_SOCKET`` | ``/run/agent-guest/control.sock`` | absolute path |
| ``AGENT_GUEST_API_SOCKET`` | ``/run/agent-guest/api.sock`` | absolute path |
| ``AGENT_GUEST_CONTROL_GROUP`` | ``agent-ctl`` | group of the control socket |
| ``AGENT_GUEST_API_GROUP`` | ``platform`` | group of the api socket |
| ``AGENT_GUEST_CONTROL_TOKEN_FILE`` | ``/etc/agent-guest/control-token`` | absolute path |
| ``AGENT_GUEST_JOURNAL`` | ``/var/lib/agent-guest/journal.db`` | absolute path |
| ``AGENT_GUEST_API_PORT`` | ``7700`` | 0-65535 (0: any free port, tests) |
| ``AGENT_GUEST_BUILD_FILE`` | ``/opt/agent-guest/build.json`` | absolute path, optional file |
| ``AGENT_GUEST_CLAUDE_INSTALL_FILE`` | ``/opt/claude/install.json`` | absolute path, optional file |
| ``AGENT_GUEST_BOOT_ID_FILE`` | ``/proc/sys/kernel/random/boot_id`` | absolute path |
| ``AGENT_GUEST_CONTRACTS`` | ``contracts`` next to the package (``/opt/agent-guest/contracts``) | absolute path |
| ``AGENT_GUEST_GOLDEN_INSTALL_FILE`` | ``/var/lib/agent-golden/install.json`` | absolute path (TEST_MODE marker) |
| ``AGENT_GUEST_FAULT_HOOKS`` | empty | empty or ``500`` (hooks answer 500; only in a TEST_MODE golden) |
| ``AGENT_GUEST_PEER_TABLE`` | empty | empty or absolute path of the TCP socket table (``peer.py``) |

``AGENT_GUEST_PEER_TABLE`` (``/proc/net/tcp`` in ``agent-guest-api.service``) binds every session token to the UID
of the connecting process; empty switches the binding off (tests, other operating systems).

The API process binds ``127.0.0.1`` only; the address is not configurable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PREFIX = "AGENT_GUEST_"
API_HOST = "127.0.0.1"
DEFAULT_CONTRACTS = str(Path(__file__).resolve().parent.parent / "contracts")
DEFAULTS: dict[str, str] = {
    "CONTROL_SOCKET": "/run/agent-guest/control.sock",
    "API_SOCKET": "/run/agent-guest/api.sock",
    "CONTROL_GROUP": "agent-ctl",
    "API_GROUP": "platform",
    "CONTROL_TOKEN_FILE": "/etc/agent-guest/control-token",
    "JOURNAL": "/var/lib/agent-guest/journal.db",
    "API_PORT": "7700",
    "BUILD_FILE": "/opt/agent-guest/build.json",
    "CLAUDE_INSTALL_FILE": "/opt/claude/install.json",
    "BOOT_ID_FILE": "/proc/sys/kernel/random/boot_id",
    "CONTRACTS": DEFAULT_CONTRACTS,
    "GOLDEN_INSTALL_FILE": "/var/lib/agent-golden/install.json",
    "FAULT_HOOKS": "",
    "PEER_TABLE": "",
}
PATHS = frozenset(DEFAULTS) - {"CONTROL_GROUP", "API_GROUP", "API_PORT", "FAULT_HOOKS", "PEER_TABLE"}
FAULT_HOOKS_VALUES = frozenset({"", "500"})
_GROUP_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_PORT_RE = re.compile(r"0|[1-9][0-9]{0,4}")


class ConfigError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"config_invalid {PREFIX}{name}")
        self.name = name


@dataclass(frozen=True)
class GuestConfig:
    control_socket: str
    api_socket: str
    control_group: str
    api_group: str
    control_token_file: str
    journal: str
    api_port: int
    build_file: str
    claude_install_file: str
    boot_id_file: str
    contracts: str
    golden_install_file: str = DEFAULTS["GOLDEN_INSTALL_FILE"]
    fault_hooks: str = ""
    peer_table: str = ""

    @property
    def api_host(self) -> str:
        return API_HOST


def load_config(environ: Mapping[str, str] | None = None) -> GuestConfig:
    env = os.environ if environ is None else environ
    for key in sorted(env):
        if key.startswith(PREFIX) and key[len(PREFIX) :] not in DEFAULTS:
            raise ConfigError(key[len(PREFIX) :])
    values = {name: env.get(PREFIX + name, default) for name, default in DEFAULTS.items()}
    for name in sorted(PATHS):
        value = values[name]
        if not value.startswith("/") or "\x00" in value or "\n" in value:
            raise ConfigError(name)
    for name in ("CONTROL_GROUP", "API_GROUP"):
        if not _GROUP_RE.fullmatch(values[name]):
            raise ConfigError(name)
    if not _PORT_RE.fullmatch(values["API_PORT"]) or int(values["API_PORT"]) > 65535:
        raise ConfigError("API_PORT")
    if values["FAULT_HOOKS"] not in FAULT_HOOKS_VALUES:
        raise ConfigError("FAULT_HOOKS")
    table = values["PEER_TABLE"]
    if table and (not table.startswith("/") or "\x00" in table or "\n" in table):
        raise ConfigError("PEER_TABLE")
    return GuestConfig(
        control_socket=values["CONTROL_SOCKET"],
        api_socket=values["API_SOCKET"],
        control_group=values["CONTROL_GROUP"],
        api_group=values["API_GROUP"],
        control_token_file=values["CONTROL_TOKEN_FILE"],
        journal=values["JOURNAL"],
        api_port=int(values["API_PORT"]),
        build_file=values["BUILD_FILE"],
        claude_install_file=values["CLAUDE_INSTALL_FILE"],
        boot_id_file=values["BOOT_ID_FILE"],
        contracts=values["CONTRACTS"],
        golden_install_file=values["GOLDEN_INSTALL_FILE"],
        fault_hooks=values["FAULT_HOOKS"],
        peer_table=table,
    )
