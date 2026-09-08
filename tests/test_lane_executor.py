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

    def __init__(self, selected, **kwargs):
        type(self).started += 1
        assert selected.model == "explicit-model"
        assert kwargs["binary"] == "/explicit/native/cli"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def fresh_transport():
    Transport.responses = []
    Transport.prompts = []
    Transport.started = 0


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
