"""U-RUN-github (KOR-47, ADR-078 sections 1, 5 and 7): where `github.connect` puts the account, company accounts and
signing keys. Real request frames, the hub kit with `GET|PUT /v1/device/github-identities/{id}` (`IdentityHub`), real
PostgreSQL and the GitHub double (classic tokens with and without `write:ssh_signing_key`, SSH signing keys).

- personal: the first account becomes the primary record, the same account updates it, another account adds a new
  record, `identity_id` reconnects a named record (also the primary one), a missing or foreign record is
  `credential_missing`, a primary record that cannot be opened is replaced as before;
- company (`org_id`): only an active admin, only on a device assigned in the organization, nothing stored, the body
  answered (the operator's token with `source:"operator"` for `OPERATOR_GITHUB_ORG`);
- `signing_key`: registered, `scope_missing`, `refused`, a network error, and never for the operator's copy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.credentials import cache
from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.ids import uuid7
from agent_runner.encoding import b64u_encode
from agent_runner.github import device_flow, operator
from agent_runner.github.client import GitHubOptions
from agent_runner.github.signing import new_seed, public_blob, public_line
from agent_runner.requests.service import service_for

from ..credentials import kit
from ..credentials.kit import ALICE, BOB, Env, Json, kit_env, make_record, pid, seal_request
from ..settle import settle as settle_tasks
from .conftest import GitHub, seed
from .test_operator_github import OPERATOR_TOKEN, operator_world, write_token

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")
ORG_A = pid("a/org")
ORG_Z = pid("z/org")
OCTO = "double-octo-classic0sign"  # not secrets: the double only compares them
OCTO_REPO = "double-octo-classic0repo"
OTHER = "double-other-classic0repo"
APP = "double-user-token"


class IdentityHub(kit.CredHub):
    """`CredHub` with the device routes of identity records (03-api.md section 6.1): a user's primary record is the
    legacy `(owner, "github")` record, every other one lives in `identities` by ID."""

    identities: dict[str, Json]
    identity_put_refusals: list[tuple[int, Json]]
    identity_get_refusals: list[tuple[int, Json]]
    identity_lost: list[str]
    identity_puts: int

    def setup(self) -> None:
        super().setup()
        self.identities, self.identity_put_refusals, self.identity_get_refusals = {}, [], []
        self.identity_lost, self.identity_puts = [], 0

    def route(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(target).path.strip("/").split("/")
        if parts[:3] == ["v1", "device", "github-identities"] and len(parts) == 4:
            if method == "GET":
                return self.get_identity(parts[3])
            if method == "PUT":
                return self.put_identity(parts[3], body)
        return super().route(method, target, body)

    def get_identity(self, record_id: str) -> tuple[int, dict[str, Any]]:
        if self.identity_get_refusals:
            return self.identity_get_refusals.pop(0)
        answer: Json | None = None
        for (owner, kind), record in self.records.items():
            if kind == "github" and record["header"]["id"] == record_id:
                answer = {"owner_id": owner, "owner_kind": "user", "primary": True, "record": record}
        found = self.identities.get(record_id)
        if found is not None:
            answer = {**found, "primary": False}
        if answer is None:
            return 404, {"error": "not_found"}
        self.check("api/device-github-identity-get.response.json", answer)
        return 200, answer

    def put_identity(self, record_id: str, body: Any) -> tuple[int, dict[str, Any]]:  # noqa: PLR0911
        self.check("api/device-github-identity-put.request.json", body)
        self.identity_puts += 1
        lost = self.identity_lost.pop(0) if self.identity_lost else None
        if lost == "before":
            raise httpx.ReadError("connection lost")
        if self.identity_put_refusals:
            refusal = self.identity_put_refusals.pop(0)
            if refusal[0] == 0:  # an answer that does not verify: a transport error for the runner
                self.tamper.insert(0, "bad_signature")
                return 200, {}
            return refusal
        record, owner = body["record"], body["owner_id"]
        header = credential_format.verify_credential(record, self.device_public)
        request = self.stored_requests.get(body["request_id"])
        if request is None or request.closed or request.envelope["header"]["user"] != owner:
            return 403, {"error": "forbidden"}
        if (header["id"], header["owner"], header["kind"], header["author"]) != (
            record_id,
            owner,
            "github",
            self.device_id,
        ):
            return 400, {"error": "header_invalid"}
        current = self.identities.get(record_id)
        version = 0 if current is None else int(current["record"]["header"]["version"])
        if current is not None and current["record"] == record:
            return 200, {"version": version}
        if body["expected_version"] != version:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        self.identities[record_id] = {"owner_id": owner, "owner_kind": "user", "record": record}
        if lost == "after":
            raise httpx.ReadError("connection lost")
        return 200, {"version": version + 1}

    def opened(self, record_id: str, owner: str = ALICE) -> Json:
        """The body of an identity record, opened with the owner's `CK` (epoch 1)."""
        record = self.identities[record_id]["record"]
        plain = credential_format.open_credential(kit.ck(owner), record, self.device_public)
        return cast(Json, parse_json(plain))


