"""GitHub identities in the hub fake as `agent_hub.github.routes`, `identities` and `credentials.records` run them
(ADR-078, 03-api.md section 4.8):

- the primary record of a user is the `github` row of `HubFake.credentials` (what `/v1/me/credentials/github` serves);
  further own records and company records live in `records` by ID; `org_epochs` holds each organization's `cred`
  epoch;
- `GET /v1/me/github-identities`: the own records and the company records of the caller's orgs (newest change
  first), the defaults of those orgs and of their workgroups;
- `GET /v1/github-identities/{id}`: a visible record (own, or a company record of an org the caller is a member of),
  else `404`;
- `PUT /v1/github-identities/{id}`: an own record signed by the caller under the current `CK` epoch (`409
  epoch_stale` otherwise), or a company record by an admin (`403 forbidden` otherwise) with `key {epoch, wraps}`
  (`400 request_invalid` without), the epoch the org's current one + 1 (`409 version_conflict`, `detail.expected`),
  every wrap passing W3 for the org's `cred` scope and covering every reader device; the version must be the stored
  one (`409 version_conflict`, `detail.expected`); a first own record becomes primary;
- `DELETE /v1/github-identities/{id}` (company records by admins only), `PUT …/primary` (own records only);
- `PUT /v1/github-defaults/{scope}/{scope_id}` (org: admins; wg: admins and writers; a company record of that org
  only, `403 identity_forbidden`) and `PUT /v1/tasks/{task}/github-identity` (own or company records of an org of
  the caller); a non-null choice is `409 peer_outdated` with `detail.devices` while `outdated` names devices;
- `POST /v1/tasks` stores `github_identity` under the same rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto.credential import verify_credential
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.wraps import CRED, check_cred_wrap, cred_readers

from ..hubfake import HubFake, Json, Request, User, now_ms_text

USER, ORG = "user", "org"


@dataclass
class Identities:
    records: dict[str, Json] = field(default_factory=dict)
    defaults: dict[tuple[str, str], Json] = field(default_factory=dict)
    org_epochs: dict[str, int] = field(default_factory=dict)
    outdated: list[str] = field(default_factory=list)
    puts: list[Json] = field(default_factory=list)


def state_of(hub: HubFake) -> Identities:
    found = getattr(hub, "kor47_identities", None)
    if found is None:
        found = Identities()
        setattr(hub, "kor47_identities", found)  # noqa: B010
    return cast(Identities, found)


def primary_row(hub: HubFake, user: str) -> Json | None:
    """The user's primary record as an identity row, from `HubFake.credentials`."""
    stored = hub.credentials.get((user, "github"))
    if stored is None:
        return None
    header = stored["record"]["header"]
    return {
        "id": header["id"],
        "owner_id": user,
        "owner_kind": USER,
        "primary": True,
        "version": stored["version"],
        "epoch": header["epoch"],
        "record": stored["record"],
        "created_at": "2026-11-24T00:00:00.000Z",
        "updated_at": "2026-11-24T00:00:00.000Z",
    }


def add_record(hub: HubFake, owner: str, kind: str, record: Json, *, epoch: int | None = None) -> str:
    """Seed a further own record (`kind` user) or a company record (`kind` org) as stored."""
    header = record["header"]
    state_of(hub).records[header["id"]] = {
        "id": header["id"],
        "owner_id": owner,
        "owner_kind": kind,
        "primary": False,
        "version": header["version"],
        "epoch": header["epoch"] if epoch is None else epoch,
        "record": record,
        "created_at": now_ms_text(),
        "updated_at": now_ms_text(),
    }
    return str(header["id"])


def meta(row: Json) -> Json:
    keys = ("id", "owner_id", "owner_kind", "primary", "version", "epoch", "created_at", "updated_at")
    return {key: row[key] for key in keys}


