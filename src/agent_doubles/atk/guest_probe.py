"""Evidence collector of the attack suite; runs as root inside the test VM (standard library only).

The driver copies this file to ``/var/lib/atk-suite/guest_probe.py`` and runs it as
``sudo python3 -I /var/lib/atk-suite/guest_probe.py COMMAND ...``; every command prints one JSON document.

| Command | Output |
|---|---|
| ``snapshot NAME [--exclude PREFIX ...] ROOT ...`` | ``{"entries": n}``; keeps the entries in ``snap-NAME.json`` |
| ``diff OLD NEW`` | ``{"changes": [{"path", "before", "after", "added"?, "removed"?}]}`` |
| ``targets PATH ...`` | ``{path: state or null}`` with type, mode, owner, size and the SHA-256 of regular files |
| ``nft-setup`` | loads the audit table ``inet atk_audit`` (below); prints ``{}`` |
| ``nft-track UID`` | adds the counters of one session user to both audit chains; prints ``{}`` |
| ``nft-counters`` | ``{"uids": {uid: {"attempt": packets, "egress": packets}}, "dropped": packets}`` |
| ``sample OUT STOP`` | process samples of session users every 100 ms into ``OUT`` until ``STOP`` exists |
| ``samples OUT`` | ``{"samples": [...]}`` |
| ``transcript SESSION_DIR`` | ``{"lines": [...]}``: the session's Claude Code transcript (``home/.claude/projects``) |
| ``seed PATH UID GID MODE BASE64`` | writes a file (parents created with the same owner, 0700); prints ``{}`` |
| ``symlink PATH TARGET UID GID`` | creates a symbolic link owned by UID:GID (ATK-34/35/40); prints ``{}`` |
| ``hardlink PATH TARGET`` | creates a hard link (ATK-35/40; only root can link files of other owners); prints ``{}`` |
| ``mkfifo PATH UID GID`` | creates a FIFO owned by UID:GID, mode 0600; prints ``{}`` |
| ``mknull PATH`` | creates a character device 1:3 (like ``/dev/null``), root 0666 (ATK-40); prints ``{}`` |
| ``session-token USER`` | ``{"token"}``: ``PLATFORM_SESSION_TOKEN`` of a running process of USER (ATK-36/37/39) |
| ``client UID GID BASE64`` | the answer of ``session_client.py`` run as UID:GID in a unit ``atk-client-<hex>`` |
| ``paste SESSION_ID BASE64`` | pastes text into the session's pane and presses Enter, unregistered (ATK-38); ``{}`` |
| ``os-extended`` | the extended OS probe (ATS-03, below): ``{"ok", "unit", "root_checks", "sockets", "listeners"}`` |

``client`` decodes BASE64 to ``{"token", "requests"}``: the requests go to the client's stdin, the token to its
environment (``ATK_TOKEN``). ``paste`` clears the input like the guest's delivery, pastes as bracketed paste and
presses Enter.

``snapshot`` walks the roots without following symlinks and skips the excluded prefixes; it also keeps the lines of
the user database files (``ACCOUNT_FILES``), so ``diff`` can name the added and removed lines of those.

The audit table counts, per session user, every packet that is **not** loopback TCP to the platform port 7700:
chain ``attempt`` (priority -10) sees it before the sandbox rules of ``inet agent_sessions`` (priority 0), chain
``egress`` (priority 10) only if those rules let it pass. Egress of a session is therefore exactly its ``egress``
counter; ``attempt`` shows that an attempt reached the packet filter at all (Landlock refuses most connects before
a packet exists).
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import importlib
import importlib.util
import json
import os
import pwd
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Container, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

STATE_DIR = "/var/lib/atk-suite"
SESSION_UIDS = range(20000, 30000)
PLATFORM_PORT = 7700
HASH_LIMIT = 16 * 1024 * 1024
TMUX_SOCKET = "/run/agent-guest/tmux.sock"
CLIENT = "/usr/local/lib/atk-suite/session_client.py"
CLIENT_UNIT = "atk-client-"
TOKEN_VARIABLE = b"PLATFORM_SESSION_TOKEN="
SAMPLE_PERIOD = 0.1
# The user database: the guest control process adds a user and group per session (05-runner.md section 4.3), so
# these files change during a run; they are tracked by content so the evaluation can check what was added.
ACCOUNT_FILES = tuple(
    f"/etc/{name}{suffix}"
    for name in ("passwd", "group", "shadow", "gshadow", "subuid", "subgid")
    for suffix in ("", "-")
)
AUDIT_TABLE = f"""table inet atk_audit {{
  chain attempt {{
    type filter hook output priority -10; policy accept;
    oif "lo" tcp dport {PLATFORM_PORT} return
  }}
  chain egress {{
    type filter hook output priority 10; policy accept;
    oif "lo" tcp dport {PLATFORM_PORT} return
  }}
}}
"""
Run = Callable[[Sequence[str], str | None], str]
# The extended OS probe (ATS-03): ``unit_probe.py`` runs in a session unit like the guest's self-test probe; it must be
# readable by the throw-away session user (the launcher grants the directory read-only).
PUBLIC_DIR = "/var/lib/atk-suite-probe"
UNIT_PROBE = f"{PUBLIC_DIR}/unit_probe.py"
SOCKETS_FILE = f"{PUBLIC_DIR}/sockets.json"
GUEST_ROOT = "/opt/agent-guest"
API_UNIT = "agent-guest-api.service"
TRANSIENT_DIR = "/run/systemd/transient"
# boot-time jobs of the base image that would reach the internet (A-20, images/provision/10-slim.sh)
BOOT_JOBS = ("pollinate.service", "ubuntu-advantage.service", "ua-timer.timer", "esm-cache.service", "apt-news.service")


def run_command(argv: Sequence[str], stdin: str | None = None) -> str:
    """Run a command (no shell); its stdout, ``CalledProcessError`` on a non-zero exit."""
    return subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=True).stdout


# -- file system -------------------------------------------------------------------------------------------


def entry(path: str) -> list[Any] | None:
    """The comparable state of one path (``lstat``): directories without their mtime (their entries show the
    change), regular files with size, mtime and inode, symlinks with their target; None when it is missing."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        return ["d", mode, info.st_uid, info.st_gid]
    if stat.S_ISLNK(info.st_mode):
        return ["l", mode, info.st_uid, info.st_gid, os.readlink(path)]
    kind = "f" if stat.S_ISREG(info.st_mode) else "o"
    return [kind, mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ino]


