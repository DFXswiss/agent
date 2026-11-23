"""U-DBL-mail: the command line ``python -m agent_doubles.mail`` (in process and as a real subprocess)."""

from __future__ import annotations

import json
import os
import signal
import smtplib
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.mail.__main__ import main
from agent_doubles.mail.message import compose

SRC = Path(__file__).resolve().parents[4] / "src"


def test_main_serves_accounts_and_writes_the_info_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    info = tmp_path / "info.json"
    port_file = tmp_path / "port"
    journal = tmp_path / "journal.jsonl"
    seen: dict[str, Any] = {}

    def wait(stop: threading.Event) -> None:
        endpoints = json.loads(info.read_text())
        raw = compose("a@bulk.test", ["u@example.test"], "s", when=0, text="x")
        with smtplib.SMTP(endpoints["smtp"]["host"], endpoints["smtp"]["port"]) as client:
            client.sendmail("a@bulk.test", ["u@example.test"], raw)
        control = DoubleClient(f"http://127.0.0.1:{port_file.read_text().strip()}")
        seen["messages"] = control.state()["messages"]
        seen["ca"] = Path(endpoints["ca_file"]).is_file()

    argv = ["--account", "u@example.test:p:w", "--junk-sender", "a@bulk.test", "--info-file", str(info)]
    argv += ["--port-file", str(port_file), "--journal", str(journal), "--ca-dir", str(tmp_path / "ca")]
    assert main(argv, wait=wait) == 0
    assert seen == {"messages": {"u@example.test": {"INBOX": 0, "Junk": 1}}, "ca": True}
    out, err = capsys.readouterr()
    assert out.startswith("http://127.0.0.1:")
    assert json.loads(err)["ok"] is True
    assert [json.loads(line)["path"] for line in journal.read_text().splitlines()][-2:] == ["DATA", "QUIT"]


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--host", "0.0.0.0", "--no-tls"]) == 2
    assert "invalid bind address" in capsys.readouterr().err
    assert main(["--no-tls", "--account", "no-password"]) == 2
    assert "USER:PASSWORD" in capsys.readouterr().err
    assert main(["--no-tls", "--account", "u:"]) == 2
    assert "invalid password" in capsys.readouterr().err

    def unexpected(stop: threading.Event) -> None:
        DoubleClient(json.loads((tmp_path / "i.json").read_text())["control"]).request("GET", "/x")

    assert main(["--no-tls", "--info-file", str(tmp_path / "i.json")], wait=unexpected) == 1


def test_module_runs_as_a_process_until_sigterm(tmp_path: Path) -> None:
    info = tmp_path / "info.json"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_doubles.mail", "--account", "u@example.test:pw", "--info-file", str(info)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        url = process.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:")
        endpoints = json.loads(info.read_text())
        ca_dir = Path(endpoints["ca_file"]).parent
        assert ca_dir.is_dir()
    finally:
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=20)
    assert process.returncode == 0, err
    assert json.loads(err)["ok"] is True
    assert not ca_dir.exists()


def test_main_reads_an_accounts_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps([{"user": "a@example.test", "password": "pw", "junk_name": "Spam"}]))
    seen: dict[str, Any] = {}

    def wait(stop: threading.Event) -> None:
        control = DoubleClient(json.loads((tmp_path / "i.json").read_text())["control"])
        seen["accounts"] = control.request("GET", "/_double/accounts").json()

    argv = ["--no-tls", "--accounts-file", str(accounts), "--info-file", str(tmp_path / "i.json")]
    assert main(argv, wait=wait) == 0
    assert seen["accounts"] == [{"user": "a@example.test", "folders": ["INBOX", "Spam"], "junk": "Spam"}]
    capsys.readouterr()
    for content in ("{}", "not json", '[{"user": 1}]'):
        accounts.write_text(content)
        assert main(["--no-tls", "--accounts-file", str(accounts)]) == 2
    assert main(["--no-tls", "--accounts-file", str(tmp_path / "missing.json")]) == 2
    assert "--accounts-file must hold a JSON list" in capsys.readouterr().err
