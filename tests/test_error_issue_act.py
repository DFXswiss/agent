from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.error_issue_act import (
    ISSUE_LABEL,
    MAX_ISSUE_BODY,
    STORM_MARKER,
    _create_issue,
    _create_storm_issue,
    _recently_touched,
    extract_variant,
    find_issue_number,
    marker_for,
    parse_variants_section,
    render_variants_section,
    scan_error_issue,
    splice_variants,
    touch_history,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError, utcnow


def _runner_session(store: Store) -> None:
    store.write(
        "session",
        "insert",
        "runner-1",
        {
            "id": "runner-1",
            "kind": "runner",
            "status": "active",
            "skills": ["spine", "error-fix"],
        },
    )


def _seen(
    store: Store,
    *,
    template_fingerprint: str | None = "api|error|abc123|prod",
    excerpt: str = "Timeout updating balances for Ethereum: Error: Timeout",
    service: str = "api",
    cls: str = "error",
    activity_id: str = "error-seen-1",
) -> None:
    payload: dict[str, object] = {
        "fingerprint": "api|error|def456|prod",
        "excerpt": excerpt,
        "service": service,
        "class": cls,
    }
    if template_fingerprint is not None:
        payload["template_fingerprint"] = template_fingerprint
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": "runner-1",
            "type": "error.seen",
            "payload": payload,
            "execution_status": "done",
        },
    )


def _issue(store: Store, error_id: str = "error-seen-1", activity_id: str = "issue-1") -> None:
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": "runner-1",
            "type": "error.issue",
            "payload": {"error_id": error_id},
            "execution_status": "pending",
        },
    )


# ---- pure helpers ----


def test_extract_variant_finds_known_chain_or_falls_back_to_generic() -> None:
    assert extract_variant("Timeout updating balances for Ethereum") == "Ethereum"
    assert extract_variant("Failed to check Bank Frick order status") == "Frick"
    assert extract_variant("Failed to get price for token tether -> usd") == "generic"


def test_extract_variant_combines_chain_and_asset() -> None:
    assert extract_variant("Balance for Arbitrum/USDC went low") == "Arbitrum/USDC"
    assert extract_variant("Balance for Base/WBTC went low") == "Base/WBTC"


def test_marker_for_embeds_template_fingerprint() -> None:
    marker = marker_for("api|error|abc123|prod")
    assert marker == "<!-- error-log-template:api|error|abc123|prod -->"


def test_variants_section_round_trips() -> None:
    variants = {
        "Ethereum": {"first_seen": "2026-08-31T10:00:00Z", "last_seen": "2026-08-31T10:00:00Z"},
        "Polygon": {"first_seen": "2026-08-31T11:00:00Z", "last_seen": "2026-08-31T11:30:00Z"},
    }
    section = render_variants_section(variants)
    assert "Ethereum" in section
    assert "Polygon" in section
    parsed = parse_variants_section(section)
    assert parsed == variants


def test_splice_variants_only_touches_delimited_section() -> None:
    body = "Human-written context above.\n\nMore human notes.\n"
    with_section = splice_variants(body, {"Ethereum": {"first_seen": "t1", "last_seen": "t1"}})
    assert "Human-written context above." in with_section
    assert "More human notes." in with_section
    assert "Ethereum" in with_section

    updated = splice_variants(
        with_section,
        {
            "Ethereum": {"first_seen": "t1", "last_seen": "t2"},
            "Polygon": {"first_seen": "t2", "last_seen": "t2"},
        },
    )
    assert "Human-written context above." in updated
    assert "More human notes." in updated
    assert "Polygon" in updated
    # Splicing again must not duplicate the human-written prose above the section.
    assert updated.count("Human-written context above.") == 1


def test_find_issue_number_none_when_empty(tmp_path: Path) -> None:
    def runner(argv: list[str]) -> Completed:
        assert argv[:4] == ["gh", "issue", "list", "--repo"]
        return Completed(0, "[]", "")

    assert find_issue_number(runner, "org/tracker", "api|error|abc|prod") is None


def test_find_issue_number_parses_first_match() -> None:
    def runner(argv: list[str]) -> Completed:
        return Completed(0, '[{"number": 42}, {"number": 43}]', "")

    assert find_issue_number(runner, "org/tracker", "api|error|abc|prod") == 42


