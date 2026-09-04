"""Loopback allowlist for remote argv. Store and spine only; never git/gh/control."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any

from .daemon import agent_argv
from .runtime import Completed, run_argv as _default_run_argv
from .store import StoreError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7846
MAX_REQUEST_BYTES = 1_048_576

_STORE_COMMANDS = frozenset(
    {
        "activity",
        "task",
        "checklist",
        "round",
        "agent",
        "check",
        "gate",
        "work",
        "allow",
        "next",
        "close-step",
        "status",
        "skills",
    }
)
_SESSION_SUB = frozenset({"register", "heartbeat", "list", "close", "skill"})


def allowed(argv: list[str]) -> bool:
    """True when argv is a store/spine command. Control, git, and hub stay local."""
    if not argv:
        return False
    cmd = argv[0]
    if cmd in _STORE_COMMANDS:
        return True
    if cmd != "session" or len(argv) < 2:
        return False
    return argv[1] in _SESSION_SUB


def handle_request(
    raw: bytes,
    *,
    runner: Callable[[list[str]], Completed] | None = None,
) -> dict[str, Any]:
    """Parse one JSON request, run an allowlisted command, return a JSON-ready dict."""
    run = runner or _default_run_argv
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"exit_code": 2, "stdout": "", "stderr": "cli-bridge: invalid JSON"}
    if not isinstance(body, dict):
        return {"exit_code": 2, "stdout": "", "stderr": "cli-bridge: request must be an object"}
    argv = body.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) and item != "" for item in argv):
        return {"exit_code": 2, "stdout": "", "stderr": "cli-bridge: argv must be an array of strings"}
    if not allowed(argv):
        return {"exit_code": 2, "stdout": "", "stderr": "cli-bridge: command not allowed"}
    completed = run([*agent_argv(), *argv])
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    runner: Callable[[list[str]], Completed] | None = None,
    bind: Callable[..., socket.socket] | None = None,
) -> None:
    """Listen on loopback and serve one request per connection. Does not return."""
    opener = bind or socket.socket
    if host not in ("127.0.0.1", "::1"):
        raise StoreError("cli-bridge bind must be 127.0.0.1 or ::1")
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    sock = opener(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(16)
    while True:
        try:
            conn, addr = sock.accept()
        except OSError:
            continue
        try:
            conn.settimeout(30)
            peer = addr[0] if addr else ""
            if peer not in ("127.0.0.1", "::1", ""):
                continue
            raw = _read_request(conn)
            payload = handle_request(raw, runner=runner)
            conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            continue
        except Exception as exc:
            try:
                conn.sendall(
                    (
                        json.dumps(
                            {
                                "exit_code": 2,
                                "stdout": "",
                                "stderr": f"cli-bridge: {type(exc).__name__}",
                            }
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            except OSError:
                pass
            continue
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _read_request(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = conn.recv(4096)
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
        if total > MAX_REQUEST_BYTES:
            return b""
        if b"\n" in piece:
            break
    return b"".join(chunks).split(b"\n", 1)[0]
