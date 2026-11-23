"""Guest half of `probe_tui_start.py`: Claude Code TUI start with and without Landlock (PRB-01, WP-M0-14).

Runs as root inside a probe clone of the golden image (`python3 -E -s tui_guest.py RUNS TIMEOUT`; the host half
hands it in as a one-time Lima system provisioning step, because the Lima default user of a hardened clone may not
run anything as root). Every run creates a throw-away session of the sandbox self-test (`selftest.
ThrowawaySessions`, own user and directories, a pre-seeded `.claude.json` like a real session), builds the
session's command line with the real driver (`ClaudeCodeDriver.argv`, initial prompt on the command line) and starts
it in a detached tmux pane:

* `landlock` - exactly the start of a session: `systemd-run` with every unit property around the Landlock launcher
  (`unit.build`);
* `plain` - the same `systemd-run` and unit properties, but Claude Code started directly (no launcher, no Landlock).

The variants alternate (landlock first in odd runs) so that drift hits both. From the `tmux new-session` call that
runs `systemd-run` the run measures, polling every 10 ms: `paint_ms` (first TUI screen in the pane) and
`transcript_ms` (Claude Code created its transcript below `CLAUDE_CONFIG_DIR/projects`: the prompt was taken).
The throw-away session's token is unknown to the guest API, so hooks and model requests are refused (401); that
happens after both marks and is the same for both variants. Prints one JSON document on standard output.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, "/opt/agent-guest")

from agent_guest.driver import ClaudeCodeDriver, Launch
from agent_guest.sandbox import selftest, unit
from agent_guest.sessions import claude_json

PROMPT = "What is 2+2?"
POLL_S = 0.01
PAINTED = re.compile("(Claude Code|" + chr(0x256D) + "|" + chr(0x276F) + "|Welcome)")
VARIANTS = ("landlock", "plain")


def _seed(session_id: str, user: str) -> None:
    base = unit.session_dir(session_id)
    seeded = Path(f"{base}/home/.claude/.claude.json")
    seeded.write_text(claude_json(f"{base}/work"), encoding="utf-8")
    os.chmod(seeded, 0o600)
    uid = int(subprocess.run(["id", "-u", user], capture_output=True, text=True, check=True).stdout)
    os.chown(seeded, uid, uid)


def _transcript(session_id: str) -> bool:
    projects = Path(unit.session_dir(session_id)) / "home" / ".claude" / "projects"
    return projects.is_dir() and any(projects.glob("*/*.jsonl"))


def one_run(variant: str, version: str, timeout: float) -> dict[str, Any]:
    python, dynload = unit.interpreter_paths()
    paths = unit.load_exec_paths()
    sessions = selftest.ThrowawaySessions()
    name = "prb-tui"
    unit_name = unit.UNIT_PREFIX + sessions.own
    result: dict[str, Any] = {"variant": variant, "paint_ms": None, "transcript_ms": None}
    try:
        sessions.create()
        _seed(sessions.own, sessions.user)
        spec = unit.SessionSpec(
            session_id=sessions.own,
            token=secrets.token_urlsafe(32),
            python=python,
            lib_dynload=dynload,
            exec_paths=paths,
            kind="main",
        )
        launch = Launch(session_id=sessions.own, harness_file=unit.harness_file(sessions.own), initial_prompt=PROMPT)
        args = ClaudeCodeDriver(version).argv(launch)[1:]
        if variant == "landlock":
            argv = unit.build(spec, args)
        else:
            argv = unit.systemd_run(spec, [spec.claude, *args], unit_name=unit_name)
        start = time.monotonic()
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "160", "-y", "48", *argv], check=True)
        while time.monotonic() - start < timeout:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            if result["paint_ms"] is None:
                shot = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", name], capture_output=True, text=True, check=False
                )
                if PAINTED.search(shot.stdout):
                    result["paint_ms"] = elapsed
            if result["transcript_ms"] is None and _transcript(sessions.own):
                result["transcript_ms"] = elapsed
            if result["paint_ms"] is not None and result["transcript_ms"] is not None:
                break
            time.sleep(POLL_S)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)
        subprocess.run(["systemctl", "stop", unit_name], capture_output=True, check=False)
        sessions.remove()
    return result


def main(argv: list[str]) -> int:
    runs, timeout = int(argv[1]), float(argv[2])
    build = json.loads(Path(unit.BUILD_FILE).read_text(encoding="utf-8"))
    version = str(build["claude_version"])
    out: dict[str, Any] = {
        "claude_version": version,
        "landlock_abi": build.get("landlock_abi"),
        "kernel": os.uname().release,
        "runs": [],
    }
    for run in range(1, runs + 1):
        order = VARIANTS if run % 2 else tuple(reversed(VARIANTS))
        for variant in order:
            out["runs"].append({"run": run, **one_run(variant, version, timeout)})
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
