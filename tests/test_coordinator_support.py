"""Shared fakes for coordinator tests (no real network/models/tests)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from agent_cli.coordinator_config import RepositoryConfig, WorkerConfig
from agent_cli.runtime import Completed
from agent_cli.store import Store
from agent_cli.github_act import scan_github as _REAL_SCAN


def write_accounts(home: Path) -> None:
    (home / "github-accounts.json").write_text(
        json.dumps(
            {
                "accounts": {
                    "worker": {
                        "login": "worker-bot",
                        "gh_config_dir": "/test/gh-worker",
                        "git": {
                            "name": "Worker Bot",
                            "email": "worker@example.com",
                            "signing_format": "ssh",
                            "signing_key": "/test/worker.key",
                        },
                    },
                    "reviewer": {
                        "login": "review-bot",
                        "gh_config_dir": "/test/gh-review",
                    },
                },
                "sessions": {
                    "worker-session": "worker",
                    "review-session": "reviewer",
                },
            }
        ),
        encoding="utf-8",
    )
    (home / "ai-accounts.json").write_text(
        json.dumps(
            {
                "accounts": {
                    "grok-w": {"provider": "grok", "config_dir": "/test/grok"},
                    "codex-w": {"provider": "codex", "config_dir": "/test/codex"},
                },
                "roles": {
                    "impl": {
                        "account": "grok-w",
                        "model": "grok-model",
                        "access": "workspace-write",
                    },
                    "rev": {
                        "account": "grok-w",
                        "model": "grok-model",
                        "access": "read-only",
                    },
                    "codex-rev": {
                        "account": "codex-w",
                        "model": "codex-model",
                        "access": "read-only",
                    },
                },
                "sessions": {
                    "worker-session": {
                        "interactive": "impl",
                        "lanes": {
                            "grok:implementer": "impl",
                            "grok:reviewer": "rev",
                            "grok:pr-reviewer-quality": "rev",
                            "grok:pr-reviewer-logic": "rev",
                            "codex:pr-reviewer-quality": "codex-rev",
                            "codex:pr-reviewer-logic": "codex-rev",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def make_session(store: Store, sid: str, skills: list[str]) -> None:
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "status": "active",
            "skills": skills,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


def make_worker(root: Path) -> WorkerConfig:
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    return WorkerConfig(
        session_id="worker-session",
        review_session="review-session",
        workspace_root=work.resolve(),
        repositories={
            "example/project": RepositoryConfig(
                repo="example/project",
                base="develop",
                publication_repo="example/project",
                check_argv=("/operator/checks", "--full"),
                readiness_argv=("/operator/readiness",),
            )
        },
        reply_logins=("human-owner",),
        poll_seconds=30,
        lane_timeout=60,
        check_timeout=30,
    )


class FakeGh:
    """Minimal fake gh/git transport for coordinator ticks."""

    def __init__(self) -> None:
        self.comments: list[dict[str, Any]] = []
        self.pr_comments: list[dict[str, Any]] = []
        # Optional multi-page shapes for --paginate --slurp regression coverage.
        # When set, each entry is one GitHub API page (list of items).
        self.issue_pages: list[list[dict[str, Any]]] | None = None
        self.comment_pages: list[list[dict[str, Any]]] | None = None
        self.last_login = "worker-bot"
        self.reviews: list[dict[str, Any]] = []
        self.issues = [
            {
                "number": 7,
                "id": 700,
                "user": {"login": "human-owner", "type": "User"},
                "title": "Fix widget",
                "body": "Please fix",
                "html_url": "https://github.com/example/project/issues/7",
                "state": "open",
                "updated_at": "2026-09-01T00:00:00Z",
                "assignees": [{"login": "worker-bot"}],
            }
        ]
        self.pr: dict[str, Any] = {
            "number": 42,
            "url": "https://github.com/example/project/pull/42",
            "state": "OPEN",
            "isDraft": True,
            "author": {"login": "worker-bot"},
            "headRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "baseRefName": "develop",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [],
            "mergedAt": None,
            "mergeCommit": None,
            "mergedBy": None,
        }
        self.head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.base = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self.dirty = False
        self.commits_ahead = False
        self.signed = True
        self.workflow_runs: list[dict[str, Any]] = []
        self.model_outputs: dict[str, str] = {
            "implementer": "STATUS: complete\nRESULT: done\nSUMMARY_EN: Correct widget initialization.\nSUMMARY_DE: Widget-Initialisierung korrigiert.\npatched\n",
            "reviewer": "STATUS: complete\nRESULT: approved\n",
            "pr-reviewer-quality": "STATUS: complete\nRESULT: approved\n",
            "pr-reviewer-logic": "STATUS: complete\nRESULT: approved\n",
        }
        self.launched: list[str] = []
        self.parallel_launch_seen = False
        self._lane_barrier = threading.Barrier(2)
        self._lane_lock = threading.Lock()
        self.pr_created = False
        self._inflight_roles: set[str] = set()
        self.check_rc = 0
        self.readiness_rc = 0
        self.force_push_attempted = False
        self.git_commands: list[list[str]] = []
        self.commit_used_S = False
        self.remotes = {}
        self.branch = "develop"
        self.branches = {"develop"}
        self.staged = False
        self.commit_count = 0

    def __call__(self, argv: list[str]) -> Completed:
        if argv and argv[0] == "env":
            if "gh" in argv:
                argv = argv[argv.index("gh") :]
            elif "git" in argv:
                argv = argv[argv.index("git") :]
            else:
                for i, part in enumerate(argv):
                    if part.startswith("/") or part.endswith("checks") or part.endswith("readiness"):
                        argv = argv[i:]
                        break

        if argv[:3] == ["gh", "api", "user"] or (
            len(argv) >= 4 and argv[0] == "gh" and argv[1] == "api" and argv[2] == "user"
        ):
            if "--jq" in argv:
                return Completed(0, "worker-bot", "")
            return Completed(0, json.dumps({"login": "worker-bot"}), "")

        if argv == ["gh", "api", "user", "--jq", ".login"]:
            return Completed(0, "worker-bot", "")

        joined = " ".join(argv)

        if "repos/example/project/issues?assignee=" in joined or (
            argv[0] == "gh" and argv[1] == "api" and "--paginate" in argv and "issues?assignee=" in argv[-1]
        ):
            pages = self.issue_pages
            if pages is not None:
                if "--slurp" not in argv:
                    # Real gh without --slurp concatenates page JSON — unparsable.
                    return Completed(0, "".join(json.dumps(page) for page in pages), "")
                return Completed(0, json.dumps(pages), "")
            if "--paginate" in argv and "--slurp" not in argv:
                return Completed(0, "invalid concatenated pages", "")
            return Completed(0, json.dumps(self.issues), "")

        if argv[0] == "gh" and argv[1] == "api" and str(argv[-1]).endswith("/issues/7"):
            issue = self.issues[0] if self.issues else {"number": 7, "state": "open", "assignees": []}
            if self.issue_pages:
                for page in self.issue_pages:
                    for item in page:
                        if item.get("number") == 7:
                            issue = item
                            break
            return Completed(0, json.dumps(issue), "")

        if "issues/7/events" in joined or "issues/7/timeline" in joined:
            return Completed(0, json.dumps([{"id": 701, "event": "assigned",
                "assignee": {"login": "worker-bot"}, "actor": {"login": "human-owner", "type": "User"},
                "created_at": "2026-09-01T00:00:00Z"}]), "")
        if argv[:2] == ["gh", "api"] and argv[-1] == "repos/example/project/pulls/42":
            target = self.remotes.get("origin", "https://github.com/example/project.git")
            publication = self.remotes.get("publication", target)
            def name(url): return url.removeprefix("https://github.com/").removesuffix(".git")
            merged = self.pr.get("state") == "MERGED"
            return Completed(0, json.dumps({
                "number": 42, "html_url": self.pr["url"], "state": "closed" if merged else self.pr["state"].lower(),
                "draft": self.pr["isDraft"], "user": self.pr["author"], "merged": merged,
                "merged_by": self.pr.get("mergedBy"), "merged_at": self.pr.get("mergedAt"),
                "merge_commit_sha": (self.pr.get("mergeCommit") or {}).get("oid"),
                "head": {"sha": self.pr["headRefOid"], "ref": self.pr.get("headRefName", self.branch),
                         "repo": {"full_name": name(publication)}},
                "base": {"ref": self.pr["baseRefName"], "sha": self.base, "repo": {"full_name": name(target)}},
            }), "")

        if "issues/7/comments" in joined:
            if "-X" in argv and "POST" in argv:
                return Completed(0, json.dumps({"id": 1, "html_url": "https://x/1"}), "")
            pages = self.comment_pages
            if pages is not None:
                if "--slurp" not in argv:
                    return Completed(0, "".join(json.dumps(page) for page in pages), "")
                return Completed(0, json.dumps(pages), "")
            if "--paginate" in argv and "--slurp" not in argv:
                return Completed(0, "invalid concatenated pages", "")
            return Completed(0, json.dumps(self.comments), "")

        if argv[:3] == ["gh", "issue", "comment"]:
            body = argv[argv.index("--body") + 1]
            self.comments.append(
                {
                    "id": len(self.comments) + 1,
                    "body": body,
                    "html_url": "https://x/c",
                    "user": {"login": "worker-bot"},
                }
            )
            return Completed(0, f"https://github.com/example/project/issues/7#issuecomment-{self.comments[-1]['id']}", "")

        if argv[:3] == ["gh", "pr", "view"]:
            if argv[3] != "42" and not self.pr_created:
                return Completed(1, "", "no pull request found for branch")
            # gh pr view's mapped actor omits the REST account type.
            view = dict(self.pr)
            if isinstance(view.get("mergedBy"), dict):
                view["mergedBy"] = {"login": view["mergedBy"].get("login")}
            return Completed(0, json.dumps(view), "")

        if argv[:3] == ["gh", "pr", "create"]:
            self.pr_created = True
            self.pr["number"] = 42
            return Completed(0, self.pr["url"], "")

        if "issues/42/comments" in joined:
            return Completed(0, json.dumps(self.pr_comments), "")
        if argv[:3] == ["gh", "pr", "comment"]:
            body = argv[argv.index("--body") + 1]
            cid = 100 + len(self.pr_comments)
            self.pr_comments.append({"id": cid, "body": body, "user": {"login": "worker-bot"},
                                     "html_url": f"https://github.com/example/project/pull/42#issuecomment-{cid}"})
            return Completed(0, self.pr_comments[-1]["html_url"], "")

        if argv[:3] == ["gh", "pr", "ready"]:
            self.pr["isDraft"] = False
            return Completed(0, "", "")

        if "pulls/42/reviews" in joined and "-X" in argv:
            body = ""
            commit_id = ""
            event = "COMMENT"
            for part in argv:
                if part.startswith("body="):
                    body = part[5:]
                if part.startswith("event="):
                    event = part[6:]
                if part.startswith("commit_id="):
                    commit_id = part[10:]
            if event == "APPROVE" and not commit_id:
                return Completed(1, "", "commit_id required")
            review = {
                "id": len(self.reviews) + 1,
                "body": body,
                "html_url": "https://x/r",
                "state": "APPROVED" if event == "APPROVE" else "COMMENTED",
                "commit_id": commit_id or self.head,
                "user": {"login": self.last_login},
            }
            self.reviews.append(review)
            return Completed(0, json.dumps(review), "")

        if argv[:2] == ["gh", "api"] and "/pulls/42/reviews/" in argv[-1]:
            rid = int(argv[-1].rsplit("/", 1)[1])
            return Completed(0, json.dumps(next(r for r in self.reviews if r["id"] == rid)), "")
        if "pulls/42/reviews" in joined:
            return Completed(0, json.dumps(self.reviews), "")

        if "actions/runs" in joined and "/logs" in joined:
            return Completed(0, "failing log line\n", "")

        if "actions/runs" in joined:
            return Completed(
                0,
                json.dumps(
                    {"total_count": len(self.workflow_runs), "workflow_runs": self.workflow_runs}
                ),
                "",
            )

        if argv[0] == "git":
            return self._git(argv)

        if argv[0] == "/operator/checks" or argv[:1] == ["/operator/checks"]:
            return Completed(self.check_rc, "ok" if self.check_rc == 0 else "fail", "")

        if argv[0] == "/operator/readiness" or "/operator/readiness" in argv:
            return Completed(self.readiness_rc, "ready" if self.readiness_rc == 0 else "no", "")

        if argv[0] == "gh" and argv[1] == "api" and "comments" in joined:
            pages = self.comment_pages
            if pages is not None:
                if "--slurp" not in argv:
                    return Completed(0, "".join(json.dumps(page) for page in pages), "")
                return Completed(0, json.dumps(pages), "")
            if "--paginate" in argv and "--slurp" not in argv:
                return Completed(0, "invalid concatenated pages", "")
            return Completed(0, json.dumps(self.comments), "")

        return Completed(1, "", f"unhandled: {argv}")

    def _git(self, argv: list[str]) -> Completed:
        self.git_commands.append(list(argv))
        args = argv[1:]
        if args and args[0] == "-C":
            args = args[2:]
        if not args:
            return Completed(1, "", "empty git")
        cmd = args[0]
        if cmd == "clone":
            dest = Path(args[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()
            self.remotes = {"origin": args[-2]}
            self.head = self.base
            self.branch = args[args.index("--branch") + 1] if "--branch" in args else "develop"
            self.branches = {self.branch}
            return Completed(0, "", "")
        if cmd == "remote":
            if len(args) == 1:
                return Completed(0, "\n".join(self.remotes), "")
            action = args[1]
            name = args[2]
            if action == "get-url":
                return Completed(0, self.remotes[name], "") if name in self.remotes else Completed(2, "", "missing remote")
            if action == "add" and name not in self.remotes:
                self.remotes[name] = args[3]
                return Completed(0, "", "")
            if action == "rename" and name in self.remotes:
                self.remotes[args[3]] = self.remotes.pop(name)
                return Completed(0, "", "")
            return Completed(1, "", "invalid remote mutation")
        if cmd == "fetch":
            return Completed(0 if "origin" in args and "origin" in self.remotes else 1, "", "")
        if cmd == "rev-parse":
            if "--abbrev-ref" in args:
                return Completed(0, self.branch + "\n", "")
            if args[-1].endswith("develop"):
                return Completed(0, self.base + "\n", "")
            if args[-1] == "HEAD":
                return Completed(0, self.head + "\n", "")
            return Completed(1, "", "unknown revision")
        if cmd == "checkout":
            if "-b" in args:
                branch = args[args.index("-b") + 1]
                if branch in self.branches:
                    return Completed(1, "", "branch exists")
                self.branches.add(branch)
                self.branch = branch
                self.head = args[-1]
                return Completed(0, "", "")
            return Completed(1, "", "unexpected checkout mutation")
        if cmd == "merge-base" and "--is-ancestor" in args:
            return Completed(0 if args[-2] == self.base else 1, "", "")
        if cmd == "log":
            return Completed(0, f"{self.head} implement\n" if self.commits_ahead else "", "")
        if cmd == "status":
            return Completed(0, " M file.py\n" if self.dirty or self.staged else "", "")
        if cmd == "add":
            self.staged = self.dirty
            return Completed(0, "", "")
        if cmd == "diff":
            if "--quiet" in args:
                return Completed(1 if self.staged else 0, "", "")
            return Completed(0, "", "")
        if cmd == "commit":
            if not self.staged:
                return Completed(1, "", "nothing to commit")
            self.commit_used_S = "-S" in args
            self.commits_ahead = True
            self.head = format((12 + self.commit_count) % 16, 'x') * 40
            self.commit_count += 1
            self.dirty = self.staged = False
            return Completed(0, "", "")
        if cmd == "push":
            if "--force" in args or "-f" in args:
                self.force_push_attempted = True
                return Completed(1, "", "refusing force")
            if args[-1] != f"HEAD:refs/heads/{self.branch}":
                return Completed(1, "", "unexpected push ref")
            self.pr["headRefOid"] = self.head
            return Completed(0, "", "")
        if cmd == "verify-commit":
            return Completed(0 if self.signed else 1, "", "")
        if cmd == "cat-file":
            return Completed(0, "gpgsig -----BEGIN\n", "")
        return Completed(1, "", f"unhandled git: {args}")

    def account_runner(self, base, *, login="worker-bot"):
        fake = self

        def scoped(argv: list[str]) -> Completed:
            # This replaces Account.runner, so its seam receives gh/git, not env.
            assert argv[0] in {"gh", "git"}
            if "gh" in argv:
                cmd = argv[argv.index("gh") :]
                if cmd == ["gh", "api", "user", "--jq", ".login"]:
                    return Completed(0, login, "")
                if cmd == ["gh", "api", "user"]:
                    return Completed(0, json.dumps({"login": login}), "")
                fake.last_login = login
                return base(cmd)
            if "git" in argv:
                idx = argv.index("git")
                return base(argv[idx:])
            return base(argv)

        return scoped


def patch_account_runners(monkeypatch: Any, fake: FakeGh) -> None:
    from agent_cli import github_accounts

    def runner(self, base, *, require_git=False):  # noqa: ANN001
        return fake.account_runner(base, login=self.login)

    monkeypatch.setattr(github_accounts.Account, "runner", runner)


def lane_runner(fake: FakeGh):
    def run(argv: list[str], stdin: str | None = None) -> Completed:
        role = "implementer"
        joined = " ".join(argv) + "\n" + (stdin or "")
        for name in ("pr-reviewer-quality", "pr-reviewer-logic", "reviewer", "implementer"):
            if name in joined:
                role = name
                break
        if role.startswith("pr-reviewer"):
            with fake._lane_lock:
                fake._inflight_roles.add(role)
            fake._lane_barrier.wait(timeout=3)
            with fake._lane_lock:
                fake.parallel_launch_seen = {"pr-reviewer-quality", "pr-reviewer-logic"} <= fake._inflight_roles
        fake.launched.append(role)
        if role == "implementer":
            assert "--deny" in argv and "Bash" in argv
            assert "--no-subagents" in argv
            assert "--disable-web-search" in argv
            assert "timeout" not in argv
        out = fake.model_outputs.get(role, "STATUS: partial\n")
        if role.startswith("pr-reviewer"):
            fake._lane_barrier.wait(timeout=3)
            with fake._lane_lock:
                fake._inflight_roles.discard(role)
        return Completed(0, out, "")

    return run


def scan_done(store_, runner_):
    """Use the real activity executor against fake GitHub effects and facts."""
    return _REAL_SCAN(store_, runner_)


def patch_command_runner(monkeypatch, fake):
    """Only operator check/readiness processes are replaced by this test transport."""
    def command(argv, *, timeout, cwd=None, stdin_text=None, env=None, clear_ambient_github=False):
        assert cwd and timeout > 0
        env = env or {}
        assert env.get("AGENT_COORDINATOR_HEAD") == fake.head
        if argv[0] == "/operator/checks":
            return Completed(fake.check_rc, "configured full-check result", "")
        if argv[0] == "/operator/readiness":
            assert clear_ambient_github
            return Completed(fake.readiness_rc, json.dumps({
                "head": fake.head, "base": fake.base, "contributing_ok": True,
                "deviation": {"declared": False},
            }), "")
        raise AssertionError(f"unconfigured test command: {argv}")
    monkeypatch.setattr("agent_cli.coordinator_runtime.run_bounded", command)
    monkeypatch.setattr("agent_cli.coordinator_github.run_bounded", command)
