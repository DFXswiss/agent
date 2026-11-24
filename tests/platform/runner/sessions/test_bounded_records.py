"""U-RUN-sessions (KOR-35, ADR-086 section 3): records beyond the event bound are clipped, not skipped; hook summaries
of the guest pass as `session.hook` events (the rig's hub checks every event against the contracts)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.crypto.encoding import body_json
from agent_runner.sessions import clip as clip_module
from agent_runner.sessions.clip import MARKER, clip

from ..reconcile.rig import Rig
from .kit import session_id
from .test_journal import consume, run, started

ESC = "\x1b" * 65536  # 64 Ki characters, 384 KiB of body JSON
TOOL = {
    "tool_use_id": "toolu_01",
    "name": "Bash",
    "input_summary": "ls",
    "input": "{}",
    "result_summary": "ok",
    "is_error": False,
    "duration_ms": 3,
}


def size(body: Any) -> int:
    return len(body_json(body))


def kept(value: str) -> int:
    """The characters of the original text a clipped value keeps."""
    head, _, rest = value.rpartition("\n[clipped: ")
    assert rest.endswith(" more characters]")
    return len(head)


def test_a_body_that_fits_is_unchanged() -> None:
    body = {**TOOL, "result_summary": "x" * 1000}
    assert clip("session.tool", body) is body
    big = {"event": "Stop", "decision": "none", "reason": ESC}
    assert clip("session.hook", big) is big  # no clippable field: left to the seal checks
    surrogate = {**TOOL, "input": "\ud800" + ESC}
    assert clip("session.tool", surrogate) is surrogate  # no body JSON at all: refused later as before


def test_the_largest_field_is_clipped_to_the_longest_fitting_prefix() -> None:
    body = {**TOOL, "result_summary": ESC, "input": "x" * 1000}
    clipped = clip("session.tool", body)
    assert size(clipped) <= clip_module.MAX_BODY
    keep = kept(clipped["result_summary"])
    assert clipped["result_summary"] == ESC[:keep] + MARKER.format(n=65536 - keep)
    longer = {**clipped, "result_summary": ESC[: keep + 1] + MARKER.format(n=65536 - keep - 1)}
    assert size(longer) > clip_module.MAX_BODY
    assert clipped["input"] == "x" * 1000
    assert "truncated" not in clipped
    assert body["result_summary"] == ESC  # the record itself is not changed


def test_a_field_that_cannot_fit_becomes_its_marker_and_the_next_one_is_clipped() -> None:
    body = {**TOOL, "input": ESC, "result_summary": ESC[:50000], "input_summary": 3}
    clipped = clip("session.tool", body)
    assert clipped["input"] == MARKER.format(n=65536)
    assert 0 < kept(clipped["result_summary"]) < 50000
    assert size(clipped) <= clip_module.MAX_BODY
    assert clipped["input_summary"] == 3  # not a text: left alone


def test_an_output_is_clipped_and_marked_truncated() -> None:
    body = {"message_id": "m1", "role": "assistant", "text": ESC, "turn": 1}
    clipped = clip("session.output", body)
    assert clipped["truncated"] is True
    assert size(clipped) <= clip_module.MAX_BODY
    assert len(clipped["text"]) <= 65536


@pytest.mark.parametrize("limit", [40, 300])
def test_a_body_that_cannot_fit_keeps_its_markers(limit: int) -> None:
    body = {**TOOL, "tool_use_id": "t" * 100, "input": "x" * 200, "result_summary": "y" * 200}
    clipped = clip("session.tool", body, limit)
    if limit == 40:
        assert clipped["input"] == MARKER.format(n=200)
        assert clipped["result_summary"] == MARKER.format(n=200)
        assert size(clipped) > limit
    else:
        assert size(clipped) <= limit
    assert clipped["input_summary"] == "ls"  # its marker would be longer
    tiny = {**TOOL, "input": "ab", "result_summary": "ab"}
    assert clip("session.tool", tiny, size(tiny) - 1) == tiny  # never longer than before


def test_oversized_records_arrive_clipped_instead_of_skipped(rig: Rig) -> None:
    async def scenario(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        found.guest.emit(sid, "session.tool", {**TOOL, "input": ESC, "result_summary": ESC})
        found.guest.emit(sid, "session.output", {"message_id": "m1", "role": "assistant", "text": ESC, "turn": 1})
        await consume(found, reader)
        assert reader.skipped == 0
        events = {header["kind"]: body for header, body in await found.pushed_events()}
        assert events["session.tool"]["input"] == MARKER.format(n=65536)
        assert kept(events["session.tool"]["result_summary"]) > 0
        assert events["session.output"]["truncated"] is True

    run(rig, scenario)


def test_hook_summaries_pass_as_events_without_audit(rig: Rig) -> None:
    async def scenario(env: Any) -> None:
        found, reader = await started(env)
        sid = session_id()
        summary = {
            "event": "summary",
            "decision": "allow",
            "counts": {"PreToolUse": 5, "PostToolUse": 4},
            "tools": {"Read": 5},
            "from": "2026-11-24T10:00:00.000Z",
            "to": "2026-11-24T10:00:09.000Z",
        }
        found.guest.emit(sid, "session.hook", summary)
        await consume(found, reader)
        assert reader.skipped == 0
        hooks = [body for header, body in await found.pushed_events() if header["kind"] == "session.hook"]
        assert hooks == [summary]
        assert [audit for audit in await found.of_kind("audit") if audit["action"] == "hook.denied"] == []

    run(rig, scenario)
