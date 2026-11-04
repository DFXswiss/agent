from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from agent_cli.main import (
    _canonicalize_snapshot_names,
    _latest_gates,
    load_task_dict,
    main,
)
from agent_cli.store import Store, StoreError, utcnow
from agent_cli.usage import AuthStale


def run(home: Path, argv: list[str]) -> None:
    import os

    # The local harness supplies the device model assignment before task creation.
    if argv[:2] == ["task", "create"] and not (home / "ai-accounts.json").exists():
        (home / "ai-accounts.json").write_text(
            json.dumps(
                {
                    "models": {"first": "grok", "second": "codex"},
                    "lane_execution": {"mode": "this-device-is-lane-host"},
                }
            ),
            encoding="utf-8",
        )
    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def _last_task_id(out: str) -> str:
    task_line = [ln for ln in out.splitlines() if ln.startswith("task ")][-1]
    return task_line.split()[1]


def _last_agent_id(out: str) -> str:
    agent_line = [ln for ln in out.splitlines() if ln.startswith("agent ")][-1]
    return agent_line.split()[1]


@pytest.mark.no_pg
def test_dashboard_snapshot_canonicalizes_legacy_names() -> None:
    # Keep legacy names here to verify snapshot alias compatibility.
    snapshot = {
        "agents": [
            {"stage": "grok-pr"},
            {"stage": "codex-pr"},
            {"stage": None},
        ],
        "gates": [{"stage": "grok-pr"}, {"stage": "review-2"}],
        "checklist": [{"key": "codex_pr_logic"}, {"key": "review_1_quality"}],
    }

    _canonicalize_snapshot_names(snapshot)

    assert snapshot == {
        "agents": [
            {"stage": "review-1"},
            {"stage": "review-2"},
            {"stage": None},
        ],
        "gates": [{"stage": "review-1"}, {"stage": "review-2"}],
        "checklist": [{"key": "review_2_logic"}, {"key": "review_1_quality"}],
    }


