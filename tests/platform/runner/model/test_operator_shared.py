"""U-RUN-model (KOR-69, ADR-090 section 3): shared subscriptions of users in the operator pool. Entries `sub-<record>`
come from `subscriptions.shared` of the desired state, are read from verified records and serve only the organizations
their sealed `use` allows (`orgs`, or `pool` for an owner with the platform role `operator` and an organization the
platform log grants); without a directory the pool holds them alone."""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner.credentials import subscriptions
from agent_runner.metrics.sampler import Sampler
from agent_runner.model import access, exhaustion, gateways, limits, operator, provision
from agent_runner.model.operator import BLOCKED, USABLE, Entry, Pool, Use
from agent_runner.quota import operator as quota_operator
from agent_runner.sessions.records import time_text

from ..credentials.kit import ALICE, BOB, Env, ck, kit_env, make_record
from ..credentials.subscription_kit import KIND, SubscriptionHub, subscription_body
from .test_operator import Clock, pool_dir, put, token

ORG = "0b9c6a52-4a35-4c1e-9d8e-8f3a0e8d5b11"
OTHER = "1b9c6a52-4a35-4c1e-9d8e-8f3a0e8d5b12"
VALID = token(7)
Body = Callable[[Env, SubscriptionHub], Awaitable[None]]


def shared_pool(clock: Clock | None = None, directory: Path | None = None) -> Pool:
    return Pool(directory=directory, orgs=frozenset(), clock=clock or Clock(), wall=lambda: 5000.0)


def add_shared(pool: Pool, name: str, *, version: int = 1, pool_flag: bool = False, orgs: tuple[str, ...] = ()) -> None:
    pool.shared[name] = Entry((0, 0, version, 0), VALID, None)
    pool.uses[name] = Use(owner=ALICE, pool=pool_flag, orgs=frozenset(orgs), version=version)


def platform(roles: set[tuple[str, str]], granted: dict[str, bool]) -> Any:
    state = SimpleNamespace(operator_subscriptions=granted)
    state.has_role = lambda user, role: (user, role) in roles
    state.operator_subscribed = lambda org: granted.get(org, False)
    return state


def test_a_shared_entry_serves_only_what_its_sealed_use_allows(tmp_path: Path) -> None:
    pool = shared_pool()
    add_shared(pool, "sub-a", orgs=(ORG,))
    add_shared(pool, "sub-p", pool_flag=True)
    assert pool.allowed("sub-a", ORG)
    assert not pool.allowed("sub-a", OTHER)
    assert not pool.allowed("sub-a", None)
    assert not pool.allowed("sub-p", OTHER)  # no verified platform log yet
    pool.platform = lambda: cast(Any, platform({(ALICE, "operator")}, {OTHER: True, ORG: False}))
    assert pool.allowed("sub-p", OTHER)
    assert not pool.allowed("sub-p", ORG)  # the log revoked it
    pool.platform = lambda: cast(Any, platform(set(), {OTHER: True}))
    assert not pool.allowed("sub-p", OTHER)  # the owner is no operator
    assert not pool.allowed("nothing", ORG)
    pool.platform = lambda: None
    assert pool.serves(ORG)
    assert not pool.serves(OTHER)
    assert pool.choose(org_id=ORG) == "sub-a"
    assert pool.choose() is None  # without an organization: token files only
    assert pool.choose(org_id=OTHER) is None
    pool.scan()  # no directory: no files, the shared entries stay
    assert (pool.files, sorted(pool.entries())) == ({}, ["sub-a", "sub-p"])
    assert pool.state("sub-a") == USABLE
    assert pool.current("sub-a") == (VALID, (0, 0, 1, 0))
    assert VALID not in repr(pool)


