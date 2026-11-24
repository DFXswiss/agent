"""The local state of the agent tool (11-agent-access.md section 8.1).

`$AGENT_PLATFORM_HOME` (default `~/.agent-platform`) is a directory of mode `0700` owned by the user. It holds
`state.json` (`0600`): hub origin, pinned hub key, platform root key, agent and user ID, label, the sealed identity
and the handover secret `r`; and the agent's private keys: in the macOS login keychain (generic password, service
`agent-platform`, account = agent ID, through the `security` program of `AGENT_PLATFORM_SECURITY`) where one is
available, else in `agent.key` (`0600`). A directory or file with another mode or owner, or a symlink, is refused
(`state_damaged`). `delete` removes both (after `key_revoked`).

A pending pairing from another computer (ADR-067, `agent_platform.remote`) keeps `pending.json` (`0600`) and its
private keys in the keychain item `agent-platform` / `<pairing id>` where available, else in `pending.key` (`0600`);
the paired state above stays untouched until the pairing completes.

The keychain is written with `security -i` and the command on standard input, so the private key never appears on a
command line.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.errors import PlatformError
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.keys import Identity, ed25519_seed, x25519_private_raw

STATE_FILE = "state.json"
KEY_FILE = "agent.key"
PENDING_FILE = "pending.json"
PENDING_KEY_FILE = "pending.key"
KEYCHAIN_SERVICE = "agent-platform"
DEFAULT_SECURITY = "/usr/bin/security"
SECURITY_TIMEOUT_S = 30.0
STATE_VERSION = 1
KEYCHAIN = "keychain"
FILE = "file"


def home_dir(environ: Mapping[str, str]) -> Path:
    """`$AGENT_PLATFORM_HOME`, else `~/.agent-platform`."""
    configured = environ.get("AGENT_PLATFORM_HOME", "")
    return Path(configured) if configured else Path.home() / ".agent-platform"


def security_program(environ: Mapping[str, str]) -> str:
    return environ.get("AGENT_PLATFORM_SECURITY", "") or DEFAULT_SECURITY


def run_security(program: str, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the `security` program; its output is returned to the caller and never logged."""
    return subprocess.run(
        [program, *args], input=stdin, capture_output=True, text=True, timeout=SECURITY_TIMEOUT_S, check=False
    )


def keychain_available(program: str, platform: str = sys.platform) -> bool:
    """macOS with an executable `security` program."""
    return platform == "darwin" and os.path.isfile(program) and os.access(program, os.X_OK)


def _check(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    mode = 0o700 if directory else 0o600
    if not kind_ok or stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.geteuid():
        raise PlatformError("state_damaged")


def encode_keys(agent: Identity) -> str:
    """The private keys as one b64u text: X25519 private key (32 B) followed by the Ed25519 seed (32 B)."""
    return b64u_encode(x25519_private_raw(agent.kem) + ed25519_seed(agent.sig))


def decode_keys(text: str) -> Identity:
    try:
        raw = b64u_decode(text.strip(), 64)
    except Exception:
        raise PlatformError("state_damaged") from None
    return Identity.from_raw(raw[:32], raw[32:])


@dataclass
class Home:
    """The state directory and how the private keys are kept."""

    path: Path
    security: str
    keychain: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Home:
        program = security_program(environ)
        return cls(home_dir(environ), program, keychain_available(program))

    @property
    def state_file(self) -> Path:
        return self.path / STATE_FILE

    @property
    def key_file(self) -> Path:
        return self.path / KEY_FILE

    def check_dir(self) -> bool:
        """True when the directory exists and is sound; False when it does not exist; `state_damaged` otherwise."""
        try:
            _check(self.path, directory=True)
        except FileNotFoundError:
            return False
        return True

    def ensure_dir(self) -> None:
        if not self.check_dir():
            self.path.mkdir(mode=0o700, parents=True)
            os.chmod(self.path, 0o700)

    def _write(self, path: Path, text: str) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)

    def _read(self, path: Path) -> str:
        _check(path, directory=False)
        return path.read_text(encoding="utf-8")

    # --- state.json ---

    def load(self) -> dict[str, Any]:
        """The stored state; `not_paired` when there is none, `state_damaged` when it is unsound."""
        if not self.check_dir():
            raise PlatformError("not_paired")
        try:
            text = self._read(self.state_file)
        except FileNotFoundError:
            raise PlatformError("not_paired") from None
        try:
            data = json.loads(text)
        except ValueError:
            raise PlatformError("state_damaged") from None
        if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
            raise PlatformError("state_damaged")
        return data

    def save(self, data: Mapping[str, Any]) -> None:
        self.ensure_dir()
        self._write(self.state_file, json.dumps({**data, "v": STATE_VERSION}, sort_keys=True, indent=1) + "\n")

    # --- private keys ---

    def store_keys(self, agent_id: str, agent: Identity, key_file: Path | None = None) -> str:
        """Keep the private keys (keychain where available, else the file `key_file`, by default `agent.key`);
        returns where."""
        self.ensure_dir()
        secret = encode_keys(agent)
        if self.keychain:
            command = f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {agent_id} -w {secret}\n"
            done = run_security(self.security, ["-i"], command)
            if done.returncode == 0:
                return KEYCHAIN
        self._write(key_file or self.key_file, secret + "\n")
        return FILE

    def load_keys(self, agent_id: str, where: object, key_file: Path | None = None) -> Identity:
        if where == KEYCHAIN:
            done = run_security(self.security, ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", agent_id, "-w"])
            if done.returncode != 0:
                raise PlatformError("state_damaged")
            return decode_keys(done.stdout)
        if where != FILE:
            raise PlatformError("state_damaged")
        try:
            return decode_keys(self._read(key_file or self.key_file))
        except FileNotFoundError:
            raise PlatformError("state_damaged") from None

    # --- pending.json (a pairing from another computer) ---

    @property
    def pending_file(self) -> Path:
        return self.path / PENDING_FILE

    @property
    def pending_key_file(self) -> Path:
        return self.path / PENDING_KEY_FILE

    def load_pending(self) -> dict[str, Any] | None:
        """The pending pairing, None when there is none; `state_damaged` when it is unsound."""
        if not self.check_dir():
            return None
        try:
            text = self._read(self.pending_file)
        except FileNotFoundError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            raise PlatformError("state_damaged") from None
        if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
            raise PlatformError("state_damaged")
        return data

    def save_pending(self, data: Mapping[str, Any]) -> None:
        self.ensure_dir()
        self._write(self.pending_file, json.dumps({**data, "v": STATE_VERSION}, sort_keys=True, indent=1) + "\n")

    def delete_pending(self, agent_id: str, where: object, *, keep_keychain: bool = False) -> None:
        """Remove `pending.json` and its private keys (the keychain item unless it now belongs to the state)."""
        if where == KEYCHAIN and not keep_keychain:
            run_security(self.security, ["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", agent_id])
        for path in (self.pending_key_file, self.pending_file):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def delete(self, agent_id: str | None, where: object) -> None:
        """Remove the state and the private keys (after `key_revoked`, or before a new pairing)."""
        if where == KEYCHAIN and agent_id is not None:
            run_security(self.security, ["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", agent_id])
        for path in (self.key_file, self.state_file):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
