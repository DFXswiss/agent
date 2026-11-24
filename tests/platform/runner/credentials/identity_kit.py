"""Test kit of the KOR-47 suites (GitHub identities): the hub's `GET /v1/device/github-identities/{id}` installed on
the rig's hub (answers validated against the contract), identity records sealed in the owner's `cred` scope (a user's
or an organization's, signed by any principal of the vector identities), delivered prompts of a task in the prompt
store, and a session stand-in for the resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from psycopg.types.json import Jsonb

from agent_runner.crypto.ids import uuid7
from agent_runner.github import selection

from ..reconcile.rig import World, org_log
from .kit import ALICE, WALL, ck, make_record, ms, pid, world_with_users

Json = dict[str, Any]
ORG_A = pid("a/org")
WG_A = pid("a/wg/default")
ORG_B = pid("b/org")
TASK = "01930004-0000-7000-8000-0000000000a1"
SESSION = "01930005-0000-7000-8000-0000000000a1"


def identity_body(
    login: str = "octo-user",
    user_id: int = 101,
    token: str = "double-user-token",
    **extra: Any,
) -> Json:
    return {"token": token, "login": login, "user_id": user_id, "scopes": [], "created_at": ms(WALL), **extra}


@dataclass
class Served:
    owner_id: str
    owner_kind: str
    primary: bool
    record: Json


@dataclass
class IdentityHub:
    """The device read of identity records (03-api.md section 6.1), installed on a rig hub."""

    hub: Any
    served: dict[str, Served] = field(default_factory=dict)
    refusals: dict[str, tuple[int, Json]] = field(default_factory=dict)
    raw: dict[str, Json] = field(default_factory=dict)
    """Answers sent without the contract check (a hub that breaks the contract)."""
    asked: list[str] = field(default_factory=list)
    list_refusals: dict[str, tuple[int, Json]] = field(default_factory=dict)
    raw_lists: dict[str, Json] = field(default_factory=dict)
    """List answers sent without the contract check, per owner."""
    listed: list[str] = field(default_factory=list)
    """Owners whose list was asked for."""

    def install(self) -> IdentityHub:
        original = self.hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            split = urlsplit(target)
            parts = split.path.strip("/").split("/")
            if method == "GET" and parts[:3] == ["v1", "device", "github-identities"] and len(parts) == 4:
                return self.answer(parts[3])
            if method == "GET" and parts == ["v1", "device", "github-identities"]:
                return self.listing(parse_qs(split.query))
            answer: tuple[int, dict[str, Any]] = original(method, target, body)
            return answer

        self.hub.route = route
        return self

    def answer(self, identity_id: str) -> tuple[int, Json]:
        self.asked.append(identity_id)
        if identity_id in self.refusals:
            return self.refusals[identity_id]
        if identity_id in self.raw:
            return 200, self.raw[identity_id]
        found = self.served.get(identity_id)
        if found is None:
            return 404, {"error": "not_found"}
        answer = {"owner_id": found.owner_id, "owner_kind": found.owner_kind, "primary": found.primary}
        answer["record"] = found.record
        self.hub.check("api/device-github-identity-get.response.json", answer)
        return 200, answer

    def listing(self, query: dict[str, list[str]]) -> tuple[int, Json]:
        self.hub.check("api/device-github-identities-list.request.json", {k: v[0] for k, v in query.items()})
        owner = query["owner_id"][0]
        self.listed.append(owner)
        if owner in self.list_refusals:
            return self.list_refusals[owner]
        if owner in self.raw_lists:
            return 200, self.raw_lists[owner]
        items = [
            {
                "id": record_id,
                "owner_id": found.owner_id,
                "owner_kind": found.owner_kind,
                "primary": found.primary,
                "version": found.record["header"]["version"],
                "epoch": found.record["header"]["epoch"],
                "created_at": ms(WALL),
                "updated_at": ms(WALL),
            }
            for record_id, found in sorted(self.served.items())
            if found.owner_id == owner
        ]
        answer = {"identities": items}
        self.hub.check("api/device-github-identities-list.response.json", answer)
        return 200, answer

    def add(
        self,
        body: Json,
        *,
        owner: str,
        kind: str = "user",
        author: str = "alice",
        primary: bool = False,
        version: int = 1,
        epoch: int = 1,
        identity_id: str | None = None,
        ts: float = WALL,
    ) -> str:
        """Serve a new record (written at `ts`); returns its ID."""
        record_id = identity_id or uuid7()
        record = make_record(
            body,
            owner=owner,
            version=version,
            epoch=epoch,
            author=author,
            record_id=record_id,
            key=ck(owner, epoch),
            ts=ts,
        )
        self.served[record_id] = Served(owner, kind, primary, record)
        return record_id


def two_orgs() -> World:
    """`world_with_users` plus org `b` (founder alice, the device on its default workgroup)."""
    world = world_with_users()
    spec = world.add_org(org_log("b"))
    assert world.listed is not None
    world.listed.append(spec.org)
    return world


def session_of(
    owner: str | None = ALICE,
    *,
    task_id: str = TASK,
    session_id: str = SESSION,
    org_id: str = ORG_A,
    wg_id: str = WG_A,
    depth: int = 0,
    parent_id: str | None = None,
) -> SimpleNamespace:
    """What the resolution reads of a local session (the owner only for the older suites' helpers)."""
    return SimpleNamespace(
        id=session_id,
        task_id=task_id,
        org_id=org_id,
        wg_id=wg_id,
        owner_id=owner,
        depth=depth,
        parent_id=parent_id,
    )


def operation_of(actor: str | None = ALICE, op_type: str = "github.comment") -> Any:
    """An operation with its stamped acting user (HOT-14)."""
    return SimpleNamespace(id=uuid7(), type=op_type, actor_id=actor, params={})


async def deliver(
    pool: Any,
    author: str,
    seq: int,
    *,
    task_id: str = TASK,
    session_id: str = SESSION,
    state: str = "accepted",
    sealed: Json | None = None,
) -> str:
    """A verified prompt event of the task (its verified author, `author_id`) and its delivery row."""
    event_id = uuid7()
    stored = sealed if sealed is not None else {"header": {"author": author, "task": task_id}}
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.prompt_event (event_id, org_id, wg_id, task_id, scope_seq, author_sig, sealed,"
            " author_id) VALUES (%s, %s, %s, %s, %s, 'sig', %s, %s)",
            (event_id, ORG_A, WG_A, task_id, seq, Jsonb(stored), author),
        )
        await conn.execute(
            "INSERT INTO runner.prompt_delivery (event_id, session_id, state, task_id, scope_seq)"
            " VALUES (%s, %s, %s, %s, %s)",
            (event_id, session_id, state, task_id, seq),
        )
    return event_id


async def choose(
    pool: Any,
    *,
    session_choice: str | None = None,
    task_choice: str | None = None,
    defaults: list[tuple[str, str, str]] | None = None,
    session_id: str = SESSION,
    task_id: str = TASK,
) -> None:
    """Store a desired state's choices and defaults as the reconcile step does."""
    answer = {
        "sessions": [
            {
                "session_id": session_id,
                "task_id": task_id,
                "github_identity": session_choice,
                "task_github_identity": task_choice,
            }
        ],
        "github_defaults": [
            {"scope": scope, "scope_id": scope_id, "identity_id": identity}
            for scope, scope_id, identity in defaults or []
        ],
    }
    async with pool.connection() as conn, conn.transaction():
        await selection.store(conn, answer)
