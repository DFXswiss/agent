"""Device daemon: lock, child supervisor, and user-service install."""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn
from xml.parsers.expat import ExpatError

from .runtime import Completed, run_argv as _default_run_argv
from .store import StoreError

LOCK_NAME = "daemon.lock"
SERVICE_LABEL = "swiss.dfx.agent"
SYNC_RESTART_LIMIT = 10
SYNC_RESTART_WINDOW_S = 60.0
# launchctl bootout returns before launchd drops the service. Bootstrap in that
# window fails or is removed again, and the agent stays unloaded.
LAUNCHD_RELOAD_BOUND_S = 30.0
LAUNCHD_RELOAD_POLL_S = 0.2
_LAUNCHD_STILL_REMOVING = (
    "input/output error",
    "operation already in progress",
    "file exists",
    "still being removed",
)
_LAUNCHD_PID_RE = re.compile(r"(?m)^\s*pid\s*=\s*([1-9][0-9]*)\s*$")


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


def hub_configured(home: Path) -> bool:
    """True when device.json has non-empty hub_url and device_token strings."""
    path = home / "device.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise StoreError("device.json is not valid JSON") from exc
    except json.JSONDecodeError as exc:
        raise StoreError("device.json is not valid JSON") from exc
    if not isinstance(data, dict):
        return False
    hub_url = data.get("hub_url")
    device_token = data.get("device_token")
    return (
        isinstance(hub_url, str)
        and hub_url != ""
        and isinstance(device_token, str)
        and device_token != ""
    )


def child_specs(prefix: list[str]) -> list[tuple[str, list[str]]]:
    """Ordered child processes the supervisor starts."""
    return [
        ("knock", [*prefix, "knock"]),
        ("sync", [*prefix, "sync", "--follow"]),
        ("dashboard", [*prefix, "dashboard"]),
        ("cli-bridge", [*prefix, "cli-bridge"]),
    ]


def _terminate(proc: Any) -> None:
    if proc is None:
        return
    poll = getattr(proc, "poll", None)
    if callable(poll) and poll() is not None:
        return
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    else:
        terminate = getattr(proc, "terminate", None)
        if callable(terminate):
            terminate()
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return
    try:
        wait(timeout=5)
    except subprocess.TimeoutExpired:
        if isinstance(pid, int) and pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        else:
            kill = getattr(proc, "kill", None)
            if callable(kill):
                kill()
        try:
            wait(timeout=5)
        except subprocess.TimeoutExpired:
            return


