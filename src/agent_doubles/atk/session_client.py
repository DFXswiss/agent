"""HTTP client of the attack suite that runs as a session user inside the test VM (ATK-36, ATK-37, ATK-39).

A session that escaped Claude Code's tool restrictions could talk to the guest API on ``127.0.0.1:7700`` directly.
The suite plays that adversary: ``guest_probe.py client`` starts this file (standard library only, copied to
``CLIENT``) as the session's own user in a transient unit ``atk-client-<hex>`` (the evidence evaluation knows these
units are the suite's, ``evidence.SUITE_UNIT_PREFIX``), reads a JSON list of requests on stdin and prints one JSON
document with the answers:

    request  {"label": str, "method": "POST", "path": "/mcp", "headers": [[name, value], ...],
              "json": <any> | "text": str | "base64": str | "fill": [prefix, unit, count, suffix],
              "raw": base64 (sent as is, overrides the rest),
              "repeat": n}
    answer   {"answers": [{"label", "status", "headers": {lower name: value}, "body": first BODY_LIMIT characters,
              "seconds": float}, ...]}

``{token}`` in a header value is replaced by ``ATK_TOKEN`` of the environment (the driver passes the session's token
there, never on a command line). A connection failure is status 0 with the error in ``body``.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import os
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import IO, Any

HOST = "127.0.0.1"
PORT = 7700
BODY_LIMIT = 4000
TIMEOUT = 30.0
CLIENT = "/usr/local/lib/atk-suite/session_client.py"
KEPT_HEADERS = ("retry-after", "content-type", "www-authenticate")


def payload(request: Mapping[str, Any]) -> bytes:
    if "json" in request:
        return json.dumps(request["json"]).encode()
    if "base64" in request:
        return base64.b64decode(request["base64"])
    if "fill" in request:  # large bodies are built here: the request list travels on a command line
        prefix, unit, count, suffix = request["fill"]
        return (str(prefix) + str(unit) * int(count) + str(suffix)).encode()
    return str(request.get("text", "")).encode()


def headers_of(request: Mapping[str, Any], token: str) -> list[tuple[str, str]]:
    return [(str(name), str(value).replace("{token}", token)) for name, value in request.get("headers", [])]


def send_raw(data: bytes, host: str, port: int) -> tuple[int, dict[str, str], str]:
    """Send `data` unchanged and parse the status line and headers of the answer."""
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        received = b""
        while chunk := sock.recv(65536):
            received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    found = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        found[name.strip().lower()] = value.strip()
    return status, found, body.decode("utf-8", "replace")


def send(request: Mapping[str, Any], token: str, host: str, port: int) -> tuple[int, dict[str, str], str]:
    if "raw" in request:
        return send_raw(base64.b64decode(request["raw"]).replace(b"{token}", token.encode()), host, port)
    data = payload(request)
    connection = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        connection.putrequest(str(request.get("method", "POST")), str(request["path"]), skip_accept_encoding=True)
        for name, value in headers_of(request, token):
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(data)))
        # the server may refuse before reading the whole body (413): its answer may still be there
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            connection.endheaders(data)
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


def run(
    requests: Sequence[Mapping[str, Any]],
    token: str,
    *,
    host: str = HOST,
    port: int = PORT,
    clock: Callable[[], float] = time.perf_counter,
) -> list[dict[str, Any]]:
    answers = []
    for request in requests:
        for _ in range(int(request.get("repeat", 1))):
            begin = clock()
            try:
                status, headers, body = send(request, token, host, port)
            except (OSError, http.client.HTTPException, ValueError) as error:
                status, headers, body = 0, {}, f"{type(error).__name__}: {error}"
            answers.append(
                {
                    "label": str(request.get("label", "")),
                    "status": status,
                    "headers": {name: headers[name] for name in KEPT_HEADERS if name in headers},
                    "body": body[:BODY_LIMIT],
                    "seconds": round(clock() - begin, 6),
                }
            )
    return answers


def main(stdin: IO[str] | None = None, stdout: IO[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    source, sink = stdin or sys.stdin, stdout or sys.stdout
    env = os.environ if environ is None else environ
    requests = json.loads(source.read())
    sink.write(json.dumps({"answers": run(requests, env.get("ATK_TOKEN", ""), port=PORT)}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
