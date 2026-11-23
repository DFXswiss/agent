"""U-RUN-workvm jobs: the runner's side of the job helper in the work VM (13-work-vm.md sections 3 and 5).

Every helper answer is untrusted: the tests feed crafted `ShellResult`s to the parsers and check that anything outside
the closed answer format becomes `workvm_answer_invalid`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from agent_runner.workvm import jobs
from agent_runner.workvm.jobs import JobView, ShellResult, WorkVmError

JOB = "j-0123456789ab"


def header(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "state": "exited",
        "exit_code": 0,
        "output_bytes": 5,
        "output_start": 0,
        "body_bytes": 5,
        "duration_ms": 12,
    }
    value.update(changes)
    return value


def answer(head: object, body: bytes = b"hello", *, returncode: int = 0, truncated: bool = False) -> ShellResult:
    return ShellResult(returncode, json.dumps(head).encode() + b"\n" + body, truncated)


def wait(result: ShellResult, limit: int = 100, command: str = "echo hello") -> JobView:
    return jobs.parse_wait(result, job_id=JOB, command=command, limit=limit)


def invalid(result: ShellResult, limit: int = 100) -> None:
    with pytest.raises(WorkVmError) as caught:
        wait(result, limit)
    assert caught.value.code == "workvm_answer_invalid"


def test_new_job_id_matches_the_pattern() -> None:
    first, second = jobs.new_job_id(), jobs.new_job_id()
    assert jobs.JOB_ID_RE.fullmatch(first)
    assert jobs.JOB_ID_RE.fullmatch(second)
    assert first != second


def test_text_replaces_invalid_utf8_and_nul() -> None:
    assert jobs.text(b"a\x00b\xffc") == "a�b�c"
    assert jobs.text("grüezi".encode()) == "grüezi"


def test_command_label_is_the_first_256_characters_without_nul_or_surrogates() -> None:
    assert jobs.command_label("x" * 300) == "x" * 256
    assert jobs.command_label("a\x00b") == "a�b"
    assert jobs.command_label("a\udc80b") == "a?b"


def test_parse_wait_returns_the_job() -> None:
    view = wait(answer(header(state="running", exit_code=None, output_bytes=20, output_start=10), b"he\x00l\xff"))
    assert view == JobView(
        job_id=JOB,
        command="echo hello",
        state="running",
        exit_code=None,
        output="he�l�",
        output_start=10,
        output_bytes=20,
        duration_ms=12,
    )
    assert view.result() == {
        "job_id": JOB,
        "command": "echo hello",
        "state": "running",
        "exit_code": None,
        "output": "he�l�",
        "output_start": 10,
        "output_bytes": 20,
        "duration_ms": 12,
    }


@pytest.mark.parametrize("state", sorted(jobs.STATES))
def test_parse_wait_accepts_every_state(state: str) -> None:
    assert wait(answer(header(state=state, exit_code=255))).state == state


def test_parse_wait_accepts_bounds() -> None:
    view = wait(answer(header(output_bytes=jobs.MAX_SAFE, output_start=jobs.MAX_SAFE - 5, duration_ms=0)), limit=5)
    assert (view.output_start, view.output_bytes, view.duration_ms) == (jobs.MAX_SAFE - 5, jobs.MAX_SAFE, 0)
    empty = wait(answer(header(output_bytes=0, body_bytes=0), b""))
    assert empty.output == ""


def test_parse_wait_header_of_exactly_the_limit() -> None:
    head = header()
    line = json.dumps(head).encode()
    padded = line[:-1] + b" " * (jobs.HEADER_LIMIT - len(line)) + b"}"
    assert len(padded) == jobs.HEADER_LIMIT
    assert wait(ShellResult(0, padded + b"\nhello", False)).output == "hello"
    invalid(ShellResult(0, padded[:-1] + b" }\nhello", False))


def test_parse_wait_unknown_job() -> None:
    with pytest.raises(WorkVmError) as caught:
        wait(answer({"error": "not_found"}, b""))
    assert caught.value.code == "workvm_job_unknown"


@pytest.mark.parametrize(
    "result",
    [
        answer(header(), returncode=1),
        answer(header(), truncated=True),
        ShellResult(0, b"no newline at all", False),
        ShellResult(0, b"\xff\xfe\n", False),
        ShellResult(0, b"{not json\n", False),
        ShellResult(0, b"[1, 2]\n", False),
        answer({"error": "other"}, b""),
        answer({**header(), "extra": 1}),
        answer({k: v for k, v in header().items() if k != "state"}),
        answer(header(state="paused")),
        answer(header(state=["exited"])),
        answer(header(state={"a": 1})),
        answer(header(output_bytes=-1)),
        answer(header(output_bytes=True)),
        answer(header(output_bytes=1.0)),
        answer(header(output_bytes=jobs.MAX_SAFE + 1)),
        answer(header(output_start="0")),
        answer(header(body_bytes=None)),
        answer(header(duration_ms=-5)),
        answer(header(body_bytes=4)),
        answer(header(body_bytes=6)),
        answer(header(output_start=1)),
        answer(header(exit_code=256)),
        answer(header(exit_code=-1)),
        answer(header(exit_code=True)),
        answer(header(exit_code="0")),
    ],
)
def test_parse_wait_refuses_invalid_answers(result: ShellResult) -> None:
    invalid(result)


def test_parse_wait_refuses_a_body_above_the_requested_limit() -> None:
    invalid(answer(header()), limit=4)


def test_argv_builders() -> None:
    assert jobs.start_argv(JOB, "/workspace", None) == ["sudo", "-n", jobs.HELPER, "start", JOB, "/workspace", "0"]
    assert jobs.start_argv(JOB, "/", 30)[-1] == "30"
    assert jobs.wait_argv(JOB, None, 100, 2.5) == ["sudo", "-n", jobs.HELPER, "wait", JOB, "-1", "100", "2.5"]
    assert jobs.wait_argv(JOB, 7, 1, 0)[-3:] == ["7", "1", "0"]
    assert jobs.kill_argv(JOB, "KILL") == ["sudo", "-n", jobs.HELPER, "kill", JOB, "KILL"]
    assert jobs.list_argv() == ["sudo", "-n", jobs.HELPER, "list"]


@pytest.mark.parametrize("job_id", ["j-0123", "j-0123456789AB", "x-0123456789ab", "j-0123456789ab\n"])
def test_argv_builders_refuse_invalid_job_ids(job_id: str) -> None:
    with pytest.raises(ValueError, match=re.escape(job_id)):
        jobs.start_argv(job_id, "/", None)
    with pytest.raises(ValueError, match=re.escape(job_id)):
        jobs.wait_argv(job_id, None, 1, 0)
    with pytest.raises(ValueError, match=re.escape(job_id)):
        jobs.kill_argv(job_id, "TERM")


def test_kill_argv_refuses_unknown_signals() -> None:
    with pytest.raises(ValueError, match=JOB):
        jobs.kill_argv(JOB, "USR1")


def test_start_input() -> None:
    env = [{"name": "A", "value": "1"}, {"name": "B", "value": ""}]
    assert json.loads(jobs.start_input("ls", env, None)) == {"command": "ls", "env": {"A": "1", "B": ""}}
    assert json.loads(jobs.start_input("cat", [], "in")) == {"command": "cat", "env": {}, "stdin": "in"}
    assert json.loads(jobs.start_input("cat", [], "")) == {"command": "cat", "env": {}, "stdin": ""}


def item(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "job_id": JOB,
        "command": "make test",
        "state": "running",
        "exit_code": None,
        "started_at": 1_800_000_000_000,
        "duration_ms": 50,
    }
    value.update(changes)
    return value


def listed(items: object, *, returncode: int = 0, truncated: bool = False) -> ShellResult:
    return ShellResult(returncode, json.dumps(items).encode(), truncated)


def test_parse_list_returns_checked_jobs() -> None:
    long = "y" * 300
    jobs_found = jobs.parse_list(listed([item(), item(job_id="j-ffffffffffff", command=long, exit_code=3)]))
    assert jobs_found == [item(), item(job_id="j-ffffffffffff", command="y" * 256, exit_code=3)]
    assert jobs.parse_list(listed([])) == []
    assert len(jobs.parse_list(listed([item()] * 50))) == 50


@pytest.mark.parametrize(
    "result",
    [
        listed([item()], returncode=2),
        listed([item()], truncated=True),
        ShellResult(0, b"\xff", False),
        ShellResult(0, b"[", False),
        listed({"jobs": []}),
        listed([item()] * 51),
        listed(["j-0123456789ab"]),
        listed([{**item(), "extra": 1}]),
        listed([{k: v for k, v in item().items() if k != "state"}]),
        listed([item(job_id=5)]),
        listed([item(job_id="j-bad")]),
        listed([item(command=None)]),
        listed([item(state="gone")]),
        listed([item(state=["running"])]),
        listed([item(exit_code=300)]),
        listed([item(started_at=-1)]),
        listed([item(duration_ms="1")]),
    ],
)
def test_parse_list_refuses_invalid_answers(result: ShellResult) -> None:
    with pytest.raises(WorkVmError) as caught:
        jobs.parse_list(result)
    assert caught.value.code == "workvm_answer_invalid"
