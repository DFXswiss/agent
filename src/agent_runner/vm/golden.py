"""Golden image build and registry (docs/platform/spec/05-runner.md sections 3.1, 4.5, 15; 02-data-model.md
section 4 `golden`; ADR-009; images/README.md build contract).

`build(...)` turns the definition in `images/` (SBX-01) into a stopped, versioned Lima instance:

1. `limactl` >= 2.3.0; pins from `images/versions.json` (`--claude-version` must equal the pin, section 15);
   one build at a time per store (advisory lock), stale `building` rows of a crashed build and their instances
   are removed first.
2. `golden` row `building` -> `limactl create --name=<prefix>golden-build-<hex>` + `start` (slim provisioning).
3. Bundle (`images/`, `src/agent_guest/`, the JSON contracts the guest validates against, and the sources of the
   main orchestrator's agent tool: `src/agent_platform/`, `src/agent_runner/`, `src/agent_cli/{__init__,keys}.py`,
   05-runner.md section 17.4) copied in, stage-2 installer `images/provision/install.sh`, contracts to
   `/opt/agent-guest/contracts`.
4. `exec_libs` (`ldd` of `/opt/claude/claude`, of the interpreter and of `_ctypes`; realpaths, sorted, unique)
   -> `/opt/agent-guest/exec-paths.json`; Landlock ABI probe (< 4 fails the build) -> `/opt/agent-guest/build.json`
   `{claude_version, guest_version, landlock_abi, exec_paths_sha256, source_sha256, built_at}`.
5. Smoke: guest services healthy through the forwarded control socket (`GET /v1/health`, state `unassigned`, the
   versions and ABI of this build) and the API port, `claude --version`, sandbox self-test (section 4.5), Claude TUI
   start time under Landlock (`tui_start_ms`, stored in `build.json`).
6. Build-only state removed (services stopped, smoke token, journal, bundle), `limactl stop`, the empty mode-000
   `sock/` directory Lima leaves in the instance directory removed (it makes `limactl clone` fail), `limactl rename` to
   `<prefix>golden-<claude>-<guest>-<yyyymmdd>` (or `<prefix>golden-<tag>`), row `ready`; every older `ready`
   golden becomes `retired` (exactly one `ready` golden for new clones).

Any failure deletes the instance and the row and raises a `RunnerError` (`golden_*` or `lima_*` code); the failed
step is announced as `image.failed step=<step>` and Lima's stderr tail is passed to `log`.
`retire(...)` deletes a non-current golden that no VM references. A host-side build record (build.json plus step
timings) is written to `$AGENT_RUNNER_HOME/golden/<version>.json` (05 section 1.4).

Every external effect is injected: `Lima` (its `limactl` callable), the store DSN, the clock, `now`, the random
source, the HTTP call over the control socket and the fact sink, so the unit tests cover every step and failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import shutil
import socket
import tarfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_runner.errors import RunnerError
from agent_runner.vm.lima import SHARED_PREFIX, Lima, LimaError

MIN_LANDLOCK_ABI = 4
BUILD_LOCK = 742180210
GUEST_BUNDLE = "/tmp/agent-golden.tar"
GUEST_BUNDLE_DIR = "/tmp/agent-golden"
GUEST_ROOT = "/opt/agent-guest"
BUILD_FILE = f"{GUEST_ROOT}/build.json"
EXEC_PATHS_FILE = f"{GUEST_ROOT}/exec-paths.json"
TOKEN_FILE = "/etc/agent-guest/control-token"
SERVICES = ("agent-guest-control.service", "agent-guest-api.service")
HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.1
INSTALL_TIMEOUT_S = 900.0
SELFTEST_TIMEOUT_S = 300.0
TUI_TIMEOUT_S = 120.0
# Contract files the guest validates against at run time (GST-01: AGENT_GUEST_CONTRACTS); everything else in
# contracts/ (fixtures, vectors, the tools and changes directories, npm files) stays out of the image. CONTRACT_SKIP
# applies at every depth; CONTRACT_TOP_SKIP at the top level only, because `contracts/mcp/tools/` holds the MCP tool
# schemas the guest serves.
CONTRACT_SKIP = frozenset({"node_modules", "fixtures", "vectors"})
CONTRACT_TOP_SKIP = frozenset({"tools", "changes"})
BUNDLE_SKIP = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
TOOL_PARTS = ("src/agent_platform", "src/agent_runner")
TOOL_FILES = ("src/agent_cli/__init__.py", "src/agent_cli/keys.py")
"""The agent tool of the main orchestrator and what it imports (05-runner.md section 17.4, `install.sh` step `agent
tool`); a tree without them bundles nothing of them."""
_VERSION_PART_RE = re.compile(r"^[0-9a-z]+(?:[.-][0-9a-z]+)*$")
_TAG_RE = re.compile(r"^[0-9a-z]+(?:[.-][0-9a-z]+)*$")
_GUEST_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
MAX_SUFFIX = 99

# Runs as root inside the build VM: `python3 -I -c INSPECT_SCRIPT CLAUDE OUT_FILE`. Computes the exec libraries of
# the Claude binary, the interpreter and `_ctypes` (realpaths, sorted, unique), writes them as canonical JSON to
# OUT_FILE and prints `{"exec_paths", "exec_paths_sha256", "landlock_abi", "kernel"}`. The ABI comes from
# landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION); a kernel without Landlock reports 0.
INSPECT_SCRIPT = r"""
import ctypes, hashlib, json, os, re, subprocess, sys
import _ctypes
claude, out = sys.argv[1], sys.argv[2]
paths = set()
for binary in (claude, os.path.realpath(sys.executable), _ctypes.__file__):
    listing = subprocess.run(["ldd", binary], capture_output=True, text=True, check=True).stdout
    for line in listing.splitlines():
        match = re.search(r"(?:=>\s*)?(/\S+)\s+\(0x[0-9a-f]+\)", line)
        if match:
            paths.add(os.path.realpath(match.group(1)))
