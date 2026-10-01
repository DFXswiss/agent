"""Pure checks for the A38 review-completion declaration."""

from __future__ import annotations

import json

import pytest

from agent_cli.a38_review import (
    REVIEW_BEGIN,
    REVIEW_END,
    parse_review_block,
    select_review_comment,
    unresolved_thread_reasons,
    validate_declaration,
    validate_visible,
)

pytestmark = pytest.mark.no_pg

HEAD = "a" * 40
OTHER = "b" * 40
LANES = ("conformity-a", "logic-a", "conformity-b", "logic-b")


def _pass_payload(**overrides: object) -> dict:
    payload = {
        "schema": "a38-review/v1",
        "head": HEAD,
        "passes": 2,
        "defects": 0,
        "lanes": [
            {"id": lane, "result": "pass", "status": "complete"} for lane in LANES
        ],
    }
    payload.update(overrides)
    return payload


def _body(payload: dict, *, passes: int = 2) -> str:
    return (
        "EN:\n"
        f"Ready after {passes} review passes.\n"
        "The change is covered.\n"
        "\n"
        "DE:\n"
        f"Bereit nach {passes} Review-Durchläufen.\n"
        "Die Änderung ist abgedeckt.\n"
        "\n"
        f"{REVIEW_BEGIN}\n"
        "```json\n"
        + json.dumps(payload)
        + "\n```\n"
        f"{REVIEW_END}\n"
    )


def _comment(body: str, *, cid: int, user: int, created: str = "2026-09-01T00:00:00Z") -> dict:
    return {
        "id": cid,
        "created_at": created,
        "user": {"id": user, "login": "someone", "type": "User"},
        "body": body,
    }


def _thread(*bodies: str, resolved: bool = False, bot: bool = False) -> dict:
    author = {"login": "copilot[bot]", "__typename": "Bot"} if bot else {
        "login": "ada",
        "__typename": "User",
    }
    return {
        "isResolved": resolved,
        "comments": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"body": body, "author": author} for body in bodies],
        },
    }


def test_visible_text_rejects_a_line_between_required_steps() -> None:
    payload = _pass_payload()
    broken = _body(payload).replace(
        "The change is covered.\n\nDE:",
        "The change is covered.\nExtra note.\nDE:",
        1,
    )
    assert validate_visible(broken, 2) == ["review visible text missing"]
    trailing = _body(payload) + "\n<details>\n<summary>Details</summary>\n\nMore.\n</details>\n"
    assert validate_visible(trailing, 2) == []


def test_comment_page_without_false_has_next_page_fails_closed() -> None:
    from agent_cli.a38_review import _load_threads

    class Api:
        def request(self, method, path, body=None, retry=None):
            return 200, {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{
                                    "isResolved": True,
                                    "comments": {
                                        "nodes": [],
                                        "pageInfo": {"hasNextPage": None},
                                    },
                                }],
                            }
                        }
                    }
                }
            }, {}

    assert _load_threads(Api(), "acme/repo", 1) == ["review thread query failed"]


def test_valid_pass_declaration() -> None:
    payload = _pass_payload()
    assert validate_declaration(payload, head=HEAD, markdown_only=False) == []
    assert validate_visible(_body(payload), 2) == []
    assert parse_review_block(_body(payload))["head"] == HEAD


def test_duplicate_key_rejected() -> None:
    raw = _body(_pass_payload()).replace(
        '"defects": 0',
        '"defects": 0, "defects": 0',
        1,
    )
    with pytest.raises(Exception, match="duplicate key"):
        parse_review_block(raw)


def test_wrong_head_and_defects() -> None:
    wrong = _pass_payload(head=OTHER)
    assert "review head does not match the pull request" in validate_declaration(
        wrong, head=HEAD, markdown_only=False
    )
    defects = _pass_payload(defects=1)
    assert "review defects must be 0" in validate_declaration(
        defects, head=HEAD, markdown_only=False
    )


def _run(suffix: str, *, result: str = "pass", evidence: str | None = None) -> list[dict]:
    lanes = []
    for kind in ("conformity", "logic"):
        lane: dict = {"id": f"{kind}-{suffix}", "result": result}
        if result == "pass":
            lane["status"] = "complete"
        else:
            lane["evidence"] = evidence
        lanes.append(lane)
    return lanes


def test_one_full_run_is_enough() -> None:
    for suffix in ("a", "b"):
        payload = _pass_payload()
        payload["lanes"] = _run(suffix)
        assert validate_declaration(payload, head=HEAD, markdown_only=False) == []
    both = _pass_payload()
    assert validate_declaration(both, head=HEAD, markdown_only=False) == []


def test_half_run_is_rejected() -> None:
    payload = _pass_payload()
    payload["lanes"] = [{"id": "conformity-a", "result": "pass", "status": "complete"}]
    reasons = validate_declaration(payload, head=HEAD, markdown_only=False)
    assert "review run is incomplete" in reasons
    assert "review lanes must include one full run" in reasons
    payload["lanes"] = _run("a") + [
        {"id": "conformity-b", "result": "pass", "status": "complete"}
    ]
    reasons = validate_declaration(payload, head=HEAD, markdown_only=False)
    assert reasons == ["review run is incomplete"]
    payload["lanes"] = [{"id": "conformity-c", "result": "pass", "status": "complete"}]
    reasons = validate_declaration(payload, head=HEAD, markdown_only=False)
    assert "review lane id is invalid" in reasons


