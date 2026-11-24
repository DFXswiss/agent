"""The user's agent tool `agent-platform` in E2E-49/50 (11-agent-access.md, ADR-056).

- `POST /agent-tool` (`{"args": [...], "stdin": "<text>"?, "home": "<name>"?}`): start `agent-platform <args>` with the
  Python of the agent checkout on this machine (T1: browser, hub and tool run on the same computer, so the browser
  can hand over to the tool's loopback listener). Each `home` (default `default`) is one user's computer: its own
  `AGENT_PLATFORM_HOME`, `HOME`, `CLAUDE_CONFIG_DIR`, a browser stand-in (`AGENT_PLATFORM_BROWSER`, a script that
  appends the opened URL to a file instead of opening a window: the Playwright page opens it) and a `security`
  stand-in (`AGENT_PLATFORM_SECURITY`) that answers like macOS for the item `Claude Code-credentials` from a file the
  spec writes. Answers `{"id"}`.
- `GET /agent-tool/{id}`: `{"done", "rc", "result", "stderr_tail", "opened": [urls]}`; `result` is the parsed JSON
  object the tool printed (None until it ended); `opened` are the URLs this run handed to the browser stand-in (an
  earlier run on the same computer, e.g. of another spec, never leaves a stale link there).
- `POST /agent-tool/claude-login` (`{"home", "access_token", "expires_at_ms", "refresh_token"}`): the local Claude Code
  login of that computer as Claude Code stores it (keychain item for macOS-like homes). The refresh token is a canary:
  specs assert it never reaches the hub, the page or any output.
- `POST /agent-tool/copy-home` (`{"from", "to"}`): a copy of one computer's agent state (its state directory and the
  keychain stand-in, modes kept) on a new computer `to` that must not exist yet: a second copy of a paired agent that
  still holds the old key after the first one revoked itself (E2E-49: the hub answers `key_revoked`).
- `POST /agent-tool/state` (`{"home"}`): what an attacker holding that computer's agent state has: `state.json` as
  stored and the private keys of `agent.key` (null without the file). E2E-63 opens the wraps of a revoked agent's copy
  with them and signs log entries as the agent.
No real keychain, profile or browser of the machine is touched.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

_BROWSER = '#!/bin/sh\nprintf "%s\\n" "$1" >> "$(dirname "$0")/opened.txt"\n'
_SECURITY = (
    "#!/bin/sh\n"
    "# find-generic-password -s 'Claude Code-credentials' -w: print the stored item, else exit 44 like macOS\n"
    'for arg in "$@"; do last="$arg"; done\n'
    'item="$(dirname "$0")/keychain.json"\n'
    'if [ "$1" = "find-generic-password" ] && [ -f "$item" ]; then cat "$item"; exit 0; fi\n'
    "echo 'security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.' >&2\n"
    "exit 44\n"
)
_ids = itertools.count(1)
_lock = threading.Lock()


@dataclass
class Run:
    process: subprocess.Popen[str]
    home: Path
    stdout: str = ""
    stderr: str = ""
    collected: bool = field(default=False)
    opened_from: int = 0


_runs: dict[str, Run] = {}


def _home(ctx: Context, name: object) -> Path:
    if name is None:
        name = "default"
    if not isinstance(name, str) or not name.isidentifier():
        raise RouteError(400, "invalid", "home must be a simple name")
    root = Path(ctx.scratch or "/tmp") / "agent-tool" / name
    tools = root / "tools"
    for path in (root / "home", root / "claude", root / "state", tools):
        path.mkdir(parents=True, exist_ok=True)
    os.chmod(root / "state", 0o700)
    for name_, text in (("browser", _BROWSER), ("security", _SECURITY)):
        script = tools / name_
        if not script.exists():
            script.write_text(text)
            script.chmod(0o755)
    return root


def _env(ctx: Context, root: Path) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "HOME": str(root / "home"),
        "AGENT_PLATFORM_HOME": str(root / "state" / "agent-platform"),
        "AGENT_PLATFORM_BROWSER": str(root / "tools" / "browser"),
        "AGENT_PLATFORM_SECURITY": str(root / "tools" / "security"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "PYTHONPATH": str(Path(ctx.agent_src) / "src"),
    }
    # E2E-56 (own_device.py): the stand-in of the administrator step of `device add|remove` on this computer.
    own = root / "own-device.json"
    if own.exists():
        extra = json.loads(own.read_text())
        env.update({key: str(value) for key, value in extra.items() if key.startswith("AGENT_PLATFORM_DEVICE_")})
    return env


def _opened(root: Path) -> list[str]:
    """Every URL the browser stand-in of the computer `root` recorded, oldest first."""
    opened_file = root / "tools" / "opened.txt"
    return opened_file.read_text().split() if opened_file.exists() else []


def post_start(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    args = body.get("args") if isinstance(body, dict) else None
    stdin = body.get("stdin") if isinstance(body, dict) else None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args) or not args:
        raise RouteError(400, "invalid", 'body must be {"args": ["<command>", ...]}')
    if stdin is not None and not isinstance(stdin, str):
        raise RouteError(400, "invalid", "stdin must be text")
    root = _home(ctx, body.get("home"))
    opened_from = len(_opened(root))
    process = subprocess.Popen(
        [ctx.agent_python, "-m", "agent_platform", *args],
        env=_env(ctx, root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    run = Run(process=process, home=root, opened_from=opened_from)
    run_id = str(next(_ids))
    with _lock:
        _runs[run_id] = run

    def feed() -> None:
        out, err = process.communicate(stdin or "")
        run.stdout, run.stderr, run.collected = out, err, True

    threading.Thread(target=feed, daemon=True).start()
    return 200, {"id": run_id}


def get_run(_ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    with _lock:
        run = _runs.get(params["id"])
    if run is None:
        raise RouteError(404, "not_found", f"no agent-tool run {params['id']}")
    opened = _opened(run.home)[run.opened_from :]
    result = None
    if run.collected:
        lines = [line for line in run.stdout.splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else None
        except ValueError:
            result = {"unparsed": run.stdout[-500:]}
    return 200, {
        "done": run.collected,
        "rc": run.process.returncode if run.collected else None,
        "result": result,
        "stdout_lines": len(run.stdout.splitlines()),
        "stderr_tail": run.stderr[-500:],
        "opened": opened,
    }


def post_claude_login(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict):
        raise RouteError(400, "invalid", "body must be an object")
    token, expires, refresh = body.get("access_token"), body.get("expires_at_ms"), body.get("refresh_token")
    if not isinstance(token, str) or not isinstance(refresh, str) or not isinstance(expires, int):
        raise RouteError(400, "invalid", 'body must be {"access_token", "expires_at_ms", "refresh_token"}')
    root = _home(ctx, body.get("home"))
    item = {
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": refresh,
            "expiresAt": expires,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
    }
    (root / "tools" / "keychain.json").write_text(json.dumps(item))
    return 200, {"home": str(root.name)}


def post_copy_home(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict):
        raise RouteError(400, "invalid", "body must be an object")
    source, target = body.get("from"), body.get("to")
    for name in (source, target):
        if not isinstance(name, str) or not name.isidentifier():
            raise RouteError(400, "invalid", "from and to must be simple names")
    root = Path(ctx.scratch or "/tmp") / "agent-tool"
    if not (root / str(source)).is_dir():
        raise RouteError(404, "not_found", f"no agent-tool computer {source}")
    if (root / str(target)).exists():
        raise RouteError(409, "exists", f"agent-tool computer {target} exists")
    copy = _home(ctx, target)
    shutil.copytree(root / str(source) / "state", copy / "state", symlinks=True, dirs_exist_ok=True)
    keychain = root / str(source) / "tools" / "keychain.json"
    if keychain.exists():
        shutil.copy2(keychain, copy / "tools" / "keychain.json")
    return 200, {"home": str(target)}


def post_state(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    name = body.get("home") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name.isidentifier():
        raise RouteError(400, "invalid", 'body must be {"home": "<simple name>"}')
    state = Path(ctx.scratch or "/tmp") / "agent-tool" / name / "state" / "agent-platform"
    if not (state / "state.json").is_file():
        raise RouteError(404, "not_found", f"no agent state on computer {name}")
    key = state / "agent.key"
    return 200, {
        "state": json.loads((state / "state.json").read_text()),
        "key": key.read_text().strip() if key.is_file() else None,
    }


ROUTES = [
    Route("POST", "/agent-tool", post_start),
    Route("POST", "/agent-tool/claude-login", post_claude_login),
    Route("POST", "/agent-tool/copy-home", post_copy_home),
    Route("POST", "/agent-tool/state", post_state),
    Route("GET", "/agent-tool/{id}", get_run),
]