@pytest.mark.no_pg
def test_done_decision_ignores_the_stored_one_review_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cli.allow import ready_for_done_blocking
    from agent_cli.main import _done_one_review
    from agent_cli.pr_guard_config import PrGuardLookupError
    from test_allow import _ready_task

    stored = {"one_review_authors": ["alice"]}
    calls = {"n": 0}

    def live(_store: object, task: dict) -> tuple[str, dict | None]:
        calls["n"] += 1
        assert task["payload"]["one_review_author"]["trusted_pr_guard"] == stored
        return "alice", None

    monkeypatch.setattr("agent_cli.main._live_one_review", live)
    task = {
        "payload": {
            "one_review_author": {
                "pull_opener": "alice",
                "trusted_pr_guard": stored,
            }
        }
    }
    posted = {
        "review_2_quality": "second review not posted",
        "review_2_logic": "second review not posted",
    }
    login, config = _done_one_review(
        object(),
        task,
        {"review_2_quality": "n_a", "review_2_logic": "n_a"},
        posted,
    )
    assert calls["n"] == 1
    assert login == "alice"
    assert config is None
    done = _ready_task()
    done["checklist"]["review_2_quality"] = "n_a"
    done["checklist"]["review_2_logic"] = "n_a"
    done["gates"] = [gate for gate in done["gates"] if gate["stage"] == "review-1"]
    done["pull_opener"] = login
    done["trusted_pr_guard"] = config
    blocking = " ".join(ready_for_done_blocking(done))
    assert "review_2_quality=n_a" in blocking
    assert "review_2_logic=n_a" in blocking

    def boom(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise AssertionError("live read")

    monkeypatch.setattr("agent_cli.main._live_one_review", boom)
    assert _done_one_review(
        object(),
        task,
        {"review_2_quality": "ja", "review_2_logic": "ja"},
        posted,
    ) == ("", None)
    calls["n"] = 0
    assert _done_one_review(
        object(),
        task,
        {"review_2_quality": "n_a", "review_2_logic": "n_a"},
        {"review_2_quality": "seed", "review_2_logic": "seed"},
    ) == ("", None)
    assert calls["n"] == 0

    def missing(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise PrGuardLookupError("down")

    monkeypatch.setattr("agent_cli.main._live_one_review", missing)
    with pytest.raises(SystemExit, match="trusted review list could not be read"):
        _done_one_review(
            object(),
            task,
            {"review_2_quality": "n_a", "review_2_logic": "n_a"},
            posted,
        )


# Not no_pg: agent init opens the store. no_pg leaves AGENT_PG_DSN unset, so
# init creates a cluster and fails where the suite only has AGENT_TEST_PG.
def test_claim_done_reads_the_live_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.pr_guard_config import PrGuardLookupError
    from test_allow import _ready_task

    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "sess-1",
            "--kind",
            "human",
            "--skill",
            "spine",
        ],
    )
    capsys.readouterr()
    run(
        tmp_path,
        [
            "task",
            "create",
            "--session",
            "sess-1",
            "--workflow",
            "implement",
            "--title",
            "Ship",
        ],
    )
    tid = _last_task_id(capsys.readouterr().out)
    snap = _ready_task(session_id="sess-1")
    snap["id"] = tid
    snap["checklist"]["review_2_quality"] = "n_a"
    snap["checklist"]["review_2_logic"] = "n_a"
    snap["checklist_evidence"] = {
        "review_2_quality": "second review not posted",
        "review_2_logic": "second review not posted",
    }
    snap["gates"] = [gate for gate in snap["gates"] if gate["stage"] == "review-1"]

    def load_snaps(_store: object, _session_id: str) -> list[dict]:
        fresh = dict(snap)
        fresh["checklist"] = dict(snap["checklist"])
        fresh["checklist_evidence"] = dict(snap["checklist_evidence"])
        fresh["gates"] = list(snap["gates"])
        fresh["pull_opener"] = ""
        fresh["trusted_pr_guard"] = None
        return [fresh]

    monkeypatch.setattr("agent_cli.main.load_session_tasks", load_snaps)

    def listed(_store: object, _task: dict) -> tuple[str, dict | None]:
        return "alice", {"one_review_authors": ["alice"]}

    monkeypatch.setattr("agent_cli.main._live_one_review", listed)
    run(tmp_path, ["allow", "--action", "claim-done", "--session", "sess-1"])
    assert "allow action=claim-done" in capsys.readouterr().out

    def missing_list(_store: object, _task: dict) -> tuple[str, dict | None]:
        return "alice", None

    monkeypatch.setattr("agent_cli.main._live_one_review", missing_list)
    with pytest.raises(SystemExit):
        run(tmp_path, ["allow", "--action", "claim-done", "--session", "sess-1"])
    denied = capsys.readouterr()
    assert "review_2_quality=n_a" in f"{denied.out}\n{denied.err}"

    def down(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise PrGuardLookupError("down")

    monkeypatch.setattr("agent_cli.main._live_one_review", down)
    with pytest.raises(SystemExit, match="trusted review list could not be read"):
        run(tmp_path, ["allow", "--action", "claim-done", "--session", "sess-1"])


def test_pr_ready_does_not_read_the_trusted_review_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise AssertionError("live read")

    monkeypatch.setattr("agent_cli.main._live_one_review", boom)
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "sess-1",
            "--kind",
            "human",
            "--skill",
            "spine",
        ],
    )
    capsys.readouterr()
    run(
        tmp_path,
        [
            "task",
            "create",
            "--session",
            "sess-1",
            "--workflow",
            "implement",
            "--title",
            "Ship",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run(tmp_path, ["allow", "--action", "pr-ready", "--session", "sess-1"])
    with pytest.raises(SystemExit):
        run(tmp_path, ["allow", "--action", "claim-done", "--session", "sess-1"])


@pytest.mark.no_pg
def test_terminal_task_does_not_read_the_trusted_review_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cli.main import _attach_live_one_review

    def boom(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise AssertionError("live read")

    monkeypatch.setattr("agent_cli.main._live_one_review", boom)
    snap = {
        "id": "done-task",
        "state": "done",
        "checklist": {"review_2_quality": "n_a", "review_2_logic": "n_a"},
        "checklist_evidence": {
            "review_2_quality": "second review not posted",
            "review_2_logic": "second review not posted",
        },
    }
    _attach_live_one_review(object(), snap)
    assert "pull_opener" not in snap


@pytest.mark.no_pg
def test_wrong_evidence_does_not_read_the_trusted_review_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cli.main import _attach_live_one_review

    def boom(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise AssertionError("live read")

    monkeypatch.setattr("agent_cli.main._live_one_review", boom)

    class RowStore:
        def row(self, table: str, row_id: str) -> dict:
            assert table == "task"
            return {"id": row_id}

    snap = {
        "id": "open-task",
        "state": "pr-review",
        "checklist": {"review_2_quality": "n_a", "review_2_logic": "n_a"},
        "checklist_evidence": {
            "review_2_quality": "seed",
            "review_2_logic": "second review not posted",
        },
    }
    _attach_live_one_review(RowStore(), snap)
    assert snap["pull_opener"] == ""
    assert snap["trusted_pr_guard"] is None


def test_load_task_dict_does_not_read_the_trusted_review_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_store: object, _task: dict) -> tuple[str, dict | None]:
        raise AssertionError("live read")

    monkeypatch.setattr("agent_cli.main._live_one_review", boom)

    class LocalStore:
        def row(self, table: str, row_id: str) -> dict | None:
            assert (table, row_id) == ("task", "task-1")
            return {
                "id": "task-1",
                "payload": {
                    "one_review_author": {
                        "pull_opener": "alice",
                        "trusted_pr_guard": {"one_review_authors": ["alice"]},
                    }
                },
            }

        def rows(self, table: str) -> list[dict]:
            if table == "checklist_item":
                return [
                    {"task_id": "task-1", "key": "review_2_quality", "status": "n_a"},
                    {"task_id": "task-1", "key": "review_2_logic", "status": "n_a"},
                ]
            if table in ("local_check", "review_gate"):
                return []
            raise AssertionError(table)

    snap = load_task_dict(LocalStore(), "task-1")
    assert snap["pull_opener"] == ""
    assert snap["trusted_pr_guard"] is None


class _Home:
    def __init__(self, home: Path) -> None:
        self.home = home


def _bind_account(home: Path) -> None:
    (home / "github-accounts.json").write_text(
        json.dumps({
            "accounts": {
                "one": {"login": "WorkerOne", "gh_config_dir": "/accounts/one"},
            },
            "sessions": {"sess-1": "one"},
        }),
        encoding="utf-8",
    )


def _review_task() -> dict:
    return {"session_id": "sess-1", "repo": "owner/repo", "ref": "7"}


@pytest.mark.no_pg
def test_live_one_review_missing_account_does_not_use_ambient_gh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_cli.main import _live_one_review

    def ambient(argv: list[str], cwd: str | None = None):
        raise AssertionError(argv)

    monkeypatch.setattr("agent_cli.main._exec_argv", ambient)
    with pytest.raises(SystemExit, match="No GitHub account configured"):
        _live_one_review(_Home(tmp_path), _review_task())


@pytest.mark.no_pg
def test_live_one_review_login_mismatch_does_not_read_the_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_cli.main import _live_one_review
    from agent_cli.runtime import Completed

    _bind_account(tmp_path)
    seen: list[list[str]] = []

    def ambient(argv: list[str], cwd: str | None = None) -> Completed:
        seen.append(list(argv))
        gh = argv[argv.index("gh"):]
        if gh[:4] == ["gh", "api", "user", "--jq"]:
            return Completed(0, "OtherUser\n", "")
        raise AssertionError(gh)

    monkeypatch.setattr("agent_cli.main._exec_argv", ambient)
    with pytest.raises(SystemExit, match="does not match"):
        _live_one_review(_Home(tmp_path), _review_task())
    assert seen
    assert all("GH_CONFIG_DIR=/accounts/one" in argv for argv in seen)
    assert all(argv[0] != "gh" for argv in seen)


@pytest.mark.no_pg
def test_live_one_review_uses_the_session_account_for_both_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_cli.main import _live_one_review
    from agent_cli.runtime import Completed

    _bind_account(tmp_path)
    sha = "ab" * 20
    guard = json.dumps({
        "schema": "pr-guard/v1",
        "a38": {"enforce": [], "exclude": [], "default": "enforce"},
        "one_review_authors": ["alice"],
    })
    seen: list[list[str]] = []

    def ambient(argv: list[str], cwd: str | None = None) -> Completed:
        seen.append(list(argv))
        assert "GH_CONFIG_DIR=/accounts/one" in argv
        assert argv[0] != "gh"
        gh = argv[argv.index("gh"):]
        if gh[:4] == ["gh", "api", "user", "--jq"]:
            return Completed(0, "WorkerOne\n", "")
        if gh[:3] == ["gh", "pr", "view"]:
            return Completed(0, json.dumps({"author": {"login": "alice"}}), "")
        if gh == ["gh", "api", "repos/owner/repo"]:
            return Completed(0, json.dumps({"default_branch": "develop"}), "")
        if gh == ["gh", "api", "repos/owner/repo/commits/develop"]:
            return Completed(0, json.dumps({"sha": sha}), "")
        if gh[:2] == ["gh", "api"] and "contents/.github/pr-guard.json" in gh[-1]:
            return Completed(0, guard, "")
        raise AssertionError(gh)

    monkeypatch.setattr("agent_cli.main._exec_argv", ambient)
    login, config = _live_one_review(_Home(tmp_path), _review_task())
    assert login == "alice"
    assert config is not None
    assert config["one_review_authors"] == ["alice"]
    tails = [argv[argv.index("gh"):] for argv in seen]
    assert any(tail[:3] == ["gh", "pr", "view"] for tail in tails)
    assert any("contents/.github/pr-guard.json" in tail[-1] for tail in tails)


def test_task_repo_and_pr_uses_the_pull_target_not_the_push_remote() -> None:
    from agent_cli.main import _task_repo_and_pr

    repo, number = _task_repo_and_pr({
        "repo": "ignored/task-repo",
        "ref": "12",
        "payload": {
            "coordinator": {
                "publication_repo": "fork/widget",
                "pr_number": 34,
                "source": {"repo": "acme/widget"},
            }
        },
    })
    assert repo == "acme/widget"
    assert number == "34"

    fallback_repo, fallback_number = _task_repo_and_pr({
        "repo": "acme/widget",
        "ref": "12",
        "payload": {"coordinator": {"publication_repo": "fork/widget"}},
    })
    assert fallback_repo == "acme/widget"
    assert fallback_number == "12"


def _create_task_for_pr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    repo: str | None = None,
    ref: str | None = None,
) -> str:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"],
    )
    args = [
        "task",
        "create",
        "--session",
        "s",
        "--workflow",
        "implement",
        "--title",
        "Ship",
    ]
    if repo is not None:
        args.extend(["--repo", repo])
    if ref is not None:
        args.extend(["--ref", ref])
    run(tmp_path, args)
    return _last_task_id(capsys.readouterr().out)


def test_task_pr_attaches_repo_and_pull_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import _task_repo_and_pr

    tid = _create_task_for_pr(tmp_path, capsys)
    run(
        tmp_path,
        ["task", "pr", "--id", tid, "--repo", "DFXswiss/agent", "--pr", "149"],
    )
    assert f"task {tid} pr=DFXswiss/agent#149" in capsys.readouterr().out
    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["repo"] == "DFXswiss/agent"
    assert task["ref"] == "149"
    assert _task_repo_and_pr(task) == ("DFXswiss/agent", "149")


def test_task_pr_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _create_task_for_pr(tmp_path, capsys)
    args = ["task", "pr", "--id", tid, "--repo", "DFXswiss/agent", "--pr", "149"]
    run(tmp_path, args)
    run(tmp_path, args)
    assert capsys.readouterr().out.count(f"task {tid} pr=DFXswiss/agent#149") == 2


def test_task_pr_refuses_a_different_pull_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _create_task_for_pr(tmp_path, capsys, ref="148")
    with pytest.raises(SystemExit, match="task already has pull request 148"):
        run(
            tmp_path,
            ["task", "pr", "--id", tid, "--repo", "DFXswiss/agent", "--pr", "149"],
        )


def test_task_pr_refuses_a_different_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _create_task_for_pr(tmp_path, capsys, repo="DFXswiss/other")
    with pytest.raises(
        SystemExit,
        match="task repo is DFXswiss/other; refusing to change it to DFXswiss/agent",
    ):
        run(
            tmp_path,
            ["task", "pr", "--id", tid, "--repo", "DFXswiss/agent", "--pr", "149"],
        )


def test_task_pr_refuses_a_non_numeric_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _create_task_for_pr(tmp_path, capsys, ref="develop")
    with pytest.raises(SystemExit, match="not a pull request number"):
        run(
            tmp_path,
            ["task", "pr", "--id", tid, "--repo", "DFXswiss/agent", "--pr", "149"],
        )
    capsys.readouterr()
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["ref"] == "develop"


def test_task_pr_refuses_error_fix_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.error_fix_act import find_or_create_implement_task

    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_cli_error_seen_for_conclusion(tmp_path)
    store = Store(tmp_path)
    try:
        tid, _ = find_or_create_implement_task(
            store, "error-session", error_id, "Fix observed error"
        )
    finally:
        store.close()
    with pytest.raises(SystemExit, match="refuses error-fix tasks"):
        run(
            tmp_path,
            ["task", "pr", "--id", tid, "--repo", "org/app", "--pr", "149"],
        )
    capsys.readouterr()
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["ref"] in (None, "")


def test_task_pr_rejects_invalid_repo_and_pull_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _create_task_for_pr(tmp_path, capsys)
    for repo in (
        "DFXswiss",
        "/agent",
        "DFXswiss/",
        "DFXswiss/agent/extra",
        "DFX swiss/agent",
    ):
        with pytest.raises(SystemExit, match="--repo must be owner/name"):
            run(tmp_path, ["task", "pr", "--id", tid, "--repo", repo, "--pr", "149"])
    for pr in ("0", "-1", "1.5", "abc"):
        with pytest.raises(SystemExit, match="--pr must be a pull request number"):
            run(
                tmp_path,
                ["task", "pr", "--id", tid, "--repo", "DFXswiss/agent", "--pr", pr],
            )


def test_session_task_checklist_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship sync"])
    out = capsys.readouterr().out
    tid = _last_task_id(out)
    run(tmp_path, ["checklist", "set", "--task", tid, "--key", "session_registered", "--status", "ja", "--source", "script", "--evidence", "session register"])
    run(tmp_path, ["status"])
    status = capsys.readouterr().out
    assert "tasks_open=1" in status
    assert "agents_working=" in status
    assert "work_open=" in status
    assert "Ship sync" in status
    with pytest.raises(SystemExit, match="summaries missing"):
        run(tmp_path, ["task", "state", tid, "done"])


def test_cannot_close_with_pending_assigned(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "assigned", "--kind", "runner"])
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "asg-1",
            {
                "id": "asg-1",
                "session_id": "assigned",
                "type": "issue.assigned",
                "payload": {"assigned_at": "2026-01-01T00:00:00Z"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    with pytest.raises(SystemExit, match="pending assigned"):
        run(tmp_path, ["session", "close", "--id", "assigned"])


def test_cannot_close_with_open_task(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    with pytest.raises(SystemExit, match="open tasks"):
        run(tmp_path, ["session", "close", "--id", "s"])


def test_happy_path_round_agent_check_work(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    run(tmp_path, ["round", "start", "--task", tid])
    assert f"task {tid} round 1" in capsys.readouterr().out

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "sess-1",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "reviewing"

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "sess-1",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    rev_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", rev_id, "--verdict", "approved"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "local-check"

    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "lint",
            "--command",
            "pytest",
            "--result",
            "pass",
        ],
    )
    assert "check lint=pass" in capsys.readouterr().out

    run(
        tmp_path,
        [
            "work",
            "add",
            "--session",
            "sess-1",
            "--key",
            "standing",
            "--closable-by",
            "agent",
        ],
    )
    assert "work standing open closable_by=agent" in capsys.readouterr().out

    run(tmp_path, ["work", "list"])
    listing = capsys.readouterr().out
    assert "sess-1  standing  open  agent" in listing


def test_n_a_without_evidence_dies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "R"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="n_a requires --evidence"):
        run(
            tmp_path,
            [
                "checklist",
                "set",
                "--task",
                tid,
                "--key",
                "coverage_ok",
                "--status",
                "n_a",
                "--source",
                "script",
            ],
        )


def test_unavailable_whitespace_evidence_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    with pytest.raises(SystemExit, match="unavailable requires --evidence"):
        run(
            tmp_path,
            [
                "checklist",
                "set",
                "--task",
                tid,
                "--key",
                "review_1_quality",
                "--status",
                "unavailable",
                "--source",
                "script",
                "--evidence",
                "   ",
            ],
        )


def test_check_fail_sets_task_failed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "T"])
    tid = _last_task_id(capsys.readouterr().out)
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "lint",
            "--command",
            "false",
            "--result",
            "fail",
        ],
    )
    assert "check lint=fail" in capsys.readouterr().out
    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "failed"


def test_work_set_human_closable_rejects_runner_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(
        tmp_path,
        ["work", "add", "--session", "s", "--key", "mandate", "--closable-by", "human"],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="closable_by=human requires --source human"):
        run(
            tmp_path,
            [
                "work",
                "set",
                "--session",
                "s",
                "--key",
                "mandate",
                "--status",
                "done",
                "--source",
                "runner",
            ],
        )


