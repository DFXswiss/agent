"""Pure checks for the A38 review-completion declaration."""

from __future__ import annotations

import copy
import json

import pytest

from agent_cli.a38_guard import LOCAL_CI_BEGIN, LOCAL_CI_END, choose_author_report
from agent_cli.allow import codex_second_review_na
from agent_cli.a38_review import (
    REVIEW_BEGIN,
    REVIEW_END,
    ReviewError,
    parse_review_block,
    render_review_record,
    select_review_comment,
    unresolved_thread_reasons,
    validate_declaration,
    validate_review_record,
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


_RECORD_PROVIDER = "Acme"
_RECORD_MODEL = "Acme model"
_RECORD_NUMBER = "acme-1"
_RECORD_PROMPT = "Read the diff and name each defect with its file and line."


def _renderable(payload: dict) -> dict:
    """Facts the human record needs. A v1 payload must not carry them."""
    if payload.get("schema") == "a38-review/v2":
        return payload
    rendered = copy.deepcopy(payload)
    lanes = rendered.get("lanes")
    if not isinstance(lanes, list):
        return rendered
    for lane in lanes:
        if not isinstance(lane, dict) or lane.get("result") != "pass":
            continue
        lane.setdefault("provider", _RECORD_PROVIDER)
        lane.setdefault("model", _RECORD_MODEL)
        lane.setdefault("model_number", _RECORD_NUMBER)
        lane.setdefault("prompt", _RECORD_PROMPT)
    return rendered


def _body(payload: dict, *, passes: int = 2) -> str:
    lanes = payload.get("lanes")
    ids: set[str] = set()
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, dict) and isinstance(lane.get("id"), str):
                ids.add(lane["id"])
    if ids == {"conformity-b", "logic-b"}:
        en_title = "Second review"
        de_title = "Zweiter Review"
        en_sentence = f"Second review after {passes} review passes."
        de_sentence = f"Zweiter Review nach {passes} Review-Durchläufen."
    else:
        en_title = "First review"
        de_title = "Erster Review"
        en_sentence = f"Ready after {passes} review passes."
        de_sentence = f"Bereit nach {passes} Review-Durchläufen."
    return (
        "EN:\n"
        f"{en_title}\n"
        f"{en_sentence}\n"
        "The change is covered.\n"
        "\n"
        "DE:\n"
        f"{de_title}\n"
        f"{de_sentence}\n"
        "Die Änderung ist abgedeckt.\n"
        "\n"
        "<details>\n<summary>Details</summary>\n\n"
        + render_review_record(_renderable(payload))
        + f"{REVIEW_BEGIN}\n"
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
    record = (
        "<details>\n<summary>Details</summary>\n\n"
        f"Head: `{HEAD}`\n"
        "Runs: `conformity-a` with `logic-a`, and `conformity-b` with `logic-b`.\n"
        "Lane `conformity-a`:\n"
        "Provider: Example\n"
        "Model: Example model\n"
        "Model number: example-1\n"
        "Prompt:\n"
        "```\nnot the declaration\n```\n"
        "Result: pass, no findings left.\n"
        "Set aside: a note judged not relevant because it contradicted the repo convention.\n\n"
    )
    recorded = _body(payload).replace(f"{REVIEW_BEGIN}\n", record + f"{REVIEW_BEGIN}\n", 1)
    assert validate_visible(recorded, 2) == []
    assert parse_review_block(recorded) == payload


def test_second_run_ready_lines_are_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("b"))
    passes = payload["passes"]
    body = (
        _body(payload)
        .replace(
            f"Second review after {passes} review passes.",
            f"Ready after {passes} review passes.",
            1,
        )
        .replace(
            f"Zweiter Review nach {passes} Review-Durchläufen.",
            f"Bereit nach {passes} Review-Durchläufen.",
            1,
        )
    )
    assert validate_visible(body, passes) == ["review visible text missing"]


def test_second_run_second_review_lines_are_visible() -> None:
    payload = _pass_payload(lanes=_run("b"))
    assert validate_visible(_body(payload), payload["passes"]) == []


def test_first_run_second_review_lines_are_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("a"))
    passes = payload["passes"]
    body = (
        _body(payload)
        .replace(
            f"Ready after {passes} review passes.",
            f"Second review after {passes} review passes.",
            1,
        )
        .replace(
            f"Bereit nach {passes} Review-Durchläufen.",
            f"Zweiter Review nach {passes} Review-Durchläufen.",
            1,
        )
    )
    assert validate_visible(body, passes) == ["review visible text missing"]


