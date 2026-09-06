"""Device-local GitHub account bindings for static executors.

Covered executors load this manifest and refuse to run when it is absent or
empty. Credentials stay in each account's gh configuration directory, never in
activities or this manifest. Legacy ambient `gh` paths that do not load this
file are outside this module's enforcement; see docs/github-accounts.md.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .runtime import Completed
from .store import StoreError

Runner = Callable[[list[str]], Completed]
CONFIG_FILE = "github-accounts.json"
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_OWNER_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_TOKEN_ENV = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GIT_ASKPASS", "SSH_ASKPASS",
)
_NETWORK_GIT = frozenset({"fetch", "push", "pull", "clone"})
_GIT_OPTS_WITH_ARG = frozenset({
    "-C", "-c", "-o",
    "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env",
    "--buffered-output-size",
})
_TRANSFER_FLAGS = {
    "fetch": frozenset({"--prune", "-p", "--tags", "-t", "--no-tags", "-n",
                        "--quiet", "-q", "--verbose", "-v", "--dry-run", "--no-recurse-submodules"}),
    "push": frozenset({"--set-upstream", "-u", "--dry-run", "-n", "--porcelain",
                       "--quiet", "-q", "--verbose", "-v", "--atomic"}),
    "pull": frozenset({"--ff-only", "--no-rebase", "--quiet", "-q", "--verbose", "-v"}),
    "clone": frozenset({"--no-checkout", "-n", "--bare", "--single-branch",
                        "--no-single-branch", "--quiet", "-q", "--verbose", "-v"}),
}
_TRANSFER_VALUE_FLAGS = {
    "fetch": frozenset({"--depth", "--deepen", "--shallow-since", "--shallow-exclude", "--filter"}),
    "push": frozenset(),
    "pull": frozenset({"--depth"}),
    "clone": frozenset({"--depth", "--branch", "-b", "--filter"}),
}


class AccountError(StoreError):
    """Configuration or authentication does not authorize this execution."""


class GitHubHttpsRemoteError(AccountError):
    """A Git remote is not a safe HTTPS github.com URL.

    Messages must never include remote URL values (they may embed credentials).
    """


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value:
        raise AccountError(f"{field} must be a non-empty single-line string")
    return value


def ensure_github_https_remote(url: str) -> str:
    """Return ``owner/name`` for a credential-free HTTPS github.com remote.

    Rejects SSH, non-GitHub hosts, non-HTTPS schemes, embedded userinfo, and
    malformed paths. Error text never includes the URL value.
    """
    if not isinstance(url, str) or not url.strip() or "\x00" in url or "\n" in url or "\r" in url:
        raise GitHubHttpsRemoteError("remote URL is unsafe")
    text = url.strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        raise GitHubHttpsRemoteError("remote URL is unsafe") from None
    if parsed.scheme != "https":
        raise GitHubHttpsRemoteError("remote must be HTTPS GitHub")
    if parsed.username is not None or parsed.password is not None:
        raise GitHubHttpsRemoteError("remote URL must not contain credentials")
    if "@" in (parsed.netloc or ""):
        # urlparse missed userinfo; still refuse without echoing the value.
        raise GitHubHttpsRemoteError("remote URL must not contain credentials")
    try:
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        # Malformed ports and some bad netlocs raise ValueError; never echo URL.
        raise GitHubHttpsRemoteError("remote URL is unsafe") from None
    if host != "github.com" or port not in {None, 443}:
        raise GitHubHttpsRemoteError("remote must be HTTPS GitHub")
    if parsed.query or parsed.fragment:
        raise GitHubHttpsRemoteError("remote must be HTTPS GitHub owner/name")
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise GitHubHttpsRemoteError("remote must be HTTPS GitHub owner/name")
    owner, name = parts
    if _OWNER_NAME.fullmatch(owner) is None or _OWNER_NAME.fullmatch(name) is None:
        raise GitHubHttpsRemoteError("remote must be HTTPS GitHub owner/name")
    return f"{owner}/{name}"


def _looks_like_refspec(value: str) -> bool:
    if value.startswith("+") or value == "HEAD" or value.startswith("refs/"):
        return True
    if "://" in value or value.startswith("git@"):
        return False
    if ":" in value:
        source, _, dest = value.partition(":")
        if source in {"", "HEAD"} or source.startswith("refs/") or dest.startswith("refs/"):
            return True
    return False


def _looks_like_url(value: str) -> bool:
    if "://" in value or value.startswith("git@") or value.startswith("file:"):
        return True
    # scp-like host:path — not a Git refspec and not an absolute path
    if ":" in value and not value.startswith("/") and not _looks_like_refspec(value):
        host, _, rest = value.partition(":")
        if host and "/" not in host and rest and not rest.startswith(":"):
            return True
    return False


def _git_cwd_argv(cwd: str | None, *parts: str) -> list[str]:
    if cwd is None:
        return ["git", *parts]
    return ["git", "-C", cwd, *parts]


def _temp_remote_name() -> str:
    return f"_agent_url_{secrets.token_hex(8)}"


def _parse_get_url_stdout(stdout: str) -> list[str]:
    urls = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not urls:
        raise GitHubHttpsRemoteError("remote URL is not configured")
    return urls


def _remote_get_urls(run: Runner, cwd: str | None, remote: str, *, push: bool) -> list[str]:
    """Return effective URLs from ``git remote get-url`` (rewrites already applied)."""
    if not isinstance(remote, str) or not remote.strip() or "\x00" in remote or "\n" in remote or "\r" in remote:
        raise GitHubHttpsRemoteError("remote name is unsafe")
    name = remote.strip()
    if _looks_like_url(name):
        raise GitHubHttpsRemoteError("remote name is unsafe")
    argv = _git_cwd_argv(cwd, "remote", "get-url")
    if push:
        argv.append("--push")
    argv.extend(["--all", "--", name])
    completed = run(argv)
    if completed.returncode != 0:
        # Distinct push URL absent: fall back to fetch URLs (Git's usual behavior).
        if push:
            return _remote_get_urls(run, cwd, name, push=False)
        raise GitHubHttpsRemoteError("remote URL is not configured")
    return _parse_get_url_stdout(completed.stdout)


def _explicit_url_get_urls(run: Runner, cwd: str | None, url: str, *, push: bool) -> list[str]:
    """Resolve rewrite effects for an explicit URL via a command-scoped temporary remote."""
    if not isinstance(url, str) or not url.strip() or "\x00" in url or "\n" in url or "\r" in url:
        raise GitHubHttpsRemoteError("remote URL is unsafe")
    text = url.strip()
    token = _temp_remote_name()
    # Never print text; pass it only as a command-scoped git config value.
    argv = _git_cwd_argv(cwd, "-c", f"remote.{token}.url={text}", "remote", "get-url")
    if push:
        argv.append("--push")
    argv.extend(["--all", "--", token])
    completed = run(argv)
    if completed.returncode != 0:
        if push:
            return _explicit_url_get_urls(run, cwd, text, push=False)
        raise GitHubHttpsRemoteError("remote URL is not configured")
    return _parse_get_url_stdout(completed.stdout)


def resolve_effective_github_https_url(
    run: Runner,
    cwd: str | None,
    remote: str,
    *,
    push: bool = False,
) -> str:
    """Resolve one effective remote URL after pushurl and rewrite effects.

    Relies on ``git remote get-url [--push] --all``, which already applies
    ``insteadOf`` / ``pushInsteadOf``. Explicit URLs are resolved through a
    temporary command-scoped remote so the same Git rewrite path runs. Every
    resulting URL is validated; the first accepted URL string is returned.
    Callers that need ``owner/name`` should use ``validate_repo_remote``.
    """
    if _looks_like_url(remote):
        urls = _explicit_url_get_urls(run, cwd, remote, push=push)
    else:
        urls = _remote_get_urls(run, cwd, remote, push=push)
    for raw in urls:
        ensure_github_https_remote(raw)
    return urls[0]


def validate_repo_remote(run: Runner, cwd: str | None, remote: str) -> str:
    """Validate fetch and push effective URLs for ``remote``; return ``owner/name``."""
    fetch_url = resolve_effective_github_https_url(run, cwd, remote, push=False)
    push_url = resolve_effective_github_https_url(run, cwd, remote, push=True)
    fetch_repo = ensure_github_https_remote(fetch_url)
    push_repo = ensure_github_https_remote(push_url)
    if fetch_repo.casefold() != push_repo.casefold():
        raise GitHubHttpsRemoteError("fetch and push remotes resolve to different GitHub repositories")
    return fetch_repo


def _skip_git_option(args: list[str], index: int) -> int:
    arg = args[index]
    name = arg.split("=", 1)[0]
    if arg.startswith("--") and "=" in arg:
        return index + 1
    if arg in _GIT_OPTS_WITH_ARG or name in _GIT_OPTS_WITH_ARG:
        return index + 2
    return index + 1


def _split_git_command(git_args: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(git_args):
        arg = git_args[index]
        if arg == "--":
            index += 1
            break
        if arg.startswith("-"):
            index = _skip_git_option(git_args, index)
            continue
        return arg, git_args[index + 1 :]
    if index < len(git_args):
        return git_args[index], git_args[index + 1 :]
    return None, []


def _git_c_path(git_args: list[str]) -> str | None:
    if "-C" not in git_args:
        return None
    index = git_args.index("-C") + 1
    if index >= len(git_args):
        raise AccountError("git -C requires a worktree path")
    return git_args[index]


def _network_remote_targets(git_args: list[str]) -> list[str] | None:
    """Accept only explicitly supported single-remote transfers.

    Unknown options fail closed rather than guessing whether the next token is
    their value or a repository. Network calls permit only an optional single
    global -C; configuration/context overrides cannot differ between validation
    and execution. Other local Git commands remain available.
    """
    verb, rest = _split_git_command(git_args)
    if verb not in _NETWORK_GIT:
        return None
    leading = git_args[:len(git_args) - len(rest) - 1]
    if leading and (len(leading) != 2 or leading[0] != "-C"):
        raise GitHubHttpsRemoteError("unsupported git network configuration")
    operands: list[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg == "--":
            operands.extend(rest[index + 1:])
            break
        if not arg.startswith("-"):
            operands.append(arg)
            index += 1
            continue
        if arg in _TRANSFER_FLAGS[verb]:
            index += 1
            continue
        name, equal, value = arg.partition("=")
        if name not in _TRANSFER_VALUE_FLAGS[verb]:
            raise GitHubHttpsRemoteError("unsupported git network option")
        if not equal:
            index += 1
            if index >= len(rest):
                raise GitHubHttpsRemoteError("missing git network option value")
            value = rest[index]
        if not value or value.startswith("-"):
            raise GitHubHttpsRemoteError("invalid git network option value")
        index += 1
    if not operands or not operands[0] or operands[0].startswith("-"):
        raise GitHubHttpsRemoteError(f"git {verb} requires an explicit remote")
    if verb == "clone" and len(operands) > 2:
        raise GitHubHttpsRemoteError("unsupported git clone form")
    first = operands[0]
    if _looks_like_refspec(first) and not _looks_like_url(first):
        raise GitHubHttpsRemoteError(f"git {verb} requires an explicit remote")
    return [first]


def _map_worktree_path(path: Path, mappings: tuple[tuple[str, str], ...]) -> str:
    if not path.is_absolute() or ".." in path.parts:
        raise AccountError("Git worktree path must be absolute without parent traversal")
    mapped = str(path)
    for source, target in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True):
        if path.is_relative_to(source):
            mapped = str(Path(target) / path.relative_to(source))
            break
    return mapped


@dataclass(frozen=True)
class Account:
    name: str
    login: str
    gh_config_dir: str
    git_identity: dict[str, str] | None = None
    command_prefix: tuple[str, ...] = ()
    worktree_paths: tuple[tuple[str, str], ...] = ()

    def runner(self, base: Runner, *, require_git: bool = False) -> Runner:
        if require_git and self.git_identity is None:
            raise AccountError(f"Git identity is not configured for account {self.name}")

        def scoped(argv: list[str]) -> Completed:
            if not argv or argv[0] not in {"gh", "git"}:
                raise AccountError("GitHub account runner accepts only gh and git")
            prefix = ["env"]
            for key in _TOKEN_ENV:
                prefix.extend(["-u", key])
            prefix.extend([
                f"GH_CONFIG_DIR={self.gh_config_dir}", "GH_HOST=github.com", "GH_PROMPT_DISABLED=1",
                "GIT_TERMINAL_PROMPT=0", "GIT_SSH_COMMAND=false",
            ])
            command = list(argv)
            if argv[0] == "git":
                identity = self.git_identity
                if identity is None:
                    raise AccountError(f"Git identity is not configured for account {self.name}")
                prefix.extend([
                    "GIT_CONFIG_COUNT=0",
                    f"GIT_AUTHOR_NAME={identity['name']}",
                    f"GIT_AUTHOR_EMAIL={identity['email']}",
                    f"GIT_COMMITTER_NAME={identity['name']}",
                    f"GIT_COMMITTER_EMAIL={identity['email']}",
                ])
                git_args = list(argv[1:])
                targets = _network_remote_targets(git_args)
                if targets is not None:
                    # Keep host cwd until each nested call maps it exactly once.
                    cwd = _git_c_path(git_args)
                    for target in targets:
                        validate_repo_remote(scoped, cwd, target)
                if "-C" in git_args:
                    index = git_args.index("-C") + 1
                    if index >= len(git_args):
                        raise AccountError("git -C requires a worktree path")
                    git_args[index] = _map_worktree_path(Path(git_args[index]), self.worktree_paths)
                network_config = (
                    ["-c", "fetch.recurseSubmodules=false", "-c", "push.recurseSubmodules=no",
                     "-c", "submodule.recurse=false"] if targets is not None else []
                )
                command = [
                    "git", "-c", "core.askPass=", "-c", "http.extraHeader=",
                    "-c", "http.https://github.com/.extraHeader=", "-c", "credential.helper=",
                    "-c", "credential.helper=!gh auth git-credential",
                    "-c", f"user.name={identity['name']}", "-c", f"user.email={identity['email']}",
                    "-c", "commit.gpgsign=true", "-c", f"gpg.format={identity['signing_format']}",
                    "-c", f"user.signingkey={identity['signing_key']}", *network_config, *git_args,
                ]
            return base([*self.command_prefix, *prefix, *command])

        try:
            result = scoped(["gh", "api", "user", "--jq", ".login"])
        except OSError as exc:
            raise AccountError(f"Cannot authenticate account {self.name}") from exc
        # Do not copy authentication stderr (which may contain credentials) into the store.
        if result.returncode != 0:
            raise AccountError(f"Authentication failed for account {self.name}; no fallback")
        if result.stdout.strip().casefold() != self.login.casefold():
            raise AccountError(f"Authenticated login does not match account {self.name}; no fallback")
        return scoped


@dataclass(frozen=True)
class Accounts:
    accounts: dict[str, Account]
    sessions: dict[str, str]

    def for_session(self, session_id: str) -> Account:
        name = self.sessions.get(session_id)
        if name is None:
            raise AccountError(f"No GitHub account configured for session {session_id}")
        return self.accounts[name]


def load_accounts(home: Path) -> Accounts:
    path = home / CONFIG_FILE
    if not path.exists():
        return Accounts({}, {})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountError(f"Cannot read {CONFIG_FILE}") from exc
    if not isinstance(raw, dict) or set(raw) - {"accounts", "sessions"}:
        raise AccountError(f"{CONFIG_FILE} accepts only accounts and sessions")
    accounts_raw = raw.get("accounts")
    sessions_raw = raw.get("sessions")
    if accounts_raw is None:
        accounts_raw = {}
    if sessions_raw is None:
        sessions_raw = {}
    if not isinstance(accounts_raw, dict) or not isinstance(sessions_raw, dict):
        raise AccountError("accounts and sessions must be objects or null")
    accounts: dict[str, Account] = {}
    for name, entry in accounts_raw.items():
        _text(name, "account name")
        if not isinstance(entry, dict) or set(entry) - {"login", "gh_config_dir", "git", "command_prefix", "worktree_paths"}:
            raise AccountError(f"Invalid account fields for {name}")
        login = _text(entry.get("login"), "login")
        if _LOGIN.fullmatch(login) is None:
            raise AccountError(f"Invalid login for {name}")
        config_dir = _text(entry.get("gh_config_dir"), "gh_config_dir")
        if not Path(config_dir).is_absolute():
            raise AccountError("gh_config_dir must be an absolute path")
        identity = entry.get("git")
        if identity is not None:
            keys = {"name", "email", "signing_format", "signing_key"}
            if not isinstance(identity, dict) or set(identity) != keys:
                raise AccountError(f"git for {name} requires name, email, signing_format, signing_key")
            identity = {key: _text(value, f"git.{key}") for key, value in identity.items()}
            if identity["signing_format"] not in {"ssh", "openpgp", "x509"}:
                raise AccountError(f"Invalid signing_format for {name}")
        command_prefix = entry.get("command_prefix")
        if command_prefix is None:
            command_prefix = []
        if not isinstance(command_prefix, list):
            raise AccountError("command_prefix must be an argv array")
        command_prefix = tuple(_text(item, "command_prefix item") for item in command_prefix)
        mappings = entry.get("worktree_paths")
        if mappings is None:
            mappings = {}
        if not isinstance(mappings, dict):
            raise AccountError("worktree_paths must map absolute host paths to executor paths")
        paths = []
        for source, target in mappings.items():
            source = _text(source, "worktree source")
            target = _text(target, "worktree target")
            if not Path(source).is_absolute() or not Path(target).is_absolute():
                raise AccountError("worktree paths must be absolute")
            if ".." in Path(source).parts or ".." in Path(target).parts:
                raise AccountError("worktree paths must not contain parent traversal")
            paths.append((str(Path(source)), str(Path(target))))
        accounts[name] = Account(name, login, config_dir, identity, command_prefix, tuple(paths))
    sessions: dict[str, str] = {}
    for session, name in sessions_raw.items():
        _text(session, "session id")
        _text(name, "session account")
        if name not in accounts:
            raise AccountError(f"Session {session} references an unknown account")
        sessions[session] = name
    return Accounts(accounts, sessions)