@pytest.fixture(autouse=True)
def identity_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kit, "CredHub", IdentityHub)


def world(extra_users: list[Json] | None = None) -> dict[str, Any]:
    """The conftest world with classic tokens for both accounts (one may add signing keys)."""
    found = seed()
    octo, other = found["users"]
    octo["tokens"] += [
        {"token": OCTO, "kind": "classic", "scopes": ["repo", "write:ssh_signing_key"]},
        {"token": OCTO_REPO, "kind": "classic", "scopes": ["repo"]},
    ]
    other["tokens"] += [{"token": OTHER, "kind": "classic", "scopes": ["repo"]}]
    found["users"] += extra_users or []
    return found


@pytest.fixture
def gh(start_github: Callable[..., GitHub]) -> GitHub:
    return start_github(world=world())


Body = Callable[[Env], Awaitable[None]]


def run(dsn: str, github: GitHub | None, body: Body, *, ck: bool = True, transport: Any = None) -> None:
    async def main() -> None:
        url = github.url if github is not None else "https://github.mock"
        wall = github.clock if github is not None else (lambda: kit.WALL)
        async with kit_env(dsn, github_url=url, github_wall=wall) as env:
            if transport is not None:
                env.context.github_options = GitHubOptions(wall=wall, transport=transport)
            if ck:
                await env.add_ck()
            await body(env)

    asyncio.run(main())
    if github is not None:
        assert github.failures() == []


async def connect(env: Env, body: Json, *, user: str = "alice") -> list[Json]:
    """One `github.connect` of `user`; the opened responses (each checked against the contract)."""
    service = service_for(env.context)
    assert service is not None
    request_id = env.hub.add_request(seal_request("github.connect", body, user=user))
    await service.process(request_id)
    await settle_tasks(lambda: [f.task for f in service.flows.values()], "the request flows")
    bodies = env.hub.bodies(request_id, user=user)
    for answer in bodies:
        assert SCHEMAS.errors("requests/github.connect.response.json", answer) == []
    return bodies


def hub(env: Env) -> IdentityHub:
    return cast(IdentityHub, env.hub)


def primary(env: Env) -> Json:
    return env.hub.records[(ALICE, "github")]


async def primary_body(env: Env) -> Json:
    async with cache.opened(env.context, ALICE, "github") as body:
        return dict(body)


def done(login: str, identity_id: Any, *, scopes: list[str] | None = None, **extra: Any) -> Json:
    return {
        "step": "done",
        "login": login,
        "scopes": ["repo"] if scopes is None else scopes,
        "source": "token",
        "identity_id": identity_id,
        **extra,
    }


# --- personal accounts --------------------------------------------------------------------------------------------


