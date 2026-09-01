from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.error_issue_act import (
    ISSUE_LABEL,
    MAX_ISSUE_BODY,
    MAX_TRACKED_VARIANTS,
    STORM_MARKER,
    _burst_folded_templates,
    _VARIANTS_END,
    _VARIANTS_START,
    _storm_label,
    _create_issue,
    _create_storm_issue,
    _within_cooldown,
    _extract_variant,
    _find_issue_number,
    _marker_for,
    _parse_variants_section,
    _render_variants_section,
    scan_error_issue,
    _splice_variants,
    _touch_history,
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
    environment: str = "prod",
    activity_id: str = "error-seen-1",
) -> None:
    payload: dict[str, object] = {
        "fingerprint": "api|error|def456|prod",
        "excerpt": excerpt,
        "service": service,
        "class": cls,
        "environment": environment,
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
    assert _extract_variant("Timeout updating balances for Ethereum") == "Ethereum"
    assert _extract_variant("Failed to check Bank Frick order status") == "Frick"
    assert _extract_variant("Failed to get price for token tether -> usd") == "generic"


def test_extract_variant_combines_chain_and_asset() -> None:
    assert _extract_variant("Balance for Arbitrum/USDC went low") == "Arbitrum/USDC"
    assert _extract_variant("Balance for Base/WBTC went low") == "Base/WBTC"


def test_marker_is_a_digest_and_never_carries_raw_text() -> None:
    """service, class and environment are free text from the log source. A raw
    fingerprint in the marker could close the HTML comment early or embed the
    section markers, corrupting the body and losing the marker the next lookup
    needs."""
    marker = _marker_for("api|error|abc123|prod")
    assert marker.startswith("<!-- error-log-template:")
    assert marker.endswith(" -->")
    assert _marker_for("api|error|abc123|prod") == marker
    assert _marker_for("api|error|abc123|staging") != marker

    hostile = _marker_for(f"api --> {_VARIANTS_START}|error|abc|prod")
    assert hostile.count("-->") == 1
    assert _VARIANTS_START not in hostile


def test_variants_section_round_trips() -> None:
    variants = {
        "Ethereum": {"first_seen": "2026-08-31T10:00:00Z", "last_seen": "2026-08-31T10:00:00Z"},
        "Polygon": {"first_seen": "2026-08-31T11:00:00Z", "last_seen": "2026-08-31T11:30:00Z"},
    }
    section = _render_variants_section(variants)
    assert "Ethereum" in section
    assert "Polygon" in section
    parsed = _parse_variants_section(section)
    assert parsed == variants


def test_splice_variants_only_touches_delimited_section() -> None:
    body = "Human-written context above.\n\nMore human notes.\n"
    with_section = _splice_variants(body, {"Ethereum": {"first_seen": "t1", "last_seen": "t1"}})
    assert "Human-written context above." in with_section
    assert "More human notes." in with_section
    assert "Ethereum" in with_section

    updated = _splice_variants(
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


def test_find_issue_number_none_when_empty() -> None:
    def runner(argv: list[str]) -> Completed:
        assert argv[:4] == ["gh", "issue", "list", "--repo"]
        return Completed(0, "[]", "")

    assert _find_issue_number(runner, "org/tracker", "api|error|abc|prod") is None


def test_find_issue_number_matches_the_marker_in_the_body() -> None:
    def runner(argv: list[str]) -> Completed:
        assert "--search" not in argv
        return Completed(
            0,
            json.dumps(
                [
                    {"number": 42, "body": "another template <!-- error-log-template:x -->"},
                    {"number": 43, "body": "carries api|error|abc|prod here"},
                ]
            ),
            "",
        )

    assert _find_issue_number(runner, "org/tracker", "api|error|abc|prod") == 43


def test_find_issue_number_ignores_issues_without_the_marker() -> None:
    """A labeled issue that search might surface but that does not carry this
    template's marker must not be adopted as its issue."""
    def runner(argv: list[str]) -> Completed:
        return Completed(0, json.dumps([{"number": 42, "body": "unrelated"}]), "")

    assert _find_issue_number(runner, "org/tracker", "api|error|abc|prod") is None


def test_find_issue_number_raises_when_the_list_is_truncated() -> None:
    """A full page may hide the marker on an unseen issue. Reporting "none" there
    would file a duplicate, so this fails loud instead."""
    def runner(argv: list[str]) -> Completed:
        return Completed(
            0,
            json.dumps([{"number": n, "body": "unrelated"} for n in range(100)]),
            "",
        )

    with pytest.raises(StoreError, match="issue list truncated"):
        _find_issue_number(runner, "org/tracker", "api|error|abc|prod")


def test_find_issue_number_returns_none_on_a_partial_page() -> None:
    def runner(argv: list[str]) -> Completed:
        return Completed(
            0,
            json.dumps([{"number": n, "body": "unrelated"} for n in range(99)]),
            "",
        )

    assert _find_issue_number(runner, "org/tracker", "api|error|abc|prod") is None


def test_find_issue_number_raises_when_gh_is_missing() -> None:
    def runner(argv: list[str]) -> Completed:
        raise OSError("No such file or directory: 'gh'")

    with pytest.raises(StoreError, match="gh issue list failed"):
        _find_issue_number(runner, "org/tracker", "api|error|abc|prod")


def test_find_issue_number_raises_on_gh_failure() -> None:
    def runner(argv: list[str]) -> Completed:
        return Completed(1, "", "not found")

    with pytest.raises(StoreError, match="not found"):
        _find_issue_number(runner, "org/tracker", "api|error|abc|prod")


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
    assert _marker_for("api|error|abc123|prod") in body
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
        _marker_for("api|error|abc123|prod")
        + "\n\nAutomated error-log finding.\n\n"
        + _render_variants_section({"Ethereum": {"first_seen": "t0", "last_seen": "t0"}})
        + "\n"
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps([{"number": 9, "body": _marker_for("api|error|abc123|prod")}]),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"]:
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
        _marker_for("api|error|abc123|prod")
        + "\n\nAutomated error-log finding.\n\n"
        + _render_variants_section({"Ethereum": {"first_seen": "t0", "last_seen": "t0"}})
        + "\n"
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps([{"number": 9, "body": _marker_for("api|error|abc123|prod")}]),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"]:
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
    mode: str | None = None,
    number: int | None = None,
) -> None:
    result: dict[str, object] = {
        "issue_repo": "org/tracker",
        "template_fingerprint": template_fingerprint,
        "at": at,
    }
    if skipped:
        result["skipped"] = "cooldown"
    if mode is not None:
        result["mode"] = mode
    if number is not None:
        result["number"] = number
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


def test_cooldown_false_with_no_history(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    assert _within_cooldown(_touch_history(store, "org/tracker"), "api|error|abc|prod", utcnow(), 60) is False


def test_cooldown_true_within_window(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-1",
    )
    assert (
        _within_cooldown(_touch_history(store, "org/tracker"), "api|error|abc|prod", "2026-08-31T10:30:00Z", 60)
        is True
    )


def test_cooldown_false_after_expiry(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-1",
    )
    assert (
        _within_cooldown(_touch_history(store, "org/tracker"), "api|error|abc|prod", "2026-08-31T11:30:00Z", 60)
        is False
    )


def test_cooldown_ignores_skipped_results(tmp_path: Path) -> None:
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
    assert (
        _within_cooldown(_touch_history(store, "org/tracker"), "api|error|abc|prod", "2026-08-31T10:30:00Z", 60)
        is False
    )


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
        + _render_variants_section({"api: error (oldhash)": {"first_seen": "t0", "last_seen": "t0"}})
        + "\n"
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps([{"number": 55, "body": STORM_MARKER}]), "")
        if argv[:3] == ["gh", "issue", "view"]:
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
    """Two variants of the same template in one scan belong in one issue. Here
    that issue does not exist yet, so both land in the opening table of a single
    create — no update and no comment. Cooldown safety comes from the history
    snapshot, not from this grouping."""
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
            return Completed(
                0,
                json.dumps([{"number": 12, "body": _marker_for("api|error|same|prod")}]),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(
                0,
                json.dumps({"body": f'{_marker_for("api|error|same|prod")}\n\ntext\n'}),
                "",
            )
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
            return Completed(
                0,
                json.dumps([{"number": 12, "body": _marker_for("api|error|abc123|prod")}]),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(
                0,
                json.dumps({"body": f'{_marker_for("api|error|abc123|prod")}\n\ntext\n'}),
                "",
            )
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
        _splice_variants(damaged, {"Ethereum": {"first_seen": "t1", "last_seen": "t1"}})


def test_splice_variants_refuses_a_body_over_the_github_limit() -> None:
    """The ceiling still guards a body that is oversized for reasons the table
    cap cannot control, such as very long human-written prose."""
    huge_human_body = "human prose. " * (MAX_ISSUE_BODY // 10)
    with pytest.raises(StoreError):
        _splice_variants(huge_human_body, {"Ethereum": {"first_seen": "t1", "last_seen": "t1"}})


def test_render_variants_section_caps_the_table() -> None:
    """Unbounded growth would walk the issue into the body limit and wedge every
    later update on it, so the table keeps the most recent variants and says how
    many it dropped."""
    variants = {
        f"chain-{i:04d}": {"first_seen": "t1", "last_seen": f"2026-08-{(i % 28) + 1:02d}"}
        for i in range(MAX_TRACKED_VARIANTS + 50)
    }
    section = _render_variants_section(variants)
    parsed = _parse_variants_section(section)
    assert len(parsed) == MAX_TRACKED_VARIANTS
    assert "50 older variants dropped" in section
    # The dropped-count note must not survive as a phantom variant row.
    assert all(name.startswith("chain-") for name in parsed)


def test_variants_table_stays_under_the_ceiling_when_saturated() -> None:
    variants = {
        f"chain-{i:04d}": {"first_seen": "2026-08-31T10:00:00Z", "last_seen": "2026-08-31T10:00:00Z"}
        for i in range(MAX_TRACKED_VARIANTS * 5)
    }
    assert len(_splice_variants("Human notes.\n", variants)) <= MAX_ISSUE_BODY


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
    # The storm body is bounded by the same table cap, so it stays writable even
    # with far more templates than the cap.
    templates = {f"t-{i}": {"first_seen": "t1", "last_seen": "t1"} for i in range(6000)}
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        return Completed(0, "https://github.com/org/tracker/issues/1\n", "")

    _create_storm_issue(
        runner, issue_repo="org/tracker", templates=templates
    )
    body = created[0][created[0].index("--body") + 1]
    assert len(body) <= MAX_ISSUE_BODY


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
    history = _touch_history(store, "org/tracker")
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
    assert _touch_history(store, "org/tracker") == {}


# ---- untrusted excerpt text ----


def test_excerpt_cannot_move_the_section_boundary(tmp_path: Path) -> None:
    """A log line is untrusted data. One carrying the section marker would
    otherwise make a later splice rewrite everything between it and the real
    marker, destroying the excerpt and the issue's own structure."""
    store = Store(tmp_path)
    _runner_session(store)
    hostile = f"Timeout for Ethereum {_VARIANTS_START} injected"
    _seen(store, excerpt=hostile)
    _issue(store)
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/1\n", "")

    scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    body = created[-1][created[-1].index("--body") + 1]
    assert body.count(_VARIANTS_START) == 1
    assert body.count(_VARIANTS_END) == 1

    # A later update must keep the excerpt intact rather than splicing over it.
    updated = _splice_variants(body, _parse_variants_section(body))
    assert updated.count(_VARIANTS_START) == 1
    assert "injected" in updated


def test_storm_label_survives_a_round_trip_through_the_table() -> None:
    """service/class come from the error payload; a pipe or a marker there would
    break the row apart so the label would not parse back."""
    label = _storm_label("api|error|abc|prod", {"service": "a|b", "class": "<!-- variants:end -->"})
    section = _render_variants_section({label: {"first_seen": "t1", "last_seen": "t1"}})
    assert _parse_variants_section(section) == {label: {"first_seen": "t1", "last_seen": "t1"}}


# ---- clock skew ----


def test_cooldown_ignores_a_touch_dated_in_the_future(tmp_path: Path) -> None:
    """A future-dated touch would otherwise read as "no time has passed" forever
    and skip this template on every later scan."""
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-09-30T10:00:00Z",
        activity_id="prior-future",
    )
    assert (
        _within_cooldown(_touch_history(store, "org/tracker"), "api|error|abc|prod", "2026-08-31T10:00:00Z", 60)
        is False
    )


# ---- interrupted burst, then retry ----


def test_retry_after_a_partially_marked_burst_does_not_split_the_template(tmp_path: Path) -> None:
    """A burst writes its issue in one gh call but marks its rows one at a time.
    If the process dies mid-loop, the rows left pending belong to templates the
    burst issue already lists, so a retry must not file a second issue for them."""
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")

    def storm_runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/99\n", "")

    scan_error_issue(
        store, storm_runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2
    )

    # Simulate the crash: put one of the burst's rows back to pending.
    row = store.row("activity", "storm-issue-2")
    assert row is not None
    replayed = {k: v for k, v in row.items() if not k.startswith("_")}
    replayed["execution_status"] = "pending"
    replayed.pop("result", None)
    store.write("activity", "update", "storm-issue-2", replayed)

    # The burst issue is the only record that template t2 was already folded.
    burst_body = _render_variants_section(
        {
            _storm_label(f"api|error|t{i}|prod", {"service": "api", "class": "error"}): {
                "first_seen": "2026-08-31T10:00:00Z",
                "last_seen": "2026-08-31T10:00:00Z",
            }
            for i in range(3)
        }
    )

    def fail_if_created(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "create"]:
            raise AssertionError(f"must not open a second issue: {argv}")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps([{"number": 99, "body": STORM_MARKER}]), "")
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(0, json.dumps({"body": f"{STORM_MARKER}\n\n{burst_body}\n"}), "")
        return Completed(0, "", "")

    lines = scan_error_issue(
        store, fail_if_created, issue_repo="org/tracker", dry_run=False, storm_threshold=2
    )
    assert lines == ["error.issue storm-issue-2 already-in-burst number=99"]
    row = store.row("activity", "storm-issue-2")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["mode"] == "storm"


def test_storm_label_separates_environments() -> None:
    """service|class|template-sig|environment: the same error in two environments
    is two templates, so one shared row would hide one and undercount the burst."""
    payload = {"service": "api", "class": "error"}
    assert _storm_label("api|error|abc123def|prod", payload) != _storm_label(
        "api|error|abc123def|staging", payload
    )


def test_storm_issue_lists_each_environment_separately(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for index, environment in enumerate(("prod", "staging", "test")):
        _seen_and_issue(store, index=index, template_fingerprint=f"api|error|same|{environment}")
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/99\n", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2
    )
    assert all("storm size=3" in line for line in lines)
    body = created[-1][created[-1].index("--body") + 1]
    assert len(_parse_variants_section(body)) == 3


def test_evicted_burst_label_still_blocks_a_second_issue(tmp_path: Path) -> None:
    """The burst table is capped, so an old fold can drop out of the issue body.
    Local history has to cover that gap, or the template gets a second issue and
    ends up split across two."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, template_fingerprint="api|error|old|prod")
    _issue(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|old|prod",
        at="2026-08-01T10:00:00Z",
        activity_id="prior-storm",
        mode="storm",
        number=99,
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "create"]:
            raise AssertionError(f"must not open a second issue: {argv}")
        if argv[:3] == ["gh", "issue", "list"]:
            marker = STORM_MARKER if "--search" not in argv else ""
            return Completed(0, json.dumps([{"number": 99, "body": STORM_MARKER}]), marker)
        if argv[:3] == ["gh", "issue", "view"]:
            # The burst issue is open, but this template's row was evicted.
            body = f"{STORM_MARKER}\n\n" + _render_variants_section(
                {"other/prod: error (zzzzzzzz)": {"first_seen": "t1", "last_seen": "t1"}}
            )
            return Completed(0, json.dumps({"body": body}), "")
        return Completed(0, "", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=0
    )
    assert lines == ["error.issue issue-1 already-in-burst number=99"]


def test_a_closed_burst_lets_the_template_get_its_own_issue(tmp_path: Path) -> None:
    """Once a human closes the burst issue, a template that recurs has earned an
    issue of its own — history alone must not suppress it forever."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, template_fingerprint="api|error|old|prod")
    _issue(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|old|prod",
        at="2026-08-01T10:00:00Z",
        activity_id="prior-storm",
        mode="storm",
        number=42,
    )
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/5\n", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=0
    )
    assert lines == ["error.issue issue-1 created variant=Ethereum"]
    assert any(c[:3] == ["gh", "issue", "create"] for c in created)


def test_a_fold_into_a_closed_burst_does_not_count_for_a_different_one(tmp_path: Path) -> None:
    """A template folded into a burst that has since been closed must not be
    marked as handled by whatever burst happens to be open now — that burst does
    not list it, so the error would be swallowed with no issue mentioning it."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, template_fingerprint="api|error|old|prod")
    _issue(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|old|prod",
        at="2026-08-01T10:00:00Z",
        activity_id="prior-storm",
        mode="storm",
        number=42,
    )
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            # A different burst issue is open now; it does not list this template.
            return Completed(0, json.dumps([{"number": 777, "body": STORM_MARKER}]), "")
        if argv[:3] == ["gh", "issue", "view"]:
            body = f"{STORM_MARKER}\n\n" + _render_variants_section(
                {"other/prod: error (unrelated)": {"first_seen": "t1", "last_seen": "t1"}}
            )
            return Completed(0, json.dumps({"body": body}), "")
        return Completed(0, "https://github.com/org/tracker/issues/5\n", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=0
    )
    assert lines == ["error.issue issue-1 created variant=Ethereum"]
    assert any(c[:3] == ["gh", "issue", "create"] for c in created)


def test_a_created_burst_records_its_issue_number(tmp_path: Path) -> None:
    """The number is what later scans match a fold against, so a burst that was
    created rather than reused has to record it too."""
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/99\n", "")

    scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2)
    for i in range(3):
        row = store.row("activity", f"storm-issue-{i}")
        assert row is not None
        assert row["result"]["created"] is True
        assert row["result"]["number"] == 99
    assert _burst_folded_templates(store, "org/tracker") == {
        f"api|error|t{i}|prod": {99} for i in range(3)
    }


def test_storm_label_is_unique_per_template_despite_separators(tmp_path: Path) -> None:
    """service, class and environment are free text from the log source, so they
    can contain the separators the fingerprint joins on and the table renders
    with. Two different templates must still get two rows, or one goes missing
    from the burst issue while a row claims to cover it."""
    first = _storm_label(
        "api|error|abc123def|prod/eu",
        {"service": "api", "class": "error", "environment": "prod/eu"},
    )
    second = _storm_label(
        "api/prod|error|abc123def|eu",
        {"service": "api/prod", "class": "error", "environment": "eu"},
    )
    assert first != second

    # A pipe inside a component must not shift which field the label reports.
    shifted = _storm_label(
        "api|x|error|sigAAAA|prod",
        {"service": "api|x", "class": "error", "environment": "prod"},
    )
    assert shifted.startswith("`api/x/prod: error (")


def test_storm_issue_keeps_a_row_per_colliding_template(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    for index, (service, environment) in enumerate(
        (("api", "prod/eu"), ("api/prod", "eu"), ("api", "eu"))
    ):
        seen_id, issue_id = f"seen-{index}", f"storm-issue-{index}"
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
                    "template_fingerprint": f"{service}|error|abc123def|{environment}",
                    "excerpt": "Some error",
                    "service": service,
                    "class": "error",
                    "environment": environment,
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
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/99\n", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, storm_threshold=2
    )
    assert all("storm size=3" in line for line in lines)
    body = created[-1][created[-1].index("--body") + 1]
    assert len(_parse_variants_section(body)) == 3


# ---- untrusted text cannot escape its quoting ----


def test_excerpt_cannot_break_out_of_its_code_fence(tmp_path: Path) -> None:
    """A log line containing a fence would close the quote early and let the
    rest render as live Markdown in an issue presented as an inert log quote."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, excerpt="boom ``` then [a](http://x) and more")
    _issue(store)
    created: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        created.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, "[]", "")
        return Completed(0, "https://github.com/org/tracker/issues/1\n", "")

    scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    body = created[-1][created[-1].index("--body") + 1]
    fence = "````"
    assert body.count(fence) == 2
    quoted = body.split(fence)[1]
    assert "boom ``` then [a](http://x) and more" in quoted


def test_splice_refuses_a_duplicated_marker(tmp_path: Path) -> None:
    """Two copies of a marker mean the body is damaged; splicing across them
    would silently rewrite whatever a human put between the copies."""
    section = _render_variants_section({"Ethereum": {"first_seen": "t1", "last_seen": "t1"}})
    doubled = f"{section}\n\nhuman notes worth keeping\n\n{section}\n"
    with pytest.raises(StoreError, match="damaged variants section"):
        _splice_variants(doubled, {"Ethereum": {"first_seen": "t1", "last_seen": "t2"}})


# ---- history is per issue_repo ----


def test_cooldown_history_does_not_leak_across_repos(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-1",
    )
    assert _touch_history(store, "org/tracker") != {}
    assert _touch_history(store, "org/other-tracker") == {}


def test_burst_folds_do_not_leak_across_repos(tmp_path: Path) -> None:
    """Issue numbers are per repo, so a fold recorded against another tracker
    must not mark a template as covered here."""
    store = Store(tmp_path)
    _runner_session(store)
    _prior_touch(
        store,
        template_fingerprint="api|error|abc|prod",
        at="2026-08-31T10:00:00Z",
        activity_id="prior-1",
        mode="storm",
        number=99,
    )
    assert _burst_folded_templates(store, "org/tracker") == {"api|error|abc|prod": {99}}
    assert _burst_folded_templates(store, "org/other-tracker") == {}


def test_a_row_named_like_the_header_survives_the_round_trip(tmp_path: Path) -> None:
    """service is free text, so a label can begin with "variant". Matching the
    header by prefix would drop that row, and a template the burst issue already
    lists would be filed a second time."""
    label = _storm_label(
        "variant|error|abc123def|prod",
        {"service": "variant", "class": "error", "environment": "prod"},
    )
    assert label.startswith("`variant")
    section = _render_variants_section({label: {"first_seen": "t1", "last_seen": "t1"}})
    assert _parse_variants_section(section) == {label: {"first_seen": "t1", "last_seen": "t1"}}


def test_a_row_named_like_the_header_still_blocks_a_second_issue(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(
        store,
        template_fingerprint="variant|error|abc123def|prod",
        service="variant",
        excerpt="Some error",
    )
    _issue(store)
    label = _storm_label(
        "variant|error|abc123def|prod",
        {"service": "variant", "class": "error", "environment": "prod"},
    )
    burst_body = f"{STORM_MARKER}\n\n" + _render_variants_section(
        {label: {"first_seen": "t1", "last_seen": "t1"}}
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "create"]:
            raise AssertionError(f"must not open a second issue: {argv}")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps([{"number": 99, "body": STORM_MARKER}]), "")
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(0, json.dumps({"body": burst_body}), "")
        return Completed(0, "", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=0
    )
    assert lines == ["error.issue issue-1 already-in-burst number=99"]


def test_storm_label_renders_log_text_inertly() -> None:
    """service and class come from the log source. A bare mention or link in a
    table cell renders as a live mention or link in the issue."""
    label = _storm_label(
        "api|error|abc|prod",
        {"service": "@someone", "class": "[click](http://x)", "environment": "prod"},
    )
    assert label.startswith("`") and label.endswith("`")
    assert "@someone" in label  # the text is kept, only made inert
    # A backtick cannot survive inside a single-backtick span.
    assert "`" not in label[1:-1]
    spanned = _storm_label(
        "api|error|abc|prod",
        {"service": "a`b", "class": "error", "environment": "prod"},
    )
    assert "`" not in spanned[1:-1]


def test_gh_argument_errors_stay_on_the_row(tmp_path: Path) -> None:
    """subprocess refuses a NUL byte with ValueError, not OSError. Uncaught, it
    would abort the whole scan and the row would block every later run."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)

    def runner(argv: list[str]) -> Completed:
        raise ValueError("embedded null byte")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == ["error.issue issue-1 error"]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert "embedded null byte" in row["execution_error"]


def test_burst_lookup_fails_loud_on_a_damaged_body(tmp_path: Path) -> None:
    """A damaged burst body parses to nothing, which would read as "covers no
    template" and file a duplicate. It has to fail like a splice would."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)
    damaged = f"{STORM_MARKER}\n\n{_VARIANTS_START}\n\n| a | t1 | t1 |\n"

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "create"]:
            raise AssertionError(f"must not open an issue off a damaged body: {argv}")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps([{"number": 99, "body": STORM_MARKER}]), "")
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(0, json.dumps({"body": damaged}), "")
        return Completed(0, "", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=0
    )
    assert lines == ["error.issue issue-1 error"]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert "damaged variants section" in row["execution_error"]


