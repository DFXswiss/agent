"""U-GST-transcript: transcript parser and follower (05-runner.md section 6.4, 03-api.md section 7.2; GST-05).

The fixtures under ``fixtures/transcript/`` are anonymised transcript lines of Claude Code 2.1.330 recorded in
Spike C against the model double (paths replaced, request blobs and environment attachments removed).
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import transcript
from agent_guest.control import ControlContext
from agent_guest.state import Session
from agent_guest.transcript import LineReader, Record, TranscriptFollower, TranscriptParser

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
SID = "01a1ee56-5e00-7003-8f1b-340691d99f03"
CLAUDE = "8e9d794a-70a5-4d82-bbd5-531bd6272078"
OTHER_CLAUDE = "5d0c9a3e-7f61-4b6e-9a42-3c1d2e4f5a6b"


def fixture(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]


def parse(entries: list[dict[str, Any]]) -> tuple[TranscriptParser, list[Record]]:
    parser = TranscriptParser()
    records: list[Record] = []
    for entry in entries:
        records.extend(parser.feed(entry))
    records.extend(parser.finish())
    return parser, records


def assistant(
    message_id: str, *blocks: dict[str, Any], stamp: str = "2026-11-21T10:00:00.000Z", **extra: Any
) -> dict[str, Any]:
    message = {"id": message_id, "role": "assistant", "content": list(blocks), "stop_reason": "end_turn"}
    return {"type": "assistant", "message": message, "timestamp": stamp, "sessionId": CLAUDE, **extra}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str, tool_input: object) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def result(tool_id: str, content: object, stamp: str = "2026-11-21T10:00:01.250Z", **extra: Any) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content, **extra}
    return {"type": "user", "message": {"role": "user", "content": [block]}, "timestamp": stamp}


def prompt(value: object = "Hello", **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": value}, **extra}


# -- parser with recorded fixtures ------------------------------------------------------------------------------


def test_text_and_tool_pairs_of_a_recorded_session() -> None:
    parser, records = parse(fixture("tools"))
    assert parser.claude_session_id == CLAUDE
    assert parser.turn == 2
    outputs = [record.body for record in records if record.kind == "session.output"]
    assert outputs == [
        {
            "message_id": "msg_double_0001",
            "role": "assistant",
            "text": "I will create notes.txt first.",
            "turn": 1,
            "stop_reason": "tool_use",
        },
        {
            "message_id": "msg_double_0007",
            "role": "assistant",
            "text": "SPIKE-E2E-1 done: notes.txt now reads alpha/gamma, 2+3=5, task task-0001.",
            "turn": 1,
            "stop_reason": "end_turn",
        },
        {
            "message_id": "msg_double_0008",
            "role": "assistant",
            "text": "SPIKE-E2E-2 remembered: alpha/gamma and sum 5.",
            "turn": 2,
            "stop_reason": "end_turn",
        },
    ]
    tools = {record.body["tool_use_id"]: record for record in records if record.kind == "session.tool"}
    assert [tools[key].body["name"] for key in tools] == [
        "Write", "Read", "Edit", "Glob", "Grep", "mcp__spike__add", "mcp__spike__task_context"
    ]  # fmt: skip
    assert {key for key, record in tools.items() if record.hook_owned} == {
        "toolu_double_write_1",
        "toolu_double_edit_0",
    }
    add = tools["toolu_double_mcp_0"].body
    assert add == {
        "tool_use_id": "toolu_double_mcp_0",
        "name": "mcp__spike__add",
        "input_summary": '{"a":2,"b":3}',
        "input": '{"a": 2, "b": 3}',
        "result_summary": "sum=5",
        "is_error": False,
        "duration_ms": add["duration_ms"],
    }
    assert 0 <= add["duration_ms"] < 1000
    assert tools["toolu_double_read_0"].body["input_summary"] == f"/srv/sessions/{SID}/work/notes.txt"
    assert tools["toolu_double_write_1"].body["result_summary"].startswith("File created successfully")


def test_a_failed_tool_call_is_an_error_and_not_left_to_the_hook() -> None:
    _, records = parse(fixture("tool-error"))
    (bash,) = [record for record in records if record.kind == "session.tool"]
    assert bash.body["name"] == "Bash"
    assert bash.body["is_error"] is True
    assert bash.body["result_summary"].startswith("<tool_use_error>Error: No such tool available: Bash.")
    assert bash.hook_owned is False


def test_an_api_error_answer_is_shown_and_system_lines_end_the_message() -> None:
    _, records = parse(fixture("api-error"))
    assert [(record.kind, record.body["message_id"], record.body["text"]) for record in records] == [
        ("session.output", "d77e0ec3-e6b6-4e78-8124-4f30ccfc15f8", "Invalid API key · Fix external API key")
    ]


def test_meta_user_lines_do_not_start_a_turn() -> None:
    parser, records = parse(fixture("meta-turn"))
    assert [(record.body["text"], record.body["turn"], record.body["stop_reason"]) for record in records] == [
        ("partial", 1, "max_tokens"),
        ("SPIKE-OK", 1, "end_turn"),
    ]
    assert parser.turn == 1


def test_every_fixture_body_matches_the_journal_contract(context: ControlContext) -> None:
    for name in ("tools", "tool-error", "api-error", "meta-turn"):
        for record in parse(fixture(name))[1]:
            assert context.schemas.errors(f"guest/journal/{record.kind}.json", record.body) == []


# -- parser details -----------------------------------------------------------------------------------------------


def test_blocks_of_one_message_are_joined_and_late_blocks_become_a_further_record() -> None:
    parser = TranscriptParser()
    assert parser.feed(prompt()) == []
    assert parser.feed(assistant("msg_1", text("one"), {"type": "thinking", "thinking": "hidden"})) == []
    assert parser.feed(assistant("msg_1", text("two"), "not a block")) == []  # type: ignore[arg-type]
    assert not parser.idle
    (first,) = parser.feed({"type": "last-prompt"})
    assert first.body == {"message_id": "msg_1", "role": "assistant", "text": "one\n\ntwo", "turn": 1,
                          "stop_reason": "end_turn"}  # fmt: skip
    parser.feed(assistant("msg_1", text("three")))
    (second,) = parser.flush()
    assert second.body["message_id"] == "msg_1-2"
    assert second.key == "msg_1-2"
    assert parser.idle


def test_a_new_message_id_ends_the_previous_message() -> None:
    parser = TranscriptParser()
    assert parser.feed(assistant("msg_1", text("a"))) == []
    (record,) = parser.feed(assistant("msg_2", text("b")))
    assert record.body["text"] == "a"
    assert record.body["turn"] == 1  # before any prompt the turn counts as 1
    assert [r.body["text"] for r in parser.flush()] == ["b"]


def test_messages_without_text_and_unknown_entries_write_nothing() -> None:
    parser = TranscriptParser()
    assert parser.feed(assistant("msg_1", tool_use("toolu_1", "Read", {"file_path": "a"}))) == []
    assert parser.feed({"type": "queue-operation", "operation": "enqueue"}) == []
    assert parser.feed({"type": "attachment", "message": {"content": []}}) == []
    assert parser.feed({"type": "system", "subtype": "api_error"}) == []
    assert parser.flush() == []


def test_message_ids_are_made_safe_or_taken_from_the_entry() -> None:
    parser = TranscriptParser()
    parser.feed(assistant("msg with/odd:chars" + "x" * 200, text("a")))
    (odd,) = parser.flush()
    assert odd.body["message_id"] == ("msg_with_odd_chars" + "x" * 200)[:120]
    entry = assistant("", text("b"), uuid="3c84b254-1111-4222-8333-444455556666")
    entry["message"]["id"] = None
    parser.feed(entry)
    (from_uuid,) = parser.flush()
    assert from_uuid.body["message_id"] == "3c84b254-1111-4222-8333-444455556666"
    nameless = assistant("", text("c"))
    del nameless["message"]["id"]
    parser.feed(nameless)
    assert parser.flush()[0].body["message_id"] == "message"


def test_stop_reasons_outside_the_code_pattern_are_left_out() -> None:
    parser = TranscriptParser()
    entry = assistant("msg_1", text("a"))
    entry["message"]["stop_reason"] = "Not A Code"
    parser.feed(entry)
    assert "stop_reason" not in parser.flush()[0].body


def test_side_chains_and_entries_without_message_are_ignored() -> None:
    parser = TranscriptParser()
    assert parser.feed(assistant("msg_1", text("sub"), isSidechain=True)) == []
    assert parser.feed({"type": "assistant", "message": "text"}) == []
    assert parser.feed({"type": "assistant", "message": {"content": "plain"}}) == []
    assert parser.flush() == []


def test_turns_count_prompts_but_not_meta_lines_tool_results_or_other_content() -> None:
    parser = TranscriptParser()
    parser.feed(prompt("first"))
    parser.feed(prompt([text("second as blocks"), "junk"]))
    parser.feed(prompt("meta", isMeta=True))
    parser.feed(prompt(None))
    parser.feed(prompt([{"type": "image"}]))
    assert parser.turn == 2


def test_long_texts_are_cut_at_64_kib_on_a_character_boundary() -> None:
    parser = TranscriptParser()
    parser.feed(assistant("msg_1", text("€" * 30000)))
    (record,) = parser.flush()
    body = record.body
    assert body["truncated"] is True
    assert len(body["text"].encode("utf-8")) <= transcript.MAX_TEXT
    assert body["text"] == "€" * (transcript.MAX_TEXT // 3)


def test_large_tool_input_and_output_are_cut_and_the_summary_is_bounded() -> None:
    parser = TranscriptParser()
    parser.feed(assistant("msg_1", tool_use("toolu_big", "Write", {"file_path": "p" * 400, "content": "x" * 70000})))
    (record,) = parser.feed(result("toolu_big", [text("y" * 70000), {"type": "image"}, {"no": "type"}]))
    body = record.body
    assert len(body["input"]) == transcript.MAX_TEXT
    assert body["result_summary"] == "y" * transcript.MAX_TEXT
    assert body["input_summary"] == "p" * transcript.MAX_SUMMARY


def test_tool_results_of_any_shape_become_text() -> None:
    parser = TranscriptParser()
    for index, content in enumerate(["plain", [text("a"), {"type": "image"}, {"type": 3}], {"k": 1}, None, 7]):
        parser.feed(assistant(f"msg_{index}", tool_use(f"toolu_{index}", "mcp__platform__get_context", {})))
        (record,) = parser.feed(result(f"toolu_{index}", content))
        assert record.body["result_summary"] == ["plain", "a\n[image]", '{"k": 1}', "", "7"][index]


def test_summaries_prefer_paths_patterns_and_operation_types() -> None:
    assert transcript.summary({"pattern": "*.py", "path": ""}) == "*.py"
    assert transcript.summary({"type": "github.create_pull_request", "params": {}}) == "github.create_pull_request"
    assert transcript.summary(["a", 1]) == '["a",1]'
    assert transcript.summary({"file_path": 3}) == '{"file_path":3}'


def test_durations_come_from_the_entry_timestamps() -> None:
    parser = TranscriptParser()
    parser.feed(assistant("msg_1", tool_use("toolu_1", "Read", {}), stamp="2026-11-21T10:00:00.000Z"))
    (timed,) = parser.feed(result("toolu_1", "ok", stamp="2026-11-21T10:00:01.250Z"))
    assert timed.body["duration_ms"] == 1250
    parser.feed(assistant("msg_2", tool_use("toolu_2", "Read", {}), stamp="not a time"))
    (untimed,) = parser.feed(result("toolu_2", "ok"))
    assert untimed.body["duration_ms"] == 0
    parser.feed(assistant("msg_3", tool_use("toolu_3", "Read", {}), stamp="2026-11-21T10:00:05.000Z"))
    backwards = result("toolu_3", "ok", stamp="2026-11-21T10:00:01.000Z")
    assert parser.feed(backwards)[0].body["duration_ms"] == 0
    parser.feed(assistant("msg_4", tool_use("toolu_4", "Read", {})))
    stampless = result("toolu_4", "ok")
    del stampless["timestamp"]
    assert parser.feed(stampless)[0].body["duration_ms"] == 0


def test_unknown_results_bad_ids_and_bad_names_are_skipped() -> None:
    parser = TranscriptParser()
    assert parser.feed(result("toolu_unknown", "x")) == []
    assert parser.feed(result(None, "x")) == []  # type: ignore[arg-type]
    parser.feed(assistant("msg_1", tool_use("bad id!", "Read", {}), tool_use("toolu_n", 5, {})))  # type: ignore[arg-type]
    parser.feed(assistant("msg_2", tool_use("toolu_bad_name", "Bad Name!", {})))
    assert parser.feed(result("toolu_bad_name", "x")) == []
    assert parser.idle


def test_open_tool_calls_are_written_as_interrupted_when_the_session_ends() -> None:
    parser = TranscriptParser()
    parser.feed(assistant("msg_1", text("working"), tool_use("toolu_b", "Grep", {"pattern": "x"})))
    parser.feed(assistant("msg_1", tool_use("toolu_a", "Bad Name!", {})))
    records = parser.finish()
    assert [record.kind for record in records] == ["session.output", "session.tool"]
    assert records[1].body | {"duration_ms": 0} == {
        "tool_use_id": "toolu_b",
        "name": "Grep",
        "input_summary": "x",
        "input": '{"pattern": "x"}',
        "result_summary": "interrupted",
        "is_error": True,
        "duration_ms": 0,
    }
    assert parser.idle


def test_lone_surrogates_never_reach_the_journal() -> None:
    assert transcript.cut("a\ud800b") == ("a?b", False)
    assert transcript.cwd_key(f"/srv/sessions/{SID}/work") == f"-srv-sessions-{SID}-work"


# -- line reader --------------------------------------------------------------------------------------------------


@pytest.fixture
def growing(tmp_path: Path) -> tuple[Path, Any]:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"")

    def read(reader: LineReader) -> list[bytes]:
        fd = os.open(path, os.O_RDONLY)
        try:
            return reader.read(fd, os.fstat(fd))
        finally:
            os.close(fd)

    return path, read


def test_partial_lines_wait_for_their_end(growing: tuple[Path, Any]) -> None:
    path, read = growing
    reader = LineReader()
    path.write_bytes(b'{"a":1}\n{"b":')
    assert read(reader) == [b'{"a":1}']
    with path.open("ab") as handle:
        handle.write(b"2}\n")
    assert read(reader) == [b'{"b":2}']
    assert read(reader) == []


def test_a_truncated_or_replaced_file_is_read_from_its_start(growing: tuple[Path, Any]) -> None:
    path, read = growing
    reader = LineReader()
    path.write_bytes(b"one\ntwo\n")
    assert read(reader) == [b"one", b"two"]
    path.write_bytes(b"3\n")
    assert read(reader) == [b"3"]
    replacement = path.with_name("new")
    replacement.write_bytes(b"x\ny\nzz\n")
    os.replace(replacement, path)
    assert read(reader) == [b"x", b"y", b"zz"]


def test_an_endless_line_is_dropped_up_to_its_end(growing: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    path, read = growing
    monkeypatch.setattr(transcript, "MAX_LINE", 10)
    monkeypatch.setattr(transcript, "READ_CHUNK", 8)
    reader = LineReader()
    path.write_bytes(b"ok\n" + b"x" * 30)
    assert read(reader) == [b"ok"]
    with path.open("ab") as handle:
        handle.write(b"xx\nafter\n")
    assert read(reader) == [b"after"]


def test_entries_skips_invalid_lines() -> None:
    assert transcript.entries([b'{"a": 1}', b"{broken", b"[1]", b""]) == [{"a": 1}]


# -- follower -----------------------------------------------------------------------------------------------------


class Ticks:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / SID
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def project(base: Path) -> Path:
    path = base / "home" / ".claude" / "projects" / transcript.cwd_key(f"{base}/work")
    path.mkdir(parents=True)
    return path


@pytest.fixture
def starting(context: ControlContext, base: Path) -> Session:
    session = Session(
        id=SID,
        uid=os.getuid(),
        token_sha256="0" * 64,
        state="agent_starting",
        workspace=f"{base}/work",
        claude_session_id=None,
        created_at="2026-11-21T10:00:00.000Z",
    )
    context.state.save_session(session)
    return session


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


@pytest.fixture
def follower(context: ControlContext, starting: Session, base: Path, ticks: Ticks) -> TranscriptFollower:
    return TranscriptFollower(context.journal, context.state, SID, str(base), os.getuid(), monotonic=ticks, poll=0.01)


def journal(context: ControlContext, *kinds: str) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 1000)
    return [record for record in records if not kinds or record["kind"] in kinds]


def write(path: Path, *entries: dict[str, Any], partial: str = "") -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(entry) + "\n" for entry in entries) + partial)


def test_follower_journals_the_transcript_and_reports_the_tui_and_claude_session(
    context: ControlContext, follower: TranscriptFollower, project: Path, ticks: Ticks
) -> None:
    assert follower.step() == 0  # no transcript yet
    lines = (FIXTURES / "tools.jsonl").read_text(encoding="utf-8")
    head, _, tail = lines.partition('"msg_double_0007"')
    (project / f"{CLAUDE}.jsonl").write_text(head, encoding="utf-8")
    follower.step()
    states = journal(context, "session.state")
    assert [(r["body"]["state"], r["body"].get("claude_session_id")) for r in states] == [("prompt_delivering", CLAUDE)]
    session = context.state.session(SID)
    assert session is not None
    assert (session.state, session.claude_session_id) == ("prompt_delivering", CLAUDE)
    with (project / f"{CLAUDE}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('"msg_double_0007"' + tail)
    follower.step()
    ticks.value += transcript.QUIET_SECONDS
    follower.step()
    kinds = [record["kind"] for record in journal(context, "session.output", "session.tool")]
    assert kinds.count("session.output") == 3
    # Write and Edit wait for the PostToolUse hook; the hook record of the Write arrives in time.
    hook_record = {"tool_use_id": "toolu_double_write_1", "name": "Write", "input_summary": "notes.txt",
                   "input": "{}", "result_summary": "", "is_error": False, "duration_ms": 1}  # fmt: skip
    context.journal.append("session.tool", hook_record, SID)
    assert kinds.count("session.tool") == 5
    ticks.value += transcript.HOOK_GRACE_SECONDS
    assert follower.step() == 1  # only the Edit, whose hook record never came
    tools = [record["body"]["tool_use_id"] for record in journal(context, "session.tool")]
    assert tools.count("toolu_double_write_1") == 1
    assert tools.count("toolu_double_edit_0") == 1
    assert follower.finish() == 0
    for record in journal(context):
        assert context.schemas.errors(f"guest/journal/{record['kind']}.json", record["body"]) == []


def test_a_restarted_follower_skips_what_the_journal_holds(
    context: ControlContext, follower: TranscriptFollower, project: Path, base: Path, ticks: Ticks
) -> None:
    (project / f"{CLAUDE}.jsonl").write_text((FIXTURES / "tool-error.jsonl").read_text(encoding="utf-8"))
    assert follower.finish() == 2
    again = TranscriptFollower(context.journal, context.state, SID, str(base), os.getuid(), monotonic=ticks)
    assert again.finish() == 0
    assert again._reported == CLAUDE  # the file name, not the sessionId inside the lines


def test_the_known_claude_session_is_preferred_and_a_newer_file_is_switched_to(
    context: ControlContext, starting: Session, base: Path, project: Path, ticks: Ticks
) -> None:
    context.state.save_session(replace(starting, state="working", claude_session_id=OTHER_CLAUDE))
    known, newer = project / f"{OTHER_CLAUDE}.jsonl", project / f"{CLAUDE}.jsonl"
    write(known, prompt(), assistant("msg_old", text("old")), partial='{"type":"assistant"')
    write(newer, prompt())
    os.utime(known, ns=(1, 1))
    follower = TranscriptFollower(context.journal, context.state, SID, str(base), os.getuid(), monotonic=ticks)
    follower.step()
    assert follower.current == known.name
    write(newer, assistant("msg_new", text("new"), stamp="2026-11-21T10:00:09.000Z"))
    follower.step()
    assert follower.current == newer.name
    follower.finish()
    assert [r["body"]["text"] for r in journal(context, "session.output")] == ["old", "new"]
    # The new Claude session ID is reported in the current state (no transition).
    assert [(r["body"]["state"], r["body"].get("claude_session_id")) for r in journal(context, "session.state")] == [
        ("working", CLAUDE)
    ]


def test_files_that_are_not_the_sessions_transcripts_are_never_read(
    context: ControlContext, follower: TranscriptFollower, project: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.jsonl"
    write(secret, assistant("msg_secret", text("secret")))
    (project / f"{CLAUDE}.jsonl").symlink_to(secret)
    (project / "notes.jsonl").write_text("{}\n")
    (project / f"{OTHER_CLAUDE}.jsonl").mkdir()
    follower.finish()
    assert journal(context, "session.output") == []
    assert follower.current is None


def test_a_symlinked_project_directory_is_not_followed(
    context: ControlContext, follower: TranscriptFollower, base: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write(elsewhere / f"{CLAUDE}.jsonl", assistant("msg_x", text("x")))
    (base / "home" / ".claude" / "projects").mkdir(parents=True)
    (base / "home" / ".claude" / "projects" / transcript.cwd_key(f"{base}/work")).symlink_to(elsewhere)
    assert follower.finish() == 0


def test_files_owned_by_somebody_else_are_ignored(
    context: ControlContext, starting: Session, base: Path, project: Path, ticks: Ticks
) -> None:
    write(project / f"{CLAUDE}.jsonl", assistant("msg_x", text("x")))
    foreign = TranscriptFollower(context.journal, context.state, SID, str(base), os.getuid() + 1, monotonic=ticks)
    assert foreign.finish() == 0
    assert foreign.current is None


def test_a_vanished_transcript_is_kept_and_read_again_when_it_comes_back(
    context: ControlContext, follower: TranscriptFollower, project: Path, ticks: Ticks
) -> None:
    path = project / f"{CLAUDE}.jsonl"
    write(path, prompt(), assistant("msg_1", text("one")))
    follower.step()
    path.unlink()
    assert follower.step() == 0
    assert follower.current == path.name
    write(path, prompt(), assistant("msg_2", text("two")))
    follower.finish()
    assert [(r["body"]["text"], r["body"]["turn"]) for r in journal(context, "session.output")] == [
        ("one", 1),
        ("two", 2),
    ]


def test_unreadable_transcripts_are_tolerated(
    context: ControlContext, follower: TranscriptFollower, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / f"{CLAUDE}.jsonl"
    write(path, prompt(), assistant("msg_1", text("one")))
    real_stat = os.stat

    def failing_stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if name == path.name:
            raise PermissionError(name)
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)
    assert follower.step() == 0
    assert follower.current is None
    monkeypatch.undo()

    def refused(*args: Any) -> Any:
        raise PermissionError("refused")

    monkeypatch.setattr(transcript, "open_file", refused)
    assert follower.finish() == 0
    assert follower.current == path.name


def test_tui_up_moves_only_a_starting_session_and_never_an_unknown_one(
    context: ControlContext, starting: Session
) -> None:
    state, log = context.state, context.journal
    assert transcript.tui_up(log, state, "01a1ee56-5e00-7003-8f1b-340691d99fff") is False
    assert transcript.tui_up(log, state, SID) is True
    assert transcript.tui_up(log, state, SID) is False  # already prompt_delivering, no ID
    assert transcript.tui_up(log, state, SID, CLAUDE) is True
    assert transcript.tui_up(log, state, SID, CLAUDE) is False
    context.state.save_session(replace(starting, state="stopping"))
    assert transcript.tui_up(log, state, SID, OTHER_CLAUDE) is False
    bodies = [record["body"] for record in journal(context, "session.state")]
    assert [(body["state"], body.get("claude_session_id")) for body in bodies] == [
        ("prompt_delivering", None),
        ("prompt_delivering", CLAUDE),
    ]


def test_the_follower_thread_polls_until_stopped(
    context: ControlContext, follower: TranscriptFollower, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(project / f"{CLAUDE}.jsonl", prompt(), assistant("msg_1", text("live")), {"type": "last-prompt"})
    follower.start()
    deadline = 3000  # up to 30 s (a loaded device), returns as soon as reached
    while not journal(context, "session.output") and deadline:
        deadline -= 1
        follower._stop.wait(0.01)
    assert [r["body"]["text"] for r in journal(context, "session.output")] == ["live"]
    calls: list[int] = []

    def broken() -> int:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(follower, "step", broken)
    while not calls:
        follower._stop.wait(0.01)
    follower.stop(final=False)
    stopped = TranscriptFollower(context.journal, context.state, SID, follower.base, os.getuid())
    stopped.stop()


def test_text_beyond_the_limit_is_not_kept_in_memory() -> None:
    parser = TranscriptParser()
    parser.feed(assistant("msg_1", text("a" * (transcript.MAX_TEXT + 1))))
    parser.feed(assistant("msg_1", text("never kept")))
    (record,) = parser.flush()
    assert record.body["truncated"] is True
    assert "never kept" not in record.body["text"]


def test_entries_read_again_count_once() -> None:
    parser = TranscriptParser()
    first = [prompt("Hi", uuid="u-1"), assistant("msg_1", text("a"), uuid="u-2")]
    for entry in first + first:
        parser.feed(entry)
    assert parser.turn == 1
    assert [record.body["message_id"] for record in parser.flush()] == ["msg_1"]


def test_a_poll_reads_a_bounded_amount_and_finish_reads_the_rest(
    context: ControlContext, follower: TranscriptFollower, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcript, "MAX_READ", 64)
    monkeypatch.setattr(transcript, "READ_CHUNK", 64)
    path = project / f"{CLAUDE}.jsonl"
    write(path, prompt(), *[assistant(f"msg_{n}", text(f"t{n}")) for n in range(5)])
    follower.step()
    assert follower.reader.more is True
    follower.finish()
    assert [r["body"]["text"] for r in journal(context, "session.output")] == [f"t{n}" for n in range(5)]


def test_a_stuck_poll_is_never_joined_by_a_concurrent_finish(
    context: ControlContext, follower: TranscriptFollower, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stuck:
        def join(self, timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    follower._thread = Stuck()  # type: ignore[assignment]
    follower.finish = lambda: pytest.fail("finish must not run")  # type: ignore[method-assign]
    follower.stop()
    assert "did not stop in time" in capsys.readouterr().err
