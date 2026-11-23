"""U-RUN-model: a gateway that lost its credential (restart of `agent-guest-api`) is provisioned again at once
(05-runner.md sections 6.1, 10.2; 03-api.md section 7.1; AUD-01 A-04, decisions/KOR-02.md).

The guest is the fake guest of the session kit plus `GatewayDouble`, which behaves like the API process of the guest:
the credential lives in its memory only, a restart loses it, and a model request without one answers `503` and
journals `guest.error credential_missing`. Store, credential records, journal loop and outbox are real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_runner.guestlink.errors import GuestRefused
from agent_runner.model import gateways
from agent_runner.model.access import provisions
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import HANDLERS
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..sessions.kit import desired, session_id
from .kit import (
    API_KEY,
    Env,
    Kit,
    api_key_body,
    attach,
    credentials,
    model_env,
    model_kit,
    store_record,
    subscription_body,
)

Body = Callable[[Env], Awaitable[None]]


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            await body(env)

    asyncio.run(main())


@dataclass
class GatewayDouble:
    """The guest's API process: credentials in memory, taken from the runner's `PUT .../credential`."""

    found: Kit
    held: dict[str, dict[str, Any]] = field(default_factory=dict)

    def take(self) -> None:
        for sid, credential in credentials(self.found):
            self.held[sid] = credential

    def restart(self) -> None:
        self.held.clear()
        self.found.guest.calls.clear()

    def request(self, sid: str) -> int:
        """One model request of the session: 200 with a credential, else 503 and the journal record."""
        self.take()
        if sid not in self.held:
            self.found.guest.emit(sid, "guest.error", {"code": "credential_missing"})
            return 503
        return 200


async def running(env: Env, preference: str = "api_key") -> tuple[Kit, GatewayDouble]:
    store_record(env, "anthropic_api_key", api_key_body())
    store_record(env, "claude_subscription", subscription_body())
    found = await model_kit(env)
    assert (await Starter(found.session_env).start(desired(model_access_pref=preference))).started
    gateway = GatewayDouble(found)
    gateway.take()
    found.guest.calls.clear()
    attach(found)
    return found, gateway


async def consume(found: Kit) -> VmJournal:
    """Read the guest's journal until it is acknowledged, then wait for the provisioning tasks."""
    row = await vm_records.get(found.env.pool, "vm-00000001")
    assert row is not None
    reader = VmJournal(found.session_env, row, handlers=HANDLERS)
    stopping = asyncio.Event()
    task = asyncio.ensure_future(reader.run(stopping))
    last = found.guest.records[-1].seq
    for _ in range(500):
        if found.guest.acked >= last or task.done():
            break
        await asyncio.sleep(0.005)
    stopping.set()
    await task
    await settled(found)
    return reader


async def settled(found: Kit) -> None:
    pending = list(gateways.reprovisions(found.env.context).tasks.values())
    await asyncio.gather(*pending, return_exceptions=True)


def text(found: Kit) -> str:
    return "\n".join(found.lines)


