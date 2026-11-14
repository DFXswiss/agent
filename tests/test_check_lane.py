"""Single-purpose check lane and duplicate-check resolver coverage."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_cli import coordinator_checks
from agent_cli.coordinator_checks import (
    RESOLVERS,
    CheckContext,
    CheckValue,
    Deferred,
    evaluate_action,
    record_check,
    registry_row_id,
)
from agent_cli.coordinator_common import (
    CoordinatorError,
    control_dir,
    coord,
    save_task,
    strip_row,
)
from agent_cli.preconditions import CHECKS
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    make_order_worker,
    make_session,
    order_lane_runner,
    patch_account_runners,
    seed_order_task,
    write_accounts,
    write_check_lane,
)


ORDER_ID = "11111111-1111-4111-8111-111111111111"
FOREIGN = {
    "number": 5,
    "title": "Add a widget",
    "body": "Adds it",
    "head": {"ref": "feature"},
    "user": {"login": "someone"},
}


class RecordingGh(FakeGh):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> Completed:
        self.calls.append(list(argv))
        return super().__call__(argv)

    def listed(self) -> bool:
        return any(
            call and "/pulls?state=open" in call[-1] for call in self.calls
        )


def setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane_retries: int = 0,
    check_output: str | None = None,
    lanes=None,
    text: str = "Do it",
    bind: bool = True,
    **checkpoint,
):
    store = Store(tmp_path)
    write_accounts(store.home)
    if bind:
        write_check_lane(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_order_worker(tmp_path, lane_retries=lane_retries)
    fake = RecordingGh()
    fake.open_pulls = [dict(FOREIGN)]
    patch_account_runners(monkeypatch, fake)
    task = seed_order_task(
        store,
        worker,
        ORDER_ID,
        phase="start_draft",
        text=text,
        **checkpoint,
    )
    if lanes is None:
        lanes = (
            order_lane_runner(fake)
            if check_output is None
            else order_lane_runner(fake, check_output)
        )
    ctx = CheckContext(
        store,
        worker,
        task,
        fake,
        lanes,
        CHECKS["duplicates_checked"],
        "open_draft",
    )
    return store, worker, task, fake, ctx


def _resolve(ctx: CheckContext) -> CheckValue | Deferred:
    from agent_cli.coordinator_check_lane import resolve_duplicates_checked

    return resolve_duplicates_checked(ctx)


def _state(store: Store) -> dict[str, Any] | None:
    task = store.row("task", ORDER_ID)
    assert task is not None
    return coord(task).get("checks")


def _agents(store: Store) -> list[dict[str, Any]]:
    return [
        row for row in store.rows("agent") if row.get("task_id") == ORDER_ID
    ]


def _lane_runs(store: Store) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows("activity")
        if row.get("type") == "lane.run"
        and (row.get("payload") or {}).get("task_id") == ORDER_ID
    ]


@pytest.mark.no_pg
def test_every_declared_resolver_is_registered() -> None:
    assert RESOLVERS["duplicates_checked"].__module__ == coordinator_checks.__name__
    assert set(RESOLVERS) == {
        check.resolver for check in CHECKS.values() if check.resolver
    }


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "RESULT: duplicate 5\nREASON: Same goal.",
            ("duplicate", 5, "Same goal."),
        ),
        (
            "RESULT: duplicate #5\nREASON: Same goal.",
            ("duplicate", 5, "Same goal."),
        ),
        (
            "RESULT: no-duplicate\nREASON: Different goal.",
            ("no-duplicate", None, "Different goal."),
        ),
        (
            "RESULT: no-duplicate\r\nREASON: Different goal.\r\n",
            ("no-duplicate", None, "Different goal."),
        ),
        ("RESULT: no-duplicate", None),
        ("RESULT: no-duplicate\nREASON: x\nextra", None),
        ("RESULT: maybe\nREASON: x", None),
        ("RESULT: no-duplicate\nREASON:", None),
        (f"RESULT: no-duplicate\nREASON: {'x' * 301}", None),
        ("RESULT: duplicate 1234567890\nREASON: x", None),
        (" RESULT: no-duplicate\nREASON: x", None),
    ],
)
def test_parse_duplicate_result(text: str, expected) -> None:
    from agent_cli.coordinator_check_lane import parse_duplicate_result

    assert parse_duplicate_result(text) == expected


@pytest.mark.no_pg
def test_parsed_reason_is_redacted_and_one_line() -> None:
    from agent_cli.coordinator_check_lane import parse_duplicate_result

    parsed = parse_duplicate_result(
        "RESULT: no-duplicate\n"
        "REASON: See ghp_abcdefghijklmnopqrstuvwxyz0123456789 for it."
    )
    assert parsed is not None
    reason = parsed[2]
    assert "[redacted]" in reason
    assert "ghp_" not in reason
    assert "\n" not in reason


def test_zero_open_pull_requests_needs_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _task, fake, ctx = setup(tmp_path, monkeypatch)
    fake.open_pulls = []
    result = _resolve(ctx)
    assert result == CheckValue(
        "ja", "no open pull requests in example/project"
    )
    assert "check" not in fake.launched
    assert not _agents(store)
    assert _state(store) is None


def test_own_pull_request_is_not_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, task, fake, ctx = setup(tmp_path, monkeypatch)
    fake.open_pulls = [
        {
            **FOREIGN,
            "head": {"ref": coord(task)["branch"]},
            "user": {"login": "Worker-Bot"},
        }
    ]
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    assert "check" not in fake.launched
    assert not _agents(store)


def test_no_duplicate_records_ja(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _task, _fake, ctx = setup(tmp_path, monkeypatch)
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    assert result.evidence.startswith(
        "no duplicate among 1 open pull requests: "
    )
    agents = _agents(store)
    assert len(agents) == 1
    agent = agents[0]
    assert agent["role"] == "check"
    assert agent["check_key"] == "duplicates_checked"
    assert agent["status"] == "done"
    assert agent["round"] is None
    assert agent["model"] == "grok-model"
    runs = _lane_runs(store)
    assert len(runs) == 1
    assert runs[0]["payload"]["role"] == "check"
    assert runs[0]["payload"]["status"] == "complete"
    assert runs[0]["payload"]["agent_id"] == agent["id"]
    assert _state(store) is None


def test_duplicate_records_nein(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _worker, _task, _fake, ctx = setup(
        tmp_path,
        monkeypatch,
        check_output="RESULT: duplicate 5\nREASON: Same goal.\n",
    )
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "nein"
    assert (
        'open pull request #5 "Add a widget" looks like the same work: Same goal.'
        in result.evidence
    )
    assert "https://github.com/example/project/pull/5" in result.evidence


def test_number_outside_the_list_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _worker, _task, _fake, ctx = setup(
        tmp_path,
        monkeypatch,
        check_output="RESULT: duplicate 99\nREASON: Same goal.\n",
    )
    result = _resolve(ctx)
    assert result == CheckValue(
        "unavailable",
        "check lane failed 1 times: pull request #99 is not in the list",
    )


def test_invalid_output_is_retried_then_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, task, fake, ctx = setup(
        tmp_path,
        monkeypatch,
        lane_retries=1,
        check_output="I think so",
    )
    first = _resolve(ctx)
    assert first == Deferred(
        [
            "check duplicates_checked attempt 1 failed: "
            "output does not match the protocol; retrying"
        ]
    )
    assert _state(store)["duplicates_checked"]["attempts"] == 1
    calls = list(fake.calls)
    launched = list(fake.launched)
    second = _resolve(ctx)
    assert isinstance(second, Deferred)
    assert "retries in" in second.lines[0]
    assert fake.calls == calls
    assert fake.launched == launched
    coord(task)["checks"]["duplicates_checked"]["at"] = "2020-01-01T00:00:00Z"
    save_task(store, task)
    third = _resolve(ctx)
    assert third == CheckValue(
        "unavailable",
        "check lane failed 2 times: output does not match the protocol",
    )
    assert _state(store) is None
    assert [row["payload"]["status"] for row in _lane_runs(store)] == [
        "partial",
        "partial",
    ]


def test_unfinished_last_attempt_is_unavailable_without_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _task, fake, ctx = setup(
        tmp_path,
        monkeypatch,
        checks={
            "duplicates_checked": {
                "attempts": 1,
                "at": "2020-01-01T00:00:00Z",
                "detail": "lane did not finish",
            }
        },
    )
    result = _resolve(ctx)
    assert result == CheckValue(
        "unavailable", "check lane failed 1 times: lane did not finish"
    )
    assert "check" not in fake.launched
    assert not fake.listed()
    assert _state(store) is None


def test_interrupted_start_keeps_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_argv, _stdin=None):
        raise SystemExit(143)

    store, _worker, _task, _fake, ctx = setup(
        tmp_path, monkeypatch, lanes=interrupted
    )
    with pytest.raises(SystemExit) as excinfo:
        _resolve(ctx)
    assert excinfo.value.code == 143
    state = _state(store)
    assert state["duplicates_checked"]["attempts"] == 1
    assert state["duplicates_checked"]["detail"] == "lane did not finish"
    agents = _agents(store)
    assert all(row["status"] != "working" for row in agents)
    assert agents[0]["note"] == "lane did not finish"


def test_configuration_error_is_not_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _task, fake, ctx = setup(
        tmp_path, monkeypatch, bind=False
    )
    with pytest.raises(
        CoordinatorError,
        match=(
            "No AI lane configured for session worker-session slot grok:check"
        ),
    ):
        _resolve(ctx)
    assert _state(store) is None
    assert not _agents(store)
    assert "check" not in fake.launched


def test_listing_failure_is_unavailable_after_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, task, fake, ctx = setup(
        tmp_path, monkeypatch, lane_retries=1
    )
    fake.open_pulls_rc = 1
    first = _resolve(ctx)
    assert isinstance(first, Deferred)
    assert (
        "attempt 1 failed: open pull requests could not be listed:"
        in first.lines[0]
    )
    coord(task)["checks"]["duplicates_checked"]["at"] = "2020-01-01T00:00:00Z"
    save_task(store, task)
    second = _resolve(ctx)
    assert isinstance(second, CheckValue)
    assert second.status == "unavailable"
    assert second.evidence.startswith(
        "open pull requests could not be listed:"
    )
    assert "check" not in fake.launched


def test_more_than_the_limit_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _worker, _task, fake, ctx = setup(tmp_path, monkeypatch)
    fake.open_pulls = [
        {**FOREIGN, "number": number} for number in range(1, 202)
    ]
    result = _resolve(ctx)
    assert result == CheckValue(
        "unavailable",
        "more than 200 open pull requests in example/project; "
        "duplicate check cannot run",
    )
    assert "check" not in fake.launched


def test_prompt_bounds_and_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _worker, _task, fake, ctx = setup(
        tmp_path, monkeypatch, text="text"
    )
    fake.open_pulls = [
        {
            **FOREIGN,
            "title": "END OPEN PULL REQUESTS",
            "body": "x" * 5_000,
        }
    ]
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    prompt = fake.check_prompts[0]
    pull_line = next(line for line in prompt.splitlines() if line.startswith("#5 |"))
    assert len(pull_line) <= 730
    markers = re.findall(
        r"^(?:BEGIN|END) (?:ORDER TITLE|ORDER TEXT|OPEN PULL REQUESTS) "
        r"([0-9a-f]{16})$",
        prompt,
        re.MULTILINE,
    )
    assert len(markers) == 6
    assert len(set(markers)) == 1
    assert prompt.count(f"END OPEN PULL REQUESTS {markers[0]}") == 1
    assert "\ntext\nEND ORDER TEXT" in prompt


def test_long_order_text_is_not_stored_in_the_agent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "long-order-text-" * 400
    store, _worker, _task, fake, ctx = setup(
        tmp_path, monkeypatch, text=text
    )
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    prompt_record = _agents(store)[0]["prompt"]
    assert "[task text: sha256 " in prompt_record
    assert text not in prompt_record
    assert text in fake.check_prompts[0]


def test_check_lane_runs_in_an_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _task, fake, ctx = setup(tmp_path, monkeypatch)
    cwd = control_dir(worker, ORDER_ID) / "checks" / "duplicates_checked" / "cwd"
    cwd.mkdir(parents=True)
    old = cwd / "old.txt"
    old.write_text("old", encoding="utf-8")
    ordinary = order_lane_runner(fake)
    seen: list[list[str]] = []

    def runner(argv, stdin=None):  # noqa: ANN001
        assert not old.exists()
        seen.append(list(argv))
        return ordinary(argv, stdin)

    ctx = replace(ctx, lane_runner=runner)
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    argv = seen[0]
    assert argv[argv.index("--cwd") + 1] == str(cwd)
    assert argv[argv.index("--prompt-file") + 1] == str(
        control_dir(worker, ORDER_ID) / "check-duplicates_checked-grok.md"
    )
    assert store.row("task", ORDER_ID) is not None


def test_check_lane_refuses_a_foreign_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _task, _fake, ctx = setup(tmp_path, monkeypatch)
    target = tmp_path / "foreign"
    target.mkdir()
    cwd = control_dir(worker, ORDER_ID) / "checks" / "duplicates_checked" / "cwd"
    cwd.parent.mkdir(parents=True)
    cwd.symlink_to(target, target_is_directory=True)
    with pytest.raises(
        CoordinatorError, match="check lane path is not a managed directory"
    ):
        _resolve(ctx)
    assert target.exists()
    assert _state(store) is None


def test_check_lane_argv_carries_no_github_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.lane import LANE_GITHUB_TOKEN_ENV

    _store, _worker, _task, fake, ctx = setup(tmp_path, monkeypatch)
    ordinary = order_lane_runner(fake)
    profiles: list[Path] = []

    def runner(argv, stdin=None):  # noqa: ANN001
        assignments = [
            value for value in argv if value.startswith("GH_CONFIG_DIR=")
        ]
        assert len(assignments) == 1
        profile = Path(assignments[0].split("=", 1)[1])
        assert profile.is_dir()
        assert list(profile.iterdir()) == []
        profiles.append(profile)
        for key in LANE_GITHUB_TOKEN_ENV:
            assert argv[argv.index(key) - 1] == "-u"
        return ordinary(argv, stdin)

    result = _resolve(replace(ctx, lane_runner=runner))
    assert isinstance(result, CheckValue) and result.status == "ja"
    assert profiles and not profiles[0].exists()


def _configure_check_provider(
    store: Store, task: dict[str, Any], vendor: str
) -> str:
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if vendor == "grok":
        role = "rev"
    else:
        account = f"{vendor}-w"
        role = f"{vendor}-rev"
        data["accounts"][account] = {
            "provider": vendor,
            "config_dir": f"/test/{vendor}",
        }
        data["roles"][role] = {
            "account": account,
            "model": f"{vendor}-model",
            "access": "read-only",
        }
    data["models"]["first"] = vendor
    path.write_text(json.dumps(data), encoding="utf-8")
    write_check_lane(store.home, vendor, role)
    task["models"] = {"first": vendor, "second": "codex"}
    store.write("task", "update", ORDER_ID, strip_row(task))
    return role


@pytest.mark.parametrize("vendor", ["grok", "claude", "openrouter"])
def test_check_lane_final_text_per_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    store, _worker, task, fake, ctx = setup(
        tmp_path, monkeypatch, bind=False
    )
    _configure_check_provider(store, task, vendor)
    ordinary = order_lane_runner(fake)
    seen: list[list[str]] = []
    output_files: list[Path] = []

    def runner(argv, stdin=None):  # noqa: ANN001
        seen.append(list(argv))
        if "ANTHROPIC_BASE_URL=https://openrouter.ai/api" in argv:
            wrapper = next(
                i for i, value in enumerate(argv) if "openrouter-api-key" in value
            )
            output_files.append(Path(argv[wrapper + 2]))
        return ordinary(argv, stdin)

    result = _resolve(replace(ctx, lane_runner=runner))
    assert isinstance(result, CheckValue) and result.status == "ja"
    if vendor in ("claude", "openrouter"):
        assert seen[0][seen[0].index("--tools") + 1] == ""
    if vendor == "openrouter":
        assert output_files and not output_files[0].exists()


def test_unreadable_output_file_is_a_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, task, _fake, ctx = setup(
        tmp_path, monkeypatch, bind=False
    )
    _configure_check_provider(store, task, "openrouter")
    output_files: list[Path] = []

    def unreadable(argv, _stdin=None):  # noqa: ANN001
        wrapper = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        output = Path(argv[wrapper + 2])
        output_files.append(output)
        output.write_bytes(b"\xff")
        return Completed(0, "", "")

    result = _resolve(replace(ctx, lane_runner=unreadable))
    assert result == CheckValue(
        "unavailable",
        "check lane failed 1 times: lane output file is not valid UTF-8",
    )
    assert _lane_runs(store)[0]["payload"]["status"] == "unavailable"
    assert output_files and not output_files[0].exists()


def test_output_file_failure_is_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, task, _fake, ctx = setup(
        tmp_path, monkeypatch, bind=False
    )
    _configure_check_provider(store, task, "openrouter")

    def no_space(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(
        "agent_cli.coordinator_check_lane.tempfile.mkstemp", no_space
    )
    with pytest.raises(
        CoordinatorError, match="cannot create a check lane output file"
    ):
        _resolve(ctx)
    assert _state(store) is None
    assert not _agents(store)


@pytest.mark.parametrize(
    ("returncode", "lane_status"), [(2, "unavailable"), (124, "timeout")]
)
def test_nonzero_exit_is_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    lane_status: str,
) -> None:
    store, _worker, _task, _fake, ctx = setup(
        tmp_path,
        monkeypatch,
        lanes=lambda _argv, _stdin=None: Completed(returncode, "ignored", ""),
    )
    result = _resolve(ctx)
    assert result == CheckValue(
        "unavailable", f"check lane failed 1 times: exit {returncode}"
    )
    assert _lane_runs(store)[0]["payload"]["status"] == lane_status


def test_raising_runner_leaves_no_working_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising(_argv, _stdin=None):
        raise RuntimeError("transport")

    store, _worker, _task, _fake, ctx = setup(
        tmp_path, monkeypatch, lanes=raising
    )
    result = _resolve(ctx)
    assert result == CheckValue(
        "unavailable", "check lane failed 1 times: transport"
    )
    assert all(row["status"] != "working" for row in _agents(store))


def test_stored_lane_text_hides_the_account_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _task, _fake, ctx = setup(
        tmp_path,
        monkeypatch,
        check_output=(
            "RESULT: no-duplicate\n"
            "REASON: cannot read /test/grok/settings.json.\n"
        ),
    )
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    assert "<config_dir>/settings.json" in result.evidence
    assert "/test/grok" not in result.evidence
    record_check(
        store,
        ctx.task,
        CHECKS["duplicates_checked"],
        result,
        action="open_draft",
    )
    evidence = store.row(
        "checklist_item", registry_row_id(ORDER_ID, "duplicates_checked")
    )["evidence"]
    note = _agents(store)[0]["note"]
    final_text = _lane_runs(store)[0]["payload"]["final_text"]
    for value in (evidence, note, final_text):
        assert "<config_dir>/settings.json" in value
        assert "/test/grok" not in value


def test_check_lane_closes_its_own_stale_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _task, _fake, ctx = setup(tmp_path, monkeypatch)
    for aid, key in (
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "duplicates_checked"),
        ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "other"),
    ):
        store.write(
            "agent",
            "insert",
            aid,
            {
                "id": aid,
                "session_id": "worker-session",
                "task_id": ORDER_ID,
                "round": None,
                "role": "check",
                "check_key": key,
                "vendor": "grok",
                "provider": "grok",
                "model": "grok-model",
                "prompt": "old",
                "status": "working",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None,
                "note": None,
            },
        )
    result = _resolve(ctx)
    assert isinstance(result, CheckValue) and result.status == "ja"
    same = store.row("agent", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    other = store.row("agent", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert same["status"] == "done"
    assert same["note"] == "recovered: check lane restarted"
    assert other["status"] == "working"


def _verified_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    check_output: str | None = None,
):
    store, worker, task, fake, ctx = setup(
        tmp_path, monkeypatch, check_output=check_output
    )
    worktree = worker.workspace_root / ORDER_ID
    worktree.mkdir()
    (worktree / ".git").mkdir()
    c = coord(task)
    c["worktree"] = str(worktree)
    c["base_sha"] = fake.base
    save_task(store, task)
    branch = f"task-{ORDER_ID[:8]}"
    fake.remotes = {
        "origin": "https://github.com/example/project.git",
        "publication": "https://github.com/example/project.git",
    }
    fake.branch = branch
    fake.branches.add(branch)
    from agent_cli.coordinator_git import _write_marker

    _write_marker(
        worker,
        ORDER_ID,
        {
            "phase": "ready",
            "task_id": ORDER_ID,
            "session_id": worker.session_id,
            "target_repo": "example/project",
            "publication_repo": "example/project",
            "branch": branch,
            "base": "develop",
            "base_sha": fake.base,
            "worktree": str(worktree),
            "worker_login": "worker-bot",
        },
    )
    return store, worker, task, fake, ctx


def test_duplicate_raises_the_override_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, fake, ctx = _verified_order(
        tmp_path,
        monkeypatch,
        check_output="RESULT: duplicate 5\nREASON: Same goal.\n",
    )
    lines = evaluate_action(
        store,
        worker,
        task,
        fake,
        ctx.lane_runner,
        action="open_draft",
    )
    assert lines == ["check duplicates_checked=nein; question raised"]
    saved = store.row("task", ORDER_ID)
    c = coord(saved)
    assert c["phase"] == "ask"
    assert c["resume_phase"] == "start_draft"
    assert c["failed_check"] == "duplicates_checked"
    assert (
        f"agent order override --id {ORDER_ID} --check duplicates_checked "
        "--reason TEXT"
        in c["question_text"]
    )
    assert (
        f"agent order cancel --id {ORDER_ID} --reason TEXT"
        in c["question_text"]
    )
    assert not [
        row
        for row in store.rows("activity")
        if row.get("type") in ("comment.post", "pr.open")
    ]
    assert not any("commit" in command for command in fake.git_commands)
    assert fake.pr_created is False
    registry = store.row(
        "checklist_item", registry_row_id(ORDER_ID, "duplicates_checked")
    )
    assert registry["status"] == "nein"


def test_stored_duplicate_is_not_resolved_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, fake, ctx = _verified_order(
        tmp_path,
        monkeypatch,
        check_output="RESULT: duplicate 5\nREASON: Same goal.\n",
    )
    evaluate_action(
        store,
        worker,
        task,
        fake,
        ctx.lane_runner,
        action="open_draft",
    )
    listings = sum(
        1 for call in fake.calls if call and "/pulls?state=open" in call[-1]
    )
    saved = store.row("task", ORDER_ID)
    coord(saved)["phase"] = "start_draft"
    save_task(store, saved)
    lines = evaluate_action(
        store,
        worker,
        saved,
        fake,
        ctx.lane_runner,
        action="open_draft",
    )
    assert lines == ["check duplicates_checked=nein; question raised"]
    assert fake.launched.count("check") == 1
    assert sum(
        1 for call in fake.calls if call and "/pulls?state=open" in call[-1]
    ) == listings


def test_unavailable_question_offers_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, fake, ctx = _verified_order(tmp_path, monkeypatch)
    fake.open_pulls = [
        {**FOREIGN, "number": number} for number in range(1, 202)
    ]
    lines = evaluate_action(
        store,
        worker,
        task,
        fake,
        ctx.lane_runner,
        action="open_draft",
    )
    assert lines == [
        "check duplicates_checked=unavailable; question raised"
    ]
    saved = store.row("task", ORDER_ID)
    text = coord(saved)["question_text"]
    assert text.startswith(
        'Check duplicates_checked could not run for order "Add a widget": '
    )
    assert f"agent order reply --id {ORDER_ID} --body-file PATH" in text
    assert (
        f"agent order override --id {ORDER_ID} --check duplicates_checked "
        "--reason TEXT"
        in text
    )


def test_a_waived_check_starts_no_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task, fake, ctx = _verified_order(tmp_path, monkeypatch)
    record_check(
        store,
        task,
        CHECKS["duplicates_checked"],
        CheckValue(
            "ja",
            "skipped at submit by human-owner: none exists",
            source="human",
        ),
        action="open_draft",
        skip={
            "by": "human-owner",
            "reason": "none exists",
            "at": "2026-01-01T00:00:00Z",
        },
    )
    result = evaluate_action(
        store,
        worker,
        task,
        fake,
        ctx.lane_runner,
        action="open_draft",
    )
    assert result is None
    assert "check" not in fake.launched
    assert not fake.listed()
