"""The tool for AI chat programs without a command line (KOR-63): `mcp` serves it as a local MCP server over stdio,
`install-connector` enters that server in the configuration of Claude Desktop.

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP, stdio transport) and has two tools: `search`
(the lookup of `agent-platform search`) and `run` (one command, run in-process exactly as `cli.run` runs it, with the
same result object). It serves both eras of the protocol: a client that sends `initialize` gets the handshake of
revision 2025-11-25 and earlier, a request carrying the version in `_meta` is served without one (2026-07-28,
https://modelcontextprotocol.io/specification/latest). Nothing but protocol messages reaches stdout: while serving,
`sys.stdout` is stderr, and commands get their stdin from the tool call, never from the protocol stream.

`mcp --socket <path>` serves the same protocol over HTTP/1.1 on a unix socket (the main orchestrator inside a platform
session, ADR-082 sections 6.5 and 9.7, 05-runner.md section 17.5): `POST /mcp` with one JSON-RPC message (at most
`MAX_BODY` bytes, `Content-Length` required) is answered with `application/json`, a notification or response with
`202`; a body that is no JSON is the JSON-RPC parse error with `400`, a larger one `413`; `GET`/`DELETE /mcp` are `405`
and every other path `404`. The socket gets mode `0666`: the directory it lies in (`agent-orch:platform 0710` in the
guest) decides who reaches it. A stale socket file is replaced, any other file at the path is `invalid_argument`. The
server runs until `SIGTERM`/`SIGINT` and removes its socket. In session mode the tool `run` also refuses `pair` and the
commands of `relay.refused_in_session`: the session's agent never pairs another agent or changes this one.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import socketserver
import stat
import sys
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, NoReturn, TextIO

from agent_platform import __version__, lookup, relay
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError

SERVER = "agent-dfx-swiss"
MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MAX_BODY = 1024 * 1024
SOCKET_MODE = 0o666
PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS, UNSUPPORTED_VERSION = -32700, -32600, -32601, -32602, -32022
# Commands that make no sense through the server: the server itself, its installation, and an endless loop.
REFUSED = (["mcp"], ["install-connector"], ["claude", "watch"])
INSTRUCTIONS = (
    "The agent tool of agent.dfx.swiss: tasks, organizations, workgroups and devices of the user's account. Start "
    'with the tool run and args ["guide"], find the command for a job with search, then call it with run.'
)
TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "title": "Find a command",
        "description": (
            "Find the agent-platform command for a job (English or German words): purpose, call, example and next "
            'step of the best matches. Read the guide first: run with args ["guide"].'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "what you want to do, e.g. invite a person"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run",
        "title": "Run a command",
        "description": (
            'Run one agent-platform command and get its JSON result. Start with args ["guide"]; find commands with '
            'search; args are the words after agent-platform, e.g. ["task", "list", "--wg", "<id>"]. Where the '
            'guide pipes text into a command (an invitation link, a secret, a prompt with "--text -"), pass that '
            "text as stdin."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "args": {"type": "array", "items": {"type": "string"}, "description": "the command line words"},
                "stdin": {"type": "string", "description": "text the command reads from stdin (optional)"},
            },
            "required": ["args"],
            "additionalProperties": False,
        },
    },
]


def _cli() -> Any:
    from agent_platform import cli  # noqa: PLC0415 - cli imports the registry, which imports this module

    return cli


# --- the tools ---


def _bad() -> dict[str, Any]:
    return PlatformError("invalid_argument").to_json()


def tool_search(arguments: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or set(arguments) != {"query"}:
        return _bad()
    return {"ok": True, **lookup.search(_cli().build_parser(), query)}


def tool_run(arguments: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    argv = arguments.get("args")
    stdin = arguments.get("stdin", "")
    if (
        not isinstance(argv, list)
        or not all(isinstance(word, str) for word in argv)
        or not isinstance(stdin, str)
        or not set(arguments) <= {"args", "stdin"}
    ):
        return _bad()
    words = relay.command_words(argv)
    if any(words[: len(refused)] == refused for refused in REFUSED):
        return _bad()
    if environ.get(relay.RELAY_ENV) and (words[:1] == ["pair"] or relay.refused_in_session(argv)):
        return PlatformError("not_in_session").to_json()
    out = io.StringIO()
    _cli().run(argv, environ, io.StringIO(stdin), out)
    document: dict[str, Any] = json.loads(out.getvalue())
    return document


HANDLERS: dict[str, Callable[[Mapping[str, Any], Mapping[str, str]], dict[str, Any]]] = {
    "search": tool_search,
    "run": tool_run,
}


# --- the protocol ---


class Failure(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _version(params: Mapping[str, Any]) -> str | None:
    """The protocol version of a request of the modern era (in `_meta`), or None for the legacy era."""
    meta = params.get("_meta")
    if not isinstance(meta, dict) or META_VERSION not in meta:
        return None
    version = meta[META_VERSION]
    if version not in MODERN:
        raise Failure(
            UNSUPPORTED_VERSION, "Unsupported protocol version", {"supported": [*MODERN, *LEGACY], "requested": version}
        )
    if not isinstance(meta.get(META_CAPABILITIES), dict):
        raise Failure(INVALID_PARAMS, f"Missing {META_CAPABILITIES}")
    return str(version)


def _initialize(params: Mapping[str, Any]) -> dict[str, Any]:
    asked = params.get("protocolVersion")
    return {
        "protocolVersion": asked if asked in LEGACY else LEGACY[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER, "title": "agent.dfx.swiss", "version": __version__},
        "instructions": INSTRUCTIONS,
    }


def _discover() -> dict[str, Any]:
    return {
        "supportedVersions": [*MODERN, *LEGACY],
        "capabilities": {"tools": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": SERVER, "version": __version__}},
        "instructions": INSTRUCTIONS,
    }


def _call(params: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise Failure(INVALID_PARAMS, "arguments must be an object")
    if not isinstance(name, str) or name not in HANDLERS:
        raise Failure(INVALID_PARAMS, f"Unknown tool: {name}")
    document = HANDLERS[name](arguments, environ)
    text = json.dumps(document, ensure_ascii=False, sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "structuredContent": document, "isError": not document["ok"]}


def answer(message: object, environ: Mapping[str, str]) -> dict[str, Any] | None:
    """The answer to one message, or None for a notification or a response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Invalid Request"}}
    if "method" not in message or "id" not in message:
        return None
    ident = message["id"]
    try:
        method = message["method"]
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise Failure(INVALID_PARAMS, "params must be an object")
        modern = _version(params) is not None
        if method == "initialize":
            result = _initialize(params)
        elif method == "ping":
            result = {}
        elif method == "server/discover":
            result = _discover()
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _call(params, environ)
        else:
            raise Failure(NO_METHOD, f"Method not found: {method}")
    except Failure as failure:
        error: dict[str, Any] = {"code": failure.code, "message": failure.message}
        if failure.data is not None:
            error["data"] = failure.data
        return {"jsonrpc": "2.0", "id": ident, "error": error}
    if modern:
        result = {"resultType": "complete", **result}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _parse(raw: bytes) -> tuple[bool, dict[str, Any] | None]:
    """`(parsed, reply)` of one request body: the answer, or the parse error when it is no JSON."""
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError):
        return False, {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "Parse error"}}
    return True, message


