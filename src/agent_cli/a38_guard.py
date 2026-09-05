"""dfx pr guard: trusted-base policy plus author local-CI reports.

Stateless bot. Reads policy from the immutable pull-request base and author
reports from pull-request comments. Never checks out or executes pull-request
code. Invoked event-driven (GitHub Actions) or via explicit reconcile.
"""

from __future__ import annotations

import argparse
import base64
import json
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .a38 import load_policy, verify_report

API_ORIGIN = "https://api.github.com"
API_HOST = "api.github.com"
POLICY_PATH = ".github/a38.json"
GUARD_MARKER = "<!-- PR-GUARD:A38:v1 -->"
GUARD_DOCS = "docs/a38-guard.md"
POLICY_DOCS = "docs/a38.md"
LOCAL_CI_BEGIN = "<!-- DFX-LOCAL-CI:v1 -->"
LOCAL_CI_END = "<!-- /DFX-LOCAL-CI:v1 -->"
LOCAL_CI_HINT_RE = re.compile(r"DFX-LOCAL-CI", re.IGNORECASE)
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_FILE_RE = re.compile(r"^\.github/workflows/[^/]+\.(yml|yaml)$")
GITHUB_ACTIONS_BOT_LOGIN = "github-actions[bot]"
# Public numeric id for github-actions[bot]; used only after /user is unavailable.
GITHUB_ACTIONS_BOT_ID = 41898282
MAX_COMMENT_PAGES_ITEMS = 2000
MAX_STATUS_DESC = 140
MAX_COMMENT_BODY = 12000
MAX_FILE_BYTES = 1024 * 1024
MAX_API_BYTES = 16 * 1024 * 1024
POLICY_APPROVAL_PREFIX = "A38-POLICY-APPROVAL:v1"
HTTP_TIMEOUT_S = 30.0
GET_RETRIES = 3
RETRY_BACKOFF_S = 0.4
ASSESS_RETRIES = 3
USER_AGENT = "dfx-pr-guard/1"

PR_TARGET_ACTIONS = frozenset(
    {"opened", "reopened", "synchronize", "edited", "ready_for_review"}
)
ISSUE_COMMENT_ACTIONS = frozenset({"created", "edited", "deleted"})

RequestFn = Callable[..., tuple[int, Any, dict[str, str]]]


class GuardError(RuntimeError):
    """Loud failure of the guard (API, config, bounds). Not a soft report miss."""


@dataclass(frozen=True)
class PullSnapshot:
    repo: str
    number: int
    state: str
    head_sha: str
    base_sha: str
    base_ref: str
    private: bool
    author_id: int
    author_login: str
    head_repo: str = ""


@dataclass
class Assessment:
    ok: bool
    status: str
    reasons: list[str] = field(default_factory=list)
    mode: str = "enforce"
    repo: str = ""
    pr: int = 0
    head_sha: str = ""
    base_sha: str = ""
    base_ref: str = ""
    head_repo: str = ""
    report_fingerprint: str = ""
    approval_fingerprint: str = ""
    policy_sha: str = ""
    private: bool = False
    required_names: list[str] = field(default_factory=list)
    policy_docs_url: str = ""
    guard_docs_url: str = ""
    report_status: str | None = None
    closed: bool = False
    context: str = ""
    observe_context: str = ""
    state_for_status: str = "failure"
    description: str = ""
    comment_body: str = ""
    skip_publish: bool = False
    dry_run: bool = False
    writes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "reasons": list(self.reasons),
            "mode": self.mode,
            "repo": self.repo,
            "pr": self.pr,
            "head": self.head_sha,
            "base": self.base_sha,
            "base_ref": self.base_ref,
            "policy_revision": self.policy_sha,
            "private": self.private,
            "required_names": list(self.required_names),
            "context": self.context,
            "observe_context": self.observe_context,
            "state": self.state_for_status,
            "description": self.description,
            "closed": self.closed,
            "skip_publish": self.skip_publish,
            "dry_run": self.dry_run,
            "writes": list(self.writes),
            "comment_body": self.comment_body,
        }


