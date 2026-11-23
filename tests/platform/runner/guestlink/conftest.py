"""Fixtures of the guestlink tests: a real guest control process (GST-01 `ControlService`) on a Unix socket in a
short directory under /tmp, the stand-in for Lima's forwarded `<instance>/sock/agent-guest.sock`."""

from __future__ import annotations

import grp
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_guest.config import load_config
from agent_guest.control import ControlContext, ControlService

TOKEN = "Z3Vlc3RsaW5rLXRlc3QtdG9rZW4tMDEyMzQ1Njc4OWFi"
BOOT_ID = "0b6f7f2c-8d1e-4f3a-9c5b-7a6e5d4c3b2a"


@dataclass
class Guest:
    socket: str
    token: str
    context: ControlContext
    service: ControlService


@pytest.fixture
def guest(repo_root: Path) -> Iterator[Guest]:
    base = Path(tempfile.mkdtemp(prefix="gl", dir="/tmp"))
    (base / "control-token").write_text(TOKEN, encoding="ascii")
    (base / "boot_id").write_text(BOOT_ID + "\n", encoding="ascii")
    (base / "build.json").write_text(json.dumps({"landlock_abi": 4}), encoding="utf-8")
    (base / "install.json").write_text(json.dumps({"version": "2.1.330"}), encoding="utf-8")
    group = grp.getgrgid(os.getgid()).gr_name
    config = load_config(
        {
            "AGENT_GUEST_CONTROL_SOCKET": str(base / "control.sock"),
            "AGENT_GUEST_API_SOCKET": str(base / "api.sock"),
            "AGENT_GUEST_CONTROL_GROUP": group,
            "AGENT_GUEST_API_GROUP": group,
            "AGENT_GUEST_CONTROL_TOKEN_FILE": str(base / "control-token"),
            "AGENT_GUEST_JOURNAL": str(base / "state" / "journal.db"),
            "AGENT_GUEST_API_PORT": "0",
            "AGENT_GUEST_BUILD_FILE": str(base / "build.json"),
            "AGENT_GUEST_CLAUDE_INSTALL_FILE": str(base / "install.json"),
            "AGENT_GUEST_BOOT_ID_FILE": str(base / "boot_id"),
            "AGENT_GUEST_CONTRACTS": str(repo_root / "contracts"),
        }
    )
    service = ControlService(config)
    context = service.start()
    try:
        yield Guest(socket=config.control_socket, token=TOKEN, context=context, service=service)
    finally:
        service.stop()
        shutil.rmtree(base, ignore_errors=True)