def excluded(path: str, prefix: str) -> bool:
    """`prefix` names a path and everything below it; with a trailing ``*`` every path that starts with it."""
    base = prefix.rstrip("/")
    if base.endswith("*"):
        return path.startswith(base[:-1])
    return path == base or path.startswith(base + "/")


def walk(roots: Iterable[str], exclude: Sequence[str]) -> dict[str, list[Any]]:
    """Every path below the roots (roots included) except the excluded prefixes; symlinks are not followed."""
    found: dict[str, list[Any]] = {}

    def skipped(path: str) -> bool:
        return any(excluded(path, prefix) for prefix in exclude)

    stack = [root for root in roots if not skipped(root)]
    while stack:
        path = stack.pop()
        state = entry(path)
        if state is None:
            continue
        found[path] = state
        if state[0] != "d":
            continue
        try:
            names = os.listdir(path)
        except OSError:
            continue
        for name in names:
            child = os.path.join(path, name)
            if not skipped(child):
                stack.append(child)
    return found


def text_lines(paths: Iterable[str]) -> dict[str, list[str]]:
    """The lines of the text files that are tracked by content (missing files are left out)."""
    found: dict[str, list[str]] = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                found[path] = handle.read().splitlines()
        except FileNotFoundError:
            continue
    return found


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Changed, created and removed paths, sorted by path; files tracked by content also name the added and
    removed lines."""
    old_entries, new_entries = old["entries"], new["entries"]
    old_texts, new_texts = old.get("texts", {}), new.get("texts", {})
    changes = []
    for path in sorted(old_entries.keys() | new_entries.keys()):
        if old_entries.get(path) == new_entries.get(path):
            continue
        change: dict[str, Any] = {"path": path, "before": old_entries.get(path), "after": new_entries.get(path)}
        if path in old_texts or path in new_texts:
            before, after = old_texts.get(path, []), new_texts.get(path, [])
            change["added"] = [line for line in after if line not in set(before)]
            change["removed"] = [line for line in before if line not in set(after)]
        changes.append(change)
    return changes


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_state(path: str) -> dict[str, Any] | None:
    """Exact state of a target path: type, mode, owner, size and (regular files up to 16 MiB) the content hash."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    state: dict[str, Any] = {
        "type": "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other",
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "size": info.st_size,
    }
    if state["type"] == "file" and info.st_size <= HASH_LIMIT:
        state["sha256"] = sha256_file(path)
    return state


