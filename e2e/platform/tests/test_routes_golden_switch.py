"""Control route of the golden switch scenario (E2E-69, KOR-70): a workgroup VM marked as cloned from an older golden
on the runner's own store, against a scripted node."""

from __future__ import annotations

from pathlib import Path

from orchestrator.control import dispatch
from orchestrator.routes import golden_switch
from orchestrator.services import Runner
from test_routes_guest import Result, ScriptNode, failed, make_ctx, ok

VM = "vm-0a1b2c3d"


def test_vm_outdated(tmp_path: Path) -> None:
    answers = ['["vm-0a1b2c3d"]\n', "[]\n"]

    def respond(argv: list[str]) -> Result:
        assert argv[:2] == ["/agent/python", "-c"]
        assert argv[3:] == ["~/r1", VM, golden_switch.OUTDATED]
        return ok(answers.pop(0)) if answers else failed()

    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    ctx.runners = {"runner-1": Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "~/r1"})}

    def call(name: str, body: object) -> tuple[int, object]:
        return dispatch(golden_switch.ROUTES, ctx, "POST", f"/runners/{name}/vm-outdated", body)

    assert call("runner-1", {"vm_id": VM}) == (200, {"vm_id": VM, "golden_version": "e2e-outdated"})
    assert call("runner-1", {"vm_id": VM})[0] == 404  # no live workgroup VM of that ID
    assert call("runner-1", {"vm_id": VM})[0] == 500  # the store update failed
    assert call("runner-9", {"vm_id": VM})[0] == 404
    bad: object
    for bad in ({"vm_id": "vm-x"}, {}, []):
        assert call("runner-1", bad)[0] == 400