def test_round_start_on_review_workflow_dies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="round start requires workflow"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_done_requires_gates_after_summaries_and_checklist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    with pytest.raises(SystemExit, match="summaries missing"):
        run(tmp_path, ["task", "state", tid, "done"])

    run(
        tmp_path,
        [
            "task",
            "summary",
            "--id",
            tid,
            "--en",
            "Adds complete session-store CLI.",
            "--de",
            "Ergaenzt die vollstaendige Session-Store-CLI.",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        keys = [
            r["key"]
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid
        ]
    finally:
        store.close()
    for key in keys:
        argv = [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            key,
            "--status",
            "ja",
            "--source",
            "human",
            "--evidence",
            "done-gate fixture",
        ]
        if key == "deviation_granted":
            argv.extend(
                [
                    "--deviation-granted",
                    "true",
                    "--granted-by",
                    "reviewer",
                    "--actor-session",
                    "s",
                ]
            )
        run(tmp_path, argv)
    capsys.readouterr()

    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "lint",
            "--command",
            "true",
            "--result",
            "pass",
        ],
    )
    capsys.readouterr()

    with pytest.raises(SystemExit, match="missing gate|gate"):
        run(tmp_path, ["task", "state", tid, "done"])


def test_gate_record_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "grok",
        ],
    )
    agent_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", agent_id, "--verdict", "approved"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "approved",
            "--head",
            "abcdef0",
            "--agent",
            agent_id,
        ],
    )
    assert "gate review-1/quality=approved" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("legacy_stage", "canonical", "vendor"),
    [
        ("grok-pr", "review-1", "grok"),
        ("codex-pr", "review-2", "codex"),
    ],
)
def test_gate_record_accepts_stored_legacy_agent_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    legacy_stage: str,
    canonical: str,
    vendor: str,
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    head = "abcdef0"
    agent_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        if canonical == "review-2":
            for dimension in ("quality", "logic"):
                gate_id = str(uuid.uuid4())
                store.write(
                    "review_gate",
                    "insert",
                    gate_id,
                    {
                        "id": gate_id,
                        "task_id": tid,
                        "stage": "grok-pr",
                        "dimension": dimension,
                        "vendor": "grok",
                        "verdict": "approved",
                        "evidence": None,
                        "head_sha": head,
                        "agent_id": str(uuid.uuid4()),
                        "recorded_at": utcnow(),
                    },
                )
        store.write(
            "agent",
            "insert",
            agent_id,
            {
                "id": agent_id,
                "session_id": "s",
                "task_id": tid,
                "round": None,
                "role": "pr-reviewer-quality",
                "vendor": vendor,
                "stage": legacy_stage,
                "status": "done",
                "started_at": utcnow(),
                "finished_at": utcnow(),
                "note": "approved",
            },
        )
    finally:
        store.close()

    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            canonical,
            "--dimension",
            "quality",
            "--vendor",
            vendor,
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            agent_id,
        ],
    )

    assert f"gate {canonical}/quality=approved" in capsys.readouterr().out


def test_gate_record_review_2_before_review_1_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    # Agent start now enforces this ordering too. Seed a valid completed row so
    # this test continues to exercise the independent gate-write boundary.
    agent_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        store.write(
            "agent",
            "insert",
            agent_id,
            {
                "id": agent_id,
                "session_id": "s",
                "task_id": tid,
                "round": None,
                "role": "pr-reviewer-quality",
                "vendor": "codex",
                "stage": "review-2",
                "status": "done",
                "started_at": utcnow(),
                "finished_at": utcnow(),
                "note": "approved",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="review-2 requires approved review-1"):
        run(
            tmp_path,
            [
                "gate",
                "record",
                "--task",
                tid,
                "--stage",
                "review-2",
                "--dimension",
                "quality",
                "--vendor",
                "codex",
                "--verdict",
                "approved",
                "--head",
                "abcdef0",
                "--agent",
                agent_id,
            ],
        )


def test_legacy_stage_alias_writes_canonical_stage_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    aid = _start_pr_reviewer(
        tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="grok"
    )
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    capsys.readouterr()

    # Keep the legacy stage here to verify CLI alias compatibility.
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "grok-pr",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "approved",
            "--head",
            "abcdef0",
            "--agent",
            aid,
        ],
    )
    captured = capsys.readouterr()
    assert captured.err == "deprecated stage/key name grok-pr; use review-1\n"
    assert "gate review-1/quality=approved" in captured.out

    store = Store(tmp_path)
    try:
        stages = [
            row["stage"]
            for row in store.rows("review_gate")
            if row.get("task_id") == tid
        ]
    finally:
        store.close()
    assert stages == ["review-1"]


def test_legacy_checklist_key_alias_writes_canonical_key_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)

    # Keep the legacy key here to verify CLI alias compatibility.
    run(
        tmp_path,
        [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            "grok_pr_quality",
            "--status",
            "pending",
            "--source",
            "script",
        ],
    )
    captured = capsys.readouterr()
    assert captured.err == (
        "deprecated stage/key name grok_pr_quality; use review_1_quality\n"
    )
    assert captured.out == "checklist review_1_quality=pending\n"

    store = Store(tmp_path)
    try:
        keys = {
            row["key"]
            for row in store.rows("checklist_item")
            if row.get("task_id") == tid
        }
    finally:
        store.close()
    assert "review_1_quality" in keys
    assert "grok_pr_quality" not in keys


def test_stored_legacy_checklist_rows_open_canonical_second_review_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    prerequisites = {
        "session_registered",
        "spec_written",
        "implementer_done",
        "reviewer_approved",
        "local_check_pass",
        "pushed",
    }
    # Keep legacy keys here to verify stored-row compatibility.
    legacy_names = {
        "review_1_quality": "grok_pr_quality",
        "review_1_logic": "grok_pr_logic",
    }
    store = Store(tmp_path)
    try:
        for stored in store.rows("checklist_item"):
            if stored.get("task_id") != tid:
                continue
            row = {
                key: value
                for key, value in stored.items()
                if not key.startswith("_")
            }
            key = str(row["key"])
            if key in prerequisites:
                row["status"] = "ja"
            if key in legacy_names:
                row["key"] = legacy_names[key]
                row["status"] = "ja"
            store.write("checklist_item", "update", str(row["id"]), row)
    finally:
        store.close()

    run(tmp_path, ["next", "--task", tid, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert {step["key"] for step in payload["next"]} == {
        "review_2_quality",
        "review_2_logic",
    }


def test_reviewer_start_before_implementer_done_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="reviewing|implementer_verdict"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "reviewer",
                "--vendor",
                "grok",
                "--round",
                "1",
            ],
        )


def test_implementer_start_requires_the_first_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    # Without a separate pin, the task resolves its implementer to slot first.
    with pytest.raises(SystemExit, match=r"implementer requires --vendor grok \(configured implementer\)"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "implementer",
                "--vendor",
                "codex",
                "--round",
                "1",
            ],
        )
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    aid = _last_agent_id(capsys.readouterr().out)
    store = Store(tmp_path)
    try:
        rows = [a for a in store.rows("agent") if a.get("id") == aid]
        assert len(rows) == 1
        assert rows[0]["vendor"] == "grok"
    finally:
        store.close()


def test_implementer_start_still_accepts_grok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    aid = _last_agent_id(capsys.readouterr().out)
    store = Store(tmp_path)
    try:
        rows = [a for a in store.rows("agent") if a.get("id") == aid]
        assert len(rows) == 1
        assert rows[0]["vendor"] == "grok"
    finally:
        store.close()


def test_reviewer_start_rejects_codex_vendor(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match="reviewer requires --vendor grok"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                "t",
                "--role",
                "reviewer",
                "--vendor",
                "codex",
                "--round",
                "1",
            ],
        )


def test_implementer_and_reviewer_require_round(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match="implementer requires --round"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                "t",
                "--role",
                "implementer",
                "--vendor",
                "codex",
            ],
        )
    with pytest.raises(SystemExit, match="reviewer requires --round"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                "t",
                "--role",
                "reviewer",
                "--vendor",
                "grok",
            ],
        )


def test_implementer_finish_blocked_sets_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "blocked"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "failed"


def _seed_foreign_implement_task(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "sess-f",
                "payload": {"id": "sess-f", "kind": "human", "status": "active"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 2,
                "table": "task",
                "op": "insert",
                "row_id": "t-foreign",
                "payload": {
                    "id": "t-foreign",
                    "session_id": "sess-f",
                    "workflow": "implement",
                    "state": "implementing",
                    "current_round": 1,
                },
                "occurred_at": "2026-08-13T12:00:01Z",
            }
        )
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 3,
                "table": "task_round",
                "op": "insert",
                "row_id": "r-foreign",
                "payload": {
                    "id": "r-foreign",
                    "task_id": "t-foreign",
                    "round": 1,
                    "implementer_verdict": None,
                },
                "occurred_at": "2026-08-13T12:00:02Z",
            }
        )
    finally:
        store.close()


def test_agent_start_on_foreign_task_dies(tmp_path: Path) -> None:
    _seed_foreign_implement_task(tmp_path)
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "sess-f",
                "--task",
                "t-foreign",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--round",
                "1",
            ],
        )


def test_check_record_on_foreign_task_dies(tmp_path: Path) -> None:
    _seed_foreign_implement_task(tmp_path)
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "check",
                "record",
                "--task",
                "t-foreign",
                "--name",
                "lint",
                "--command",
                "true",
                "--result",
                "pass",
            ],
        )


def test_work_add_on_foreign_session_dies(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "sess-f",
                "payload": {"id": "sess-f", "kind": "human", "status": "active"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "work",
                "add",
                "--session",
                "sess-f",
                "--key",
                "standing",
                "--closable-by",
                "agent",
            ],
        )


def test_implementer_blocked_sets_round_finished_at(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "blocked"])
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        rounds = [
            r
            for r in store.rows("task_round")
            if r.get("task_id") == tid and r.get("round") == 1
        ]
        assert len(rounds) == 1
        assert rounds[0].get("finished_at") is not None
    finally:
        store.close()


def test_reviewer_finish_rejected_sets_implementing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    rev_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", rev_id, "--verdict", "rejected"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "implementing"


def test_implementer_finish_on_pr_review_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["task", "state", tid, "pr-review"])
    capsys.readouterr()

    with pytest.raises(SystemExit, match="task state must be implementing"):
        run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])