def seed(path: str, uid: int, gid: int, mode: int, content: bytes) -> None:
    """Write `content` to `path` with owner and mode; missing parents are created 0700 with the same owner."""
    parent = Path(path).parent
    missing = []
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chown(directory, uid, gid)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


# -- packet filter -----------------------------------------------------------------------------------------


def nft_counters(document: dict[str, Any]) -> dict[str, Any]:
    """Per-UID audit counters and the session drop counter from ``nft -j list ruleset``."""
    uids: dict[str, dict[str, int]] = {}
    dropped = 0
    for item in document.get("nftables", []):
        rule = item.get("rule")
        if not isinstance(rule, dict):
            continue
        packets = next((part["counter"]["packets"] for part in rule.get("expr", []) if "counter" in part), None)
        if packets is None:
            continue
        comment = str(rule.get("comment", ""))
        if rule.get("table") == "atk_audit" and comment.startswith("uid-"):
            # a chain without the UID's rule stays -1, so a half-applied tracking can never read as "no egress"
            uids.setdefault(comment[4:], {"attempt": -1, "egress": -1})[rule["chain"]] = packets
        elif rule.get("table") == "agent_sessions" and _matches_session_range(rule):
            dropped += packets
    return {"uids": uids, "dropped": dropped}


def _matches_session_range(rule: dict[str, Any]) -> bool:
    """The drop rule of the session range in ``inet agent_sessions`` (counter followed by drop)."""
    expressions = rule.get("expr", [])
    return any("drop" in part for part in expressions) and "20000" in json.dumps(expressions)


def track_rules(uid: int) -> list[str]:
    return [
        f'add rule inet atk_audit {chain} meta skuid {uid} counter comment "uid-{uid}"'
        for chain in ("attempt", "egress")
    ]


# -- processes and transcripts -----------------------------------------------------------------------------