def test_a_damaged_burst_body_is_only_fetched_once(tmp_path: Path) -> None:
    """The lookup promises one fetch per scan. Without caching the failure, every
    later template would repeat both gh calls against the same broken body."""
    store = Store(tmp_path)
    _runner_session(store)
    for i in range(3):
        _seen_and_issue(store, index=i, template_fingerprint=f"api|error|t{i}|prod")
    damaged = f"{STORM_MARKER}\n\n{_VARIANTS_START}\n\n| a | t1 | t1 |\n"
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps([{"number": 99, "body": STORM_MARKER}]), "")
        if argv[:3] == ["gh", "issue", "view"]:
            return Completed(0, json.dumps({"body": damaged}), "")
        return Completed(0, "", "")

    lines = scan_error_issue(
        store, runner, issue_repo="org/tracker", dry_run=False, cooldown_minutes=0
    )
    assert len(lines) == 3
    assert all(line.endswith("error") for line in lines)
    # One list + one view for the burst issue, plus the per-template marker
    # lookups; the damaged body must not be re-fetched per template.
    assert len([c for c in calls if c[:3] == ["gh", "issue", "view"]]) == 1


def test_an_issue_that_lost_its_marker_is_not_edited(tmp_path: Path) -> None:
    """The marker is matched on the listed body, but the body that gets spliced
    is fetched again. If it lost the marker in between, writing to it would
    leave an issue this module can never find again and the next scan would open
    a second one for the same template."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _issue(store)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps([{"number": 12, "body": _marker_for("api|error|abc123|prod")}]),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"]:
            # Someone edited the marker away between the two calls.
            return Completed(0, json.dumps({"body": "human rewrote this\n"}), "")
        if argv[:3] == ["gh", "issue", "edit"]:
            raise AssertionError(f"must not edit an issue that lost its marker: {argv}")
        return Completed(0, "", "")

    lines = scan_error_issue(store, runner, issue_repo="org/tracker", dry_run=False)
    assert lines == ["error.issue issue-1 error"]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert "carries its marker 0 times" in row["execution_error"]
