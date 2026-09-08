import json

import pytest

from agent_cli.ai_accounts import AIAccount, AIRole, LaneRuntime
from agent_cli.lane_executor import execute
from agent_cli.lane_protocol import ProtocolError, digest


def role(*, write=True, configured=True):
    runtime = LaneRuntime("/explicit/native/cli", "0" * 64) if configured else None
    return AIRole("explicit-role", AIAccount("explicit-account", "grok", "/explicit/profile", runtime),
                  "explicit-model", "workspace-write" if write else "read-only")


class Transport:
    responses = []
    prompts = []
    started = 0
    events = []
    exit_error = None
    observe_path = None

    def __init__(self, selected, **kwargs):
        type(self).started += 1
        assert selected.model == "explicit-model"
        assert kwargs["binary"] == "/explicit/native/cli"

    def __enter__(self):
        type(self).events.append("enter")
        return self

    def __exit__(self, *_):
        type(self).events.append("exit")
        if type(self).observe_path is not None:
            type(self).events.append(
                ("content_during_exit", type(self).observe_path.read_text()))
        if type(self).exit_error is not None:
            raise type(self).exit_error

    def complete(self, prompt):
        type(self).events.append("complete")
        self.prompts.append(prompt)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def fresh_transport():
    Transport.responses = []
    Transport.prompts = []
    Transport.started = 0
    Transport.events = []
    Transport.exit_error = None
    Transport.observe_path = None


def run(tmp_path, selected=None):
    return execute(selected or role(), cwd=str(tmp_path), manifest=["file.py"], spec="Implement the task.",
                   timeout=30, transport_factory=Transport)


def test_script_serves_reads_and_applies_only_completed_implementation(tmp_path):
    (tmp_path / "file.py").write_text("old")
    Transport.responses = [json.dumps(r) for r in (
        {"action": "read", "path": "file.py", "offset": 0, "limit": 10},
        {"action": "write", "path": "file.py", "expected_sha256": digest("old"), "content": "new"},
        {"action": "finish", "text": "STATUS: complete\nRESULT: done\n"},
    )]
    result = run(tmp_path)
    assert result.returncode == 0 and "RESULT: done" in result.stdout
    assert (tmp_path / "file.py").read_text() == "new"
    assert '"content": "old"' in Transport.prompts[1]
    budgets = [json.loads(p.rsplit("SCRIPT WORK BUDGET: ", 1)[1]) for p in Transport.prompts]
    assert [b["remaining_requests"] for b in budgets] == [200, 199, 198]
    assert all(0 <= b["remaining_seconds"] <= 30 for b in budgets)
    assert "TASK DATA:\nImplement the task." in Transport.prompts[0]


@pytest.mark.parametrize("text", ["STATUS: partial\nRESULT: done", "STATUS: complete\nRESULT: blocked",
                                  "STATUS: complete\nRESULT: ask", "done",
                                  "STATUS: complete\nRESULT: done\nVERDICT: approved",
                                  "STATUS: complete\nRESULT: done\nRESULT: invalid",
                                  "STATUS: complete\nSTATUS: invalid\nRESULT: done"])
def test_partial_blocked_or_ambiguous_work_leaves_no_edits(tmp_path, text):
    (tmp_path / "file.py").write_text("old")
    Transport.responses = [json.dumps(r) for r in (
        {"action": "write", "path": "file.py", "expected_sha256": digest("old"), "content": "new"},
        {"action": "finish", "text": text},
    )]
    run(tmp_path)
    assert (tmp_path / "file.py").read_text() == "old"


def test_disallowed_action_aborts_without_another_model_call(tmp_path):
    (tmp_path / "file.py").write_text("old")
    Transport.responses = ['{"action":"monitor","command":"anything"}', 'unused']
    with pytest.raises(ProtocolError):
        run(tmp_path)
    assert Transport.responses == ['unused']
    assert (tmp_path / "file.py").read_text() == "old"


def test_reviewer_cannot_obtain_writable_execution(tmp_path):
    (tmp_path / "file.py").write_text("old")
    Transport.responses = [json.dumps({"action":"write", "path":"file.py", "expected_sha256":digest("old"), "content":"new"})]
    with pytest.raises(ProtocolError, match="read-only"):
        run(tmp_path, role(write=False))
    assert (tmp_path / "file.py").read_text() == "old"


def test_unconfigured_runtime_starts_no_transport(tmp_path):
    with pytest.raises(ProtocolError, match="unconfigured"):
        run(tmp_path, role(configured=False))
    assert Transport.started == 0


def test_teardown_failure_after_complete_done_leaves_source_unchanged(tmp_path):
    target = tmp_path / "file.py"
    target.write_text("old")
    Transport.observe_path = target
    Transport.exit_error = RuntimeError("auth persist failed")
    Transport.responses = [json.dumps(r) for r in (
        {"action": "write", "path": "file.py", "expected_sha256": digest("old"), "content": "new"},
        {"action": "finish", "text": "STATUS: complete\nRESULT: done\n"},
    )]
    with pytest.raises(RuntimeError, match="auth persist failed"):
        run(tmp_path)
    assert target.read_text() == "old"
    assert ("content_during_exit", "old") in Transport.events
    assert Transport.events[:3] == ["enter", "complete", "complete"]
    assert "exit" in Transport.events


def test_successful_teardown_applies_only_after_exit(tmp_path):
    target = tmp_path / "file.py"
    target.write_text("old")
    Transport.observe_path = target
    Transport.responses = [json.dumps(r) for r in (
        {"action": "write", "path": "file.py", "expected_sha256": digest("old"), "content": "new"},
        {"action": "finish", "text": "STATUS: complete\nRESULT: done\n"},
    )]
    result = run(tmp_path)
    assert result.returncode == 0 and "RESULT: done" in result.stdout
    assert target.read_text() == "new"
    assert Transport.events == [
        "enter", "complete", "complete", "exit", ("content_during_exit", "old"),
    ]


def test_readonly_finish_unavailable_when_teardown_fails(tmp_path):
    (tmp_path / "file.py").write_text("old")
    Transport.exit_error = RuntimeError("temp cleanup failed")
    Transport.responses = [json.dumps(
        {"action": "finish", "text": "STATUS: complete\nVERDICT: approved\n"},
    )]
    with pytest.raises(RuntimeError, match="temp cleanup failed"):
        run(tmp_path, role(write=False))
    assert (tmp_path / "file.py").read_text() == "old"
    assert Transport.events == ["enter", "complete", "exit"]