def test_a_refused_shared_entry_is_blocked_until_a_new_login(tmp_path: Path) -> None:
    clock = Clock()
    directory = pool_dir(tmp_path)
    put(directory, "f", token(1))
    pool = shared_pool(clock, directory)
    pool.orgs = frozenset({ORG})
    add_shared(pool, "sub-a", orgs=(ORG,))
    pool.scan()
    assert sorted(pool.entries()) == ["f", "sub-a"]
    pool.delivered_at["s1"] = clock.now - 100
    pool.observe("sub-a", 401, (0, 0, 1, 0), "s1")
    assert pool.state("sub-a") == BLOCKED
    assert pool.choose(org_id=ORG) == "f"
    pool.scan()
    assert pool.state("sub-a") == BLOCKED  # a scan keeps the block of an unchanged shared entry
    add_shared(pool, "sub-a", version=2, orgs=(ORG,))
    pool.scan()
    assert pool.state("sub-a") == USABLE
    pool.deferred["sub-a"] = clock.now + 10
    pool.directory = None
    pool.blocked["gone"] = (1, 1, 1, 1)
    pool.scan()
    assert ("gone" in pool.blocked, "sub-a" in pool.deferred, pool.files) == (False, True, {})
    observed = pool.observe("missing", 401, None, "s2")
    assert observed is False


def test_the_shared_rows_of_the_desired_state_and_a_sealed_use_are_checked() -> None:
    record = "01930099-0000-7000-8000-0000000000c1"
    rows = [
        {"subscription_id": record, "owner_id": ALICE},
        {"subscription_id": "x", "owner_id": ALICE},
        {"subscription_id": record},
        "junk",
    ]
    assert operator.listed_shared({"subscriptions": {"api": 1, "shared": rows}}) == [(record, ALICE)]
    for desired in (None, {}, {"subscriptions": None}, {"subscriptions": {"shared": "x"}}):
        assert operator.listed_shared(desired) == []
    assert operator._use(ALICE, 1, {"pool": True, "orgs": [ORG]}) == Use(ALICE, True, frozenset({ORG}), 1)
    for value in (None, {"pool": "yes", "orgs": []}, {"pool": False, "orgs": "x"}, {"pool": False, "orgs": ["x"]}):
        assert operator._use(ALICE, 1, value) is None


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with kit_env(dsn) as env:
            await env.add_ck()
            await body(env, SubscriptionHub(env.hub).install())

    asyncio.run(main())


def listing(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str]]) -> None:
    shared = [{"subscription_id": record, "owner_id": owner} for record, owner in rows]
    desired = {"subscriptions": {"api": 1, "shared": shared}}
    monkeypatch.setattr(
        gateways, "running_service", lambda _context: SimpleNamespace(env=SimpleNamespace(desired=desired))
    )