def _token_from_env(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = source.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise GuardError("GH_TOKEN or GITHUB_TOKEN is required")


def _validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or REPO_RE.match(repo) is None:
        raise GuardError("repo must be owner/name")
    return repo


def _validate_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or HEAD_SHA_RE.match(value.lower()) is None:
        raise GuardError(f"{label} must be a 40-character lowercase hex SHA")
    return value.lower()


def _validate_base_for_context(base_ref: str) -> str:
    if not isinstance(base_ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,74}", base_ref):
        raise GuardError("base ref must be a bounded branch name (1–75 characters)")
    if ".." in base_ref or "//" in base_ref or base_ref.endswith(("/", ".", ".lock")):
        raise GuardError("invalid base branch name")
    return base_ref


def status_context_enforce(base_ref: str) -> str:
    branch = _validate_base_for_context(base_ref)
    return f"A38 / report ({branch})"


def status_context_observe(base_ref: str) -> str:
    branch = _validate_base_for_context(base_ref)
    return f"A38 / report (observe: {branch})"


def truncate_desc(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_STATUS_DESC:
        return text
    return text[: MAX_STATUS_DESC - 1] + "…"


def looks_like_report(body: str | None) -> bool:
    """True when a comment looks like a local-CI report (valid or malformed)."""
    if not isinstance(body, str) or not body:
        return False
    if LOCAL_CI_BEGIN in body or LOCAL_CI_END in body:
        return True
    return LOCAL_CI_HINT_RE.search(body) is not None


def pick_latest_author_report(
    comments: Sequence[Mapping[str, Any]], author_id: int
) -> Mapping[str, Any] | None:
    """Latest report-like comment by the PR author, ordered by (updated_at, id)."""
    candidates: list[tuple[str, int, Mapping[str, Any]]] = []
    for comment in comments:
        user = comment.get("user") or {}
        if not isinstance(user, Mapping):
            continue
        uid = user.get("id")
        if uid != author_id:
            continue
        body = comment.get("body")
        if not looks_like_report(body if isinstance(body, str) else None):
            continue
        updated = comment.get("updated_at") or comment.get("created_at") or ""
        cid = comment.get("id")
        if not isinstance(cid, int):
            continue
        if not isinstance(updated, str):
            updated = ""
        candidates.append((updated, cid, comment))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def _parse_link_next(link_header: str | None) -> str | None:
    if not link_header:
        return None
    # <url>; rel="next", <url>; rel="last"
    parts = link_header.split(",")
    for part in parts:
        section = part.strip()
        if 'rel="next"' not in section and "rel=next" not in section:
            continue
        start = section.find("<")
        end = section.find(">")
        if start < 0 or end < 0 or end <= start + 1:
            continue
        return section[start + 1 : end]
    return None


def _ensure_api_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.port not in {None, 443}:
        raise GuardError(f"refusing non-{API_HOST} URL: {parsed.hostname!r}")
    if parsed.username or parsed.password:
        raise GuardError("refusing URL with embedded credentials")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirects so Authorization never leaves api.github.com."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise GuardError(f"refusing HTTP redirect ({code}) to {newurl!r}")


_OPENER = urllib.request.build_opener(_NoRedirect)


def default_request(
    method: str,
    url: str,
    *,
    token: str,
    body: bytes | None = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> tuple[int, Any, dict[str, str]]:
    """HTTPS request to api.github.com. Does not follow redirects."""
    safe = _ensure_api_url(url)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(safe, data=body, headers=headers, method=method.upper())
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 — host checked
            raw = resp.read(MAX_API_BYTES + 1)
            if len(raw) > MAX_API_BYTES:
                raise GuardError("GitHub API response exceeds size limit")
            header_map = {k.lower(): v for k, v in resp.headers.items()}
            status = getattr(resp, "status", None) or resp.getcode()
            if not raw:
                return int(status), None, header_map
            ctype = header_map.get("content-type", "")
            if "json" in ctype or raw[:1] in (b"{", b"["):
                try:
                    return int(status), json.loads(raw.decode("utf-8")), header_map
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GuardError(f"GitHub API returned invalid JSON: {exc}") from exc
            return int(status), raw.decode("utf-8", errors="replace"), header_map
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_API_BYTES + 1) if hasattr(exc, "read") else b""
        if len(raw) > MAX_API_BYTES:
            raise GuardError("GitHub API error response exceeds size limit")
        header_map = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        payload: Any
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = raw.decode("utf-8", errors="replace") if raw else None
        return int(exc.code), payload, header_map
    except urllib.error.URLError as exc:
        raise GuardError(f"GitHub API request failed: {exc}") from exc


class GitHubApi:
    """Injectable GitHub API client restricted to api.github.com."""

    def __init__(
        self,
        token: str,
        *,
        request_fn: RequestFn | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if not token or not isinstance(token, str):
            raise GuardError("token must be a non-empty string")
        self._token = token
        self._request_fn = request_fn or (
            lambda method, url, body=None: default_request(
                method, url, token=self._token, body=body
            )
        )
        self._sleep = sleep_fn or time.sleep
        self._own_id: int | None = None
        self._own_login: str | None = None
        self._immutable_cache: dict[str, tuple[int, Any, dict[str, str]]] = {}

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        body: Any | None = None,
        retry: bool | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        if path_or_url.startswith("https://"):
            url = _ensure_api_url(path_or_url)
        else:
            if not path_or_url.startswith("/"):
                raise GuardError("API path must start with /")
            url = API_ORIGIN + path_or_url
        raw_body = None if body is None else json.dumps(body).encode("utf-8")
        method_u = method.upper()
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        immutable = method_u == "GET" and (
            ("/contents/" in parsed.path and HEAD_SHA_RE.fullmatch(query.get("ref", [""])[0]) is not None)
            or re.search(r"/git/trees/[0-9a-f]{40}$", parsed.path) is not None
        )
        if immutable and url in self._immutable_cache:
            return self._immutable_cache[url]
        do_retry = retry if retry is not None else method_u == "GET"
        attempts = GET_RETRIES if do_retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                status, data, headers = self._request_fn(method_u, url, raw_body)
            except GuardError:
                raise
            except Exception as exc:  # noqa: BLE001 — convert to loud GuardError
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise GuardError(f"GitHub API {method_u} failed: {exc}") from exc
                self._sleep(RETRY_BACKOFF_S * (attempt + 1))
                continue
            if status in {502, 503, 504} and do_retry and attempt + 1 < attempts:
                self._sleep(RETRY_BACKOFF_S * (attempt + 1))
                continue
            result = (status, data, headers)
            if immutable and status in {200, 404} and len(self._immutable_cache) < 128:
                self._immutable_cache[url] = result
            return result
        raise GuardError(f"GitHub API {method_u} failed: {last_exc}")

    def get_json(self, path: str) -> Any:
        status, data, _ = self.request("GET", path)
        if status == 401 or status == 403:
            raise GuardError(f"GitHub API denied ({status}) for {path}")
        if status == 404:
            raise GuardError(f"GitHub API not found (404) for {path}")
        if status < 200 or status >= 300:
            raise GuardError(f"GitHub API HTTP {status} for {path}")
        return data

    def get_optional(self, path: str) -> tuple[int, Any]:
        status, data, _ = self.request("GET", path)
        if status in {401, 403}:
            raise GuardError(f"GitHub API denied ({status}) for {path}")
        return status, data

    def paginate(self, path: str, *, hard_limit: int = MAX_COMMENT_PAGES_ITEMS) -> list[Any]:
        items: list[Any] = []
        next_url: str | None
        if path.startswith("https://"):
            next_url = _ensure_api_url(path)
        else:
            sep = "&" if "?" in path else "?"
            next_url = API_ORIGIN + path + f"{sep}per_page=100"

        visited: set[str] = set()
        while next_url:
            if next_url in visited or len(visited) >= 100:
                raise GuardError("pagination cycle or page bound exceeded")
            visited.add(next_url)
            status, data, headers = self.request("GET", next_url)
            if status in {401, 403}:
                raise GuardError(f"GitHub API denied ({status}) while paginating")
            if status < 200 or status >= 300:
                raise GuardError(f"GitHub API HTTP {status} while paginating")
            if not isinstance(data, list):
                raise GuardError("GitHub API pagination expected a JSON array")
            items.extend(data)
            if len(items) > hard_limit:
                raise GuardError(
                    f"pagination exceeded bound ({hard_limit}); refusing partial accept"
                )
            nxt = _parse_link_next(headers.get("link"))
            if nxt is None:
                break
            next_url = _ensure_api_url(nxt)
        return items

    def resolve_own_user(self) -> tuple[int, str]:
        if self._own_id is not None and self._own_login is not None:
            return self._own_id, self._own_login
        status, data, _ = self.request("GET", "/user")
        if status == 200 and isinstance(data, dict) and isinstance(data.get("id"), int):
            login = data.get("login")
            if not isinstance(login, str) or not login:
                raise GuardError("/user returned no login")
            self._own_id = data["id"]
            self._own_login = login
            return self._own_id, self._own_login
        # GitHub Actions GITHUB_TOKEN often cannot call /user.
        if status in {401, 403, 404} and os.environ.get("GITHUB_ACTIONS") == "true":
            status2, data2 = self.get_optional(f"/users/{urllib.parse.quote(GITHUB_ACTIONS_BOT_LOGIN)}")
            if status2 == 200 and isinstance(data2, dict) and isinstance(data2.get("id"), int):
                login = data2.get("login")
                if login != GITHUB_ACTIONS_BOT_LOGIN or data2["id"] != GITHUB_ACTIONS_BOT_ID:
                    raise GuardError("github-actions[bot] login mismatch")
                self._own_id = int(data2["id"])
                self._own_login = GITHUB_ACTIONS_BOT_LOGIN
                return self._own_id, self._own_login
            raise GuardError("cannot resolve official GitHub Actions bot identity")
        raise GuardError(f"cannot resolve acting user (HTTP {status})")


def fetch_pull(api: GitHubApi, repo: str, number: int) -> PullSnapshot:
    repo = _validate_repo(repo)
    data = api.get_json(f"/repos/{repo}/pulls/{number}")
    if not isinstance(data, dict):
        raise GuardError("pull response is not an object")
    head = data.get("head") or {}
    base = data.get("base") or {}
    user = data.get("user") or {}
    if not isinstance(head, dict) or not isinstance(base, dict) or not isinstance(user, dict):
        raise GuardError("pull payload missing head/base/user")
    base_repo = base.get("repo")
    head_repo = head.get("repo")
    # Closed fork PRs retain their head SHA after the fork repository is deleted.
    # No head contents will be read or published for this successful no-op.
    if data.get("state") == "closed" and head_repo is None:
        head_repo = {"full_name": repo}
    if not isinstance(base_repo, dict) or not isinstance(head_repo, dict):
        raise GuardError("pull repositories missing (head repository may have been deleted)")
    if _validate_repo(base_repo.get("full_name")).lower() != repo.lower():
        raise GuardError("pull base repository differs from requested repository")
    head_repository = _validate_repo(head_repo.get("full_name"))
    head_sha = head.get("sha")
    base_sha = base.get("sha")
    base_ref = base.get("ref")
    if not isinstance(head_sha, str) or not isinstance(base_sha, str):
        raise GuardError("pull head/base sha missing")
    base_ref = _validate_base_for_context(base_ref)
    author_id = user.get("id")
    author_login = user.get("login")
    if type(author_id) is not int or author_id <= 0:
        raise GuardError("pull author id must be numeric")
    if not isinstance(author_login, str):
        author_login = ""
    private = base_repo.get("private")
    if type(private) is not bool:
        raise GuardError("pull repository visibility must be a JSON boolean")
    state = data.get("state")
    if state not in {"open", "closed"}:
        raise GuardError("pull state missing")
    return PullSnapshot(
        repo=repo,
        number=number,
        state=state,
        head_sha=_validate_sha(head_sha, "head"),
        base_sha=_validate_sha(base_sha, "base"),
        base_ref=base_ref,
        private=private,
        author_id=author_id,
        author_login=author_login,
        head_repo=head_repository,
    )


def _decode_contents_file(data: Mapping[str, Any]) -> bytes:
    encoding = data.get("encoding")
    content = data.get("content")
    if encoding != "base64" or not isinstance(content, str):
        raise GuardError("contents API response missing base64 content")
    if len(content) > 2 * MAX_FILE_BYTES:
        raise GuardError("repository file exceeds size limit")
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
        if len(raw) > MAX_FILE_BYTES:
            raise GuardError("repository file exceeds size limit")
        return raw
    except (ValueError, TypeError) as exc:
        raise GuardError(f"contents API base64 decode failed: {exc}") from exc


def fetch_file_at_ref(api: GitHubApi, repo: str, path: str, ref: str) -> bytes | None:
    status, data = api.get_optional(
        f"/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    )
    if status == 404:
        return None
    if status < 200 or status >= 300:
        raise GuardError(f"contents HTTP {status} for {path}@{ref}")
    if not isinstance(data, dict):
        raise GuardError("contents response must be a file object")
    if data.get("type") != "file":
        raise GuardError(f"{path} is not a file at {ref}")
    return _decode_contents_file(data)


def fetch_policy_text(api: GitHubApi, repo: str, base_sha: str) -> str | None:
    raw = fetch_file_at_ref(api, repo, POLICY_PATH, base_sha)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuardError(f"{POLICY_PATH} is not valid UTF-8: {exc}") from exc


def list_workflow_paths(api: GitHubApi, repo: str, sha: str) -> list[str]:
    data = api.get_json(f"/repos/{repo}/git/trees/{sha}?recursive=1")
    if not isinstance(data, dict):
        raise GuardError("git tree response is not an object")
    if data.get("truncated"):
        raise GuardError("git tree response is truncated; refuse partial workflow list")
    tree = data.get("tree")
    if not isinstance(tree, list):
        raise GuardError("git tree missing tree array")
    paths: list[str] = []
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if isinstance(path, str) and WORKFLOW_FILE_RE.match(path):
            paths.append(path)
    return sorted(set(paths))


def _load_yaml(text: str) -> Any:
    """Safe YAML only; duplicate keys, excessive nesting and aliases fail closed."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise GuardError("PyYAML is required to validate workflow inventory") from exc

    class StrictLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise GuardError("workflow YAML aliases are not supported")
            self.a38_depth = getattr(self, "a38_depth", 0) + 1
            if self.a38_depth > 64:
                raise GuardError("workflow YAML nesting exceeds limit")
            try:
                return super().compose_node(parent, index)
            finally:
                self.a38_depth -= 1

        def construct_mapping(self, node, deep=False):
            keys = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, (str, int, float, bool, type(None))):
                    raise GuardError("workflow YAML mapping key must be scalar")
                if key in keys:
                    raise GuardError("workflow YAML has duplicate mapping keys")
                keys.add(key)
            return super().construct_mapping(node, deep=deep)

    try:
        return yaml.load(text, Loader=StrictLoader)
    except Exception as exc:  # noqa: BLE001 — surface YAML errors loudly
        raise GuardError(f"workflow YAML safe_load failed: {exc}") from exc


def enumerate_workflow_jobs(workflow_path: str, content: bytes) -> list[str]:
    if len(content) > MAX_FILE_BYTES:
        raise GuardError("workflow exceeds size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuardError(f"{workflow_path} is not UTF-8: {exc}") from exc
    loaded = _load_yaml(text)
    if not isinstance(loaded, dict):
        raise GuardError(f"{workflow_path} top level must be a mapping")
    jobs = loaded.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise GuardError(f"{workflow_path} jobs must be a mapping")
    out: list[str] = []
    for key in jobs:
        if not isinstance(key, str) or not key:
            raise GuardError(f"{workflow_path} has a non-string job id")
        out.append(key)
    return out


def classified_pairs(policy: Mapping[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for job in policy.get("jobs") or []:
        if not isinstance(job, Mapping):
            continue
        pairs.add((str(job["workflow"]), str(job["job"])))
    for exc in policy.get("exclusions") or []:
        if not isinstance(exc, Mapping):
            continue
        pairs.add((str(exc["workflow"]), str(exc["job"])))
    return pairs


def check_workflows_against_policy(
    api: GitHubApi,
    repo: str,
    *,
    head_sha: str,
    base_sha: str,
    policy: Mapping[str, Any],
    head_repo: str | None = None,
    allow_changes: bool = False,
) -> list[str]:
    """Return maintainer-facing problems for head workflows vs trusted base policy."""
    problems: list[str] = []
    try:
        head_paths = list_workflow_paths(api, head_repo or repo, head_sha)
        base_paths = list_workflow_paths(api, repo, base_sha)
    except GuardError as exc:
        return [f"workflow inventory failed: {exc}"]

    allowed = classified_pairs(policy)
    actual: set[tuple[str, str]] = set()
    all_paths = sorted(set(head_paths) | set(base_paths))
    for path in all_paths:
        head_bytes = fetch_file_at_ref(api, head_repo or repo, path, head_sha)
        base_bytes = fetch_file_at_ref(api, repo, path, base_sha)
        if head_bytes is None and base_bytes is None:
            continue
        if head_bytes != base_bytes and not allow_changes:
            problems.append(
                f"{path} bytes changed vs base; explicit current-head/base maintainer "
                "A38 policy approval is required"
            )
        if head_bytes is None:
            # Removed at head: still fine for classification; no head jobs to require.
            continue
        try:
            job_ids = enumerate_workflow_jobs(path, head_bytes)
        except GuardError as exc:
            problems.append(str(exc))
            continue
        for job_id in job_ids:
            actual.add((path, job_id))
            if (path, job_id) not in allowed:
                problems.append(
                    f"unclassified workflow job {path!r} / {job_id!r}; "
                    "add it to jobs or exclusions in .github/a38.json on the base"
                )
    if allowed - actual:
        problems.append("policy classifies workflow jobs absent from the current head")
    # Matrix: we classify the job id only; do not invent variant counts.
    return problems


def blob_url(repo: str, sha: str, path: str) -> str:
    return f"https://github.com/{repo}/blob/{sha}/{path}"


def build_run_instructions(base_sha: str) -> str:
    return (
        "python -m agent_cli.a38 run --repo . --policy /tmp/a38-policy.json "
        f"--base-sha {base_sha} --output /tmp/a38-report.md --logs-dir /tmp/a38-logs"
    )


def build_comment_body(assessment: Assessment) -> str:
    names = ", ".join(assessment.required_names) if assessment.required_names else "(none)"
    problems = "; ".join(assessment.reasons) if assessment.reasons else "none"
    if len(problems) > 800:
        problems = problems[:799] + "…"
    en = (
        f"A38 {assessment.status}: "
        + (
            "author local-CI report accepted for this head."
            if assessment.ok and assessment.status == "pass"
            else "author local-CI report missing or invalid for this head."
        )
    )
    de = (
        f"A38 {assessment.status}: "
        + (
            "Autor-Local-CI-Report für diesen Head akzeptiert."
            if assessment.ok and assessment.status == "pass"
            else "Autor-Local-CI-Report für diesen Head fehlt oder ist ungültig."
        )
    )
    if assessment.mode == "observe":
        en = "Observe mode (advisory, not branch-required). " + en
        de = "Observe-Modus (Hinweis, nicht branch-pflichtig). " + de
    run_cmd = build_run_instructions(assessment.base_sha or "BASE_SHA")
    run_cmd += f" --repository {assessment.repo}"
    details = (
        f"- Docs (active policy revision): {assessment.policy_docs_url or POLICY_DOCS}\n"
        f"- Base: `{assessment.base_sha}`; policy revision: `{assessment.policy_sha or assessment.base_sha}`.\n"
        "- Save the active revision of `.github/a38.json` as `/tmp/a38-policy.json` before running.\n"
        f"- Guard docs: {assessment.guard_docs_url or GUARD_DOCS}\n"
        f"- Required jobs: {names}\n"
        f"- Problems: {problems}\n"
        "- For a workflow/policy migration, another maintainer must submit an approved review with "
        f"`{POLICY_APPROVAL_PREFIX} head={assessment.head_sha} base={assessment.base_sha}`.\n"
        f"- Run (outside the repo output paths): `{run_cmd}`\n"
        "- Publish: post the complete generated report as a pull-request comment "
        "using the PR author's account, preserving its report block.\n"
    )
    body = (
        f"{GUARD_MARKER}\n"
        "dfx pr guard\n\n"
        f"EN: Thanks for your contribution! This repository follows A38. {en}\n\n"
        f"DE: Danke für deinen Beitrag! In diesem Repository gilt A38. {de}\n\n"
        f"<details>\n<summary>Details</summary>\n\n{details}\n</details>\n"
    )
    if len(body) > MAX_COMMENT_BODY:
        body = body[: MAX_COMMENT_BODY - 1] + "…\n"
    return body


def _status_bits(assessment: Assessment) -> None:
    base = assessment.base_ref
    if assessment.mode == "observe":
        assessment.observe_context = status_context_observe(base)
        assessment.context = ""
        # Observe never claims blocking success on the enforce context.
        assessment.state_for_status = "success"
        if assessment.ok and assessment.status == "pass":
            assessment.description = truncate_desc(
                f"advisory pass for {assessment.head_sha[:7]} (observe)"
            )
        else:
            assessment.description = truncate_desc(
                f"advisory: {assessment.status}; see PR comment (observe)"
            )
        return
    assessment.context = status_context_enforce(base)
    assessment.observe_context = ""
    if assessment.ok and assessment.status == "pass":
        assessment.state_for_status = "success"
        assessment.description = truncate_desc(f"pass for {assessment.head_sha[:7]}")
    else:
        assessment.state_for_status = "failure"
        reason = assessment.reasons[0] if assessment.reasons else assessment.status
        assessment.description = truncate_desc(f"{assessment.status}: {reason}")


def assess_from_parts(
    *,
    pull: PullSnapshot,
    policy: Mapping[str, Any] | None,
    policy_error: str | None,
    workflow_problems: Sequence[str],
    author_comment: Mapping[str, Any] | None,
    dry_run: bool = False,
) -> Assessment:
    assessment = Assessment(
        ok=False,
        status="fail",
        repo=pull.repo,
        pr=pull.number,
        head_sha=pull.head_sha,
        base_sha=pull.base_sha,
        base_ref=pull.base_ref,
        head_repo=pull.head_repo or pull.repo,
        report_fingerprint=_report_fingerprint(author_comment),
        private=pull.private,
        closed=pull.state != "open",
        dry_run=dry_run,
        policy_docs_url=blob_url(pull.repo, pull.base_sha, POLICY_DOCS),
        guard_docs_url=blob_url(pull.repo, pull.base_sha, GUARD_DOCS),
    )
    if policy is None:
        if policy_error:
            assessment.status = "invalid_policy"
            assessment.reasons = [policy_error]
        else:
            assessment.status = "not_configured"
            assessment.reasons = [
                f"{POLICY_PATH} missing on base {pull.base_sha}; "
                "maintainer must add a valid A38 manifest before the guard can pass"
            ]
        assessment.mode = "enforce"
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment

    mode = policy.get("mode")
    if mode not in {"enforce", "observe"}:
        assessment.status = "invalid_policy"
        assessment.reasons = [
            "maintainer config error: policy mode must be enforce|observe"
        ]
        assessment.mode = "enforce"
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment
    assessment.mode = mode
    jobs = policy.get("jobs") or []
    assessment.required_names = [
        str(j.get("name")) for j in jobs if isinstance(j, Mapping) and j.get("name")
    ]

    reasons: list[str] = []
    if policy_error:
        reasons.append(policy_error)
    reasons.extend(workflow_problems)

    if reasons:
        assessment.status = "fail"
        assessment.reasons = reasons
        assessment.ok = False
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment

    if author_comment is None:
        assessment.status = "fail"
        assessment.reasons = ["no author local-CI report comment on this pull request"]
        assessment.ok = False
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment

    body = author_comment.get("body")
    if not isinstance(body, str):
        assessment.status = "fail"
        assessment.reasons = ["author report comment body missing"]
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment

    try:
        verdict = verify_report(
            body,
            dict(policy),
            repo=pull.repo,
            head=pull.head_sha,
            private=pull.private,
        )
    except Exception as exc:  # noqa: BLE001 — treat validator crashes as fail
        assessment.status = "fail"
        assessment.reasons = [f"report verification error: {exc}"]
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment

    if not isinstance(verdict, Mapping):
        assessment.status = "fail"
        assessment.reasons = ["verify_report returned a non-object"]
        _status_bits(assessment)
        assessment.comment_body = build_comment_body(assessment)
        return assessment

    ok = bool(verdict.get("ok"))
    status = verdict.get("status")
    if not isinstance(status, str) or not status:
        status = "pass" if ok else "fail"
    raw_reasons = verdict.get("reasons") or []
    if isinstance(raw_reasons, list):
        reasons = [str(r) for r in raw_reasons]
    else:
        reasons = ["verify_report reasons missing"]
        ok = False
        status = "fail"

    assessment.ok = ok
    assessment.status = status
    assessment.reasons = reasons
    assessment.report_status = status
    _status_bits(assessment)
    assessment.comment_body = build_comment_body(assessment)
    return assessment


def load_base_policy(
    api: GitHubApi, repo: str, base_sha: str
) -> tuple[dict[str, Any] | None, str | None]:
    text = fetch_policy_text(api, repo, base_sha)
    if text is None:
        return None, None
    try:
        policy = load_policy(text)
    except Exception as exc:  # noqa: BLE001
        return None, (
            f"maintainer config error: invalid {POLICY_PATH} on base {base_sha}: {exc}"
        )
    if not isinstance(policy, dict):
        return None, f"maintainer config error: load_policy did not return an object"
    return policy, None


def collect_comments(api: GitHubApi, repo: str, number: int) -> list[dict[str, Any]]:
    raw = api.paginate(f"/repos/{repo}/issues/{number}/comments")
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
    return out


def _report_fingerprint(comment: Mapping[str, Any] | None) -> str:
    if comment is None:
        return ""
    payload = [comment.get("id"), comment.get("updated_at"), comment.get("body")]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def migration_approval(api: GitHubApi, pull: PullSnapshot) -> str:
    """Return a fingerprint of explicit, current maintainer authorization, or empty."""
    reviews = api.paginate(f"/repos/{pull.repo}/pulls/{pull.number}/reviews")
    latest: dict[int, Mapping[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, Mapping):
            raise GuardError("review response must contain objects")
        state = review.get("state")
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        user = review.get("user")
        if not isinstance(user, Mapping) or type(user.get("id")) is not int:
            raise GuardError("review author identity missing")
        uid = user["id"]
        if uid == pull.author_id:
            continue
        if type(review.get("id")) is not int or not isinstance(review.get("submitted_at"), str):
            raise GuardError("submitted review identity/timestamp missing")
        previous = latest.get(uid)
        key = (review["submitted_at"], review["id"])
        if previous is None or key > (previous["submitted_at"], previous["id"]):
            latest[uid] = review
    marker = f"{POLICY_APPROVAL_PREFIX} head={pull.head_sha} base={pull.base_sha}"
    approved: list[tuple[int, int, str]] = []
    for uid, review in latest.items():
        if review.get("commit_id") != pull.head_sha:
            continue
        state = review["state"]
        body = review.get("body")
        explicit = isinstance(body, str) and marker in [line.strip() for line in body.splitlines()]
        if state != "CHANGES_REQUESTED" and not (state == "APPROVED" and explicit):
            continue
        login = review["user"].get("login")
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            raise GuardError("reviewer login invalid")
        permission = api.get_json(
            f"/repos/{pull.repo}/collaborators/{urllib.parse.quote(login)}/permission"
        )
        if not isinstance(permission, Mapping):
            raise GuardError("reviewer permission response invalid")
        actor = permission.get("user")
        if not isinstance(actor, Mapping) or actor.get("id") != uid:
            raise GuardError("reviewer permission identity mismatch")
        role = permission.get("permission")
        if role not in {"write", "maintain", "admin"}:
            continue
        if state == "CHANGES_REQUESTED":
            return ""
        approved.append((uid, review["id"], str(role)))
    if not approved:
        return ""
    return hashlib.sha256(json.dumps(sorted(approved)).encode()).hexdigest()


def assess_pull(
    api: GitHubApi,
    repo: str,
    number: int,
    *,
    dry_run: bool = False,
    pull: PullSnapshot | None = None,
) -> Assessment:
    snap = pull or fetch_pull(api, repo, number)
    if snap.state != "open":
        return Assessment(
            ok=True, status="closed", closed=True, skip_publish=True,
            reasons=["pull request is closed; nothing to reconcile"],
            repo=snap.repo, pr=snap.number, head_sha=snap.head_sha,
            base_sha=snap.base_sha, base_ref=snap.base_ref,
            head_repo=snap.head_repo, private=snap.private,
            state_for_status="", dry_run=dry_run,
        )
    policy, policy_error = load_base_policy(api, snap.repo, snap.base_sha)
    base_mode = policy.get("mode") if policy else "enforce"
    approval = migration_approval(api, snap)
    policy_repo, policy_sha = snap.repo, snap.base_sha
    if approval:
        head_policy, head_error = load_base_policy(api, snap.head_repo or snap.repo, snap.head_sha)
        policy, policy_error = head_policy, head_error
        if policy is not None:
            policy = dict(policy, mode=base_mode)
        policy_repo, policy_sha = snap.head_repo or snap.repo, snap.head_sha
    workflow_problems: list[str] = []
    if policy is not None and policy_error is None:
        workflow_problems = check_workflows_against_policy(
            api,
            snap.repo,
            head_sha=snap.head_sha,
            base_sha=snap.base_sha,
            policy=policy,
            head_repo=snap.head_repo or snap.repo,
            allow_changes=bool(approval),
        )
    comments = collect_comments(api, snap.repo, snap.number)
    author_comment = pick_latest_author_report(comments, snap.author_id)
    assessment = assess_from_parts(
        pull=snap,
        policy=policy,
        policy_error=policy_error,
        workflow_problems=workflow_problems,
        author_comment=author_comment,
        dry_run=dry_run,
    )
    assessment.approval_fingerprint = approval
    assessment.policy_sha = policy_sha
    assessment.policy_docs_url = blob_url(policy_repo, policy_sha, (policy or {}).get("documentation", POLICY_DOCS))
    assessment.comment_body = build_comment_body(assessment)
    return assessment


def _find_own_guard_comment(
    comments: Sequence[Mapping[str, Any]], own_id: int
) -> Mapping[str, Any] | None:
    owned: list[tuple[str, int, Mapping[str, Any]]] = []
    for comment in comments:
        user = comment.get("user") or {}
        if not isinstance(user, Mapping) or user.get("id") != own_id:
            continue
        # Never trust type==Bot alone; numeric id must match the resolved actor.
        body = comment.get("body")
        if not isinstance(body, str) or GUARD_MARKER not in body:
            continue
        cid = comment.get("id")
        if not isinstance(cid, int):
            continue
        updated = comment.get("updated_at") or comment.get("created_at") or ""
        if not isinstance(updated, str):
            updated = ""
        owned.append((updated, cid, comment))
    if not owned:
        return None
    owned.sort(key=lambda item: (item[0], item[1]))
    return owned[-1][2]


def _existing_status(
    api: GitHubApi, repo: str, sha: str, context: str
) -> Mapping[str, Any] | None:
    # Combined status statuses list; paginate carefully.
    try:
        items = api.paginate(f"/repos/{repo}/commits/{sha}/statuses")
    except GuardError:
        return None
    for item in items:
        if isinstance(item, dict) and item.get("context") == context:
            return item
    return None


def publish_assessment(
    api: GitHubApi,
    assessment: Assessment,
    *,
    force: bool = False,
) -> Assessment:
    if assessment.skip_publish:
        return assessment
    if assessment.closed:
        assessment.writes.append("skipped:closed")
        return assessment
    if assessment.dry_run:
        assessment.writes.append("dry-run")
        return assessment

    # Re-read PR before any successful publication.
    fresh = fetch_pull(api, assessment.repo, assessment.pr)
    if (
        fresh.head_sha != assessment.head_sha
        or fresh.base_sha != assessment.base_sha
        or fresh.base_ref != assessment.base_ref
        or fresh.head_repo != assessment.head_repo
        or fresh.private != assessment.private
        or fresh.state != ("closed" if assessment.closed else "open")
    ):
        raise GuardError("pull head/base/state changed before publish; retry assessment")

    own_id, _own_login = api.resolve_own_user()
    comments = collect_comments(api, assessment.repo, assessment.pr)
    latest = pick_latest_author_report(comments, fresh.author_id)
    if _report_fingerprint(latest) != assessment.report_fingerprint:
        raise GuardError("author report changed before publish; retry assessment")
    if migration_approval(api, fresh) != assessment.approval_fingerprint:
        raise GuardError("maintainer approval changed before publish; retry assessment")
    existing = _find_own_guard_comment(comments, own_id)
    body = assessment.comment_body or build_comment_body(assessment)

    if existing is not None and existing.get("body") == body and not force:
        assessment.writes.append("comment:unchanged")
    elif existing is not None:
        cid = existing["id"]
        status, _data, _ = api.request(
            "PATCH",
            f"/repos/{assessment.repo}/issues/comments/{cid}",
            body={"body": body},
            retry=False,
        )
        if status in {401, 403}:
            raise GuardError(f"GitHub API denied ({status}) updating comment")
        if status < 200 or status >= 300:
            raise GuardError(f"comment update HTTP {status}")
        assessment.writes.append(f"comment:update:{cid}")
    else:
        # Do not update a human's forged marker; create our own comment.
        status, data, _ = api.request(
            "POST",
            f"/repos/{assessment.repo}/issues/{assessment.pr}/comments",
            body={"body": body},
            retry=False,
        )
        if status in {401, 403}:
            raise GuardError(f"GitHub API denied ({status}) creating comment")
        if status < 200 or status >= 300:
            raise GuardError(f"comment create HTTP {status}")
        new_id = data.get("id") if isinstance(data, dict) else None
        assessment.writes.append(f"comment:create:{new_id}")

    def _post_status(context: str, state: str, description: str) -> None:
        prev = _existing_status(api, assessment.repo, assessment.head_sha, context)
        if state == "success":
            latest_pull = fetch_pull(api, assessment.repo, assessment.pr)
            if latest_pull != fresh:
                raise GuardError("pull changed before publish; retry assessment")
            current_comments = collect_comments(api, assessment.repo, assessment.pr)
            current_report = pick_latest_author_report(current_comments, fresh.author_id)
            if _report_fingerprint(current_report) != assessment.report_fingerprint:
                raise GuardError("author report changed before publish; retry assessment")
            if migration_approval(api, fresh) != assessment.approval_fingerprint:
                raise GuardError("maintainer approval changed before publish; retry assessment")
        if (
            prev is not None
            and prev.get("state") == state
            and (prev.get("description") or "") == description
            and not force
        ):
            assessment.writes.append(f"status:unchanged:{context}")
            return
        payload = {
            "state": state,
            "description": description,
            "context": context,
        }
        status, _data, _ = api.request(
            "POST",
            f"/repos/{assessment.repo}/statuses/{assessment.head_sha}",
            body=payload,
            retry=False,
        )
        if status in {401, 403}:
            raise GuardError(f"GitHub API denied ({status}) creating status")
        if status < 200 or status >= 300:
            raise GuardError(f"status create HTTP {status}")
        assessment.writes.append(f"status:create:{context}")

    if assessment.mode == "observe":
        _post_status(
            assessment.observe_context or status_context_observe(assessment.base_ref),
            "success",
            assessment.description,
        )
    else:
        _post_status(
            assessment.context or status_context_enforce(assessment.base_ref),
            assessment.state_for_status,
            assessment.description,
        )
    return assessment


def reconcile_pull(
    api: GitHubApi,
    repo: str,
    number: int,
    *,
    dry_run: bool = False,
    publish: bool = True,
) -> Assessment:
    last_err: Exception | None = None
    snap: PullSnapshot | None = None
    for _ in range(ASSESS_RETRIES):
        try:
            snap = fetch_pull(api, repo, number)
            assessment = assess_pull(api, repo, number, dry_run=dry_run, pull=snap)
            if dry_run or not publish or assessment.closed:
                if assessment.closed and not dry_run:
                    assessment.writes.append("skipped:closed")
                elif dry_run:
                    assessment.writes.append("dry-run")
                return assessment
            return publish_assessment(api, assessment)
        except GuardError as exc:
            if "changed before publish" in str(exc):
                last_err = exc
                continue
            if publish and not dry_run and snap is not None:
                invalidate_status(api, snap)
            raise
    if publish and not dry_run and snap is not None:
        invalidate_status(api, snap)
    raise GuardError(f"assessment republish failed after retries: {last_err}")


def invalidate_status(api: GitHubApi, pull: PullSnapshot) -> None:
    """Best-effort error status on known head; never hide the original API failure."""
    if pull.state != "open":
        return
    try:
        status, _, _ = api.request(
            "POST", f"/repos/{pull.repo}/statuses/{pull.head_sha}",
            body={"state": "error", "context": status_context_enforce(pull.base_ref),
                  "description": "A38 reconciliation failed; rerun the guard before merging."},
            retry=False,
        )
        if not 200 <= status < 300:
            print("a38-guard: could not invalidate prior status (API denied or unavailable)", file=sys.stderr)
    except GuardError:
        print("a38-guard: could not invalidate prior status (API denied or unavailable)", file=sys.stderr)


def list_open_pulls(api: GitHubApi, repo: str) -> list[int]:
    items = api.paginate(f"/repos/{repo}/pulls?state=open")
    numbers: list[int] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("number"), int):
            numbers.append(item["number"])
    return numbers


def event_should_ignore(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    own_id: int | None,
) -> str | None:
    """Return a reason string when the event must be ignored, else None."""
    action = payload.get("action")
    if event_name == "pull_request_review":
        if action not in {"submitted", "edited", "dismissed"}:
            return "ignored unsupported review action"
        return None
    if event_name == "pull_request_target" or event_name == "pull_request":
        if action not in PR_TARGET_ACTIONS:
            return f"ignored pull_request action {action!r}"
        return None
    if event_name == "issue_comment":
        if action not in ISSUE_COMMENT_ACTIONS:
            return f"ignored issue_comment action {action!r}"
        issue = payload.get("issue") or {}
        if not isinstance(issue, Mapping) or "pull_request" not in issue:
            return "ignored issue-only comment event"
        comment = payload.get("comment") or {}
        user = comment.get("user") if isinstance(comment, Mapping) else None
        if isinstance(user, Mapping) and own_id is not None and user.get("id") == own_id:
            return "ignored own bot comment (loop prevention)"
        return None
    if event_name == "workflow_dispatch":
        return None
    return f"ignored unsupported event {event_name!r}"


def extract_repo_pr_from_event(
    event_name: str, payload: Mapping[str, Any]
) -> tuple[str, int]:
    if event_name in {"pull_request_target", "pull_request", "pull_request_review"}:
        pr = payload.get("pull_request") or {}
        repo = payload.get("repository") or {}
        if not isinstance(pr, Mapping) or not isinstance(repo, Mapping):
            raise GuardError("event missing pull_request/repository")
        full = repo.get("full_name")
        number = pr.get("number")
        if not isinstance(full, str) or not isinstance(number, int):
            raise GuardError("event repo/pr invalid")
        return _validate_repo(full), number
    if event_name == "issue_comment":
        issue = payload.get("issue") or {}
        repo = payload.get("repository") or {}
        if not isinstance(issue, Mapping) or not isinstance(repo, Mapping):
            raise GuardError("issue_comment missing issue/repository")
        full = repo.get("full_name")
        number = issue.get("number")
        if not isinstance(full, str) or not isinstance(number, int):
            raise GuardError("issue_comment repo/pr invalid")
        return _validate_repo(full), number
    raise GuardError(f"cannot extract repo/pr from event {event_name}")


def reconcile_event(
    api: GitHubApi,
    *,
    event_name: str,
    payload: Mapping[str, Any],
    repo: str | None = None,
    pr: int | None = None,
    dry_run: bool = False,
    publish: bool = True,
) -> Assessment | dict[str, Any]:
    own_id: int | None
    try:
        own_id, _ = api.resolve_own_user()
    except GuardError:
        own_id = None

    ignore = event_should_ignore(event_name, payload, own_id=own_id)
    if ignore is not None:
        return {
            "ok": True,
            "status": "ignored",
            "reasons": [ignore],
            "writes": [],
            "dry_run": dry_run,
        }

    if event_name == "workflow_dispatch":
        if not repo or pr is None:
            raise GuardError("workflow_dispatch requires --repo and --pr")
        return reconcile_pull(api, repo, pr, dry_run=dry_run, publish=publish)

    event_repo, event_pr = extract_repo_pr_from_event(event_name, payload)
    use_repo = repo or event_repo
    use_pr = pr if pr is not None else event_pr
    return reconcile_pull(api, use_repo, use_pr, dry_run=dry_run, publish=publish)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_cli.a38_guard",
        description="dfx pr guard: verify author local-CI reports against trusted base policy.",
    )
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("reconcile", help="Assess and optionally publish for a PR or event")
    rec.add_argument("--repo", help="owner/name")
    rec.add_argument("--pr", type=int, help="pull request number")
    rec.add_argument(
        "--event-file",
        default=None,
        help="GitHub event JSON path (default: env GITHUB_EVENT_PATH)",
    )
    rec.add_argument(
        "--event-name",
        default=None,
        help="GitHub event name (default: env GITHUB_EVENT_NAME)",
    )
    rec.add_argument(
        "--all-open",
        action="store_true",
        help="Reconcile every open PR in --repo (standalone / scheduled host)",
    )
    rec.add_argument(
        "--dry-run",
        action="store_true",
        help="Prospective JSON only; no comment/status writes",
    )
    rec.add_argument(
        "--no-publish",
        action="store_true",
        help="Assess without publishing (alias of dry-run semantics for writes)",
    )
    rec.add_argument(
        "--json",
        action="store_true",
        help="Print JSON result on stdout",
    )

    pub = sub.add_parser(
        "publish",
        help="Explicit authorization to publish an assessment JSON previously produced",
    )
    pub.add_argument("--repo", required=True)
    pub.add_argument("--pr", type=int, required=True)
    pub.add_argument(
        "--assessment-file",
        required=True,
        help="JSON file from a prior --dry-run reconcile",
    )
    pub.add_argument("--json", action="store_true")

    return parser


def _load_event(
    event_file: str | None, event_name: str | None, env: Mapping[str, str]
) -> tuple[str | None, dict[str, Any] | None]:
    path = event_file or env.get("GITHUB_EVENT_PATH") or None
    name = event_name or env.get("GITHUB_EVENT_NAME") or None
    if not path:
        return name, None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise GuardError(f"cannot read event file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GuardError(f"event file is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GuardError("event file must contain a JSON object")
    return name, payload


def _assessment_exit_code(assessment: Assessment) -> int:
    """A closed PR is a successful no-op; observe remains advisory."""
    return 0 if assessment.closed or assessment.ok or assessment.mode == "observe" else 1


def main(argv: Sequence[str] | None = None, *, env: MutableMapping[str, str] | None = None,
         api: GitHubApi | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0].startswith("--") and arguments[0] != "--help":
        arguments.insert(0, "reconcile")
    args = build_parser().parse_args(arguments)
    environ: MutableMapping[str, str] = env if env is not None else os.environ
    if not args.command:
        build_parser().print_help()
        return 2

    try:
        client = api or GitHubApi(_token_from_env(environ))
    except GuardError as exc:
        print(f"a38-guard: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "reconcile":
            dry = bool(args.dry_run or args.no_publish)
            publish = not dry
            if args.all_open:
                if not args.repo:
                    raise GuardError("--all-open requires --repo")
                results = []
                exit_codes = []
                for number in list_open_pulls(client, _validate_repo(args.repo)):
                    assessment = reconcile_pull(
                        client, args.repo, number, dry_run=dry, publish=publish
                    )
                    results.append(assessment.to_json())
                    exit_codes.append(_assessment_exit_code(assessment))
                print(json.dumps({"results": results}, indent=2, sort_keys=True))
                # Fail the process if any enforce assessment failed.
                return 1 if any(exit_codes) else 0

            event_name, payload = _load_event(args.event_file, args.event_name, environ)
            if payload is not None:
                if not event_name:
                    raise GuardError("event name required with event file")
                result = reconcile_event(
                    client,
                    event_name=event_name,
                    payload=payload,
                    repo=args.repo,
                    pr=args.pr,
                    dry_run=dry,
                    publish=publish,
                )
                out = result.to_json() if isinstance(result, Assessment) else result
                print(json.dumps(out, indent=2, sort_keys=True))
                if isinstance(result, Assessment):
                    return _assessment_exit_code(result)
                return 0

            if not args.repo or args.pr is None:
                raise GuardError("reconcile requires --repo and --pr, or an event file")
            assessment = reconcile_pull(
                client, args.repo, args.pr, dry_run=dry, publish=publish
            )
            print(json.dumps(assessment.to_json(), indent=2, sort_keys=True))
            return _assessment_exit_code(assessment)

        if args.command == "publish":
            # Explicit authorization command: publish using the token for a stored assessment.
            with open(args.assessment_file, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise GuardError("assessment file must be a JSON object")
            # Re-assess live (never trust stale dry-run ok for enforce publish).
            assessment = reconcile_pull(
                client, args.repo, args.pr, dry_run=False, publish=True
            )
            print(json.dumps(assessment.to_json(), indent=2, sort_keys=True))
            return _assessment_exit_code(assessment)

        raise GuardError(f"unknown command {args.command}")
    except GuardError as exc:
        print(f"a38-guard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
