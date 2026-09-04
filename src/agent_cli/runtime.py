"""Local tmux process holder. argv lists only; never shell=True."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .grok_pane import grok_pane_is_working
from .store import StoreError

TARGETS_FILE = "runtime-targets.json"

GROK_DEFAULT_MODEL = "grok-4.6"
GROK_STRIP_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str


def tmux_name(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", session_id)[:50]
    if cleaned == "":
        raise StoreError("session id produces empty tmux name")
    return f"agent-{cleaned}"


def grok_new_session_id() -> str:
    """Grok `--session-id` rejects a ULID. Always mint a UUID."""
    return str(uuid.uuid4())


def grok_model(raw: str | None) -> str:
    if raw is None:
        return GROK_DEFAULT_MODEL
    stripped = raw.strip()
    if stripped == "":
        return GROK_DEFAULT_MODEL
    return stripped


def grok_launch_argv(*, existing: str, model: str, new_id: str) -> list[str]:
    resolved = grok_model(model)
    if existing:
        return ["grok", "--always-approve", "--resume", existing, "--model", resolved]
    if not _UUID_RE.match(new_id):
        raise SystemExit("grok --session-id requires a UUID")
    return [
        "grok",
        "--always-approve",
        "--session-id",
        new_id,
        "--model",
        resolved,
    ]


def grok_tmux_command_argv(*, existing: str, model: str, new_id: str) -> list[str]:
    """Prefix that drops Claude credentials from the pane environment."""
    argv: list[str] = ["env"]
    for key in GROK_STRIP_ENV:
        argv.extend(["-u", key])
    argv.extend(grok_launch_argv(existing=existing, model=model, new_id=new_id))
    return argv


def run_argv(argv: list[str]) -> Completed:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


_default_runner = run_argv


_KEY_MAP = {
    "enter": "Enter",
    "ctrl-c": "C-c",
    "tab": "Tab",
}


def load_tmux_targets(home: Path) -> dict[str, list[str]]:
    """Load home/runtime-targets.json. Missing file is {}. Invalid is StoreError."""
    path = home / TARGETS_FILE
    if not path.exists():
        return {}
    if not path.is_file():
        raise StoreError(f"{TARGETS_FILE} is not a file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise StoreError(f"{TARGETS_FILE} is not valid JSON") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"{TARGETS_FILE} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise StoreError(f"{TARGETS_FILE} must be an object")
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key == "":
            raise StoreError(f"{TARGETS_FILE} keys must be non-empty strings")
        if not isinstance(value, list):
            raise StoreError(f"{TARGETS_FILE} {key!r} must be an array of strings")
        prefix: list[str] = []
        for item in value:
            if not isinstance(item, str) or item == "":
                raise StoreError(f"{TARGETS_FILE} {key!r} must be an array of strings")
            prefix.append(item)
        out[key] = prefix
    return out


class Runtime:
    def __init__(
        self,
        runner: Callable[[list[str]], Completed] | None = None,
        *,
        tmux_targets: dict[str, list[str]] | None = None,
    ) -> None:
        self._runner = runner or _default_runner
        self._tmux_targets = dict(tmux_targets) if tmux_targets is not None else {}

    def _run(self, argv: list[str]) -> Completed:
        return self._runner(argv)

    def _prefix(self, session_id: str) -> list[str]:
        raw = self._tmux_targets.get(session_id)
        if not isinstance(raw, list):
            return []
        return list(raw)

    def _tmux(self, session_id: str, tmux_argv: list[str]) -> Completed:
        return self._run(self._prefix(session_id) + tmux_argv)

    def available(self, session_id: str = "") -> bool:
        return self._tmux(session_id, ["tmux", "-V"]).returncode == 0

    def exists(self, session_id: str, *, target: str | None = None) -> bool:
        name = target or tmux_name(session_id)
        return self._tmux(session_id, ["tmux", "has-session", "-t", name]).returncode == 0

    def is_busy(self, session_id: str, *, settle: float | None = None) -> bool:
        """True when Grok's TUI in this tmux pane shows an in-flight turn."""
        del settle
        return self.grok_working(session_id)

    def grok_working(self, session_id: str) -> bool:
        if not self.exists(session_id):
            return False
        return grok_pane_is_working(self.capture(session_id))

    def start(
        self,
        session_id: str,
        command: str | None,
        cols: int | None,
        rows: int | None,
        command_argv: list[str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if not self.available(session_id):
            raise SystemExit("tmux is not installed")
        name = tmux_name(session_id)
        if self.exists(session_id):
            if cols is not None and rows is not None:
                self.resize(session_id, cols, rows)
            return
        argv: list[str] = ["tmux", "new-session", "-d", "-s", name]
        if cwd is not None:
            argv.extend(["-c", cwd])
        if cols is not None:
            argv.extend(["-x", str(cols)])
        if rows is not None:
            argv.extend(["-y", str(rows)])
        if command_argv:
            argv.append("--")
            argv.extend(command_argv)
        elif command is not None and command != "":
            argv.append("--")
            try:
                argv.extend(shlex.split(command))
            except ValueError as exc:
                raise SystemExit("invalid command quoting") from exc
        completed = self._tmux(session_id, argv)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "tmux new-session failed").strip()
            raise SystemExit(detail)

    def stop(self, session_id: str) -> None:
        if not self.exists(session_id):
            return
        name = tmux_name(session_id)
        completed = self._tmux(session_id, ["tmux", "kill-session", "-t", name])
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "tmux kill-session failed").strip()
            raise SystemExit(detail)

    def input_text(self, session_id: str, data: str, *, target: str | None = None) -> None:
        name = target or tmux_name(session_id)
        completed = self._tmux(session_id, ["tmux", "send-keys", "-t", name, "-l", "--", data])
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "tmux send-keys failed").strip()
            raise SystemExit(detail)

    def input_key(self, session_id: str, key: str, *, target: str | None = None) -> None:
        mapped = _KEY_MAP.get(key)
        if mapped is None:
            raise SystemExit(f"unknown key: {key}")
        name = target or tmux_name(session_id)
        completed = self._tmux(session_id, ["tmux", "send-keys", "-t", name, mapped])
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "tmux send-keys failed").strip()
            raise SystemExit(detail)

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        name = tmux_name(session_id)
        completed = self._tmux(
            session_id, ["tmux", "resize-window", "-t", name, "-x", str(cols), "-y", str(rows)]
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "tmux resize-window failed").strip()
            raise SystemExit(detail)

    def capture(self, session_id: str) -> str:
        name = tmux_name(session_id)
        completed = self._tmux(session_id, ["tmux", "capture-pane", "-t", name, "-p", "-e"])
        if completed.returncode != 0:
            return ""
        return completed.stdout
