"""Control routes of the session-context scenarios (KOR-33: VM deletion, agent kill, guest retention, runner cache
eviction, hub checkpoint tampering) against a scripted node."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from orchestrator.routes import session_context
from orchestrator.services import Cluster, Runner
from test_routes_guest import (
    PREFIX,
    SESSION,
    VM1,
    Result,
    ScriptNode,
    call,
    failed,
    guest,
    make_ctx,
    ok,
    session_handler,
)

TASK = "01a20172-b86a-789c-89ab-7aa8af7fa990"
OBJECT = "01a20172-b86a-789c-89ab-7aa8af7fa991"
R1 = f"{PREFIX}r1-"
NESTED = f"{PREFIX}r1-x-"


def with_runners(tmp_path: Path, node: ScriptNode) -> tuple[Any, Runner, Runner]:
    ctx = make_ctx(tmp_path, node)
    first = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "~/r1", "AGENT_RUNNER_VM_PREFIX": R1})
    nested = Runner(node, "runner-2", "/agent/python", {"AGENT_RUNNER_HOME": "~/r2", "AGENT_RUNNER_VM_PREFIX": NESTED})
    ctx.runners = {"runner-0": Runner(node, "runner-0", "/agent/python", {}), "runner-1": first, "runner-2": nested}
    return ctx, first, nested


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(session_context, "CLOCK", lambda: now[0])
    monkeypatch.setattr(session_context, "SLEEP", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_vm_delete(tmp_path: Path, fast: list[float]) -> None:
    states = ['[{"state": "running"}]\n', '[{"state": "deleted"}]\n']

    def respond(argv: list[str]) -> Result:
        if argv[:3] == ["limactl", "delete", "-f"]:
            return ok()
        assert argv[:2] == ["/agent/python", "-c"]
        assert argv[3] == "~/r1"
        assert argv[4] == f"SELECT state FROM runner.vm WHERE lima_name = '{VM1}'"
        return ok(states.pop(0))

    node = ScriptNode(respond)
    ctx, first, _ = with_runners(tmp_path, node)
    node.alive_names.add("runner-1")
    assert call(ctx, "POST", f"/vms/{VM1}/delete") == (200, {"runner": "runner-1", "vm": VM1})
    assert first.stopped_on_request is False  # started again
    assert ["limactl", "delete", "-f", VM1] in node.runs
    assert fast[0] == 1.0
    # a stopped runner is not stopped again; a row already gone is enough
    node.alive_names.clear()
    node.respond = lambda argv: ok("[]\n") if argv[0] == "/agent/python" else ok()
    assert call(ctx, "POST", f"/vms/{VM1}/delete")[0] == 200


def test_vm_delete_picks_the_longest_prefix_and_refuses(tmp_path: Path, fast: list[float]) -> None:
    node = ScriptNode(lambda argv: ok('[{"state": "deleted"}]\n'))
    ctx, _, _ = with_runners(tmp_path, node)
    assert call(ctx, "POST", f"/vms/{NESTED}arp-1/delete")[1]["runner"] == "runner-2"
    assert call(ctx, "POST", "/vms/golden/delete")[0] == 400
    assert call(ctx, "POST", f"/vms/{PREFIX}Upper/delete")[0] == 400
    assert call(ctx, "POST", f"/vms/{PREFIX}r9-arp/delete")[0] == 404
    # limactl fails: the runner runs again, the route reports it
    node.respond = lambda argv: failed(1, "no such instance") if argv[0] == "limactl" else ok("[]\n")
    status, answer = call(ctx, "POST", f"/vms/{VM1}/delete")
    assert (status, answer["error"]) == (500, "failed")
    assert "no such instance" in answer["detail"]
    # the store script fails
    node.respond = lambda argv: failed(2, "psycopg.OperationalError") if argv[0] == "/agent/python" else ok()
    assert "store script exited with 2" in call(ctx, "POST", f"/vms/{VM1}/delete")[1]["detail"]
    # the row never settles
    node.respond = lambda argv: ok('[{"state": "running"}]\n') if argv[0] == "/agent/python" else ok()
    status, answer = call(ctx, "POST", f"/vms/{VM1}/delete")
    assert status == 500
    assert "still lists" in answer["detail"]


def test_agent_kill(tmp_path: Path) -> None:
    pid = ["4242\n"]

    def extra(_vm: str, argv: list[str]) -> Result | None:
        if argv[0] == "systemctl":
            assert argv[-1] == f"agent-session-{SESSION}"
            return ok(pid[0])
        if argv[:2] == ["kill", "-KILL"]:
            return ok() if argv[2] == "4242" else failed(1, "no such process")
        return None

    node = ScriptNode(guest(session_handler(extra)))
    ctx = make_ctx(tmp_path, node)
    assert call(ctx, "POST", f"/sessions/{SESSION}/agent-kill") == (200, {"vm": VM1, "pid": 4242})
    pid[0] = "0\n"
    assert call(ctx, "POST", f"/sessions/{SESSION}/agent-kill")[0] == 409
    pid[0] = "\n"
    assert call(ctx, "POST", f"/sessions/{SESSION}/agent-kill")[0] == 409
    pid[0] = "4343\n"
    status, answer = call(ctx, "POST", f"/sessions/{SESSION}/agent-kill")
    assert (status, answer["error"]) == (500, "failed")
    node.respond = guest(lambda vm, argv: failed(1, "can't find session"))
    assert call(ctx, "POST", f"/sessions/{SESSION}/agent-kill")[0] == 500


def test_expire(tmp_path: Path) -> None:
    seen: list[list[str]] = []
    present = [True]

    def handler(vm: str, argv: list[str]) -> Result:
        assert vm == VM1
        seen.append(argv)
        if argv[0] == "test":
            return ok() if present[0] else failed(1, "")
        if argv[0] == "userdel":
            return failed(6, "does not exist")
        return ok()

    node = ScriptNode(guest(handler))
    ctx = make_ctx(tmp_path, node)
    assert call(ctx, "POST", f"/vms/{VM1}/sessions/{SESSION}/expire") == (200, {"vm": VM1, "existed": True})
    user = "s-01a20172b86a89c9ab7aa8af7fa995"
    assert seen == [
        ["test", "-d", f"/srv/sessions/{SESSION}"],
        ["rm", "-rf", f"/srv/sessions/{SESSION}"],
        ["rm", "-f", f"/run/agent-guest/harness/{SESSION}.md", f"/var/lib/agent-guest/sessions/{SESSION}.json"],
        ["userdel", user],
        ["groupdel", user],
    ]
    present[0] = False
    assert call(ctx, "POST", f"/vms/{VM1}/sessions/{SESSION}/expire")[1]["existed"] is False
    assert call(ctx, "POST", f"/vms/{VM1}/sessions/not-a-session/expire")[0] == 400
    assert call(ctx, "POST", f"/vms/other/sessions/{SESSION}/expire")[0] == 400
    node.respond = guest(lambda vm, argv: failed(1, "read-only file system"))
    status, answer = call(ctx, "POST", f"/vms/{VM1}/sessions/{SESSION}/expire")
    assert (status, answer["error"]) == (500, "failed")


def test_evict(tmp_path: Path) -> None:
    def respond(argv: list[str]) -> Result:
        assert argv[:2] == ["/agent/python", "-c"]
        assert "DELETE FROM runner.context_object" in argv[2]
        assert argv[3:] == ["~/r1", TASK]
        return ok(f'[["{OBJECT}", "committed"]]\n')

    node = ScriptNode(respond)
    ctx, _, _ = with_runners(tmp_path, node)
    assert call(ctx, "POST", "/runners/runner-1/contexts/evict", {"task": TASK}) == (
        200,
        {"objects": [[OBJECT, "committed"]]},
    )
    assert call(ctx, "POST", "/runners/runner-9/contexts/evict", {"task": TASK})[0] == 404
    assert call(ctx, "POST", "/runners/runner-1/contexts/evict", {"task": "x'; DROP"})[0] == 400
    assert call(ctx, "POST", "/runners/runner-1/contexts/evict", [TASK])[0] == 400


def test_tamper(tmp_path: Path) -> None:
    statements: list[str] = []

    def respond(argv: list[str]) -> Result:
        assert argv[0] == "/pg/psql"
        statements.append(argv[argv.index("-c") + 1])
        return ok(f"{OBJECT}\n\n")

    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    body: dict[str, Any] = {"task": TASK, "action": "corrupt"}
    assert call(ctx, "POST", "/hub-db/contexts/tamper", body)[0] == 409
    ctx.cluster = Cluster(node, "/pg", "/data", 21005, {})
    assert call(ctx, "POST", "/hub-db/contexts/tamper", body) == (200, {"objects": [OBJECT]})
    assert "set_byte(c.ct, 20, get_byte(c.ct, 20) # 255)" in statements[-1]
    assert f"o.task_id = '{TASK}' AND o.state = 'complete'" in statements[-1]
    assert call(ctx, "POST", "/hub-db/contexts/tamper", {"task": TASK, "action": "delete"})[0] == 200
    assert statements[-1] == f"DELETE FROM hub.context_object WHERE task_id = '{TASK}' RETURNING id"
    uncommit = {"task": TASK, "action": "uncommit", "objects": [OBJECT]}
    assert call(ctx, "POST", "/hub-db/contexts/tamper", uncommit)[0] == 200
    assert "SET state = 'uploading', completed_at = NULL" in statements[-1]
    assert statements[-1].endswith(f"AND id IN ('{OBJECT}') RETURNING id")
    for bad in (
        {"task": TASK, "action": "rewrite"},
        {"task": "nope", "action": "delete"},
        {"task": TASK, "action": "uncommit"},
        {"task": TASK, "action": "uncommit", "objects": "all"},
        {"task": TASK, "action": "corrupt", "objects": [OBJECT] * 17},
        {"task": TASK, "action": "uncommit", "objects": ["x"]},
        None,
    ):
        assert call(ctx, "POST", "/hub-db/contexts/tamper", bad)[0] == 400, bad
