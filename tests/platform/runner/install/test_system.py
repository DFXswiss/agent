"""U-RUN-install-system: what the installation helper takes from the Mac (ADR-074): programs run without a shell
(missing program 127, timeout 124), downloads over HTTPS or loopback HTTP with a size limit, and the defaults of
`System`."""

from __future__ import annotations

import os
import shutil
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_runner.install import system as system_module
from agent_runner.install.layout import Layout
from agent_runner.install.system import System

from .conftest import Kit, refused

BODY = b"x" * (system_module.FETCH_CHUNK + 10)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/missing":
            self.send_error(404)
            return
        self.server.agents.append(self.headers.get("User-Agent", ""))  # type: ignore[attr-defined]
        body = b"" if self.path == "/empty" else BODY
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[tuple[str, list[str]]]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    agents: list[str] = []
    httpd.agents = agents  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", agents
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_a_program_runs_with_its_output_captured() -> None:
    done = system_module.run_argv(["/bin/echo", "a b", "$HOME"])
    assert (done.returncode, done.stdout, done.stderr) == (0, "a b $HOME\n", "")
    assert done.args == ["/bin/echo", "a b", "$HOME"]
    failed = system_module.run_argv(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"])
    assert (failed.returncode, failed.stdout, failed.stderr) == (3, "out\n", "err\n")
    assert system_module.run_argv(["/usr/bin/true"]).returncode == 0


def test_a_program_reads_the_given_text_on_standard_input() -> None:
    done = system_module.run_with_input(["/bin/cat"], "secret\n")
    assert (done.returncode, done.stdout, done.stderr) == (0, "secret\n", "")
    assert done.args == ["/bin/cat"]
    failed = system_module.run_with_input(["/bin/sh", "-c", "read line; echo got $line >&2; exit 4"], "x\n")
    assert (failed.returncode, failed.stdout, failed.stderr) == (4, "", "got x\n")


def test_a_missing_program_is_exit_127(tmp_path: Path) -> None:
    done = system_module.run_argv([str(tmp_path / "absent"), "x"])
    assert (done.returncode, done.stdout, done.stderr) == (127, "", "not found")
    assert done.args == [str(tmp_path / "absent"), "x"]
    piped = system_module.run_with_input([str(tmp_path / "absent")], "x")
    assert (piped.returncode, piped.stdout, piped.stderr) == (127, "", "not found")
    assert piped.args == [str(tmp_path / "absent")]


def test_a_timeout_is_exit_124(monkeypatch: pytest.MonkeyPatch) -> None:
    done = system_module.run_argv(["/bin/sleep", "5"], timeout=0.05)
    assert (done.returncode, done.stdout, done.stderr) == (124, "", "timed out")
    assert done.args == ["/bin/sleep", "5"]
    monkeypatch.setattr(system_module, "RUN_TIMEOUT_S", 0.05)
    piped = system_module.run_with_input(["/bin/sleep", "5"], "")
    assert (piped.returncode, piped.stdout, piped.stderr) == (124, "", "timed out")
    assert piped.args == ["/bin/sleep", "5"]


def test_a_download_returns_the_body(server: tuple[str, list[str]]) -> None:
    url, agents = server
    assert system_module.fetch_url(f"{url}/file", len(BODY)) == BODY
    assert system_module.fetch_url(f"{url}/empty", 0) == b""
    assert agents == ["agent-runner-install", "agent-runner-install"]


def test_localhost_http_passes_the_scheme_check() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # accepted by the scheme check; nothing listens, so the download itself fails
    with refused("download_failed"):
        system_module.fetch_url(f"http://localhost:{port}/", 100)


def test_a_download_above_the_limit_is_refused(server: tuple[str, list[str]]) -> None:
    url, _ = server
    with refused("download_failed", "size"):
        system_module.fetch_url(f"{url}/file", len(BODY) - 1)


def test_a_failed_download_is_refused(server: tuple[str, list[str]]) -> None:
    url, _ = server
    with refused("download_failed"):
        system_module.fetch_url(f"{url}/missing", 100)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # nothing listens on the port any more: the connection is refused
    with refused("download_failed"):
        system_module.fetch_url(f"http://127.0.0.1:{port}/", 100)


@pytest.mark.parametrize(
    "url", ["http://example.com/x", "ftp://127.0.0.1:21/x", "file:///etc/passwd", "http://127.0.0.1/x", "x"]
)
def test_only_https_or_loopback_http_is_fetched(url: str) -> None:
    with refused("download_failed", "scheme"):
        system_module.fetch_url(url, 100)


def test_free_space_and_the_clock(tmp_path: Path) -> None:
    free = system_module.free_bytes(tmp_path)
    assert free > 0
    assert abs(free - shutil.disk_usage(tmp_path).free) < 1024**3
    now = system_module.utc_now()
    assert now.tzinfo is UTC
    assert abs(now - system_module.utc_now()) < timedelta(seconds=5)


def test_the_defaults_are_the_real_mac(tmp_path: Path) -> None:
    system = System()
    assert system.layout == Layout()
    assert system.layout.root == Path("/")
    assert system.run is system_module.run_argv
    assert system.run_input is system_module.run_with_input
    assert system.fetch is system_module.fetch_url
    assert dict(system.env) == dict(os.environ)
    assert system.env is not os.environ
    assert system.geteuid() == os.geteuid()
    assert system.root_uid == 0
    assert 0 <= system.local_hour() <= 23
    assert system.machine() != ""
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    # the default chown does not follow links (the own IDs: no privilege needed)
    system.chown(link, os.getuid(), os.getgid())
    assert link.lstat().st_uid == os.getuid()


def test_must_returns_stdout_or_raises_the_code(kit: Kit) -> None:
    kit.run.answer(["/bin/a"], stdout="out\n")
    kit.run.answer(["/bin/b"], rc=2, stdout="ignored")
    assert kit.system.must(["/bin/a", "x"], "a_failed") == "out\n"
    with refused("b_failed", "item"):
        kit.system.must(["/bin/b"], "b_failed", "item")
    with refused("b_failed"):
        kit.system.must(["/bin/b"], "b_failed")
    assert kit.run.calls == [["/bin/a", "x"], ["/bin/b"], ["/bin/b"]]