def test_find_issue_number_raises_on_gh_failure() -> None:
    def runner(argv: list[str]) -> Completed:
        return Completed(1, "", "not found")

    with pytest.raises(StoreError, match="not found"):
        find_issue_number(runner, "org/tracker", "api|error|abc|prod")


# ---- scan_error_issue: dry run ----


def test_scan_dry_run_never_calls_gh(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=True)
    assert calls == []
    assert lines == ["error.issue issue-1 dry-run variant=Ethereum"]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["mode"] == "dry-run"
    assert row["result"]["variant"] == "Ethereum"
    assert row["result"]["template_fingerprint"] == "api|error|abc123|prod"


def test_scan_dry_run_is_a_noop_on_rerun(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=True)
    assert scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=True) == []
    assert calls == []


# ---- scan_error_issue: create path ----


def test_scan_creates_issue_when_none_exists(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(0, "https://github.com/org/tracker/issues/7\n", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == ["error.issue issue-1 created variant=Ethereum"]
    create_call = next(c for c in calls if c[:3] == ["gh", "issue", "create"])
    assert "--repo" in create_call and "org/tracker" in create_call
    assert "--label" in create_call and ISSUE_LABEL in create_call
    body = create_call[create_call.index("--body") + 1]
    assert marker_for("api|error|abc123|prod") in body
    assert "Ethereum" in body
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["created"] is True
    assert row["result"]["url"] == "https://github.com/org/tracker/issues/7"


# ---- scan_error_issue: update path ----


def test_scan_updates_existing_issue_same_variant_no_comment(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    existing_body = (
        marker_for("api|error|abc123|prod")
        + "\n\nAutomated error-log finding.\n\n"
        + render_variants_section({"Ethereum": {"first_seen": "t0", "last_seen": "t0"}})
        + "\n"
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, '[{"number": 9}]', "")
        if argv[:3] == ["gh", "issue", "view"]:
            import json

            return Completed(0, json.dumps({"body": existing_body}), "")
        if argv[:3] == ["gh", "issue", "edit"]:
            return Completed(0, "", "")
        if argv[:3] == ["gh", "issue", "comment"]:
            raise AssertionError("must not comment when the variant already existed")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == [
        "error.issue issue-1 updated number=9 variant=Ethereum new_variant=False"
    ]
    edit_call = next(c for c in calls if c[:3] == ["gh", "issue", "edit"])
    body = edit_call[edit_call.index("--body") + 1]
    assert "Ethereum" in body
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["result"]["new_variant"] is False


def test_scan_updates_existing_issue_new_variant_posts_comment(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(
        store,
        template_fingerprint="api|error|abc123|prod",
        excerpt="Timeout updating balances for Polygon: Error: Timeout",
    )
    _issue(store)
    existing_body = (
        marker_for("api|error|abc123|prod")
        + "\n\nAutomated error-log finding.\n\n"
        + render_variants_section({"Ethereum": {"first_seen": "t0", "last_seen": "t0"}})
        + "\n"
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, '[{"number": 9}]', "")
        if argv[:3] == ["gh", "issue", "view"]:
            import json

            return Completed(0, json.dumps({"body": existing_body}), "")
        if argv[:3] in (["gh", "issue", "edit"], ["gh", "issue", "comment"]):
            return Completed(0, "", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == [
        "error.issue issue-1 updated number=9 variant=Polygon new_variant=True"
    ]
    edit_call = next(c for c in calls if c[:3] == ["gh", "issue", "edit"])
    body = edit_call[edit_call.index("--body") + 1]
    assert "Ethereum" in body
    assert "Polygon" in body
    comment_call = next(c for c in calls if c[:3] == ["gh", "issue", "comment"])
    comment_body = comment_call[comment_call.index("--body") + 1]
    assert "Polygon" in comment_body


# ---- error handling ----


def test_scan_marks_error_when_template_fingerprint_missing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, template_fingerprint=None)
    _issue(store)

    lines = scan_error_issue(
        store, lambda _argv: Completed(0, "[]", ""), issue_repo="org/tracker", dry_run=False
    )
    assert lines == ["error.issue issue-1 error"]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert row["execution_error"] == "template_fingerprint is required"


def test_scan_marks_error_when_create_fails(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(1, "", "permission denied")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == ["error.issue issue-1 error"]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert row["execution_error"] == "permission denied"


def test_scan_leaves_non_pending_rows_alone(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    scan_error_issue(
        store, lambda _argv: Completed(0, "[]", ""), issue_repo="org/tracker", dry_run=True
    )
    calls: list[list[str]] = []

    def fail_if_called(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    assert scan_error_issue(store, fail_if_called, issue_repo="org/tracker", dry_run=False) == []
    assert calls == []


def _prior_touch(
    store: Store,
    *,
    template_fingerprint: str,
    at: str,
    activity_id: str,
    skipped: bool = False,
) -> None:
    result: dict[str, object] = {
        "issue_repo": "org/tracker",
        "template_fingerprint": template_fingerprint,
        "at": at,
    }
    if skipped:
        result["skipped"] = "cooldown"
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": "runner-1",
            "type": "error.issue",
            "payload": {"error_id": "irrelevant"},
            "execution_status": "done",
            "result": result,
        },
    )


def _seen_and_issue(
    store: Store, *, index: int, template_fingerprint: str, service: str = "api", cls: str = "error"
) -> None:
    seen_id = f"seen-{index}"
    issue_id = f"storm-issue-{index}"
    store.write(
        "activity",
        "insert",
        seen_id,
        {
            "id": seen_id,
            "session_id": "runner-1",
            "type": "error.seen",
            "payload": {
                "fingerprint": f"fp-{index}",
                "template_fingerprint": template_fingerprint,
                "excerpt": f"Some error number {index}",
                "service": service,
                "class": cls,
            },
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        issue_id,
        {
            "id": issue_id,
            "session_id": "runner-1",
            "type": "error.issue",
            "payload": {"error_id": seen_id},
            "execution_status": "pending",
        },
    )


# ---- cooldown ----


def test_recently_touched_false_with_no_history(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    assert _recently_touched(store, "api|error|abc|prod", utcnow(), 60) is False


def test_recently_touched_true_within_window(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-1",
    )
    assert _recently_touched(store, "api|error|abc|prod", "2026-08-31T10:30:00Z", 60) is True


def test_recently_touched_false_after_expiry(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-1",
    )
    assert _recently_touched(store, "api|error|abc|prod", "2026-08-31T11:30:00Z", 60) is False


def test_recently_touched_ignores_skipped_results(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:29:00Z",
        activity_id="prior-1",
        skipped=True,
    )
    # A skip-only history must not itself extend the cooldown window.
    assert _recently_touched(store, "api|error|abc|prod", "2026-08-31T10:30:00Z", 60) is False


def test_scan_skips_recently_touched_template_without_gh_calls(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    _prior_touch(
        store, template_fingerprint="api|error|abc123|prod", at=utcnow(), activity_id="prior-1"
    )
    calls: list[list[str]] = []

    def fail_if_called(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_error_issue(
        store, fail_if_called, issue_repo="org/tracker", dry_run=False, cooldown_minutes=60
    )
    assert lines == ["error.issue issue-1 skipped-cooldown"]
    assert calls == []
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["skipped"] == "cooldown"


def test_scan_processes_normally_after_cooldown_expires(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc123|prod",
        at="2020-01-01T00:00:00Z",
        activity_id="prior-1",
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(0, "https://github.com/org/tracker/issues/1\n", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=60
    )
    assert lines == ["error.issue issue-1 created variant=Ethereum"]


# ---- burst / storm detection ----


def test_scan_does_not_storm_at_or_below_threshold(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(2):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(0, "https://github.com/org/tracker/issues/1\n", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2)
    assert len(lines) == 2
    assert all("created" in line for line in lines)
    create_calls = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 2  # two separate issues, not folded


def test_scan_folds_burst_into_one_storm_issue(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(0, "https://github.com/org/tracker/issues/99\n", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2)
    assert len(lines) == 3
    assert all("storm" in line for line in lines)
    create_calls = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 1
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert STORM_MARKER in body
    for i in range(3):
        row = store.row("activity", f"storm-issue-{i}")
        assert row is not None
        assert row["execution_status"] == "done"
        assert row["result"]["mode"] == "storm"


def test_scan_storm_dry_run_never_calls_gh(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=True, storm_threshold=2)
    assert calls == []
    assert len(lines) == 3
    assert all("storm-dry-run" in line for line in lines)


def test_scan_storm_reuses_existing_open_storm_issue(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")
    existing_body = (
        STORM_MARKER
        + "\n\n"
        + render_variants_section({"api: error (oldhash)": {"first_seen": "t0", "last_seen": "t0"}})
        + "\n"
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, '[{"number": 55}]', "")
        if argv[:3] == ["gh", "issue", "view"]:
            import json

            return Completed(0, json.dumps({"body": existing_body}), "")
        if argv[:3] == ["gh", "issue", "edit"]:
            return Completed(0, "", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2)
    assert len(lines) == 3
    assert [c for c in calls if c[:3] == ["gh", "issue", "create"]] == []
    edit_calls = [c for c in calls if c[:3] == ["gh", "issue", "edit"]]
    assert len(edit_calls) == 1
    body = edit_calls[0][edit_calls[0].index("--body") + 1]
    assert "api: error (oldhash)" in body
    assert body.count(STORM_MARKER) == 1


def test_scan_storm_marks_all_rows_error_on_create_failure(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(1, "", "permission denied")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2)
    assert len(lines) == 3
    assert all(line.endswith("error") for line in lines)
    for i in range(3):
        row = store.row("activity", f"storm-issue-{i}")
        assert row is not None
        assert row["execution_status"] == "error"


# ---- one template, several pending rows in one scan ----


def test_scan_merges_two_variants_of_one_template_into_one_issue(tmp_path: Path) -> None:
    """Two variants of the same template in one scan belong in one issue. Handled
    row by row, the first would become a cooldown touch for the second and that
    variant would be dropped."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(
        store,
        template_fingerprint="api|error|same|prod",
        excerpt="Balance for Arbitrum/USDC went low",
        activity_id="seen-a",
    )
    _seen(
        store,
        template_fingerprint="api|error|same|prod",
        excerpt="Balance for Arbitrum/WBTC went low",
        activity_id="seen-b",
    )
    _issue(store, error_id="seen-a", activity_id="issue-a")
    _issue(store, error_id="seen-b", activity_id="issue-b")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(0, "https://github.com/org/tracker/issues/7\n", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=60
    )
    assert lines == [
        "error.issue issue-a created variant=Arbitrum/USDC",
        "error.issue issue-b created variant=Arbitrum/WBTC",
    ]
    create_calls = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 1
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert "Arbitrum/USDC" in body
    assert "Arbitrum/WBTC" in body


def test_scan_comments_once_for_several_new_variants(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(
        store,
        template_fingerprint="api|error|same|prod",
        excerpt="Balance for Arbitrum/USDC went low",
        activity_id="seen-a",
    )
    _seen(
        store,
        template_fingerprint="api|error|same|prod",
        excerpt="Balance for Base/WBTC went low",
        activity_id="seen-b",
    )
    _issue(store, error_id="seen-a", activity_id="issue-a")
    _issue(store, error_id="seen-b", activity_id="issue-b")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, '[{"number": 12}]', "")
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(0, '{"body": "text\\n"}', "")
        return Completed(0, "", "")

    calls: list[list[str]] = []

    def recording(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return runner(argv)

    scan_error_issue(store, recording, issue_repo="org/tracker", dry_run=False)
    comments = [c for c in calls if c[:3] == ["gh", "issue", "comment"]]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert "Arbitrum/USDC" in body
    assert "Base/WBTC" in body


def test_scan_keeps_the_edit_when_the_comment_fails(tmp_path: Path) -> None:
    """The edit is the durable record. Failing the row on a comment error would
    strand a variant that is already in the table and can never be re-announced,
    because a retry no longer sees it as new."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, '[{"number": 12}]', "")
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(0, '{"body": "text\\n"}', "")
        if argv[:3] == ["gh", "issue", "comment"]:
            return Completed(1, "", "rate limited")
        return Completed(0, "", "")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == [
        "error.issue issue-1 updated number=12 variant=Ethereum new_variant=True comment-failed"
    ]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["comment_error"] == "rate limited"


# ---- dry run must not open a cooldown window ----


def test_dry_run_does_not_start_a_cooldown_window(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    assert scan_error_issue(
        store, lambda _argv: Completed(0, "[]", ""), issue_repo="org/tracker", dry_run=True
    ) == ["error.issue issue-1 dry-run variant=Ethereum"]

    _issue(store, activity_id="issue-2")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/1\n", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=60
    )
    assert lines == ["error.issue issue-2 created variant=Ethereum"]
    assert calls != []


# ---- burst detection counts only templates never filed before ----


def test_storm_threshold_ignores_already_tracked_templates(tmp_path: Path) -> None:
    """A backlog of known templates draining after downtime is a volume spike,
    not a burst of new problems, and must keep updating its own issues."""
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")
    for i in range(3):
        _prior_touch(
            store,
            template_fingerprint=f"api|error|t{i}|prod",
            at="2026-08-30T10:00:00Z",
            activity_id=f"prior-{i}",
        )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        if argv[:3] == ["gh", "issue", "create"]:
            return Completed(0, "https://github.com/org/tracker/issues/1\n", "")
        raise AssertionError(f"unexpected call: {argv}")

    lines = scan_error_issue(
        store,
        runner,
        issue_repo="org/tracker",
        dry_run=False,
        storm_threshold=2,
        cooldown_minutes=0,
    )
    assert len(lines) == 3
    assert all(line.endswith("created variant=generic") for line in lines)
    for i in range(3):
        row = store.row("activity", f"storm-issue-{i}")
        assert row is not None
        assert row["result"].get("mode") != "storm"


# ---- damaged variants section ----


def test_splice_variants_refuses_a_half_open_section() -> None:
    damaged = "Human notes.\n\n<!-- variants:start -->\n\n| variant | first seen | last seen |\n"
    with pytest.raises(StoreError):
        splice_variants(damaged, {"Ethereum": {"first_seen": "t1", "last_seen": "t1"}})


def test_splice_variants_refuses_a_body_over_the_github_limit() -> None:
    variants = {f"chain-{i}": {"first_seen": "t1", "last_seen": "t1"} for i in range(6000)}
    with pytest.raises(StoreError):
        splice_variants("Human notes.\n", variants)


def test_create_paths_apply_the_same_body_ceiling() -> None:
    """The ceiling is a property of every body sent to gh, not just of a splice
    into an existing issue."""
    huge = "x" * (MAX_ISSUE_BODY + 1)
    with pytest.raises(StoreError):
        _create_issue(
            _unreachable_runner,
            issue_repo="org/tracker",
            title="api: error",
            template_fingerprint="api|error|abc|prod",
            excerpt=huge,
            variants=["generic"],
            now="2026-08-31T10:00:00Z",
        )
    templates = {f"t-{i}": {"first_seen": "t1", "last_seen": "t1"} for i in range(6000)}
    with pytest.raises(StoreError):
        _create_storm_issue(
            _unreachable_runner,
            issue_repo="org/tracker",
            templates=templates,
            now="2026-08-31T10:00:00Z",
        )


def _unreachable_runner(argv: list[str]) -> Completed:
    raise AssertionError(f"gh must not be called: {argv}")


# ---- touch history ----


def test_touch_history_keeps_the_newest_touch_per_template(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-old",
    )
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T12:00:00Z",
        activity_id="prior-new",
    )
    history = touch_history(store)
    assert history["api|error|abc|prod"].isoformat() == "2026-08-31T12:00:00+00:00"


def test_touch_history_ignores_unusable_rows(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for activity_id, result in (
        ("bad-1", "not-a-dict"),
        ("bad-2", {"template_fingerprint": "api|error|abc|prod"}),  # no "at"
        ("bad-3", {"template_fingerprint": "api|error|abc|prod", "at": "not-a-date"}),
        ("bad-4", {"at": "2026-08-31T10:00:00Z"}),  # no template_fingerprint
    ):
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "runner-1",
                "type": "error.issue",
                "payload": {"error_id": "irrelevant"},
                "execution_status": "done",
                "result": result,
            },
        )
    assert touch_history(store) == {}