def test_the_next_model_request_succeeds_after_a_restart_of_guest_api(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, gateway = await running(env)
        sid = session_id()
        assert gateway.request(sid) == 200
        gateway.restart()
        assert gateway.request(sid) == 503
        await consume(found)
        assert gateway.request(sid) == 200  # no user action: the runner provisioned again
        [(put_sid, credential)] = credentials(found)
        assert (put_sid, credential["auth_value"], credential["access"]) == (sid, API_KEY, "api_key")
        assert provisions(env.context).stamps[sid].version == 1
        assert f"credential_reprovisioned session_id={sid} access=api_key" in text(found)
        assert (await found.stored(sid)).state == "agent_starting"
        assert API_KEY not in text(found)

    run(store_dsn, body)


def test_without_a_usable_credential_the_session_ends_with_model_access_unavailable(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, gateway = await running(env)
        sid = session_id()
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")  # the owner's record is gone
        gateway.restart()
        assert gateway.request(sid) == 503
        await consume(found)
        assert (await found.transitions())[-1] == ("failed", "model_access_unavailable")
        assert credentials(found)[-1][1]["models_allowed"] == []
        assert ("control", (sid, "stop")) in found.guest.calls
        assert f"access_withdrawn session_id={sid} access=api_key cause=credential_missing" in text(found)

    run(store_dsn, body)


def test_a_credential_that_cannot_be_used_now_is_provisioned_by_the_next_renewal(store_dsn: str) -> None:
    # a platform key file that cannot be read now (or a record that does not open or cannot be fetched now) is no
    # deleted credential: the session stays, the renewal provisions it once the key is usable again
    async def body(env: Env) -> None:
        found, gateway = await running(env, "platform")
        sid = session_id()
        key = found.session_env.platform_key
        found.session_env.platform_key = lambda: None
        gateway.restart()
        assert gateway.request(sid) == 503
        await consume(found)
        assert (await found.stored(sid)).state == "agent_starting"
        assert f"reprovision_deferred session_id={sid}" in text(found)
        assert credentials(found) == []
        found.session_env.platform_key = key
        await gateways.refresh_all(found.session_env, set())
        assert gateway.request(sid) == 200

    run(store_dsn, body)


def test_stale_records_after_a_provisioning_change_nothing_until_the_cooldown_ends(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, gateway = await running(env, "subscription")
        sid = session_id()
        now = [1000.0]
        found.session_env.clock = lambda: now[0]
        gateway.restart()
        for _ in range(3):  # Claude Code retries 503: a burst of records, read over several pages
            assert gateway.request(sid) == 503
        await consume(found)
        found.guest.emit(sid, "guest.error", {"code": "credential_missing"})  # a late record of the same burst
        await consume(found)
        [(_sid, credential)] = credentials(found)
        assert credential["access"] == "subscription"
        assert gateway.request(sid) == 200
        now[0] += gateways.COOLDOWN_S
        gateway.restart()
        assert gateway.request(sid) == 503
        await consume(found)
        assert gateway.request(sid) == 200
        assert len(credentials(found)) == 1
        # the time of the last provisioning is dropped with the session
        assert sid in gateways.reprovisions(env.context).done_at
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'stopped' WHERE id = %s", (sid,))
        await gateways.refresh_all(found.session_env, set())
        assert gateways.reprovisions(env.context).done_at == {}

    run(store_dsn, body)


def test_a_guest_that_does_not_take_it_now_is_provisioned_by_the_next_renewal(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, gateway = await running(env)
        sid = session_id()
        gateway.restart()
        found.guest.fail["put_credential"] = GuestRefused("credential", 404, "not_found")
        assert gateway.request(sid) == 503
        await consume(found)
        assert sid not in provisions(env.context).stamps
        del found.guest.fail["put_credential"]
        await gateways.refresh_all(found.session_env, set())
        assert gateway.request(sid) == 200
        assert f"credential_renewed session_id={sid} access=api_key" in text(found)

    run(store_dsn, body)


def test_sessions_that_must_not_be_provisioned_again_are_left_alone(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, _gateway = await running(env)
        sid = session_id()
        service = attach(found)
        session = await found.stored(sid)
        # a start or stop of the session runs: it provisions (or ends) the session itself
        busy: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        service.tasks[sid] = asyncio.ensure_future(busy)
        await gateways.reprovision(found.session_env, sid)
        del service.tasks[sid]
        busy.cancel()
        # withdrawn before, unknown, ended, or without model access
        provisions(env.context).withdrawn.add(sid)
        await gateways.reprovision(found.session_env, sid)
        provisions(env.context).withdrawn.clear()
        await gateways.reprovision(found.session_env, "01930005-0000-7000-8000-0000000000aa")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET model_access = NULL WHERE id = %s", (sid,))
        await gateways.reprovision(found.session_env, sid)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'stopped' WHERE id = %s", (sid,))
        await gateways.reprovision(found.session_env, sid)
        assert credentials(found) == []
        # one provisioning at a time per session
        gateways.credential_missing(found.session_env, session)
        gateways.credential_missing(found.session_env, session)
        assert len(gateways.reprovisions(env.context).tasks) == 1
        await settled(found)
        assert gateways.reprovisions(env.context).tasks == {}

    run(store_dsn, body)


def test_a_failing_provisioning_is_logged(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, _gateway = await running(env)
        sid = session_id()
        found.guest.fail["put_credential"] = RuntimeError("boom")
        gateways.credential_missing(found.session_env, await found.stored(sid))
        await settled(found)
        assert f"reprovision_failed session_id={sid} error=RuntimeError" in text(found)
        # a cancelled provisioning (daemon shutdown) is not reported
        found.guest.fail.clear()
        gateways.credential_missing(found.session_env, await found.stored(sid))
        for task in gateways.reprovisions(env.context).tasks.values():
            task.cancel()
        await settled(found)
        assert text(found).count("reprovision_failed") == 1

    run(store_dsn, body)


def test_records_without_a_local_session_change_nothing(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, _gateway = await running(env)
        found.guest.emit(None, "guest.error", {"code": "credential_missing"})
        found.guest.emit("01930005-0000-7000-8000-0000000000aa", "guest.error", {"code": "credential_missing"})
        await consume(found)
        assert credentials(found) == []
        assert "foreign_session" in text(found)

    run(store_dsn, body)