def test_without_a_directory_the_pool_is_made_for_listed_shared_subscriptions(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        context = env.context
        assert operator._desired(context) is None  # no session service
        assert operator.ensure(SimpleNamespace(config=None)) is None
        assert await operator.refreshed(context) is None
        record = hub.add(subscription_body(token=VALID, use={"pool": False, "orgs": [ORG]}), owner=ALICE)
        listing(monkeypatch, [(record, ALICE)])
        pool = await operator.refreshed(context)
        assert pool is not None
        assert operator.ensure(context) is pool
        assert pool.directory is None
        name = "sub-" + record
        assert sorted(pool.entries()) == [name]
        picked = await operator.pick(context, ORG)
        assert picked is not None
        assert picked[0] == name
        assert await operator.peek(context, ORG) == name
        assert await operator.pick(context, OTHER) is None
        assert await operator.peek(context, OTHER) is None
        before = pool.shared[name]
        await operator.refresh_shared(context, pool)
        assert pool.shared[name] is before  # the same version is not opened again
        # metrics and the operator's quota show token files only
        assert Sampler.operator(cast(Any, SimpleNamespace(context=context))) is None
        assert quota_operator.snapshot(context) is None
        # no longer listed: no entry
        listing(monkeypatch, [])
        await operator.refresh_shared(context, pool)
        assert pool.entries() == {}

    run(store_dsn, body)


def test_a_listed_record_that_cannot_serve_is_no_entry(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        context = env.context
        use = {"pool": False, "orgs": [ORG]}
        logged_out = hub.add(subscription_body(token=None, use=use), owner=ALICE)
        unused = hub.add(subscription_body(token=VALID), owner=ALICE)
        primary = hub.add(subscription_body(token=VALID, use=use), owner=ALICE, primary=True)
        theirs = hub.add(subscription_body(token=VALID, use=use), owner=ALICE)
        broken = "01930099-0000-7000-8000-0000000000c9"
        hub.served[broken] = hub.served[unused].__class__(
            ALICE, False, make_record(subscription_body(), owner=ALICE, kind=KIND, record_id=broken, key=ck(BOB))
        )
        missing = "01930099-0000-7000-8000-0000000000ca"
        hub.refusals[missing] = (403, {"error": "forbidden"})
        rows = [
            (logged_out, ALICE),
            (unused, ALICE),
            (primary, ALICE),
            (theirs, BOB),
            (broken, ALICE),
            (missing, ALICE),
        ]
        listing(monkeypatch, rows)
        pool = await operator.refreshed(context)
        assert pool is not None
        assert pool.entries() == {}
        log = context.log.getvalue()
        assert f"shared_unusable id={logged_out} reason=unused" in log
        assert f"shared_unusable id={unused} reason=unused" in log
        assert f"shared_unusable id={broken} reason=invalid" in log
        assert f"shared_unusable id={missing} reason=fetch" in log
        asked = hub.asked.count(missing)
        await operator.refresh_shared(context, pool)
        assert hub.asked.count(missing) == asked  # not asked again within FETCH_RETRY_S
        pool.failed[missing] -= operator.FETCH_RETRY_S
        await operator.refresh_shared(context, pool)
        assert hub.asked.count(missing) == asked + 1

    run(store_dsn, body)


def test_a_bound_session_moves_when_its_use_no_longer_allows_its_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = shared_pool()
    add_shared(pool, "sub-a", orgs=(OTHER,))
    add_shared(pool, "sub-b", orgs=(ORG,))
    bound: list[tuple[str, str]] = []

    async def refreshed(_context: Any) -> Pool:
        return pool

    async def bind(_context: Any, _pool: Pool, session_id: str, name: str) -> None:
        bound.append((session_id, name))

    monkeypatch.setattr(operator, "refreshed", refreshed)
    monkeypatch.setattr(operator, "bind", bind)
    monkeypatch.setattr(access, "upstream_anthropic", lambda _context: "https://upstream.invalid")
    monkeypatch.setattr(provision, "owner_credential", lambda **_kw: {"auth_value": "x"})
    context = SimpleNamespace(log=io.StringIO())
    env = SimpleNamespace(context=context, catalog=None, log=lambda text: context.log.write(text + "\n"))
    session = SimpleNamespace(id="s1", org_id=ORG, owner_id=ALICE)
    target = asyncio.run(gateways.operator_target(cast(Any, env), cast(Any, session), "sub-a"))
    assert bound == [("s1", "sub-b")]
    assert target is not None
    assert "operator_file_switched session_id=s1 from=sub-a to=sub-b" in context.log.getvalue()


def test_a_token_file_may_not_use_the_shared_prefix_and_shared_tokens_are_read_for_their_quota(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "sub-x", token(2))
    put(directory, "f", token(3))
    pool = shared_pool(directory=directory)
    add_shared(pool, "sub-a", orgs=(ORG,))
    pool.scan()
    assert sorted(pool.entries()) == ["f", "sub-a"]
    context = SimpleNamespace(**{operator.ATTRIBUTE: pool})
    token_of = asyncio.run(exhaustion.token_of(context, limits.operator_key("sub-a")))
    assert token_of == VALID


def test_a_full_login_serves_its_access_token_and_a_renewal_replaces_it(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KOR-71, ADR-092 section 3: a session gets only the access token of a full login, never its refresh token; a
    renewal is a new record version, which the pool reads anew (the bound sessions are provisioned again)."""

    async def body(env: Env, hub: SubscriptionHub) -> None:
        context = env.context
        use = {"pool": False, "orgs": [ORG]}
        refresh = "refresh-token-never-for-a-guest-0123456789"
        full = {**subscription_body(token=VALID, use=use), "kind": "oauth_login", "refresh_token": refresh}
        record = hub.add(full, owner=ALICE)
        listing(monkeypatch, [(record, ALICE)])
        pool = await operator.refreshed(context)
        assert pool is not None
        name = "sub-" + record
        picked = await operator.pick(context, ORG)
        assert picked is not None
        assert picked[:2] == (name, VALID)
        assert refresh not in repr(pool.shared) + repr(picked)
        renewed = token(8)
        hub.add({**full, "token": renewed, "refresh_token": "next-refresh-0123456789"}, owner=ALICE, version=2,
                record_id=record)  # fmt: skip
        await subscriptions.sync_owner(context, ALICE)  # the `credentials` hint of the new version
        await operator.refresh_shared(context, pool)
        assert pool.current(name) == (renewed, (0, 0, 2, 0))

    run(store_dsn, body)


def test_an_expired_token_is_never_handed_out_from_the_device_copy(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOT-26: the measuring device renewed the login, this device missed the hint and still holds the expired
    version. A new or restarted session must get the current token, not "401 OAuth access token has expired"."""

    async def body(env: Env, hub: SubscriptionHub) -> None:
        context = env.context
        use = {"pool": False, "orgs": [ORG]}
        now = time.time()
        full = {
            **subscription_body(token=VALID, use=use),
            "kind": "oauth_login",
            "refresh_token": "refresh-token-never-for-a-guest-0123456789",
            "expires_at": time_text(now + 3600),
        }
        record = hub.add(full, owner=ALICE)
        listing(monkeypatch, [(record, ALICE)])
        pool = await operator.refreshed(context)
        assert pool is not None
        name = "sub-" + record
        assert pool.current(name) == (VALID, (0, 0, 1, 0))
        # renewed at the hub; no hint reaches this device
        renewed = token(8)
        hub.add({**full, "token": renewed, "expires_at": time_text(now + 7200)}, owner=ALICE, version=2,
                record_id=record)  # fmt: skip
        await operator.refresh_shared(context, pool)
        assert pool.current(name) == (VALID, (0, 0, 1, 0))  # still valid: the copy is used, the hub not asked
        # the copy's token expired: the hub is asked, the session gets the renewed token
        pool.wall = lambda: now + 3600
        picked = await operator.pick(context, ORG)
        assert picked is not None
        assert picked[:3] == (name, renewed, (0, 0, 2, 0))
        # the hub has nothing newer and that token expired as well: no entry, asked again only after FETCH_RETRY_S
        clock = Clock()
        clock.now = pool.clock() + operator.FETCH_RETRY_S  # past the fetch for the first expiry, whatever the uptime
        pool.clock = clock
        pool.wall = lambda: now + 7200
        asked = hub.asked.count(record)
        assert await operator.pick(context, ORG) is None
        assert f"shared_unusable id={record} reason=expired" in context.log.getvalue()
        assert hub.asked.count(record) == asked + 1
        assert await operator.pick(context, ORG) is None
        assert hub.asked.count(record) == asked + 1
        clock.now += operator.FETCH_RETRY_S
        assert await operator.pick(context, ORG) is None
        assert hub.asked.count(record) == asked + 2

    run(store_dsn, body)


def test_a_refused_shared_token_asks_the_hub_for_the_renewed_record(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOT-26: the provider revoked the token with a renewal elsewhere (`401` blocks the entry): the renewed record is
    fetched, so the session's next provisioning carries it."""

    async def body(env: Env, hub: SubscriptionHub) -> None:
        context = env.context
        use = {"pool": False, "orgs": [ORG]}
        full = {**subscription_body(token=VALID, use=use), "kind": "oauth_login", "refresh_token": "r" * 32}
        record = hub.add(full, owner=ALICE)
        listing(monkeypatch, [(record, ALICE)])
        pool = await operator.refreshed(context)
        assert pool is not None
        name = "sub-" + record
        pool.blocked[name] = (0, 0, 1, 0)  # the provider refused the content the pool holds
        hub.add({**full, "token": token(8)}, owner=ALICE, version=2, record_id=record)
        assert await operator.pick(context, ORG) == (name, token(8), (0, 0, 2, 0))

    run(store_dsn, body)
