"""Shared lifecycle for A38 local job adapters.

Work and artifacts live outside the repository. Subprocess argv lists are never
passed through a shell. Cleanup is bounded and ownership-aware.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
LOCK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
COMPOSE_SAFE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DOCKER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
REL_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+$")

BUILTIN_UNSET = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "NODE_OPTIONS"})
CLEANUP_BUDGET_S = 25
DIAGNOSTIC_TIMEOUT_S = 15
DEFAULT_LOCK_BUDGET_S = 3600
DEFAULT_LOCK_POLL_S = 15
DOCKER_HEAVY_LOCK = "docker-heavy"

BASE_PLACEHOLDERS = frozenset({"repo", "head", "base", "work", "artifacts", "project"})
COMMON_KEYS = frozenset({"unset", "unset_prefixes", "env", "lock", "npm", "postgres"})


class JobError(ValueError):
    """Invalid adapter configuration or hard lifecycle failure."""


class JobInterrupted(Exception):
    """Signal interrupted the adapter; carries the conventional exit status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"interrupted with status {status}")
        self.status = status


def _reject_nonfinite(name: str) -> None:
    raise JobError(f"JSON contains non-finite number: {name}")


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise JobError(f"JSON contains duplicate key: {key}")
        out[key] = value
    return out


def loads_strict_json(text: str) -> Any:
    """Parse JSON rejecting duplicate keys and NaN/Infinity."""
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite,
            object_pairs_hook=_object_pairs_no_duplicates,
        )
    except JobError:
        raise
    except json.JSONDecodeError as exc:
        raise JobError(f"JSON is invalid: {exc.msg}") from exc


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JobError(f"{label} must be an object")
    return value


def require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise JobError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise JobError(f"{label} must not contain NUL")
    return value