def test_second_implementer_finish_same_round_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    first_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", first_id, "--verdict", "done"])
    capsys.readouterr()

    # First finish left state reviewing + implementer_verdict set. Force a second
    # working implementer on the same round so finish hits the double-finish guard.
    second_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["state"] = "implementing"
        store.write(
            "task",
            "update",
            tid,
            {k: v for k, v in task.items() if not k.startswith("_")},
        )
        store.write(
            "agent",
            "insert",
            second_id,
            {
                "id": second_id,
                "session_id": "s",
                "task_id": tid,
                "round": 1,
                "role": "implementer",
                "vendor": "grok",
                "status": "working",
                "started_at": utcnow(),
                "finished_at": None,
                "note": None,
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="implementer already finished this round"):
        run(tmp_path, ["agent", "finish", "--id", second_id, "--verdict", "done"])


def test_round_start_with_working_agent_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    capsys.readouterr()

    with pytest.raises(SystemExit, match="round still has a working agent"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_round_start_with_working_pr_reviewer_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "grok",
        ],
    )
    capsys.readouterr()

    with pytest.raises(SystemExit, match="round still has a working agent"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_round_start_awaiting_reviewer_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    with pytest.raises(SystemExit, match="round 1 awaits its reviewer"):
        run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    capsys.readouterr()


def test_round_start_after_blocked_implementer_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "blocked"])
    capsys.readouterr()

    run(tmp_path, ["round", "start", "--task", tid])
    assert "round 2" in capsys.readouterr().out


def test_round_start_after_rejected_reviewer_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    rev_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", rev_id, "--verdict", "rejected"])
    capsys.readouterr()

    run(tmp_path, ["round", "start", "--task", tid])
    assert "round 2" in capsys.readouterr().out


def test_round_start_heals_missing_round_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        rounds = [
            r for r in store.rows("task_round") if r.get("task_id") == tid
        ]
        assert len(rounds) == 1
        row_id = rounds[0]["id"]
        store.write("task_round", "delete", row_id, {})
    finally:
        store.close()

    run(tmp_path, ["round", "start", "--task", tid])
    assert "round 2" in capsys.readouterr().out


def test_round_start_after_failed_check_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(tmp_path, ["check", "record", "--task", tid, "--name", "lint", "--command", "pytest", "--result", "fail"])
    capsys.readouterr()
    run(tmp_path, ["round", "start", "--task", tid])
    assert "round 2" in capsys.readouterr().out


def test_round_start_after_rejected_gate_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "grok",
        ],
    )
    reviewer_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", reviewer_id, "--verdict", "rejected"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "grok-pr",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "rejected",
            "--head",
            "abcdef0",
            "--agent",
            reviewer_id,
            "--evidence",
            "finding",
        ],
    )
    capsys.readouterr()

    run(tmp_path, ["round", "start", "--task", tid])
    assert "round 2" in capsys.readouterr().out


def test_round_start_with_duplicate_round_rows_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    row_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        # Both rows are done so only the duplicate row count decides this case.
        store.write(
            "task_round",
            "insert",
            row_id,
            {
                "id": row_id,
                "task_id": tid,
                "round": 1,
                "implementer_verdict": "done",
                "reviewer_verdict": None,
                "started_at": utcnow(),
                "finished_at": None,
            },
        )
    finally:
        store.close()

    run(tmp_path, ["round", "start", "--task", tid])
    assert "round 2" in capsys.readouterr().out


def test_implementer_finish_after_session_closed_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)

    store = Store(tmp_path)
    try:
        session = store.row("session", "s")
        assert session is not None
        session["status"] = "closed"
        store.write(
            "session",
            "update",
            "s",
            {k: v for k, v in session.items() if not k.startswith("_")},
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="session is not active"):
        run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])


def test_agent_finish_on_foreign_agent_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    foreign_agent_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "agent",
                "op": "insert",
                "row_id": foreign_agent_id,
                "payload": {
                    "id": foreign_agent_id,
                    "session_id": "s",
                    "task_id": tid,
                    "round": 1,
                    "role": "implementer",
                    "vendor": "grok",
                    "status": "working",
                    "started_at": utcnow(),
                    "finished_at": None,
                    "note": None,
                },
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="another device"):
        run(tmp_path, ["agent", "finish", "--id", foreign_agent_id, "--verdict", "done"])

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "implementing"


def test_watch_grok_usage_prints_snapshot_or_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    monkeypatch.setattr(
        "agent_cli.main.scan_usage",
        lambda store: "11111111-1111-1111-1111-111111111111",
    )
    run(tmp_path, ["watch", "grok-usage"])
    assert "usage.snapshot 11111111-1111-1111-1111-111111111111" in capsys.readouterr().out

    monkeypatch.setattr("agent_cli.main.scan_usage", lambda store: None)
    run(tmp_path, ["watch", "grok-usage"])
    assert "usage.snapshot none" in capsys.readouterr().out


def test_watch_grok_usage_auth_stale_prints_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()

    def fake_scan(_store: object) -> str:
        raise AuthStale("grok auth token expired; run grok login")

    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan)
    run(tmp_path, ["watch", "grok-usage"])
    captured = capsys.readouterr()
    assert captured.out.strip() == "usage.snapshot skipped"
    assert "error" not in captured.out.lower()
    assert "error" not in captured.err.lower()


def test_watch_errors_empty_scan_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    monkeypatch.setattr("agent_cli.errors.scan_errors", lambda store, fetch: ([], []))
    run(tmp_path, ["watch", "errors"])
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(
        "agent_cli.errors.scan_errors",
        lambda store, fetch: (["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"], ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]),
    )
    run(tmp_path, ["watch", "errors"])
    out = capsys.readouterr().out
    assert "error.seen aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in out
    assert "error.seen enrich bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in out


def test_watch_error_fix_empty_scan_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", lambda store, runner: [])
    run(tmp_path, ["watch", "error-fix"])
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(
        "agent_cli.error_fix_act.scan_error_fix",
        lambda store, runner: ["error.fix x task=t worktree=/tmp/w"],
    )
    run(tmp_path, ["watch", "error-fix"])
    assert "error.fix x task=t worktree=/tmp/w" in capsys.readouterr().out


def test_knock_once_does_not_poll_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    calls: list[object] = []

    def fake_scan(store: object) -> None:
        calls.append(store)

    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan)
    run(tmp_path, ["knock", "--once"])
    assert calls == []


def test_knock_daemon_polls_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    scan_calls = {"n": 0}
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(_store: object) -> str:
        scan_calls["n"] += 1
        if scan_calls["n"] == 1:
            raise StoreError("boom")
        return "11111111-1111-1111-1111-111111111111"

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        if listen_calls["n"] == 2:
            raise SystemExit("stop")
        return None

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "usage.snapshot error: boom" in captured.err
    assert "usage.snapshot 11111111-1111-1111-1111-111111111111" in captured.out


def test_knock_daemon_auth_stale_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(_store: object) -> str:
        raise AuthStale("grok auth token expired; run grok login")

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        raise SystemExit("stop")

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "usage.snapshot error" not in captured.err
    assert "usage.snapshot" not in captured.out


def test_knock_once_does_not_poll_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    (tmp_path / "error-fix.json").write_text('{"session_id":"s"}', encoding="utf-8")
    calls: list[object] = []

    def fake_scan(store: object, fetch: object) -> tuple[list[str], list[str]]:
        calls.append(store)
        return ([], [])

    monkeypatch.setattr("agent_cli.errors.scan_errors", fake_scan)
    run(tmp_path, ["knock", "--once"])
    assert calls == []


def test_knock_once_does_not_call_scan_error_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    calls: list[object] = []

    def fake_scan(*_args: object, **_kwargs: object) -> list[str]:
        calls.append(_args)
        return []

    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", fake_scan)
    run(tmp_path, ["knock", "--once"])
    assert calls == []


def test_knock_daemon_polls_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    (tmp_path / "error-fix.json").write_text('{"session_id":"s"}', encoding="utf-8")
    capsys.readouterr()
    scan_calls = {"n": 0}
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(_store: object, _fetch: object) -> tuple[list[str], list[str]]:
        scan_calls["n"] += 1
        if scan_calls["n"] == 1:
            raise StoreError("nope")
        return (
            ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
        )

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        if listen_calls["n"] == 2:
            raise SystemExit("stop")
        return None

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.errors.scan_errors", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "error.seen error: nope" in captured.err
    assert "error.seen aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in captured.out
    assert "error.seen enrich bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in captured.out


def test_knock_daemon_calls_scan_error_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    scan_calls = {"n": 0}
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(*_args: object, **_kwargs: object) -> list[str]:
        scan_calls["n"] += 1
        if scan_calls["n"] == 1:
            raise StoreError("nope")
        return ["error.fix x task=t worktree=/tmp/w"]

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        if listen_calls["n"] == 2:
            raise SystemExit("stop")
        return None

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "error.fix error: nope" in captured.err
    assert "error.fix x task=t worktree=/tmp/w" in captured.out


def test_knock_daemon_skips_errors_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    calls: list[object] = []
    listen_calls = {"n": 0}

    def fake_scan(store: object, fetch: object) -> tuple[list[str], list[str]]:
        calls.append(store)
        return ([], [])

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        raise SystemExit("stop")

    monkeypatch.setattr("agent_cli.main.usage_poll_due", lambda *_a, **_k: True)
    monkeypatch.setattr("agent_cli.main.scan_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.errors.scan_errors", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    assert calls == []


def test_work_set_done_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(
        tmp_path,
        ["work", "add", "--session", "s", "--key", "standing", "--closable-by", "agent"],
    )
    capsys.readouterr()
    run(
        tmp_path,
        [
            "work",
            "set",
            "--session",
            "s",
            "--key",
            "standing",
            "--status",
            "done",
            "--source",
            "script",
        ],
    )
    assert "work standing=done" in capsys.readouterr().out
    run(tmp_path, ["work", "list"])
    listing = capsys.readouterr().out
    assert "s  standing  done  agent" in listing


def test_checklist_deviation_flags_persist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(
        tmp_path,
        [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--status",
            "ja",
            "--source",
            "human",
            "--evidence",
            "session register",
            "--deviation-declared",
            "true",
            "--deviation-granted",
            "true",
            "--granted-by",
            "reviewer",
            "--actor-session",
            "s",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        items = [
            r
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid and r.get("key") == "session_registered"
        ]
        assert len(items) == 1
        item = items[0]
        assert item["status"] == "ja"
        assert item["deviation_declared"] is True
        assert item["deviation_granted"] is True
        assert item["granted_by"] == "reviewer"
    finally:
        store.close()


def test_activity_add(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    payload = tmp_path / "mail.json"
    payload.write_text('{"to_session": "sess-2", "body": "hello"}', encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "sess-1",
            "--type",
            "message",
            "--payload-file",
            str(payload),
        ],
    )
    out = capsys.readouterr().out
    assert "activity " in out
    assert "type=message" in out
    store = Store(tmp_path)
    try:
        rows = store.rows("activity")
        assert len(rows) == 1
        assert rows[0]["type"] == "message"
        assert rows[0]["session_id"] == "sess-1"
        assert rows[0]["payload"]["to_session"] == "sess-2"
        assert rows[0]["execution_status"] == "pending"
    finally:
        store.close()


def _seed_cli_error_seen_for_conclusion(
    tmp_path: Path,
    *,
    error_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    fingerprint: str = "traceback-fingerprint",
    repo: str | None = "org/app",
    with_error_fix_skill: bool = True,
) -> None:
    run(tmp_path, ["init"])
    register = [
        "session",
        "register",
        "--id",
        "error-session",
        "--kind",
        "human",
        "--skill",
        "spine",
    ]
    if with_error_fix_skill:
        register.extend(["--skill", "error-fix"])
    run(tmp_path, register)
    payload: dict[str, object] = {"fingerprint": fingerprint}
    if repo is not None:
        payload["repo"] = repo
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            error_id,
            {
                "id": error_id,
                "session_id": "error-session",
                "type": "error.seen",
                "payload": payload,
                "execution_status": "done",
            },
        )
    finally:
        store.close()


def _add_cli_error_conclusion(
    tmp_path: Path,
    *,
    typ: str,
    payload: dict[str, object],
) -> None:
    payload_file = tmp_path / f"{typ}.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "error-session",
            "--type",
            typ,
            "--payload-file",
            str(payload_file),
        ],
    )


