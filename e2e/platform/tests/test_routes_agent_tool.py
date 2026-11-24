"""Control routes of the user's agent tool (E2E-49/50): start, poll and the local Claude Code login, against a shell
stand-in for the Python of the agent checkout (`-m agent_platform <command>`)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.node import Node
from support import CORE, write_exec

# $1 $2 = -m agent_platform; $3 = the command of the stand-in.
AGENT_TOOL = r"""#!/bin/sh
shift 2
case "$1" in
  pair)
    "$AGENT_PLATFORM_BROWSER" "http://127.0.0.1:9/pair#one"
    "$AGENT_PLATFORM_BROWSER" "http://127.0.0.1:9/pair#two"
    input="$(cat)"
    echo "waiting for the browser"
    echo
    printf '{"args":"%s","stdin":"%s","home":"%s","state":"%s","claude":"%s","path":"%s"}\n' \
      "$*" "$input" "$HOME" "$AGENT_PLATFORM_HOME" "$CLAUDE_CONFIG_DIR" "$PYTHONPATH"
    ;;
  wait)
    while [ ! -f "$HOME/go" ]; do sleep 0.01; done
    exit 3
    ;;
  device)
    printf '{"admin":"%s","root":"%s","keys":"%s","other":"%s"}\n' \
      "$AGENT_PLATFORM_DEVICE_ADMIN" "$AGENT_PLATFORM_DEVICE_ROOT" "$AGENT_PLATFORM_DEVICE_KEYS" "$OTHER"
    ;;
  keychain)
    "$AGENT_PLATFORM_SECURITY" find-generic-password -s "Claude Code-credentials" -w || exit $?
    ;;
  *)
    echo "not json"
    echo "agent-platform: broken" >&2
    exit 1
    ;;
