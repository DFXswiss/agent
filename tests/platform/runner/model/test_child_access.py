"""U-RUN-model (HOT-31, ADR-081 sections 1-5, 05-runner.md section 10.7): a session the desired state does not list
yet - a subagent right after its `subagent.request`, or a resumed session the hub does not name - follows the same
model access rules as its main session: the owner's preference, `fallback.own_to_dfx` and the task's `model_consent`
of the parent's entry (else of an entry of the same task and owner). With the owner's own subscription used up, a
subagent therefore runs on an operator subscription when the setting or the consent allows it, and fails with
`model_quota_exhausted` without both."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_runner.model import access, limits, policy
from agent_runner.model.access import AccessRefused
from agent_runner.model.limits import EXHAUSTED, Limit
from agent_runner.prompts.resume import RESUMED, Resumer, desired_of
from agent_runner.sessions import records
from agent_runner.sessions.start import Starter
from agent_runner.subagents.controller import child_desired, model_refusal

from ..sessions.kit import OWNER, TASK, desired, session_id
from .fallback_kit import AT, HOUR, T0, FKit, entry, fallback_kit, state
from .kit import ALICE, FLASH, OPUS, TOKEN, model_env
from .test_operator import pool_dir, token

OWN = limits.own_key(ALICE)
Body = Callable[[FKit], Awaitable[None]]
SETTINGS = [
    pytest.param({"own_to_dfx": True}, id="fallback-own-to-dfx"),
    pytest.param({"consent": True}, id="model-consent"),
]


def run(dsn: str, tmp_path: Path, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env, fallback_kit(env, pool_dir(tmp_path), **options) as found:
            await body(found)

    asyncio.run(main())


async def used_up(found: FKit) -> None:
    """The owner's own subscription is used up for an hour."""
    await limits.mark(found.context, Limit(OWN, EXHAUSTED, AT + HOUR, True, T0))


async def main_session(found: FKit) -> records.LocalSession:
    """The main session of the task, started on the used-up own subscription with the desired state of the found
    kit; returns its row."""
    assert (await found.start()).started, found.log()
    return await found.stored(session_id())


def credential_of(found: FKit, sid: str) -> str:
    [value] = [credential["auth_value"] for given, credential in found.credentials() if given == sid]
    return str(value)


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_subagent_follows_its_main_session_to_an_operator_subscription(
    store_dsn: str, tmp_path: Path, setting: dict[str, bool]
) -> None:
    async def body(found: FKit) -> None:
        await used_up(found)
        main = await main_session(found)
        # the main session moved to the operator's pool (ADR-081 section 1)
        assert credential_of(found, session_id()) == f"Bearer {token(1)}"
        # the subagent: the hub lists it only with a later desired state, the main session's entry is all there is
        senv = found.kit.session_env
        assert policy.entry(senv.desired, session_id(2)) is None
        child = child_desired(senv.desired, main, session_id(2), OPUS)
        assert (child.depth, child.parent_session_id, child.model_id_pref) == (1, session_id(), OPUS)
        report = await Starter(senv).start(child)
        assert report.started, (report.reason, found.log())
        # an operator subscription, never the used-up own one
        assert credential_of(found, session_id(2)) == f"Bearer {token(2)}"
        assert found.pool.bindings[session_id(2)] == "b"
        assert TOKEN not in credential_of(found, session_id(2))
        assert await found.pause_row(session_id(2)) is None
        # also the child as the start of an older runner built it: the parent's recorded access as preference
        built = desired(3, depth=1, parent_session_id=session_id(), model_access_pref=str(main.model_access))
        resolved = await access.resolve(senv, replace(built, model_id_pref=OPUS))
        assert resolved.label == "operator"

    run(store_dsn, tmp_path, body, desired_state=state(entry(1, **setting)))


