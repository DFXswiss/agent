"""U-RUN-identity / reconcile: the life of an org device after `agent-runner enroll --code` (04-crypto.md section 5.8,
05-runner.md sections 2.2, 2.5 and 3.5): its verified copy makes it act for its workgroup, it wraps the package keys
for the entitled members (catch-up), a later org on the hub is never adopted, and its removal (`key_revoked`) ends
everything."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_runner.assignments import reconcile as assignment_step
from agent_runner.assignments import tasks as revocation
from agent_runner.assignments import teardown
from agent_runner.catchup import reconcile as catchup_step
from agent_runner.config import load_config
from agent_runner.enroll import org
from agent_runner.orgs.book import book_for
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS as BASE_STEPS
from agent_runner.vm import lima
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import TokenStore

from ..reconcile.rig import ROOT, Env, org_log, pid
from ..vm.conftest import FakeLimactl
from .orgkit import HOST, ODEV, ONE, WALL, OrgHub, OrgWorld, cluster_tools, code_text, odev_keys, org_env


def enroll_odev(hub: OrgHub, home: Path, dsn: str, *, make_host: Callable[..., Any], env_for: Any) -> None:
    env = env_for(home, AGENT_RUNNER_HUB_URL=f"http://{HOST}", AGENT_RUNNER_PLATFORM_ROOT_KEY=ROOT)
    host = make_host(env, cluster=cluster_tools(dsn, [])).host
    keys = odev_keys()
    options = org.EnrollOptions(
        transport=httpx.ASGITransport(app=hub),  # type: ignore[arg-type]
        wall=lambda: WALL,
        generate=lambda: keys,
        new_id=lambda: pid(ODEV),
        arch="arm64",
        physical_mib=lambda: 65536,
        store_running=lambda path: True,
    )
    org.run_enroll(load_config(env, platform="darwin"), code_text(), host, options)


async def once(env: Env) -> Run:
    engine = getattr(env.context, "reconcile_engine", None)
    if engine is None:
        engine = Engine(
            env.context, sorted([*BASE_STEPS, *assignment_step.STEPS, *catchup_step.STEPS], key=lambda s: s.order)
        )
        env.context.reconcile_engine = engine
    run = await engine.run_once()
    assert run is not None
    return run


def test_an_enrolled_org_device_acts_wraps_and_is_ended_by_its_removal(
    store_dsn: str, runner_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    make_host = request.getfixturevalue("make_host")
    env_for = request.getfixturevalue("env_for")
    monkeypatch.setattr(teardown, "service_for", lambda context: None)
    world = OrgWorld()
    hub = OrgHub(world)
    enroll_odev(hub, runner_home, store_dsn, make_host=make_host, env_for=env_for)
    uploaded: list[dict[str, Any]] = []
    original = hub.route

    def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        if (method, target) == ("POST", "/v1/device/wraps"):
            hub.check("api/device-wraps-post.request.json", body)
            uploaded.extend(body["wraps"])
            return 200, {"stored": len(body["wraps"]), "ignored": 0}
        return original(method, target, body)

    hub.route = route  # type: ignore[method-assign]
    spec = world.spec

    async def body() -> None:
        async with org_env(store_dsn, world, hub) as env:
            env.context._vm_manager = VmManager(
                VmEnv(
                    lima=lima.Lima(FakeLimactl(lima_dir=tmp_path / "lima"), prefix=""),
                    db=env.pool,
                    settings=VmSettings(
                        cpus=4, memory_mib=8192, disk_gib=20, pool_size=0, memory_reserve_mib=0, vm_idle_seconds=1800
                    ),
                    tokens=TokenStore(tmp_path / "vm"),
                    physical_mib=lambda: 65536,
                )
            )
            run = await once(env)
            assert run.halted is None, env.log()
            book = book_for(env.context)
            assert book.org_device
            assert book.assignments() == [(spec.org, ONE)]
            assert sorted((w["scope"], w["recipient_id"]) for w in uploaded) == sorted(
                [
                    ("org", pid("alice")),
                    ("org", pid("bob")),
                    ("org", pid(ODEV)),
                    ("wg", pid("alice")),  # the creator of the workgroup has write on it
                    ("wg", pid("bob")),
                    ("wg", pid(ODEV)),
                ]
            )
            # an org the device never enrolled in is not adopted from the hub's answer
            stranger = world.add_org(org_log("s", device=None))
            run = await once(env)
            assert run.halted is None
            assert stranger.org not in book.logs
            del world.orgs[stranger.org]
            # removal: the hub refuses the device's key from now on
            spec.log.add("remove", "alice", subject=pid(ODEV))

            def refused(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
                return 401, {"error": "key_revoked"}

            hub.route = refused  # type: ignore[method-assign]
            task = asyncio.ensure_future(revocation.run_revocation(env.context))
            # the revocation task may record the revocation while a run is going on: that run already halts as
            # `revoked` (the first one never does: the task confirms a refusal only after `CONFIRM_S`)
            halts = []
            for _ in range(5):
                halts.append((await once(env)).halted)
                for _ in range(20):
                    await asyncio.sleep(0)
                if await teardown.revoked(env.context) is not None:
                    break
                env.clock.now += 31
            assert halts[0] == "failed"
            assert set(halts) <= {"failed", "revoked"}
            env.context.stopping.set()
            await asyncio.wait_for(task, 5)
            assert await env.rows("SELECT count(*) FROM runner.scope_key") == [(0,)]
            assert await teardown.revoked(env.context) == "key_revoked"

    asyncio.run(body())
