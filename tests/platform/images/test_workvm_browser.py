"""U-RUN-browser helper: the platform browser helper `workvm-browser` of the work image
(docs/platform/spec/13-work-vm.md section 13.3, ADR-085 section 5; decision KOR-44 point 5 pins the snapshot format
here).

The helper runs with Node and the Playwright of the web client (`web/node_modules`, the browsers of
`PLAYWRIGHT_BROWSERS_PATH`) against a local HTTP server on 127.0.0.1: state directory and socket come from the test
overrides `WORKVM_BROWSER_DIR` and `WORKVM_BROWSER_SOCKET`, `WORKVM_BROWSER_START=direct` starts the daemon without
systemd. Without Node or Playwright the browser tests are skipped, unless `WORKVM_BROWSER_TEST_REQUIRED=1` says the
machine must run them (then they fail). The static checks of the image wiring always run."""

from __future__ import annotations

import base64
import contextlib
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "images" / "workvm" / "files" / "usr" / "local" / "bin" / "workvm-browser"
PROVISION = REPO_ROOT / "images" / "workvm" / "provision.sh"
NODE_MODULES = REPO_ROOT / "web" / "node_modules"
REQUIRED_ENV = "WORKVM_BROWSER_TEST_REQUIRED"
CHROMIUM_PROBE = "process.exit(require('fs').existsSync(require('playwright').chromium.executablePath()) ? 0 : 3)"
ANSWER_KEYS = {
    "ok",
    "error",
    "notice",
    "url",
    "title",
    "tab",
    "tabs",
    "snapshot",
    "masked",
    "result",
    "image",
    "file",
}
DOWNLOAD = b"report bytes \x00\x01\x02 end\n"
PAGES: dict[str, tuple[str, bytes]] = {
    "/": (
        "text/html",
        b"""<!doctype html><html><head><title>Start</title></head><body>
<h1>Welcome</h1>
<p>Plain text block <b>with bold</b> part.</p>
<a href="/signin">Sign in</a>
<a href="/report.bin">Report</a>
<a href="/second" target="_blank">Open second</a>
<label for="color">Color</label>
<select id="color"><option value="r">Red</option><option value="g">Green</option></select>
<button onclick="alert('hello there')">Alert me</button>
<iframe src="/frame" title="Inner"></iframe>
<div style="height:3000px"></div><p id="bottom">Bottom line</p>
</body></html>""",
    ),
    "/frame": ("text/html", b"<!doctype html><body><button>Frame button</button></body>"),
    "/signin": (
        "text/html",
        b"""<!doctype html><html><head><title>Sign in</title></head><body>
<form action="/welcome" method="get">
<label for="user">User</label><input id="user" name="user">
<label for="pw">Password</label><input id="pw" name="pw" type="password">
<button type="submit">Continue</button>
</form></body></html>""",
    ),
    "/second": ("text/html", b"<!doctype html><title>Second</title><p>Second page</p>"),
    "/report.bin": ("application/octet-stream", DOWNLOAD),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/welcome":
            user = self.path.split("user=", 1)[1].split("&", 1)[0] if "user=" in self.path else ""
            body = f"<!doctype html><title>Welcome</title><p>Signed in as {user}</p>".encode()
            content_type = "text/html"
        elif path in PAGES:
            content_type, body = PAGES[path]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if path == "/report.bin":
            self.send_header("Content-Disposition", 'attachment; filename="report.bin"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def test_the_image_installs_the_helper_and_runs_its_selftest() -> None:
    text = PROVISION.read_text(encoding="utf-8")
    assert 'install -m 0755 "$FILES/usr/local/bin/workvm-browser" /usr/local/bin/workvm-browser' in text
    assert 'install -m 0755 "$FILES/usr/local/bin/workvm-browse" /usr/local/bin/workvm-browse' in text
    assert "/usr/local/bin/workvm-browser selftest" in text
    assert text.index("workvm-browser selftest") < text.index('touch "$GUARD"')
    assert "rm -rf /var/lib/workvm-browser" in text


def test_the_helper_keeps_the_production_defaults() -> None:
    source = HELPER.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env node\n")
    assert os.access(HELPER, os.X_OK)
    assert "process.env.WORKVM_BROWSER_DIR || '/var/lib/workvm-browser'" in source
    assert "process.env.WORKVM_BROWSER_SOCKET || '/run/workvm-browser.sock'" in source
    assert "`--unit=${UNIT}`, '--collect'" in source
    assert "const UNIT = 'workvm-browser';" in source
    assert "const CALL_MS = 25000;" in source
    # ADR-101: with Xvfb Chromium draws into the display :99 (a person can take it over by VNC), else headless
    assert "headless: screen === null" in source
    assert "const DISPLAY_NAME = ':99';" in source
    assert "const XVFB = '/usr/bin/Xvfb';" in source
    assert "process.env.WORKVM_BROWSER_DISPLAY === 'none'" in source
    assert "'--no-sandbox'" in source
    assert "acceptDownloads: true" in source


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(f"{REQUIRED_ENV}=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def node_env() -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        _unavailable("Node is not installed")
    if not (NODE_MODULES / "playwright" / "package.json").is_file():
        _unavailable("web/node_modules has no Playwright (npm ci in web/)")
    env = dict(os.environ, NODE_PATH=str(NODE_MODULES))
    probe = subprocess.run(
        [
            str(node),
            "-e",
            "const fs=require('fs');process.exit(fs.existsSync(require('playwright').chromium.executablePath())?0:3)",
        ],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        _unavailable("the Chromium of Playwright is not installed (npx playwright install chromium)")
    return env


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class Helper:
    def __init__(self, env: dict[str, str], state: Path, socket: Path) -> None:
        self.state = state
        self.socket = socket
        self.env = dict(
            env,
            WORKVM_BROWSER_DIR=str(state),
            WORKVM_BROWSER_SOCKET=str(socket),
            WORKVM_BROWSER_START="direct",
        )

    def run(self, *args: str, stdin: bytes = b"", timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["node", str(HELPER), *args], input=stdin, env=self.env, capture_output=True, timeout=timeout, check=False
        )

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        result = self.run("call", stdin=json.dumps({"tool": tool, "arguments": arguments}).encode())
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        lines = result.stdout.decode().splitlines()
        assert len(lines) == 1
        answer: dict[str, Any] = json.loads(lines[0])
        assert set(answer) == ANSWER_KEYS
        return answer

    def stop(self) -> None:
        pid_file = self.state / "serve.pid"
        if pid_file.is_file():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)


@pytest.fixture(scope="module")
def helper(node_env: dict[str, str]) -> Iterator[Helper]:
    # A short socket path: macOS limits Unix socket paths to 104 bytes.
    base = Path(tempfile.mkdtemp(prefix="wb-", dir="/tmp"))
    instance = Helper(node_env, base / "state", base / "b.sock")
    try:
        yield instance
    finally:
        instance.stop()
        shutil.rmtree(base, ignore_errors=True)


def ref(snapshot: str, role: str, name: str) -> str:
    match = re.search(rf'^\s*- {role} "{re.escape(name)}" \[ref=((?:f[0-9]+)?e[0-9]+)\]', snapshot, re.MULTILINE)
    assert match is not None, snapshot
    return match.group(1)


def check_image(answer: dict[str, Any]) -> None:
    image = answer["image"]
    assert set(image) == {"width", "height", "jpeg"}
    data = base64.b64decode(image["jpeg"], validate=True)
    assert data[:2] == b"\xff\xd8"
    assert len(data) <= 100 * 1024
    assert 0 < image["width"] <= 960
    assert image["height"] > 0


def test_selftest_starts_the_daemon_with_a_private_socket(helper: Helper) -> None:
    result = helper.run("selftest")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b'{"ok":true}\n'
    assert helper.socket.stat().st_mode & 0o777 == 0o600


def test_navigate_answers_the_page_with_snapshot_refs_and_image(helper: Helper, site: str) -> None:
    answer = helper.call("browser_navigate", url=f"{site}/")
    assert (answer["ok"], answer["error"], answer["masked"], answer["result"], answer["file"]) == (
        True,
        None,
        False,
        None,
        None,
    )
    assert (answer["url"], answer["title"]) == (f"{site}/", "Start")
    assert answer["tab"] == 0
    assert answer["tabs"][answer["tab"]]["active"] is True
    snapshot = answer["snapshot"]
    assert '- heading "Welcome" [level=1]' in snapshot
    assert '- text "Plain text block with bold part."' in snapshot
    assert re.search(r'- link "Sign in" \[ref=e[0-9]+\] href=' + re.escape(f"{site}/signin"), snapshot)
    assert re.search(r'- combobox "Color" \[ref=e[0-9]+\] value="Red"', snapshot)
    assert '  - option "Green"' in snapshot
    assert re.search(r'- iframe "Inner" \[frame=f1\]\n  - button "Frame button" \[ref=f1e1\]', snapshot)
    check_image(answer)
    without = helper.call("browser_scroll", dy=300, snapshot=False)
    assert (without["ok"], without["snapshot"]) == (True, "")


def test_select_and_click_in_a_frame_by_ref(helper: Helper, site: str) -> None:
    page = helper.call("browser_navigate", url=f"{site}/")
    chosen = helper.call("browser_select", target=ref(page["snapshot"], "combobox", "Color"), values=["Green"])
    assert chosen["ok"] is True
    assert re.search(r'- combobox "Color" \[ref=e[0-9]+\] value="Green"', chosen["snapshot"])
    clicked = helper.call("browser_click", target=ref(chosen["snapshot"], "button", "Frame button"))
    assert clicked["ok"] is True, clicked["error"]


def test_sign_in_masks_the_password_and_submits(helper: Helper, site: str) -> None:
    start = helper.call("browser_navigate", url=f"{site}/")
    signin = helper.call("browser_click", target=ref(start["snapshot"], "link", "Sign in"))
    assert (signin["ok"], signin["title"]) == (True, "Sign in")
    user = helper.call("browser_type", target=ref(signin["snapshot"], "textbox", "User"), text="alice")
    assert (user["ok"], user["masked"]) == (True, False)
    assert re.search(r'- textbox "User" \[ref=e[0-9]+\] value="alice"', user["snapshot"])
    password = helper.call("browser_type", target=ref(user["snapshot"], "textbox", "Password"), text="s3cret")
    assert (password["ok"], password["masked"]) == (True, True)
    assert re.search(r'- textbox "Password" \[ref=e[0-9]+\] type=password value="••••"', password["snapshot"])
    assert "s3cret" not in json.dumps(password)
    submitted = helper.call(
        "browser_type", target=ref(password["snapshot"], "textbox", "User"), text="bob", submit=True
    )
    assert (submitted["ok"], submitted["masked"], submitted["title"]) == (True, False, "Welcome")
    assert "Signed in as bob" in submitted["snapshot"]


def test_a_dialog_is_accepted_and_reported(helper: Helper, site: str) -> None:
    page = helper.call("browser_navigate", url=f"{site}/")
    answer = helper.call("browser_click", target=ref(page["snapshot"], "button", "Alert me"))
    assert answer["ok"] is True
    assert answer["notice"] == 'The alert dialog "hello there" was accepted.'
    assert helper.call("browser_dialog", action="dismiss")["ok"] is True
    dismissed = helper.call("browser_click", target="text=Alert me")
    assert dismissed["notice"] == 'The alert dialog "hello there" was dismissed.'
    assert helper.call("browser_dialog", action="accept")["ok"] is True


def test_evaluate_hover_wait_and_press(helper: Helper, site: str) -> None:
    helper.call("browser_navigate", url=f"{site}/")
    evaluated = helper.call("browser_evaluate", expression="document.title + ':' + location.pathname")
    assert (evaluated["ok"], evaluated["result"]) == (True, '"Start:/"')
    assert helper.call("browser_hover", target="h1")["ok"] is True
    waited = helper.call("browser_wait", text="Bottom line", url="127.0.0.1", ms=10, timeout_ms=2000)
    assert waited["ok"] is True
    assert helper.call("browser_press", key="End")["ok"] is True
    assert helper.call("browser_scroll", target="#bottom")["ok"] is True


def test_tabs_new_select_close_and_popup(helper: Helper, site: str) -> None:
    page = helper.call("browser_navigate", url=f"{site}/")
    popup = helper.call("browser_click", target=ref(page["snapshot"], "link", "Open second"))
    assert popup["ok"] is True
    assert "A new tab opened" in popup["notice"]
    assert popup["title"] == "Second"
    count = len(popup["tabs"])
    opened = helper.call("browser_tabs", action="new", url=f"{site}/signin")
    assert (len(opened["tabs"]), opened["tab"], opened["title"]) == (count + 1, count, "Sign in")
    selected = helper.call("browser_tabs", action="select", index=0)
    assert (selected["tab"], selected["tabs"][0]["active"]) == (0, True)
    closed = helper.call("browser_tabs", action="close", index=count)
    assert len(closed["tabs"]) == count
    helper.call("browser_tabs", action="close", index=count - 1)
    listed = helper.call("browser_tabs", action="list")
    assert listed["ok"] is True
    missing = helper.call("browser_tabs", action="select", index=40)
    assert (missing["ok"], missing["error"]) == (False, "There is no tab 40.")


def test_download_by_target_and_by_url(helper: Helper, site: str) -> None:
    page = helper.call("browser_navigate", url=f"{site}/")
    by_target = helper.call("browser_download", target=ref(page["snapshot"], "link", "Report"))
    assert by_target["ok"] is True, by_target["error"]
    file = by_target["file"]
    assert (file["name"], file["bytes"]) == ("report.bin", len(DOWNLOAD))
    assert Path(file["path"]).read_bytes() == DOWNLOAD
    assert Path(file["path"]).parent.parent == helper.state / "downloads"
    by_url = helper.call("browser_download", url=f"{site}/report.bin")
    assert by_url["ok"] is True, by_url["error"]
    assert Path(by_url["file"]["path"]).read_bytes() == DOWNLOAD
    assert by_url["file"]["path"] != file["path"]


def test_screenshot_writes_a_png(helper: Helper, site: str) -> None:
    helper.call("browser_navigate", url=f"{site}/")
    answer = helper.call("browser_screenshot", full_page=True)
    file = answer["file"]
    assert answer["ok"] is True
    data = Path(file["path"]).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert (file["bytes"], Path(file["path"]).parent) == (len(data), helper.state / "screenshots")


def test_upload_sets_the_files_of_an_input(helper: Helper, site: str, tmp_path: Path) -> None:
    upload = tmp_path / "note.txt"
    upload.write_text("hello", encoding="utf-8")
    helper.call("browser_navigate", url=f"{site}/")
    helper.call(
        "browser_evaluate",
        expression="(() => { const i = document.createElement('input'); i.type = 'file'; i.id = 'up';"
        " document.body.prepend(i); return true; })()",
    )
    answer = helper.call("browser_upload", target="#up", files=[str(upload)])
    assert answer["ok"] is True, answer["error"]
    assert 'type=file value="note.txt"' in answer["snapshot"]


def test_a_failed_action_is_ok_false_with_the_current_page(helper: Helper, site: str) -> None:
    helper.call("browser_navigate", url=f"{site}/")
    stale = helper.call("browser_click", target="e999")
    assert stale["ok"] is False
    assert stale["error"].startswith("Unknown ref e999")
    assert (stale["title"], stale["url"]) == ("Start", f"{site}/")
    assert "Welcome" in stale["snapshot"]
    check_image(stale)
    missing = helper.call("browser_click", target="#nothing-here", snapshot=False)
    assert missing["ok"] is False
    assert len(missing["error"]) <= 2048
    unknown = helper.call("browser_fly")
    assert (unknown["ok"], unknown["error"]) == (False, 'Unknown tool "browser_fly".')
    broken = helper.run("call", stdin=b"not json")
    assert json.loads(broken.stdout)["ok"] is False
    thrown = helper.call("browser_evaluate", expression="(() => { throw new Error('boom') })()")
    assert thrown["ok"] is False
    assert "boom" in thrown["error"]


def test_stream_prints_header_lines_and_jpeg_frames(helper: Helper, site: str) -> None:
    helper.call("browser_navigate", url=f"{site}/")
    process = subprocess.Popen(
        ["node", str(HELPER), "stream", "5", "60", "640"],
        env=helper.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdin is not None
        header = json.loads(process.stdout.readline())
        assert set(header) == {"bytes", "width", "height"}
        frame = process.stdout.read(header["bytes"])
        assert frame[:2] == b"\xff\xd8"
        assert len(frame) == header["bytes"]
        assert 0 < header["width"] <= 640
        assert 0 < header["height"] <= 400
        # a page at rest still shows: a picture again after the keyframe time without any paint
        second = json.loads(process.stdout.readline())
        assert process.stdout.read(second["bytes"])[:2] == b"\xff\xd8"
        assert 0 < second["width"] <= 640
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert helper.run("stream", "0", "60", "640").returncode == 2