def test_without_setting_and_consent_a_subagent_fails_with_quota_exhausted(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        await used_up(found)
        main = await main_session(found)
        # the main session waits on its own subscription
        assert credential_of(found, session_id()) == f"Bearer {TOKEN}"
        assert await found.pause_row(session_id()) is not None
        senv = found.kit.session_env
        child = child_desired(senv.desired, main, session_id(2), OPUS)
        with pytest.raises(AccessRefused) as refused:
            await access.resolve(senv, child)
        assert refused.value.reason == "model_quota_exhausted"
        report = await Starter(senv).start(child)
        assert (report.started, report.reason) == (False, "model_quota_exhausted")
        assert found.pool.bindings == {}

    run(store_dsn, tmp_path, body, desired_state=state(entry(1)))


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_resumed_subagent_the_hub_does_not_list_follows_its_main_session(
    store_dsn: str, tmp_path: Path, setting: dict[str, bool]
) -> None:
    async def body(found: FKit) -> None:
        senv = found.kit.session_env
        main = await main_session(found)  # on the own subscription: nothing is used up yet
        child = child_desired(senv.desired, main, session_id(2), None)
        assert (await Starter(senv).start(child)).started, found.log()
        assert credential_of(found, session_id(2)) == f"Bearer {TOKEN}"
        # the idle child was suspended; then the own subscription is used up
        await found.state(session_id(2), "suspended")
        await used_up(found)
        row = await found.stored(session_id(2))
        assert desired_of(senv, row).model_access_pref == "auto"  # the owner's, not the recorded `subscription`
        assert await Resumer(senv).resume(row) == RESUMED, found.log()
        resumed = [c["auth_value"] for given, c in found.credentials() if given == session_id(2)]
        assert resumed[-1] == f"Bearer {token(1)}"
        assert found.pool.bindings[session_id(2)] == "a"

    listed = entry(1, **setting)
    run(store_dsn, tmp_path, body, desired_state=state(listed))


def test_a_resumed_subagent_without_setting_and_consent_fails(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        senv = found.kit.session_env
        main = await main_session(found)
        assert (await Starter(senv).start(child_desired(senv.desired, main, session_id(2), None))).started
        await found.state(session_id(2), "suspended")
        await used_up(found)
        outcome = await Resumer(senv).resume(await found.stored(session_id(2)))
        assert outcome == "model_quota_exhausted"
        assert (await found.stored(session_id(2))).state == "failed"

    run(store_dsn, tmp_path, body, desired_state=state(entry(1)))


# -- a requested model chooses the access (HOT-31, findings of 27.11.) ----------------------------------------------


PLATFORM_KEY = "Bearer test-platform-key-SECRETMARKER-0123456789abcdef"


def created_model(found: FKit, sid: str) -> object:
    [request] = [value for name, value in found.guest.calls if name == "create_session" and value["session_id"] == sid]
    return request["model"]["model_id"]


def test_a_subagent_with_deepseek_gets_the_platform_access(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        main = await main_session(found)  # the owner's own subscription, nothing used up
        assert credential_of(found, session_id()) == f"Bearer {TOKEN}"
        senv = found.kit.session_env
        child = child_desired(senv.desired, main, session_id(2), FLASH)
        assert child.model_required
        assert await model_refusal(senv, child) is None
        report = await Starter(senv).start(child)
        assert report.started, (report.reason, found.log())
        # the platform access with the requested model, not a Claude model on the subscription
        assert credential_of(found, session_id(2)) == PLATFORM_KEY
        assert created_model(found, session_id(2)) == FLASH
        assert (await found.stored(session_id(2))).model_access == "platform"

    run(store_dsn, tmp_path, body, desired_state=state(entry(1, consent=True)))


def test_a_subagent_without_model_gets_a_free_dfx_subscription(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        await used_up(found)
        main = await main_session(found)
        assert found.pool.bindings == {session_id(): "a"}
        senv = found.kit.session_env
        # the parent runs on DFX's platform access (its recorded access): the child still takes the owner's order,
        # a free operator subscription before `platform`
        parent = replace(main, model_access="platform", model_id=FLASH)
        child = child_desired(senv.desired, parent, session_id(2), None)
        assert not child.model_required
        report = await Starter(senv).start(child)
        assert report.started, (report.reason, found.log())
        assert credential_of(found, session_id(2)) == f"Bearer {token(2)}"
        assert found.pool.bindings[session_id(2)] == "b"  # the file with fewer sessions

    run(store_dsn, tmp_path, body, desired_state=state(entry(1, consent=True)))


def test_a_requested_model_without_an_allowed_access_is_refused_with_its_reason(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        main = await main_session(found)
        senv = found.kit.session_env

        async def refusal_of(model: str = FLASH) -> tuple[str, str] | None:
            return await model_refusal(senv, child_desired(senv.desired, main, session_id(2), model))

        # neither the owner's setting nor the task's consent
        assert await refusal_of() == (
            "model_not_allowed",
            f"The model {FLASH} needs DFX model access: the owner's setting fallback.own_to_dfx or the task's consent"
            " to DFX access must allow it.",
        )
        # a model of the own subscription needs nothing of that
        assert await refusal_of(OPUS) is None
        # the owner's preference names no access that serves the model
        found.set_desired(state(entry(1, consent=True, model_access_pref="subscription")))
        assert await refusal_of() == (
            "model_not_allowed",
            f"The model {FLASH} is not served by any model access of the owner's preference subscription.",
        )
        # no platform key on this device
        found.set_desired(state(entry(1, consent=True)))
        key = senv.platform_key
        senv.platform_key = lambda _org: None
        assert await refusal_of() == (
            "model_not_allowed",
            f"No model access that serves the model {FLASH} is available on this device.",
        )
        senv.platform_key = key
        # the organization's platform budget is spent
        org = main.org_id
        budget = {"usd_per_org_day": 1, "spent_today_by_org": {org: 1.0}}
        found.set_desired(state(entry(1, consent=True), platform_budget=budget))
        assert await refusal_of() == (
            "budget_exceeded",
            f"The model {FLASH} needs DFX model access and the organization's platform budget is spent.",
        )
        # an unknown preference
        broken = replace(child_desired(None, main, session_id(2), FLASH), model_access_pref="bogus")
        assert await model_refusal(senv, broken) == (
            "model_not_allowed",
            "The owner's model access preference bogus is unknown.",
        )
        # a used-up access that serves the model is a wait, judged by the start (`model_quota_exhausted`)
        found.set_desired(state(entry(1)))
        await used_up(found)
        assert await refusal_of(OPUS) is None
        # and a child without a requested model is never refused here
        assert await model_refusal(senv, child_desired(senv.desired, main, session_id(2), None)) is None

    run(store_dsn, tmp_path, body, desired_state=state(entry(1)))


def test_serves_and_refusal_of_a_session_without_a_required_model(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        senv = found.kit.session_env
        plain = desired(model_id_pref=FLASH)
        assert all(access.serves(senv, plain, step) for step in ("subscription", "api_key", "operator", "platform"))
        required = replace(plain, model_required=True)
        assert not access.serves(senv, required, "operator")
        assert access.serves(senv, required, "platform")
        assert access.refusal(senv, plain, access.Walk()) is None

    run(store_dsn, tmp_path, body)


# -- the pieces --------------------------------------------------------------------------------------------------


def listed(n: int, **over: Any) -> dict[str, Any]:
    return {"session_id": session_id(n), "task_id": TASK, "owner_id": OWNER, **over}


def test_kin_takes_the_parent_first_then_a_session_of_the_same_task_and_owner() -> None:
    parent = listed(1, model_consent=True)
    other = listed(4, fallback={"own_to_dfx": True})
    foreign_task = listed(5, task_id=session_id(9), model_consent=True)
    foreign_owner = listed(6, owner_id=session_id(8), model_consent=True)
    found = {"sessions": ["junk", foreign_task, foreign_owner, other, parent]}
    assert policy.kin(found, task_id=TASK, owner_id=OWNER, parent_id=session_id(1)) == parent
    assert policy.kin(found, task_id=TASK, owner_id=OWNER, parent_id=session_id(7)) == other
    assert policy.kin(found, task_id=TASK, owner_id=OWNER) == other
    assert policy.kin(found, task_id=session_id(3), owner_id=OWNER) is None
    assert policy.kin(None, task_id=TASK, owner_id=OWNER) is None
    assert policy.kin({"sessions": "x"}, task_id=TASK, owner_id=OWNER) is None


def test_settings_of_a_session_that_is_not_listed() -> None:
    parent = listed(1, fallback={"own_to_dfx": True, "operator_to_platform": False}, model_consent=True)
    found = {"sessions": [parent]}
    kin = {"task_id": TASK, "owner_id": OWNER, "parent_id": session_id(1)}
    assert policy.settings(found, session_id(2), **kin) == policy.Settings(True, False, True)
    # the session's own entry comes first
    own = listed(2)
    assert policy.settings({"sessions": [parent, own]}, session_id(2), **kin) == policy.Settings()
    # without task and owner, or without an entry of them: the defaults
    assert policy.settings(found, session_id(2)) == policy.Settings()
    assert policy.settings(found, session_id(2), task_id=TASK) == policy.Settings()
    assert policy.settings(found, session_id(2), task_id=session_id(3), owner_id=OWNER) == policy.Settings()


def test_preference_of_a_session_that_is_not_listed() -> None:
    def pref(value: object) -> str | None:
        return policy.preference({"sessions": [listed(1, model_access_pref=value)]}, task_id=TASK, owner_id=OWNER)

    assert pref("api_key") == "api_key"
    assert pref("bogus") is None
    assert pref(3) is None
    assert policy.preference(None, task_id=TASK, owner_id=OWNER) is None


def test_child_desired_without_an_entry_keeps_the_parent_access_and_model(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        main = await main_session(found)
        parent = replace(main, model_access="platform", model_id="parent-model")
        child = child_desired(None, parent, session_id(2), "")
        assert (child.model_access_pref, child.model_id_pref) == ("platform", "parent-model")
        listed_state = {"sessions": [listed(1, model_access_pref="subscription")]}
        child = child_desired(listed_state, parent, session_id(2), 5)
        assert (child.model_access_pref, child.model_id_pref) == ("subscription", "parent-model")
        assert child_desired(listed_state, parent, session_id(2), OPUS).model_id_pref == OPUS

    run(store_dsn, tmp_path, body)