def test_first_run_first_review_title_is_visible() -> None:
    payload = _pass_payload(lanes=_run("a"))
    body = _body(payload)
    assert "First review\n" in body
    assert "Erster Review\n" in body
    assert validate_visible(body, payload["passes"]) == []


def test_first_run_second_review_title_is_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("a"))
    body = (
        _body(payload)
        .replace("First review\n", "Second review\n", 1)
        .replace("Erster Review\n", "Zweiter Review\n", 1)
    )
    assert validate_visible(body, payload["passes"]) == ["review visible text missing"]


def test_first_run_ready_lines_without_title_are_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("a"))
    body = _body(payload).replace("First review\n", "", 1).replace("Erster Review\n", "", 1)
    assert validate_visible(body, payload["passes"]) == ["review visible text missing"]


def test_second_run_second_review_title_is_visible() -> None:
    payload = _pass_payload(lanes=_run("b"))
    body = _body(payload)
    assert "Second review\n" in body
    assert "Zweiter Review\n" in body
    assert validate_visible(body, payload["passes"]) == []


def test_second_run_first_review_title_is_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("b"))
    body = (
        _body(payload)
        .replace("Second review\n", "First review\n", 1)
        .replace("Zweiter Review\n", "Erster Review\n", 1)
    )
    assert validate_visible(body, payload["passes"]) == ["review visible text missing"]


def test_second_run_second_review_lines_without_title_are_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("b"))
    body = (
        _body(payload)
        .replace("Second review\n", "", 1)
        .replace("Zweiter Review\n", "", 1)
    )
    assert validate_visible(body, payload["passes"]) == ["review visible text missing"]


def test_second_run_ready_lines_as_summary_are_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("b"))
    passes = payload["passes"]
    body = (
        _body(payload, passes=passes)
        .replace("The change is covered.", f"Ready after {passes} review passes.", 1)
        .replace(
            "Die Änderung ist abgedeckt.",
            f"Bereit nach {passes} Review-Durchläufen.",
            1,
        )
    )
    assert "Second review after" in body
    assert "Zweiter Review nach" in body
    assert validate_visible(body, passes) == ["review visible text missing"]


def test_second_run_ready_lines_with_second_review_title_are_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("b"))
    passes = payload["passes"]
    body = (
        _body(payload)
        .replace(
            f"Second review after {passes} review passes.",
            f"Ready after {passes} review passes.",
            1,
        )
        .replace(
            f"Zweiter Review nach {passes} Review-Durchläufen.",
            f"Bereit nach {passes} Review-Durchläufen.",
            1,
        )
    )
    assert "Second review\n" in body
    assert "Zweiter Review\n" in body
    assert validate_visible(body, passes) == ["review visible text missing"]


def test_both_runs_first_review_title_passes_visible() -> None:
    payload = _pass_payload()
    body = _body(payload)
    assert "First review\n" in body
    assert "Erster Review\n" in body
    assert f"Ready after {payload['passes']} review passes." in body
    assert validate_visible(body, payload["passes"]) == []
    assert "review runs must be separate comments" in validate_declaration(
        payload, head=HEAD, markdown_only=False
    )


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
    payload = _pass_payload(lanes=_run("a"))
    assert validate_declaration(payload, head=HEAD, markdown_only=False) == []
    assert validate_visible(_body(payload), 2) == []
    assert parse_review_block(_body(payload))["head"] == HEAD
    both = _pass_payload()
    both_reasons = validate_declaration(both, head=HEAD, markdown_only=False)
    assert "review runs must be separate comments" in both_reasons
    assert "review lanes must include one full run" not in both_reasons


def test_duplicate_key_rejected() -> None:
    raw = _body(_pass_payload()).replace(
        '"defects": 0',
        '"defects": 0, "defects": 0',
        1,
    )
    with pytest.raises(Exception, match="duplicate key"):
        parse_review_block(raw)
    nan_body = _body(_pass_payload()).replace('"passes": 2', '"passes": NaN', 1)
    with pytest.raises(Exception, match="non-finite"):
        parse_review_block(nan_body)
    duplicated = _body(_pass_payload()).replace(REVIEW_BEGIN, REVIEW_BEGIN + REVIEW_BEGIN, 1)
    with pytest.raises(Exception, match="review markers missing or duplicated"):
        parse_review_block(duplicated)
    two_fences = _body(_pass_payload()).replace(
        "```json\n",
        "```json\n{}\n```\n```json\n",
        1,
    )
    with pytest.raises(Exception, match="review block must contain one json fence"):
        parse_review_block(two_fences)
    payload = _pass_payload()
    payload["extra"] = 1
    assert "review declaration has unknown keys" in validate_declaration(
        payload, head=HEAD, markdown_only=False
    )


