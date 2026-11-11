"""Comment-mandated conflict-resolution tests with fake transports only."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_cli.conflict_mandate import (
    ConflictsError,
    mandate_matches,
    source_lock_key,
    tick,
)
from agent_cli.conflict_mandate_config import load_conflict_config
from agent_cli.conflict_mandate_github import ConflictTransientError
from agent_cli.conflict_mandate_state import (
    Request,
    context,
    safe,
    save_state,
    unfinished_requests,
)
from agent_cli.coordinator_common import prompt_prohibitions
from agent_cli.github_accounts import _network_remote_targets
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_coordinator_support import make_session, patch_account_runners, write_accounts


def config(root: Path) -> dict[str, Any]:
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    return {
        "workers": {
            "worker-session": {
                "workspace_root": str(work),
                "repositories": {"example/project": {"check_argv": ["/operator/checks"]}},
                "request_logins": ["AuthorizedHuman"],
                "poll_seconds": 30,
                "lane_timeout": 60,
                "check_timeout": 60,
                "max_rebase_steps": 3,
            }
        }
    }


def worker(root: Path):
    (root / "conflicts.json").write_text(json.dumps(config(root)), encoding="utf-8")
    return load_conflict_config(root)["worker-session"]


class FakeConflictTransport:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.comments: list[dict[str, Any]] = []
        self.pr_comments: list[dict[str, Any]] = []
        self.clone_path: Path | None = None
        self.pull = {
            "state": "open",
            "mergeable": False,
            "head": {
                "ref": "feature", "sha": "a" * 40,
                "repo": {"full_name": "worker-bot/project"},
            },
            "base": {"ref": "develop", "sha": "b" * 40, "repo": {"full_name": "example/project"}},
        }
        self.permissions = {"push": True}
        self.status = ""
        self.changed = {"src/app.py"}
        self.conflict_file = "src/app.py"
        self.rebase_rc = 1
        self.continue_rc = 0
        self.rebase_in_progress = True
        self.push_rc = 0
        self.push_stderr = "lease rejected"
        self.remote_head_sha = "a" * 40
        self.remote_base_sha = "b" * 40
        self.rev_parse_head = "a" * 40
        self.remote_read_fails = False
        self.signed_ok = True
        self.merge_commits = False
        self.unmerged_state = "UU"
        self.calls: list[list[str]] = []
        self.raw_calls: list[list[str]] = []
        self.lanes: list[str] = []
        self.lane_results: list[str] = [
            "STATUS: complete\nRESULT: done\n",
        ]
        self.lane_output: str | None = None
        self.lane_dirty = False
        self.lane_changes: set[str] | None = None
        self.preexisting_dirty = False
        self.reactions: list[int] = []
        self.check_rc = 0
        self.check_calls: list[dict[str, Any]] = []
        self.github_down = False
        self.comment_posts = 0
        self.comment_post_rc = 0

    def _git(self, argv: list[str]) -> Completed:
        args = argv[argv.index("git") + 1 :]
        while args and args[0] in {"-C", "-c"}:
            args = args[2:]
        self.calls.append(["git", *args])
        if args[:1] == ["rebase"]:
            if "--abort" in args:
                self.rebase_in_progress = False
                return Completed(0, "", "")
            if "--continue" in args:
                if self.continue_rc == 0:
                    self.lane_dirty = False
                    self.rebase_in_progress = False
                return Completed(self.continue_rc, "", "")
            self.rebase_in_progress = True
            return Completed(self.rebase_rc, "", "conflict")
        if args[:1] == ["clone"]:
            dest = Path(args[-1])
            self.clone_path = dest
            conflict_path = dest / self.conflict_file
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            git_dir = Path(args[args.index("--separate-git-dir") + 1])
            git_dir.mkdir(parents=True, exist_ok=True)
            (dest / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            conflict_path.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\n", encoding="utf-8")
            return Completed(0, "", "")
        if args[:1] == ["remote"]:
            if "get-url" in args:
                name = args[args.index("get-url") + 1]
                repo = "worker-bot/project" if name == "publication" else "example/project"
                return Completed(0, f"https://github.com/{repo}.git\n", "")
            return Completed(0, "", "")
        if args[:1] == ["fetch"]:
            return Completed(0, "", "")
        if args[:1] == ["rev-parse"]:
            if "--git-path" in args:
                marker = args[args.index("--git-path") + 1]
                if self.rebase_in_progress and marker in {"rebase-merge", "rebase-apply"}:
                    return Completed(0, f".git/{marker}\n", "")
                return Completed(0, f".git/not-{marker}\n", "")
            if args[-1].startswith("origin/"):
                return Completed(0, self.remote_base_sha + "\n", "")
            return Completed(0, self.rev_parse_head + "\n", "")
        if args[:1] == ["checkout"]:
            if "--" in args:
                restored = set(args[args.index("--") + 1:])
                self.changed.difference_update(restored)
            return Completed(0, "", "")
        if args[:1] == ["rev-list"]:
            if "--merges" in args:
                return Completed(0, "c" * 40 + "\n" if self.merge_commits else "", "")
            return Completed(0, self.pull["head"]["sha"] + "\n", "")
        if args[:1] == ["status"]:
            if "--porcelain=v2" in args:
                if (
                    self.clone_path is not None
                    and self.rebase_rc != 0
                    and self.rebase_in_progress
                ):
                    return Completed(
                        0,
                        (
                            f"u {self.unmerged_state} N... 100644 100644 100644 "
                            f"100644 {'1' * 40} {'2' * 40} {'3' * 40} {self.conflict_file}\n"
                        ),
                        "",
                    )
                return Completed(0, "", "")
            if self.status:
                return Completed(0, self.status, "")
            if (self.lane_dirty or self.preexisting_dirty) and self.clone_path is not None:
                if "-z" in args:
                    return Completed(
                        0, "".join(f"{path}\0" for path in sorted(self.changed)), ""
                    )
                return Completed(0, "".join(f" M {path}\n" for path in sorted(self.changed)), "")
            return Completed(0, "", "")
        if args[:1] == ["log"]:
            return Completed(0, "Replay task.\n", "")
        if args[:1] == ["add"]:
            return Completed(0, "", "")
        if args[:1] == ["diff"]:
            if "--numstat" in args:
                return Completed(0, f"1\t1\t{self.conflict_file}\n", "")
            if "--name-only" in args:
                paths = (
                    self.changed
                    if (self.lane_dirty or self.preexisting_dirty)
                    and self.clone_path is not None
                    else set()
                )
                return Completed(0, "".join(f"{path}\0" for path in sorted(paths)), "")
            return Completed(0, "", "")
        if args[:1] == ["ls-files"]:
            return Completed(0, "", "")
        if args[:1] == ["merge-base"]:
            return Completed(0, "", "")
        if args[:1] == ["verify-commit"]:
            return Completed(0 if self.signed_ok else 1, "", "")
        if args[:1] == ["push"]:
            return Completed(self.push_rc, "", self.push_stderr if self.push_rc else "")
        if args[:1] == ["ls-remote"]:
            if self.remote_read_fails:
                return Completed(1, "", "network unavailable")
            ref = args[-1]
            return Completed(0, f"{self.remote_head_sha}\t{ref}\n", "")
        return Completed(0, "", "")

    def __call__(self, argv: list[str]) -> Completed:
        self.raw_calls.append(list(argv))
        self.calls.append(argv)
        if self.github_down:
            return Completed(1, "", "GitHub unreachable")
        if "gh" not in argv and "git" in argv:
            return self._git(argv)
        command = argv[argv.index("gh") :] if "gh" in argv else argv
        if command[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "worker-bot"}), "")
        if (
            command[:2] == ["gh", "api"]
            and command[-1].startswith("repos/")
            and "/issues/comments" in command[-1]
        ):
            return Completed(0, json.dumps([self.comments]), "")
        if command[:2] == ["gh", "api"] and command[-1].startswith("repos/") and command[-1].endswith("/reactions"):
            return Completed(0, json.dumps([]), "")
        joined = " ".join(command)
        if command[:3] == ["gh", "api", "-X"] and command[3] == "POST":
            if any("reactions" in value for value in command):
                comment_id = int(next(value for value in command if "reactions" in value).split("/")[-2])
                self.reactions.append(comment_id)
                return Completed(0, "", "")
            body = command[-1].split("=", 1)[1] if command[-1].startswith("body=") else ""
            self.comment_posts += 1
            if self.comment_post_rc:
                self.comment_post_rc -= 1
                return Completed(self.comment_post_rc + 1, "", "comment failed")
            self.pr_comments.append(
                {
                    "id": 900 + len(self.pr_comments),
                    "body": body,
                    "user": {"login": "worker-bot"},
                }
            )
            return Completed(0, "", "")
        if "issues/42/comments" in joined:
            return Completed(0, json.dumps([self.pr_comments]), "")
        if "/pulls/42" in joined:
            return Completed(0, json.dumps(self.pull), "")
        if command[:2] == ["gh", "api"] and command[-1] == "repos/example/project":
            return Completed(0, json.dumps({"permissions": self.permissions}), "")
        return Completed(1, "", f"unhandled {command}")

    def account_runner(self, base, *, login: str = "worker-bot"):
        def scoped(argv: list[str]) -> Completed:
            assert argv[0] in {"gh", "git"}
            if argv[0] == "gh":
                if argv[1:3] == ["api", "user"]:
                    return Completed(0, json.dumps({"login": login}), "")
                return base(argv)
            return base(argv)

        return scoped

    def lane(self, argv: list[str], stdin: str | None = None) -> Completed:
        self.lanes.append((stdin or "") + " " + " ".join(argv))
        if not self.preexisting_dirty:
            self.lane_dirty = True
            if self.lane_changes is None:
                self.lane_changes = set(self.changed)
            self.changed = set(self.lane_changes)
        if self.clone_path is not None:
            conflict_path = self.clone_path / self.conflict_file
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            conflict_path.write_text("resolved\n", encoding="utf-8")
        output = (
            self.lane_output
            if self.lane_output is not None
            else (
                self.lane_results.pop(0)
                if self.lane_results
                else "STATUS: complete\nRESULT: done\n"
            )
        )
        self.lane_output = None
        return Completed(0, output, "")

    def check(self, argv: list[str], **kwargs: Any) -> Completed:
        self.check_calls.append({"argv": argv, **kwargs})
        return Completed(self.check_rc, "ok" if self.check_rc == 0 else "", "")


@pytest.mark.parametrize("payload", [None, {}, {"workers": None}, {"workers": {}}])
def test_missing_or_empty_configuration_enables_no_worker(tmp_path: Path, payload: Any) -> None:
    if payload is not None:
        (tmp_path / "conflicts.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_conflict_config(tmp_path) == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_root", "relative"),
        ("repositories", {}),
        ("request_logins", []),
        ("poll_seconds", 29),
        ("lane_timeout", 59),
        ("check_timeout", 59),
        ("max_rebase_steps", 0),
        ("max_rebase_steps", 201),
    ],
)
def test_each_invalid_worker_field_is_rejected(tmp_path: Path, field: str, value: Any) -> None:
    data = config(tmp_path)
    data["workers"]["worker-session"][field] = value
    (tmp_path / "conflicts.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoreError, match=field):
        load_conflict_config(tmp_path)


def test_unsafe_session_id_is_rejected(tmp_path: Path) -> None:
    data = config(tmp_path)
    data["workers"]["../escape"] = data["workers"].pop("worker-session")
    (tmp_path / "conflicts.json").write_text(json.dumps(data), encoding="utf-8")
    unsafe = load_conflict_config(tmp_path)["../escape"]
    with pytest.raises(StoreError, match="safe single path component"):
        context(
            unsafe,
            Request(
                repo="example/project",
                number=42,
                comment_id=7,
                created_at="2026-09-01T00:00:00Z",
            ),
            worker_login="worker-bot",
        )


def test_unknown_keys_and_empty_checks_are_rejected(tmp_path: Path) -> None:
    data = config(tmp_path)
    data["workers"]["worker-session"]["unexpected"] = True
    (tmp_path / "conflicts.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoreError, match="unexpected"):
        load_conflict_config(tmp_path)
    data = config(tmp_path)
    data["workers"]["worker-session"]["repositories"]["example/project"]["check_argv"] = []
    (tmp_path / "conflicts.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoreError, match="check_argv"):
        load_conflict_config(tmp_path)


@pytest.mark.parametrize("body", [" @Worker-Bot   ConFlicts \n", "@worker-bot conflicts"])
def test_exact_mandate_accepts_case_and_whitespace(body: str) -> None:
    assert mandate_matches(
        body=body, author_login="AuthorizedHuman", author_type="User",
        worker_login="worker-bot", request_logins=("authorizedhuman",),
        created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z",
        is_pull_request=True, pull_open=True,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"body": "@worker-bot conflicts please"},
        {"author_login": "SomeoneElse"},
        {"author_type": "Bot"},
        {"updated_at": "2026-09-01T00:01:00Z"},
        {"is_pull_request": False},
        {"pull_open": False},
        {"body": "@other conflicts"},
    ],
)
def test_non_exact_or_unauthorized_mandates_are_rejected(kwargs: dict[str, Any]) -> None:
    values = dict(
        body="@worker-bot conflicts", author_login="AuthorizedHuman", author_type="User",
        worker_login="worker-bot", request_logins=("authorizedhuman",),
        created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z",
        is_pull_request=True, pull_open=True,
    )
    values.update(kwargs)
    assert not mandate_matches(**values)


def test_first_run_ignores_comments_older_than_24_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    fake.comments = [{
        "id": 7, "body": "@worker-bot conflicts", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z", "user": {"login": "AuthorizedHuman", "type": "User"},
        "html_url": "https://github.com/example/project/pull/42#issuecomment-7",
    }]
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate._now", lambda: NOW)
    lines = tick(store, worker(tmp_path), runner=fake, dry_run=True)
    assert lines
    assert not fake.pr_comments


def test_push_route_foreign_fork_is_a_blocker_without_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    fake.pull["head"]["repo"]["full_name"] = "someone-else/project"
    fake.comments = [{
        "id": 7, "body": "@worker-bot conflicts", "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z", "user": {"login": "AuthorizedHuman", "type": "User"},
        "html_url": "https://github.com/example/project/pull/42#issuecomment-7",
    }]
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate._now", lambda: NOW)
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("blocker" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_mergeable_true_reports_without_clone_or_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    fake.pull["mergeable"] = True
    fake.comments = [{
        "id": 7, "body": "@worker-bot conflicts", "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z", "user": {"login": "AuthorizedHuman", "type": "User"},
        "html_url": "https://github.com/example/project/pull/42#issuecomment-7",
    }]
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate._now", lambda: NOW)
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("no conflicts" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert fake.lanes == []


def test_result_facts_include_and_store_mergeable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert "Mergeable: false" in fake.pr_comments[0]["body"]
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["mergeable"] == "false"
    assert state["old_head"] == "a" * 40


def _result_after_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mergeable_after_push: Any = True,
    read_after_push_fails: bool = False,
):
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    state = {"pushed": False}

    def runner(argv: list[str]) -> Completed:
        if argv[:1] == ["git"] and "push" in argv:
            fake.pull["head"]["sha"] = "d" * 40
            fake.pull["mergeable"] = mergeable_after_push
            fake.rev_parse_head = "d" * 40
            state["pushed"] = True
        if read_after_push_fails and state["pushed"] and "/pulls/42" in " ".join(argv):
            return Completed(1, "", "GitHub unreachable")
        return fake(argv)

    return fake, store, worker_cfg, runner


def test_result_comment_reports_mergeable_read_after_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, store, worker_cfg, runner = _result_after_push(
        tmp_path, monkeypatch, mergeable_after_push=True
    )
    assert fake.pull["mergeable"] is False
    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert len(fake.pr_comments) == 1
    assert "Mergeable: true" in fake.pr_comments[0]["body"]


def test_result_comment_reports_unknown_when_post_push_read_stays_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, store, worker_cfg, runner = _result_after_push(
        tmp_path, monkeypatch, mergeable_after_push=None
    )
    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert len(fake.pr_comments) == 1
    assert "Mergeable: unknown" in fake.pr_comments[0]["body"]


def test_result_comment_reports_unknown_when_post_push_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, store, worker_cfg, runner = _result_after_push(
        tmp_path, monkeypatch, read_after_push_fails=True
    )
    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert len(fake.pr_comments) == 1
    assert "Mergeable: unknown" in fake.pr_comments[0]["body"]


def test_result_pending_resume_reads_mergeable_before_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["sha"] = "d" * 40
    fake.pull["mergeable"] = True
    request = Request(
        repo="example/project", number=42, comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    ctx = context(worker_cfg, request, worker_login="worker-bot")
    save_state(
        ctx,
        "result-pending",
        old_head="a" * 40,
        new_head="d" * 40,
        replayed_commits=2,
        resolved_files=["src/app.py"],
        check_result="passed",
        mergeable="false",
    )
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert lines == ["result comment posted"]
    assert len(fake.pr_comments) == 1
    assert "Mergeable: true" in fake.pr_comments[0]["body"]


def test_dry_run_has_no_reaction_or_state_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    fake.comments = [{
        "id": 7, "body": "@worker-bot conflicts", "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z", "user": {"login": "AuthorizedHuman", "type": "User"},
        "html_url": "https://github.com/example/project/pull/42#issuecomment-7",
    }]
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate._now", lambda: NOW)
    lines = tick(store, worker(tmp_path), runner=fake, dry_run=True)
    assert any("would rebase" in line for line in lines)
    assert not (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "checkpoint.json"
    ).exists()
    assert fake.pr_comments == []
    assert fake.lanes == []
    assert fake.reactions == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_dry_run_masks_token_shaped_branch_in_every_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["base"]["ref"] = "develop/sk-" + "A" * 20
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would rebase" in line for line in lines)
    assert not any("sk-" in line for line in lines)
    assert fake.pr_comments == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_no_conflict_result_post_is_retried_from_state_without_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.pull["mergeable"] = True
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.comment_post_rc = 1
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("transient" in line for line in lines)
    assert fake.pr_comments == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "result-pending"
    stored = state["result_body"]
    assert "no conflicts" in stored.casefold()

    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert lines == ["result comment posted"]
    assert len(fake.pr_comments) == 1
    assert fake.pr_comments[0]["body"].endswith(stored)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "finished"


def _mandate_comment() -> dict[str, Any]:
    return {
        "id": 7, "body": "@worker-bot conflicts", "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z", "user": {"login": "AuthorizedHuman", "type": "User"},
        "html_url": "https://github.com/example/project/pull/42#issuecomment-7",
    }


NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def _mandate_comment_at(created: str, *, comment_id: int = 7, body: str = "@worker-bot conflicts") -> dict[str, Any]:
    comment = _mandate_comment()
    comment.update({"id": comment_id, "body": body, "created_at": created, "updated_at": created})
    return comment


def _ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeConflictTransport) -> tuple[Store, Any]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake.comments = [_mandate_comment()]
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate._now", lambda: NOW)
    return store, worker(tmp_path)


def test_conflict_loop_resolves_and_pushes_with_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert any("--force-with-lease=refs/heads/feature:" + "a" * 40 in " ".join(call) for call in fake.calls)
    assert len(fake.pr_comments) == 1
    assert len(fake.lanes) == 1
    assert "--no-subagents" in fake.lanes[0]
    assert "--no-git" not in fake.lanes[0]
    assert "STATUS: complete" not in " ".join(" ".join(call) for call in fake.calls)
    assert any(fake_call == ["git", "add", "--", "src/app.py"] for fake_call in fake.calls)
    assert fake.check_calls[0]["argv"] == ["/operator/checks"]
    assert fake.check_calls[0]["cwd"] == str(fake.clone_path)
    assert fake.check_calls[0]["env"]["AGENT_CONFLICTS_HEAD"]
    assert fake.check_calls[0]["clear_ambient_github"] is True


def test_network_git_argv_uses_account_runner_allowlist_and_sets_hooks_after_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    git_calls = [
        call[call.index("git"):]
        for call in fake.raw_calls
        if "git" in call
    ]
    checked = []
    for call in git_calls:
        args = call[1:]
        verb_index = 2 if args[:1] == ["-C"] else 0
        verb = args[verb_index]
        if verb not in {"clone", "fetch", "push"}:
            continue
        checked.append(call)
        targets = _network_remote_targets(args)
        assert targets
        assert "-c" not in call
        if verb in {"fetch", "push"}:
            assert "-C" in call
    assert any(
        call[1:2] == ["clone"] or call[3:4] == ["clone"] for call in checked
    )
    assert any("fetch" in call for call in checked)
    assert any("push" in call for call in checked)
    clone_index = next(
        index
        for index, call in enumerate(git_calls)
        if call[:2] == ["git", "clone"]
    )
    assert git_calls[clone_index + 1] == [
        "git", "-C", str(fake.clone_path), "-c", "core.hooksPath=/dev/null",
        "config", "core.hooksPath", "/dev/null",
    ]


def test_lane_marker_retries_then_blocks_without_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    calls = {"n": 0}

    def bad_lane(argv: list[str], stdin: str | None = None) -> Completed:
        calls["n"] += 1
        fake.lane_dirty = True
        if fake.clone_path is not None:
            (fake.clone_path / "src").mkdir(parents=True, exist_ok=True)
            (fake.clone_path / "src" / "app.py").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\n", encoding="utf-8")
        return Completed(0, "STATUS: complete\nRESULT: done\n", "")

    lines = tick(store, worker_cfg, runner=fake, lane_runner=bad_lane)
    assert calls["n"] == 2
    assert any("blocker" in line for line in lines)
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)
    assert len(fake.pr_comments) == 1
    assert "conflict marker remains" in fake.pr_comments[0]["body"]
    second_spec = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "lane-2.md"
    )
    assert "conflict marker remains" in second_spec.read_text(encoding="utf-8")


def test_existing_marker_finishes_without_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pr_comments = [{"id": 1, "body": "<!-- agent-conflicts:result:v1:7 -->\nEN: done", "user": {"login": "worker-bot"}}]
    lines = tick(store, worker_cfg, runner=fake)
    assert "finished" in lines[0]
    assert fake.lanes == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_quoted_result_marker_later_in_worker_comment_does_not_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pr_comments = [
        {
            "id": 1,
            "body": "EN: this only quotes the marker\n<!-- agent-conflicts:result:v1:7 -->\n",
            "user": {"login": "worker-bot"},
        }
    ]
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert not any("finished" in line for line in lines)
    assert any("would rebase" in line for line in lines)
    assert fake.lanes == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_marker_on_first_tick_is_reported_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pr_comments = [
        {
            "id": 1,
            "body": "<!-- agent-conflicts:result:v1:7 -->\nEN: already done",
            "user": {"login": "worker-bot"},
        }
    ]
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("finished" in line for line in lines)
    assert fake.reactions == []
    assert fake.lanes == []


def test_lease_rejection_leaves_request_unfinished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.push_rc = 1
    fake.remote_head_sha = "d" * 40
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line or "transient" in line for line in lines)
    assert fake.pr_comments == []
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] != "finished"
    fake.calls.clear()
    fake.push_rc = 0
    fake.lane_results = ["STATUS: complete\nRESULT: done\n"]
    tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("repos/example/project/pulls/42" in " ".join(call) for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_missing_skill_and_unknown_provider_refuse_before_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", [])
    fake = FakeConflictTransport(tmp_path)
    patch_account_runners(monkeypatch, fake)
    assert "preflight blocked" in tick(store, worker(tmp_path), runner=fake)[0]
    data = json.loads((store.home / "ai-accounts.json").read_text(encoding="utf-8"))
    data["models"]["second"] = "claude"
    data["implementer"] = "codex"
    (store.home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")
    store.write(
        "session",
        "update",
        "worker-session",
        {
            "id": "worker-session",
            "kind": "runner",
            "status": "active",
            "skills": ["conflicts"],
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("codex" in line for line in lines)


def test_conflicts_error_is_visible() -> None:
    assert issubclass(ConflictsError, StoreError)


def test_unknown_session_id_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    fake = FakeConflictTransport(tmp_path)
    patch_account_runners(monkeypatch, fake)
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("not found" in line for line in lines)
    assert fake.comments == []


def test_comment_at_checkpoint_second_is_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    checkpoint = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "checkpoint.json"
    )
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps({"example/project": "2026-09-01T00:00:00Z"}), encoding="utf-8"
    )
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would accept" in line for line in lines)


def test_pending_result_times_out_after_24_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    request = Request(
        repo="example/project", number=42, comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    ctx = context(worker_cfg, request, worker_login="worker-bot")
    save_state(
        ctx,
        "result-pending",
        result_body="stored result",
        transient_start="2026-09-01T00:00:00Z",
    )
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert len(fake.pr_comments) == 1
    assert "blocked" in fake.pr_comments[0]["body"]
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "blocked"


def test_resume_after_push_posts_full_result_not_no_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    new_head = "d" * 40
    fake.pull["head"]["sha"] = new_head
    fake.pull["mergeable"] = True
    request = Request(
        repo="example/project", number=42, comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    ctx = context(worker_cfg, request, worker_login="worker-bot")
    save_state(
        ctx,
        "pushing",
        old_head="a" * 40,
        new_head=new_head,
        replayed_commits=2,
        resolved_files=["src/app.py"],
        check_result="passed",
    )
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    body = fake.pr_comments[0]["body"]
    assert "Old head" in body and "New head" in body
    assert "Replayed commits: 2" in body
    assert "src/app.py" in body
    assert "no conflicts" not in body.casefold()
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "finished"


def test_dry_run_with_unknown_mergeability_prints_no_would_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["mergeable"] = None
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("GitHub mergeable state is still unknown" in line for line in lines)
    assert not any("would rebase" in line for line in lines)
    assert fake.reactions == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_malformed_mergeable_is_transient_not_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["mergeable"] = "yes"
    lines = tick(store, worker_cfg, runner=fake)
    assert any("mergeable state is unreadable" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert fake.pr_comments == []


@pytest.mark.parametrize(
    "base_ref",
    [
        "bad..base",
        "base@{1}",
        ".hidden",
        "/base",
        "base.",
        "base/",
        "base.lock",
        "base//next",
    ],
)
def test_git_invalid_base_branch_name_blocks_before_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_ref: str
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["base"]["ref"] = base_ref
    lines = tick(store, worker_cfg, runner=fake)
    assert any("unsupported base branch name" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_unsupported_base_branch_name_blocks_before_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["base"]["ref"] = "bad base"
    lines = tick(store, worker_cfg, runner=fake)
    assert any("unsupported base branch name" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_github_unreachable_still_aborts_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    state = {"down": False}

    def failing_github(argv: list[str]) -> Completed:
        if argv[:1] == ["gh"] and state["down"]:
            return Completed(1, "", "GitHub unreachable")
        return fake(argv)

    def lane(argv: list[str], stdin: str | None = None) -> Completed:
        fake.lane(argv, stdin)
        state["down"] = True
        return Completed(0, "STATUS: complete\nRESULT: blocked\nNeeds a product decision.\n", "")

    lines = tick(store, worker_cfg, runner=failing_github, lane_runner=lane)
    assert any("blocker" in line or "transient" in line for line in lines)
    assert any("--abort" in call for call in fake.calls)
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] in {
        "blocker-pending",
        "blocked",
    }


def test_comment_post_failure_keeps_retrying_only_the_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.comment_post_rc = 1
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    request = Request(
        repo="example/project", number=42, comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    ctx = context(worker_cfg, request, worker_login="worker-bot")
    save_state(
        ctx,
        "blocker-pending",
        blocker="previous blocker",
        blocker_facts=[],
        transient_start="2026-09-02T00:00:00Z",
    )
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line or "transient" in line for line in lines)
    assert fake.comment_posts >= 1
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "blocker-pending"


def test_safe_masks_absolute_paths_and_keeps_repository_names(tmp_path: Path) -> None:
    worker_cfg = worker(tmp_path)
    text = safe(
        worker_cfg,
        "example/project /mnt/data/x ~/private/y refs/heads/develop https://example.test/a/b",
    )
    assert "example/project" in text
    assert "refs/heads/develop" in text
    assert "https://example.test/a/b" in text
    assert "/mnt/data/x" not in text
    assert "~/private/y" not in text


def test_documentation_describes_unlimited_blocker_retry_and_24_hour_limit() -> None:
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "conflict-mandate.md"
    ).read_text(encoding="utf-8")
    assert "A blocker comment that cannot be posted" in doc
    assert "retried on every tick without a time limit" in doc
    assert "24-hour limit applies to every other transient failure" in doc
    assert "including `result-pending`" in doc
    assert "`lane changed the repository metadata`" in doc
    assert "`unexpected worktree state before the lane`" in doc
    assert "`checks changed the repository state`" in doc
    assert "`unsupported head branch name`" in doc


def test_checkpoint_advances_and_finished_request_is_not_reprocessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    checkpoint = json.loads(
        (tmp_path / "work" / ".conflicts-control" / "worker-session" / "checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["example/project"] == "2026-09-01T00:00:00Z"
    fake.calls.clear()
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert lines == ["idle"] or any("finished" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_older_comment_does_not_hide_a_newer_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    old = _mandate_comment_at("2026-09-01T00:00:00Z", comment_id=7)
    new = _mandate_comment_at("2026-09-01T06:00:00Z", comment_id=8)
    fake.comments = [old, new]
    fake.pr_comments = [
        {
            "id": 900,
            "body": "<!-- agent-conflicts:result:v1:7 -->\nEN: done",
            "user": {"login": "worker-bot"},
        }
    ]
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("comment 8" in line for line in lines)
    assert fake.reactions == []


def test_same_repository_push_route_requires_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["repo"]["full_name"] = "example/project"
    fake.permissions = {"push": False}
    lines = tick(store, worker_cfg, runner=fake)
    assert any("blocker" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert fake.pr_comments


def test_same_repository_push_route_with_permission_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["repo"]["full_name"] = "example/project"
    fake.permissions = {"push": True}
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("with repository push permission" in line for line in lines)


def test_protected_head_ref_posts_one_blocker_without_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["ref"] = "develop"
    lines = tick(store, worker_cfg, runner=fake)
    assert any("protected" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert len(fake.pr_comments) == 1
    assert "develop" in fake.pr_comments[0]["body"]


def test_lane_changing_unlisted_file_is_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.changed = {"src/app.py", "src/other.py"}
    fake.preexisting_dirty = False
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert len(fake.lanes) == 2
    assert "outside the conflicted set" in fake.pr_comments[0]["body"]
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def test_unsupported_unmerged_state_blocks_without_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.unmerged_state = "DU"
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert fake.lanes == []
    assert "src/app.py" in fake.pr_comments[0]["body"]
    assert "DU" in fake.pr_comments[0]["body"]


def test_lane_blocked_quotes_one_bounded_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.lane_results = []
    fake.lane_output = (
        "STATUS: complete\nRESULT: blocked\n"
        "A product decision is required. "
        + "x" * 700
        + "\n"
    )
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert len(fake.lanes) == 1
    body = fake.pr_comments[0]["body"]
    assert "A product decision is required." in body
    assert len(body) < 4000
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def test_check_failure_posts_redacted_tail_and_does_not_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    secret = "ghp_" + "A" * 36
    def failed_check(argv: list[str], **kwargs: Any) -> Completed:
        fake.check_calls.append({"argv": argv, **kwargs})
        return Completed(1, "", f"failure\n{secret}\n{tmp_path}/work/private")

    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", failed_check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)
    body = fake.pr_comments[0]["body"]
    assert "failure" in body
    assert secret not in body
    assert str(tmp_path) not in body


def test_check_timeout_is_a_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    def timed_out(argv: list[str], **kwargs: Any) -> Completed:
        fake.check_calls.append({"argv": argv, **kwargs})
        return Completed(124, "", "timeout")

    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", timed_out)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)
    assert "timeout" in fake.pr_comments[0]["body"]


def test_lease_rejection_restarts_from_pull_read_on_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    fake.push_rc = 1
    fake.remote_head_sha = "d" * 40
    tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] != "finished"
    fake.calls.clear()
    fake.push_rc = 0
    fake.lane_results = ["STATUS: complete\nRESULT: done\n"]
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert any("repos/example/project/pulls/42" in " ".join(call) for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_missing_account_or_lane_binding_refuses_precisely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    patch_account_runners(monkeypatch, fake)
    (store.home / "github-accounts.json").write_text(json.dumps({"accounts": {}, "sessions": {}}), encoding="utf-8")
    assert "No GitHub account configured" in tick(store, worker(tmp_path), runner=fake)[0]
    write_accounts(store.home)
    data = json.loads((store.home / "ai-accounts.json").read_text(encoding="utf-8"))
    data["sessions"]["worker-session"]["lanes"].pop("grok:implementer")
    (store.home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("No AI lane configured" in line for line in lines)
    assert fake.comments == []


def test_read_only_implementer_binding_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    patch_account_runners(monkeypatch, fake)
    data = json.loads((store.home / "ai-accounts.json").read_text(encoding="utf-8"))
    data["roles"]["impl"]["access"] = "read-only"
    (store.home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("workspace-write" in line for line in lines)
    assert fake.comments == []


def test_preflight_refuses_implementer_outside_lane_vendors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    fake = FakeConflictTransport(tmp_path)
    patch_account_runners(monkeypatch, fake)
    data = json.loads((store.home / "ai-accounts.json").read_text(encoding="utf-8"))
    data["implementer"] = "deepseek"
    (store.home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")
    lines = tick(store, worker(tmp_path), runner=fake)
    assert any("preflight blocked" in line for line in lines)
    assert any("deepseek" in line and "lane launcher" in line for line in lines)
    assert fake.comments == []


def test_dynamic_text_never_contains_pr_text_local_paths_or_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["title"] = "SECRET TITLE"
    fake.pull["body"] = "SECRET BODY"
    fake.comments[0]["body"] = " @Worker-Bot   ConFlicts "
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    rendered = "\n".join(lines)
    bodies = "\n".join(comment["body"] for comment in fake.pr_comments)
    specs = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "work" / ".conflicts-control").rglob("*.md"))
    assert "SECRET TITLE" not in rendered + bodies + specs
    assert "SECRET BODY" not in rendered + bodies + specs
    assert str(tmp_path) not in rendered + bodies + specs
    assert "ghp_" not in rendered + bodies + specs


def test_lane_spec_redacts_base_branch_and_commit_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["base"]["ref"] = "develop/ghp_" + "A" * 36
    fake.comments[0]["body"] = "@worker-bot conflicts"

    def secret_subject(argv: list[str]) -> Completed:
        if argv[:1] == ["git"] and "log" in argv:
            return Completed(0, "Replay ghp_" + "B" * 36 + "\n", "")
        return fake(argv)

    lines = tick(store, worker_cfg, runner=secret_subject, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    specs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "work" / ".conflicts-control").rglob("*.md")
    )
    assert "Replay ghp_" not in specs


def _two_worker_config(root: Path) -> dict[str, Any]:
    data = config(root)
    work = root / "work"
    template = data["workers"]["worker-session"]
    data["workers"]["other-session"] = {
        **template,
        "workspace_root": str(work),
        "request_logins": ["AuthorizedHuman", "OtherHuman"],
    }
    (root / "conflicts.json").write_text(json.dumps(data), encoding="utf-8")
    return load_conflict_config(root)


def test_worker_isolation_ignores_another_logins_unfinished_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["conflicts"])
    make_session(store, "other-session", ["conflicts"])
    accounts = json.loads(
        (store.home / "github-accounts.json").read_text(encoding="utf-8")
    )
    accounts["accounts"]["other"] = {
        "login": "other-bot",
        "gh_config_dir": "/test/gh-other",
        "git": accounts["accounts"]["worker"]["git"],
    }
    accounts["sessions"]["other-session"] = "other"
    (store.home / "github-accounts.json").write_text(
        json.dumps(accounts), encoding="utf-8"
    )
    ai = json.loads((store.home / "ai-accounts.json").read_text(encoding="utf-8"))
    ai["sessions"]["other-session"] = ai["sessions"]["worker-session"]
    (store.home / "ai-accounts.json").write_text(json.dumps(ai), encoding="utf-8")
    fake = FakeConflictTransport(tmp_path)
    patch_account_runners(monkeypatch, fake)
    workers = _two_worker_config(tmp_path)
    first = workers["worker-session"]
    second = workers["other-session"]
    request = Request(
        repo="example/project",
        number=42,
        comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    first_ctx = context(first, request, worker_login="worker-bot")
    save_state(first_ctx, "blocker-pending", blocker="first worker request")
    state_before = first_ctx.state_path.read_text(encoding="utf-8")
    second_ctx = context(second, request, worker_login="other-bot")
    save_state(
        second_ctx,
        "blocker-pending",
        worker_login="worker-bot",
        blocker="wrong login must be ignored",
    )
    second_state_before = second_ctx.state_path.read_text(encoding="utf-8")
    assert first_ctx.control.is_relative_to(
        first.workspace_root / ".conflicts-control" / "worker-session"
    )
    assert first_ctx.worktree.is_relative_to(first.workspace_root / "worker-session")
    lines = tick(store, second, runner=fake)
    assert lines == ["idle"]
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)
    assert fake.pr_comments == []
    assert first_ctx.control.exists()
    assert first_ctx.state_path.read_text(encoding="utf-8") == state_before
    assert second_ctx.state_path.read_text(encoding="utf-8") == second_state_before
    assert unfinished_requests(second, "other-bot") == []


def test_lane_spec_contains_the_shared_prompt_prohibitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    spec = (
        tmp_path
        / "work"
        / ".conflicts-control"
        / "worker-session"
        / "example__project"
        / "pr-42-c7"
        / "lane-1.md"
    ).read_text(encoding="utf-8")
    assert prompt_prohibitions() in spec


def test_workflow_accepts_conflicted_path_with_space_and_non_ascii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.conflict_file = "space ä.txt"
    fake.changed = {fake.conflict_file}
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)

    def runner(argv: list[str]) -> Completed:
        if argv[:1] == ["git"] and "push" in argv:
            fake.pull["head"]["sha"] = "d" * 40
            fake.remote_head_sha = "d" * 40
            fake.rev_parse_head = "d" * 40
        return fake(argv)

    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert "outside the conflicted set" not in fake.pr_comments[0]["body"]
    assert len(fake.lanes) == 1


def test_lane_that_rewrites_git_file_blocks_without_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)

    def lane(argv: list[str], stdin: str | None = None) -> Completed:
        if fake.clone_path is not None:
            (fake.clone_path / ".git").write_text("gitdir: /rewritten\n", encoding="utf-8")
        return Completed(0, "STATUS: complete\nRESULT: done\n", "")

    lines = tick(store, worker_cfg, runner=fake, lane_runner=lane)
    assert any("blocker" in line for line in lines)
    assert "repository metadata" in fake.pr_comments[0]["body"]
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def test_feature_at_branch_reaches_push_with_exact_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.pull["head"]["ref"] = "feature@2"
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert any(
        "--force-with-lease=refs/heads/feature@2:" + "a" * 40 in " ".join(call)
        for call in fake.calls
    )


def test_unsupported_head_branch_blocks_before_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.pull["head"]["ref"] = "bad..head"
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    lines = tick(store, worker_cfg, runner=fake)
    assert any("unsupported head branch name" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)
    assert len(fake.pr_comments) == 1


def test_invalid_head_with_unknown_mergeable_blocks_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["mergeable"] = None
    fake.pull["head"]["ref"] = "bad..head"
    lines = tick(store, worker_cfg, runner=fake)
    assert any("blocker" in line for line in lines)
    assert "unsupported head branch name" in fake.pr_comments[0]["body"]
    assert "mergeable" not in " ".join(lines).casefold()
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_dry_run_reports_unsupported_head_as_would_post_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.pull["head"]["ref"] = "bad..head"
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would accept" in line for line in lines)
    assert any(
        "would post blocker" in line and "unsupported head branch name" in line
        for line in lines
    )
    assert not any("would rebase" in line for line in lines)
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_check_that_moves_head_blocks_without_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)

    def moved_check(argv: list[str], **kwargs: Any) -> Completed:
        fake.check_calls.append({"argv": argv, **kwargs})
        fake.pull["head"]["sha"] = "d" * 40
        fake.rev_parse_head = "d" * 40
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", moved_check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert "checks changed the repository state" in fake.pr_comments[0]["body"]
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def test_check_that_moves_origin_base_blocks_without_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)

    def moved_check(argv: list[str], **kwargs: Any) -> Completed:
        fake.check_calls.append({"argv": argv, **kwargs})
        fake.remote_base_sha = "e" * 40
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", moved_check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert "checks changed the repository state" in fake.pr_comments[0]["body"]
    assert "origin/develop changed" in fake.pr_comments[0]["body"]
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def test_check_that_rewrites_publication_url_blocks_without_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    state = {"rewritten": False}

    def check(argv: list[str], **kwargs: Any) -> Completed:
        fake.check_calls.append({"argv": argv, **kwargs})
        state["rewritten"] = True
        return Completed(0, "", "")

    def runner(argv: list[str]) -> Completed:
        if state["rewritten"] and argv[:1] == ["git"] and "get-url" in argv:
            return Completed(0, "https://github.com/attacker/project.git\n", "")
        return fake(argv)

    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", check)
    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert "checks changed the repository state" in fake.pr_comments[0]["body"]
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def _failed_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remote_head_sha: str,
    remote_read_fails: bool = False,
):
    fake = FakeConflictTransport(tmp_path)
    fake.push_rc = 1
    fake.push_stderr = "x" * 2000 + " remote: policy refused the update"
    fake.remote_head_sha = remote_head_sha
    fake.remote_read_fails = remote_read_fails
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    return fake, lines


def test_push_failure_unreadable_remote_head_is_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, lines = _failed_push(
        tmp_path, monkeypatch, remote_head_sha="a" * 40, remote_read_fails=True
    )
    assert any("transient" in line for line in lines)
    assert fake.pr_comments == []


def test_push_failure_moved_remote_head_is_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, lines = _failed_push(tmp_path, monkeypatch, remote_head_sha="d" * 40)
    assert any("transient" in line for line in lines)
    assert fake.pr_comments == []


def test_push_failure_unchanged_remote_head_blocks_with_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, lines = _failed_push(tmp_path, monkeypatch, remote_head_sha="a" * 40)
    assert any("blocker" in line for line in lines)
    assert len(fake.pr_comments) == 1
    body = fake.pr_comments[0]["body"]
    assert "push was refused by the remote" in body
    assert "policy refused the update" in body
    assert "x" * 1600 not in body
    assert any(
        call[:3] == ["git", "ls-remote", "publication"] for call in fake.calls
    )
    ls_remote = next(
        call
        for call in fake.raw_calls
        if "git" in call and "ls-remote" in call
    )
    ls_remote_args = ls_remote[ls_remote.index("git") + 1:]
    assert _network_remote_targets(ls_remote_args) is None


def test_source_lock_key_is_case_insensitive() -> None:
    assert source_lock_key("Example/Project", 42) == source_lock_key("example/project", 42)
    assert source_lock_key("example/project", 42).startswith("conflicts-source:example/project:")


def test_dry_run_reports_protected_head_and_route_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["ref"] = "develop"
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would accept" in line for line in lines)
    assert any("would post blocker" in line and "protected" in line for line in lines)
    assert fake.pr_comments == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_dry_run_reports_refused_push_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["repo"]["full_name"] = "someone-else/project"
    fake.pull["mergeable"] = True
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would accept" in line for line in lines)
    assert any("would post blocker" in line and "cannot push" in line for line in lines)
    assert not any("would report" in line for line in lines)
    assert fake.pr_comments == []
    assert not any(call[:1] == ["clone"] for call in fake.calls)


def test_checkpoint_advances_without_a_qualifying_mandate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.comments = [
        {
            "id": 11,
            "body": "ordinary discussion",
            "created_at": "2026-09-01T03:00:00Z",
            "updated_at": "2026-09-01T03:00:00Z",
            "user": {"login": "AuthorizedHuman", "type": "User"},
            "html_url": "https://github.com/example/project/pull/42#issuecomment-11",
        }
    ]
    lines = tick(store, worker_cfg, runner=fake)
    assert any("finished" in line or line == "idle" for line in lines)
    assert not any("/pulls/" in " ".join(call) for call in fake.calls)
    checkpoint = json.loads(
        (tmp_path / "work" / ".conflicts-control" / "worker-session" / "checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["example/project"] == "2026-09-01T03:00:00Z"


def test_unreadable_first_pull_request_does_not_stop_the_second_mandate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.comments = [
        _mandate_comment_at("2026-09-01T00:00:00Z", comment_id=7),
        {
            **_mandate_comment_at("2026-09-01T01:00:00Z", comment_id=8),
            "html_url": "https://github.com/example/project/pull/43#issuecomment-8",
        },
    ]
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if argv[:1] == ["gh"] and "repos/example/project/pulls/42" in joined:
            return Completed(1, "", "GitHub unreachable")
        if argv[:1] == ["gh"] and "repos/example/project/pulls/43" in joined:
            return Completed(0, json.dumps(fake.pull), "")
        if (
            argv[:1] == ["gh"]
            and argv[:4] != ["gh", "api", "-X", "POST"]
            and "repos/example/project/issues/43/comments" in joined
        ):
            return Completed(0, json.dumps([]), "")
        return fake(argv)

    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("finished request 8" in line for line in lines)
    assert len(fake.pr_comments) == 1
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "transient"
    assert state["transient_start"] is not None
    second_state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-43-c8" / "state.json"
    )
    assert json.loads(second_state_path.read_text(encoding="utf-8"))["phase"] == "finished"

    fake.calls.clear()
    lines = tick(store, worker_cfg, runner=runner, lane_runner=fake.lane)
    assert any("transient" in line for line in lines)
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "transient"
    assert json.loads(second_state_path.read_text(encoding="utf-8"))["phase"] == "finished"
    assert len(fake.pr_comments) == 1


def test_checkpoint_advances_past_only_marker_finished_mandate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    finished = _mandate_comment_at("2026-09-01T00:00:00Z", comment_id=7)
    ordinary = {
        "id": 11,
        "body": "ordinary discussion",
        "created_at": "2026-09-01T03:00:00Z",
        "updated_at": "2026-09-01T03:00:00Z",
        "user": {"login": "AuthorizedHuman", "type": "User"},
        "html_url": "https://github.com/example/project/pull/42#issuecomment-11",
    }
    fake.comments = [finished, ordinary]
    fake.pr_comments = [
        {
            "id": 900,
            "body": "<!-- agent-conflicts:result:v1:7 -->\nEN: done",
            "user": {"login": "worker-bot"},
        }
    ]
    lines = tick(store, worker_cfg, runner=fake)
    assert any("finished" in line or line == "idle" for line in lines)
    checkpoint = json.loads(
        (tmp_path / "work" / ".conflicts-control" / "worker-session" / "checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["example/project"] == "2026-09-01T03:00:00Z"
    fake.calls.clear()
    fake.raw_calls.clear()
    assert tick(store, worker_cfg, runner=fake) == ["idle"]
    assert not any("issues/42/comments" in " ".join(call) for call in fake.calls)


def test_marker_lookup_failure_records_transient_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    request = Request(
        repo="example/project", number=42, comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    ctx = context(worker_cfg, request, worker_login="worker-bot")
    save_state(ctx, "rebased", transient_start=None)
    monkeypatch.setattr(
        "agent_cli.conflict_mandate._marker_exists",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConflictTransientError("marker lookup failed")
        ),
    )
    lines = tick(store, worker_cfg, runner=fake)
    assert lines == ["transient: marker lookup failed"]
    state = json.loads(
        (
            tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["transient_start"] is not None


def test_marker_lookup_failure_times_out_after_24_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    request = Request(
        repo="example/project", number=42, comment_id=7,
        created_at="2026-09-01T00:00:00Z",
    )
    ctx = context(worker_cfg, request, worker_login="worker-bot")
    save_state(ctx, "rebased", transient_start="2026-09-01T00:00:00Z")
    monkeypatch.setattr(
        "agent_cli.conflict_mandate._marker_exists",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConflictTransientError("marker lookup failed")
        ),
    )
    lines = tick(store, worker_cfg, runner=fake)
    assert any("blocker" in line for line in lines)
    assert len(fake.pr_comments) == 1
    assert "24 hours" in fake.pr_comments[0]["body"]


def test_discovery_marker_failure_times_out_after_24_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    clock = {"now": NOW}
    monkeypatch.setattr("agent_cli.conflict_mandate._now", lambda: clock["now"])

    monkeypatch.setattr(
        "agent_cli.conflict_mandate.find_result_comment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConflictTransientError("marker lookup failed")
        ),
    )
    assert tick(store, worker_cfg, runner=fake) == ["transient: marker lookup failed"]
    state_path = (
        tmp_path / "work" / ".conflicts-control" / "worker-session" / "example__project" / "pr-42-c7" / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "transient"
    assert state["transient_start"] == "2026-09-02T00:00:00Z"

    clock["now"] = datetime(2026, 9, 3, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_cli.conflict_mandate._marker_exists", lambda *_a, **_k: False)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert len(fake.pr_comments) == 1
    assert "24 hours" in fake.pr_comments[0]["body"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "blocked"


def test_mixed_change_set_passes_when_only_conflicted_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.changed = {fake.conflict_file}
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("pushed" in line for line in lines)
    assert "outside the conflicted set" not in fake.pr_comments[0]["body"]


def test_mixed_change_set_fails_when_lane_edits_cleanly_applied_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.changed = {fake.conflict_file, "src/clean.py"}
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert "src/clean.py" in fake.pr_comments[0]["body"]
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


def test_before_lane_state_with_outside_path_blocks_without_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    fake.changed = {"src/app.py", "src/leftover.py"}
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    monkeypatch.setattr("agent_cli.conflict_mandate.run_bounded", fake.check)
    fake.preexisting_dirty = True
    lines = tick(store, worker_cfg, runner=fake, lane_runner=fake.lane)
    assert any("blocker" in line for line in lines)
    assert "unexpected worktree state before the lane" in fake.pr_comments[0]["body"]
    assert fake.lanes == []
    assert not any("--force-with-lease" in " ".join(call) for call in fake.calls)


@pytest.mark.parametrize("head_ref", ["feature@2", "feature-ä", "_topic"])
def test_git_valid_head_names_reach_dry_run_without_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head_ref: str
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["head"]["ref"] = head_ref
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would accept" in line for line in lines)
    assert any("would rebase" in line for line in lines)
    assert not any("unsupported head branch name" in line for line in lines)


def test_dry_run_reports_invalid_base_as_would_post_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeConflictTransport(tmp_path)
    store, worker_cfg = _ready(tmp_path, monkeypatch, fake)
    fake.pull["base"]["ref"] = "bad..base"
    lines = tick(store, worker_cfg, runner=fake, dry_run=True)
    assert any("would accept" in line for line in lines)
    assert any(
        "would post blocker" in line and "unsupported base branch name" in line
        for line in lines
    )
    assert not any(call[:1] == ["clone"] for call in fake.calls)
