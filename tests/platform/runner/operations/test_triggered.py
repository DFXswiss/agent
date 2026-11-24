"""U-RUN-operations (KOR-46, ADR-083 section 2): the registration of an operation names the person whose instruction
it follows from (`triggered_by` = the acting user stamped at the receipt), only towards a hub that announces the
section `prompt_queue` (an older hub refuses unknown fields)."""

from __future__ import annotations

from typing import Any

from ..reconcile.rig import Rig
from .kit import OWNER, run, setup

QUEUE: dict[str, Any] = {"prompt_queue": {"via_agent": [], "withdrawn": []}}


def test_the_registration_names_the_acting_user_for_a_hub_with_the_queue(rig: Rig) -> None:
    async def body(env: Any) -> None:
        k = await setup(env)
        k.kit.session_env.desired = QUEUE
        mine = await k.session(1)
        nobody = await k.session(2, actor=None)
        await k.run(mine, "task.set_title", {"title": "One"})
        await k.run(nobody, "task.set_title", {"title": "Two"})
        first, second = k.hub.requests
        assert first["triggered_by"] == OWNER
        assert second["triggered_by"] is None

    run(rig, body)


def test_an_older_hub_gets_no_triggered_by(rig: Rig) -> None:
    async def body(env: Any) -> None:
        k = await setup(env)
        sid = await k.session(1)
        await k.run(sid, "task.set_title", {"title": "One"})
        k.kit.session_env.desired = {"billing": {"api": 1}}
        await k.run(sid, "task.set_title", {"title": "Two"})
        assert all("triggered_by" not in request for request in k.hub.requests)
        assert len(k.hub.requests) == 2

    run(rig, body)