def test_wrong_head_and_defects() -> None:
    wrong = _pass_payload(head=OTHER, lanes=_run("a"))
    assert "review head does not match the pull request" in validate_declaration(
        wrong, head=HEAD, markdown_only=False
    )
    defects = _pass_payload(defects=1, lanes=_run("a"))
    assert "review defects must be 0" in validate_declaration(
        defects, head=HEAD, markdown_only=False
    )


def test_one_full_run_is_enough() -> None:
    for suffix in ("a", "b"):
        payload = _pass_payload()
        payload["lanes"] = _run(suffix)
        assert validate_declaration(payload, head=HEAD, markdown_only=False) == []
    both = _pass_payload()
    reasons = validate_declaration(both, head=HEAD, markdown_only=False)
    assert "review runs must be separate comments" in reasons
    assert "review lanes must include one full run" not in reasons


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
    both_na = validate_declaration(payload, head=HEAD, markdown_only=True)
    assert "review runs must be separate comments" in both_na
    payload["lanes"] = _run("a", result="n_a", evidence=evidence)
    assert validate_declaration(payload, head=HEAD, markdown_only=True) == []
    assert "review n_a requires a markdown-only change set" in validate_declaration(
        payload, head=HEAD, markdown_only=False
    )
    same = f"markdown-only rebase, previously approved at {HEAD}"
    payload["lanes"] = _run("a", result="n_a", evidence=same)
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
    declaration = _comment(_body(_pass_payload(lanes=_run("a"))), cid=1, user=10)
    guard = _comment("guard note", cid=2, user=99, created="2026-09-02T00:00:00Z")
    chosen, reasons = _select([declaration, guard])
    assert reasons == []
    assert chosen is declaration
    later = _comment("please look again", cid=3, user=11, created="2026-09-03T00:00:00Z")
    chosen, reasons = _select([declaration, guard, later])
    assert reasons == []
    assert chosen is declaration


def test_malformed_later_block_falls_back_to_older_valid() -> None:
    good = _comment(_body(_pass_payload(lanes=_run("a"))), cid=1, user=10)
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
    early = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    chosen, reasons = _select([early], committed_at="2026-09-02T00:00:00Z")
    assert chosen is None
    assert reasons == ["review completion comment missing"]
    same_instant = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=2, user=10, created="2026-09-02T00:00:00Z"
    )
    chosen, reasons = _select([same_instant], committed_at="2026-09-02T00:00:00Z")
    assert chosen is None
    edited = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=3, user=10, created="2026-09-01T00:00:00Z"
    )
    edited["updated_at"] = "2026-09-03T00:00:00Z"
    chosen, reasons = _select([edited], committed_at="2026-09-02T00:00:00Z")
    assert chosen is None
    assert reasons == ["review completion comment missing"]
    posted = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=4, user=10, created="2026-09-03T00:00:00Z"
    )
    chosen, reasons = _select([edited, posted], committed_at="2026-09-02T00:00:00Z")
    assert reasons == []
    assert chosen is posted


def test_unreadable_other_comment_keeps_an_older_declaration() -> None:
    good = _comment(_body(_pass_payload(lanes=_run("a"))), cid=1, user=10)
    deleted = {
        "id": 5,
        "created_at": "2026-09-05T00:00:00Z",
        "user": None,
        "body": "account gone",
    }
    broken = {
        "id": "not-an-int",
        "created_at": None,
        "user": {"id": 12, "login": "other", "type": "User"},
        "body": "<!-- A38-REVIEW:v1 -->",
    }
    chosen, reasons = _select([good, deleted, broken, "not-a-comment"])
    assert reasons == []
    assert chosen is good
    only, reasons = _select([deleted, broken])
    assert only is None
    assert reasons == ["review completion comment missing"]


