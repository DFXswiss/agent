"""U-RUN-dfxkeys: the DFX key issuer of a platform device end to end against the model double's OpenRouter key
management API and a real store (ADR-081 section 7): announcement, issue, crash orphan cleanup, the org device's use
of the key (platform access, `402` at the limit), cumulative usage reports, limit updates within the issuer's cap,
disable, revoke with the final spend - and neither the management key nor the platform key ever reaches the org
device or a log."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from agent_doubles.common.client import DoubleClient
from agent_doubles.model.server import ModelDouble
from agent_runner.dfxkeys.issuer import ATTRIBUTE as ISSUER_ATTRIBUTE
from agent_runner.dfxkeys.issuer import Issuer, IssuerOptions, issuer_for
from agent_runner.model import provision
from agent_runner.model.catalog import load_catalog
from agent_runner.model.dfxkey import accept, platform_key
from agent_runner.sessions.records import time_text

from ..reconcile.rig import pid
from .kit import (
    COST,
    FALLBACK,
    ISSUER,
    MANAGEMENT,
    NOW,
    ODEV,
    ORG,
    PLATFORM,
    QUESTION,
    REQUEST,
    REQUEST_TWO,
    Issuing,
    double,
    held_entry,
    issuer_env,
    issuer_state,
    key_entry,
    org_context,
    request_entry,
    store_text,
    write_secret,
)

MICRO = 1_000_000


def chat(started: ModelDouble, key: str) -> int:
    return (
        DoubleClient(started.url).request("POST", "/v1/messages", QUESTION, {"Authorization": f"Bearer {key}"}).status
    )


def provider(started: ModelDouble, key_ref: str) -> dict[str, Any]:
    found = started.keys.get(key_ref)
    assert found is not None
    return found.document()


def key_calls(started: ModelDouble, key_ref: str) -> int:
    """Requests the double received for one key (`GET`, `PATCH`, `DELETE /v1/keys/{key_ref}`)."""
    return sum(1 for entry in started.exchanges if entry["path"] == f"/v1/keys/{key_ref}")


def issuer(issuing: Issuing) -> Issuer:
    found = issuer_for(issuing.context)
    assert found is not None
    return found


async def issue(found: Issuer, issuing: Issuing, started: ModelDouble) -> dict[str, Any]:
    """Announce, issue once (also when the hub lists the request again) and return the `dfx_key_issued` body."""
    assert issuer_for(issuing.context) is found  # one per daemon
    await found.observe(issuer_state([request_entry()]))
    assert await issuing.items("dfx_key_state") == [{"issuer": True}]
    await found.tick(NOW)
    [issued] = await issuing.items("dfx_key_issued")
    key_ref = issued["key_ref"]
    assert issued == {
        "request_id": REQUEST,
        "org_id": ORG,
        "device_id": pid(ODEV),
        "key_ref": key_ref,
        "envelope": issued["envelope"],
    }
    assert provider(started, key_ref)["name"] == f"dfx-{REQUEST}"
    assert provider(started, key_ref)["limit"] == COST  # the room the request grants
    # the hub still lists the request until the item is pushed: nothing is issued twice
    await found.observe(issuer_state([request_entry()]))
    await found.tick(NOW + 5)
    assert len(started.keys.documents()) == 1
    assert len(await issuing.items("dfx_key_issued")) == 1
    assert await issuing.items("dfx_key_state") == [{"issuer": True}]  # announced once
    return issued


async def use(org: Any, issued: dict[str, Any], started: ModelDouble) -> tuple[str, dict[str, Any]]:
    """The org device accepts the envelope and gets the sub-key - and only the sub-key - for its provisioning."""
    await accept(org, {"dfx_key_held": [held_entry(issued)]})
    sub = platform_key(org, ORG)
    secret = started.keys.get(issued["key_ref"])
    assert secret is not None
    assert sub == secret.secret
    body = provision.platform_credential(key=sub, upstream=started.url, catalog=load_catalog(None), budget=None)
    assert body["auth_value"] == f"Bearer {sub}"
    assert chat(started, sub) == 200
    assert chat(started, sub) == 402  # the key's limit is reached until the issuer raises it
    return sub, body


async def measure(found: Issuer, issuing: Issuing, started: ModelDouble, key_ref: str, sub: str) -> None:
    """The first poll reports the spend and sets the limit; spend is reported cumulatively; the cap bounds the room."""
    await found.observe(issuer_state([], [key_entry(key_ref, headroom=1.0)]))
    await found.tick(NOW + 10)
    assert provider(started, key_ref)["limit"] == COST + 1.0
    assert chat(started, sub) == 200
    assert chat(started, sub) == 200
    await found.tick(NOW + 30)  # not due
    await found.tick(NOW + 70)
    assert provider(started, key_ref)["limit"] == 3 * COST + 1.0
    # a hub that grants more than the issuer's cap gets the cap (5 USD)
    await found.observe(issuer_state([], [key_entry(key_ref, headroom=100)]))
    await found.tick(NOW + 71)  # the entry changed: polled at once
    assert provider(started, key_ref)["limit"] == 3 * COST + 5.0
    await found.tick(NOW + 131)  # due, but nothing changed: no report, no update
    assert provider(started, key_ref)["limit"] == 3 * COST + 5.0
    usage = await issuing.items("dfx_key_usage")
    assert [item["spent_micro_usd"] for item in usage] == [int(COST * MICRO), int(3 * COST * MICRO)]
    assert usage[1] == {
        "key_ref": key_ref,
        "org_id": ORG,
        "device_id": pid(ODEV),
        "spent_micro_usd": 750_000,
        "at": time_text(NOW + 70),
    }


async def disable_and_revoke(found: Issuer, issuing: Issuing, started: ModelDouble, key_ref: str, sub: str) -> None:
    # exhausted organization: switched off at the provider, then on again with credit
    await found.observe(issuer_state([], [key_entry(key_ref, headroom=1.0, disabled=True)]))
    await found.tick(NOW + 72)
    assert provider(started, key_ref)["disabled"] is True
    assert chat(started, sub) == 401
    await found.observe(issuer_state([], [key_entry(key_ref, headroom=1.0)]))
    await found.tick(NOW + 73)
    assert provider(started, key_ref)["disabled"] is False
    assert chat(started, sub) == 200
    # the device leaves: the final spend is read and reported, then the key is deleted
    await found.observe(issuer_state([], [key_entry(key_ref, revoke=True)]))
    await found.tick(NOW + 74)
    assert started.keys.documents() == []
    spent = [item["spent_micro_usd"] for item in await issuing.items("dfx_key_usage")]
    assert spent == [250_000, 750_000, 1_000_000]
    # the hub lists the key until it processed that report: the last spend is repeated every 60 s with a fresh
    # `at`, without calling the provider and without logging
    log = issuing.env.log()
    calls = key_calls(started, key_ref)
    await found.tick(NOW + 100)  # not due
    assert len(await issuing.items("dfx_key_usage")) == 3
    await found.tick(NOW + 134)
    assert key_calls(started, key_ref) == calls
    repeated = (await issuing.items("dfx_key_usage"))[-1]
    assert (repeated["spent_micro_usd"], repeated["at"]) == (1_000_000, time_text(NOW + 134))
    assert issuing.env.log() == log
    await found.observe(issuer_state())
    await found.tick(NOW + 300)
    assert found.keys == {}
    assert await issuing.env.rows("SELECT request_id FROM runner.dfx_key_issued") == []


def test_issue_use_measure_limit_disable_and_revoke(store_dsn: str, org_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing, org_context(org_dsn) as org:
                found = issuer(issuing)
                issued = await issue(found, issuing, started)
                sub, body = await use(org, issued, started)
                await measure(found, issuing, started, issued["key_ref"], sub)
                await disable_and_revoke(found, issuing, started, issued["key_ref"], sub)
                # attack case: no key of the platform reaches the org device or a log
                org_seen = await store_text(org.pool) + org.log.getvalue() + str(held_entry(issued)) + str(body)
                for secret_text in (MANAGEMENT, PLATFORM, "MGMTKEYMARKER", "PLATKEYMARKER"):
                    assert secret_text not in org_seen
                issuer_seen = await store_text(issuing.env.pool) + issuing.env.log()
                for secret_text in ("MGMTKEYMARKER", "PLATKEYMARKER", sub):
                    assert secret_text not in issuer_seen
                assert sub not in org.log.getvalue()
                assert sub not in await store_text(org.pool)
                assert started.state()["failures"] == []

    asyncio.run(main())


def test_a_key_left_behind_by_a_crash_is_deleted_before_the_new_one(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                orphan, _ = started.keys.create({"name": f"dfx-{REQUEST}", "limit": 3})
                unrelated, _ = started.keys.create({"name": f"dfx-{REQUEST_TWO}", "limit": 3})
                started.keys.page_size = 1  # the lookup by name reads every page
                found = issuer(issuing)
                await found.observe(issuer_state([request_entry()]))
                await found.tick(NOW)
                names = sorted(doc["name"] for doc in started.keys.documents())
                assert names == [f"dfx-{REQUEST}", f"dfx-{REQUEST_TWO}"]
                assert started.keys.get(orphan.hash) is None
                assert started.keys.get(unrelated.hash) is not None
                assert f"orphan_deleted request_id={REQUEST} key_ref={orphan.hash}" in issuing.env.log()

    asyncio.run(main())


def test_no_issuer_without_a_management_key_or_an_older_hub(store_dsn: str, tmp_path: Path) -> None:
    """A device that never was an issuer sends nothing; an older hub gets nothing at all."""

    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path, management=None) as issuing:
                found = issuer(issuing)
                await found.observe({"dfx_keys": {"requests": [request_entry()]}})  # an older hub
                await found.tick(NOW)
                await found.observe(issuer_state([request_entry()]))
                await found.tick(NOW)
                await found.observe(issuer_state([request_entry()]))
                assert await issuing.items("dfx_key_state") == []
                assert started.keys.documents() == []
                assert found.announced is False

    asyncio.run(main())


def test_an_issuer_that_loses_its_key_withdraws_also_after_a_restart(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                found = issuer(issuing)
                await found.observe(issuer_state())
                key_file = issuing.context.config.openrouter_management_key_file
                key_file.unlink()
                await found.observe(issuer_state())
                assert await issuing.items("dfx_key_state") == [{"issuer": True}, {"issuer": False}]
                write_secret(key_file, MANAGEMENT)
                await found.observe(issuer_state())
                # a restarted daemon announces `true` once more; without the file it withdraws the stored `true`
                delattr(issuing.context, ISSUER_ATTRIBUTE)
                await issuer(issuing).observe(issuer_state())
                key_file.unlink()
                delattr(issuing.context, ISSUER_ATTRIBUTE)
                await issuer(issuing).observe(issuer_state())
                delattr(issuing.context, ISSUER_ATTRIBUTE)
                await issuer(issuing).observe(issuer_state())
                states = [item["issuer"] for item in await issuing.items("dfx_key_state")]
                assert states == [True, False, True, True, False]
                assert "announced issuer=false" in issuing.env.log()

    asyncio.run(main())


class Faults:
    """The requests a test makes fail: `(method, path prefix) -> status`, or None for a transport error."""

    def __init__(self, faults: dict[tuple[str, str], int | None]) -> None:
        self.faults = faults


class FaultTransport(httpx.AsyncBaseTransport):
    """The real double, except for the requests `Faults` names (one transport per client, closed with it)."""

    def __init__(self, faults: Faults) -> None:
        self.state = faults
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for (method, prefix), status in self.state.faults.items():
            if request.method == method and request.url.path.startswith(prefix):
                if status is None:
                    raise httpx.ConnectError("refused", request=request)
                return httpx.Response(status, json={}, request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def faulty(issuing: Issuing, faults: dict[tuple[str, str], int | None]) -> Faults:
    state = Faults(faults)
    issuing.context.dfx_key_options = IssuerOptions(
        client=lambda: httpx.AsyncClient(
            transport=FaultTransport(state), timeout=8, follow_redirects=False, trust_env=False
        )
    )
    return state


def test_provider_failures_are_logged_and_retried(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                transport = faulty(issuing, {("POST", "/v1/keys"): 500})
                found = issuer(issuing)
                await found.observe(issuer_state([request_entry()]))
                await found.tick(NOW)
                assert f"issue_failed request_id={REQUEST} reason=status_500" in issuing.env.log()
                assert await issuing.items("dfx_key_issued") == []
                transport.faults = {}
                await found.tick(NOW + 5)
                [issued] = await issuing.items("dfx_key_issued")
                key_ref = issued["key_ref"]
                # reading the key fails: logged, polled again after the interval
                transport.faults = {("GET", f"/v1/keys/{key_ref}"): None}
                await found.observe(issuer_state([], [key_entry(key_ref)]))
                await found.tick(NOW + 10)
                assert f"poll_failed key_ref={key_ref} reason=ConnectError" in issuing.env.log()
                # changing it fails: logged
                transport.faults = {("PATCH", "/v1/keys/"): 503}
                await found.tick(NOW + 70)
                assert f"update_failed key_ref={key_ref} reason=status_503" in issuing.env.log()
                # deleting it fails: the revoke is tried again
                transport.faults = {("DELETE", "/v1/keys/"): 500}
                await found.observe(issuer_state([], [key_entry(key_ref, revoke=True)]))
                await found.tick(NOW + 80)
                assert f"update_failed key_ref={key_ref} reason=status_500" in issuing.env.log()
                transport.faults = {}
                await found.tick(NOW + 140)
                assert started.keys.documents() == []
                assert [i["spent_micro_usd"] for i in await issuing.items("dfx_key_usage")] == [0, 0, 0]
                # a key the provider no longer knows
                await found.observe(issuer_state([], [key_entry("f" * 64), key_entry("g" * 64, revoke=True)]))
                await found.tick(NOW + 150)
                await found.tick(NOW + 151)
                log = issuing.env.log()
                assert f"key_missing key_ref={'f' * 64}" in log
                assert log.count(f"key_missing key_ref={'g' * 64}") == 1
                # a revoked key the provider no longer knows (deleted before a restart) is reported with the last
                # known spend (none in this daemon: 0), so the hub can close it; then repeated without provider calls
                usage = await issuing.items("dfx_key_usage")
                assert (usage[-1]["key_ref"], usage[-1]["spent_micro_usd"]) == ("g" * 64, 0)
                calls = key_calls(started, "f" * 64), key_calls(started, "g" * 64)
                await found.tick(NOW + 300)
                after = issuing.env.log()
                assert after.count(f"key_missing key_ref={'f' * 64}") == 1  # logged once, not at every poll
                assert after.count(f"key_missing key_ref={'g' * 64}") == 1
                assert (key_calls(started, "f" * 64), key_calls(started, "g" * 64)) == (calls[0] + 1, calls[1])
                assert [u["key_ref"] for u in await issuing.items("dfx_key_usage")][-1] == "g" * 64

    asyncio.run(main())


def test_a_key_that_cannot_be_sealed_is_deleted_again(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                transport = faulty(issuing, {})
                found = issuer(issuing)
                small_order = "A" * 43  # the all-zero X25519 point: no shared secret
                await found.observe(issuer_state([request_entry(kem=small_order)]))
                await found.tick(NOW)
                assert f"issue_failed request_id={REQUEST} reason=seal" in issuing.env.log()
                assert started.keys.documents() == []
                assert await issuing.items("dfx_key_issued") == []
                # and when that delete fails too, the next round's cleanup by name removes it
                transport.faults = {("DELETE", "/v1/keys/"): 500}
                await found.tick(NOW + 5)
                assert "delete_failed key_ref=" in issuing.env.log()
                assert len(started.keys.documents()) == 1
                transport.faults = {("POST", "/v1/keys"): 500}
                await found.tick(NOW + 10)
                assert started.keys.documents() == []

    asyncio.run(main())


def test_malformed_entries_are_skipped(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                found = issuer(issuing)
                requests: list[Any] = [
                    "x",
                    {**request_entry(), "extra": 1},
                    request_entry(request_id="nope"),
                    request_entry(kem="short"),
                    request_entry(REQUEST_TWO, headroom_usd=2.5),
                ]
                keys: list[Any] = [
                    "x",
                    {**key_entry("h" * 64), "extra": 1},
                    key_entry("bad ref"),
                    {**key_entry("h" * 64), "org_id": "nope"},
                    {**key_entry("h" * 64), "disabled": "no"},
                    {**key_entry("h" * 64), "revoke": 1},
                ]
                await found.observe(issuer_state(requests, keys))
                await found.tick(NOW)
                log = issuing.env.log()
                assert "requests_invalid count=4" in log
                assert "keys_invalid count=6" in log
                [doc] = started.keys.documents()
                assert doc["limit"] == 2.5  # an optional initial headroom of the request
                await found.observe({**FALLBACK, "dfx_keys": "nonsense"})
                await found.tick(NOW + 5)
                await found.observe(issuer_state([], [key_entry(doc["hash"], headroom="lots")]))
                await found.tick(NOW + 6)
                assert provider(started, doc["hash"])["limit"] == 0  # malformed headroom: no room

    asyncio.run(main())


def test_the_issuer_only_exists_on_an_enrolled_platform_device(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                context = issuing.context
                identity = context.identity
                context.identity = None
                assert issuer_for(context) is None
                context.identity = identity
                device = identity.device
                context.identity = replace(identity, device=replace(device, kind="org"))
                assert issuer_for(context) is None

    asyncio.run(main())


def test_issuer_identity_in_the_envelope(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                found = issuer(issuing)
                await found.observe(issuer_state([request_entry()]))
                await found.tick(NOW)
                [issued] = await issuing.items("dfx_key_issued")
                assert held_entry(issued)["issuer_device_id"] == ISSUER == found.device_id

    asyncio.run(main())
