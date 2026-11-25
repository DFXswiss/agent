"""The command line, the state directory and the closed error list (11-agent-access.md sections 8.1 and 8.4):
one JSON object per run, no tracebacks, state checks, revocation, the shipped guide."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_platform import __version__, cli, errors, state
from agent_platform.__main__ import main
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.keys import Identity

from .conftest import Tool, make_tool, pair_user
from .hubfake import HubFake, User

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "platform" / "spec" / "11-agent-access.md"


def test_error_codes_are_the_closed_list_of_the_spec() -> None:
    text = SPEC.read_text(encoding="utf-8")
    listed = text.split("Codes (closed list in `agent_platform/errors.py`):", 1)[1].split(".\n", 1)[0]
    assert set(re.findall(r"`([a-z_]+)`", listed)) == errors.CODES
    with pytest.raises(ValueError, match="unknown error code"):
        PlatformError("nonsense")


def test_guide_is_a_byte_copy() -> None:
    shipped = REPO_ROOT / "src" / "agent_platform" / "guide.md"
    assert shipped.read_bytes() == (REPO_ROOT / "docs" / "platform" / "agents.md").read_bytes()


def test_guide_version_help(tool: Tool) -> None:
    assert (
        tool.run("guide", "--topic", "all")[1]["guide"] == (REPO_ROOT / "docs" / "platform" / "agents.md").read_text()
    )
    assert tool.run("--version")[1] == {"ok": True, "version": __version__}
    assert "usage: agent-platform" in tool.run()[1]["help"]
    assert "usage: agent-platform org invite" in tool.run("org", "invite", "--help")[1]["help"]
    assert tool.run("org")[1]["error"] == "invalid_argument"
    assert tool.run("--bogus")[1]["error"] == "invalid_argument"


def test_unexpected_failure_is_internal_without_traceback(tool: Tool, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object) -> object:
        raise RuntimeError("secret detail")

    monkeypatch.setattr("agent_platform.lookup.resources.files", boom)
    code, document = tool.run("guide")
    assert (code, document) == (1, {"ok": False, "error": "internal", "next": errors.NEXT["internal"]})


def test_interrupt(tool: Tool, monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(*args: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "execute", stop)
    assert tool.run("guide")[1]["reason"] == "interrupted"


def test_default_streams(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_PLATFORM_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.run(["status"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_paired"
    assert main(["--version"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == __version__


def test_console_script_and_module(tmp_path: Path) -> None:
    script = shutil.which("agent-platform") or str(Path(sys.executable).with_name("agent-platform"))
    env = {**os.environ, "AGENT_PLATFORM_HOME": str(tmp_path / "h")}
    for argv in ([script, "status"], [sys.executable, "-m", "agent_platform", "status"]):
        done = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
        assert done.returncode == 1
        assert json.loads(done.stdout)["error"] == "not_paired"
        assert done.stderr == ""


# --- state directory ---


def test_state_directory_checks(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    state_file = tool.home / "state.json"
    state_file.chmod(0o644)
    assert tool.run("status")[1]["error"] == "state_damaged"
    state_file.chmod(0o600)
    tool.home.chmod(0o755)
    assert tool.run("status")[1]["error"] == "state_damaged"
    tool.home.chmod(0o700)
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    assert tool.run("status")[1]["error"] == "state_damaged"
    monkeypatch.undo()
    target = tool.root / "elsewhere.json"
    target.write_text(state_file.read_text())
    target.chmod(0o600)
    state_file.unlink()
    state_file.symlink_to(target)
    assert tool.run("status")[1]["error"] == "state_damaged"


def test_home_is_a_symlink(tool: Tool, hub: HubFake, account: User) -> None:
    real = tool.root / "real-home"
    tool.home.rename(real)
    tool.home.symlink_to(real)
    assert tool.run("status")[1]["error"] == "state_damaged"


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"v": 2}), json.dumps([1]), json.dumps({"v": 1}), json.dumps({"v": 1, "agent_id": 1})],
)
def test_damaged_state_file(tool: Tool, hub: HubFake, account: User, content: str) -> None:
    (tool.home / "state.json").write_text(content)
    assert tool.run("status")[1]["error"] == "state_damaged"


def test_keys_must_be_present(tool: Tool, hub: HubFake, account: User) -> None:
    data = json.loads((tool.home / "state.json").read_text())
    for item in tool.keychain.glob("agent-platform__*"):
        item.unlink()
    refused = tool.run("status")[1]
    assert (refused["error"], refused["reason"]) == ("state_damaged", "keychain_unreadable")
    assert "security unlock-keychain" in refused["next"]
    for where in ("file", "other"):
        (tool.home / "state.json").write_text(json.dumps({**data, "key_store": where}))
        assert tool.run("status")[1]["error"] == "state_damaged"
    (tool.home / "agent.key").write_text("garbage")
    (tool.home / "agent.key").chmod(0o600)
    (tool.home / "state.json").write_text(json.dumps({**data, "key_store": "file"}))
    assert tool.run("status")[1]["error"] == "state_damaged"


def test_home_and_keychain_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert state.home_dir({}) == tmp_path / ".agent-platform"
    assert state.security_program({}) == state.DEFAULT_SECURITY
    assert state.keychain_available(str(tmp_path / "missing"), "darwin") is False
    assert state.keychain_available(sys.executable, "linux") is False
    home = Home(tmp_path / "h", "/missing", False)
    home.ensure_dir()
    home._write(home.key_file.with_name("agent.key.tmp"), "stale")
    assert home.store_keys("a", Identity.generate()) == state.FILE
    home.delete(None, state.KEYCHAIN)
    assert not home.key_file.exists()
    home.delete("a", state.FILE)


def test_over_ssh_the_keys_go_to_the_file_and_the_output_says_where(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rule E-27 (KOR-67): over ssh the login keychain is locked or cannot ask, so the keys go to `agent.key`; every
    command that names the connected agent prints where its keys are."""
    shown = tool.run("status")[1]
    on_mac = sys.platform == "darwin"
    assert shown["keys"] == ("login keychain, item agent-platform" if on_mac else str(tool.home / "agent.key"))
    assert tool.run("status", env={"SSH_CONNECTION": "192.0.2.1 50000 192.0.2.2 22"})[1]["keys"] == shown["keys"]
    for name in state.SSH_VARIABLES:
        remote = Home.from_env({**tool.env, name: "x"})
        assert remote.keychain is False
        assert remote.store_keys("a", Identity.generate()) == state.FILE
        assert remote.keys_at(state.FILE) == str(remote.key_file)
    assert Home.from_env(tool.env).keychain is on_mac
    assert Home.from_env(tool.env).keys_at("other") is None
    assert cli.keys_of({**tool.env, "AGENT_PLATFORM_HOME": str(tmp_path / "none")}, shown["agent_id"]) == {}
    assert cli.keys_of(tool.env, "someone-else") == {}

    def hangs(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("security", state.SECURITY_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", hangs)
    assert state.run_security("/usr/bin/security", ["-i"], "x").returncode == 124


# --- revocation and transport ---


def test_revoked_agent_deletes_its_state(tool: Tool, hub: HubFake, account: User) -> None:
    agent_id = json.loads((tool.home / "state.json").read_text())["agent_id"]
    hub.revoke(agent_id)
    code, document = tool.run("status")
    assert (code, document["error"]) == (1, "agent_revoked")
    assert "Pair again" in document["next"]
    assert not (tool.home / "state.json").exists()
    assert not (tool.keychain / f"agent-platform__{agent_id}").exists()
    assert tool.run("status")[1]["error"] == "not_paired"


def test_disabled_user_is_revoked(tool: Tool, hub: HubFake, account: User) -> None:
    account.disabled = True
    assert tool.run("org", "list")[1]["error"] == "agent_revoked"


def test_unknown_agent(tool: Tool, hub: HubFake, account: User) -> None:
    hub.agents.clear()
    document = tool.run("status")[1]
    assert (document["error"], document["hub_error"]) == ("not_paired", "key_unknown")
    assert (tool.home / "state.json").exists()


def test_unsigned_answers_are_refused(tool: Tool, hub: HubFake, account: User) -> None:
    hub.sign_answers = False
    document = tool.run("status")[1]
    assert (document["error"], document["reason"]) == ("hub_refused", "response_unsigned")


def test_hub_answer_signed_by_another_key(tool: Tool, hub: HubFake, account: User) -> None:
    hub.hub_key = Ed25519PrivateKey.generate()
    assert tool.run("status")[1]["reason"] == "response_signature_invalid"


def test_hub_unreachable(tool: Tool, hub: HubFake, account: User) -> None:
    hub.stop()
    assert tool.run("status")[1]["error"] == "hub_unreachable"


def test_requests_are_signed_with_the_agent_key(tool: Tool, hub: HubFake, account: User) -> None:
    tool.run("status")
    agent_id = json.loads((tool.home / "state.json").read_text())["agent_id"]
    assert {request.agent for request in hub.requests if request.user is not None} == {agent_id}


def test_second_computer_pairs_independently(tool: Tool, hub: HubFake, tmp_path: Path) -> None:
    user = hub.add_user()
    pair_user(tool, hub, user)
    other = make_tool(tmp_path / "second")
    pair_user(other, hub, user)
    assert tool.run("status")[1]["agent_id"] != other.run("status")[1]["agent_id"]