def test_another_author_cannot_satisfy_the_gate() -> None:
    foreign = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=4, user=11, created="2026-09-04T00:00:00Z"
    )
    chosen, reasons = _select([foreign])
    assert chosen is None
    assert reasons == ["review completion comment missing"]
    own = _comment(_body(_pass_payload(lanes=_run("a"))), cid=1, user=10)
    chosen, reasons = _select([own, foreign])
    assert reasons == []
    assert chosen is own


def test_review_record_requires_prompt_model_and_set_aside() -> None:
    payload = _pass_payload()
    assert validate_review_record(_body(payload), payload) == []
    bare_fence = _body(payload).replace("```text\n", "```\n", 1)
    assert validate_review_record(bare_fence, payload) == []
    placeholder_prompt = _body(payload).replace(_RECORD_PROMPT, "not recorded")
    assert "review prompt missing" in validate_review_record(placeholder_prompt, payload)
    placeholder = _body(payload).replace(
        f"Provider: {_RECORD_PROVIDER}\n", "Provider: not recorded\n", 1
    )
    assert validate_review_record(placeholder, payload) == ["review provider missing"]
    wrong_runs = _body(payload).replace(
        "Runs: `conformity-a` with `logic-a`, and `conformity-b` with `logic-b`.",
        "Runs: `conformity-a` with `logic-a`. The second run may be omitted.",
        1,
    )
    assert "review runs line does not match" in validate_review_record(wrong_runs, payload)
    bare = (
        "EN:\nFirst review\nReady after 2 review passes.\nThe change is covered.\n\n"
        "DE:\nErster Review\nBereit nach 2 Review-Durchläufen.\nDie Änderung ist abgedeckt.\n\n"
        f"{REVIEW_BEGIN}\n```json\n{json.dumps(payload)}\n```\n{REVIEW_END}\n"
    )
    assert validate_review_record(bare, payload) == ["review record missing"]
    assert validate_visible(bare, 2) == []
    without_prompt_label = "\n".join(
        line for line in _body(payload).splitlines() if not line.startswith("Prompt:")
    ) + "\n"
    assert "review prompt missing" in validate_review_record(without_prompt_label, payload)
    inline_only = _body(payload).replace(
        "```text\n" + _RECORD_PROMPT + "\n```\n",
        "Prompt: " + _RECORD_PROMPT + "\n",
    )
    assert "review prompt missing" in validate_review_record(inline_only, payload)
    underscored = _body(payload).replace("Lane `", "_Lane `")
    assert validate_review_record(underscored, payload) == []
    emphasized = _body(payload).replace("Lane `", "__Lane `")
    assert validate_review_record(emphasized, payload) == []
    for denied_head in (
        f"Head: isn't `{HEAD}`",
        f"Head: cannot be `{HEAD}`",
        f"Head: `{HEAD}` is not this commit.",
    ):
        denied = _body(payload).replace(f"Head: `{HEAD}`", denied_head, 1)
        assert "review head line does not match" in validate_review_record(denied, payload)
    other_clause = _body(payload).replace(
        f"Head: `{HEAD}`",
        f"Head: `{HEAD}`, not a different repository.",
        1,
    )
    assert validate_review_record(other_clause, payload) == []


def test_one_run_record_and_na_lane_shape() -> None:
    payload = _pass_payload()
    payload["lanes"] = _run("a")
    payload["passes"] = 1
    body = _body(payload, passes=1)
    assert validate_review_record(body, payload) == []
    assert "The second run may be omitted." in body
    evidence = f"markdown-only rebase, previously approved at {OTHER}"
    na = _pass_payload()
    na["lanes"] = _run("a", result="n_a", evidence=evidence)
    na_body = _body(na)
    assert validate_review_record(na_body, na) == []
    named = na_body.replace(
        "Lane `conformity-a`:\nResult: n_a\n",
        "Lane `conformity-a`:\nProvider: Acme\nResult: n_a\n",
        1,
    )
    assert "review n_a lane must not name a model" in validate_review_record(named, na)
    explained = na_body.replace(
        "Lane `conformity-a`:\nResult: n_a\n",
        "Lane `conformity-a`:\nResult: n_a\nMarkdown-only rebase, previously approved.\n",
        1,
    )
    assert validate_review_record(explained, na) == []


