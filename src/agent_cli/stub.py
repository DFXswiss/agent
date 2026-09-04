"""Stdlib-only client for the loopback CLI bridge. No store, no git, no tmux."""

from __future__ import annotations

import json
import os
import socket
import sys

from .cli_bridge import DEFAULT_HOST, DEFAULT_PORT, MAX_REQUEST_BYTES


def parse_endpoint(raw: str) -> tuple[str, int]:
    text = raw.strip()
    if text == "":
        raise SystemExit("cli-bridge endpoint is empty")
    if ":" not in text:
        raise SystemExit("cli-bridge endpoint must be host:port")
    host, _, port_s = text.rpartition(":")
    if host == "":
        raise SystemExit("cli-bridge endpoint must be host:port")
    try:
        port = int(port_s)
    except ValueError as exc:
        raise SystemExit("cli-bridge endpoint must be host:port") from exc
    if port <= 0 or port > 65535:
        raise SystemExit("cli-bridge endpoint must be host:port")
    return host, port


def call(argv: list[str], *, endpoint: str | None = None) -> int:
    raw = endpoint if endpoint is not None else os.environ.get("AGENT_CLI_BRIDGE", "")
    if raw is None or raw == "":
        host, port = DEFAULT_HOST, DEFAULT_PORT
    else:
        host, port = parse_endpoint(raw)
    sock = socket.create_connection((host, port), timeout=30)
    try:
        sock.sendall((json.dumps({"argv": argv}) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        total = 0
        while True:
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
            if total > MAX_REQUEST_BYTES:
                raise SystemExit("cli-bridge response too large")
            if b"\n" in piece:
                break
        payload = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("cli-bridge: invalid response") from exc
    finally:
        sock.close()
    if not isinstance(payload, dict):
        raise SystemExit("cli-bridge: invalid response")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    code = payload.get("exit_code")
    if isinstance(stdout, str) and stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if isinstance(stderr, str) and stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    if not isinstance(code, int):
        return 2
    return code


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    raise SystemExit(call(args))


if __name__ == "__main__":
    main()