listed = sorted(paths)
text = json.dumps(listed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
with open(out + ".part", "w", encoding="ascii") as handle:
    handle.write(text)
os.chmod(out + ".part", 0o644)
os.replace(out + ".part", out)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
abi = libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
print(json.dumps({"exec_paths": listed, "exec_paths_sha256": hashlib.sha256(text.encode("ascii")).hexdigest(),
                  "landlock_abi": max(int(abi), 0), "kernel": os.uname().release}))
"""

# Runs as root inside the build VM from /opt/agent-guest: `python3 -E -s -c TUI_SCRIPT TIMEOUT`. Starts Claude Code
# exactly like a main session (throw-away session of the self-test, `unit.build`: systemd unit + Landlock launcher)
# in a detached tmux pane and measures the time until the TUI has painted its first screen. Prints
# `{"ok", "tui_start_ms", "screen"}`; the session, unit and tmux session are removed again.
TUI_SCRIPT = r"""
import json, re, secrets, subprocess, sys, time
from agent_guest.sandbox import selftest, unit
limit = float(sys.argv[1])
painted = re.compile("(Claude Code|\\u256d|\\u276f|Welcome|trust)")
python, dynload = unit.interpreter_paths()
paths = unit.load_exec_paths()
sessions = selftest.ThrowawaySessions()
name = "agent-golden-tui"
unit_name = unit.UNIT_PREFIX + sessions.own
screen, elapsed = "", None
try:
    sessions.create()
    spec = unit.SessionSpec(session_id=sessions.own, token=secrets.token_urlsafe(32), python=python,
                            lib_dynload=dynload, exec_paths=paths, kind="main")
    argv = unit.build(spec, [])
    start = time.monotonic()
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "160", "-y", "48", *argv], check=True)
    while time.monotonic() - start < limit:
        shot = subprocess.run(["tmux", "capture-pane", "-p", "-t", name], capture_output=True, text=True)
        screen = shot.stdout
        if painted.search(screen):
            elapsed = time.monotonic() - start
            break
        if shot.returncode != 0:
            break
        time.sleep(0.02)
finally:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    subprocess.run(["systemctl", "stop", unit_name], capture_output=True)
    sessions.remove()
lines = [line.rstrip() for line in screen.splitlines() if line.strip()]
print(json.dumps({"ok": elapsed is not None, "tui_start_ms": None if elapsed is None else round(elapsed * 1000),
                  "screen": "\n".join(lines[:12])[:1500]}))