def test_set_aside_rejects_placeholder_and_accepts_none() -> None:
    payload = _pass_payload(passes=1)
    payload["lanes"] = _run("a")
    body = _body(payload, passes=1)
    assert validate_review_record(body, payload) == []
    dropped = body.replace("Set aside: none.", "Set aside: not recorded")
    assert validate_review_record(dropped, payload) == ["review set aside missing"]
    filed = body.replace(
        "Set aside: none.",
        "Set aside: none were filed and then dropped. The note did not cover this diff.",
    )
    assert validate_review_record(filed, payload) == []
    bullets = body.replace(
        "Set aside: none.",
        "Set aside:\n- A style note, not counted because it does not cover this diff.",
    )
    assert validate_review_record(bullets, payload) == []


def test_review_record_ignores_field_order_and_extra_lines() -> None:
    payload = _pass_payload(passes=1)
    payload["lanes"] = _run("a")
    body = _body(payload, passes=1)
    reordered = body.replace(
        f"Provider: {_RECORD_PROVIDER}\nModel: {_RECORD_MODEL}\n"
        f"Model number: {_RECORD_NUMBER}\nResult: pass\nPrompt:",
        f"Prompt:\nResult: pass, complete.\nModel number: {_RECORD_NUMBER}\n"
        f"Model: {_RECORD_MODEL}\nProvider: {_RECORD_PROVIDER}",
    )
    worded = reordered.replace(
        "Runs: `conformity-a` with `logic-a`. The second run may be omitted.",
        "Runs: logic-a with conformity-a (the other run is omitted).",
    )
    extra = worded.replace(
        "Set aside: none.\n",
        "Set aside: none.\n</details>\nNothing else was dropped.\n",
    )
    assert validate_review_record(extra, payload) == []
    contradicted = body.replace(
        f"Provider: {_RECORD_PROVIDER}\n",
        f"Provider: {_RECORD_PROVIDER}\nProvider: Someone else\n",
        1,
    )
    assert validate_review_record(contradicted, payload) == ["review provider missing"]


def test_na_result_wording_does_not_reject_an_explanation() -> None:
    evidence = f"markdown-only rebase, previously approved at {OTHER}"
    payload = _pass_payload()
    payload["lanes"] = _run("a", result="n_a", evidence=evidence)
    body = _body(payload)
    assert validate_review_record(body, payload) == []
    accepted = (
        "Result: n_a. It did not fail.",
        "Result: n_a. A pass was not required.",
        "Result: n_a. A pass wasn't required.",
        "Result: n_a. It didn't fail.",
        "Result: n_a. It doesn't fail.",
        "Result: n_a. It can't fail.",
        "Result: n_a. Ein Pass war nicht nötig.",
    )
    for line in accepted:
        worded = body.replace("Result: n_a\n", f"{line}\n")
        assert validate_review_record(worded, payload) == []
    rejected = (
        "Result: not n_a",
        "Result: this is not n_a",
        "Result: n_a but it passed",
        "Result: n_a was not recorded.",
        "Result: n_a wasn't recorded.",
        "Result: this isn't n_a.",
        "Result: this doesn't n_a.",
        "Result: don't n_a.",
        "Result: this can't n_a.",
        "Result: never n_a.",
        "Result: nicht n_a",
        "Result: kein n_a",
        "Result: keine n_a",
    )
    for line in rejected:
        worded = body.replace("Result: n_a\n", f"{line}\n")
        assert validate_review_record(worded, payload) == ["review result missing"]