def require_str_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise JobError(f"{label} must be an array of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        if item == "" or "\x00" in item:
            raise JobError(f"{label}[{index}] must be a non-empty string without NUL")
        out.append(item)
    return out


def require_argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise JobError(f"{label} must be a non-empty argv list")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item == "" or "\x00" in item:
            raise JobError(f"{label}[{index}] must be a non-empty string without NUL")
        out.append(item)
    return out


def require_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise JobError(f"{label} must be finite")
    return number


def require_env_name(value: Any, label: str) -> str:
    name = require_str(value, label)
    if ENV_NAME_RE.fullmatch(name) is None:
        raise JobError(f"{label} is not a valid environment variable name")
    return name


def require_rel_path(value: Any, label: str) -> str:
    path = require_str(value, label)
    if path.startswith("/") or path.startswith("~") or "\\" in path:
        raise JobError(f"{label} must be a relative path")
    parts = Path(path).parts
    if any(part in ("", ".", "..") for part in parts):
        raise JobError(f"{label} must not contain . or .. segments")
    if REL_PATH_RE.fullmatch(path.replace("\\", "/")) is None:
        raise JobError(f"{label} is not a safe relative path")
    return path


def reject_unknown_keys(obj: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise JobError(f"{label} has unknown keys: {', '.join(sorted(extra))}")


def validate_placeholders(
    text: str,
    *,
    label: str,
    allow_companion: bool = False,
    allow_compose_token: bool = False,
) -> None:
    """Reject unknown placeholders without creating a runtime or temp dirs."""
    if not isinstance(text, str) or "\x00" in text:
        raise JobError(f"{label} must be a NUL-free string")
    consumed: list[tuple[int, int]] = []
    for match in PLACEHOLDER_RE.finditer(text):
        consumed.append(match.span())
        key = match.group(1)
        if key in BASE_PLACEHOLDERS:
            continue
        if key == "companion":
            if allow_companion:
                continue
            raise JobError(f"{label} uses unsupported placeholder {{companion}}")
        if key == "compose":
            # Only the exact argv token "{compose}" is valid, and only as argv[0].
            if allow_compose_token and match.group(0) == text == "{compose}":
                continue
            raise JobError(f"{label} uses {{compose}} outside the first argv element")
        if key.startswith("image:"):
            name = key[len("image:") :]
            if IMAGE_NAME_RE.fullmatch(name) is None:
                raise JobError(f"{label} has invalid image placeholder name: {name}")
            continue
        raise JobError(f"{label} has unknown placeholder: {{{key}}}")
    remainder = list(text)
    for start, end in consumed:
        remainder[start:end] = " " * (end - start)
    if "{" in remainder or "}" in remainder:
        raise JobError(f"{label} contains a malformed or unsupported placeholder")


def validate_argv_placeholders(
    argv: Sequence[str],
    *,
    label: str,
    allow_companion: bool = False,
    allow_compose_token: bool = False,
) -> None:
    for index, part in enumerate(argv):
        if part == "{compose}":
            if not allow_compose_token or index != 0:
                raise JobError(f"{label}[{index}]: {{compose}} is only allowed as the first argv element")
            continue
        validate_placeholders(
            part,
            label=f"{label}[{index}]",
            allow_companion=allow_companion,
            allow_compose_token=False,
        )


def _git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout_s: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_s,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise JobError(f"git {' '.join(args)} timed out") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise JobError(f"git {' '.join(args)} failed: {detail or completed.returncode}")
    return completed


def repo_root_from_cwd(cwd: Path | None = None) -> Path:
    base = Path.cwd() if cwd is None else Path(cwd)
    completed = _git(["rev-parse", "--show-toplevel"], cwd=base, timeout_s=30)
    root = Path(completed.stdout.strip())
    if not root.is_dir():
        raise JobError("cannot resolve git worktree root")
    return root.resolve()


def require_full_commit(value: str, label: str) -> str:
    if COMMIT_RE.fullmatch(value) is None:
        raise JobError(f"{label} must be a full 40-character lowercase commit SHA")
    return value


def require_docker_id(value: str, label: str) -> str:
    if DOCKER_ID_RE.fullmatch(value) is None:
        raise JobError(f"{label} did not return a Docker container/image id")
    return value


def parse_docker_host_port(output: str, label: str) -> str:
    for line in output.splitlines():
        if ":" not in line:
            continue
        candidate = line.rsplit(":", 1)[-1].strip().strip("\r")
        if candidate.isascii() and candidate.isdigit():
            port = int(candidate)
            if 1 <= port <= 65535:
                return candidate
    raise JobError(f"failed to resolve a valid loopback host port for {label}")


def verify_runner_commits(root: Path, head: str, base: str) -> tuple[str, str]:
    head = require_full_commit(head, "A38_HEAD_SHA")
    base = require_full_commit(base, "A38_BASE_SHA")
    actual = _git(["rev-parse", "HEAD"], cwd=root, timeout_s=30).stdout.strip()
    if actual != head:
        raise JobError(f"A38_HEAD_SHA {head} does not match repository HEAD {actual}")
    verified_base = _git(
        ["rev-parse", "--verify", f"{base}^{{commit}}"], cwd=root, timeout_s=30
    ).stdout.strip()
    if verified_base != base:
        raise JobError(f"A38_BASE_SHA must be a full commit identity; resolved {verified_base}")
    return head, base


def _unique_run_id(work_dir: Path) -> str:
    encoded = hashlib.sha256(work_dir.name.encode("utf-8")).hexdigest()[:24]
    run_id = f"a38-{encoded}"
    if COMPOSE_SAFE_RE.fullmatch(run_id) is None:
        raise JobError("internal run id is not Compose-safe")
    return run_id


def _ensure_outside_repo(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return resolved
    raise JobError(f"{label} must be outside the repository")


def expand_placeholders(
    text: str,
    *,
    mapping: Mapping[str, str],
    images: Mapping[str, str] | None = None,
) -> str:
    images = images or {}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in mapping:
            value = mapping[key]
            if key == "companion" and value == "":
                raise JobError("placeholder {companion} is not available in this adapter")
            return value
        if key.startswith("image:"):
            name = key[len("image:") :]
            if IMAGE_NAME_RE.fullmatch(name) is None:
                raise JobError(f"invalid image placeholder name: {name}")
            if name not in images:
                raise JobError(f"unknown image placeholder: {{{key}}}")
            return images[name]
        raise JobError(f"unknown placeholder: {{{key}}}")

    return PLACEHOLDER_RE.sub(replace, text)


def expand_argv(
    argv: Sequence[str],
    *,
    mapping: Mapping[str, str],
    images: Mapping[str, str] | None = None,
    allow_compose_token: bool = False,
) -> list[str]:
    if not argv:
        raise JobError("argv must be a non-empty list")
    out: list[str] = []
    for index, part in enumerate(argv):
        if not isinstance(part, str) or part == "" or "\x00" in part:
            raise JobError("argv entries must be non-empty strings without NUL")
        if part == "{compose}":
            if not allow_compose_token or index != 0:
                raise JobError("{compose} is only allowed as the first argv element")
            out.append(part)
            continue
        out.append(expand_placeholders(part, mapping=mapping, images=images))
    return out


def resolve_artifacts_path(path_text: str, artifacts: Path) -> Path:
    """Resolve an optional stdout path and require it stay under artifacts."""
    path = Path(path_text)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (artifacts / path).resolve()
    try:
        resolved.relative_to(artifacts.resolve())
    except ValueError as exc:
        raise JobError("stdout path must stay under the artifacts directory") from exc
    if resolved == artifacts.resolve():
        raise JobError("stdout path must be a file under artifacts, not the directory itself")
    return resolved


@dataclass
class CommonConfig:
    unset: list[str] = field(default_factory=list)
    unset_prefixes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    lock: str | None = None
    npm: dict[str, Any] | None = None
    postgres: dict[str, Any] | None = None


def parse_common_config(
    raw: Mapping[str, Any],
    *,
    allow_companion: bool = False,
) -> CommonConfig:
    cfg = CommonConfig()
    if "unset" in raw:
        cfg.unset = [require_env_name(item, "unset[]") for item in require_str_list(raw["unset"], "unset")]
    if "unset_prefixes" in raw:
        prefixes = require_str_list(raw["unset_prefixes"], "unset_prefixes")
        for prefix in prefixes:
            if not prefix or any(ch in prefix for ch in ("=", "\n", "\r", "\x00")):
                raise JobError("unset_prefixes entries must be plain prefixes")
        cfg.unset_prefixes = prefixes
    if "env" in raw:
        env_obj = require_mapping(raw["env"], "env")
        env: dict[str, str] = {}
        for key, value in env_obj.items():
            name = require_env_name(key, "env key")
            if not isinstance(value, str) or "\x00" in value:
                raise JobError(f"env.{name} must be a string without NUL")
            validate_placeholders(
                value,
                label=f"env.{name}",
                allow_companion=allow_companion,
            )
            env[name] = value
        cfg.env = env
    if "lock" in raw:
        lock = require_str(raw["lock"], "lock")
        if LOCK_NAME_RE.fullmatch(lock) is None:
            raise JobError("invalid lock name")
        cfg.lock = lock
    if "npm" in raw:
        npm = require_mapping(raw["npm"], "npm")
        reject_unknown_keys(npm, frozenset({"node_major", "canaries"}), "npm")
        if "node_major" not in npm or "canaries" not in npm:
            raise JobError("npm requires node_major and canaries")
        major = require_finite_number(npm["node_major"], "npm.node_major")
        if major != int(major) or int(major) < 1:
            raise JobError("npm.node_major must be a positive integer")
        canaries = [
            require_rel_path(item, "npm.canaries[]")
            for item in require_str_list(npm["canaries"], "npm.canaries")
        ]
        if not canaries:
            raise JobError("npm.canaries must be non-empty")
        cfg.npm = {"node_major": int(major), "canaries": canaries}
    if "postgres" in raw:
        pg = require_mapping(raw["postgres"], "postgres")
        required = frozenset({"image", "user", "password", "database", "url_env", "port_env"})
        reject_unknown_keys(pg, required, "postgres")
        missing = required - set(pg)
        if missing:
            raise JobError(f"postgres missing keys: {', '.join(sorted(missing))}")
        cfg.postgres = {
            "image": require_str(pg["image"], "postgres.image"),
            "user": require_str(pg["user"], "postgres.user"),
            "password": require_str(pg["password"], "postgres.password"),
            "database": require_str(pg["database"], "postgres.database"),
            "url_env": require_env_name(pg["url_env"], "postgres.url_env"),
            "port_env": require_env_name(pg["port_env"], "postgres.port_env"),
        }
    return cfg


def npm_lock_name(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"node-modules-install-{digest}"


class JobRuntime:
    """Owns scratch dirs, locks, env scoping, and bounded subprocesses."""

    def __init__(
        self,
        *,
        adapter: str,
        common: CommonConfig,
        cwd: Path | None = None,
        lock_root: Path | None = None,
        environ: Mapping[str, str] | None = None,
        allow_companion: bool = False,
    ) -> None:
        self.adapter = adapter
        self.common = common
        self.allow_companion = allow_companion
        self.interrupted = False
        self.interrupt_status = 0
        self._cleanup_deadline: float | None = None
        self._cleaning = False
        self._group_cleanup_uncertain = False
        self._held_locks: list[str] = []
        self._pg_container: str | None = None
        self._owned_images: list[str] = []
        self._job_cleanup: Callable[[int], int] | None = None
        self._child: subprocess.Popen[Any] | None = None
        self._child_pgid: int | None = None
        self._prev_signals: dict[int, Any] = {}
        self.images: dict[str, str] = {}
        self.env: dict[str, str] = {}
        self.mapping: dict[str, str] = {}
        self.work: Path | None = None
        self.artifacts: Path | None = None
        self.source_env = dict(environ if environ is not None else os.environ)

        head = self.source_env.get("A38_HEAD_SHA", "")
        base = self.source_env.get("A38_BASE_SHA", "")
        if not head or not base:
            raise JobError("A38_HEAD_SHA and A38_BASE_SHA are required")

        self.root = repo_root_from_cwd(cwd)
        self.head, self.base = verify_runner_commits(self.root, head, base)

        work_dir: Path | None = None
        artifacts_dir: Path | None = None
        try:
            work_dir = Path(tempfile.mkdtemp(prefix="a38-work.", dir=tempfile.gettempdir()))
            artifacts_dir = Path(
                tempfile.mkdtemp(prefix="a38-artifacts.", dir=tempfile.gettempdir())
            )
            self.work = _ensure_outside_repo(work_dir, self.root, "work directory")
            self.artifacts = _ensure_outside_repo(
                artifacts_dir, self.root, "artifacts directory"
            )
            self.run_id = _unique_run_id(self.work)
            self.project = self.run_id
            if COMPOSE_SAFE_RE.fullmatch(self.project) is None:
                raise JobError("project name is not Compose-safe")

            if lock_root is not None:
                candidate = Path(lock_root)
            else:
                override = self.source_env.get("A38_LOCK_ROOT", "").strip()
                configured_home = self.source_env.get("HOME", "").strip()
                home = Path(configured_home) if configured_home else Path.home()
                candidate = Path(override) if override else home / ".a38-locks"
            self.lock_root = _ensure_outside_repo(candidate, self.root, "lock root")
            self.lock_root.mkdir(parents=True, exist_ok=True)
            self.lock_poll_s = float(self.source_env.get("A38_LOCK_POLL_SECONDS", DEFAULT_LOCK_POLL_S))
            if not math.isfinite(self.lock_poll_s) or self.lock_poll_s <= 0:
                raise JobError("A38_LOCK_POLL_SECONDS must be a positive finite number")

            print(f"a38: artifacts: {self.artifacts}", flush=True)

            self.mapping = {
                "repo": str(self.root),
                "head": self.head,
                "base": self.base,
                "work": str(self.work),
                "artifacts": str(self.artifacts),
                "project": self.project,
            }
            if allow_companion:
                # Filled by compose after the pristine snapshot is ready.
                self.mapping["companion"] = ""
            self.env = self._scoped_env(self.source_env)
        except Exception:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
            if artifacts_dir is not None:
                shutil.rmtree(artifacts_dir, ignore_errors=True)
            self.work = None
            self.artifacts = None
            raise

    def image_tag(self, name: str) -> str:
        if IMAGE_NAME_RE.fullmatch(name) is None or len(name) > 64:
            raise JobError(f"invalid image name: {name}")
        if name not in self.images:
            self.images[name] = f"a38-{name}:{self.run_id}"
        return self.images[name]

    def ensure_image_placeholders(self, texts: Sequence[str]) -> None:
        for text in texts:
            for match in PLACEHOLDER_RE.finditer(text):
                key = match.group(1)
                if key.startswith("image:"):
                    self.image_tag(key[len("image:") :])

    def _scoped_env(self, source: Mapping[str, str]) -> dict[str, str]:
        env = dict(source)
        for key in list(env):
            if key in BUILTIN_UNSET or key in self.common.unset:
                env.pop(key, None)
                continue
            for prefix in self.common.unset_prefixes:
                if key.startswith(prefix):
                    env.pop(key, None)
                    break
        self.ensure_image_placeholders(list(self.common.env.values()))
        for key, value in self.common.env.items():
            if self.allow_companion and any(
                match.group(1) == "companion" for match in PLACEHOLDER_RE.finditer(value)
            ):
                # Compose fills this after verifying and archiving the companion.
                continue
            env[key] = expand_placeholders(value, mapping=self.mapping, images=self.images)
        env["A38_HEAD_SHA"] = self.head
        env["A38_BASE_SHA"] = self.base
        return env

    def refresh_configured_env(self) -> None:
        """Re-expand configured env after mapping/images updates (compose)."""
        self.ensure_image_placeholders(list(self.common.env.values()))
        for key, value in self.common.env.items():
            self.env[key] = expand_placeholders(value, mapping=self.mapping, images=self.images)

    def set_job_cleanup(self, hook: Callable[[int], int]) -> None:
        self._job_cleanup = hook

    def track_image(self, image: str) -> None:
        if image and image not in self._owned_images:
            self._owned_images.append(image)

    @property
    def owned_images(self) -> list[str]:
        return list(self._owned_images)

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, status in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            self._prev_signals[sig] = signal.getsignal(sig)

            def _handler(signum: int, frame: Any, status: int = status) -> None:
                self._on_signal(status)

            signal.signal(sig, _handler)

    def restore_signal_handlers(self) -> None:
        for sig, previous in self._prev_signals.items():
            signal.signal(sig, previous)
        self._prev_signals.clear()

    def _begin_cleanup_deadline(self) -> None:
        if self._cleanup_deadline is None:
            self._cleanup_deadline = time.monotonic() + CLEANUP_BUDGET_S

    def _on_signal(self, status: int) -> None:
        self.interrupted = True
        self.interrupt_status = status
        self._begin_cleanup_deadline()
        try:
            self._kill_foreground_within_deadline()
        except OSError as exc:
            # Signal provenance is authoritative even if a platform-level
            # ownership operation fails. Cleanup uncertainty remains hard.
            self._record_group_cleanup_error(
                "signal termination", self._child_pgid, exc
            )
        if self._cleaning:
            # Cap already started; stop diagnostics by remaining deadline only.
            return
        raise JobInterrupted(status)

    def _remaining_cleanup_s(self) -> float:
        if self._cleanup_deadline is None:
            return float(CLEANUP_BUDGET_S)
        return max(0.0, self._cleanup_deadline - time.monotonic())

    def _kill_foreground_within_deadline(self) -> None:
        proc = self._child
        pgid = self._child_pgid
        if proc is None and pgid is None:
            return
        budget = self._remaining_cleanup_s()
        if budget <= 0:
            return
        self._terminate_owned_group(
            proc,
            pgid=pgid,
            budget_s=budget,
            stage="signal termination",
        )

    def _record_group_cleanup_error(
        self, stage: str, pgid: int | None, error: BaseException | None = None
    ) -> None:
        self._group_cleanup_uncertain = True
        detail = "owned process group still present"
        if error is not None:
            detail = f"{type(error).__name__}: {error}"
        print(
            f"a38: process-group cleanup uncertain during {stage} "
            f"(pgid={pgid if pgid is not None else '?'}): {detail}",
            file=sys.stderr,
        )

    def _terminate_owned_group(
        self,
        proc: subprocess.Popen[Any] | None,
        *,
        pgid: int | None,
        budget_s: float,
        stage: str,
    ) -> bool:
        try:
            gone = _terminate_process_group(proc, pgid=pgid, budget_s=budget_s)
        except OSError as exc:
            self._record_group_cleanup_error(stage, pgid, exc)
            return False
        if not gone:
            self._record_group_cleanup_error(stage, pgid)
        return gone

    def bounded(
        self,
        timeout_s: float,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
        stdin: Any = subprocess.DEVNULL,
    ) -> subprocess.CompletedProcess[str]:
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise JobError("bounded timeout must be positive and finite")
        if self._cleanup_deadline is not None:
            remaining = self._remaining_cleanup_s()
            if remaining <= 0:
                return subprocess.CompletedProcess(list(argv), 124, "", "cleanup deadline exceeded")
            timeout_s = min(timeout_s, remaining)
        return self.run_argv(
            argv,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            check=False,
        )

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        stdout: Any = None,
        stderr: Any = None,
        stdin: Any = subprocess.DEVNULL,
        check: bool = False,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not argv or any(not isinstance(part, str) or part == "" or "\x00" in part for part in argv):
            raise JobError("argv must be a non-empty list of NUL-free strings")
        if self._cleanup_deadline is not None and self._remaining_cleanup_s() <= 0:
            return subprocess.CompletedProcess(list(argv), 124, "", "cleanup deadline exceeded")
        popen_kwargs: dict[str, Any] = {
            "args": list(argv),
            "cwd": str(cwd) if cwd is not None else str(self.root),
            "env": dict(env) if env is not None else dict(self.env),
            "stdin": stdin,
            "start_new_session": True,
        }
        if text:
            popen_kwargs["text"] = True
            popen_kwargs["stdout"] = subprocess.PIPE if stdout is None else stdout
            popen_kwargs["stderr"] = subprocess.PIPE if stderr is None else stderr
        else:
            popen_kwargs["stdout"] = stdout
            popen_kwargs["stderr"] = stderr
        proc = subprocess.Popen(**popen_kwargs)  # noqa: S603
        self._child = proc
        self._child_pgid = proc.pid
        try:
            command_deadline = (
                time.monotonic() + timeout_s if timeout_s is not None else None
            )
            while True:
                if command_deadline is None:
                    poll_timeout = 0.2
                else:
                    remaining = command_deadline - time.monotonic()
                    if remaining <= 0:
                        budget = (
                            self._remaining_cleanup_s()
                            if self._cleanup_deadline is not None
                            else 7.0
                        )
                        self._terminate_owned_group(
                            proc,
                            pgid=proc.pid,
                            budget_s=max(0.0, budget),
                            stage="command timeout",
                        )
                        out, err = _drain_popen(proc, self._remaining_cleanup_s())
                        completed = subprocess.CompletedProcess(
                            list(argv), 124, out, err
                        )
                        break
                    poll_timeout = min(0.2, remaining)
                try:
                    out, err = proc.communicate(timeout=poll_timeout)
                except subprocess.TimeoutExpired:
                    if proc.poll() is None:
                        continue
                    # The argv leader exited but descendants may still own its
                    # process group and inherited output descriptors. Clean the
                    # whole owned group before allowing the adapter to return.
                    leader_code = _returncode(proc.returncode)
                    budget = (
                        self._remaining_cleanup_s()
                        if self._cleanup_deadline is not None
                        else 7.0
                    )
                    group_gone = self._terminate_owned_group(
                        proc,
                        pgid=proc.pid,
                        budget_s=max(0.0, budget),
                        stage="leader-exit cleanup",
                    )
                    out, err = _drain_popen(proc, self._remaining_cleanup_s())
                    code = leader_code if group_gone or leader_code != 0 else 125
                    completed = subprocess.CompletedProcess(list(argv), code, out, err)
                    break
                else:
                    code = _returncode(proc.returncode)
                    group_gone = True
                    if _process_group_alive(proc.pid):
                        budget = (
                            self._remaining_cleanup_s()
                            if self._cleanup_deadline is not None
                            else 7.0
                        )
                        group_gone = self._terminate_owned_group(
                            proc,
                            pgid=proc.pid,
                            budget_s=max(0.0, budget),
                            stage="command completion cleanup",
                        )
                    if not group_gone and code == 0:
                        code = 125
                    completed = subprocess.CompletedProcess(
                        list(argv), code, out or "", err or ""
                    )
                    break
        finally:
            if self._child is proc:
                self._child = None
                self._child_pgid = None
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise JobError(f"command failed ({completed.returncode}): {' '.join(argv)}: {detail}")
        return completed

    def lock_acquire(self, name: str, budget_s: float = DEFAULT_LOCK_BUDGET_S) -> None:
        if LOCK_NAME_RE.fullmatch(name) is None:
            raise JobError(f"invalid lock name: {name}")
        if not math.isfinite(budget_s) or budget_s <= 0:
            raise JobError("lock budget must be positive and finite")
        directory = self.lock_root / f"{name}.lock"
        holder = directory / "holder"
        started = time.monotonic()
        while True:
            try:
                directory.mkdir(exist_ok=False)
            except FileExistsError:
                age = time.monotonic() - started
                if age >= budget_s:
                    raise JobError(
                        f"lock {directory} not acquired within {int(budget_s)}s; "
                        "inspect its holder and active workloads manually before removing an abandoned lock"
                    ) from None
                print(f"a38: waiting for lock {name} ({int(age)}s/{int(budget_s)}s)", flush=True)
                time.sleep(self.lock_poll_s)
                continue
            try:
                holder.write_text(
                    f"pid={os.getpid()}\nrun_id={self.run_id}\njob={self.adapter}\n"
                    f"since={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                try:
                    directory.rmdir()
                except OSError:
                    pass
                raise JobError(f"cannot initialize lock {directory}: {exc}") from exc
            self._held_locks.append(name)
            print(f"a38: acquired lock {name}", flush=True)
            return

    def lock_release(self, name: str) -> None:
        if LOCK_NAME_RE.fullmatch(name) is None:
            raise JobError(f"invalid lock name: {name}")
        directory = self.lock_root / f"{name}.lock"
        holder = directory / "holder"
        if not directory.exists():
            raise JobError(f"owned lock {name} disappeared before release")
        try:
            text = holder.read_text(encoding="utf-8") if holder.is_file() else ""
        except OSError as exc:
            raise JobError(f"cannot read lock ownership for {name}: {exc}") from exc
        pid_line = ""
        run_ok = False
        for line in text.splitlines():
            if line.startswith("pid="):
                pid_line = line.split("=", 1)[1]
            if line == f"run_id={self.run_id}":
                run_ok = True
        if pid_line != str(os.getpid()) or not run_ok:
            raise JobError(
                f"refusing to release lock {name} held by pid {pid_line or '?'} (this is {os.getpid()})"
            )
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise JobError(f"cannot release owned lock {name}: {exc}") from exc
        self._held_locks = [item for item in self._held_locks if item != name]
        print(f"a38: released lock {name}", flush=True)

    def ensure_node_modules(self) -> None:
        if self.common.npm is None:
            return
        major = int(self.common.npm["node_major"])
        canaries: list[str] = list(self.common.npm["canaries"])
        node = shutil.which("node", path=self.env.get("PATH"))
        npm = shutil.which("npm", path=self.env.get("PATH"))
        if not node or not npm:
            raise JobError("required command not found: node/npm")
        reported = self.run_argv(
            [node, "-p", 'process.versions.node.split(".")[0]'],
            check=True,
        ).stdout.strip()
        if reported != str(major):
            raise JobError(f"Node major must be {major}; got {reported}")
        lock_file = self.root / "package-lock.json"
        if not lock_file.is_file():
            raise JobError("package-lock.json missing at repository root")
        lock_name = npm_lock_name(self.root)
        self.lock_acquire(lock_name, budget_s=900)
        try:
            digest = hashlib.sha256(lock_file.read_bytes()).hexdigest()
            version = self.run_argv([node, "-p", "process.version"], check=True).stdout.strip()
            arch = os.uname().machine
            expect = f"{digest}-node{version}-{arch}"
            stamp = self.root / "node_modules" / ".a38-nm-ok"

            def canaries_ok() -> bool:
                node_modules = self.root / "node_modules"
                if node_modules.is_symlink() or not node_modules.is_dir():
                    return False
                resolved_root = node_modules.resolve()
                for rel in canaries:
                    candidate = node_modules / rel
                    if candidate.is_symlink() or not candidate.is_file():
                        return False
                    try:
                        candidate.resolve().relative_to(resolved_root)
                    except ValueError:
                        return False
                return True

            usable = (
                (self.root / "node_modules").is_dir()
                and stamp.is_file()
                and stamp.read_text(encoding="utf-8").strip() == expect
                and canaries_ok()
            )
            if usable:
                print(f"a38: node_modules stamp ok ({expect})", flush=True)
                return
            print("a38: npm ci (lock/node stamp miss or unusable tree)", flush=True)
            nm = self.root / "node_modules"
            if nm.exists():
                shutil.rmtree(nm)
            completed = self.run_argv([npm, "ci", "--prefer-offline"], check=False)
            if completed.returncode != 0:
                raise JobError(f"npm ci failed with exit {completed.returncode}")
            if not canaries_ok():
                raise JobError("npm ci completed but configured canaries are missing under node_modules")
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(expect + "\n", encoding="utf-8")
        finally:
            self.lock_release(lock_name)

    def postgres_start(self) -> None:
        if self.common.postgres is None:
            return
        pg = self.common.postgres
        docker = shutil.which("docker", path=self.env.get("PATH"))
        if not docker:
            raise JobError("required command not found: docker")
        name = f"a38-pg-{self.adapter}-{self.run_id}"
        created = self.run_argv(
            [
                docker,
                "create",
                "--name",
                name,
                "-e",
                f"POSTGRES_PASSWORD={pg['password']}",
                "-e",
                f"POSTGRES_USER={pg['user']}",
                "-e",
                f"POSTGRES_DB={pg['database']}",
                "-p",
                "127.0.0.1::5432",
                pg["image"],
            ],
            check=False,
        )
        if created.returncode != 0:
            raise JobError(f"failed to create postgres container {name}")
        container_id = require_docker_id(created.stdout.strip(), "docker create")
        self._pg_container = container_id
        started = self.run_argv([docker, "start", container_id], check=False)
        if started.returncode != 0:
            raise JobError(f"failed to start postgres container {container_id}")
        port_result = self.run_argv(
            [docker, "port", container_id, "5432/tcp"], check=False
        )
        if port_result.returncode != 0:
            raise JobError(f"failed to resolve host port for {container_id}")
        port = parse_docker_host_port(port_result.stdout, "Postgres")
        self.env[pg["port_env"]] = port
        self.env[pg["url_env"]] = (
            f"postgres://{pg['user']}:{pg['password']}@127.0.0.1:{port}/{pg['database']}"
        )
        ready = False
        for _ in range(30):
            probe = self.run_argv(
                [docker, "exec", container_id, "pg_isready", "-U", pg["user"]],
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise JobError(f"postgres {container_id} not ready")
        print(f"a38: postgres ready on 127.0.0.1:{port}", flush=True)

    def postgres_stop(self) -> None:
        if not self._pg_container:
            return
        docker = shutil.which("docker", path=self.env.get("PATH"))
        if not docker:
            print(
                f"a38: warning: could not remove owned Postgres container {self._pg_container}; "
                "inspect it on this host",
                file=sys.stderr,
            )
            return
        completed = self.bounded(20, [docker, "rm", "-f", self._pg_container])
        if completed.returncode == 0:
            print(f"a38: removed postgres {self._pg_container}", flush=True)
            self._pg_container = None
        else:
            print(
                f"a38: warning: could not remove owned Postgres container {self._pg_container}; "
                "inspect it on this host",
                file=sys.stderr,
            )

    def isolate_docker_config(self) -> None:
        docker = shutil.which("docker", path=self.env.get("PATH"))
        if not docker:
            raise JobError("required command not found: docker")
        previous_config = self.env.get("DOCKER_CONFIG") or str(Path.home() / ".docker")
        if self.env.get("DOCKER_CONTEXT"):
            context = self.env["DOCKER_CONTEXT"]
            endpoint = self.run_argv(
                [docker, "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"],
                check=True,
            ).stdout.strip()
        elif self.env.get("DOCKER_HOST"):
            endpoint = self.env["DOCKER_HOST"]
        else:
            context = self.run_argv([docker, "context", "show"], check=True).stdout.strip()
            endpoint = self.run_argv(
                [docker, "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"],
                check=True,
            ).stdout.strip()
        if not endpoint.startswith("unix:///"):
            raise JobError(
                "A38 adapters require a local Docker Unix socket; remote/TLS/SSH contexts "
                "are unsupported by isolated configuration. Select a local context or local DOCKER_HOST."
            )
        self.env["DOCKER_HOST"] = endpoint
        for key in ("DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "BUILDX_BUILDER"):
            self.env.pop(key, None)
        assert self.work is not None
        config_dir = self.work / "docker-config"
        plugins = config_dir / "cli-plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        self.env["DOCKER_CONFIG"] = str(config_dir)
        fallbacks = [
            Path(previous_config) / "cli-plugins",
            Path("/opt/homebrew/lib/docker/cli-plugins"),
            Path("/usr/local/lib/docker/cli-plugins"),
        ]
        for plug in ("docker-compose", "docker-buildx"):
            for base in fallbacks:
                src = base / plug
                try:
                    resolved = src.resolve(strict=True)
                    if not resolved.is_file() or not os.access(resolved, os.X_OK):
                        continue
                except (OSError, RuntimeError):
                    continue
                target = plugins / plug
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(resolved)
                break

    def acquire_configured_lock(self, default: str | None = None) -> None:
        name = self.common.lock if self.common.lock is not None else default
        if name:
            self.lock_acquire(name)

    def cleanup(self, primary_status: int) -> int:
        if self._cleaning:
            # Re-entrant signal path: deadline already active; do not restart work.
            self._kill_foreground_within_deadline()
            return primary_status if primary_status != 0 else 1
        self._cleaning = True
        if self.interrupted:
            self._begin_cleanup_deadline()
        result = primary_status
        try:
            if self._job_cleanup is not None:
                try:
                    hook_status = int(self._job_cleanup(result))
                except JobError as exc:
                    print(f"a38: {exc}", file=sys.stderr)
                    hook_status = 1
                except OSError as exc:
                    print(f"a38: cleanup OSError: {exc}", file=sys.stderr)
                    hook_status = 1
                except Exception as exc:  # noqa: BLE001
                    print(f"a38: cleanup hook failed: {type(exc).__name__}", file=sys.stderr)
                    hook_status = 1
                if hook_status != 0 and result == 0:
                    result = 1
            try:
                self.postgres_stop()
            except (JobError, OSError) as exc:
                print(f"a38: warning: Postgres cleanup failed: {exc}", file=sys.stderr)
            for name in list(self._held_locks):
                try:
                    if self._cleanup_deadline is not None and self._remaining_cleanup_s() <= 0:
                        raise JobError("cleanup deadline exceeded before lock release")
                    self.lock_release(name)
                except JobError as exc:
                    print(f"a38: {exc}", file=sys.stderr)
                    if result == 0:
                        result = 1
            if self._group_cleanup_uncertain and result == 0:
                result = 1
        finally:
            if self.work is not None:
                shutil.rmtree(self.work, ignore_errors=True)
        return result


def _close_pipes(proc: subprocess.Popen[Any]) -> None:
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _returncode(value: int | None) -> int:
    code = -1 if value is None else value
    return 128 - code if code < 0 else code


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _drain_popen(proc: subprocess.Popen[Any], available_s: float) -> tuple[Any, Any]:
    timeout = min(0.25, max(0.01, available_s))
    try:
        out, err = proc.communicate(timeout=timeout)
        return out or "", err or ""
    except subprocess.TimeoutExpired:
        _close_pipes(proc)
        return "", ""


def _terminate_process_group(
    proc: subprocess.Popen[Any] | None,
    *,
    pgid: int | None,
    budget_s: float,
) -> bool:
    """TERM then KILL a process group within one bounded budget.

    The process-group liveness probe is independent of the leader, because a
    short-lived parent may leave descendants holding captured pipe descriptors.
    """
    target = pgid if pgid is not None else (proc.pid if proc is not None else None)
    if target is None:
        return True
    if budget_s <= 0:
        try:
            os.killpg(target, 0)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            print(
                f"a38: PermissionError during deadline-exhausted liveness probe "
                f"(pgid={target}): {exc}",
                file=sys.stderr,
            )
            raise
        return False
    deadline = time.monotonic() + budget_s
    term_deadline = min(deadline, time.monotonic() + 5.0)

    def signal_owned_group(sig: signal.Signals, operation: str) -> bool:
        try:
            os.killpg(target, sig)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            print(
                f"a38: PermissionError during {operation} (pgid={target}): {exc}",
                file=sys.stderr,
            )
            raise
        return True

    sent = signal_owned_group(signal.SIGTERM, "SIGTERM")
    if not sent:
        return True
    while time.monotonic() < term_deadline:
        if proc is not None:
            try:
                proc.poll()
            except PermissionError as exc:
                print(
                    f"a38: PermissionError during waitpid poll after SIGTERM "
                    f"(pgid={target}): {exc}",
                    file=sys.stderr,
                )
                raise
        try:
            os.killpg(target, 0)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            print(
                f"a38: PermissionError during liveness probe after SIGTERM "
                f"(pgid={target}): {exc}",
                file=sys.stderr,
            )
            raise
        time.sleep(0.05)

    sent = signal_owned_group(signal.SIGKILL, "SIGKILL")
    if not sent:
        return True
    kill_deadline = min(deadline, time.monotonic() + 2.0)
    while time.monotonic() < kill_deadline:
        if proc is not None:
            try:
                proc.poll()
            except PermissionError as exc:
                print(
                    f"a38: PermissionError during waitpid poll after SIGKILL "
                    f"(pgid={target}): {exc}",
                    file=sys.stderr,
                )
                raise
        try:
            os.killpg(target, 0)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            print(
                f"a38: PermissionError during liveness probe after SIGKILL "
                f"(pgid={target}): {exc}",
                file=sys.stderr,
            )
            raise
        time.sleep(0.02)

    try:
        leader_alive = proc is not None and proc.poll() is None
    except PermissionError as exc:
        print(
            f"a38: PermissionError during final waitpid poll (pgid={target}): {exc}",
            file=sys.stderr,
        )
        raise
    if leader_alive and proc is not None:
        try:
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass
        except PermissionError as exc:
            print(
                f"a38: PermissionError during final waitpid wait (pgid={target}): {exc}",
                file=sys.stderr,
            )
            raise
    try:
        os.killpg(target, 0)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        print(
            f"a38: PermissionError during final liveness probe (pgid={target}): {exc}",
            file=sys.stderr,
        )
        raise
    return False


def origin_matches_repository(origin: str, repository: str) -> bool:
    repository = repository.strip()
    if REPO_NAME_RE.fullmatch(repository) is None:
        return False
    owner, name = repository.split("/", 1)
    patterns = (
        f"https://github.com/{owner}/{name}",
        f"https://github.com/{owner}/{name}.git",
        f"git@github.com:{owner}/{name}.git",
        f"ssh://git@github.com/{owner}/{name}.git",
        f"ssh://git@github.com/{owner}/{name}",
        f"git@github.com:{owner}/{name}",
    )
    return origin.rstrip("/") in patterns or origin in patterns


def safe_join(root: Path, relative: str) -> Path:
    """Join a relative path under root; reject escapes and symlink escapes."""
    rel = require_rel_path(relative, "path")
    root_resolved = root.resolve()
    candidate = (root / rel)
    # Reject intermediate symlink escape before resolve follows it out.
    cursor = root_resolved
    for part in Path(rel).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            resolved = cursor.resolve()
            try:
                resolved.relative_to(root_resolved)
            except ValueError as exc:
                raise JobError(f"path escapes root via symlink: {relative}") from exc
        if not cursor.exists():
            break
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise JobError(f"path escapes root: {relative}") from exc
    return resolved


def run_lifecycle(
    *,
    adapter: str,
    common: CommonConfig,
    body: Callable[[JobRuntime], int],
    cwd: Path | None = None,
    lock_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    default_lock: str | None = None,
    allow_companion: bool = False,
) -> int:
    """Create runtime, run body, always cleanup. Returns process exit status."""
    runtime: JobRuntime | None = None
    status = 1
    try:
        runtime = JobRuntime(
            adapter=adapter,
            common=common,
            cwd=cwd,
            lock_root=lock_root,
            environ=environ,
            allow_companion=allow_companion,
        )
        runtime.install_signal_handlers()
        try:
            if common.npm is not None:
                runtime.ensure_node_modules()
            runtime.acquire_configured_lock(default=default_lock)
            if common.postgres is not None:
                runtime.postgres_start()
            status = int(body(runtime))
        except JobInterrupted as exc:
            status = exc.status
        except JobError as exc:
            print(f"a38: {exc}", file=sys.stderr)
            status = 1
        except OSError as exc:
            print(f"a38: OSError: {exc}", file=sys.stderr)
            status = 1
        except KeyboardInterrupt:
            status = 130
            if runtime is not None:
                runtime.interrupted = True
                runtime.interrupt_status = 130
                runtime._begin_cleanup_deadline()
    except JobError as exc:
        print(f"a38: {exc}", file=sys.stderr)
        status = 1
    except OSError as exc:
        print(f"a38: OSError: {exc}", file=sys.stderr)
        status = 1
    finally:
        if runtime is not None:
            try:
                status = runtime.cleanup(status)
            finally:
                runtime.restore_signal_handlers()
    return status