esac
"""

ROUTES = discover()


class NoNode(Node):
    """The agent tool runs as a local process of the orchestrator; no node is used."""

    def __init__(self) -> None:
        self.name = "none"

    def call(self, op: str, /, **args: Any) -> Any:
        raise AssertionError(f"unexpected node call {op}")


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    python = write_exec(tmp_path / "agent" / "python", AGENT_TOOL)
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    return Context(config, NoNode(), NoNode(), str(python), agent_src=str(tmp_path / "agent"), scratch=str(tmp_path))


def call(ctx: Context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return dispatch(ROUTES, ctx, method, path, body)


def start(ctx: Context, body: Any) -> str:
    status, answer = call(ctx, "POST", "/agent-tool", body)
    assert status == 200, answer
    run_id: str = answer["id"]
    return run_id


def finished(ctx: Context, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, answer = call(ctx, "GET", f"/agent-tool/{run_id}")
        assert status == 200
        if answer["done"]:
            return dict(answer)
        time.sleep(0.02)
    raise AssertionError(f"agent-tool run {run_id} did not end")


def test_a_run_gets_its_own_computer_and_answers_the_last_json_line(ctx: Context, tmp_path: Path) -> None:
    run_id = start(ctx, {"args": ["pair", "--hub", "x"], "stdin": "secret-input"})
    answer = finished(ctx, run_id)
    root = tmp_path / "agent-tool" / "default"
    assert answer["rc"] == 0
    assert answer["result"] == {
        "args": "pair --hub x",
        "stdin": "secret-input",
        "home": str(root / "home"),
        "state": str(root / "state" / "agent-platform"),
        "claude": str(root / "claude"),
        "path": str(tmp_path / "agent" / "src"),
    }
    assert answer["stdout_lines"] == 3
    assert answer["stderr_tail"] == ""
    assert answer["opened"] == ["http://127.0.0.1:9/pair#one", "http://127.0.0.1:9/pair#two"]
    assert (root / "state").stat().st_mode & 0o777 == 0o700
    # A second run on the same computer keeps its stand-ins; it answers only the URLs it opened itself (the earlier
    # run's links stay recorded but never show up as this run's); without stdin it reads nothing.
    (root / "tools" / "browser").write_text('#!/bin/sh\necho "kept:$1" >> "$(dirname "$0")/opened.txt"\n')
    again = finished(ctx, start(ctx, {"args": ["pair"], "home": "default"}))
    assert again["result"]["stdin"] == ""
    assert again["opened"] == ["kept:http://127.0.0.1:9/pair#one", "kept:http://127.0.0.1:9/pair#two"]
    assert len((root / "tools" / "opened.txt").read_text().split()) == 4


def test_a_running_tool_is_not_done_and_an_empty_output_has_no_result(ctx: Context, tmp_path: Path) -> None:
    run_id = start(ctx, {"args": ["wait"], "home": "alice"})
    status, answer = call(ctx, "GET", f"/agent-tool/{run_id}")
    assert status == 200
    assert answer == {"done": False, "rc": None, "result": None, "stdout_lines": 0, "stderr_tail": "", "opened": []}
    (tmp_path / "agent-tool" / "alice" / "home" / "go").touch()
    answer = finished(ctx, run_id)
    assert (answer["rc"], answer["result"], answer["opened"]) == (3, None, [])


def test_output_that_is_not_json_is_handed_back_unparsed(ctx: Context) -> None:
    answer = finished(ctx, start(ctx, {"args": ["broken"]}))
    assert answer["rc"] == 1
    assert answer["result"] == {"unparsed": "not json\n"}
    assert answer["stderr_tail"] == "agent-platform: broken\n"


def test_the_claude_login_is_the_keychain_item_of_that_computer(ctx: Context, tmp_path: Path) -> None:
    missing = finished(ctx, start(ctx, {"args": ["keychain"], "home": "bob"}))
    assert missing["rc"] == 44
    assert "could not be found in the keychain" in missing["stderr_tail"]
    login = {"home": "bob", "access_token": "access-1", "expires_at_ms": 1_800_000_000_000, "refresh_token": "canary"}
    assert call(ctx, "POST", "/agent-tool/claude-login", login) == (200, {"home": "bob"})
    found = finished(ctx, start(ctx, {"args": ["keychain"], "home": "bob"}))
    assert found["rc"] == 0
    assert found["result"] == {
        "claudeAiOauth": {
            "accessToken": "access-1",
            "refreshToken": "canary",
            "expiresAt": 1_800_000_000_000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
    }
    item = json.loads((tmp_path / "agent-tool" / "bob" / "tools" / "keychain.json").read_text())
    assert item == found["result"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (None, "body must be"),
        ({"args": []}, "body must be"),
        ({"args": "pair"}, "body must be"),
        ({"args": ["pair", 1]}, "body must be"),
        ({"args": ["pair"], "stdin": 5}, "stdin must be text"),
        ({"args": ["pair"], "home": "../elsewhere"}, "home must be a simple name"),
        ({"args": ["pair"], "home": 7}, "home must be a simple name"),
    ],
)
def test_start_refuses_bad_bodies(ctx: Context, body: Any, detail: str) -> None:
    status, answer = call(ctx, "POST", "/agent-tool", body)
    assert (status, answer["error"]) == (400, "invalid")
    assert detail in answer["detail"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ([], "body must be an object"),
        ({"access_token": "a", "expires_at_ms": 1}, "body must be"),
        ({"access_token": "a", "expires_at_ms": "1", "refresh_token": "r"}, "body must be"),
        ({"access_token": 1, "expires_at_ms": 1, "refresh_token": "r"}, "body must be"),
        ({"access_token": "a", "expires_at_ms": 1, "refresh_token": "r", "home": "a b"}, "simple name"),
    ],
)
def test_claude_login_refuses_bad_bodies(ctx: Context, body: Any, detail: str) -> None:
    status, answer = call(ctx, "POST", "/agent-tool/claude-login", body)
    assert (status, answer["error"]) == (400, "invalid")
    assert detail in answer["detail"]


def test_copy_home_gives_a_new_computer_the_same_agent_state(ctx: Context, tmp_path: Path) -> None:
    finished(ctx, start(ctx, {"args": ["pair"], "home": "ada"}))
    root = tmp_path / "agent-tool"
    state = root / "ada" / "state" / "agent-platform"
    state.mkdir(mode=0o700)
    (state / "agent.key").write_text("key")
    (state / "agent.key").chmod(0o600)
    login = {"home": "ada", "access_token": "a", "expires_at_ms": 1, "refresh_token": "r"}
    assert call(ctx, "POST", "/agent-tool/claude-login", login)[0] == 200
    assert call(ctx, "POST", "/agent-tool/copy-home", {"from": "ada", "to": "ada_copy"}) == (200, {"home": "ada_copy"})
    copied = root / "ada_copy" / "state" / "agent-platform" / "agent.key"
    assert copied.read_text() == "key"
    assert copied.stat().st_mode & 0o777 == 0o600
    assert copied.parent.stat().st_mode & 0o777 == 0o700
    assert (root / "ada_copy" / "state").stat().st_mode & 0o777 == 0o700
    keychain = (root / "ada" / "tools" / "keychain.json").read_text()
    assert (root / "ada_copy" / "tools" / "keychain.json").read_text() == keychain
    # the copy runs as its own computer; without a keychain item at the source none is copied
    answer = finished(ctx, start(ctx, {"args": ["pair"], "home": "ada_copy"}))
    assert answer["result"]["state"] == str(root / "ada_copy" / "state" / "agent-platform")
    assert call(ctx, "POST", "/agent-tool/copy-home", {"from": "ada_copy", "to": "bare"})[0] == 200
    (root / "bob" / "state").mkdir(parents=True)
    assert call(ctx, "POST", "/agent-tool/copy-home", {"from": "bob", "to": "bob_copy"})[0] == 200
    assert not (root / "bob_copy" / "tools" / "keychain.json").exists()


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ([], 400, "invalid"),
        ({"from": "ada"}, 400, "invalid"),
        ({"from": "ada", "to": "../x"}, 400, "invalid"),
        ({"from": 1, "to": "x"}, 400, "invalid"),
        ({"from": "nobody", "to": "x"}, 404, "not_found"),
        ({"from": "ada", "to": "ada"}, 409, "exists"),
    ],
)
def test_copy_home_refuses_bad_bodies(ctx: Context, tmp_path: Path, body: Any, status: int, error: str) -> None:
    (tmp_path / "agent-tool" / "ada" / "state").mkdir(parents=True)
    answer = call(ctx, "POST", "/agent-tool/copy-home", body)
    assert (answer[0], answer[1]["error"]) == (status, error)


def test_an_own_device_file_gives_the_computer_the_device_seam(ctx: Context, tmp_path: Path) -> None:
    # without own-device.json (E2E-56, own_device.py) the tool runs without a seam
    assert finished(ctx, start(ctx, {"args": ["device"], "home": "mac"}))["result"] == {
        "admin": "",
        "root": "",
        "keys": "",
        "other": "",
    }
    extra = {"AGENT_PLATFORM_DEVICE_ADMIN": "/seam", "AGENT_PLATFORM_DEVICE_ROOT": 7, "OTHER": "ignored"}
    (tmp_path / "agent-tool" / "mac" / "own-device.json").write_text(json.dumps(extra))
    assert finished(ctx, start(ctx, {"args": ["device"], "home": "mac"}))["result"] == {
        "admin": "/seam",
        "root": "7",
        "keys": "",
        "other": "",
    }


def test_an_unknown_run_is_not_found(ctx: Context) -> None:
    status, answer = call(ctx, "GET", "/agent-tool/no-such-run")
    assert (status, answer["error"]) == (404, "not_found")


def test_state_answers_the_stored_state_and_the_private_keys(ctx: Context, tmp_path: Path) -> None:
    state = tmp_path / "agent-tool" / "ada" / "state" / "agent-platform"
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"agent_id": "a", "v": 1}))
    assert call(ctx, "POST", "/agent-tool/state", {"home": "ada"}) == (
        200,
        {"state": {"agent_id": "a", "v": 1}, "key": None},
    )
    (state / "agent.key").write_text("private-keys\n")
    assert call(ctx, "POST", "/agent-tool/state", {"home": "ada"})[1]["key"] == "private-keys"


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ([], 400, "invalid"),
        ({}, 400, "invalid"),
        ({"home": "../x"}, 400, "invalid"),
        ({"home": "nobody"}, 404, "not_found"),
    ],
)
def test_state_refuses_bad_bodies_and_unknown_computers(ctx: Context, body: Any, status: int, error: str) -> None:
    answer = call(ctx, "POST", "/agent-tool/state", body)
    assert (answer[0], answer[1]["error"]) == (status, error)
