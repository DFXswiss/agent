"""Device-local GitHub account bindings for static executors.

No configured account means no GitHub execution. Credentials stay in each
account's gh configuration directory, never in activities or this manifest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime import Completed
from .store import StoreError

Runner = Callable[[list[str]], Completed]
CONFIG_FILE = "github-accounts.json"
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_TOKEN_ENV = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GIT_ASKPASS", "SSH_ASKPASS",
)


class AccountError(StoreError):
    """Configuration or authentication does not authorize this execution."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value:
        raise AccountError(f"{field} must be a non-empty single-line string")
    return value


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
                if "-C" in git_args:
                    index = git_args.index("-C") + 1
                    if index >= len(git_args):
                        raise AccountError("git -C requires a worktree path")
                    path = Path(git_args[index])
                    if not path.is_absolute() or ".." in path.parts:
                        raise AccountError("Git worktree path must be absolute without parent traversal")
                    for source, target in sorted(self.worktree_paths, key=lambda pair: len(pair[0]), reverse=True):
                        if path.is_relative_to(source):
                            git_args[index] = str(Path(target) / path.relative_to(source))
                            break
                command = [
                    "git", "-c", "core.askPass=", "-c", "http.extraHeader=",
                    "-c", "http.https://github.com/.extraHeader=", "-c", "credential.helper=",
                    "-c", "credential.helper=!gh auth git-credential",
                    "-c", f"user.name={identity['name']}", "-c", f"user.email={identity['email']}",
                    "-c", "commit.gpgsign=true", "-c", f"gpg.format={identity['signing_format']}",
                    "-c", f"user.signingkey={identity['signing_key']}", *git_args,
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
