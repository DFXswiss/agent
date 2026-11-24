"""U-RUN-operations (KOR-47): the registration of every operation names the acting human (`triggered_by`) and, for
`github.*` types, the identity the pre-check resolved (`github_identity`); `subagent.request` may name the child
session's identity, which the acting human must be allowed to use (ADR-078 sections 2, 3 and 6; 03-api.md section
8.3)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.operations import register
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.identity_kit import IdentityHub, identity_body, operation_of, session_of
from ..credentials.kit import ck, pid
from ..reconcile.rig import Rig
from ..sessions.kit import ORG, OWNER
from .kit import ok, run, setup

BOB = pid("bob")
IDENTITY = "0193000a-0000-7000-8000-00000000000b"
COMMENT = {"repo": "octo-org/hello", "number": 7, "body": "Looks good."}
SUBAGENT = {"purpose": "Review", "prompt": "Review PR 7."}


async def resolved(request: PrecheckRequest) -> PrecheckResult:
    return PrecheckResult(data={"github_identity": IDENTITY, "acting_user": request.operation.actor_id})


def test_registrations_carry_the_stamped_actor_and_the_github_identity(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(
            env,
            executors={"github.comment": ok, "task.set_title": ok},
            prechecks={"github.comment": resolved, "task.set_title": resolved},
        )
        await kit_.connect(BOB)
        sid = await kit_.session(actor=BOB)  # bob instructed the turn in alice's task
        op = await kit_.run(sid, "github.comment", COMMENT)
        assert op.state == "succeeded", op.answer
        sent = kit_.hub.requests[-1]
        assert (sent["triggered_by"], sent["github_identity"]) == (BOB, IDENTITY)
        # a type that is not GitHub names no identity, even when a pre-check put one into its data
        op = await kit_.run(sid, "task.set_title", {"title": "Fix"})
        sent = kit_.hub.requests[-1]
        assert (sent["triggered_by"], "github_identity" in sent) == (BOB, False)
        # rejected by the guest: no checks ran, the stamp still names the person
        rejected = {"code": "params_invalid", "message": "The parameters do not match."}
        await kit_.run(sid, "github.comment", {"x": 1}, rejected=rejected)
        sent = kit_.hub.requests[-1]
        assert (sent["local_check"], sent["triggered_by"], "github_identity" in sent) == ("rejected", BOB, False)
        # a turn without one acting user: nobody to name
        other = await kit_.session(2, actor=None)
        await kit_.run(other, "task.set_title", {"title": "x"})
        assert "triggered_by" not in kit_.hub.requests[-1]
        assert OWNER != BOB

    run(rig, body)


def test_a_subagent_may_only_get_an_identity_the_acting_human_may_use(rig: Rig) -> None:
    async def body(env: Any) -> None:
        hub = IdentityHub(env.hub).install()
        async with session(env.pool) as conn:
            for owner in (ORG, BOB):
                await store_scope_key(conn, env.sync.storage_key, ("cred", owner, 1), ck(owner), source="wrap")
        company = hub.add(identity_body(login="octo-org"), owner=ORG, kind="org")
        bobs = hub.add(identity_body(login="bob-user"), owner=BOB, author="bob")
        by_member = hub.add(identity_body(), owner=ORG, kind="org", author="bob")
        hub.refusals[pid("hidden")] = (403, {"error": "forbidden"})
        hub.refusals[pid("odd")] = (400, {"error": "request_invalid"})
        kit_ = await setup(env, executors={"subagent.request": ok})
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "accepted", "child_session_id": pid("child")})
        op = await kit_.run(sid, "subagent.request", {**SUBAGENT, "github_identity": company})
        assert op.state == "succeeded", op.answer
        assert kit_.hub.requests[-1]["github_identity"] == company
        cases = [
            (bobs, "identity_forbidden"),
            (pid("hidden"), "identity_forbidden"),
            (pid("gone"), "identity_unavailable"),
            (pid("odd"), "identity_unavailable"),
            (by_member, "identity_unavailable"),
        ]
        for chosen, code in cases:
            op = await kit_.run(sid, "subagent.request", {**SUBAGENT, "github_identity": chosen})
            assert op.answer is not None
            assert (op.answer["status"], op.answer["error"]["code"]) == ("rejected", code), chosen
        # without a choice nothing is checked and no identity is sent
        kit_.hub.decisions.append({"state": "accepted", "child_session_id": pid("child2")})
        await kit_.run(sid, "subagent.request", SUBAGENT)
        assert "github_identity" not in kit_.hub.requests[-1]

    run(rig, body)


def test_without_any_human_a_subagent_identity_is_forbidden(rig: Rig) -> None:
    async def body(env: Any) -> None:
        sub, title = operation_of(None, "subagent.request"), operation_of(None, "task.set_title")
        with pytest.raises(CheckFailed) as failed:
            await register.attribution(env.context, session_of(None), sub, {"github_identity": IDENTITY})
        assert failed.value.code == "identity_forbidden"
        assert await register.attribution(env.context, session_of(None), title, {}) == {}

    run(rig, body)
