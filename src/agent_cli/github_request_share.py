"""Device-wide sharing of coordinator GitHub login checks and assignee discovery.

Workers are separate processes, so an in-memory cache cannot keep a device
inside the GitHub REST rate limit. Login checks and assignee listings live in
separate files: a discovery request holds only the listing lock, so a busy
worker's login check is not stuck behind that HTTP call. ``flock`` is per
process and not reentrant. A second descriptor for the same file in this
process would block on the lock this call already holds. Nested callers of one
file therefore share this thread's document, and the outermost exit writes it
once. A failed write still drops that document, so the next call locks and
reads the file again.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coordinator_common import CoordinatorError, json_list, redact
from .runtime import Completed

SHARE_FILE = "github-assignment-share.json"
LOGIN_SHARE_FILE = "github-login-share.json"
_CONFIG_DIR = "GH_CONFIG_DIR="
_STATUS_RE = re.compile(r"(?m)^HTTP/\d+(?:\.\d+)? (\d{3})\b")
_HTTP_304_RE = re.compile(r"\bHTTP 304\b", re.IGNORECASE)
_ETAG_RE = re.compile(r"(?im)^etag:[ \t]*([^\r\n]+?)[ \t]*$")
# A stored validator is sent back as a header value, so it has to be one token.
_SAFE_ETAG = re.compile(r'(?:W/)?"[^"\\\s]+"')
_AUTH_MARKERS = (
    "http 401",
    "bad credentials",
    "requires authentication",
    "authentication failed",
    "gh auth login",
)

Runner = Callable[[list[str]], Completed]
_local = threading.local()


def wall_time() -> float:
    """Wall clock for the shared record. ``time.monotonic`` is not comparable across processes."""
    return time.time()


@dataclass
class _Share:
    home: Path
    document: dict[str, Any]
    dirty: bool


@dataclass(frozen=True)
class _Parsed:
    status: int
    issues: list[Any]
    etag: str | None


def authentication_failure(result: Completed) -> bool:
    """True for a failed command whose text says the GitHub login was rejected.

    HTTP 304 is not an authentication failure. HTTP 403 is not either: a 403 is
    often a scope or rate-limit response, and an exhausted rate limit is a
    separate concern from a login that must be checked again.
    """
    if result.returncode == 0:
        return False
    text = f"{result.stdout}\n{result.stderr}".casefold()
    return any(marker in text for marker in _AUTH_MARKERS)


def _config_dir(argv: list[str]) -> str | None:
    try:
        gh_at = argv.index("gh")
    except ValueError:
        gh_at = len(argv)
    found: str | None = None
    for item in argv[:gh_at]:
        if item.startswith(_CONFIG_DIR):
            found = item[len(_CONFIG_DIR) :]
    return found


def _login_key(argv: list[str]) -> str:
    found = _config_dir(argv)
    if found is not None:
        return found
    return json.dumps(argv, separators=(",", ":"))


def _read_document(home: Path, filename: str) -> dict[str, Any]:
    path = home / filename
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CoordinatorError(f"cannot read {filename}: {exc}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Unreadable JSON is a miss. The next successful write replaces it.
        # It is not a stored login.
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _write_document(home: Path, filename: str, document: dict[str, Any]) -> None:
    path = home / filename
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    replaced = False
    try:
        tmp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced and tmp.exists():
            tmp.unlink()


def _open_sessions() -> dict[str, _Share]:
    sessions = getattr(_local, "sessions", None)
    if not isinstance(sessions, dict):
        sessions = {}
        _local.sessions = sessions
    return sessions


@contextmanager
def _locked(home: Path, filename: str) -> Iterator[_Share]:
    sessions = _open_sessions()
    current = sessions.get(filename)
    if isinstance(current, _Share):
        if current.home != home:
            raise CoordinatorError(
                "GitHub request share is already open for another home"
            )
        yield current
        return
    home.mkdir(parents=True, exist_ok=True)
    handle = (home / f"{filename}.lock").open("a+")
    try:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        share = _Share(home=home, document=_read_document(home, filename), dirty=False)
        sessions[filename] = share
        try:
            yield share
        finally:
            try:
                if share.dirty:
                    _write_document(home, filename, share.document)
            finally:
                sessions.pop(filename, None)
    finally:
        handle.close()


def _age(at: object, now: float) -> float | None:
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return now - float(at)


def _fresh(at: object, now: float, bound: float, *, inclusive: bool) -> bool:
    age = _age(at, now)
    if age is None or age < 0:
        return False
    if inclusive:
        return age <= bound
    return age < bound


def note_authentication_failure(
    home: Path,
    argv: list[str],
    memory: dict[str, tuple[float, Completed]],
) -> None:
    """Drop the saved login so the next check calls GitHub."""
    key = _config_dir(argv)
    with _locked(home, LOGIN_SHARE_FILE) as share:
        logins = share.document
        if key is None:
            logins.clear()
            memory.clear()
        else:
            logins.pop(key, None)
            memory.pop(key, None)
        share.dirty = True


def _matches(entry: object, result: Completed) -> bool:
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("returncode") == result.returncode
        and entry.get("stdout") == result.stdout
        and entry.get("stderr") == result.stderr
    )


def _login_from_disk(entry: object, now: float, max_age_seconds: int) -> Completed | None:
    if not isinstance(entry, dict) or entry.get("returncode") != 0:
        return None
    stdout = entry.get("stdout")
    stderr = entry.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    if not _fresh(entry.get("at"), now, max_age_seconds, inclusive=True):
        return None
    return Completed(0, stdout, stderr)


def cached_account_login(
    runner: Runner,
    argv: list[str],
    *,
    home: Path,
    max_age_seconds: int,
    memory: dict[str, tuple[float, Completed]],
) -> Completed:
    """Return the cached ``gh api user`` result, or call ``runner`` inside the lock.

    A hit in this process returns the same ``Completed`` object. Another process
    reconstructs one from the device file. A failed verification is not stored.
    """
    key = _login_key(argv)
    now = wall_time()
    with _locked(home, LOGIN_SHARE_FILE) as share:
        logins = share.document
        entry = logins.get(key)
        hit = memory.get(key)
        if (
            hit is not None
            and _fresh(hit[0], now, max_age_seconds, inclusive=True)
            and isinstance(entry, dict)
            and _fresh(entry.get("at"), now, max_age_seconds, inclusive=True)
            and _matches(entry, hit[1])
        ):
            return hit[1]
        disk = _login_from_disk(entry, now, max_age_seconds)
        if disk is not None and isinstance(entry, dict):
            memory[key] = (float(entry["at"]), disk)
            return disk
        result = runner(argv)
        if result.returncode == 0:
            memory[key] = (now, result)
            logins[key] = {
                "at": now,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": 0,
            }
        else:
            memory.pop(key, None)
            logins.pop(key, None)
        share.dirty = True
        return result


def _command_failed(completed: Completed) -> None:
    detail = redact((completed.stderr or completed.stdout or "command failed").strip())
    raise CoordinatorError(detail or "command failed")


def _status_codes(text: str) -> list[int]:
    return [int(match) for match in _STATUS_RE.findall(text)]


def _safe_etag(value: str) -> str | None:
    stripped = value.strip()
    if _SAFE_ETAG.fullmatch(stripped) is None:
        return None
    return stripped


def _header_etag(headers: str) -> str | None:
    match = _ETAG_RE.search(headers)
    if match is None:
        return None
    return _safe_etag(match.group(1))


def _split_headers(stdout: str) -> tuple[str, str]:
    if "\r\n\r\n" in stdout:
        headers, body = stdout.split("\r\n\r\n", 1)
        return headers, body
    if "\n\n" in stdout:
        headers, body = stdout.split("\n\n", 1)
        return headers, body
    return stdout, ""


def _json_values(body: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    length = len(body)
    while index < length:
        while index < length and body[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        try:
            value, end = decoder.raw_decode(body, index)
        except json.JSONDecodeError:
            newline = body.find("\n", index)
            if newline < 0:
                break
            index = newline + 1
            continue
        values.append(value)
        index = end
    return values


def _issues_from_body(body: str) -> list[Any]:
    stripped = body.strip()
    if stripped == "":
        raise CoordinatorError("empty command output")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        data = None
    else:
        return json_list(data)
    values = _json_values(body)
    if not values:
        raise CoordinatorError("invalid JSON from command")
    if len(values) == 1:
        return json_list(values[0])
    flat: list[Any] = []
    for value in values:
        flat.extend(json_list(value))
    return flat


def _not_modified_without_headers(stdout: str, stderr: str) -> bool:
    if stdout.lstrip().startswith("HTTP/"):
        return False
    blob = f"{stdout}\n{stderr}"
    codes = _status_codes(blob)
    if codes and all(code == 304 for code in codes):
        return True
    return _HTTP_304_RE.search(blob) is not None


def _parse_completed(completed: Completed) -> _Parsed:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if _not_modified_without_headers(stdout, stderr):
        return _Parsed(304, [], _header_etag(stdout))
    stripped = stdout.lstrip()
    if not stripped.startswith("HTTP/"):
        if completed.returncode != 0:
            _command_failed(completed)
        raw = stripped.strip()
        if raw == "":
            raise CoordinatorError("empty command output")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CoordinatorError("invalid JSON from command") from exc
        return _Parsed(200, json_list(data), None)
    headers, body = _split_headers(stripped)
    codes = _status_codes(headers) or _status_codes(stderr)
    if not codes and _HTTP_304_RE.search(f"{stdout}\n{stderr}"):
        codes = [304]
    if not codes:
        _command_failed(completed)
    if all(code == 304 for code in codes):
        return _Parsed(304, [], _header_etag(headers))
    if completed.returncode != 0 or any(code != 200 for code in codes):
        _command_failed(completed)
    return _Parsed(200, _issues_from_body(body), _header_etag(headers))


def _run(runner: Runner, argv: list[str]) -> Completed:
    try:
        return runner(argv)
    except OSError as exc:
        raise CoordinatorError(f"command unavailable: {exc}") from exc


def _assignment_key(login: str, repo: str) -> str:
    return f"{login.casefold()}\n{repo.casefold()}"


def _assignment_argv(repo: str, login: str, etag: str | None) -> list[str]:
    argv = ["gh", "api", "--include", "--paginate", "--slurp"]
    if etag is not None:
        argv.extend(["-H", f"If-None-Match: {etag}"])
    argv.append(f"repos/{repo}/issues?assignee={login}&state=open&per_page=100")
    return argv


def _stored_etag(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    etag = entry.get("etag")
    if not isinstance(etag, str):
        return None
    return _safe_etag(etag)


def _stored_issues(entry: object, now: float, poll_seconds: int) -> list[Any] | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("issues"), list):
        return None
    if not _fresh(entry.get("at"), now, poll_seconds, inclusive=False):
        return None
    return entry["issues"]


def shared_assigned_issues(
    home: Path,
    runner: Runner,
    *,
    repo: str,
    login: str,
    poll_seconds: int,
) -> list[Any]:
    """Return assigned open issues for one login and repository.

    A record younger than ``poll_seconds`` is shared by every worker on the
    device. At the interval boundary the device asks GitHub again. When an
    ETag was stored, that request sends ``If-None-Match`` and a 304 keeps the
    stored issues. The caller receives a copy.
    """
    key = _assignment_key(login, repo)
    now = wall_time()
    with _locked(home, SHARE_FILE) as share:
        assignments = share.document
        entry = assignments.get(key)
        fresh = _stored_issues(entry, now, poll_seconds)
        if fresh is not None:
            return copy.deepcopy(fresh)
        parsed = _parse_completed(
            _run(runner, _assignment_argv(repo, login, _stored_etag(entry)))
        )
        if parsed.status == 304:
            issues = entry.get("issues") if isinstance(entry, dict) else None
            if isinstance(entry, dict) and isinstance(issues, list):
                entry["at"] = now
                if parsed.etag is not None:
                    entry["etag"] = parsed.etag
                share.dirty = True
                return copy.deepcopy(issues)
            parsed = _parse_completed(_run(runner, _assignment_argv(repo, login, None)))
            if parsed.status == 304:
                raise CoordinatorError("empty command output")
        stored: dict[str, Any] = {"at": now, "issues": copy.deepcopy(parsed.issues)}
        if parsed.etag is not None:
            stored["etag"] = parsed.etag
        assignments[key] = stored
        share.dirty = True
        return copy.deepcopy(parsed.issues)


__all__ = [
    "LOGIN_SHARE_FILE",
    "SHARE_FILE",
    "authentication_failure",
    "cached_account_login",
    "note_authentication_failure",
    "shared_assigned_issues",
    "wall_time",
]