def test_activity_add_error_fix_happy_path(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)

    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": error_id, "fingerprint": fingerprint},
    )

    store = Store(tmp_path)
    try:
        rows = [row for row in store.rows("activity") if row["type"] == "error.fix"]
        assert len(rows) == 1
        assert rows[0]["type"] == "error.fix"
        assert rows[0]["execution_status"] == "pending"
    finally:
        store.close()


def test_activity_add_error_skip_happy_path(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)

    _add_cli_error_conclusion(
        tmp_path,
        typ="error.skip",
        payload={
            "error_id": error_id,
            "fingerprint": fingerprint,
            "reason": "Known external failure",
        },
    )

    store = Store(tmp_path)
    try:
        rows = [row for row in store.rows("activity") if row["type"] == "error.skip"]
        assert len(rows) == 1
        assert rows[0]["type"] == "error.skip"
        assert rows[0]["execution_status"] == "pending"
    finally:
        store.close()


def test_activity_add_error_fix_requires_mapped_repo(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(
        tmp_path,
        error_id=error_id,
        fingerprint=fingerprint,
        repo=None,
    )

    with pytest.raises(SystemExit, match="unmapped-repo"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_fix_requires_skill(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(
        tmp_path,
        error_id=error_id,
        fingerprint=fingerprint,
        with_error_fix_skill=False,
    )

    with pytest.raises(SystemExit, match="error-fix"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_refuses_second_error_conclusion(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    _add_cli_error_conclusion(
        tmp_path,
        typ="error.skip",
        payload={
            "error_id": error_id,
            "fingerprint": fingerprint,
            "reason": "Known external failure",
        },
    )

    with pytest.raises(SystemExit, match="conclusion already exists"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_skip_requires_reason(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)

    with pytest.raises(SystemExit, match="reason is required"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.skip",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_conclusion_rejects_fingerprint_mismatch(
    tmp_path: Path,
) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_cli_error_seen_for_conclusion(
        tmp_path,
        error_id=error_id,
        fingerprint="traceback-fingerprint",
    )

    with pytest.raises(SystemExit, match="fingerprint mismatch"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": "different-fingerprint"},
        )


def test_activity_add_error_fix_rejects_already_open_draft(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"fingerprint": fingerprint},
                "execution_status": "pending",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="already-open-draft"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_fix_rejects_draft_by_head(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"repo": "org/app", "head": "error-fix-aaaaaaaa", "title": "Fix"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="already-open-draft"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_task_create_error_id_rejects_already_open_draft(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"fingerprint": fingerprint},
                "execution_status": "pending",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="already-open-draft"):
        run(
            tmp_path,
            [
                "task",
                "create",
                "--session",
                "error-session",
                "--workflow",
                "implement",
                "--error-id",
                error_id,
                "--title",
                "Fix observed error",
            ],
        )


def test_activity_add_error_fix_after_merged_draft_is_allowed(tmp_path: Path) -> None:
    first_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    second_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=first_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"fingerprint": fingerprint},
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
            {
                "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "session_id": "error-session",
                "type": "pr.merged",
                "payload": {"pr_open_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            second_id,
            {
                "id": second_id,
                "session_id": "error-session",
                "type": "error.seen",
                "payload": {"fingerprint": fingerprint, "repo": "org/app"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()

    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": second_id, "fingerprint": fingerprint},
    )


def test_task_create_error_id_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id)
    capsys.readouterr()

    create = [
        "task",
        "create",
        "--session",
        "error-session",
        "--workflow",
        "implement",
        "--error-id",
        error_id,
        "--title",
        "Fix observed error",
    ]
    run(tmp_path, create)
    first_id = _last_task_id(capsys.readouterr().out)
    run(tmp_path, create)
    second_id = _last_task_id(capsys.readouterr().out)

    assert first_id == second_id
    store = Store(tmp_path)
    try:
        assert len(store.rows("task")) == 1
    finally:
        store.close()


def test_activity_add_rejects_issue_assigned_ack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "assigned", "--kind", "runner"])
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "asg-1",
            {
                "id": "asg-1",
                "session_id": "assigned",
                "type": "issue.assigned",
                "payload": {"assigned_at": "2026-01-01T00:00:00Z"},
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "asg-2",
            {
                "id": "asg-2",
                "session_id": "assigned",
                "type": "issue.assigned",
                "payload": {"assigned_at": "2026-02-01T00:00:00Z"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    bad = tmp_path / "ack-bad.json"
    bad.write_text('{"assigned_id": "asg-2"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="written by a script"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "assigned",
                "--type",
                "issue.assigned.ack",
                "--payload-file",
                str(bad),
            ],
        )
    good = tmp_path / "ack-good.json"
    good.write_text('{"assigned_id": "asg-1"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="written by a script"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "assigned",
                "--type",
                "issue.assigned.ack",
                "--payload-file",
                str(good),
            ],
        )


@pytest.mark.parametrize(
    "typ",
    [
        "pr.merged",
        "mail.ingest",
        "query.result",
        "session.register",
        "usage.snapshot",
        "issue.assigned",
        "issue.assigned.ack",
    ],
)
def test_activity_add_rejects_script_only(tmp_path: Path, typ: str) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    payload = tmp_path / "script.json"
    payload.write_text('{"repo": "o/r", "number": 1}', encoding="utf-8")
    with pytest.raises(SystemExit, match="written by a script"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "sess-1",
                "--type",
                typ,
                "--payload-file",
                str(payload),
            ],
        )


def test_open_store_dies_on_legacy_sqlite(tmp_path: Path) -> None:
    (tmp_path / "ledger.sqlite").write_bytes(b"")
    with pytest.raises(SystemExit, match="move it aside"):
        run(tmp_path, ["init"])


def test_activity_add_unknown_type_is_error(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    payload = tmp_path / "x.json"
    payload.write_text('{"k": "v"}', encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "sess-1",
            "--type",
            "not-a-catalog-type",
            "--payload-file",
            str(payload),
        ],
    )
    store = Store(tmp_path)
    try:
        rows = store.rows("activity")
        assert len(rows) == 1
        assert rows[0]["execution_status"] == "error"
    finally:
        store.close()


def test_activity_add_rejects_foreign_session(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_replica_row(
            {
                "table": "session",
                "row_id": "foreign",
                "origin_device_id": "other-device",
                "payload": {"id": "foreign", "kind": "human", "status": "active"},
                "updated_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()
    payload = tmp_path / "mail.json"
    payload.write_text('{"to_session": "x", "body": "hello"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "foreign",
                "--type",
                "message",
                "--payload-file",
                str(payload),
            ],
        )


def test_activity_add_rejects_closed_session(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["session", "close", "--id", "sess-1"])
    payload = tmp_path / "mail.json"
    payload.write_text('{"to_session": "x", "body": "hello"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="not active"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "sess-1",
                "--type",
                "message",
                "--payload-file",
                str(payload),
            ],
        )


def test_spine_skill_required_for_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "bare", "--kind", "human"])
    with pytest.raises(SystemExit, match="does not have skill spine"):
        run(tmp_path, ["task", "create", "--session", "bare", "--workflow", "implement", "--title", "Nope"])
    run(tmp_path, ["session", "skill", "list", "--id", "bare"])
    listed = capsys.readouterr().out
    assert "(none)" in listed
    run(tmp_path, ["session", "skill", "attach", "--id", "bare", "--skill", "spine"])
    run(tmp_path, ["session", "skill", "list", "--id", "bare"])
    assert "spine" in capsys.readouterr().out
    run(tmp_path, ["task", "create", "--session", "bare", "--workflow", "implement", "--title", "Yes"])


def test_review_loop_and_pr_review_skills_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "T"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="does not have skill review-loop"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--round",
                "1",
            ],
        )
    with pytest.raises(SystemExit, match="does not have skill pr-review"):
        run(
            tmp_path,
            [
                "gate",
                "record",
                "--task",
                tid,
                "--stage",
                "review-1",
                "--dimension",
                "quality",
                "--vendor",
                "grok",
                "--verdict",
                "approved",
                "--head",
                "abc1234",
                "--agent",
                str(uuid.uuid4()),
            ],
        )
    with pytest.raises(SystemExit, match="does not have skill pr-review"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "pr-reviewer-quality",
                "--vendor",
                "grok",
            ],
        )
    run(tmp_path, ["session", "skill", "attach", "--id", "s", "--skill", "review-loop"])
    run(tmp_path, ["session", "skill", "attach", "--id", "s", "--skill", "pr-review"])


def test_knock_and_watch_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    run(tmp_path, ["knock", "--once"])
    run(tmp_path, ["watch", "pr-merged"])
    out = capsys.readouterr().out
    assert "pr.merged none" in out


def test_watch_usage_mentions_grok_usage() -> None:
    with pytest.raises(SystemExit, match="grok-usage"):
        main(["watch"])
    with pytest.raises(SystemExit, match="grok-usage"):
        main(["watch", "nope"])


def test_watch_assigned_rejects_unknown_flag(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match=r"assigned \[--follow\]"):
        run(tmp_path, ["watch", "assigned", "--folow"])
    with pytest.raises(SystemExit, match=r"assigned \[--follow\]"):
        run(tmp_path, ["watch", "assigned", "--follow", "--follow"])


def test_watch_assigned_requires_watch_json(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match="assigned_repos|watch.json"):
        run(tmp_path, ["watch", "assigned"])


def test_watch_usage_mentions_assigned(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match=r"assigned \[--follow\]\|grok-usage"):
        run(tmp_path, ["watch"])


def test_watch_usage_mentions_errors() -> None:
    with pytest.raises(SystemExit, match="errors"):
        main(["watch"])


def test_watch_usage_mentions_error_fix() -> None:
    with pytest.raises(SystemExit, match="error-fix"):
        main(["watch"])


def test_session_close_refuses_pending_error_fix(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": error_id, "fingerprint": fingerprint},
    )
    with pytest.raises(SystemExit, match="pending error.fix"):
        run(tmp_path, ["session", "close", "--id", "error-session"])


def test_session_close_refuses_retryable_error_fix_configuration_error(
    tmp_path: Path,
) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(
        tmp_path, error_id=error_id, fingerprint=fingerprint
    )
    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": error_id, "fingerprint": fingerprint},
    )
    store = Store(tmp_path)
    try:
        fixes = [
            row
            for row in store.rows("activity")
            if row.get("type") == "error.fix"
        ]
        assert len(fixes) == 1
        fix = fixes[0]
        updated = {key: value for key, value in fix.items() if not key.startswith("_")}
        updated["execution_status"] = "error"
        updated["execution_error"] = "models is not configured"
        updated["result"] = {
            "kind": "model-configuration",
            "configuration_fingerprint": "fingerprint",
        }
        store.write("activity", "update", fix["id"], updated)
    finally:
        store.close()

    with pytest.raises(SystemExit, match="pending error.fix"):
        run(tmp_path, ["session", "close", "--id", "error-session"])