"""

Announce = Callable[[str, dict[str, str | int | bool]], None]
HttpGet = Callable[[str, str, Mapping[str, str], float], tuple[int, bytes]]


# -- source (images/, src/agent_guest/, contracts/) ---------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """The build input: a checkout (or installed tree) with `images/`, `src/agent_guest/` and `contracts/`."""

    root: Path
    template: Path
    versions: Mapping[str, Any]
    guest_version: str

    @property
    def claude_version(self) -> str:
        return str(self.versions["claude_code"])

    @property
    def image_digest(self) -> str:
        return str(self.versions["ubuntu_image_digest"])


def default_root() -> Path:
    """Root of this installation's tree (`src/agent_runner/vm/golden.py` -> the checkout)."""
    return Path(__file__).resolve().parents[3]


def load_source(template: Path | None = None, *, root: Path | None = None) -> Source:
    """Read and check the build input; `template` (GOLDEN_TEMPLATE) defaults to `<root>/images/golden.yaml`.

    The tree is the template's `images/..` directory, so a configured template brings its own provisioning
    files and pins.
    """
    path = template if template is not None else (root or default_root()) / "images" / "golden.yaml"
    tree = path.resolve().parent.parent
    try:
        versions = json.loads((tree / "images" / "versions.json").read_text(encoding="utf-8"))
        init = (tree / "src" / "agent_guest" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise RunnerError("golden_source_missing") from None
    match = _GUEST_VERSION_RE.search(init)
    claude = versions.get("claude_code") if isinstance(versions, dict) else None
    digest = versions.get("ubuntu_image_digest") if isinstance(versions, dict) else None
    if (
        not path.is_file()
        or not (tree / "images" / "provision" / "install.sh").is_file()
        or not (tree / "contracts").is_dir()
        or match is None
        or not isinstance(claude, str)
        or not isinstance(digest, str)
        or not _VERSION_PART_RE.fullmatch(claude)
        or not _VERSION_PART_RE.fullmatch(match.group(1))
    ):
        raise RunnerError("golden_source_missing")
    return Source(root=tree, template=path, versions=versions, guest_version=match.group(1))


def _bundle_members(source: Source) -> list[tuple[str, Path]]:
    """`(archive name, path)` of every bundled file, sorted by name."""
    members: list[tuple[str, Path]] = []
    for top, deep_skip, top_skip, suffixes in (
        ("images", BUNDLE_SKIP, frozenset(), None),
        ("src/agent_guest", BUNDLE_SKIP, frozenset(), None),
        ("contracts", BUNDLE_SKIP | CONTRACT_SKIP, CONTRACT_TOP_SKIP, (".json",)),
        *((part, BUNDLE_SKIP, frozenset(), None) for part in TOOL_PARTS),
    ):
        base = source.root / top
        for directory, dirnames, filenames in os.walk(base):
            skip = deep_skip | top_skip if Path(directory) == base else deep_skip
            dirnames[:] = sorted(name for name in dirnames if name not in skip and not name.startswith("."))
            for filename in filenames:
                # Hidden files (.DS_Store, AppleDouble ._*, editor state) are never part of the image.
                if filename.startswith(".") or filename.endswith(".pyc"):
                    continue
                if suffixes is not None and not filename.endswith(suffixes):
                    continue
                path = Path(directory) / filename
                if path.is_symlink() or not path.is_file():
                    continue
                members.append((path.relative_to(source.root).as_posix(), path))
    for name in TOOL_FILES:
        path = source.root / name
        if path.is_file() and not path.is_symlink():
            members.append((name, path))
    return sorted(members)


def write_bundle(source: Source, target: Path) -> str:
    """Write the bundle tar (root-owned entries, no macOS metadata) and return the source digest.

    The digest is SHA-256 over the sorted `name NUL sha256(content) NUL mode` lines of all members: equal inputs
    give equal digests, whatever the file times are.
    """
    digest = hashlib.sha256()
    with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, path in _bundle_members(source):
            data = path.read_bytes()
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(path.stat().st_mtime)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))
            digest.update(f"{name}\0{hashlib.sha256(data).hexdigest()}\0{mode:o}\n".encode())
    return digest.hexdigest()