def test_first_same_and_other_accounts(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        first = await connect(env, {"token": OCTO_REPO})
        record_id = primary(env)["header"]["id"]
        assert first == [done("octo-user", record_id)]
        assert primary(env)["header"]["version"] == 1
        # the same GitHub account (another token of it) updates the primary record
        assert await connect(env, {"token": OCTO}) == [
            done("octo-user", record_id, scopes=["repo", "write:ssh_signing_key"])
        ]
        assert (primary(env)["header"]["id"], primary(env)["header"]["version"]) == (record_id, 2)
        assert (await primary_body(env))["token"] == OCTO
        assert hub(env).identity_puts == 0
        # another account becomes a new record; the primary one stays
        (added,) = await connect(env, {"token": OTHER})
        new_id = added["identity_id"]
        assert added == done("other-user", new_id)
        assert new_id != record_id
        stored = hub(env).identities[new_id]
        assert (stored["owner_id"], stored["record"]["header"]["version"]) == (ALICE, 1)
        assert stored["record"]["header"]["author"] == env.context.identity.device.device_id
        opened = hub(env).opened(new_id)
        assert (opened["login"], opened["user_id"], opened["token"], opened["source"]) == (
            "other-user",
            102,
            OTHER,
            "token",
        )
        assert (await primary_body(env))["login"] == "octo-user"
        cached = await env.rows("SELECT id::text, owner_id::text, version FROM runner.github_identity_cache")
        assert cached == [(new_id, ALICE, 1)]
        # the reconnect of the added record: same ID, version + 1
        assert await connect(env, {"token": OTHER, "identity_id": new_id}) == [done("other-user", new_id)]
        assert hub(env).identities[new_id]["record"]["header"]["version"] == 2
        assert await env.rows("SELECT version FROM runner.github_identity_cache") == [(2,)]
        # naming the primary record writes it through the legacy path
        assert await connect(env, {"token": OCTO_REPO, "identity_id": record_id}) == [done("octo-user", record_id)]
        assert primary(env)["header"]["version"] == 3
        assert f"github_connect stored identity_id={new_id} version=2" in env.log()
        for token in (OCTO, OCTO_REPO, OTHER):
            assert token not in env.log()

    run(store_dsn, gh, body)


def test_a_named_record_that_is_missing_or_not_the_users_is_credential_missing(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        bobs = make_record(kit.github_body(), owner=BOB, author="bob", record_id=uuid7())
        hub(env).identities[bobs["header"]["id"]] = {"owner_id": BOB, "owner_kind": "user", "record": bobs}
        orgs = make_record(kit.github_body(), owner=ORG_A, author="alice", record_id=uuid7(), key=bytes(32))
        hub(env).identities[orgs["header"]["id"]] = {"owner_id": ORG_A, "owner_kind": "org", "record": orgs}
        for named in (uuid7(), bobs["header"]["id"], orgs["header"]["id"]):
            assert await connect(env, {"token": OCTO_REPO, "identity_id": named}) == [
                {"step": "failed", "code": "credential_missing"}
            ]
        assert "github_connect store_failed reason=missing code=None" in env.log()
        # a record under another ID than the one asked for does not count as the user's
        mine = make_record(kit.github_body(), record_id=uuid7())
        hub(env).identities[uuid7()] = {"owner_id": ALICE, "owner_kind": "user", "record": mine}
        wrong = next(key for key, value in hub(env).identities.items() if value["record"] is mine)
        assert await connect(env, {"token": OCTO_REPO, "identity_id": wrong}) == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert "store_failed reason=invalid" in env.log()
        # a refused read of the named record
        hub(env).identity_get_refusals.append((403, {"error": "forbidden"}))
        assert await connect(env, {"token": OCTO_REPO, "identity_id": wrong}) == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert "step=store kind=hub status=403 error=forbidden" in env.log()
        assert hub(env).identity_puts == 0

    run(store_dsn, gh, body)


def test_a_header_that_is_no_record_header_is_invalid(
    store_dsn: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env) -> None:
        mine = make_record(kit.github_body(), record_id=uuid7())
        named = mine["header"]["id"]
        hub(env).identities[named] = {"owner_id": ALICE, "owner_kind": "user", "record": mine}
        original = IdentityHub.get_identity

        def broken(self: IdentityHub, record_id: str) -> tuple[int, dict[str, Any]]:
            status, answer = original(self, record_id)
            return status, {**answer, "record": {"header": {"v": 9}}}

        monkeypatch.setattr(IdentityHub, "get_identity", broken)
        assert await connect(env, {"token": OCTO_REPO, "identity_id": named}) == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert "store_failed reason=invalid" in env.log()

    run(store_dsn, gh, body)


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ({"author": "bob"}, "a primary record this device cannot verify"),
        ({"epoch": 2}, "a primary record under a key this device does not hold"),
        ({"body": {**kit.github_body(), "user_id": "x"}}, "a body without an account ID"),
    ],
)
def test_a_primary_record_that_cannot_be_read_is_replaced(
    store_dsn: str, gh: GitHub, stored: Json, reason: str
) -> None:
    async def body(env: Env) -> None:
        old = make_record(
            stored.get("body", {**kit.github_body(), "user_id": 999}),
            author=stored.get("author", "alice"),
            epoch=stored.get("epoch", 1),
        )
        env.hub.records[(ALICE, "github")] = old
        assert await connect(env, {"token": OTHER}) == [done("other-user", old["header"]["id"])], reason
        assert primary(env)["header"]["version"] == 2
        assert hub(env).identities == {}

    run(store_dsn, gh, body)