def test_allow_next_close_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "sess-1",
            "--kind",
            "human",
            "--skill",
            "spine",
        ],
    )
    capsys.readouterr()
    run(tmp_path, ["allow", "--action", "claim-done", "--session", "sess-1"])
    assert "allow action=claim-done" in capsys.readouterr().out
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["next", "--task", tid])
    assert "session_registered" in capsys.readouterr().out
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    with pytest.raises(SystemExit):
        run(tmp_path, ["allow", "--action", "pr-ready", "--session", "sess-1"])
    run(tmp_path, ["allow", "--action", "pr-create", "--draft", "true"])
    assert "allow action=pr-create" in capsys.readouterr().out
    capsys.readouterr()
    run(tmp_path, ["run", "--task", tid, "--dry-run"])
    dry = capsys.readouterr().out
    assert f"run task={tid}" in dry
    assert "spec_written" in dry


def test_next_implementer_prints_the_first_slot_vendor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "human",
            "--evidence",
            "five-part spec",
        ],
    )
    capsys.readouterr()
    run(tmp_path, ["next", "--task", tid])
    out = capsys.readouterr().out
    assert "implementer_done" in out
    assert "vendor=grok" in out
    assert "vendor=grok|codex" not in out
    assert "vendor=None" not in out


_GATE_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _seed_pr_review_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    repo: str | None = "owner/name",
    ref: str | None = "7",
    verdict: str = "rejected",
) -> tuple[str, str]:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "pr-review"])
    create = ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"]
    if repo is not None:
        create += ["--repo", repo]
    if ref is not None:
        create += ["--ref", ref]
    run(tmp_path, create)
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "grok"])
    aid = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", verdict])
    capsys.readouterr()
    return tid, aid


def _gate_argv(tid: str, aid: str, verdict: str, *extra: str, head: str = _GATE_HEAD) -> list[str]:
    return [
        "gate", "record", "--task", tid, "--stage", "review-1", "--dimension", "quality",
        "--vendor", "grok", "--verdict", verdict, "--head", head, "--agent", aid, *extra,
    ]


def _mark_activity_error(tmp_path: Path, activity_id: str) -> None:
    """Mark a queued activity `error`, the way github_act does on a failed request."""
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
        assert row is not None
        store.write(
            "activity",
            "update",
            activity_id,
            {
                "id": activity_id,
                "session_id": row["session_id"],
                "type": row["type"],
                "payload": row["payload"],
                "execution_status": "error",
            },
        )
    finally:
        store.close()


def _review_activities(tmp_path: Path) -> list[dict]:
    store = Store(tmp_path)
    try:
        return [row for row in store.rows("activity") if row["type"] == "review.post"]
    finally:
        store.close()


def test_rejected_gate_without_evidence_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    with pytest.raises(SystemExit, match="--evidence is required"):
        run(tmp_path, _gate_argv(tid, aid, "rejected"))
    assert _review_activities(tmp_path) == []


def test_rejected_gate_queues_its_findings_for_the_pull_request(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()"))
    out = capsys.readouterr().out
    assert "gate review-1/quality=rejected" in out
    assert "type=review.post" in out
    rows = _review_activities(tmp_path)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["repo"] == "owner/name"
    assert payload["number"] == 7
    assert "dto.ts:91 keeps @IsOptional()" in payload["body"]
    # The heading shows the short SHA; the identity key still uses the full one.
    # Checking the prefix alone would not do: the full SHA contains it.
    assert _GATE_HEAD[:7] in payload["body"]
    assert _GATE_HEAD not in payload["body"]
    assert rows[0]["execution_status"] == "pending"


def test_approved_gate_queues_no_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys, verdict="approved")
    run(tmp_path, _gate_argv(tid, aid, "approved"))
    out = capsys.readouterr().out
    assert "gate review-1/quality=approved" in out
    assert "type=review.post" not in out
    assert _review_activities(tmp_path) == []


def test_rejected_gate_without_a_pull_request_queues_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys, repo=None, ref=None)
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "no pull request yet"))
    out = capsys.readouterr().out
    assert "gate review-1/quality=rejected" in out
    assert "type=review.post" not in out
    assert "no pull request on this task: findings not queued" in out
    assert _review_activities(tmp_path) == []


def test_rejected_gate_with_blank_evidence_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    with pytest.raises(SystemExit, match="--evidence is required"):
        run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "   \t "))
    assert _review_activities(tmp_path) == []


def test_rejected_gate_recorded_twice_queues_one_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    first = capsys.readouterr().out
    run(tmp_path, argv)
    second = capsys.readouterr().out
    assert "type=review.post" in first
    assert "type=review.post" not in second
    assert len(_review_activities(tmp_path)) == 1


def test_rejected_gate_requeues_a_comment_the_executor_gave_up_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    capsys.readouterr()
    rows = _review_activities(tmp_path)
    assert len(rows) == 1
    activity_id = rows[0]["id"]

    # github_act marks a transient GitHub failure as `error`; scan_github only
    # picks up pending work, so nothing would retry it on its own.
    _mark_activity_error(tmp_path, activity_id)

    run(tmp_path, argv)
    out = capsys.readouterr().out
    assert "type=review.post" in out
    rows = _review_activities(tmp_path)
    assert len(rows) == 1
    assert rows[0]["id"] == activity_id
    assert rows[0]["execution_status"] == "pending"


@pytest.mark.parametrize('stale_read', [False, True])
def test_gate_retry_preserves_account_and_refuses_switch(tmp_path, capsys, monkeypatch, stale_read):
    from agent_cli.github_act import scan_github

    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, 'rejected', '--evidence', 'file.py:1 retry finding')
    run(tmp_path, argv)
    activity_id = _review_activities(tmp_path)[0]['id']
    binding = {'account': 'original', 'login': 'firstworker'}
    store = Store(tmp_path)
    try:
        row = {k: v for k, v in store.row('activity', activity_id).items() if not k.startswith('_')}
        row.update(execution_status='error', execution_account=binding)
        store.write('activity', 'update', activity_id, row)
    finally:
        store.close()
    (tmp_path / 'github-accounts.json').write_text(json.dumps({
        'accounts': {'replacement': {'login': 'SecondWorker', 'gh_config_dir': '/test/replacement'}},
        'sessions': {'s': 'replacement'},
    }))
    real_row = Store.row
    reads = []
    def read(self, table, rid):
        if table == 'activity' and rid == activity_id:
            reads.append(rid)
            if stale_read and len(reads) == 1:
                return None
        return real_row(self, table, rid)
    monkeypatch.setattr(Store, 'row', read)
    run(tmp_path, argv)
    store = Store(tmp_path)
    try:
        assert store.row('activity', activity_id)['execution_account'] == binding
        assert store.row('activity', activity_id)['execution_status'] == 'pending'
        def forbidden(argv):
            pytest.fail('Changed account must be refused before any GitHub access')
        assert scan_github(store, forbidden) == [f'review.post {activity_id} error']
        assert 'refusing account switch' in store.row('activity', activity_id)['execution_error']
    finally:
        store.close()


def test_rejected_gate_survives_a_stale_existence_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two callers can record the same rejection. One inserts the deterministic
    # activity and the executor marks it `error` before the other takes the lock,
    # so the second caller's insert/update choice is already stale when it writes.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    capsys.readouterr()
    activity_id = _review_activities(tmp_path)[0]["id"]

    _mark_activity_error(tmp_path, activity_id)

    real_row = Store.row
    seen: list[int] = []

    def _stale_op_read(self: Store, table: str, row_id: str):
        # Call 1 is now the combined existence/status read that picks insert or
        # update — that is the read a concurrent insert makes stale, so it alone
        # reports the row as absent. `_settled()` runs later, under the advisory
        # lock, and must see the errored row for the retry to proceed at all.
        if table == "activity" and row_id == activity_id:
            seen.append(1)
            if len(seen) == 1:
                return None
        return real_row(self, table, row_id)

    monkeypatch.setattr(Store, "row", _stale_op_read)

    run(tmp_path, argv)
    out = capsys.readouterr().out
    assert "type=review.post" in out
    rows = _review_activities(tmp_path)
    assert len(rows) == 1
    assert rows[0]["execution_status"] == "pending"


def test_rejected_gate_does_not_retry_a_failed_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recovery exists only for a stale `insert`. An `update` that fails is a real
    # failure: retrying it repeats the same write and buries the original error.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    capsys.readouterr()
    activity_id = _review_activities(tmp_path)[0]["id"]

    _mark_activity_error(tmp_path, activity_id)

    ops: list[str] = []

    def _refuse(self: Store, table: str, op: str, row_id: str, payload: dict, **kwargs: object):
        ops.append(op)
        raise StoreError("write refused")

    monkeypatch.setattr(Store, "write_with_advisory", _refuse)

    with pytest.raises((SystemExit, StoreError)):
        run(tmp_path, argv)
    assert ops == ["update"]


def test_a_second_rejection_with_new_findings_queues_its_own_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same task, lane and head, different findings. Keying only on the lane would
    # treat the second rejection as a repeat and drop its evidence.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "first finding"))
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "corrected finding"))
    capsys.readouterr()
    bodies = sorted(row["payload"]["body"] for row in _review_activities(tmp_path))
    assert len(bodies) == 2
    assert any("first finding" in b for b in bodies)
    assert any("corrected finding" in b for b in bodies)


def test_the_same_finding_at_a_new_head_queues_its_own_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The head is part of the comment's identity: the same defect surviving a new
    # push is a new rejection to report, not a repeat of the one already posted.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    evidence = "dto.ts:91 keeps @IsOptional()"
    other_head = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence, head=other_head))
    capsys.readouterr()
    rows = _review_activities(tmp_path)
    assert len(rows) == 2
    bodies = sorted(row["payload"]["body"] for row in rows)
    assert any(_GATE_HEAD[:7] in b for b in bodies)
    assert any(other_head[:7] in b for b in bodies)