def run_supervisor(
    *,
    home: Path,
    argv_prefix: list[str],
    popen: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """
    Hold the daemon lock and supervise knock, sync --follow, dashboard, and cli-bridge.

    Sync starts only once device.json has hub_url and device_token. Sync deaths
    restart with a limit of SYNC_RESTART_LIMIT in SYNC_RESTART_WINDOW_S.
    knock/dashboard/cli-bridge deaths end the supervisor. SIGTERM/SIGINT terminate children and exit 0.
    """
    start = popen or subprocess.Popen
    now_fn = monotonic or time.monotonic
    sleep_fn = sleep or time.sleep

    from .coordinator_config import load_coordinator_config
    configured_workers = load_coordinator_config(home)
    lock_handle = acquire_lock(home)
    children: dict[str, Any] = {}
    sync_deaths: list[float] = []
    stopping = False
    specs = dict(child_specs(argv_prefix))
    for sid in configured_workers:
        specs["coordinator:" + sid] = [*argv_prefix, "coordinate", "--session", sid, "--follow"]
    required_children = ["knock", "dashboard", "cli-bridge", *(
        "coordinator:" + sid for sid in configured_workers
    )]

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
        try:
            children["knock"] = start(specs["knock"], start_new_session=True)
            children["dashboard"] = start(specs["dashboard"], start_new_session=True)
            children["cli-bridge"] = start(specs["cli-bridge"], start_new_session=True)
            for sid in configured_workers:
                name = "coordinator:" + sid
                children[name] = start(specs[name], start_new_session=True)
            if hub_configured(home):
                children["sync"] = start(specs["sync"], start_new_session=True)
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(str(exc) or type(exc).__name__) from exc

        while True:
            if stopping:
                raise SystemExit(0)
            sleep_fn(0.2)
            for name in required_children:
                proc = children[name]
                code = proc.poll()
                if code is not None:
                    others = {k: v for k, v in children.items() if k != name}
                    for other in others.values():
                        _terminate(other)
                    raise SystemExit(f"daemon child {name} exited {code}")
            sync = children.get("sync")
            if sync is None:
                if hub_configured(home):
                    children["sync"] = start(specs["sync"], start_new_session=True)
                continue
            code = sync.poll()
            if code is None:
                continue
            if not hub_configured(home):
                _terminate(sync)
                children.pop("sync", None)
                continue
            stamp = now_fn()
            sync_deaths.append(stamp)
            sync_deaths = [t for t in sync_deaths if stamp - t <= SYNC_RESTART_WINDOW_S]
            n = len(sync_deaths)
            print(f"daemon sync restart {n}", file=sys.stderr)
            if n >= SYNC_RESTART_LIMIT:
                terminate_remaining()
                raise SystemExit("daemon sync restart limit")
            children["sync"] = start(specs["sync"], start_new_session=True)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        terminate_remaining()
        lock_handle.close()


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def service_unit_text(
    *,
    program: list[str],
    home: Path,
    platform: str,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Render a launchd plist (darwin) or systemd user unit (linux)."""
    env = extra_env or {}
    if platform == "darwin":
        args_xml = "\n".join(
            f"    <string>{_xml_escape(part)}</string>" for part in program
        )
        env_xml = (
            "    <key>AGENT_HOME</key>\n"
            f"    <string>{_xml_escape(str(home))}</string>\n"
        )
        for key, value in env.items():
            env_xml += (
                f"    <key>{_xml_escape(key)}</key>\n"
                f"    <string>{_xml_escape(value)}</string>\n"
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
            f"{env_xml}"
            "  </dict>\n"
            "</dict>\n"
            "</plist>\n"
        )
    if platform == "linux":
        exec_start = " ".join(shlex.quote(part) for part in program)
        env_lines = f"Environment=AGENT_HOME={shlex.quote(str(home))}\n"
        for key, value in env.items():
            env_lines += f"Environment={key}={shlex.quote(value)}\n"
        return (
            "[Unit]\n"
            "Description=DFX agent device daemon\n"
            "\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            f"{env_lines}"
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


def service_home(path: Path, platform: str) -> Path | None:
    """AGENT_HOME recorded in an installed unit file; None when there is no unit file.

    A unit file that exists but records no AGENT_HOME, or that cannot be read or parsed,
    raises StoreError so that callers fail closed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read service unit {path}: {exc}") from exc
    recorded: str | None = None
    if platform == "darwin":
        try:
            data = plistlib.loads(text.encode("utf-8"))
        except (plistlib.InvalidFileException, ValueError, ExpatError) as exc:
            raise StoreError(f"malformed service unit {path}: {exc}") from exc
        env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        value = env.get("AGENT_HOME") if isinstance(env, dict) else None
        recorded = value if isinstance(value, str) else None
    elif platform == "linux":
        for line in text.splitlines():
            if not line.startswith("Environment="):
                continue
            try:
                tokens = shlex.split(line[len("Environment="):])
            except ValueError as exc:
                raise StoreError(f"malformed Environment entry in service unit {path}: {exc}") from exc
            if not tokens:
                recorded = None
                continue
            for token in tokens:
                if token.startswith("AGENT_HOME="):
                    recorded = token[len("AGENT_HOME="):]
    if not recorded:
        raise StoreError(f"service unit {path} records no AGENT_HOME")
    return Path(recorded)


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


def existing_service_agent_pg_bin(text: str, platform: str) -> str | None:
    if platform == "darwin":
        try:
            data = plistlib.loads(text.encode("utf-8"))
        except (plistlib.InvalidFileException, ValueError, ExpatError):
            return None
        env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        value = env.get("AGENT_PG_BIN") if isinstance(env, dict) else None
        return value.strip() or None if isinstance(value, str) else None
    if platform == "linux":
        recorded: str | None = None
        for line in text.splitlines():
            if not line.startswith("Environment="):
                continue
            try:
                tokens = shlex.split(line[len("Environment="):])
            except ValueError:
                return None
            if not tokens:
                recorded = None
                continue
            for token in tokens:
                if token.startswith("AGENT_PG_BIN="):
                    recorded = token[len("AGENT_PG_BIN="):].strip()
        return recorded or None
    return None


def _unit_belongs_to(path: Path, platform: str, home: Path) -> bool:
    """True when the installed unit at path records this home (after ~ expansion and symlink resolution)."""
    try:
        recorded = service_home(path, platform)
    except StoreError:
        return False
    if recorded is None:
        return False
    return recorded.expanduser().resolve() == home.expanduser().resolve()


def kept_service_agent_pg_bin(home: Path, platform: str | None = None) -> str | None:
    """AGENT_PG_BIN recorded in the installed unit of THIS home, or None (missing, unreadable, malformed, or another home's unit)."""
    plat = sys.platform if platform is None else platform
    path = service_path(plat, home)
    if not _unit_belongs_to(path, plat, home):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return existing_service_agent_pg_bin(text, plat)


def adopt_kept_agent_pg_bin(home: Path, platform: str | None = None) -> str | None:
    """When AGENT_PG_BIN is not in the environment, export the value kept in the installed unit.

    Returns the adopted value, or None when nothing was adopted.
    """
    if "AGENT_PG_BIN" in os.environ:
        return None
    kept = kept_service_agent_pg_bin(home, platform)
    if kept:
        os.environ["AGENT_PG_BIN"] = kept
    return kept


def service_extra_env(*, existing_pg_bin: str | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin"}
    if "AGENT_PG_BIN" in os.environ:
        pg_bin = os.environ["AGENT_PG_BIN"].strip()
        if pg_bin:
            env["AGENT_PG_BIN"] = pg_bin
    elif isinstance(existing_pg_bin, str) and existing_pg_bin.strip():
        env["AGENT_PG_BIN"] = existing_pg_bin.strip()
    dsn = os.environ.get("AGENT_PG_DSN")
    if dsn:
        env["AGENT_PG_DSN"] = dsn
    return env


def _launchctl_detail(completed: Completed, fallback: str) -> str:
    text = completed.stderr.strip()
    if text == "":
        text = completed.stdout.strip()
    if text == "":
        return fallback
    return text


def _launchd_absent(completed: Completed) -> bool:
    """True when launchctl print says this domain no longer has the service."""
    return completed.returncode != 0 and _already_unloaded(completed.stderr, completed.stdout)


def _running_pid(completed: Completed) -> int | None:
    if completed.returncode != 0:
        return None
    match = _LAUNCHD_PID_RE.search(f"{completed.stdout}\n{completed.stderr}")
    if match is None:
        return None
    return int(match.group(1))


def _observed_service_state(completed: Completed) -> str:
    if _launchd_absent(completed):
        return "not loaded"
    if completed.returncode != 0:
        return "print failed"
    pid = _running_pid(completed)
    if pid is None:
        return "loaded without a running process"
    return f"running pid={pid}"


def _raise_launchd_failure(detail: str, observed: Completed) -> NoReturn:
    raw_parts = [observed.stdout.strip(), observed.stderr.strip()]
    raw = "\n".join(part for part in raw_parts if part)
    message = f"{detail.strip()}\nobserved service state: {_observed_service_state(observed)}"
    if raw and raw not in message:
        message = f"{message}\n{raw}"
    raise StoreError(message)


def _bootstrap_still_removing(completed: Completed) -> bool:
    if completed.returncode == 0:
        return False
    text = f"{completed.stderr}\n{completed.stdout}".lower()
    return any(needle in text for needle in _LAUNCHD_STILL_REMOVING)


def _verify_failure_detail(bootstrap: Completed | None) -> str:
    prefix = "launchctl bootstrap did not leave the service loaded with a running process"
    if bootstrap is None:
        return prefix
    output = _launchctl_detail(bootstrap, "")
    if output == "":
        return prefix
    return f"{prefix}\n{output}"


def _reload_darwin_service(
    *,
    runner: Callable[[list[str]], Completed],
    domain: str,
    label: str,
    path: Path,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> None:
    """Leave gui/<uid>/swiss.dfx.agent loaded with a running process.

    bootout returns before launchd removes the service. This waits, bounded by
    LAUNCHD_RELOAD_BOUND_S, until print no longer knows it, then bootstraps.
    A bootstrap that fails because the previous instance is still being removed
    is retried inside that same bound. The command fails with the launchctl
    output and the observed service state instead of leaving the service unloaded.
    """
    deadline = now_fn() + LAUNCHD_RELOAD_BOUND_S
    bootout = _runner_result(runner(["launchctl", "bootout", label]))
    bootout_problem = ""
    if bootout.returncode != 0 and not _already_unloaded(bootout.stderr, bootout.stdout):
        bootout_problem = _launchctl_detail(bootout, "launchctl bootout failed")
    last_bootstrap: Completed | None = None

    def remaining() -> float:
        return deadline - now_fn()

    def print_service() -> Completed:
        return _runner_result(runner(["launchctl", "print", label]))

    def wait_until_absent() -> Completed:
        while True:
            observed = print_service()
            if _launchd_absent(observed):
                return observed
            if observed.returncode != 0:
                _raise_launchd_failure(
                    _launchctl_detail(observed, "launchctl print failed"),
                    observed,
                )
            if remaining() <= 0:
                if last_bootstrap is not None:
                    detail = _launchctl_detail(last_bootstrap, "launchctl bootstrap failed")
                elif bootout_problem != "":
                    detail = bootout_problem
                else:
                    detail = f"launchd still knows {label} after bootout"
                _raise_launchd_failure(detail, observed)
            sleep_fn(min(LAUNCHD_RELOAD_POLL_S, remaining()))

    while True:
        observed = wait_until_absent()
        if remaining() <= 0:
            detail = "reload bound elapsed before launchctl bootstrap"
            if last_bootstrap is not None:
                detail = _launchctl_detail(last_bootstrap, detail)
            _raise_launchd_failure(detail, observed)
        last_bootstrap = _runner_result(
            runner(["launchctl", "bootstrap", domain, str(path)])
        )
        if last_bootstrap.returncode != 0:
            detail = _launchctl_detail(last_bootstrap, "launchctl bootstrap failed")
            if not _bootstrap_still_removing(last_bootstrap) or remaining() <= 0:
                _raise_launchd_failure(detail, print_service())
            sleep_fn(min(LAUNCHD_RELOAD_POLL_S, remaining()))
            continue
        while True:
            observed = print_service()
            if _running_pid(observed) is not None:
                return
            if _launchd_absent(observed):
                if remaining() <= 0:
                    _raise_launchd_failure(
                        _verify_failure_detail(last_bootstrap),
                        observed,
                    )
                sleep_fn(min(LAUNCHD_RELOAD_POLL_S, remaining()))
                break
            if observed.returncode != 0:
                _raise_launchd_failure(
                    _launchctl_detail(observed, "launchctl print failed"),
                    observed,
                )
            if remaining() <= 0:
                _raise_launchd_failure(
                    _verify_failure_detail(last_bootstrap),
                    observed,
                )
            sleep_fn(min(LAUNCHD_RELOAD_POLL_S, remaining()))


def install_and_start_service(
    *,
    home: Path,
    program: list[str],
    platform: str | None = None,
    run_argv: Callable[[list[str]], Completed] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Write the user service unit and start it.

    Under pytest the launchctl/systemctl calls are skipped unless the caller
    passes run_argv, so the unit file can be asserted without a user service.
    """
    plat = sys.platform if platform is None else platform
    path = service_path(plat, home)
    kept = kept_service_agent_pg_bin(home, plat)
    text = service_unit_text(
        program=program,
        home=home,
        platform=plat,
        extra_env=service_extra_env(existing_pg_bin=kept),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if run_argv is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return
    runner = _default_run_argv if run_argv is None else run_argv
    if plat == "darwin":
        uid = os.getuid()
        domain = f"gui/{uid}"
        label = f"{domain}/{SERVICE_LABEL}"
        _reload_darwin_service(
            runner=runner,
            domain=domain,
            label=label,
            path=path,
            now_fn=time.monotonic if monotonic is None else monotonic,
            sleep_fn=time.sleep if sleep is None else sleep,
        )
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
