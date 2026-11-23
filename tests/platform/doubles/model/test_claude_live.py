"""Real Claude Code against the model double (marker ``live``: needs a local ``claude`` binary; never in platform-ci).

Runs the Spike C scenario ``e2e-tools`` with the pinned Claude Code version: two turns (Write, Read, Edit, parallel
Grep+Glob, two MCP tools, a scripted 529 that Claude Code must retry, then a resumed turn with a thinking block).

Isolation rules (DBL-01): a fresh ``CLAUDE_CONFIG_DIR`` under the checkout (``build/claude-live/<id>``, removed
afterwards unless ``DBL_KEEP_LIVE_DIR`` is set), ``HOME`` on a temporary directory, a dummy ``ANTHROPIC_API_KEY``, an
allow-list environment (nothing of the calling session leaks in), a dead proxy so any request that is not for the
double fails at once, and non-essential traffic switched off. No existing Claude Code profile and no keychain login is
ever used. The result (checks, double state, endpoint differences) is written to ``result.json`` in the run directory.

    pytest -c tests/platform/pytest.ini -m live tests/platform/doubles/model/test_claude_live.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.model.access import Access
from agent_doubles.model.journal import summarize_endpoints, unknown_names
from agent_doubles.model.scenario import packaged_scenario
from agent_doubles.model.server import ModelDouble

pytestmark = pytest.mark.live

PINNED_VERSION = "2.1.330"
TOKEN = "double-test-key"  # dummy credential: the double only compares it
FIXTURES = Path(__file__).resolve().with_name("fixtures")
REPO = Path(__file__).resolve().parents[4]
KEEP_ENV = ("PATH", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME", "SHELL")
QUIET_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
}
DEAD_PROXY = {
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "127.0.0.1,localhost",
}
TOOLS = "Read,Write,Edit,Grep,Glob"
MCP_TOOLS = "mcp__spike__add,mcp__spike__task_context,mcp__spike__fail"
PROMPT_1 = "SPIKE-E2E-1: create notes.txt, change beta to gamma, search, then use the MCP tools."
PROMPT_2 = "SPIKE-E2E-2: what did you do in the first turn?"


@dataclass
class Run:
    returncode: int
    seconds: float
    events: list[dict[str, Any]]
    stderr: str

    @property
    def result(self) -> dict[str, Any]:
        return next((e for e in reversed(self.events) if e.get("type") == "result"), {})


@pytest.fixture
def run_dir() -> Iterator[Path]:
    root = REPO / "build" / "claude-live" / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    yield root
    if not os.environ.get("DBL_KEEP_LIVE_DIR"):
        shutil.rmtree(root)


def claude_binary() -> str:
    found = shutil.which("claude")
    if found is None:
        pytest.fail("the live test needs a local claude binary on PATH")
    version = subprocess.run([found, "--version"], capture_output=True, text=True, check=True, timeout=60).stdout
    if not version.startswith(PINNED_VERSION):
        pytest.fail(f"claude {version.strip()} is not the pinned {PINNED_VERSION}")
    return found


def run_claude(binary: str, root: Path, env: dict[str, str], prompt: str, resume: str | None = None) -> Run:
    mcp = root / "mcp.json"
    server: dict[str, Any] = {
        "command": sys.executable,
        "args": ["-I", str(FIXTURES / "mcp_stdio_server.py")],
        "env": {"MCP_SPIKE_LOG": str(root / "mcp.log")},
    }
    mcp.write_text(json.dumps({"mcpServers": {"spike": server}}))
    argv = [binary, "--print", "--verbose", "--output-format", "stream-json", "--include-partial-messages"]
    argv += ["--model", "double-model", "--restricted", "--disable-slash-commands", "--tools", TOOLS]
    argv += [
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp),
    ]
    argv += ["--allowedTools", MCP_TOOLS]
    if resume:
        argv += ["--resume", resume]
    started = time.monotonic()
    proc = subprocess.run(
        argv, input=prompt, capture_output=True, text=True, cwd=root / "ws", env=env, timeout=300, check=False
    )
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    return Run(proc.returncode, time.monotonic() - started, events, proc.stderr)


def test_claude_code_runs_the_e2e_tools_scenario(run_dir: Path) -> None:
    binary = claude_binary()
    (run_dir / "ws").mkdir()
    (run_dir / "claude-config").mkdir()
    with tempfile.TemporaryDirectory(prefix="dbl01-home-") as home:
        double = ModelDouble(
            packaged_scenario("e2e-tools"), access=Access("api_key", TOKEN), journal_path=run_dir / "journal.jsonl"
        ).start()
        try:
            env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
            env.update(QUIET_ENV)
            env.update(DEAD_PROXY)
            env.update(
                HOME=home,
                TMPDIR=home,
                CLAUDE_CONFIG_DIR=str(run_dir / "claude-config"),
                ANTHROPIC_BASE_URL=double.url,
                ANTHROPIC_API_KEY=TOKEN,
            )
            first = run_claude(binary, run_dir, env, PROMPT_1)
            session = first.result.get("session_id")
            second = run_claude(binary, run_dir, env, PROMPT_2, resume=session)
        finally:
            double.stop()
    state = double.state()
    notes = run_dir / "ws" / "notes.txt"
    mcp_log = run_dir / "mcp.log"
    mcp_calls = sorted(
        json.loads(line)["params"]["name"]
        for line in (mcp_log.read_text().splitlines() if mcp_log.exists() else [])
        if json.loads(line)["method"] == "tools/call"
    )
    steps = [e.get("step") for e in double.exchanges]
    summary = summarize_endpoints(double.exchanges)
    recorded = json.loads((FIXTURES / "double-endpoints.json").read_text())
    checks = {
        "turn1_exit_0": first.returncode == 0 and not first.result.get("is_error"),
        "turn2_exit_0": second.returncode == 0 and not second.result.get("is_error"),
        "double_ok": state["ok"],
        "file_content": notes.exists() and notes.read_text() == "alpha\ngamma\n",
        "mcp_calls": mcp_calls == ["add", "task_context"],
        "retry_529": "overloaded" in state["consumed"] and steps.count("final") == 1,
        "turn1_text": "SPIKE-E2E-1 done" in str(first.result.get("result")),
        "turn2_text": "SPIKE-E2E-2 remembered" in str(second.result.get("result")),
        "no_unknown_names": not unknown_names(summary, recorded),
        "dummy_key_redacted": TOKEN not in (run_dir / "journal.jsonl").read_text(),
    }
    report = {
        "claude_version": PINNED_VERSION,
        "checks": checks,
        "double_state": state,
        "seconds": [round(first.seconds, 1), round(second.seconds, 1)],
        "unknown_names": unknown_names(summary, recorded),
        "routes": {key: entry["count"] for key, entry in summary.items()},
        "stderr_tail": [first.stderr[-400:], second.stderr[-400:]],
    }
    (run_dir / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    assert all(checks.values()), report