def test_a_retargeted_task_queues_a_comment_on_the_new_pull_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same lane, head and evidence, different destination. Keying only on the
    # content would treat the retargeted comment as one already delivered.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    evidence = "dto.ts:91 keeps @IsOptional()"
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    capsys.readouterr()

    def _retarget(repo: str, ref: str) -> None:
        store = Store(tmp_path)
        try:
            task = store.row("task", tid)
            assert task is not None
            task["repo"] = repo
            task["ref"] = ref
            task["updated_at"] = utcnow()
            store.write("task", "update", tid, task)
        finally:
            store.close()

    # Move the number alone, then the repository alone. Changing both at once would
    # leave either one missing from the key undetected: the other still separates them.
    _retarget("owner/name", "99")
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    _retarget("owner/other", "99")
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    capsys.readouterr()
    rows = _review_activities(tmp_path)
    targets = sorted((row["payload"]["repo"], row["payload"]["number"]) for row in rows)
    assert targets == [("owner/name", 7), ("owner/name", 99), ("owner/other", 99)]


@pytest.mark.parametrize("bad_repo", ["noslash", "owner/name/extra", "/name", "owner/"])
def test_a_malformed_repository_queues_no_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_repo: str
) -> None:
    # A missing repository is caught by the None check on its own. Only a malformed
    # one exercises the validator, so without these the validation is unpinned.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys, repo=bad_repo, ref="7")
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "findings"))
    out = capsys.readouterr().out
    assert "gate review-1/quality=rejected" in out
    assert "no pull request on this task: findings not queued" in out
    assert _review_activities(tmp_path) == []


def test_the_evidence_reaches_the_review_unaltered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The contract says the evidence becomes the review body unaltered. Trimming it
    # would silently reflow an indented block, and Markdown reads indentation.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    evidence = "\n    dto.ts:91 wrong decorator\n        nested detail\n"
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    capsys.readouterr()
    body = _review_activities(tmp_path)[0]["payload"]["body"]
    assert evidence in body, "the evidence was altered"
    assert body.endswith(evidence)


def _spine_session_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, workflow: str = "implement"
) -> str:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "s",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(
        tmp_path,
        ["task", "create", "--session", "s", "--workflow", workflow, "--title", "Ship"],
    )
    return _last_task_id(capsys.readouterr().out)


def _start_pr_reviewer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tid: str,
    *,
    role: str,
    vendor: str,
) -> str:
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            role,
            "--vendor",
            vendor,
        ],
    )
    return _last_agent_id(capsys.readouterr().out)


def test_pr_reviewer_start_validates_stage_vendor_and_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    with pytest.raises(
        SystemExit,
        match=r"stage review-2 requires --vendor codex",
    ):
        run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--stage", "review-2",
                "--vendor", "grok",
            ],
        )
    with pytest.raises(
        SystemExit,
        match=r"review-2 requires approved review-1/quality at the same head",
    ):
        run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--stage", "review-2",
                "--vendor", "codex",
            ],
        )

    run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "pr-reviewer-quality", "--stage", "review-1",
            "--vendor", "grok",
        ],
    )
    aid = _last_agent_id(capsys.readouterr().out)
    store = Store(tmp_path)
    try:
        assert store.row("agent", aid)["stage"] == "review-1"
    finally:
        store.close()


def test_pr_reviewer_start_refuses_first_stage_gates_on_different_heads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    store = Store(tmp_path)
    try:
        for dimension, head in (("quality", "abcdef0"), ("logic", "1234567")):
            gate_id = str(uuid.uuid4())
            store.write(
                "review_gate",
                "insert",
                gate_id,
                {
                    "id": gate_id,
                    "task_id": tid,
                    "stage": "review-1",
                    "dimension": dimension,
                    "vendor": "grok",
                    "verdict": "approved",
                    "evidence": None,
                    "head_sha": head,
                    "agent_id": str(uuid.uuid4()),
                    "recorded_at": utcnow(),
                },
            )
    finally:
        store.close()

    with pytest.raises(
        SystemExit,
        match=r"review-2 requires approved review-1/logic at the same head",
    ):
        run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "pr-reviewer-quality", "--stage", "review-2",
                "--vendor", "codex",
            ],
        )

    store = Store(tmp_path)
    try:
        assert not any(
            row.get("task_id") == tid and row.get("stage") == "review-2"
            for row in store.rows("agent")
        )
    finally:
        store.close()


def test_agent_start_allows_deepseek_only_for_the_pinned_implementer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps(
            {
                "models": {"first": "grok", "second": "codex"},
                "implementer": "deepseek",
                "lane_execution": {"mode": "this-device-is-lane-host"},
            }
        ),
        encoding="utf-8",
    )
    tid = _spine_session_task(tmp_path, capsys)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "deepseek", "--round", "1",
        ],
    )
    implementer_id = _last_agent_id(capsys.readouterr().out)
    with pytest.raises(
        SystemExit,
        match="implementer requires --vendor deepseek \\(configured implementer\\)",
    ):
        run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "implementer", "--vendor", "grok", "--round", "1",
            ],
        )
    run(tmp_path, ["agent", "finish", "--id", implementer_id, "--verdict", "done"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "reviewer", "--vendor", "grok", "--round", "1",
        ],
    )
    assert _last_agent_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="vendor must be claude\\|codex\\|grok"):
        run(
            tmp_path,
            [
                "agent", "start", "--session", "s", "--task", tid,
                "--role", "reviewer", "--vendor", "deepseek", "--round", "1",
            ],
        )
    with pytest.raises(SystemExit, match="vendor must be claude\\|codex\\|grok"):
        run(
            tmp_path,
            [
                "gate", "record", "--task", tid, "--stage", "review-1",
                "--dimension", "quality", "--vendor", "deepseek",
                "--verdict", "approved", "--head", "abcdef0", "--agent", "missing",
            ],
        )


def test_agent_start_uses_task_pins_after_device_models_are_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    config_path = tmp_path / "ai-accounts.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.pop("models")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    run(
        tmp_path,
        [
            "agent", "start", "--session", "s", "--task", tid,
            "--role", "implementer", "--vendor", "grok", "--round", "1",
        ],
    )
    agent_id = _last_agent_id(capsys.readouterr().out)

    store = Store(tmp_path)
    try:
        agent = store.row("agent", agent_id)
        assert agent is not None
        assert agent["task_id"] == tid
        assert agent["vendor"] == "grok"
    finally:
        store.close()


def test_checklist_unavailable_on_gate_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(
        tmp_path,
        [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            "review_1_quality",
            "--status",
            "unavailable",
            "--source",
            "script",
            "--evidence",
            "vendor HTTP 402",
        ],
    )
    assert "checklist review_1_quality=unavailable" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="unavailable requires --evidence"):
        run(
            tmp_path,
            [
                "checklist",
                "set",
                "--task",
                tid,
                "--key",
                "review_1_logic",
                "--status",
                "unavailable",
                "--source",
                "script",
            ],
        )
    with pytest.raises(SystemExit, match="unavailable is not allowed for session_registered"):
        run(
            tmp_path,
            [
                "checklist",
                "set",
                "--task",
                tid,
                "--key",
                "session_registered",
                "--status",
                "unavailable",
                "--source",
                "script",
                "--evidence",
                "not a gate",
            ],
        )


def test_close_step_unavailable_after_gate_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    for key in (
        "session_registered",
        "spec_written",
        "implementer_done",
        "reviewer_approved",
        "local_check_pass",
        "pushed",
    ):
        run(
            tmp_path,
            [
                "checklist",
                "set",
                "--task",
                tid,
                "--key",
                key,
                "--status",
                "ja",
                "--source",
                "script",
                "--evidence",
                "fixture prerequisite",
            ],
        )
    aid = _start_pr_reviewer(
        tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="grok"
    )
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "unavailable"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "unavailable",
            "--head",
            "abcdef0",
            "--agent",
            aid,
            "--evidence",
            "vendor HTTP 402",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="Usage: agent close-step"):
        run(
            tmp_path,
            [
                "close-step",
                "--task",
                tid,
                "--key",
                "review_1_quality",
                "--source",
                "script",
                "--status",
                "unavailable",
            ],
        )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "review_1_quality",
            "--source",
            "script",
            "--status",
            "unavailable",
            "--evidence",
            "vendor HTTP 402",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        items = [
            row
            for row in store.rows("checklist_item")
            if row.get("task_id") == tid and row.get("key") == "review_1_quality"
        ]
    finally:
        store.close()
    assert len(items) == 1
    assert items[0]["status"] == "unavailable"


def test_task_state_gate_blocked_and_superseded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(tmp_path, ["task", "state", tid, "gate-blocked"])
    assert f"task {tid} state=gate-blocked" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="cannot mark done from gate-blocked"):
        run(tmp_path, ["task", "state", tid, "done"])
    run(tmp_path, ["task", "state", tid, "superseded"])
    assert f"task {tid} state=superseded" in capsys.readouterr().out
    run(tmp_path, ["status"])
    status = capsys.readouterr().out
    assert "tasks_open=0" in status
    assert f"task {tid} " not in status
    run(tmp_path, ["session", "close", "--id", "s"])
    assert "closed s" in capsys.readouterr().out


def test_cannot_supersede_a_done_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    store = Store(tmp_path)
    try:
        row = store.row("task", tid)
        assert row is not None
        row["state"] = "done"
        store.write("task", "update", tid, {k: v for k, v in row.items() if not k.startswith("_")})
    finally:
        store.close()
    with pytest.raises(SystemExit, match="cannot supersede a done task"):
        run(tmp_path, ["task", "state", tid, "superseded"])