# -- shared cache tag (ADR-027 section 7) ---------------------------------------------------------------------

CACHE_INPUTS = ("images", "src/agent_guest")
CACHE_OPTIONAL_INPUTS = ("contracts", "src/agent_runner/vm")
CACHE_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".pytest_cache"})
CACHE_SKIP_FILES = frozenset({".DS_Store"})


def cache_tag(root: Path) -> str:
    """`<sha12>` of the shared cache `cache-golden-<sha12>` built from the tree `root`: the first 12 hex digits of the
    SHA-256 over the sorted lines `<sha256(file)>  <path>` of every file below `images/`, `src/agent_guest/` and, when
    present, `contracts/` and `src/agent_runner/vm/` (the rule of the work-package tools and the E2E orchestrator)."""
    entries = []
    for sub in CACHE_INPUTS + tuple(name for name in CACHE_OPTIONAL_INPUTS if (root / name).is_dir()):
        for path in sorted((root / sub).rglob("*")):
            rel = path.relative_to(root)
            if (
                path.is_file()
                and path.name not in CACHE_SKIP_FILES
                and not CACHE_SKIP_DIRS.intersection(rel.parts[:-1])
            ):
                entries.append((rel.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(entries))
    return hashlib.sha256(lines.encode()).hexdigest()[:12]


# -- store ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Golden:
    version: str
    lima_name: str
    claude_version: str
    guest_version: str
    image_digest: str
    built_at: datetime | None
    state: str


def connect(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    try:
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    except psycopg.Error:
        raise RunnerError("store_unavailable") from None


_COLUMNS = "version, lima_name, claude_version, guest_version, image_digest, built_at, state"


def list_goldens(conn: psycopg.Connection[dict[str, Any]]) -> list[Golden]:
    """All rows, newest first (`built_at`, then version)."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM runner.golden ORDER BY built_at DESC NULLS FIRST, version DESC"
    ).fetchall()
    return [Golden(**row) for row in rows]


def ready_golden(conn: psycopg.Connection[dict[str, Any]]) -> Golden | None:
    """The golden new clones start from (VM-02): the newest `ready` row."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM runner.golden WHERE state = 'ready' ORDER BY built_at DESC, version DESC LIMIT 1"
    ).fetchone()
    return None if row is None else Golden(**row)


def references(conn: psycopg.Connection[dict[str, Any]], version: str) -> int:
    """VMs (not `deleted`) created from `version`."""
    row = conn.execute(
        "SELECT count(*) AS n FROM runner.vm WHERE golden_version = %s AND state <> 'deleted'", (version,)
    ).fetchone()
    return int(row["n"]) if row else 0


@contextlib.contextmanager
def build_lock(conn: psycopg.Connection[dict[str, Any]]) -> Iterator[None]:
    """Session advisory lock: one image build or retire at a time per store (`image_busy` otherwise)."""
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (BUILD_LOCK,)).fetchone()
    if not row or not row["ok"]:
        raise RunnerError("image_busy")
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK,))


# -- smoke over the forwarded control socket ----------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def unix_http_get(socket_path: str, path: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """`GET path` over the Unix socket; returns `(status, body)`; connection failures raise `OSError`."""
    connection = _UnixConnection(socket_path, timeout)
    try:
        connection.request("GET", path, headers=dict(headers))
        response = connection.getresponse()
        return response.status, response.read()
    except http.client.HTTPException as exc:
        raise OSError(str(exc)) from None
    finally:
        connection.close()


# -- build ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildOptions:
    cpus: int
    memory_mib: int
    disk_gib: int
    claude_version: str | None = None
    tag: str | None = None
    test_model_port: int | None = None


@dataclass
class BuildResult:
    golden: Golden
    build_info: dict[str, Any]
    steps: dict[str, float]
    seconds: float
    record: Path


