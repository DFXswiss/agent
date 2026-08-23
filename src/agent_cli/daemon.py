"""Device daemon: lock, child supervisor, and user-service install."""

from __future__ import annotations

import fcntl
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .runtime import Completed, run_argv as _default_run_argv
from .store import StoreError

LOCK_NAME = "daemon.lock"
SERVICE_LABEL = "swiss.dfx.agent"
SYNC_RESTART_LIMIT = 10
SYNC_RESTART_WINDOW_S = 60.0


def agent_argv() -> list[str]:
    """Argv prefix that re-invokes this CLI (list form; never a shell string)."""
    if not sys.argv:
        return [sys.executable, "-m", "agent_cli"]
    raw = sys.argv[0]
    path = Path(raw)
    if path.suffix in {".py", ".pyc"} or path.name == "__main__.py":
        return [sys.executable, "-m", "agent_cli"]
    try:
        resolved = path.resolve()
    except OSError:
        return [sys.executable, "-m", "agent_cli"]
    if resolved.is_file():
        return [str(resolved)]
    return [sys.executable, "-m", "agent_cli"]


def lock_path(home: Path) -> Path:
    return home / LOCK_NAME


def acquire_lock(home: Path) -> object:
    """Exclusive flock on home/daemon.lock. Never steals the lock."""
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise StoreError("agent daemon already running") from exc
    except OSError as exc:
        handle.close()
        raise StoreError("agent daemon already running") from exc
    return handle


def child_specs() -> list[tuple[str, list[str]]]:
    """Ordered child processes the supervisor starts."""
    prefix = agent_argv()
    return [
        ("knock", [*prefix, "knock"]),
        ("sync", [*prefix, "sync", "--follow"]),
        ("dashboard", [*prefix, "dashboard"]),
    ]


def _terminate(proc: Any) -> None:
    if proc is None:
        return
    poll = getattr(proc, "poll", None)
    if callable(poll) and poll() is not None:
        return
    terminate = getattr(proc, "terminate", None)
    if callable(terminate):
        terminate()


