"""`mcp` and `install-connector` (KOR-63): the tool as a local MCP server over stdio for AI chat programs, and its
entry in the configuration of Claude Desktop."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_platform import cli, connector, errors

from .conftest import Tool
from .hubfake import HubFake, User

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
MODERN = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def call(ident: int, name: str, arguments: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def stdio(env: dict[str, str], messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, int]:
    """A client on the real tool process: every message on its own line, then stdin closes."""
    done = subprocess.run(
        [sys.executable, "-m", "agent_platform", "mcp"],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
        check=False,
    )
    return [json.loads(line) for line in done.stdout.splitlines()], done.stderr, done.returncode


def serve(lines: list[str], env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    out = io.StringIO()
    connector.serve(io.StringIO("".join(line + "\n" for line in lines)), out, env or {})
    text = out.getvalue()
    assert text.endswith("\n") or not text
    return [json.loads(line) for line in text.splitlines()]


def ask(message: object, env: dict[str, str] | None = None) -> dict[str, Any]:
    replies = serve([json.dumps(message)], env)
    assert len(replies) == 1, replies
    return replies[0]


def tool_result(reply: dict[str, Any]) -> dict[str, Any]:
    result = reply["result"]
    document: dict[str, Any] = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == document
    assert result["content"][0]["type"] == "text"
    assert result["isError"] is (not document["ok"])
    return document


BAD = {"ok": False, "error": "invalid_argument", "next": errors.NEXT["invalid_argument"]}


def default_wg(hub: HubFake, user: User) -> str:
    for org in hub.orgs.values():
        if org.log.state.member(user.id) is not None:
            return str(org.log.state.default_wg)
    raise AssertionError("no org")


# --- the server over stdio ---


def test_stdio_session_with_the_real_process(tool: Tool) -> None:
    replies, stderr, code = stdio(
        tool.env,
        [
            INIT,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call(3, "search", {"query": "create a task"}),
            call(4, "run", {"args": ["guide"]}),
            call(5, "run", {"args": ["status"]}),
        ],
    )
    assert code == 0
    assert stderr == ""
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4, 5]
    init = replies[0]["result"]
    assert init["protocolVersion"] == "2025-11-25"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"]["name"] == "agent-dfx-swiss"
    assert "guide" in init["instructions"]
    assert [item["name"] for item in replies[1]["result"]["tools"]] == ["search", "run"]
    found = tool_result(replies[2])
    assert "task create" in [hit["command"] for hit in found["results"]]
    guide = tool_result(replies[3])
    assert guide["ok"] is True
    assert guide["guide"] == tool.run("guide")[1]["guide"]
    status = tool_result(replies[4])
    assert (status["ok"], status["error"]) == (False, "not_paired")
    assert replies[4]["result"]["isError"] is True


def test_run_through_the_server_creates_and_shows_a_task(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    replies, _stderr, code = stdio(
        tool.env, [INIT, call(2, "run", {"args": ["task", "create", "--wg", wg, "--text", "-"], "stdin": "Fix it"})]
    )
    assert code == 0
    created = tool_result(replies[1])
    assert created["ok"] is True, created
    assert created["title"] == "Fix it"
    replies, _stderr, code = stdio(tool.env, [call(1, "run", {"args": ["task", "show", "--task", created["task_id"]]})])
    shown = tool_result(replies[0])
    assert (shown["title"], shown["unverified"]) == ("Fix it", 0)
    assert [event["body"]["text"] for event in shown["events"]] == ["Fix it"]
    assert {event["author"] for event in shown["events"]} == {account.id}
    # the same object as the command line gives
    direct = tool.run("task", "show", "--task", created["task_id"])[1]
    assert {**direct, "runtime_s": 0} == {**shown, "runtime_s": 0}


def test_mcp_command_writes_protocol_messages_only(
    tool: Tool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def stray(arguments: object, environ: object) -> dict[str, Any]:
        print("progress that must not reach stdout")
        return {"ok": True}

    monkeypatch.setitem(connector.HANDLERS, "search", stray)
    closing = io.StringIO()
    lines = json.dumps(call(1, "search", {"query": "x"})) + "\n"
    with pytest.raises(SystemExit) as stopped:
        cli.run(["mcp"], tool.env, io.StringIO(lines), closing)
    assert stopped.value.code == 0
    assert closing.getvalue() == ""
    captured = capsys.readouterr()
    assert [json.loads(line)["id"] for line in captured.out.splitlines()] == [1]
    assert captured.err == "progress that must not reach stdout\n"


# --- the protocol, in-process ---


def test_framing_and_json_rpc_errors() -> None:
    replies = serve(
        [
            "",
            "{not json",
            "[" * 100_000,
            "[]",
            json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}),
            json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": "x", "method": "resources/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": [1]}),
        ]
    )
    assert replies == [
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
        {"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "Method not found: resources/list"}},
        {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "params must be an object"}},
    ]


def test_initialize_negotiates_the_version() -> None:
    for asked, answered in (
        ("2025-06-18", "2025-06-18"),
        ("2024-11-05", "2024-11-05"),
        ("2026-07-28", "2025-11-25"),
        ("1999-01-01", "2025-11-25"),
        (None, "2025-11-25"),
    ):
        reply = ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": asked}})
        assert reply["result"]["protocolVersion"] == answered
        assert "resultType" not in reply["result"]


def test_modern_requests_without_handshake() -> None:
    discovered = ask({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MODERN}})["result"]
    assert discovered["resultType"] == "complete"
    assert discovered["supportedVersions"][0] == "2026-07-28"
    assert "2025-11-25" in discovered["supportedVersions"]
    assert discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agent-dfx-swiss"
    listed = ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": MODERN}})["result"]
    assert listed["resultType"] == "complete"
    assert listed["tools"] == connector.TOOLS
    message = call(3, "run", {"args": ["--version"]})
    message["params"]["_meta"] = MODERN
    result = ask(message)
    assert result["result"]["resultType"] == "complete"
    assert tool_result(result)["version"]
    old = {**MODERN, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}
    error = ask({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"_meta": old}})["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": [*connector.MODERN, *connector.LEGACY], "requested": "1900-01-01"}
    bare = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    assert (
        ask({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {"_meta": bare}})["error"]["code"] == -32602
    )
    # `_meta` without a version (or not an object) is the legacy era
    for meta in ({"progressToken": 1}, "x"):
        legacy = ask({"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {"_meta": meta}})["result"]
        assert "resultType" not in legacy


def test_tool_call_errors() -> None:
    for params in (
        {"name": "shell", "arguments": {}},
        {"name": ["run"], "arguments": {}},
        {"name": "run", "arguments": ["guide"]},
    ):
        reply = ask({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        assert reply["error"]["code"] == -32602, params
    assert ask({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}})["error"]["message"] == (
        "Unknown tool: x"
    )
    # arguments that do not fit the input schema are a result of the tool, the error object of the command line
    for name, arguments in (
        ("search", {}),
        ("search", {"query": 1}),
        ("search", {"query": "x", "more": 1}),
        ("run", {}),
        ("run", {"args": "guide"}),
        ("run", {"args": ["guide", 1]}),
        ("run", {"args": ["guide"], "stdin": 1}),
        ("run", {"args": ["guide"], "shell": "ls"}),
        ("run", {"args": ["mcp"]}),
        ("run", {"args": ["install-connector", "--print"]}),
        ("run", {"args": ["claude", "watch", "--interval", "5"]}),
    ):
        reply = ask(call(1, name, arguments))
        assert tool_result(reply) == BAD, (name, arguments)
        assert reply["result"]["isError"] is True
    assert tool_result(ask({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search"}})) == BAD


def test_tool_results_are_the_ones_of_the_command_line(tool: Tool, monkeypatch: pytest.MonkeyPatch) -> None:
    found = tool_result(ask(call(1, "search", {"query": "claude desktop"}), tool.env))
    assert found["ok"] is True
    assert [hit["command"] for hit in found["results"][:2]] == ["mcp", "install-connector"]
    assert tool_result(ask(call(2, "run", {"args": ["claude", "--help"]}), tool.env))["help"].startswith("usage:")
    assert tool_result(ask(call(3, "run", {"args": ["status"], "stdin": "ignored"}), tool.env))["error"] == (
        "not_paired"
    )
    unpaired = tool.run("status")[1]
    assert tool_result(ask(call(4, "run", {"args": ["status"]}), tool.env)) == unpaired

    def boom(*args: object) -> object:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "execute", boom)
    failed = ask(call(6, "run", {"args": ["guide"]}), tool.env)
    assert tool_result(failed) == {"ok": False, "error": "internal", "next": errors.NEXT["internal"]}
    assert "secret" not in json.dumps(failed)


# --- install-connector ---


def test_install_connector_writes_and_merges(tool: Tool, tmp_path: Path) -> None:
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    code, printed = tool.run("install-connector", "--config", str(config), "--print")
    assert code == 0
    assert not config.exists()
    entry = printed["entry"]
    assert printed == {
        "ok": True,
        "config": str(config),
        "server": "agent-dfx-swiss",
        "written": False,
        "entry": entry,
        "next": "restart Claude Desktop",
    }
    assert entry["args"][-1] == "mcp"
    assert Path(entry["command"]).is_absolute()
    assert entry["env"] == {"AGENT_PLATFORM_HOME": tool.env["AGENT_PLATFORM_HOME"]}
    code, written = tool.run("install-connector", "--config", str(config))
    assert (code, written) == (0, {**printed, "written": True})
    assert json.loads(config.read_text()) == {"mcpServers": {"agent-dfx-swiss": entry}}
    # other servers and settings stay, the own entry is replaced
    other = {"command": "npx", "args": ["-y", "server"]}
    config.write_text(json.dumps({"globalShortcut": "x", "mcpServers": {"other": other, "agent-dfx-swiss": {}}}))
    assert tool.run("install-connector", "--config", str(config))[1]["written"] is True
    assert json.loads(config.read_text()) == {
        "globalShortcut": "x",
        "mcpServers": {"other": other, "agent-dfx-swiss": entry},
    }
    config.write_text(json.dumps({"globalShortcut": "x"}))
    tool.run("install-connector", "--config", str(config))
    assert json.loads(config.read_text()) == {"globalShortcut": "x", "mcpServers": {"agent-dfx-swiss": entry}}
    assert not config.with_name(config.name + ".tmp").exists()
    # the file keeps its mode, and a link to it stays a link
    config.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(config)
    assert tool.run("install-connector", "--config", str(link))[1]["written"] is True
    assert link.is_symlink()
    assert config.stat().st_mode & 0o777 == 0o600


def test_install_connector_refuses_a_broken_file(tool: Tool, tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    for text in ("{broken", "[1]", json.dumps({"mcpServers": []}), ""):
        config.write_text(text)
        code, document = tool.run("install-connector", "--config", str(config))
        assert (code, document["error"], document["reason"]) == (1, "invalid_argument", "config_invalid"), text
        assert config.read_text() == text
    code, document = tool.run("install-connector", "--config", str(tmp_path))
    assert document["reason"] == "config_invalid"


def test_desktop_config_paths_and_the_default(tool: Tool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    name = "claude_desktop_config.json"
    assert connector.desktop_config({}, "darwin") == tmp_path / "Library" / "Application Support" / "Claude" / name
    assert connector.desktop_config({"APPDATA": "/r"}, "win32") == Path("/r") / "Claude" / name
    assert connector.desktop_config({}, "win32") == tmp_path / "AppData" / "Roaming" / "Claude" / name
    assert connector.desktop_config({}, "linux") == tmp_path / ".config" / "Claude" / name
    printed = tool.run("install-connector", "--print")[1]
    assert printed["config"] == str(connector.desktop_config(tool.env, sys.platform))


def test_server_entry_names_the_running_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = tmp_path / "bin" / "agent-platform"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(program), "install-connector"])
    assert connector.server_entry({}) == {"command": str(program.resolve()), "args": ["mcp"]}
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "agent_platform" / "__main__.py")])
    assert connector.server_entry({"AGENT_PLATFORM_HOME": ""}) == {
        "command": sys.executable,
        "args": ["-m", "agent_platform", "mcp"],
    }
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "bin" / "agent-platform-missing")])
    assert connector.server_entry({})["command"] == sys.executable