@dataclass
class Builder:
    """One image build or retire with all collaborators injected (see the module docstring)."""

    lima: Lima
    dsn: str
    records: Path
    scratch: Path
    announce: Announce = field(default=lambda _name, _fields: None)
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    token_hex: Callable[[int], str] = secrets.token_hex
    http_get: HttpGet = unix_http_get
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = field(default=lambda _text: None)
    health_timeout: float = HEALTH_TIMEOUT_S
    steps: dict[str, float] = field(default_factory=dict)

    # -- helpers -------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _step(self, name: str) -> Iterator[None]:
        """Time one build step; a failure is reported as `image.failed step=<name>` (Lima's stderr goes to `log`)."""
        start = self.clock()
        try:
            yield
        except BaseException as exc:
            self.announce("image.failed", {"step": name})
            if isinstance(exc, LimaError) and exc.detail:
                self.log(f"image build step {name}: limactl {exc.operation} exit {exc.returncode}\n{exc.detail}\n")
            raise
        seconds = round(self.clock() - start, 3)
        self.steps[name] = seconds
        self.announce("image.step", {"step": name, "seconds": f"{seconds:.3f}"})

    def _root(self, name: str, script: str, *args: str, timeout: float | None = None) -> str:
        """Run `sh -c SCRIPT` as root in the guest; returns stdout."""
        return self.lima.shell(name, ["sudo", "sh", "-c", script, "sh", *args], timeout=timeout).stdout

    def _json(self, text: str, step: str) -> dict[str, Any]:
        """The JSON object a guest step printed: the whole output, else its last line (after other output)."""
        lines = text.strip().splitlines() or [""]
        try:
            value = json.loads(text)
        except ValueError:
            try:
                value = json.loads(lines[-1])
            except ValueError:
                raise RunnerError("golden_smoke_failed", step) from None
        if not isinstance(value, dict):
            raise RunnerError("golden_smoke_failed", step)
        return value

    # -- registry ------------------------------------------------------------------------------------------

    def _clean_stale(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        """Remove what a crashed build of this store left: its `building` rows and their instances.

        The row is written before the instance is created, so every build instance of this store is known here;
        build instances of other stores with the same prefix (parallel `cache-` builds) are never touched.
        """
        for row in conn.execute("SELECT version, lima_name FROM runner.golden WHERE state = 'building'").fetchall():
            # A crash after the rename leaves the instance under its final name.
            for name in (row["lima_name"], f"{self.lima.prefix}golden-{row['version']}"):
                if self.lima.owns(name):
                    self.lima.delete(name)
            conn.execute("DELETE FROM runner.golden WHERE version = %s", (row["version"],))
            self.announce("image.stale", {"version": row["version"], "lima_name": row["lima_name"]})

    def _version(self, conn: psycopg.Connection[dict[str, Any]], source: Source, tag: str | None) -> str:
        """`<claude>-<guest>-<yyyymmdd>` with `-2`, `-3`, ... when the version or name exists; or exactly the tag.

        A tag names one instance (`cache-golden-<sha12>`): an existing instance is refused (`golden_exists`), a row
        whose instance is gone is a stale registration and replaced.
        """
        if tag is not None:
            if self.lima.get(self.lima.name(f"golden-{tag}")) is not None:
                raise RunnerError("golden_exists")
            conn.execute("DELETE FROM runner.golden WHERE version = %s", (tag,))
            return tag
        base = f"{source.claude_version}-{source.guest_version}-{self.now():%Y%m%d}"
        taken = {row["version"] for row in conn.execute("SELECT version FROM runner.golden").fetchall()}
        names = {instance.name for instance in self.lima.list()}
        for index in range(1, MAX_SUFFIX + 1):
            candidate = base if index == 1 else f"{base}-{index}"
            if candidate not in taken and self.lima.name(f"golden-{candidate}") not in names:
                return candidate
        raise RunnerError("golden_version_exhausted")

    # -- build ---------------------------------------------------------------------------------------------

    def build(self, source: Source, options: BuildOptions) -> BuildResult:
        if options.claude_version is not None and options.claude_version != source.claude_version:
            raise RunnerError("claude_version_unpinned")
        if options.tag is not None and not _TAG_RE.fullmatch(options.tag):
            raise RunnerError("usage", "tag")
        started = self.clock()
        self.lima.check_version()
        with connect(self.dsn) as conn, build_lock(conn):
            self._clean_stale(conn)
            version = self._version(conn, source, options.tag)
            build_name = self.lima.name(f"golden-build-{self.token_hex(4)}")
            final_name = self.lima.name(f"golden-{version}")
            conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
                " VALUES (%s, %s, %s, %s, %s, 'building')",
                (version, build_name, source.claude_version, source.guest_version, source.image_digest),
            )
            self.announce("image.build", {"version": version, "lima_name": build_name, "state": "building"})
            current = build_name
            record = self.records / f"{version}.json"
            try:
                info = self._provision_and_check(build_name, source, options)
                with self._step("stop"):
                    self.lima.stop(build_name)
                    self._clear_socket_dir(build_name)
                with self._step("rename"):
                    self.lima.rename(build_name, final_name)
                    current = final_name
                built_at = self.now()
                seconds = round(self.clock() - started, 3)
                # The record is written before the commit: after the commit nothing may fail any more, because the
                # commit retires the previous golden.
                self._write_record(record, version, final_name, info, seconds)
                with conn.transaction():
                    conn.execute(
                        "UPDATE runner.golden SET state = 'retired' WHERE state = 'ready' AND version <> %s",
                        (version,),
                    )
                    conn.execute(
                        "UPDATE runner.golden SET lima_name = %s, built_at = %s, state = 'ready' WHERE version = %s",
                        (final_name, built_at, version),
                    )
            except BaseException:
                with contextlib.suppress(LimaError):
                    self.lima.delete(current)
                with contextlib.suppress(psycopg.Error):
                    conn.execute("DELETE FROM runner.golden WHERE version = %s", (version,))
                with contextlib.suppress(OSError):
                    record.unlink(missing_ok=True)
                raise
            golden = Golden(
                version=version,
                lima_name=final_name,
                claude_version=source.claude_version,
                guest_version=source.guest_version,
                image_digest=source.image_digest,
                built_at=built_at,
                state="ready",
            )
        return BuildResult(golden=golden, build_info=info, steps=dict(self.steps), seconds=seconds, record=record)

    def _clear_socket_dir(self, name: str) -> None:
        """Remove the host end of the socket forward (`<instance dir>/sock`) of the stopped instance.

        Lima leaves it behind empty with mode 000; `limactl clone` cannot copy such a directory, so a golden with
        it cannot be cloned. Lima creates it again at every start.
        """
        instance = self.lima.get(name)
        if instance is None:
            raise RunnerError("golden_smoke_failed", "stop")
        directory = Path(instance.dir) / "sock"
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o700)
            shutil.rmtree(directory)

    def _write_record(self, record: Path, version: str, name: str, info: Mapping[str, Any], seconds: float) -> None:
        self.records.mkdir(mode=0o700, parents=True, exist_ok=True)
        document = {"version": version, "lima_name": name, "build": dict(info), "steps": self.steps,
                    "seconds": seconds}  # fmt: skip
        part = record.with_suffix(".json.part")
        part.write_text(json.dumps(document, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        part.chmod(0o600)
        part.replace(record)

    def _provision_and_check(self, name: str, source: Source, options: BuildOptions) -> dict[str, Any]:
        with self._step("create"):
            self.lima.create(
                name,
                str(source.template),
                cpus=options.cpus,
                memory_mib=options.memory_mib,
                disk_gib=options.disk_gib,
            )
        with self._step("start"):
            self.lima.start(name)
        with self._step("bundle"):
            bundle = self.scratch / f"bundle-{self.token_hex(4)}.tar"
            try:
                source_sha256 = write_bundle(source, bundle)
                self.lima.copy_to(str(bundle), name, GUEST_BUNDLE)
            finally:
                bundle.unlink(missing_ok=True)
            self._root(
                name,
                'rm -rf "$1" && mkdir -m 0700 "$1" && tar -xf "$2" -C "$1" --no-same-owner && rm -f "$2"',
                GUEST_BUNDLE_DIR,
                GUEST_BUNDLE,
            )
        with self._step("install"):
            port = [] if options.test_model_port is None else ["--test-model-port", str(options.test_model_port)]
            self.lima.shell(
                name,
                ["sudo", "bash", f"{GUEST_BUNDLE_DIR}/images/provision/install.sh", *port],
                timeout=INSTALL_TIMEOUT_S,
            )
            self._root(
                name,
                'rm -rf "$2" && cp -R "$1" "$2" && chown -R root:root "$2"'
                ' && find "$2" -type d -exec chmod 0755 {} + && find "$2" -type f -exec chmod 0644 {} +',
                f"{GUEST_BUNDLE_DIR}/contracts",
                f"{GUEST_ROOT}/contracts",
            )
        with self._step("inspect"):
            probe = self._json(
                self.lima.shell(
                    name, ["sudo", "python3", "-I", "-c", INSPECT_SCRIPT, "/opt/claude/claude", EXEC_PATHS_FILE]
                ).stdout,
                "inspect",
            )
            abi, digest = probe.get("landlock_abi"), probe.get("exec_paths_sha256")
            if type(abi) is not int or not isinstance(digest, str):
                raise RunnerError("golden_smoke_failed", "inspect")
            if abi < MIN_LANDLOCK_ABI:
                raise RunnerError("landlock_abi_unsupported")
            info: dict[str, Any] = {
                "built_at": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "claude_version": source.claude_version,
                "exec_paths_sha256": digest,
                "guest_version": source.guest_version,
                "landlock_abi": abi,
                "source_sha256": source_sha256,
            }
            self._write_build_file(name, info)
        self._smoke(name, source, info)
        self._write_build_file(name, info)
        with self._step("seal"):
            self._root(
                name,
                "systemctl stop agent-guest-api.service agent-guest-control.service"
                ' && rm -f "$1" /var/lib/agent-guest/journal.db /var/lib/agent-guest/journal.db-wal'
                ' /var/lib/agent-guest/journal.db-shm && rm -rf "$2" && sync',
                TOKEN_FILE,
                GUEST_BUNDLE_DIR,
            )
        return info

    def _write_build_file(self, name: str, info: Mapping[str, Any]) -> None:
        text = json.dumps(dict(info), sort_keys=True, separators=(",", ":"))
        self._root(
            name, 'printf "%s" "$2" >"$1.part" && chmod 0644 "$1.part" && mv -f "$1.part" "$1"', BUILD_FILE, text
        )

    def _smoke(self, name: str, source: Source, info: dict[str, Any]) -> None:
        with self._step("services"):
            token = self._root(
                name,
                'umask 077 && install -d -m 0755 "$(dirname "$1")"'
                ' && python3 -I -c "import secrets; print(secrets.token_urlsafe(32))" >"$1" && cat "$1"'
                " && systemctl start agent-guest-control.service agent-guest-api.service",
                TOKEN_FILE,
            ).strip()
            if not _TOKEN_RE.fullmatch(token):
                raise RunnerError("golden_smoke_failed", "token")
            self._wait_healthy(name, token, source, info)
            api = self.lima.shell(
                name, ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:7700/v1/health"], check=False
            )
            if api.returncode != 0 or self._json(api.stdout or "{}", "api").get("state") != "ok":
                raise RunnerError("golden_smoke_failed", "api")
        with self._step("claude"):
            shown = self.lima.shell(name, ["/opt/claude/claude", "--version"]).stdout
            if not shown.startswith(f"{source.claude_version} "):
                raise RunnerError("golden_smoke_failed", "claude_version")
        with self._step("selftest"):
            result = self.lima.shell(
                name,
                ["sudo", "env", "-C", GUEST_ROOT, "python3", "-E", "-s", "-m", "agent_guest.sandbox.selftest"],
                timeout=SELFTEST_TIMEOUT_S,
                check=False,
            )
            report = self._json(result.stdout or "{}", "selftest") if result.stdout.strip() else {}
            if result.returncode != 0 or report.get("ok") is not True:
                failed = [c.get("check") for c in report.get("checks", []) if isinstance(c, dict) and not c.get("ok")]
                self.announce("image.selftest", {"ok": False, "failed": ",".join(map(str, failed)) or "unknown"})
                raise RunnerError("golden_smoke_failed", "selftest")
        with self._step("tui"):
            tui = self._json(
                self.lima.shell(
                    name,
                    ["sudo", "env", "-C", GUEST_ROOT, "python3", "-E", "-s", "-c", TUI_SCRIPT, str(int(TUI_TIMEOUT_S))],
                    timeout=TUI_TIMEOUT_S + 60,
                ).stdout,
                "tui",
            )
            millis = tui.get("tui_start_ms")
            if tui.get("ok") is not True or type(millis) is not int:
                raise RunnerError("golden_smoke_failed", "tui")
            info["tui_start_ms"] = millis

    def _wait_healthy(self, name: str, token: str, source: Source, info: Mapping[str, Any]) -> None:
        instance = self.lima.get(name)
        if instance is None:
            raise RunnerError("golden_smoke_failed", "health")
        headers = {"Authorization": f"Bearer {token}"}
        deadline = self.clock() + self.health_timeout
        while True:
            try:
                status, body = self.http_get(instance.control_socket, "/v1/health", headers, 5.0)
            except OSError:
                status, body = 0, b""
            if status == 200:
                try:
                    health = json.loads(body)
                except ValueError:
                    health = {}
                expected = {
                    "state": "unassigned",
                    "guest_version": source.guest_version,
                    "claude_version": source.claude_version,
                    "landlock_abi": info["landlock_abi"],
                }
                if isinstance(health, dict) and all(health.get(key) == value for key, value in expected.items()):
                    return
                raise RunnerError("golden_smoke_failed", "health")
            if self.clock() >= deadline:
                raise RunnerError("golden_smoke_failed", "health")
            self.sleep(HEALTH_POLL_S)

    # -- adopt ---------------------------------------------------------------------------------------------

    def adopt(self, source: Source, lima_name: str) -> Golden:
        """Register a stopped shared golden cache (`cache-golden-<tag>`, built by `build --tag` under the `cache-`
        prefix) as the `ready` golden; older `ready` rows become `retired`. The tag must be the `cache_tag` of the
        local tree (`golden_stale` otherwise), so the recorded versions and digest are the cache's.

        The instance is never copied, changed or deleted by this store (`retire` deletes only own instances): the
        pool clones from it like from an own golden (`Lima.clone`). Re-adopting the same cache refreshes its row.
        """
        self.lima.check_version()
        instance = self.lima.shared(lima_name)
        if instance is None:
            raise RunnerError("golden_missing", lima_name)
        if instance.status != "Stopped":
            raise RunnerError("golden_not_stopped", lima_name)
        version = lima_name.removeprefix(SHARED_PREFIX)
        if version != cache_tag(source.root):
            # Built from other inputs: its versions and digest would not be the ones recorded here.
            raise RunnerError("golden_stale", lima_name)
        built_at = self.now()
        with connect(self.dsn) as conn, build_lock(conn), conn.transaction():
            conn.execute("DELETE FROM runner.golden WHERE version = %s", (version,))
            conn.execute("UPDATE runner.golden SET state = 'retired' WHERE state = 'ready'")
            conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
                " state) VALUES (%s, %s, %s, %s, %s, %s, 'ready')",
                (version, lima_name, source.claude_version, source.guest_version, source.image_digest, built_at),
            )
        self.announce("image.adopt", {"version": version, "lima_name": lima_name, "state": "ready"})
        return Golden(
            version=version,
            lima_name=lima_name,
            claude_version=source.claude_version,
            guest_version=source.guest_version,
            image_digest=source.image_digest,
            built_at=built_at,
            state="ready",
        )

    # -- retire --------------------------------------------------------------------------------------------

    def retire(self, version: str) -> Golden:
        """Delete a golden that is not the current `ready` one and that no VM references."""
        with connect(self.dsn) as conn, build_lock(conn):
            row = conn.execute(f"SELECT {_COLUMNS} FROM runner.golden WHERE version = %s", (version,)).fetchone()
            if row is None:
                raise RunnerError("golden_unknown")
            golden = Golden(**row)
            current = ready_golden(conn)
            if current is not None and current.version == version:
                raise RunnerError("golden_current")
            if golden.state == "building":
                raise RunnerError("image_busy")
            if references(conn, version):
                raise RunnerError("golden_in_use")
            if self.lima.owns(golden.lima_name):
                self.lima.delete(golden.lima_name)
            conn.execute("DELETE FROM runner.golden WHERE version = %s", (version,))
            with contextlib.suppress(OSError):
                (self.records / f"{version}.json").unlink(missing_ok=True)
        return Golden(**{**row, "state": "retired"})