def test_round_start_refuses_later_states(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(tmp_path, ["task", "state", tid, "pr-review"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="round start requires state open\\|implementing\\|reviewing\\|failed"):
        run(tmp_path, ["round", "start", "--task", tid])
    run(tmp_path, ["task", "state", tid, "gate-blocked"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="round start requires state open\\|implementing\\|reviewing\\|failed"):
        run(tmp_path, ["round", "start", "--task", tid])
    run(tmp_path, ["task", "state", tid, "implementing"])
    capsys.readouterr()
    run(tmp_path, ["round", "start", "--task", tid])
    assert f"task {tid} round 1" in capsys.readouterr().out


def test_round_start_refuses_unavailable_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    aid = _start_pr_reviewer(tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="grok")
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "unavailable"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "unavailable",
            "--head",
            "abcdef0",
            "--agent",
            aid,
            "--evidence",
            "vendor HTTP 402",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="round start refused while a gate is unavailable"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_same_second_gate_prefers_newer_updated_row() -> None:
    recorded_at = "2026-01-01T00:00:00Z"
    newer = {
        "task_id": "task-1",
        "stage": "review-1",
        "dimension": "quality",
        "vendor": "grok",
        "verdict": "approved",
        "head_sha": "abcdef0",
        "recorded_at": recorded_at,
        "updated_at": "2026-01-01T00:00:02Z",
    }
    older = {
        **newer,
        "verdict": "unavailable",
        "updated_at": "2026-01-01T00:00:01Z",
    }

    class GateStore:
        def rows(self, table: str) -> list[dict]:
            if table == "review_gate":
                return [newer, older]
            if table in ("checklist_item", "local_check"):
                return []
            raise AssertionError(f"unexpected table: {table}")

        def row(self, table: str, row_id: str) -> dict | None:
            assert (table, row_id) == ("task", "task-1")
            return {"id": "task-1"}

    store = GateStore()
    assert _latest_gates(store, "task-1")[("review-1", "quality")] is newer
    assert [g["verdict"] for g in load_task_dict(store, "task-1")["gates"]] == [
        "unavailable",
        "approved",
    ]


def test_new_second_stage_accepts_stored_legacy_first_stage_gates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    head = "abcdef0"
    store = Store(tmp_path)
    try:
        # Keep the legacy stage here to verify stored-gate compatibility.
        for dimension in ("quality", "logic"):
            gate_id = str(uuid.uuid4())
            store.write(
                "review_gate",
                "insert",
                gate_id,
                {
                    "id": gate_id,
                    "task_id": tid,
                    "stage": "grok-pr",
                    "dimension": dimension,
                    "vendor": "grok",
                    "verdict": "approved",
                    "evidence": None,
                    "head_sha": head,
                    "agent_id": str(uuid.uuid4()),
                    "recorded_at": utcnow(),
                },
            )
    finally:
        store.close()

    aid = _start_pr_reviewer(
        tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="codex"
    )
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-2",
            "--dimension",
            "quality",
            "--vendor",
            "codex",
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            aid,
        ],
    )

    assert "gate review-2/quality=approved" in capsys.readouterr().out


def test_gate_record_recorded_at_has_six_fractional_digits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    aid = _start_pr_reviewer(
        tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="grok"
    )
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "approved",
            "--head",
            "abcdef0",
            "--agent",
            aid,
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        gates = [
            row for row in store.rows("review_gate") if row.get("task_id") == tid
        ]
    finally:
        store.close()
    assert len(gates) == 1
    assert (
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
            gates[0]["recorded_at"],
        )
        is not None
    )


def test_pr_reviewer_unavailable_sets_gate_blocked_then_recovers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(tmp_path, ["task", "state", tid, "pr-review"])
    capsys.readouterr()
    agents = {
        ("quality", "grok"): _start_pr_reviewer(
            tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="grok"
        ),
        ("logic", "grok"): _start_pr_reviewer(
            tmp_path, capsys, tid, role="pr-reviewer-logic", vendor="grok"
        ),
    }
    grok_q = agents[("quality", "grok")]
    run(tmp_path, ["agent", "finish", "--id", grok_q, "--verdict", "unavailable"])
    capsys.readouterr()
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["state"] == "pr-review"
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "unavailable",
            "--head",
            "abcdef0",
            "--agent",
            grok_q,
            "--evidence",
            "vendor HTTP 402",
        ],
    )
    out = capsys.readouterr().out
    assert "gate review-1/quality=unavailable" in out
    assert "type=review.post" not in out
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["state"] == "gate-blocked"

    for (dim, vendor), aid in agents.items():
        if aid != grok_q:
            run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "approved"])
            capsys.readouterr()
    head = "abcdef0"
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            grok_q,
        ],
    )
    capsys.readouterr()
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["state"] == "gate-blocked"
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-1",
            "--dimension",
            "logic",
            "--vendor",
            "grok",
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            agents[("logic", "grok")],
        ],
    )
    capsys.readouterr()
    agents[("quality", "codex")] = _start_pr_reviewer(
        tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="codex"
    )
    agents[("logic", "codex")] = _start_pr_reviewer(
        tmp_path, capsys, tid, role="pr-reviewer-logic", vendor="codex"
    )
    for key in (("quality", "codex"), ("logic", "codex")):
        run(tmp_path, ["agent", "finish", "--id", agents[key], "--verdict", "approved"])
        capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-2",
            "--dimension",
            "quality",
            "--vendor",
            "codex",
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            agents[("quality", "codex")],
        ],
    )
    capsys.readouterr()
    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "review-2",
            "--dimension",
            "logic",
            "--vendor",
            "codex",
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            agents[("logic", "codex")],
        ],
    )
    capsys.readouterr()
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["state"] == "pr-review"


def test_gate_record_unavailable_without_evidence_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    aid = _start_pr_reviewer(tmp_path, capsys, tid, role="pr-reviewer-quality", vendor="grok")
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", "unavailable"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="--evidence is required when --verdict is unavailable"):
        run(
            tmp_path,
            [
                "gate",
                "record",
                "--task",
                tid,
                "--stage",
                "review-1",
                "--dimension",
                "quality",
                "--vendor",
                "grok",
                "--verdict",
                "unavailable",
                "--head",
                "abcdef0",
                "--agent",
                aid,
            ],
        )


def test_late_inner_close_from_pr_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "human",
            "--evidence",
            "five-part spec",
        ],
    )
    run(tmp_path, ["task", "state", tid, "pr-review"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "implementer_done",
            "--source",
            "script",
            "--evidence",
            "inner loop already finished",
        ],
    )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "reviewer_approved",
            "--source",
            "script",
            "--evidence",
            "inner loop already finished",
        ],
    )
    capsys.readouterr()
    run(tmp_path, ["task", "show", tid])
    assert json.loads(capsys.readouterr().out)["state"] == "pr-review"
    store = Store(tmp_path)
    try:
        items = {
            r["key"]: r["status"]
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid
        }
    finally:
        store.close()
    assert items["implementer_done"] == "ja"
    assert items["reviewer_approved"] == "ja"


def test_late_inner_close_from_implementing_without_agent_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "human",
            "--evidence",
            "five-part spec",
        ],
    )
    run(tmp_path, ["task", "state", tid, "implementing"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="no finished implementer agent"):
        run(
            tmp_path,
            [
                "close-step",
                "--task",
                tid,
                "--key",
                "implementer_done",
                "--source",
                "script",
                "--evidence",
                "no agent",
            ],
        )
    run(
        tmp_path,
        [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            "implementer_done",
            "--status",
            "ja",
            "--source",
            "script",
            "--evidence",
            "forced",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="no finished reviewer agent"):
        run(
            tmp_path,
            [
                "close-step",
                "--task",
                tid,
                "--key",
                "reviewer_approved",
                "--source",
                "script",
                "--evidence",
                "no agent",
            ],
        )


def test_first_slot_implementer_closes_implementer_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "human",
            "--evidence",
            "five-part spec",
        ],
    )
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "implementer_done",
            "--source",
            "script",
            "--evidence",
            "grok implementer finished",
        ],
    )
    assert "checklist implementer_done=ja" in capsys.readouterr().out


def test_first_slot_implementer_closes_conflicts_resolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys, workflow="resolve-conflicts")
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "conflicts_resolved",
            "--source",
            "script",
            "--evidence",
            "grok implementer finished",
        ],
    )
    assert "checklist conflicts_resolved=ja" in capsys.readouterr().out


def _codex_implementer_done(tmp_path: Path, capsys: pytest.CaptureFixture[str], *, unpin: bool) -> str:
    """Task whose only finished implementer row is codex, as develop could record it."""
    tid = _spine_session_task(tmp_path, capsys)
    for key, source, evidence in (
        ("session_registered", "script", "session register"),
        ("spec_written", "human", "five-part spec"),
    ):
        run(tmp_path, ["close-step", "--task", tid, "--key", key, "--source", source, "--evidence", evidence])
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        ["agent", "start", "--session", "s", "--task", tid, "--role", "implementer", "--vendor", "grok", "--round", "1"],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    store = Store(tmp_path)
    try:
        agent = {k: v for k, v in (store.row("agent", impl_id) or {}).items() if not k.startswith("_")}
        store.write("agent", "update", impl_id, {**agent, "vendor": "codex"})
        if unpin:
            task = {k: v for k, v in (store.row("task", tid) or {}).items() if not k.startswith("_")}
            task.pop("models", None)
            store.write("task", "update", tid, task)
    finally:
        store.close()
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()
    return tid


def test_pinned_task_does_not_close_implementer_done_with_a_codex_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _codex_implementer_done(tmp_path, capsys, unpin=False)
    with pytest.raises(SystemExit, match="no finished implementer agent"):
        run(
            tmp_path,
            ["close-step", "--task", tid, "--key", "implementer_done", "--source", "script", "--evidence", "codex row"],
        )


def test_task_without_models_pin_closes_implementer_done_with_a_codex_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _codex_implementer_done(tmp_path, capsys, unpin=True)
    run(
        tmp_path,
        ["close-step", "--task", tid, "--key", "implementer_done", "--source", "script", "--evidence", "codex row"],
    )
    assert "checklist implementer_done=ja" in capsys.readouterr().out


def test_implementer_done_n_a_still_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _spine_session_task(tmp_path, capsys)
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "human",
            "--evidence",
            "five-part spec",
        ],
    )
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="n_a is not allowed for implementer_done"):
        run(
            tmp_path,
            [
                "close-step",
                "--task",
                tid,
                "--key",
                "implementer_done",
                "--status",
                "n_a",
                "--source",
                "script",
                "--evidence",
                "no finished implementer",
            ],
        )


def test_review_second_send_validates_and_writes_session_mail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "reviewer", "--kind", "human"])
    capsys.readouterr()
    task_id = "11111111-1111-4111-8111-111111111111"
    head = "a" * 40
    result = {
        "kind": "a38.second-review/v1",
        "task_id": task_id,
        "head": head,
        "defects": 0,
        "blocks_merge": False,
        "findings": "",
        "lanes": [
            {"id": "conformity-b", "provider": "OpenAI", "model": "Reviewer Model 2", "model_number": "reviewer-model-2", "prompt": "Read the change carefully without modifying it.", "status": "complete", "result": "pass"},
            {"id": "logic-b", "provider": "OpenAI", "model": "Reviewer Model 2", "model_number": "reviewer-model-2", "prompt": "Read the change carefully without modifying it.", "status": "complete", "result": "pass"},
        ],
    }
    path = tmp_path / "second-review.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    run(
        tmp_path,
        ["review", "second", "send", "--to-session", "worker", "--task", task_id, "--head", head, "--file", str(path)],
    )
    activity_id = capsys.readouterr().out.strip()
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
        assert row["session_id"] == "reviewer"
        assert row["type"] == "message"
        assert row["execution_status"] == "pending"
        assert row["payload"] == {"to_session": "worker", "payload": result}
    finally:
        store.close()

    result["unknown"] = True
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="documented keys"):
        run(
            tmp_path,
            ["review", "second", "send", "--to-session", "worker", "--task", task_id, "--head", head, "--file", str(path)],
        )
