"""Formal rule primitives of the operation types (03-api.md section 9; `contracts/ops/rules.json`, which the tests
compare with these constants and run against its positive and negative examples).

Pure functions on plaintext, deterministic, no network and no `git`:

- `repo_valid`: `owner/name` (`^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$`);
- `branch_name` / `branch_valid`: `git check-ref-format --branch` without git (no `..`, `@{`, `//`, `/.`, control
  characters, space or `~^:?*[\\`; no leading `-`, `/`, `.`; no trailing `/`, `.`, `.lock`; no component ending in
  `.lock`; not `@` or `HEAD`; at most 200 characters), after removing a leading `refs/heads/`;
- `ref_valid`: a branch or tag name by the branch rules, or a commit SHA of 7-40 lower-case hex digits;
- `path_valid`: repository-relative paths (no empty, `.`, `..` or `.git` segment - the latter case-insensitively -,
  no control character or backslash, at most 4096 characters);
- `protected`: fnmatch-style match of a normalised branch against the workgroup's patterns (`*` matches any
  characters except `/`, everything else literally);
- `title_valid`: the single-line title of `task.set_title` (1-120 characters, no C0 or C1 control character - NEL
  is a line break -, no line or paragraph separator);
- `QUESTION_LIMITS`: the question of `human.question` / `human.approval` (1-4000 characters, KOR-39);
- `find_secret`: the first deterministic secret pattern found in a text (`secret_detected` of `github.commit`; the
  scan itself runs on the snapshot of RUN-10).

Lengths are counted in Unicode code points (Python `len`), byte limits in bytes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEADS = "refs/heads/"
BRANCH_MAX = 200
BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\\]")
BRANCH_FORBIDDEN_SUBSTRINGS = ("..", "@{", "//", "/.")
BRANCH_FORBIDDEN_PREFIXES = ("-", "/", ".")
BRANCH_FORBIDDEN_SUFFIXES = ("/", ".", ".lock")
BRANCH_FORBIDDEN_COMPONENT_SUFFIXES = (".lock",)
BRANCH_FORBIDDEN_NAMES = ("@", "HEAD")
PATH_MAX = 4096
PATH_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f\\]")
PATH_FORBIDDEN_SEGMENTS = ("", ".", "..")
PATH_FORBIDDEN_SEGMENTS_CASEFOLD = (".git",)
TITLE_RE = re.compile("^[^\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]{1,120}$")
DEFAULT_PROTECTED: tuple[str, ...] = ()
"""No platform-side default (ADR-078 section 3, A17): GitHub's own branch protection applies; a workgroup may set
its own list."""
COMMIT_LIMITS = {"file_bytes": 10485760, "files": 500, "total_bytes": 52428800, "message_max_length": 4096}
CHECKOUT_LIMITS = {"archive_bytes": 536870912}
PULL_REQUEST_LIMITS = {"title_max_length": 256, "body_max_length": 65536, "patch_total_bytes": 1048576}
COMMENT_LIMITS = {"body_max_length": 65536}
SUBAGENT_LIMITS = {
    "purpose_max_length": 200,
    "prompt_max_length": 32768,
    "max_depth": 2,
    "max_running_per_task": 3,
    "max_total_per_task": 10,
}
TITLE_LIMITS = {"max_length": 120, "single_line": True}
QUESTION_LIMITS = {"question_max_length": 4000}
SECRET_PATTERNS = (
    ("pem_private_key", "-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    ("github_token", "gh[pou]_[A-Za-z0-9]{16,}"),
    ("github_pat", "github_pat_[A-Za-z0-9_]{16,}"),
    ("anthropic_key", "sk-ant-[A-Za-z0-9_-]{16,}"),
    ("openrouter_key", "sk-or-[A-Za-z0-9_-]{16,}"),
    ("aws_access_key", "AKIA[0-9A-Z]{16}"),
)
_SECRETS = tuple((name, re.compile(pattern)) for name, pattern in SECRET_PATTERNS)


def repo_valid(value: object) -> bool:
    return isinstance(value, str) and REPO_RE.fullmatch(value) is not None


def branch_name(value: str) -> str:
    """The branch without a leading `refs/heads/` (the normalised form every rule and the dedupe tag use)."""
    return value.removeprefix(HEADS)


def branch_valid(value: object) -> bool:
    """`git check-ref-format --branch` rules on the normalised name."""
    if not isinstance(value, str):
        return False
    name = branch_name(value)
    if not 1 <= len(name) <= BRANCH_MAX or BRANCH_FORBIDDEN_RE.search(name) or name in BRANCH_FORBIDDEN_NAMES:
        return False
    if any(part in name for part in BRANCH_FORBIDDEN_SUBSTRINGS):
        return False
    if name.startswith(BRANCH_FORBIDDEN_PREFIXES) or name.endswith(BRANCH_FORBIDDEN_SUFFIXES):
        return False
    return not any(component.endswith(BRANCH_FORBIDDEN_COMPONENT_SUFFIXES) for component in name.split("/"))


def ref_valid(value: object) -> bool:
    return isinstance(value, str) and (SHA_RE.fullmatch(value) is not None or branch_valid(value))


def path_valid(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= PATH_MAX or PATH_FORBIDDEN_RE.search(value):
        return False
    for segment in value.split("/"):
        if segment in PATH_FORBIDDEN_SEGMENTS or segment.casefold() in PATH_FORBIDDEN_SEGMENTS_CASEFOLD:
            return False
    return True


def _pattern(pattern: str) -> re.Pattern[str]:
    return re.compile("".join("[^/]*" if char == "*" else re.escape(char) for char in pattern))


def protected(branch: str, patterns: Iterable[str]) -> bool:
    """Whether the normalised `branch` matches one of the protected-branch patterns."""
    name = branch_name(branch)
    return any(_pattern(pattern).fullmatch(name) is not None for pattern in patterns)


def title_valid(value: object) -> bool:
    return isinstance(value, str) and TITLE_RE.fullmatch(value) is not None


def find_secret(text: str) -> str | None:
    """The ID of the first secret pattern found anywhere in `text`, else None."""
    for name, pattern in _SECRETS:
        if pattern.search(text):
            return name
    return None
