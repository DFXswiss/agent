"""U-RUN-model: model access of a session start over real credential records (05-runner.md sections 6.1 step 3,
10.1-10.3, 2.6; 03-api.md sections 6.1 and 7.1; ADR-010, ADR-026).

The session start of RUN-04 (`Starter`) runs against the fake guest; the owner's records come from the credential hub
of RUN-09, are verified and decrypted by the real cache. Every provisioning body is checked against
`contracts/guest/session-credential.request.json`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.model import access
from agent_runner.model.access import AccessRefused, Stamp, provisions, resolve
from agent_runner.model.selection import BUILTIN
from agent_runner.sessions.start import Starter

from ..credentials.kit import make_record
from ..sessions.kit import desired, session_id
from .kit import (
    ANTHROPIC,
    API_KEY,
    FLASH,
    KEY,
    OPUS,
    TOKEN,
    Env,
    Kit,
    api_key_body,
    credentials,
    model_env,
    model_kit,
    store_record,
    subscription_body,
)

REPO = Path(__file__).resolve().parents[4]
SCHEMAS = SchemaSet(REPO / "contracts")
CREDENTIAL = "guest/session-credential.request.json"
Body = Callable[[Env], Awaitable[None]]


def run(dsn: str, body: Body, **kwargs: Any) -> None:
    async def main() -> None:
        async with model_env(dsn, **kwargs) as env:
            await body(env)

    asyncio.run(main())


async def start(found: Kit, n: int = 1, **overrides: Any) -> Any:
    return await Starter(found.session_env).start(desired(n, **overrides))


def transition_access(found: Kit, transitions: list[dict[str, Any]]) -> list[tuple[str | None, str | None]]:
    return [(t.get("model_access"), t.get("model_id")) for t in transitions]


@pytest.mark.parametrize(
    ("records", "device", "preference", "want", "header", "value"),
    [
        # auto: subscription before API key before platform
        (("subscription", "api_key"), "platform", "auto", "subscription", "authorization", f"Bearer {TOKEN}"),
        (("api_key",), "platform", "auto", "api_key", "x-api-key", API_KEY),
        ((), "platform", "auto", "platform", "authorization", f"Bearer {KEY}"),
        # own hardware (org device) with the owner's API key (E2E-36)
        (("api_key",), "org", "auto", "api_key", "x-api-key", API_KEY),
        (("subscription",), "org", "subscription", "subscription", "authorization", f"Bearer {TOKEN}"),
        # an explicit preference takes its kind even when an earlier one would be available
        (("subscription", "api_key"), "platform", "api_key", "api_key", "x-api-key", API_KEY),
        (("subscription",), "platform", "platform", "platform", "authorization", f"Bearer {KEY}"),
    ],
)
def test_the_start_provisions_the_chosen_kind(
    store_dsn: str,
    *,
    records: tuple[str, ...],
    device: str,
    preference: str,
    want: str,
    header: str,
    value: str,
) -> None:
    async def body(env: Env) -> None:
        if "subscription" in records:
            store_record(env, "claude_subscription", subscription_body())
        if "api_key" in records:
            store_record(env, "anthropic_api_key", api_key_body())
        found = await model_kit(env, device_kind=device)
        report = await start(found, model_access_pref=preference)
        assert report.started, found.lines
        [(sid, credential)] = credentials(found)
        assert sid == session_id()
        assert SCHEMAS.errors(CREDENTIAL, credential) == []
        assert (credential["access"], credential["auth_header"], credential["auth_value"]) == (want, header, value)
        create = next(v for name, v in found.guest.calls if name == "create_session")
        if want == "platform":
            assert credential["upstream_base_url"] == "https://openrouter.ai/api"
            assert create["model"]["model_id"] == FLASH
        else:
            assert credential["upstream_base_url"] == ANTHROPIC
            assert OPUS in [m["model_id"] for m in credential["models_allowed"]]
            # Claude Code's default model and no model environment for the owner's own access
            assert create["model"] == {"access": want, "model_id": None, "env": {}}
        transitions = [b for k, b in await found.outbox() if k == "session_transition"]
        assert (want, create["model"]["model_id"]) in transition_access(found, transitions)
        stored = await found.stored(session_id())
        assert (stored.model_access, stored.state) == (want, "agent_starting")
        stamp = provisions(env.context).stamps[session_id()]
        assert stamp.access == want
        assert (stamp.version is None) == (want == "platform")

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("records", "device", "preference"),
    [
        ((), "org", "auto"),  # own hardware without credentials: no platform key on org devices (E2E-36)
        ((), "platform", "subscription"),  # explicit preference without its credential: no silent fallback
        (("subscription",), "platform", "api_key"),
        (("api_key",), "org", "platform"),
        ((), "platform", "bogus"),
    ],
)
def test_unavailable_access_fails_the_start(
    store_dsn: str, records: tuple[str, ...], device: str, preference: str
) -> None:
    async def body(env: Env) -> None:
        if "subscription" in records:
            store_record(env, "claude_subscription", subscription_body())
        if "api_key" in records:
            store_record(env, "anthropic_api_key", api_key_body())
        found = await model_kit(env, device_kind=device)
        report = await start(found, model_access_pref=preference)
        assert (report.started, report.reason) == (False, "model_access_unavailable")
        assert credentials(found) == []
        assert (await found.transitions())[-1] == ("failed", "model_access_unavailable")

    run(store_dsn, body)


def test_the_owner_model_is_used_when_the_catalog_allows_it(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        store_record(env, "claude_subscription", subscription_body())
        found = await model_kit(env)
        assert (await start(found, 1, model_id_pref=OPUS)).started
        assert (await start(found, 2, model_id_pref=FLASH)).started  # not allowed for a subscription
        creates = [v["model"] for name, v in found.guest.calls if name == "create_session"]
        assert [(m["access"], m["model_id"]) for m in creates] == [("subscription", OPUS), ("subscription", None)]

    run(store_dsn, body)


def test_a_record_this_device_cannot_use_makes_the_kind_unavailable(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        # a body without a usable secret; then a record of a CK epoch the device does not hold
        store_record(env, "claude_subscription", {"token": "two words", "email": None, "kind": "setup_token"})
        found = await model_kit(env)
        report = await start(found, 1, model_access_pref="subscription")
        assert report.reason == "model_access_unavailable"
        assert "credential_unusable owner=" in "\n".join(found.lines)
        assert "kind=claude_subscription reason=body" in "\n".join(found.lines)
        env.hub.records[(desired().owner_id, "anthropic_api_key")] = make_record(
            api_key_body(), kind="anthropic_api_key", epoch=2
        )
        report = await start(found, 2, model_access_pref="api_key")
        assert report.reason == "model_access_unavailable"
        assert "kind=anthropic_api_key reason=key_missing" in "\n".join(found.lines)

    run(store_dsn, body)


def test_a_hub_failure_while_fetching_makes_the_kind_unavailable_and_auto_goes_on(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.get_refusals.append((503, {"error": "unavailable"}))
        found = await model_kit(env)
        report = await start(found)  # auto: subscription fetch fails, no API key, platform
        assert report.started
        assert credentials(found)[0][1]["access"] == "platform"
        assert "credential_fetch_failed" in "\n".join(found.lines)
        assert "error=HubRefusal" in "\n".join(found.lines)

    run(store_dsn, body)


def test_a_spent_platform_budget_refuses_new_platform_sessions(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        org = desired().org_id
        state = {"platform_budget": {"usd_per_org_day": 2, "spent_today_by_org": {org: 2.0}}}
        found = await model_kit(env, desired_state=state)
        report = await start(found)
        assert (report.started, report.reason) == (False, "budget_exceeded")
        assert (await found.transitions())[-1] == ("failed", "budget_exceeded")
        assert credentials(found) == []
        # the owner's own access is not limited by the platform budget
        store_record(env, "anthropic_api_key", api_key_body())
        assert (await start(found, 2)).started
        # below the budget the platform session starts and its gateway gets the budget
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 2, "spent_today_by_org": {org: 0.5}}}
        assert (await start(found, 3, model_access_pref="platform")).started
        assert credentials(found)[-1][1]["budget"] == {"usd_per_day": 2.0, "spent_usd": 0.5}

    run(store_dsn, body)


def test_resolve_without_daemon_context_or_strategy(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        store_record(env, "anthropic_api_key", api_key_body())
        found = await model_kit(env)
        senv = found.session_env
        senv.context = None  # no daemon context: the owner's credentials cannot be opened
        resolved = await resolve(senv, desired())
        assert resolved.choice.access == "platform"
        assert "credential=<redacted>" in repr(resolved)
        assert KEY not in repr(resolved)
        senv.context = env.context
        senv.strategies = {}  # a kind whose strategy refuses is skipped
        with pytest.raises(AccessRefused) as refused:
            await resolve(senv, desired())
        assert refused.value.reason == "model_access_unavailable"
        senv.strategies = BUILTIN
        assert (await resolve(senv, desired())).choice.access == "api_key"

    run(store_dsn, body)


def test_small_helpers() -> None:
    assert access.upstream_anthropic(object()) == access.DEFAULT_UPSTREAM_ANTHROPIC
    assert access.budget_stamp(None) is None
    assert access.budget_stamp({"usd_per_day": 1.0, "spent_usd": 0.5}) == (1.0, 0.5)
    holder = type("Holder", (), {})()
    assert provisions(holder) is provisions(holder)
    assert Stamp(access="platform", owner_id="o") == Stamp(access="platform", owner_id="o")
