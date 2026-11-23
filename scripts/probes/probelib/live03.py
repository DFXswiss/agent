"""LIVE-03 helpers (07-testing.md sections 2.4 and 6; PROJECT rule "subscription logins are never put at risk").

* `keychain_service(config_dir)` - the secret-store service of a Claude Code profile directory (rule of the legacy
  `agent_cli.usage.keychain_service_name`: `Claude Code-credentials-<first 8 hex of SHA-256(NFC(dir))>`).
* `keychain_metadata(text)` - the attributes of `security find-generic-password -s ... -a ...` **without** `-w`
  (no secret): the before/after comparison of the keychain entry.
* `profile_files(root)` / `snapshot(...)` / `compare_snapshots(...)` - content digests of every profile file
  (symbolic links by target) plus the keychain attributes; any difference fails LIVE-03.
* `access_token(raw, now_ms, min_valid_s)` - the current access token out of the stored login object. It never
  refreshes: an access token that expires within `min_valid_s` is refused, and the refresh token is never returned.
  Errors never contain the token and suppress the exception context (a JSON error would carry the document).
* `usage_shape(status, body)` - names, never values, of the usage endpoint's answer (05-runner.md section 10.6).
* `token_findings(text, token)` / `scan_files(paths, token)` / `redact(text, token)` - the secret scan of every
  artefact after the run.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

KEYCHAIN_SERVICE = "Claude Code-credentials"
OAUTH_OBJECT = "claudeAiOauth"
USAGE_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
_TOKEN_TEXT_RE = re.compile(r"[!-~]{16,4096}")
_ATTRIBUTE_RE = re.compile(r"^\s*(\S.*?)=(.*)$")
_HEADER_RE = re.compile(r"^(keychain|version|class):\s*(.*)$")
# Known credential shapes; literals are assembled so that the repository hygiene scan does not see them.
_PATTERN_RE = re.compile("sk-" + r"ant-[A-Za-z0-9_-]{20,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}=*")
REDACTED = "<redacted>"


class TokenError(ValueError):
    """The stored login object cannot give a usable access token (message without any secret)."""


@dataclass(frozen=True)
class AccessToken:
    token: str = field(repr=False)
    expires_at_ms: int
    subscription: str | None


def keychain_service(config_dir: str) -> str:
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode("utf-8")).hexdigest()[:8]
    return f"{KEYCHAIN_SERVICE}-{digest}"


def keychain_metadata(text: str) -> dict[str, str]:
    """Header lines and attributes of `security find-generic-password` (without `-w`)."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            found[header.group(1)] = header.group(2).strip()
            continue
        attribute = _ATTRIBUTE_RE.match(line)
        if attribute and line.startswith((" ", "\t")):
            found[attribute.group(1).strip()] = attribute.group(2).strip()
    if not any(key not in {"keychain", "version", "class"} for key in found):
        raise ValueError("no keychain attributes in the output")
    return found


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def profile_files(root: Path) -> dict[str, str]:
    """Relative path -> `sha256:<hex>` of the content, or `link:<target>` for a symbolic link."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "link:" + str(path.readlink())
        elif path.is_file():
            out[rel] = "sha256:" + _digest(path.read_bytes())
    return out


def snapshot(root: Path, keychain_text: str) -> dict[str, object]:
    metadata = keychain_metadata(keychain_text)
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"files": profile_files(root), "keychain": metadata, "keychain_sha256": _digest(canonical)}


def _mapping(value: object) -> Mapping[str, str]:
    return value if isinstance(value, dict) else {}


def compare_snapshots(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    """Every difference: changed, removed and added files, changed keychain attributes."""
    problems: list[str] = []
    old, new = _mapping(before.get("files")), _mapping(after.get("files"))
    problems += [f"changed: {name}" for name in sorted(old) if name in new and old[name] != new[name]]
    problems += [f"removed: {name}" for name in sorted(old) if name not in new]
    problems += [f"added: {name}" for name in sorted(new) if name not in old]
    old_kc, new_kc = _mapping(before.get("keychain")), _mapping(after.get("keychain"))
    for key in sorted(set(old_kc) | set(new_kc)):
        if old_kc.get(key) != new_kc.get(key):
            problems.append(f"keychain attribute changed: {key}")
    return problems


def access_token(raw: str, *, now_ms: int, min_valid_s: int) -> AccessToken:
    """The current access token of the login object `raw` (never refreshed, see the module docstring)."""
    data: object = None
    valid_json = True
    try:
        data = json.loads(raw)
    except ValueError:
        valid_json = False
    if not valid_json:
        raise TokenError("the stored login is not JSON")
    oauth = data.get(OAUTH_OBJECT) if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        raise TokenError(f"the stored login has no {OAUTH_OBJECT} object")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not _TOKEN_TEXT_RE.fullmatch(token):
        raise TokenError("the stored login has no usable accessToken")
    expires = oauth.get("expiresAt")
    if type(expires) is not int:
        raise TokenError("the stored login has no expiresAt in epoch milliseconds")
    left_s = (expires - now_ms) // 1000
    if left_s < min_valid_s:
        raise TokenError(f"the access token expires in {left_s} s (< {min_valid_s} s); it is never refreshed here")
    subscription = oauth.get("subscriptionType")
    return AccessToken(
        token=token, expires_at_ms=expires, subscription=subscription if isinstance(subscription, str) else None
    )


def usage_shape(status: int, body: bytes) -> dict[str, object]:
    """Status and key names of a usage endpoint answer; no values (they describe the account)."""
    data: object = None
    try:
        data = json.loads(body)
    except ValueError:
        return {"status": status, "json": False}
    keys = sorted(data) if isinstance(data, dict) else []
    windows = [key for key in USAGE_WINDOWS if isinstance(data, dict) and isinstance(data.get(key), dict)]
    fields = {key: sorted(data[key]) for key in windows} if isinstance(data, dict) else {}
    shape: dict[str, object] = {
        "status": status,
        "json": True,
        "keys": keys,
        "windows": windows,
        "window_fields": fields,
    }
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("type"), str):
        shape["error_type"] = error["type"]
    return shape


def token_findings(text: str, token: str) -> list[str]:
    found = []
    if token and token in text:
        found.append("the access token")
    if _PATTERN_RE.search(text):
        found.append("token pattern")
    if _BEARER_RE.search(text):
        found.append("bearer value")
    return found


def scan_files(paths: Iterable[Path], token: str) -> list[str]:
    """Secret scan of artefacts (files only; bytes decoded leniently)."""
    problems = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        problems += [f"{path}: {finding}" for finding in token_findings(text, token)]
    return problems


def is_excluded(profile: Path, excluded: Iterable[str]) -> bool:
    """True when the profile directory (resolved, so `x/`, `./x` and links count) has an excluded name."""
    names = {name.strip() for name in excluded if name.strip()}
    return profile.resolve().name in names or profile.name in names


def turn_problems(result: Mapping[str, object], model: str) -> list[str]:
    """Why a LIVE-03 run did not pass the gateway check (empty: passed). The usage endpoint's answer is a finding,
    not a pass criterion (05-runner.md section 10.6 feature-detects it)."""
    problems = []
    if result.get("failure"):
        problems.append(f"session: {result.get('failure')}")
    if result.get("prompt_accepted") is not True:
        problems.append("prompt not accepted")
    requests = result.get("gateway_requests")
    items = requests if isinstance(requests, list) else []
    if not any(isinstance(i, dict) and i.get("model") == model and i.get("status") == 200 for i in items):
        problems.append(f"no gateway request of {model} with status 200")
    return problems


def redact(text: str, token: str) -> str:
    return text.replace(token, REDACTED) if token else text
