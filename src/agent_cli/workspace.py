"""Build argument vectors for git and tmux operations on a job workspace.

No execution, no I/O. Every function returns a list[str] that a caller hands to
an injected runner. The module is phase 3a of porting the existing runner's
workspace logic; the shapes below are reproduced exactly so the CLI can drive
the same operations without shelling out itself.
"""

from __future__ import annotations


def repo_slug(repo: str) -> str:
    """Stable filesystem-safe name for a repository.

    Every `/` becomes `__`, then the whole string is lowercased. Anything that
    is not exactly `owner/name` with both parts non-empty is rejected — the same
    shape of check and message as `jobs.py::job_id`.
    """
    if not isinstance(repo, str) or repo.count("/") != 1 or "" in repo.split("/"):
        raise ValueError("repo_slug requires owner/name")
    owner, name = repo.split("/")
    return f"{owner}__{name}".lower()


def bare_path(repos_root: str, repo: str) -> str:
    """Absolute path to the bare repository for `repo` under `repos_root`."""
    return f"{repos_root}/{repo_slug(repo)}.git"


def clone_argv(repo: str, dest: str) -> list[str]:
    """git clone --bare for the given GitHub repository into `dest`."""
    return ["git", "clone", "--bare", f"https://github.com/{repo}.git", dest]


def fetch_refspec_argv(bare: str) -> list[str]:
    """Configure the refspec so that fetch lands in refs/remotes/origin/*."""
    return [
        "git",
        f"--git-dir={bare}",
        "config",
        "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*",
    ]


def fetch_argv(bare: str) -> list[str]:
    """Fetch and prune so the local remote-tracking branches match origin."""
    return [
        "git",
        f"--git-dir={bare}",
        "fetch",
        "--prune",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
    ]


def worktree_add_argv(bare: str, path: str, branch: str) -> list[str]:
    """Create a detached worktree at `path` pointing at the given branch."""
    return [
        "git",
        f"--git-dir={bare}",
        "worktree",
        "add",
        "--detach",
        path,
        f"refs/remotes/origin/{branch}",
    ]


def worktree_remove_argv(bare: str, path: str) -> list[str]:
    """Force-remove the worktree at `path`."""
    return ["git", f"--git-dir={bare}", "worktree", "remove", "--force", path]


def worktree_prune_argv(bare: str) -> list[str]:
    """Prune stale worktree administrative files."""
    return ["git", f"--git-dir={bare}", "worktree", "prune"]


def new_session_argv(socket: str, session: str, worktree: str, wrapper: str) -> list[str]:
    """Start a detached tmux session rooted in the worktree."""
    return [
        "tmux",
        "-S",
        socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        worktree,
        wrapper,
    ]


def has_session_argv(socket: str, session: str) -> list[str]:
    """Check whether a session with the exact name exists.

    The `=` prefix on `-t` is load-bearing: tmux treats `=name` as an exact
    match. Without it `has-session` matches a prefix, so one session would be
    reported alive under another's name.
    """
    return ["tmux", "-S", socket, "has-session", "-t", f"={session}"]


def kill_session_argv(socket: str, session: str) -> list[str]:
    """Kill the session whose exact name matches.

    Same `=` prefix rule as `has_session_argv` for consistency.
    """
    return ["tmux", "-S", socket, "kill-session", "-t", f"={session}"]


def is_bare_argv(bare: str) -> list[str]:
    """Check whether `bare` is a bare Git repository."""
    return ["git", f"--git-dir={bare}", "rev-parse", "--is-bare-repository"]
