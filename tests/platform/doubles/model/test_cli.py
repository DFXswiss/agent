"""U-DBL-model: the command line ``python -m agent_doubles.model`` (in process and as a real subprocess)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.model.__main__ import main, parse_variables, resolve_scenario
from agent_doubles.model.scenario import ScenarioError

TOKEN_ENV = "DBL01_TEST_TOKEN"
TOKEN = "double-test-key"  # not a secret: the double only compares it
QUESTION: dict[str, Any] = {
    "model": "m",
    "max_tokens": 10,
    "stream": True,
    "tools": [{"name": "Read"}],
    "messages": [{"role": "user", "content": "What is 2+2?"}],
}


def test_resolve_and_variables(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"name": "file", "steps": [{"reply": {"content": [{"type": "text", "text": "x"}]}}]}')
    assert resolve_scenario(str(path)).name == "file"
    assert resolve_scenario("hello").name == "hello"
    assert parse_variables(["a=1", "b=x=y", "c="]) == {"a": "1", "b": "x=y", "c": ""}
    for bad in ("novalue", "=x"):
        with pytest.raises(ScenarioError, match="NAME=VALUE"):
            parse_variables([bad])


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--scenario", "missing-scenario"], "no packaged scenario"),
        (["--scenario", "hello", "--access", "api_key"], "go together"),
        (["--scenario", "hello", "--token-env", TOKEN_ENV], "go together"),
        (["--scenario", "hello", "--access", "api_key", "--token-env", "DBL01_UNSET_VARIABLE"], "must not be empty"),
        (["--scenario", "hello", "--key-token-env", "DBL01_UNSET_VARIABLE"], "must not be empty"),
        (["--scenario", "hello", "--var", "broken"], "NAME=VALUE"),
        (["--scenario", "hello", "--host", ""], "invalid bind address"),
    ],
)
def test_usage_errors(argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == 2
    assert message in capsys.readouterr().err


def test_main_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    port_file = tmp_path / "port"
    journal = tmp_path / "journal.jsonl"
    seen: dict[str, Any] = {}

    def drive(stop: threading.Event) -> None:
        client = DoubleClient(f"http://127.0.0.1:{port_file.read_text().strip()}")
        seen["refused"] = client.request("POST", "/v1/messages", QUESTION).status
        client.reset()
        seen["answer"] = client.request("POST", "/v1/messages", QUESTION, {"x-api-key": TOKEN}).status
        seen["state"] = client.state()
        seen["key"] = client.request("GET", "/v1/key", headers={"Authorization": f"Bearer {TOKEN}"}).status

    argv = ["--scenario", "hello", "--port-file", str(port_file), "--journal", str(journal)]
    access = ["--access", "api_key", "--token-env", TOKEN_ENV, "--key-token-env", TOKEN_ENV]
    code = main([*argv, *access, "--var", "x=1"], wait=drive)
    assert code == 0
    assert seen["refused"] == 401
    assert seen["answer"] == 200
    assert seen["state"]["ok"]
    assert seen["key"] == 200
    out = capsys.readouterr()
    assert out.out.startswith("http://127.0.0.1:")
    assert json.loads(out.err)["ok"]
    assert TOKEN not in journal.read_text()


def test_subprocess_exit_codes(tmp_path: Path) -> None:
    """The real process: binds to the given address, announces the URL, exits 1 unless the scenario ran fully."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    for drive, expected in ((False, 1), (True, 0)):
        port_file = tmp_path / f"port-{expected}"
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_doubles.model", "--scenario", "hello", "--port-file", str(port_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert process.stdout is not None
        url = process.stdout.readline().strip()
        assert url == f"http://127.0.0.1:{port_file.read_text().strip()}"
        if drive:
            assert DoubleClient(url).request("POST", "/v1/messages", QUESTION).status == 200
        time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=10)
        assert process.returncode == expected
        assert json.loads(err)["scenario"] == "hello"