def install(hub: HubFake) -> None:  # noqa: PLR0915
    found = state_of(hub)

    def member_orgs(user: str) -> list[str]:
        return [org.id for org in hub.orgs.values() if org.log.state.member(user) is not None]

    def is_admin(org: str, user: str) -> bool:
        return org in hub.orgs and hub.orgs[org].log.state.is_admin(user)

    def rows(user: str) -> list[Json]:
        orgs = member_orgs(user)
        primary = primary_row(hub, user)
        listed = [] if primary is None else [primary]
        for row in found.records.values():
            if (row["owner_kind"] == USER and row["owner_id"] == user) or (
                row["owner_kind"] == ORG and row["owner_id"] in orgs
            ):
                listed.append(row)
        return listed

    def visible(user: str, identity: str) -> Json | None:
        return next((row for row in rows(user) if row["id"] == identity), None)

    def usable(user: str, identity: str) -> bool:
        return visible(user, identity) is not None

    def gate() -> tuple[int, Any] | None:
        if found.outdated:
            return 409, {"error": "peer_outdated", "detail": {"devices": found.outdated}}
        return None

    def listing(request: Request, body: Any) -> tuple[int, Any]:
        user = cast(str, request.user)
        orgs = member_orgs(user)
        wgs = {wg for org in orgs for wg in hub.orgs[org].log.state.workgroups}
        defaults = [
            {"scope": scope, "scope_id": scope_id, **value}
            for (scope, scope_id), value in sorted(found.defaults.items())
            if (scope == ORG and scope_id in orgs) or (scope == "wg" and scope_id in wgs)
        ]
        listed = sorted(rows(user), key=lambda row: (row["updated_at"], row["id"]), reverse=True)
        return 200, {"identities": [meta(row) for row in listed], "defaults": defaults}

    def get(request: Request, body: Any, identity: str) -> tuple[int, Any]:
        row = visible(cast(str, request.user), identity)
        if row is None:
            return 404, {"error": "not_found"}
        keys = ("owner_id", "owner_kind", "primary", "record")
        return 200, {key: row[key] for key in keys}

    def current(identity: str, user: str) -> tuple[int, Json | None]:
        primary = primary_row(hub, user)
        if primary is not None and primary["id"] == identity:
            return primary["version"], primary
        row = found.records.get(identity)
        return (0, None) if row is None else (row["version"], row)

    def put_own(user: User, identity: str, body: Json) -> tuple[int, Any]:
        record = body["record"]
        try:
            header = verify_credential(record, user.identity.sig_public())
        except CryptoError:
            return 400, {"error": "signature_invalid"}
        if (header["owner"], header["author"], header["kind"], header["id"]) != (user.id, user.id, "github", identity):
            return 400, {"error": "header_invalid"}
        if header["epoch"] != user.cred_epoch:
            return 409, {"error": "epoch_stale"}
        version, row = current(identity, user.id)
        if body["expected_version"] != version:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        if header["version"] != version + 1:
            return 400, {"error": "request_invalid"}
        becomes_primary = row["primary"] if row is not None else primary_row(hub, user.id) is None
        if becomes_primary:
            hub.credentials[(user.id, "github")] = {"record": record, "version": header["version"]}
        else:
            add_record(hub, user.id, USER, record)
        return 200, {"version": header["version"]}

    def put_company(user: User, identity: str, org: str, body: Json) -> tuple[int, Any]:  # noqa: PLR0911
        if not is_admin(org, user.id):
            return 403, {"error": "forbidden"}
        key = body.get("key")
        if not isinstance(key, dict):
            return 400, {"error": "request_invalid"}
        record = body["record"]
        header = verify_credential(record, user.identity.sig_public())
        if (header["owner"], header["author"], header["kind"], header["id"]) != (org, user.id, "github", identity):
            return 400, {"error": "header_invalid"}
        if header["epoch"] != key["epoch"]:
            return 409, {"error": "epoch_stale"}
        row = found.records.get(identity)
        version = 0 if row is None else row["version"]
        if body["expected_version"] != version:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        epoch = found.org_epochs.get(org, 0)
        if key["epoch"] != epoch + 1:
            return 409, {"error": "version_conflict", "detail": {"expected": epoch + 1}}
        log = hub.orgs[org].log
        skip = hub.platform_state().excluded(log.state)
        for wrap in key["wraps"]:
            check_cred_wrap(wrap, user.id, [log], key["epoch"], skip, require_active=True)
            if (wrap["scope"], wrap["scope_id"]) != (CRED, org):
                return 400, {"error": "request_invalid"}
        if not cred_readers([log.state], org, skip) <= {wrap["recipient_id"] for wrap in key["wraps"]}:
            return 400, {"error": "request_invalid"}
        found.org_epochs[org] = key["epoch"]
        for wrap in key["wraps"]:
            hub.wraps.append({**wrap, "wrapped_by": user.id, "created_at": now_ms_text()})
        add_record(hub, org, ORG, record)
        return 200, {"version": header["version"]}

    def put(request: Request, body: Json, identity: str) -> tuple[int, Any]:
        user = hub.users[cast(str, request.user)]
        found.puts.append(body)
        if body["owner_id"] == user.id:
            return put_own(user, identity, body)
        return put_company(user, identity, body["owner_id"], body)

    def delete(request: Request, body: Any, identity: str) -> tuple[int, Any]:
        user = cast(str, request.user)
        row = visible(user, identity)
        if row is None:
            return 404, {"error": "not_found"}
        if row["owner_kind"] == ORG and not is_admin(row["owner_id"], user):
            return 403, {"error": "forbidden"}
        if row["primary"]:
            hub.credentials.pop((user, "github"))
        else:
            found.records.pop(identity)
        for place, value in list(found.defaults.items()):
            if value["identity_id"] == identity:
                found.defaults.pop(place)
        return 200, {}

    def primary(request: Request, body: Json, identity: str) -> tuple[int, Any]:
        user = cast(str, request.user)
        row = visible(user, identity)
        if row is None:
            return 404, {"error": "not_found"}
        if row["owner_kind"] != USER:
            return 403, {"error": "forbidden"}
        if row["primary"]:
            return 200, {}
        before = primary_row(hub, user)
        found.records.pop(identity)
        hub.credentials[(user, "github")] = {"record": row["record"], "version": row["version"]}
        if before is not None:
            add_record(hub, user, USER, before["record"])
        return 200, {}

    def set_default(request: Request, body: Json, scope: str, scope_id: str) -> tuple[int, Any]:  # noqa: PLR0911
        user = cast(str, request.user)
        if scope not in (ORG, "wg"):
            return 404, {"error": "not_found"}
        org = scope_id if scope == ORG else hub.org_of_wg(scope_id)
        if org is None or org not in hub.orgs:
            return 404, {"error": "not_found"}
        state = hub.orgs[org].log.state
        writer = scope == "wg" and state.wg_access(user, scope_id) == "write"
        if not (is_admin(org, user) or writer):
            return 403, {"error": "forbidden"}
        identity = body["identity_id"]
        if identity is None:
            found.defaults.pop((scope, scope_id), None)
            return 200, {}
        row = found.records.get(identity)
        if row is None or (row["owner_kind"], row["owner_id"]) != (ORG, org):
            return 403, {"error": "identity_forbidden"}
        outdated = gate()
        if outdated is not None:
            return outdated
        found.defaults[(scope, scope_id)] = {"identity_id": identity, "set_by": user, "set_at": now_ms_text()}
        return 200, {}

    def checked_choice(user: str, identity: object) -> tuple[int, Any] | None:
        if identity is None:
            return None
        if not usable(user, cast(str, identity)):
            return 403, {"error": "identity_forbidden"}
        return gate()

    def task_identity(request: Request, body: Json, task: str) -> tuple[int, Any]:
        stored = hub.tasks.get(task)
        if stored is None:
            return 404, {"error": "not_found"}
        refused = checked_choice(cast(str, request.user), body["identity_id"])
        if refused is not None:
            return refused
        stored["task"]["github_identity"] = body["identity_id"]
        return 200, {}

    original_create = hub.r_task_create

    def task_create(request: Request, body: Json) -> tuple[int, Any]:
        choice = body.get("github_identity")
        refused = checked_choice(cast(str, request.user), choice)
        if refused is not None:
            return refused
        status, answer = original_create(
            request, {key: value for key, value in body.items() if key != "github_identity"}
        )
        if status == 200:
            hub.tasks[body["task_id"]]["task"]["github_identity"] = choice
        return status, answer

    hub.override("r_task_create", task_create)
    identity_path = r"/v1/github-identities/(?P<identity>[0-9a-f-]+)"
    hub.add_route("GET", r"/v1/me/github-identities", listing)
    hub.add_route("GET", identity_path, get)
    hub.add_route("PUT", identity_path, put)
    hub.add_route("DELETE", identity_path, delete)
    hub.add_route("PUT", identity_path + "/primary", primary)
    hub.add_route("PUT", r"/v1/github-defaults/(?P<scope>[a-z]+)/(?P<scope_id>[0-9a-f-]+)", set_default)
    hub.add_route("PUT", r"/v1/tasks/(?P<task>[0-9a-f-]+)/github-identity", task_identity)
