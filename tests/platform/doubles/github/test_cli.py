"""U-DBL-github: the command line ``python -m agent_doubles.github`` (in process and as a real subprocess)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.github.__main__ import main

SEED = {
    "users": [{"login": "octo-user", "tokens": ["cli-token"]}],
    "repositories": [{"full_name": "octo-org/hello", "branches": [{"name": "main", "commits": [{}]}]}],
    "installations": [{"account": "octo-org", "repositories": ["octo-org/hello"], "users": ["octo-user"]}],
}
HEADERS = {
    "accept": "application/vnd.github+json",
    "x-github-api-version": "2022-11-28",
    "authorization": "Bearer cli-token",
}


def test_main_serves_the_seeded_world(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(SEED))
    faults = tmp_path / "faults.json"
    faults.write_text(json.dumps({"faults": [{"route": "GET /user", "kind": "error"}]}))
    port_file = tmp_path / "port"
    journal = tmp_path / "journal.jsonl"
    seen: dict[str, Any] = {}

    def wait(stop: threading.Event) -> None:
        client = DoubleClient(f"http://127.0.0.1:{port_file.read_text().strip()}")
        seen["first"] = client.request("GET", "/user", headers=HEADERS).status
        seen["repo"] = client.request("GET", "/repos/octo-org/hello", headers=HEADERS).json()["full_name"]

    argv = ["--seed", str(seed), "--seed", str(faults), "--port-file", str(port_file), "--journal", str(journal)]
    assert main(argv, wait=wait) == 0
    assert seen == {"first": 502, "repo": "octo-org/hello"}
    out, err = capsys.readouterr()
    assert out.startswith("http://127.0.0.1:")
    assert json.loads(err)["ok"] is True
    assert [json.loads(line)["route"] for line in journal.read_text().splitlines()] == [
        "GET /user",
        "GET /repos/{owner}/{repo}",
    ]


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--host", "0.0.0.0"]) == 2
    assert "invalid bind address" in capsys.readouterr().err
    assert main(["--seed", str(tmp_path / "missing.json")]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text('{"users": [{"login": "-x"}]}')
    assert main(["--seed", str(bad)]) == 2
    assert "invalid login" in capsys.readouterr().err
    armed = tmp_path / "armed.json"
    armed.write_text(json.dumps({"faults": [{"route": "GET /user", "kind": "error"}]}))
    argv = ["--seed", str(armed), "--public-url", "http://127.0.0.1:1", "--html-url", "https://github.example"]
    assert main(argv, wait=lambda stop: None) == 1
    assert json.loads(capsys.readouterr().err)["pending"] == ["fault error on GET /user fired 0 of 1 times"]


def test_module_runs_as_a_process_until_sigterm(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(SEED))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[4] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_doubles.github", "--seed", str(seed)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        url = process.stdout.readline().strip()
        client = DoubleClient(url)
        assert client.request("GET", "/user", headers=HEADERS).json()["login"] == "octo-user"
    finally:
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=10)
    assert process.returncode == 0
    assert json.loads(err)["requests"] == 1