def test_pass_result_wording_does_not_reject_an_explanation() -> None:
    payload = _pass_payload(passes=1)
    payload["lanes"] = _run("a")
    body = _body(payload, passes=1)
    assert validate_review_record(body, payload) == []
    accepted = (
        "Result: pass, no findings left.",
        "Result: pass. The second run was not required.",
        "Result: pass. It did not fail.",
        "Result: passed, no findings left.",
        "Result: **pass**, no findings left.",
        "Result: pass. n_a was not required.",
        "Result: pass. It didn't fail.",
        "Result: pass. It didn\u2019t fail.",
        "Result: pass. It doesn't fail.",
        "Result: pass. It can't fail.",
        "Result: pass. It won't fail.",
        "Result: pass. It cannot fail.",
        "Result: pass. It couldn't fail.",
        "Result: pass. It can never fail.",
        "Result: pass. I want the change.",
        "Result: pass, not fail",
        "Result: pass, nicht fail",
    )
    for line in accepted:
        worded = body.replace("Result: pass\n", f"{line}\n")
        assert validate_review_record(worded, payload) == []
    rejected = (
        "Result: not pass",
        "Result: not a pass",
        "Result: no",
        "Result: no pass",
        "Result: fail",
        "Result: n_a",
        "Result: this is not a pass",
        "Result: pass but fail",
        "Result: no, it passed",
        "Result: pass, n_a",
        "Result: pass n_a",
        "Result: pass was not achieved.",
        "Result: pass wasn't achieved.",
        "Result: this isn't a pass.",
        "Result: this doesn't pass.",
        "Result: pass. This doesn't pass.",
        "Result: don't pass.",
        "Result: this doesn\u2019t pass.",
        "Result: this can't pass.",
        "Result: pass. This can't pass.",
        "Result: this won't pass.",
        "Result: this cannot pass.",
        "Result: this couldn't pass.",
        "Result: never pass.",
        "Result: this can\u2019t pass.",
        "Result: nicht pass",
        "Result: kein pass",
        "Result: keine pass",
        "Result: nicht ein pass",
        "Result: das Pass ist nicht.",
    )
    for line in rejected:
        worded = body.replace("Result: pass\n", f"{line}\n")
        assert validate_review_record(worded, payload) == ["review result missing"]


def test_runs_and_final_result_follow_the_clause_rule() -> None:
    payload = _pass_payload(passes=1)
    payload["lanes"] = _run("a")
    body = _body(payload, passes=1)
    rendered_runs = (
        "Runs: `conformity-a` with `logic-a`. The second run may be omitted."
    )
    accepted_runs = (
        "Runs: `conformity-a` with `logic-a`, not a different repository. "
        "The second run may be omitted.",
    )
    for line in accepted_runs:
        worded = body.replace(rendered_runs, line)
        assert validate_review_record(worded, payload) == []
    rejected_runs = (
        "Runs: not conformity-a with logic-a. The second run may be omitted.",
        "Runs: not conformity-a with logic-a, conformity-a with logic-a",
    )
    for line in rejected_runs:
        worded = body.replace(rendered_runs, line)
        assert validate_review_record(worded, payload) == [
            "review runs line does not match"
        ]
    rendered_final = "Final result: `passes` 1, `defects` 0."
    accepted_final = (
        "Final result: `passes` 1, `defects` 0. It is not passes 2.",
        "Final result: `passes` 1, `defects` 0, not defects 1.",
        "Final result: `defects` 0, `passes` 1.",
        "Final result: defects 0, passes 1.",
        "Final result: defects 0 passes 1.",
        "Final result: 0 defects, 1 passes.",
        "Final result: defects 0, passes 1. It is not passes 2.",
    )
    for line in accepted_final:
        worded = body.replace(rendered_final, line)
        assert validate_review_record(worded, payload) == []
    rejected_final = (
        "Final result: not passes 1, defects 0",
        "Final result: passes 1, not defects 0",
        "Final result: passes 1, not passes 1, defects 0",
        "Final result: defects 0, not passes 1",
        "Final result: not defects 0, passes 1",
        "Final result: defects 1, passes 1",
        "Final result: defects 0, passes 2",
    )
    for line in rejected_final:
        worded = body.replace(rendered_final, line)
        assert validate_review_record(worded, payload) == [
            "review final result does not match"
        ]


def test_unreadable_commit_time_fails_closed() -> None:
    declaration = _comment(_body(_pass_payload(lanes=_run("a"))), cid=1, user=10)
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


def test_v2_original_holds_the_lane_record() -> None:
    prompt = "Confirm the added file is valid JSON and is not imported."
    lane = {
        "id": "conformity-a",
        "result": "pass",
        "status": "complete",
        "provider": "xAI",
        "model": "Grok",
        "model_number": "grok-4.7",
        "prompt": prompt,
    }
    payload = {
        "schema": "a38-review/v2",
        "head": HEAD,
        "passes": 1,
        "defects": 0,
        "set_aside": "none",
        "lanes": [lane, dict(lane, id="logic-a")],
    }
    body = _body(payload, passes=1)
    assert validate_declaration(payload, head=HEAD, markdown_only=False) == []
    assert validate_review_record(body, payload) == []
    drifted = dict(payload)
    drifted["lanes"] = [
        dict(lane, prompt="A different prompt that is still long enough."),
        dict(lane, id="logic-a"),
    ]
    assert "review record does not match the original" in validate_review_record(body, drifted)
    inline_only = body.replace(
        "```text\n" + prompt + "\n```\n",
        "Prompt: " + prompt + "\n",
    )
    inline_reasons = validate_review_record(inline_only, payload)
    assert "review prompt missing" in inline_reasons
    assert "review record does not match the original" in inline_reasons
    short = dict(payload)
    short["lanes"] = [{"id": "conformity-a", "result": "pass", "status": "complete"}]
    assert "review pass lane is malformed" in validate_declaration(
        short, head=HEAD, markdown_only=False
    )