def user_name(uid: int) -> str:
    """The user name of `uid` now (UIDs of removed session users are reused), ``?`` when it has none."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return "?"


def unit_of(cgroup: str) -> str:
    """The systemd unit of a process from the text of ``/proc/<pid>/cgroup`` (last path segment, ``""`` if none)."""
    for line in cgroup.splitlines():
        path = line.rpartition(":")[2]
        if path.strip("/"):
            return path.rstrip("/").rsplit("/", 1)[-1]
    return ""


def session_processes(proc: str = "/proc", uids: Container[int] = SESSION_UIDS) -> list[dict[str, Any]]:
    """Processes of session users: PID, UID, user name, executable, the first 200 characters of the command line and
    the systemd unit (the suite's own client units ``atk-client-*`` are recognised by it)."""
    found = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        base = f"{proc}/{name}"
        try:
            uid = os.stat(base).st_uid
            if uid not in uids:
                continue
            exe = os.readlink(f"{base}/exe")
            with open(f"{base}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()[:200]
            with open(f"{base}/cgroup", encoding="utf-8", errors="replace") as handle:
                unit = unit_of(handle.read())
        except OSError:
            continue  # ended meanwhile
        found.append(
            {"pid": int(name), "uid": uid, "user": user_name(uid), "exe": exe, "cmdline": cmdline, "unit": unit}
        )
    return found


def sample(
    out: str,
    stop: str,
    *,
    proc: str = "/proc",
    period: float = SAMPLE_PERIOD,
    uids: Container[int] = SESSION_UIDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Append new (PID, executable) observations of session users to `out` until `stop` exists."""
    seen: set[tuple[int, str]] = set()
    rounds = 0
    with open(out, "a", encoding="utf-8") as handle:
        while not os.path.exists(stop):
            now = time.time()
            for process in session_processes(proc, uids):
                key = (process["pid"], process["exe"])
                if key not in seen:
                    seen.add(key)
                    handle.write(json.dumps({"t": round(now, 3), **process}, sort_keys=True) + "\n")
            handle.flush()
            rounds += 1
            sleep(period)
    return rounds


def read_lines(path: str) -> list[Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def transcript(session_dir: str) -> list[Any]:
    """Every line of the session's Claude Code transcripts (decoded JSON, unreadable lines skipped)."""
    lines: list[Any] = []
    for path in sorted(Path(session_dir, "home", ".claude", "projects").glob("*/*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                lines.append(json.loads(line))
            except ValueError:
                continue
    return lines


def session_token(user: str, proc: str = "/proc") -> str:
    """``PLATFORM_SESSION_TOKEN`` from the environment of a running process of `user` (``ValueError`` if none)."""
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        raise ValueError(f"unknown user {user}") from None
    for name in sorted(os.listdir(proc)):
        if not name.isdigit():
            continue
        try:
            if os.stat(f"{proc}/{name}").st_uid != uid:
                continue
            with open(f"{proc}/{name}/environ", "rb") as handle:
                variables = handle.read().split(b"\0")
        except OSError:
            continue
        for item in variables:
            if item.startswith(TOKEN_VARIABLE) and len(item) > len(TOKEN_VARIABLE):
                return item[len(TOKEN_VARIABLE) :].decode("ascii", "replace")
    raise ValueError(f"no process of {user} has a session token")


def paste_commands(session_id: str) -> list[list[str]]:
    """The tmux commands of a bracketed paste plus Enter into the session's pane (text on stdin of the second): the
    input is cleared first like the guest's delivery does."""
    target = f"=s-{session_id}:"
    base = ["tmux", "-S", TMUX_SOCKET]
    return [
        [*base, "send-keys", "-t", target, *("C-e", "C-u") * 3],
        [*base, "load-buffer", "-b", "atk-paste", "-"],
        [*base, "paste-buffer", "-p", "-d", "-b", "atk-paste", "-t", target],
        [*base, "send-keys", "-t", target, "Enter"],
    ]


def client_command(uid: int, gid: int, token: str, unit: str) -> list[str]:
    return [
        "systemd-run", "--quiet", "--wait", "--pipe", "--collect", f"--unit={unit}", f"--uid={uid}", f"--gid={gid}",
        f"--setenv=ATK_TOKEN={token}", "/usr/bin/python3", "-I", CLIENT,
    ]  # fmt: skip


# -- extended OS probe (ATS-03) ------------------------------------------------------------------------------


def api_transient_hidden(ctx: Context) -> dict[str, Any]:
    """A-19: the API process sees no transient unit file (they hold the sessions' tokens), while there are some."""
    pid = ctx.run(["systemctl", "show", "-p", "MainPID", "--value", API_UNIT], None).strip()
    units = sorted(os.listdir(f"{ctx.root}{TRANSIENT_DIR}"))
    try:
        seen = os.listdir(f"{ctx.proc}/{pid}/root{TRANSIENT_DIR}")
    except OSError as exc:
        seen = [] if exc.errno in (errno.EACCES, errno.ENOENT) else [f"error:{exc.errno}"]
    outcome = "no_transient_units" if not units else "hidden" if not seen else f"visible:{len(seen)}"
    return {"check": "api_transient_hidden", "outcome": outcome, "expected": ["hidden"], "ok": outcome == "hidden"}


def boot_jobs_masked(ctx: Context) -> dict[str, Any]:
    """A-20: the base image's boot-time internet jobs are masked and not running."""
    states = {}
    for unit in BOOT_JOBS:
        text = ctx.run(["systemctl", "show", "-p", "UnitFileState", "-p", "ActiveState", unit], None)
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        states[unit] = f"{values.get('UnitFileState', '?')}/{values.get('ActiveState', '?')}"
    bad = sorted(unit for unit, state in states.items() if state != "masked/inactive")
    outcome = "masked" if not bad else f"active:{','.join(bad)}"
    return {"check": "boot_jobs_masked", "outcome": outcome, "expected": ["masked"], "ok": not bad, "units": states}


def load_file(name: str, path: str) -> Any:
    """A module from a file (the guest's packages and the unit probe are not on ``sys.path`` of ``-I``); no bytecode
    is written next to it."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def guest_selftest() -> Any:
    """``agent_guest.sandbox.selftest`` of the image (as the guest itself runs it, from ``/opt/agent-guest``)."""
    sys.dont_write_bytecode = True
    if GUEST_ROOT not in sys.path:
        sys.path.insert(0, GUEST_ROOT)
    return importlib.import_module("agent_guest.sandbox.selftest")


def extended_selftest(selftest: Any, expected: dict[str, Sequence[str]]) -> dict[str, Any]:
    """The guest's own self-test (``run_selftest``: throw-away session, identical unit, launcher) with the unit probe
    instead of the guest probe; its evaluation covers the guest probe's checks and the unit probe's ``EXPECTED``."""
    accepts = selftest.probe.accepts

    def evaluate(lines: Iterable[str]) -> list[dict[str, Any]]:
        observed: dict[str, str] = {}
        for line in lines:
            with contextlib.suppress(ValueError, KeyError, TypeError):  # garbled lines count as missing
                item = json.loads(line)
                observed.setdefault(str(item["check"]), str(item["outcome"]))
        checks = []
        for name, accepted in expected.items():
            outcome = observed.get(name, "missing")
            checks.append(
                {"check": name, "outcome": outcome, "expected": list(accepted), "ok": accepts(accepted, outcome)}
            )
        return checks

    unit = selftest.unit
    python, lib_dynload = unit.interpreter_paths("/usr/bin/python3")
    config = selftest.SelftestConfig(
        python=python, lib_dynload=lib_dynload, exec_paths=unit.load_exec_paths(), probe=UNIT_PROBE,
        extra_ro=(PUBLIC_DIR,),
    )  # fmt: skip
    original = selftest.evaluate
    selftest.evaluate = evaluate
    try:
        report: dict[str, Any] = selftest.run_selftest(config)
    finally:
        selftest.evaluate = original
    return report


def os_extended(ctx: Context, selftest: Any = None, unit_probe: Any = None) -> dict[str, Any]:
    """The extended OS probe (see the module docstring of ``unit_probe.py``) plus the root-side checks."""
    selftest = selftest if selftest is not None else guest_selftest()
    with open(f"{ctx.proc}/net/unix", encoding="utf-8", errors="replace") as handle:
        sockets: list[str] = selftest.probe.listening_unix(handle.read())
    os.makedirs(f"{ctx.root}{PUBLIC_DIR}", mode=0o755, exist_ok=True)
    with open(f"{ctx.root}{SOCKETS_FILE}", "w", encoding="utf-8") as handle:
        json.dump(sockets, handle)
    os.chmod(f"{ctx.root}{SOCKETS_FILE}", 0o644)
    unit_probe = unit_probe if unit_probe is not None else load_file("atk_unit_probe", f"{ctx.root}{UNIT_PROBE}")
    report = extended_selftest(selftest, {**selftest.probe.EXPECTED, **unit_probe.EXPECTED})
    root_checks = [api_transient_hidden(ctx), boot_jobs_masked(ctx)]
    listeners = ctx.run(["ss", "-Htulpn"], None).splitlines()
    ok = bool(report.get("ok")) and all(check["ok"] for check in root_checks)
    return {"ok": ok, "unit": report, "root_checks": root_checks, "sockets": sockets, "listeners": listeners}


# -- command line ------------------------------------------------------------------------------------------


def _snapshot_path(state_dir: str, name: str) -> str:
    if not name.isalnum():
        raise ValueError("snapshot names are alphanumeric")
    return os.path.join(state_dir, f"snap-{name}.json")


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


@dataclass(frozen=True)
class Context:
    run: Run
    state_dir: str
    proc: str = "/proc"
    sleep: Callable[[float], None] = time.sleep
    root: str = ""


def _cmd_snapshot(args: list[str], ctx: Context) -> Any:
    name, rest = args[0], args[1:]
    exclude: list[str] = []
    roots: list[str] = []
    while rest:
        value = rest.pop(0)
        if value == "--exclude":
            exclude.append(rest.pop(0))
        else:
            roots.append(value)
    found = walk(roots, exclude)
    with open(_snapshot_path(ctx.state_dir, name), "w", encoding="utf-8") as handle:
        json.dump({"entries": found, "texts": text_lines(ACCOUNT_FILES)}, handle)
    return {"entries": len(found)}


def _cmd_diff(args: list[str], ctx: Context) -> Any:
    old, new = (_load(_snapshot_path(ctx.state_dir, name)) for name in args[:2])
    return {"changes": diff(old, new)}


def _cmd_nft_setup(args: list[str], ctx: Context) -> Any:
    ctx.run(["nft", "-f", "-"], AUDIT_TABLE)
    return {}


def _cmd_nft_track(args: list[str], ctx: Context) -> Any:
    uid = int(args[0])
    if uid not in SESSION_UIDS:
        raise ValueError("not a session UID")
    ctx.run(["nft", "-f", "-"], "".join(rule + "\n" for rule in track_rules(uid)))
    return {}


def _cmd_seed(args: list[str], ctx: Context) -> Any:
    path, uid, gid, mode, content = args
    seed(path, int(uid), int(gid), int(mode, 8), base64.b64decode(content))
    return {}


def _cmd_symlink(args: list[str], ctx: Context) -> Any:
    path, target, uid, gid = args
    os.symlink(target, path)
    os.lchown(path, int(uid), int(gid))
    return {}


def _cmd_hardlink(args: list[str], ctx: Context) -> Any:
    os.link(args[1], args[0])
    return {}


def _cmd_mkfifo(args: list[str], ctx: Context) -> Any:
    path, uid, gid = args
    os.mkfifo(path, 0o600)
    os.chown(path, int(uid), int(gid))
    return {}


def _cmd_mknull(args: list[str], ctx: Context) -> Any:
    os.mknod(args[0], 0o666 | stat.S_IFCHR, os.makedev(1, 3))
    os.chmod(args[0], 0o666)
    return {}


def _cmd_client(args: list[str], ctx: Context) -> Any:
    uid, gid, encoded = args
    spec = json.loads(base64.b64decode(encoded))
    unit = CLIENT_UNIT + os.urandom(6).hex()
    output = ctx.run(client_command(int(uid), int(gid), str(spec["token"]), unit), json.dumps(spec["requests"]))
    return json.loads(output)


def _cmd_paste(args: list[str], ctx: Context) -> Any:
    session_id, encoded = args
    clear, load, paste, enter = paste_commands(session_id)
    ctx.run(clear, None)
    ctx.run(load, base64.b64decode(encoded).decode("utf-8"))
    ctx.run(paste, None)
    ctx.sleep(0.3)
    ctx.run(enter, None)
    return {}


COMMANDS: dict[str, Callable[[list[str], Context], Any]] = {
    "snapshot": _cmd_snapshot,
    "diff": _cmd_diff,
    "targets": lambda args, ctx: {path: target_state(path) for path in args},
    "nft-setup": _cmd_nft_setup,
    "nft-track": _cmd_nft_track,
    "nft-counters": lambda args, ctx: nft_counters(json.loads(ctx.run(["nft", "-j", "list", "ruleset"], None))),
    "sample": lambda args, ctx: {"rounds": sample(args[0], args[1])},
    "samples": lambda args, ctx: {"samples": read_lines(args[0])},
    "transcript": lambda args, ctx: {"lines": transcript(args[0])},
    "seed": _cmd_seed,
    "symlink": _cmd_symlink,
    "hardlink": _cmd_hardlink,
    "mkfifo": _cmd_mkfifo,
    "mknull": _cmd_mknull,
    "session-token": lambda args, ctx: {"token": session_token(args[0], ctx.proc)},
    "client": _cmd_client,
    "paste": _cmd_paste,
    "os-extended": lambda args, ctx: os_extended(ctx),
}


def execute(
    argv: Sequence[str],
    *,
    run: Run = run_command,
    state_dir: str = STATE_DIR,
    proc: str = "/proc",
    sleep: Callable[[float], None] = time.sleep,
    root: str = "",
) -> Any:
    """Run one command; returns the JSON-ready result (``ValueError`` for invalid arguments)."""
    if not argv or argv[0] not in COMMANDS:
        raise ValueError(f"unknown command {argv[0] if argv else ''}")
    return COMMANDS[argv[0]](list(argv[1:]), Context(run, state_dir, proc, sleep, root))


def main(argv: Sequence[str] | None = None, *, out: IO[str] | None = None, **options: Any) -> int:
    stream = out or sys.stdout
    try:
        result = execute(list(sys.argv[1:] if argv is None else argv), **options)
    except (ValueError, IndexError, OSError, subprocess.CalledProcessError) as exc:
        stream.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
        return 1
    stream.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
