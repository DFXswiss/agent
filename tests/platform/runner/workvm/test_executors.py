"""U-RUN-workvm executors: `workvm.request` and `workvm.release` (13-work-vm.md section 2).

`workvms_for` is replaced by a fake returning None (a device without work VMs) or a recording `WorkVms`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.operations.records import Operation
from agent_runner.sessions.records import LocalSession
from agent_runner.workvm import executors
from agent_runner.workvm.manager import NOT_ENABLED

SESSION = cast(LocalSession, SimpleNamespace(id="0192f3a4-0000-7000-8000-00000000000a"))
CONTEXT = SimpleNamespace(name="daemon")


def operation_context(params: Mapping[str, Any]) -> OperationContext:
    return OperationContext(
        context=CONTEXT,
        operation=cast(Operation, SimpleNamespace(type="workvm.request")),
        session=SESSION,
        params=params,
        data={},
        attempt=1,
        env=SimpleNamespace(),
    )


class FakeWorkVms:
    def __init__(self) -> None:
        self.calls: list[tuple[str, LocalSession, dict[str, Any]]] = []

    async def request(self, session: LocalSession, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("request", session, dict(params)))
        return {"workvm_id": "wvm-0a1b2c3d"}

    async def release(self, session: LocalSession, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("release", session, dict(params)))
        return {"released": True}


def test_registered_executors() -> None:
    assert {
        "workvm.request": executors.request_workvm,
        "workvm.release": executors.release_workvm,
    } == executors.EXECUTORS


def test_without_work_vms_request_fails_and_release_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def none(context: Any) -> None:
        seen.append(context)

    monkeypatch.setattr(executors, "workvms_for", none)
    with pytest.raises(OperationFailed) as caught:
        asyncio.run(executors.request_workvm(operation_context({"size": "standard"})))
    assert (caught.value.code, caught.value.retryable, caught.value.message) == (
        "workvm_unavailable",
        False,
        NOT_ENABLED,
    )
    assert asyncio.run(executors.release_workvm(operation_context({}))) == {"released": False}
    assert seen == [CONTEXT, CONTEXT]


def test_with_work_vms_the_manager_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    workvms = FakeWorkVms()
    monkeypatch.setattr(executors, "workvms_for", lambda context: workvms if context is CONTEXT else None)
    assert asyncio.run(executors.request_workvm(operation_context({"size": "large"}))) == {"workvm_id": "wvm-0a1b2c3d"}
    assert asyncio.run(executors.release_workvm(operation_context({"scope": "session"}))) == {"released": True}
    assert workvms.calls == [("request", SESSION, {"size": "large"}), ("release", SESSION, {"scope": "session"})]