def test_render_requires_pass_lane_facts() -> None:
    with pytest.raises(ReviewError, match="review lanes missing"):
        render_review_record({"head": HEAD})
    bare = _pass_payload()
    with pytest.raises(ReviewError, match="review pass lane is malformed"):
        render_review_record(bare)
    broken = _pass_payload()
    broken["lanes"] = ["nope"]
    with pytest.raises(ReviewError, match="review lane is not an object"):
        render_review_record(broken)
    invented = _body(bare).replace(f"Provider: {_RECORD_PROVIDER}\n", "Provider: Example\n", 1)
    assert validate_review_record(invented, bare) == ["review provider missing"]
    assert json.loads(_body(bare).split("```json\n", 1)[1].split("\n```", 1)[0]) == bare


def test_mixed_comment_is_neither_review_nor_report() -> None:
    mixed_body = (
        f"EN: measured\n{LOCAL_CI_BEGIN}\nLOCAL-OK\n{LOCAL_CI_END}\n"
        + _body(_pass_payload())
    )
    mixed = _comment(mixed_body, cid=1, user=10, created="2026-09-02T00:00:00Z")
    comments = [mixed]
    chosen, reasons = _select(comments)
    assert chosen is None
    assert reasons == ["local CI report and review record must be separate comments"]
    report, report_reason = choose_author_report(
        comments,
        10,
        committed_at="2026-08-01T00:00:00Z",
        meets=lambda body: "LOCAL-OK" in body,
    )
    assert report is None
    assert report_reason == "local CI report and review record must be separate comments"


def test_older_separate_comments_win_over_newer_mixed() -> None:
    report_body = f"EN: measured\n{LOCAL_CI_BEGIN}\nLOCAL-OK\n{LOCAL_CI_END}\n"
    review = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    report = _comment(report_body, cid=2, user=10, created="2026-09-01T00:00:00Z")
    mixed = _comment(
        report_body + _body(_pass_payload()),
        cid=3,
        user=10,
        created="2026-09-03T00:00:00Z",
    )
    comments = [review, report, mixed]
    chosen, reasons = _select(comments)
    assert chosen is review
    assert reasons == []
    picked, report_reason = choose_author_report(
        comments,
        10,
        committed_at="2026-08-01T00:00:00Z",
        meets=lambda body: "LOCAL-OK" in body,
    )
    assert picked is report
    assert report_reason is None


def test_separate_review_then_report_both_accepted() -> None:
    report_body = f"EN: measured\n{LOCAL_CI_BEGIN}\nLOCAL-OK\n{LOCAL_CI_END}\n"
    review = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    report = _comment(report_body, cid=2, user=10, created="2026-09-02T00:00:00Z")
    comments = [review, report]
    chosen, reasons = _select(comments)
    assert chosen is review
    assert reasons == []
    picked, report_reason = choose_author_report(
        comments,
        10,
        committed_at="2026-08-01T00:00:00Z",
        meets=lambda body: "LOCAL-OK" in body,
    )
    assert picked is report
    assert report_reason is None


def test_separate_report_then_review_both_accepted() -> None:
    report_body = f"EN: measured\n{LOCAL_CI_BEGIN}\nLOCAL-OK\n{LOCAL_CI_END}\n"
    report = _comment(report_body, cid=1, user=10, created="2026-09-01T00:00:00Z")
    review = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=2, user=10, created="2026-09-02T00:00:00Z"
    )
    comments = [report, review]
    chosen, reasons = _select(comments)
    assert chosen is review
    assert reasons == []
    picked, report_reason = choose_author_report(
        comments,
        10,
        committed_at="2026-08-01T00:00:00Z",
        meets=lambda body: "LOCAL-OK" in body,
    )
    assert picked is report
    assert report_reason is None