def test_identity_writes_retry_conflicts_and_lost_connections(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        await connect(env, {"token": OCTO_REPO})
        hub(env).identity_put_refusals.append((409, {"error": "version_conflict", "detail": {"expected": 0}}))
        hub(env).identity_lost.append("before")
        (answer,) = await connect(env, {"token": OTHER})
        assert answer["step"] == "done"
        assert hub(env).identities[answer["identity_id"]]["record"]["header"]["version"] == 1
        assert hub(env).identity_puts == 3  # lost, conflict (the version is read again), stored
        assert "github_connect store_retry step=write attempt=1 reason=hub_unreachable" in env.log()
        # stored, but the answer was lost: the same bytes again are answered with the stored version
        hub(env).identity_lost.append("after")
        assert await connect(env, {"token": OTHER, "identity_id": answer["identity_id"]}) == [
            done("other-user", answer["identity_id"])
        ]
        assert hub(env).identities[answer["identity_id"]]["record"]["header"]["version"] == 2
        assert hub(env).identity_puts == 5
        # version conflicts on every attempt, another refusal, an answer without a version
        conflict = (409, {"error": "version_conflict", "detail": {"expected": 0}})
        hub(env).identity_put_refusals += [conflict] * 3
        assert await connect(env, {"token": OTHER, "identity_id": answer["identity_id"]}) == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert "store_failed reason=refused code=version_conflict" in env.log()
        hub(env).identity_put_refusals.append((403, {"error": "forbidden"}))
        assert await connect(env, {"token": OTHER, "identity_id": answer["identity_id"]}) == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert "store_failed reason=refused code=forbidden" in env.log()
        hub(env).identity_put_refusals.append((200, {"version": 0}))
        assert await connect(env, {"token": OTHER, "identity_id": answer["identity_id"]}) == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert "store_failed reason=refused code=response_invalid" in env.log()
        # the hub stays unreachable: the connect ends unreachable after the delays
        hub(env).identity_lost += ["before"] * 4
        assert await connect(env, {"token": OTHER, "identity_id": answer["identity_id"]}) == [
            {"step": "failed", "code": "upstream_unreachable"}
        ]
        # an answer that does not verify is not repeated
        puts = hub(env).identity_puts
        hub(env).identity_put_refusals.append((0, {}))
        assert await connect(env, {"token": OTHER, "identity_id": answer["identity_id"]}) == [
            {"step": "failed", "code": "upstream_unreachable"}
        ]
        assert hub(env).identity_puts == puts + 1

    run(store_dsn, gh, body)


def test_a_record_that_cannot_be_read_back_is_kept(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        await connect(env, {"token": OCTO_REPO})
        original = hub(env).get_identity
        calls: list[str] = []

        def refuse_after_write(record_id: str) -> tuple[int, dict[str, Any]]:
            calls.append(record_id)
            if hub(env).identities:
                return 403, {"error": "forbidden"}
            return original(record_id)

        hub(env).get_identity = refuse_after_write  # type: ignore[method-assign]
        (answer,) = await connect(env, {"token": OTHER})
        assert answer["step"] == "done"
        assert f"github_connect cache_failed identity_id={answer['identity_id']} error=HubRefusal" in env.log()
        assert await env.rows("SELECT id FROM runner.github_identity_cache") == []

    run(store_dsn, gh, body)


def test_without_a_key_nothing_is_stored(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        mine = make_record(kit.github_body(), record_id=uuid7())
        named = mine["header"]["id"]
        hub(env).identities[named] = {"owner_id": ALICE, "owner_kind": "user", "record": mine}
        assert await connect(env, {"token": OCTO_REPO, "identity_id": named}) == [
            {"step": "failed", "code": "credential_missing"}
        ]
        assert "store_failed reason=key_missing" in env.log()

    run(store_dsn, gh, body, ck=False)


# --- the request body ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"identity_id": "not-a-uuid"},
        {"identity_id": 5},
        {"org_id": "01A1EE56-5E00-7359-8EC3-2C90DD7AE358"},
        {"signing_key": "yes"},
        {"identity_id": None},
    ],
)
def test_malformed_bodies_end_failed(store_dsn: str, gh: GitHub, body: Json) -> None:
    async def check(env: Env) -> None:
        assert await connect(env, body) == [{"step": "failed", "code": "upstream_error"}]
        assert "step=request kind=body" in env.log()

    run(store_dsn, gh, check)
    assert gh.paths() == []


def test_parse_request() -> None:
    named = uuid7()
    assert device_flow.parse_request({}) == device_flow.Request()
    assert device_flow.parse_request({"identity_id": named, "signing_key": True}) == device_flow.Request(
        identity_id=named, signing_key=True
    )


# --- signing keys ---------------------------------------------------------------------------------------------------


def test_a_signing_key_is_registered_and_stored(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        (answer,) = await connect(env, {"token": OCTO, "signing_key": True})
        assert answer == done(
            "octo-user", primary(env)["header"]["id"], scopes=["repo", "write:ssh_signing_key"], signing="registered"
        )
        stored = await primary_body(env)
        key = stored["signing_key"]
        (registered,) = gh.double.world.signing_keys.values()
        assert key["alg"] == "ssh-ed25519"
        assert key["github_key_id"] == registered.id
        assert registered.login == "octo-user"
        assert registered.title.startswith("agent.dfx.swiss 20")
        assert registered.blob == public_blob(b64u_decode(key["seed"]))
        assert key["public"] == registered.line
        assert SCHEMAS.errors("credentials/github.json", stored) == []
        assert key["seed"] not in env.log()
        assert " scopes=repo,write:ssh_signing_key signing=registered\n" in env.log()

    run(store_dsn, gh, body)


@pytest.mark.parametrize(("token", "status"), [(OCTO_REPO, 404), (APP, 403)])
def test_without_the_scope_the_account_is_stored_without_a_key(
    store_dsn: str, gh: GitHub, token: str, status: int
) -> None:
    async def body(env: Env) -> None:
        (answer,) = await connect(env, {"token": token, "signing_key": True})
        assert (answer["step"], answer["signing"]) == ("done", "scope_missing")
        assert "signing_key" not in await primary_body(env)
        assert "outcome=scope_missing step=signing_key kind=" in env.log()
        assert f"status={status}" in env.log()
        assert gh.double.world.signing_keys == {}

    run(store_dsn, gh, body)


def signing_mock(signing: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo-user", "id": 101}, headers={"x-oauth-scopes": "repo"})
        assert (request.method, request.url.path) == ("POST", "/user/ssh_signing_keys")
        sent = json.loads(request.content)
        assert set(sent) == {"title", "key"}
        return signing(request)

    return httpx.MockTransport(handle)


def test_other_refusals_and_answers_of_the_registration(store_dsn: str) -> None:
    answers: list[httpx.Response] = [
        httpx.Response(422, json={"message": "Validation Failed"}),
        httpx.Response(201, json=["not", "an", "object"]),
        httpx.Response(201, json={"id": True}),
    ]

    async def body(env: Env) -> None:
        (refused,) = await connect(env, {"token": OCTO, "signing_key": True})
        assert refused["signing"] == "refused"
        assert "signing_key" not in await primary_body(env)
        for _ in range(2):
            (registered,) = await connect(env, {"token": OCTO, "signing_key": True})
            assert registered["signing"] == "registered"
            key = (await primary_body(env))["signing_key"]
            assert set(key) == {"alg", "seed", "public"}

    run(store_dsn, None, body, transport=signing_mock(lambda _request: answers.pop(0)))


def test_a_network_error_of_the_registration_ends_the_connect(store_dsn: str) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def body(env: Env) -> None:
        assert await connect(env, {"token": OCTO, "signing_key": True}) == [
            {"step": "failed", "code": "upstream_unreachable"}
        ]
        assert "step=signing_key kind=unreachable" in env.log()
        assert (ALICE, "github") not in env.hub.records

    run(store_dsn, None, body, transport=signing_mock(broken))


# --- company accounts ------------------------------------------------------------------------------------------------


def test_an_admin_gets_the_company_body_and_nothing_is_stored(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        named = uuid7()  # ignored for a company account
        (answer,) = await connect(env, {"token": OCTO, "org_id": ORG_A, "signing_key": True, "identity_id": named})
        company = answer.pop("body")
        assert answer == {
            "step": "done",
            "login": "octo-user",
            "scopes": ["repo", "write:ssh_signing_key"],
            "source": "token",
            "signing": "registered",
        }
        assert (company["token"], company["login"], company["user_id"], company["source"]) == (
            OCTO,
            "octo-user",
            101,
            "token",
        )
        assert company["signing_key"]["github_key_id"] == next(iter(gh.double.world.signing_keys))
        assert SCHEMAS.errors("credentials/github.json", company) == []
        assert env.hub.records == {}
        assert hub(env).identities == {}
        assert await env.rows("SELECT owner_id FROM runner.github_minted") == []  # an existing token
        assert OCTO not in env.log()
        assert f"source=token org={ORG_A} scopes=repo,write:ssh_signing_key signing=registered" in env.log()

    run(store_dsn, gh, body)


def test_a_company_token_of_the_device_flow_is_remembered_for_the_organization(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        def on_sleep(seconds: float) -> None:
            gh.clock.now += seconds
            if len(env.clock.sleeps) == 1:
                gh.decide("authorize", login="octo-user")

        env.clock.on_sleep = on_sleep
        first, last = await connect(env, {"org_id": ORG_A})
        assert first["step"] == "device_code"
        company = last.pop("body")
        assert (last["source"], company["source"], company["login"]) == ("device_flow", "device_flow", "octo-user")
        digest = hashlib.sha256(company["token"].encode()).hexdigest()
        assert await env.rows("SELECT owner_id::text, token_sha256 FROM runner.github_minted") == [(ORG_A, digest)]
        assert env.hub.records == {}

    run(store_dsn, gh, body)


def test_a_member_who_is_no_admin_and_an_organization_without_this_device_are_refused(
    store_dsn: str, gh: GitHub
) -> None:
    async def body(env: Env) -> None:
        assert await connect(env, {"token": OCTO, "org_id": ORG_A}, user="bob") == [
            {"step": "failed", "code": "upstream_error"}
        ]
        assert f"code=upstream_error org={ORG_A} step=request kind=org reason=not_admin" in env.log()
        assert await connect(env, {"token": OCTO, "org_id": ORG_Z}) == [
            {"step": "failed", "code": "credential_missing"}
        ]
        assert "reason=not_assigned" in env.log()

    run(store_dsn, gh, body)
    assert gh.paths() == []


def test_the_operators_organization_gets_the_operators_token(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=operator_world())
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        config = env.context.config
        config.operator_github_token_file = token_file
        config.operator_github_org = ORG_A
        assert operator.for_org(env.context, ORG_A)
        assert not operator.for_org(env.context, ORG_Z)
        (answer,) = await connect(env, {"org_id": ORG_A})
        company = answer.pop("body")
        assert answer == {"step": "done", "login": "op-account", "scopes": ["repo"], "source": "operator"}
        assert (company["token"], company["source"]) == (OPERATOR_TOKEN, "operator")
        assert env.hub.records == {}
        assert await env.rows("SELECT owner_id FROM runner.github_minted") == []
        assert operator.state_for(env.context).connects == 1
        # a broken file fails like the operator's way for users, counted
        token_file.unlink()
        assert await connect(env, {"org_id": ORG_A}) == [{"step": "failed", "code": "credential_missing"}]
        assert f"code=credential_missing source=operator org={ORG_A} step=operator_file" in env.log()
        assert operator.state_for(env.context).failures == 1
        assert OPERATOR_TOKEN not in env.log()

    run(store_dsn, github, body)


def test_the_operators_copy_for_a_user_never_gets_a_key(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=operator_world())
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        env.context.config.operator_github_token_file = token_file
        env.context.config.operator_github_users = frozenset({ALICE})
        (answer,) = await connect(env, {"signing_key": True})
        assert (answer["source"], answer["signing"]) == ("operator", "refused")
        assert (await primary_body(env))["source"] == "operator"
        assert github.paths() == ["GET /user"]

    run(store_dsn, github, body)


# --- the signing key of a reconnected account ---------------------------------------------------------------------

SEED = new_seed()
SIGNING = {"alg": "ssh-ed25519", "seed": b64u_encode(SEED), "public": public_line(SEED, ""), "github_key_id": 7}


def test_a_reconnect_of_the_same_account_keeps_its_signing_key(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        (first,) = await connect(env, {"token": OCTO, "signing_key": True})
        assert first["signing"] == "registered"
        key = (await primary_body(env))["signing_key"]
        (again,) = await connect(env, {"token": OCTO_REPO})
        assert "signing" not in again
        assert (await primary_body(env))["signing_key"] == key
        await connect(env, {"token": OCTO_REPO, "identity_id": first["identity_id"]})
        assert (await primary_body(env))["signing_key"] == key
        assert primary(env)["header"]["version"] == 3
        (renewed,) = await connect(env, {"token": OCTO, "signing_key": True})
        assert renewed["signing"] == "registered"
        replaced = (await primary_body(env))["signing_key"]
        assert replaced["seed"] != key["seed"]
        assert len(gh.double.world.signing_keys) == 2
        (added,) = await connect(env, {"token": OTHER})  # another account takes nothing over
        assert "signing_key" not in hub(env).opened(added["identity_id"])
        assert key["seed"] not in env.log()

    run(store_dsn, gh, body)


@pytest.mark.parametrize(
    ("change", "kept"),
    [
        ({}, True),
        ({"user_id": 999}, False),
        ({"author": "bob"}, False),
        ({"epoch": 2}, False),
        ({"key": bytes(32)}, False),
        ({"plain": ["not", "an", "object"]}, False),
    ],
)
def test_a_named_record_carries_its_key_only_when_it_opens_for_the_same_account(
    store_dsn: str, gh: GitHub, change: Json, kept: bool
) -> None:
    async def body(env: Env) -> None:
        previous = {**kit.github_body(token="old-token", login="other-user"), "user_id": 102, "signing_key": SIGNING}
        previous["user_id"] = change.get("user_id", 102)
        record = make_record(
            change.get("plain", previous),
            record_id=uuid7(),
            author=change.get("author", "alice"),
            epoch=change.get("epoch", 1),
            key=change.get("key"),
        )
        named = record["header"]["id"]
        hub(env).identities[named] = {"owner_id": ALICE, "owner_kind": "user", "record": record}
        assert await connect(env, {"token": OTHER, "identity_id": named}) == [done("other-user", named)]
        stored = hub(env).opened(named)
        assert stored["token"] == OTHER
        assert (stored.get("signing_key") == SIGNING) is kept
        assert ("signing_key" in stored) is kept
        assert SCHEMAS.errors("credentials/github.json", stored) == []

    run(store_dsn, gh, body)