def run_supervisor(
    *,
    home: Path,
    argv_prefix: list[str],
    popen: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """
    Hold the daemon lock and supervise knock, sync --follow, and dashboard.

    Sync deaths restart with a limit of SYNC_RESTART_LIMIT in SYNC_RESTART_WINDOW_S.
    knock/dashboard deaths end the supervisor. SIGTERM/SIGINT terminate children and exit 0.
    """
    start = popen or subprocess.Popen
    now_fn = monotonic or time.monotonic
    sleep_fn = sleep or time.sleep

    lock_handle = acquire_lock(home)
    children: dict[str, Any] = {}
    sync_deaths: list[float] = []
    stopping = False

    def terminate_remaining() -> None:
        for proc in children.values():
            _terminate(proc)

    def on_signal(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        terminate_remaining()
        raise SystemExit(0)

    previous_term = signal.signal(signal.SIGTERM, on_signal)
    previous_int = signal.signal(signal.SIGINT, on_signal)
    try:
        specs = [
            ("knock", [*argv_prefix, "knock"]),
            ("sync", [*argv_prefix, "sync", "--follow"]),
            ("dashboard", [*argv_prefix, "dashboard"]),
        ]
        for name, argv in specs:
            children[name] = start(argv)

        while True:
            if stopping:
                raise SystemExit(0)
            sleep_fn(0.2)
            for name in ("knock", "dashboard"):
                proc = children[name]
                code = proc.poll()
                if code is not None:
                    others = {k: v for k, v in children.items() if k != name}
                    for other in others.values():
                        _terminate(other)
                    raise SystemExit(f"daemon child {name} exited {code}")
            sync = children["sync"]
            code = sync.poll()
            if code is None:
                continue
            stamp = now_fn()
            sync_deaths.append(stamp)
            sync_deaths = [t for t in sync_deaths if stamp - t <= SYNC_RESTART_WINDOW_S]
            n = len(sync_deaths)
            print(f"daemon sync restart {n}", file=sys.stderr)
            if n >= SYNC_RESTART_LIMIT:
                terminate_remaining()
                raise SystemExit("daemon sync restart limit")
            children["sync"] = start([*argv_prefix, "sync", "--follow"])
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        # Keep lock_handle referenced until supervisor exit so the flock stays held.
        lock_handle.close()


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def service_unit_text(*, program: list[str], home: Path, platform: str) -> str:
    """Render a launchd plist (darwin) or systemd user unit (linux)."""
    if platform == "darwin":
        args_xml = "\n".join(
            f"    <string>{_xml_escape(part)}</string>" for part in program
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{SERVICE_LABEL}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"{args_xml}\n"
            "  </array>\n"
            "  <key>RunAtLoad</key>\n"
            "  <true/>\n"
            "  <key>KeepAlive</key>\n"
            "  <true/>\n"
            "  <key>EnvironmentVariables</key>\n"
            "  <dict>\n"
            "    <key>AGENT_HOME</key>\n"
            f"    <string>{_xml_escape(str(home))}</string>\n"
            "  </dict>\n"
            "</dict>\n"
            "</plist>\n"
        )
    if platform == "linux":
        exec_start = " ".join(shlex.quote(part) for part in program)
        return (
            "[Unit]\n"
            "Description=DFX agent device daemon\n"
            "\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            f"Environment=AGENT_HOME={home}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    raise StoreError(f"daemon install unsupported on {platform}")


def service_path(platform: str, home: Path) -> Path:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return home / "daemon.service"
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if platform == "linux":
        return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_LABEL}.service"
    raise StoreError(f"daemon install unsupported on {platform}")


def _runner_result(result: Completed | object) -> Completed:
    if isinstance(result, Completed):
        return result
    returncode = int(getattr(result, "returncode", 1))
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    return Completed(returncode, stdout, stderr)


def _already_unloaded(stderr: str, stdout: str) -> bool:
    text = f"{stderr}\n{stdout}".lower()
    needles = (
        "no such process",
        "could not find service",
        "not loaded",
        "not found",
        "unit not loaded",
        "could not find domain",
    )
    return any(n in text for n in needles)


def install_and_start_service(
    *,
    home: Path,
    program: list[str],
    platform: str | None = None,
    run_argv: Callable[[list[str]], Completed] | None = None,
) -> None:
    """Write the user service unit and start it (skipped under pytest)."""
    plat = sys.platform if platform is None else platform
    text = service_unit_text(program=program, home=home, platform=plat)
    path = service_path(plat, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    runner = run_argv or _default_run_argv
    if plat == "darwin":
        uid = os.getuid()
        domain = f"gui/{uid}"
        label = f"{domain}/{SERVICE_LABEL}"
        runner(["launchctl", "bootout", label])
        completed = _runner_result(runner(["launchctl", "bootstrap", domain, str(path)]))
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "launchctl bootstrap failed").strip()
            raise StoreError(detail)
        return
    if plat == "linux":
        for argv in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"{SERVICE_LABEL}.service"],
        ):
            completed = _runner_result(runner(argv))
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "systemctl failed").strip()
                raise StoreError(detail)
        return
    raise StoreError(f"daemon install unsupported on {plat}")


def uninstall_service(
    *,
    home: Path,
    platform: str | None = None,
    run_argv: Callable[[list[str]], Completed] | None = None,
) -> None:
    """Stop the user service and remove its unit file."""
    plat = sys.platform if platform is None else platform
    path = service_path(plat, home)
    runner = run_argv or _default_run_argv
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        if plat == "darwin":
            uid = os.getuid()
            label = f"gui/{uid}/{SERVICE_LABEL}"
            completed = _runner_result(runner(["launchctl", "bootout", label]))
            if completed.returncode != 0 and not _already_unloaded(
                completed.stderr, completed.stdout
            ):
                detail = (completed.stderr or completed.stdout or "launchctl bootout failed").strip()
                raise StoreError(detail)
        elif plat == "linux":
            completed = _runner_result(
                runner(["systemctl", "--user", "disable", "--now", f"{SERVICE_LABEL}.service"])
            )
            if completed.returncode != 0 and not _already_unloaded(
                completed.stderr, completed.stdout
            ):
                detail = (completed.stderr or completed.stdout or "systemctl disable failed").strip()
                raise StoreError(detail)
        elif plat not in ("darwin", "linux"):
            raise StoreError(f"daemon install unsupported on {plat}")
    if path.is_file():
        path.unlink()
