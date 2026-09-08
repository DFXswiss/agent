import json
from pathlib import Path

import pytest

from agent_cli.ai_accounts import AIAccount, AIRole
from agent_cli.lane_protocol import ProtocolError
from agent_cli.lane_text import TextCLI, file_hash, isolated_env
from agent_cli.runtime import Completed


def test_text_process_bridge_cannot_execute_ambient_python_startup(tmp_path, monkeypatch):
    import sys
    import time
    sentinel = tmp_path / "unexpected-startup"
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").write_text('executed')\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_AMBIENT_SENTINEL", "must-not-be-inherited")
    cli = object.__new__(TextCLI)
    cli.binary = Path(sys.executable).resolve()
    cli.sha256 = file_hash(cli.binary)
    cli.cwd = tmp_path
    cli.deadline = time.monotonic() + 10
    cli.env = {"PATH": "/usr/bin:/bin"}
    result = cli._run([str(cli.binary), "-I", "-S", "-c",
                      "import os; print(os.environ.get('AGENT_AMBIENT_SENTINEL', 'isolated'))"])
    assert result.stdout.strip() == "isolated"
    assert not sentinel.exists()


def role(path):
    return AIRole("review", AIAccount("chosen", "grok", str(path)), "chosen-model", "read-only")


def test_child_environment_does_not_copy_credentials_or_extension_settings(tmp_path, monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "XAI_API_KEY", "GROK_AGENT", "GROK_SUBAGENTS",
                "GROK_CLAUDE_MCPS_ENABLED", "PYTHONPATH", "NODE_OPTIONS", "HTTP_PROXY"):
        monkeypatch.setenv(key, "ambient")
    env = isolated_env(tmp_path / "profile", tmp_path / "home")
    assert "ambient" not in env.values()
    assert env["GROK_SUBAGENTS"] == "0"
    assert env["GROK_CLAUDE_MCPS_ENABLED"] == "0"
    assert env["HOME"] == str(tmp_path / "home")
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "XAI_API_KEY", "PYTHONPATH", "NODE_OPTIONS"} & env.keys()


def test_binary_hash_mismatch_and_launcher_are_rejected_before_execution(tmp_path):
    binary = tmp_path / "cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(ProtocolError, match="hash"):
        TextCLI(role(tmp_path), binary=str(binary), sha256="0" * 64, timeout=10)
    with pytest.raises(ProtocolError, match="native"):
        TextCLI(role(tmp_path), binary=str(binary), sha256=file_hash(binary), timeout=10)


def test_grok_structured_cli_envelope_is_unwrapped_without_metadata(tmp_path):
    cli = object.__new__(TextCLI)
    cli.role, cli.root, cli.args = role(tmp_path), tmp_path, ["native-grok"]
    message = {"request": {"action": "list", "prefix": "", "offset": 0}}
    envelope = {"text": json.dumps(message), "structuredOutput": message, "stopReason": "end_turn",
                "num_turns": 1, "thought": "private provider metadata", "usage": {"output_tokens": 10}}
    cli._run = lambda *_: Completed(0, json.dumps(envelope), "")
    result = cli.complete("task")
    assert json.loads(result) == message
    assert "metadata" not in result and "output_tokens" not in result


def test_grok_prompt_serialization_roundtrips_without_host_mention_delimiter(tmp_path):
    cli = object.__new__(TextCLI)
    cli.role, cli.root, cli.args = role(tmp_path), tmp_path, ["native-grok"]
    message = {"request": {"action": "finish", "text": "done"}}
    envelope = {"text": json.dumps(message), "structuredOutput": message, "stopReason": "end_turn", "num_turns": 1}
    cli._run = lambda *_: Completed(0, json.dumps(envelope), "")
    prompt = 'source @/host/private and email@example.org\\n literal \\u0040 plus unicode ä'
    cli.complete(prompt)
    wire = (tmp_path / "request.txt").read_text()
    assert "@" not in wire
    assert json.loads(wire.split("\n", 1)[1]) == prompt


@pytest.mark.parametrize("field,value", [("stopReason", "max_turns"), ("num_turns", True), ("num_turns", 2),
                                        ("structuredOutput", None), ("text", '{}')])
def test_incomplete_or_inconsistent_cli_output_is_not_work(tmp_path, field, value):
    cli = object.__new__(TextCLI)
    cli.role, cli.root, cli.args = role(tmp_path), tmp_path, ["native-grok"]
    message = {"request": {"action": "finish", "text": "done"}}
    envelope = {"text": json.dumps(message), "structuredOutput": message, "stopReason": "end_turn", "num_turns": 1}
    envelope[field] = value
    cli._run = lambda *_: Completed(0, json.dumps(envelope), "")
    with pytest.raises(ProtocolError):
        cli.complete("task")


def test_pinned_runtime_is_unconfigured_unless_explicit(tmp_path):
    from agent_cli.ai_accounts import load_ai_accounts, AccountError
    base = {"accounts": {"chosen": {"provider": "grok", "config_dir": "/explicit/profile"}}}
    config = tmp_path / "ai-accounts.json"
    config.write_text(json.dumps(base))
    assert load_ai_accounts(tmp_path).accounts["chosen"].lane_runtime is None
    base["accounts"]["chosen"]["lane_runtime"] = None
    config.write_text(json.dumps(base))
    assert load_ai_accounts(tmp_path).accounts["chosen"].lane_runtime is None
    for runtime in ({}, {"binary": "relative", "sha256": "0" * 64},
                    {"binary": "/explicit/native", "sha256": "bad"},
                    {"binary": "/explicit/native", "sha256": "0" * 64, "fallback": True}):
        base["accounts"]["chosen"]["lane_runtime"] = runtime
        config.write_text(json.dumps(base))
        with pytest.raises(AccountError):
            load_ai_accounts(tmp_path)
    base["accounts"]["chosen"]["lane_runtime"] = {"binary": "/explicit/native", "sha256": "1" * 64}
    config.write_text(json.dumps(base))
    assert load_ai_accounts(tmp_path).accounts["chosen"].lane_runtime.binary == "/explicit/native"