def test_one_run_na_is_enough_when_markdown_only() -> None:
    evidence = f"markdown-only rebase, previously approved at {OTHER}"
    payload = _pass_payload()
    payload["lanes"] = _run("a", result="n_a", evidence=evidence)
    assert validate_declaration(payload, head=HEAD, markdown_only=True) == []
    assert "review n_a requires a markdown-only change set" in validate_declaration(
        payload, head=HEAD, markdown_only=False
    )


def test_mixed_lanes_rejected() -> None:
    payload = _pass_payload()
    payload["lanes"][0] = {
        "id": "conformity-a",
        "result": "n_a",
        "evidence": f"markdown-only rebase, previously approved at {OTHER}",
    }
    reasons = validate_declaration(payload, head=HEAD, markdown_only=True)
    assert "review lanes mix pass and n_a" in reasons


def test_na_only_when_markdown_only_and_sha_differs() -> None:
    evidence = f"markdown-only rebase, previously approved at {OTHER}"
    payload = _pass_payload()
    payload["lanes"] = [
        {"id": lane, "result": "n_a", "evidence": evidence} for lane in LANES
    ]
    assert validate_declaration(payload, head=HEAD, markdown_only=True) == []
    assert "review n_a requires a markdown-only change set" in validate_declaration(
        payload, head=HEAD, markdown_only=False
    )
    same = f"markdown-only rebase, previously approved at {HEAD}"
    payload["lanes"] = [
        {"id": lane, "result": "n_a", "evidence": same} for lane in LANES
    ]
    assert "review n_a SHA equals the current head" in validate_declaration(
        payload, head=HEAD, markdown_only=True
    )


def _select(comments: list[dict], **overrides: object) -> tuple:
    kwargs = {
        "author_id": 10,
        "guard_user_id": 99,
        "committed_at": "2026-08-01T00:00:00Z",
        "head": HEAD,
        "markdown_only": False,
    }
    kwargs.update(overrides)
    return select_review_comment(comments, **kwargs)


def test_later_comments_do_not_invalidate_a_declaration() -> None:
    declaration = _comment(_body(_pass_payload()), cid=1, user=10)
    guard = _comment("guard note", cid=2, user=99, created="2026-09-02T00:00:00Z")
    chosen, reasons = _select([declaration, guard])
    assert reasons == []
    assert chosen is declaration
    later = _comment("please look again", cid=3, user=11, created="2026-09-03T00:00:00Z")
    chosen, reasons = _select([declaration, guard, later])
    assert reasons == []
    assert chosen is declaration


def test_malformed_later_block_falls_back_to_older_valid() -> None:
    good = _comment(_body(_pass_payload()), cid=1, user=10)
    bad = _comment(
        "<!-- A38-REVIEW:v1 --> not json <!-- /A38-REVIEW:v1 -->",
        cid=2,
        user=10,
        created="2026-09-02T00:00:00Z",
    )
    chosen, reasons = _select([good, bad])
    assert reasons == []
    assert chosen is good
    chosen, reasons = _select([bad])
    assert chosen is None
    assert any("json fence" in reason for reason in reasons)


def test_declaration_must_be_after_the_head_commit() -> None:
    early = _comment(_body(_pass_payload()), cid=1, user=10, created="2026-09-01T00:00:00Z")
    chosen, reasons = _select([early], committed_at="2026-09-02T00:00:00Z")
    assert chosen is None
    assert reasons == ["review completion comment missing"]
    same_instant = _comment(
        _body(_pass_payload()), cid=2, user=10, created="2026-09-02T00:00:00Z"
    )
    chosen, reasons = _select([same_instant], committed_at="2026-09-02T00:00:00Z")
    assert chosen is None
    edited = _comment(
        _body(_pass_payload()), cid=3, user=10, created="2026-09-01T00:00:00Z"
    )
    edited["updated_at"] = "2026-09-03T00:00:00Z"
    chosen, reasons = _select([edited], committed_at="2026-09-02T00:00:00Z")
    assert reasons == []
    assert chosen is edited


def test_another_author_cannot_satisfy_the_gate() -> None:
    foreign = _comment(
        _body(_pass_payload()), cid=4, user=11, created="2026-09-04T00:00:00Z"
    )
    chosen, reasons = _select([foreign])
    assert chosen is None
    assert reasons == ["review completion comment missing"]
    own = _comment(_body(_pass_payload()), cid=1, user=10)
    chosen, reasons = _select([own, foreign])
    assert reasons == []
    assert chosen is own


def test_unreadable_commit_time_fails_closed() -> None:
    declaration = _comment(_body(_pass_payload()), cid=1, user=10)
    chosen, reasons = _select([declaration], committed_at="yesterday")
    assert chosen is None
    assert reasons == ["review commit time unavailable"]


def test_threads() -> None:
    assert unresolved_thread_reasons([]) == []
    assert unresolved_thread_reasons([_thread("nit", bot=True)]) == [
        "unresolved review threads"
    ]
    assert unresolved_thread_reasons([_thread("nit", bot=True, resolved=True)]) == []
    kept = _thread("User-Entscheid: leave the name.")
    assert unresolved_thread_reasons([kept]) == []
    revoked = _thread(
        "User-Entscheid: leave the name.\nUser-Entscheid aufgehoben: reopen."
    )
    assert unresolved_thread_reasons([revoked]) == ["unresolved review threads"]
