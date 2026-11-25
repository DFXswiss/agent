"""`POST /runners/workvm`: work VM settings of the runners (KOR-29)."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.routes import workvm
from test_routes import call, ctx  # noqa: F401 - the shared context fixture

IMAGE = "cache-wimg-0123456789ab"
BASE = "wp-t-e2e-r1-wimg-e2e"


def runs(context: Context) -> list[list[str]]:
    return [args["argv"] for op, args in context.runner_node.calls if op == "run"]  # type: ignore[attr-defined]


def prepared(context: Context) -> Context:
    context.workvm_image = IMAGE
    context.runners["runner-1"].env.update({"AGENT_RUNNER_VM_PREFIX": "wp-t-e2e-r1-", "AGENT_RUNNER_HOME": "/h/r1"})
    return context


def test_invalid_bodies(ctx: Context) -> None:  # noqa: F811
    bodies: list[Any] = [None, [], {}, {"slots": 1, "other": 1}, {"slots": 5}, {"slots": True}]
    for body in [*bodies, {"slots": 1, "pool_size": 9}]:
        status, answer = call(ctx, "POST", "/runners/workvm", body)
        assert status == 400, body
        assert answer["error"] == "invalid"


def test_no_work_image_in_the_run(ctx: Context) -> None:  # noqa: F811
    status, answer = call(ctx, "POST", "/runners/workvm", {"slots": 2})
    assert (status, answer["error"]) == (409, "no_work_image")


def test_enable_clones_registers_and_restarts_then_disable(ctx: Context) -> None:  # noqa: F811
    prepared(ctx)
    body: dict[str, Any] = {"slots": 2, "max_per_task": 1, "pool_size": 1}
    status, answer = call(ctx, "POST", "/runners/workvm", body)
    assert (status, answer) == (200, {"runner-1": {"image": BASE, "restarted": True}})
    env = ctx.runners["runner-1"].env
    assert env["AGENT_RUNNER_WORKVM_SLOTS"] == "2"
    assert env["AGENT_RUNNER_WORKVM_MAX_PER_TASK"] == "1"
    assert env["AGENT_RUNNER_WORKVM_POOL_SIZE"] == "1"
    assert env["AGENT_RUNNER_WORKVM_SLOTS_SELF"] == "1"
    argvs = runs(ctx)
    assert ["limactl", "clone", "--tty=false", IMAGE, BASE] in argvs
    assert [ctx.agent_python, "-c", workvm.REGISTER, "/h/r1", BASE] in argvs
    # the same settings again: no restart; the base is listed, so not cloned again
    ctx.runner_node.runs[""] = {"rc": 0, "stdout": f"other\n{BASE}\n", "stderr": ""}  # type: ignore[attr-defined]
    before = len(runs(ctx))
    assert call(ctx, "POST", "/runners/workvm", body)[1] == {"runner-1": {"image": BASE, "restarted": False}}
    assert not any(argv[1] == "clone" for argv in runs(ctx)[before:])
    # disabled: every variable goes, the runner restarts
    assert call(ctx, "POST", "/runners/workvm", {"slots": 0})[1] == {"runner-1": {"image": None, "restarted": True}}
    assert not any(name.startswith("AGENT_RUNNER_WORKVM_") for name in env)


def test_a_stopped_runner_is_not_restarted(ctx: Context) -> None:  # noqa: F811
    prepared(ctx)
    ctx.runner_node.alive_names.discard("runner-1")  # type: ignore[attr-defined]
    assert call(ctx, "POST", "/runners/workvm", {"slots": 1})[1] == {"runner-1": {"image": BASE, "restarted": False}}


def test_clone_and_register_failures(ctx: Context) -> None:  # noqa: F811
    prepared(ctx)
    ctx.runner_node.runs[f"{IMAGE} {BASE}"] = {"rc": 1, "stdout": "", "stderr": "no space"}  # type: ignore[attr-defined]
    status, answer = call(ctx, "POST", "/runners/workvm", {"slots": 1})
    assert status == 500
    assert "clone failed: no space" in answer["detail"]
    ctx.runner_node.runs[f"{IMAGE} {BASE}"] = {"rc": 0, "stdout": "", "stderr": ""}  # type: ignore[attr-defined]
    ctx.runner_node.runs[f"/h/r1 {BASE}"] = {"rc": 1, "stdout": "", "stderr": "no store"}  # type: ignore[attr-defined]
    status, answer = call(ctx, "POST", "/runners/workvm", {"slots": 1})
    assert status == 500
    assert "register failed: no store" in answer["detail"]


def test_browser_idle_seconds(ctx: Context) -> None:  # noqa: F811
    prepared(ctx)
    assert call(ctx, "POST", "/runners/workvm", {"slots": 1, "browser_idle_seconds": 59})[0] == 400
    status, _answer = call(ctx, "POST", "/runners/workvm", {"slots": 1, "browser_idle_seconds": 60})
    assert status == 200
    assert ctx.runners["runner-1"].env["AGENT_RUNNER_BROWSER_IDLE_SECONDS"] == "60"


def test_kill_a_work_vm(ctx: Context) -> None:  # noqa: F811
    prepared(ctx)
    for body in [None, {"lima_name": "golden"}, {"lima_name": "wp-t-e2e-r1-wvm-0123abcd", "x": 1}]:
        assert call(ctx, "POST", "/runners/workvm/kill", body)[0] == 400
    status, answer = call(ctx, "POST", "/runners/workvm/kill", {"lima_name": "wp-t-e2e-r1-wvm-0123abcd"})
    assert status == 200
    assert answer["runner"] == "runner-1"
    assert ["limactl", "stop", "-f", "wp-t-e2e-r1-wvm-0123abcd"] in runs(ctx)