def serve(reader: TextIO, writer: TextIO, environ: Mapping[str, str]) -> None:
    """Answer every line of `reader` until it ends; one JSON-RPC message per line in both directions."""
    for line in reader:
        if not line.strip():
            continue
        parsed, message = _parse(line.encode("utf-8"))
        reply = answer(message, environ) if parsed else message
        if reply is not None:
            writer.write(json.dumps(reply, separators=(",", ":")) + "\n")
            writer.flush()


class _Handler(BaseHTTPRequestHandler):
    server: SocketServer
    protocol_version = "HTTP/1.1"
    server_version = "agent-platform"

    def log_message(self, format: str, *args: Any) -> None:
        """Nothing of a request is logged."""

    def _send(self, status: int, document: object = None) -> None:
        body = b"" if document is None else json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if document is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 405:
            self.send_header("Allow", "POST")
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, status: int) -> None:
        self.close_connection = True
        codes = {400: "request_invalid", 404: "not_found", 405: "not_allowed", 413: "size_exceeded"}
        self._send(status, {"error": codes[status]})

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._refuse(404)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            self._refuse(400)
            return
        if int(length) > MAX_BODY:
            self._refuse(413)
            return
        parsed, message = _parse(self.rfile.read(int(length)))
        if not parsed:
            self._send(400, message)
            return
        reply = answer(message, self.server.environ)
        if reply is None:
            self._send(202)
        else:
            self._send(200, reply)

    def do_GET(self) -> None:
        self._refuse(405 if self.path == "/mcp" else 404)

    do_DELETE = do_GET


class SocketServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, environ: Mapping[str, str]) -> None:
        self.environ = environ
        self.path = path
        super().__init__(path, _Handler)


def socket_server(path: str, environ: Mapping[str, str]) -> SocketServer:
    """The server on `path` (module docstring): a stale socket is replaced, the new one gets `SOCKET_MODE`."""
    if not path.startswith("/"):
        raise PlatformError("invalid_argument")
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise PlatformError("invalid_argument")
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = SocketServer(path, environ)
    os.chmod(path, SOCKET_MODE)
    return server


def serve_socket(server: SocketServer) -> None:
    """Serve until `SIGTERM` or `SIGINT`, then close the server and remove its socket."""

    def stop(signum: int, frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        server.serve_forever(0.05)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(server.path)


def cmd_mcp(args: argparse.Namespace, context: Context) -> NoReturn:
    """Serve until stdin ends (or, with `--socket`, until `SIGTERM`), then exit without the closing JSON object of
    `cli.run` (stdout carries protocol messages only)."""
    if args.socket is not None:
        server = socket_server(args.socket, context.environ)
        with contextlib.redirect_stdout(sys.stderr):
            serve_socket(server)
        raise SystemExit(0)
    writer = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        serve(context.stdin, writer, context.environ)
    raise SystemExit(0)


# --- Claude Desktop ---


def desktop_config(environ: Mapping[str, str], platform: str) -> Path:
    """Where Claude Desktop keeps its configuration (modelcontextprotocol.io, connect local servers)."""
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if platform == "win32":
        roaming = Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return roaming / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def server_entry(environ: Mapping[str, str]) -> dict[str, Any]:
    """The entry that starts this tool's server: the installed `agent-platform` program, else this Python with
    `-m agent_platform`."""
    program = Path(sys.argv[0]).resolve()
    if program.name.startswith("agent-platform") and program.is_file():
        entry: dict[str, Any] = {"command": str(program), "args": ["mcp"]}
    else:
        entry = {"command": sys.executable, "args": ["-m", "agent_platform", "mcp"]}
    if environ.get("AGENT_PLATFORM_HOME"):
        entry["env"] = {"AGENT_PLATFORM_HOME": environ["AGENT_PLATFORM_HOME"]}
    return entry


def cmd_install_connector(args: argparse.Namespace, context: Context) -> dict[str, Any]:
    path = Path(args.config) if args.config else desktop_config(context.environ, sys.platform)
    entry = server_entry(context.environ)
    result = {"config": str(path), "server": SERVER, "written": False, "entry": entry, "next": "restart Claude Desktop"}
    if args.print:
        return result
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        raise PlatformError("invalid_argument", reason="config_invalid") from None
    if not isinstance(config, dict) or not isinstance(config.setdefault("mcpServers", {}), dict):
        raise PlatformError("invalid_argument", reason="config_invalid")
    config["mcpServers"][SERVER] = entry
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".tmp")
    partial.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if target.exists():
        partial.chmod(target.stat().st_mode & 0o777)
    os.replace(partial, target)
    return {**result, "written": True}


def register(tree: Tree) -> None:
    sub = tree.add(
        tree.commands,
        "mcp",
        cmd_mcp,
        "serve this tool to an AI chat program (Claude Desktop) as a local MCP server on stdin/stdout",
        common=False,
        example="agent-platform mcp",
        words="chatprogramm anschluss claude-desktop connector server chat app mcp stdio",
    )
    sub.add_argument("--socket", help=argparse.SUPPRESS)
    sub = tree.add(
        tree.commands,
        "install-connector",
        cmd_install_connector,
        "enter this tool's MCP server in the configuration of Claude Desktop (keeps the other servers)",
        common=False,
        example="agent-platform install-connector",
        words="einrichten installieren anschliessen verbinden chatprogramm claude-desktop connector install setup "
        "configure chat app mcp",
    )
    sub.add_argument("--config", help="another configuration file than the one of Claude Desktop")
    sub.add_argument("--print", action="store_true", help="return the entry without writing it")