def test_select_first_run_then_later_second_run() -> None:
    first = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    second = _comment(
        _body(_pass_payload(lanes=_run("b"))), cid=2, user=10, created="2026-09-02T00:00:00Z"
    )
    chosen, reasons = _select([first, second])
    assert chosen is first
    assert reasons == []


def test_select_second_run_then_later_first_run() -> None:
    second = _comment(
        _body(_pass_payload(lanes=_run("b"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    first = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=2, user=10, created="2026-09-02T00:00:00Z"
    )
    chosen, reasons = _select([second, first])
    assert chosen is first
    assert reasons == []


def test_select_only_second_run_is_missing() -> None:
    second = _comment(_body(_pass_payload(lanes=_run("b"))), cid=1, user=10)
    chosen, reasons = _select([second])
    assert chosen is None
    assert reasons == ["review completion comment missing"]


def test_select_only_second_run_ready_lines_is_visible_text_missing() -> None:
    payload = _pass_payload(lanes=_run("b"))
    passes = payload["passes"]
    body = (
        _body(payload)
        .replace(
            f"Second review after {passes} review passes.",
            f"Ready after {passes} review passes.",
            1,
        )
        .replace(
            f"Zweiter Review nach {passes} Review-Durchläufen.",
            f"Bereit nach {passes} Review-Durchläufen.",
            1,
        )
    )
    second = _comment(body, cid=1, user=10)
    chosen, reasons = _select([second])
    assert chosen is None
    assert reasons == ["review visible text missing"]


def test_select_first_run_when_newer_second_run_says_ready() -> None:
    first = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    payload = _pass_payload(lanes=_run("b"))
    passes = payload["passes"]
    ready_second = _comment(
        _body(payload)
        .replace(
            f"Second review after {passes} review passes.",
            f"Ready after {passes} review passes.",
            1,
        )
        .replace(
            f"Zweiter Review nach {passes} Review-Durchläufen.",
            f"Bereit nach {passes} Review-Durchläufen.",
            1,
        ),
        cid=2,
        user=10,
        created="2026-09-02T00:00:00Z",
    )
    chosen, reasons = _select([first, ready_second])
    assert chosen is first
    assert reasons == []


def test_select_newer_both_runs_does_not_displace_first_run() -> None:
    first = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    both = _comment(
        _body(_pass_payload()), cid=2, user=10, created="2026-09-02T00:00:00Z"
    )
    chosen, reasons = _select([first, both])
    assert chosen is first
    assert reasons == []


def test_select_newer_malformed_second_run_does_not_displace_first_run() -> None:
    first = _comment(
        _body(_pass_payload(lanes=_run("a"))), cid=1, user=10, created="2026-09-01T00:00:00Z"
    )
    defects = _comment(
        _body(_pass_payload(defects=1, lanes=_run("b"))),
        cid=2,
        user=10,
        created="2026-09-02T00:00:00Z",
    )
    chosen, reasons = _select([first, defects])
    assert chosen is first
    assert reasons == []
    broken = _comment(
        "<!-- A38-REVIEW:v1 --> not json <!-- /A38-REVIEW:v1 -->",
        cid=3,
        user=10,
        created="2026-09-03T00:00:00Z",
    )
    chosen, reasons = _select([first, broken])
    assert chosen is first
    assert reasons == []


def test_select_only_both_runs_must_be_separate_comments() -> None:
    both = _comment(_body(_pass_payload()), cid=1, user=10)
    chosen, reasons = _select([both])
    assert chosen is None
    assert "review runs must be separate comments" in reasons


def test_validate_declaration_accepts_second_run_only() -> None:
    payload = _pass_payload(lanes=_run("b"))
    assert validate_declaration(payload, head=HEAD, markdown_only=False) == []


def test_codex_second_review_na_only_exact_evidence() -> None:
    markdown = f"markdown-only rebase, previously approved at {HEAD}"
    assert not codex_second_review_na("codex_pr_quality", "")
    assert not codex_second_review_na("codex_pr_logic", "")
    assert not codex_second_review_na("codex_pr_quality", markdown)
    assert not codex_second_review_na("codex_pr_logic", markdown)
    assert not codex_second_review_na("codex_pr_quality", "something else")
    assert not codex_second_review_na("grok_pr_quality", "second review not posted")
    assert not codex_second_review_na("grok_pr_logic", "second review not posted")
    assert codex_second_review_na("codex_pr_quality", "second review not posted")
    assert codex_second_review_na("codex_pr_logic", "second review not posted")
