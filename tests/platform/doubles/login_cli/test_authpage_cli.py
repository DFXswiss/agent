"""U-DBL-login: ``python -m agent_doubles.login_cli.authpage`` (in process and as a real subprocess)."""

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
from agent_doubles.login_cli.authpage import main
from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.testca import DoubleCA

from .conftest import SRC, USER, Browser


def test_main_serves_pages_with_a_shared_ca(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ca_dir = tmp_path / "ca"
    with MailDouble(ca_dir=ca_dir) as mail:
        mail.add_account(USER, "pw")
        info = tmp_path / "auth.json"
        seen: dict[str, Any] = {}

        def wait(stop: threading.Event) -> None:
            urls = json.loads(info.read_text())
            browser = Browser(DoubleCA.open_or_create(ca_dir))
            page = browser.open(urls["authorize"] + "?x=1")
            seen["status"] = page.status
            control = DoubleClient(urls["control"])
            seen["seed"] = control.state()["scenario"]["seed"]
            control.scenario({"junk": True})
            page = browser.open(urls["pages"] + "/login", {"email": USER, "next": "/oauth/authorize?x"})
            seen["page"] = page.kind
            seen["junk"] = mail.store.counts()[USER]["Junk"]

        argv = ["--ca-dir", str(ca_dir), "--smtp", f"127.0.0.1:{mail.smtp.port}", "--junk", "--info-file", str(info)]
        # The scenario switch after the invalid authorize request clears its failure.
        assert main([*argv, "--seed", "cli"], wait=wait) == 0
    assert seen == {"status": 400, "seed": "cli", "page": "check-email", "junk": 1}
    out, err = capsys.readouterr()
    assert out.startswith("http://127.0.0.1:")
    state = json.loads(err)
    assert state["urls"]["ca_file"] == str(ca_dir / "ca.pem")


def test_main_with_another_host_and_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--host", "::1", "--ca-dir", str(tmp_path / "ca"), "--smtp", "[::1]:25"], wait=lambda s: None) == 0
    assert (tmp_path / "ca" / "names.txt").read_text().split() == ["localhost", "127.0.0.1", "::1"]
    assert main(["--host", "localhost", "--ca-dir", str(tmp_path / "ca2")], wait=lambda s: None) == 0
    assert main(["--host", "192.0.2.1", "--ca-dir", str(tmp_path / "ca3")]) == 2
    assert (tmp_path / "ca3" / "names.txt").read_text().split()[-1] == "192.0.2.1"
    capsys.readouterr()
    assert main(["--smtp", "nohost"]) == 2
    assert "--smtp needs HOST:PORT" in capsys.readouterr().err
    assert main(["--smtp", "h:x"]) == 2
    assert main(["--host", "0.0.0.0"]) == 2


def test_module_runs_as_a_process_until_sigterm(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    info = tmp_path / "auth.json"
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_doubles.login_cli.authpage", "--info-file", str(info), "--fault", "token"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        url = process.stdout.readline().strip()
        assert url == json.loads(info.read_text())["control"]
        assert DoubleClient(url).state()["scenario"]["fault"] == "token"
    finally:
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=20)
    assert process.returncode == 0, err
    assert json.loads(err)["urls"]["pages"].startswith("http://127.0.0.1:")
