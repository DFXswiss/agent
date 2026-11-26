"""Account and org views of the hub fake as the hub runs them (03-api.md sections 4.1-4.3, 4.9, 4.11; 11-agent-access.md
section 4):

- `GET /v1/notifications?before&limit&account` (`account` must be `1`; no other parameter, `400 request_invalid`)
  newest first, account rows only with `account=1`; `POST /v1/notifications/read` with `{ids}` (1-200 UUIDv7) or
  `{all: true[, account]}`, unknown and foreign IDs ignored;
- `GET /v1/me/quotas` with `unreadable` (`{since}` while the subscription refuses the read, else null; KOR-40);
- `GET /v1/monitoring/overview?org` (member, else `404`; missing `org` `400`; with the work VMs of KOR-29),
  `GET /v1/monitoring/series` (every parameter required, `step` at least 15);
- `GET /v1/orgs/{org}/audit` (member: own rows and rows of readable workgroups; admin: all) with the filters (also
  `on_behalf_of`, KOR-52) and the page's `actors` (`agent_actors`: session -> `audit_agent`, for the actors
  `main_agent`/`subagent`), `GET /v1/orgs/{org}/audit/verify` (admins, else `404`), `GET /v1/me/audit`;
- `GET /v1/me` with `passkeys`, `platform_roles` and `unread_notifications`; `DELETE /v1/passkeys/{id}` (`404`
  unknown, `403 last_passkey`);
- `GET|PUT /v1/me/bundles/recovery` (`new_recovery_id` required; an agent needs no assertion; a taken ID is
  `409 id_conflict`);
- `GET /v1/me/agents`, `DELETE /v1/me/agents/{id}` (`404` unknown or foreign), `DELETE /v1/me/agents`;
- `POST|GET /v1/platform/invites`, `POST /v1/platform/invites/{id}/revoke` (platform role `inviter` from the platform
  log, else `403 forbidden`; expiry in the future and at most 30 days, `pk_inv` an Ed25519 key);
- `GET /v1/orgs/{org}/catch-up` (`missing_wraps` of the copy for the caller and its own `cred` slots), and
  `POST /v1/wraps` with `org`/`wg` wraps (W3 against the org's log) besides `cred` wraps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_runner.crypto.encoding import b64u_decode, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.catchup import missing_cred_wraps, missing_wraps
from agent_runner.crypto.log.entries import entry_hash
from agent_runner.crypto.log.state import WG
from agent_runner.crypto.log.wraps import WrapSlot, check_cred_wrap, check_wrap

from ..hubfake import HubFake, Json, Request, now_ms_text

B64U_ID = re.compile(r"[A-Za-z0-9_-]{21}[AQgw]")


@dataclass
class Views:
    notifications: list[Json] = field(default_factory=list)
    quotas: dict[str, list[Any]] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    overview: dict[str, Json] = field(default_factory=dict)
    points: list[Any] = field(default_factory=list)
    audit: list[Json] = field(default_factory=list)
    agent_actors: dict[str, Json] = field(default_factory=dict)
    """Session ID -> `{task_id, number, agent_path, parent_session_id}` (KOR-52): the org audit's `actors`."""
    passkeys: dict[str, list[Json]] = field(default_factory=dict)
    bundles: dict[str, Json] = field(default_factory=dict)
    platform_invites: dict[str, Json] = field(default_factory=dict)
    series_queries: list[dict[str, str]] = field(default_factory=list)


def views(hub: HubFake) -> Views:
    found = getattr(hub, "stage_b_views", None)
    if found is None:
        found = Views()
        setattr(hub, "stage_b_views", found)  # noqa: B010
    return cast(Views, found)


def add_notification(hub: HubFake, user: str, *, org: str | None = None, kind: str = "task.done") -> str:
    notification = {
        "notification_id": uuid7(),
        "kind": kind,
        "org_id": org,
        "wg_id": None,
        "task_id": None,
        "ref": None,
        "created_at": now_ms_text(),
        "read_at": None,
        "user": user,
    }
    views(hub).notifications.append(notification)
    return cast(str, notification["notification_id"])


def grant_inviter(hub: HubFake, user: str, role: str = "inviter") -> None:
    """A `role_grant` of a platform role (`inviter`, or `operator`, ADR-079) in the platform log."""
    platform_entry(hub, type="role_grant", subject=user, role=role)


def platform_entry(hub: HubFake, **fields: object) -> None:
    """A root-signed platform-log entry with the given type fields (`platform_policy`,
    `org_operator_subscriptions`, ...)."""
    head = hub.platform_entries[-1]
    hub._platform_append(
        {
            "seq": len(hub.platform_entries),
            "prev": entry_hash(head),
            **fields,
            "by": "platform-root",
            "ts": now_ms_text(),
            "v": 1,
        }
    )


def _public(row: Json) -> Json:
    return {key: value for key, value in row.items() if key != "user"}


def install(hub: HubFake) -> None:  # noqa: PLR0915
    found = views(hub)
    original_me = hub.r_me

    def me(request: Request, body: Any) -> tuple[int, Any]:
        status, answer = original_me(request, body)
        user = cast(str, request.user)
        unread = [n for n in found.notifications if n["user"] == user and n["read_at"] is None and n["org_id"]]
        roles = ["inviter"] if hub.platform_state().has_role(user, "inviter") else []
        return status, {
            **answer,
            "passkeys": found.passkeys.get(user, []),
            "platform_roles": roles,
            "unread_notifications": len(unread),
        }

    def notifications(request: Request, body: Any) -> tuple[int, Any]:
        query = request.query
        if set(query) - {"before", "limit", "account"} or query.get("account", "1") != "1":
            return 400, {"error": "request_invalid"}
        rows = [
            _public(n)
            for n in reversed(found.notifications)
            if n["user"] == request.user and (n["org_id"] is not None or "account" in query)
        ]
        start = int(query.get("before", "0"))
        limit = int(query.get("limit", "50"))
        more = start + limit < len(rows)
        return 200, {"notifications": rows[start : start + limit], "next_before": str(start + limit) if more else None}

    def read(request: Request, body: Any) -> tuple[int, Any]:
        mine = [n for n in found.notifications if n["user"] == request.user and n["read_at"] is None]
        if isinstance(body, dict) and set(body) == {"ids"} and isinstance(body["ids"], list):
            if not 1 <= len(body["ids"]) <= 200 or not all(is_uuid7(item) for item in body["ids"]):
                return 400, {"error": "request_invalid"}
            chosen = [n for n in mine if n["notification_id"] in body["ids"]]
        elif isinstance(body, dict) and body.get("all") is True and set(body) <= {"all", "account"}:
            chosen = [n for n in mine if n["org_id"] is not None or body.get("account") is True]
        else:
            return 400, {"error": "request_invalid"}
        for row in chosen:
            row["read_at"] = now_ms_text()
        return 200, {}

    def quotas(request: Request, body: Any) -> tuple[int, Any]:
        since = found.unreadable.get(cast(str, request.user))
        return 200, {
            "quotas": found.quotas.get(cast(str, request.user), []),
            "unreadable": None if since is None else {"since": since},
        }

    def member_org(request: Request, org: str) -> bool:
        stored = hub.orgs.get(org)
        return stored is not None and stored.log.state.member(cast(str, request.user)) is not None

    def overview(request: Request, body: Any) -> tuple[int, Any]:
        org = request.query.get("org")
        if org is None:
            return 400, {"error": "request_invalid"}
        if not member_org(request, org):
            return 404, {"error": "not_found"}
        return 200, found.overview.get(org, {"devices": [], "vms": [], "sessions": [], "workvms": []})

    def series(request: Request, body: Any) -> tuple[int, Any]:
        query = request.query
        if set(query) != {"entity_kind", "entity_id", "metric", "from", "to", "step"} or int(query["step"]) < 15:
            return 400, {"error": "request_invalid"}
        found.series_queries.append(dict(query))
        return 200, {"points": found.points}

    def audit_rows(request: Request, org: str) -> list[Json]:
        state = hub.orgs[org].log.state
        user = cast(str, request.user)
        rows = [row for row in reversed(found.audit) if row["org_id"] == org]
        if state.is_admin(user):
            return rows
        return [r for r in rows if r["actor_id"] == user or (r["wg_id"] and state.can_read(user, WG, r["wg_id"]))]

    def audit(request: Request, body: Any, org: str) -> tuple[int, Any]:
        if not member_org(request, org):
            return 404, {"error": "not_found"}
        query = request.query
        if set(query) - {"before", "limit", "action", "actor", "task", "session", "wg", "on_behalf_of"}:
            return 400, {"error": "request_invalid"}
        rows = audit_rows(request, org)
        if "action" in query:
            rows = [r for r in rows if r["action"] == query["action"] or r["action"].startswith(query["action"] + ".")]
        for name in ("actor", "task", "session", "wg"):
            if name in query:
                rows = [r for r in rows if r[f"{name}_id"] == query[name]]
        if "on_behalf_of" in query:
            rows = [r for r in rows if r.get("on_behalf_of") == query["on_behalf_of"]]
        actors = {
            r["actor_id"]: found.agent_actors[r["actor_id"]]
            for r in rows
            if r["actor_kind"] in ("main_agent", "subagent") and r["actor_id"] in found.agent_actors
        }
        return 200, {"rows": rows, "next_before": None, "actors": actors}

    def verify(request: Request, body: Any, org: str) -> tuple[int, Any]:
        if not member_org(request, org) or not hub.orgs[org].log.state.is_admin(cast(str, request.user)):
            return 404, {"error": "not_found"}
        return 200, {"ok": True, "head": {"seq": len(found.audit), "hash": "0" * 64}, "checkpoint": None}

    def account_audit(request: Request, body: Any) -> tuple[int, Any]:
        agents = {row.id for row in hub.agents.values() if row.user == request.user}
        rows = [
            row
            for row in reversed(found.audit)
            if (row["actor_id"] == request.user and row["org_id"] is None) or row.get("agent_id") in agents
        ]
        return 200, {"rows": rows, "next_before": None}

    def remove_passkey(request: Request, body: Any, credential: str) -> tuple[int, Any]:
        mine = found.passkeys.get(cast(str, request.user), [])
        if not any(item["credential_id"] == credential for item in mine):
            return 404, {"error": "not_found"}
        if len(mine) == 1:
            return 403, {"error": "last_passkey"}
        mine[:] = [item for item in mine if item["credential_id"] != credential]
        return 200, {}

    def get_bundle(request: Request, body: Any) -> tuple[int, Any]:
        stored = found.bundles.get(cast(str, request.user))
        return (404, {"error": "not_found"}) if stored is None else (200, {"bundle_ct": stored["bundle_ct"]})

    def put_bundle(request: Request, body: Any) -> tuple[int, Any]:
        if not isinstance(body, dict) or set(body) - {"bundle_ct", "new_recovery_id", "assertion"}:
            return 400, {"error": "request_invalid"}
        if not isinstance(body.get("new_recovery_id"), str) or not B64U_ID.fullmatch(body["new_recovery_id"]):
            return 400, {"error": "request_invalid"}
        if request.agent is None and "assertion" not in body:
            return 403, {"error": "forbidden"}
        taken = [u for u, row in found.bundles.items() if row["recovery_id"] == body["new_recovery_id"]]
        if taken and taken != [request.user]:
            return 409, {"error": "id_conflict"}
        found.bundles[cast(str, request.user)] = {
            "bundle_ct": body["bundle_ct"],
            "recovery_id": body["new_recovery_id"],
        }
        return 200, {}

    def agents(request: Request, body: Any) -> tuple[int, Any]:
        rows = [
            {
                "agent_id": row.id,
                "state": row.state,
                "label_ct": row.label_ct,
                "created_at": now_ms_text(),
                "confirmed_at": now_ms_text(),
                "claimed_at": None,
                "last_used_at": None,
                "revoked_at": None,
                "replaces": None,
            }
            for row in hub.agents.values()
            if row.user == request.user and row.label_ct is not None
        ]
        return 200, {"agents": rows}

    def revoke_agent(request: Request, body: Any, agent: str) -> tuple[int, Any]:
        row = hub.agents.get(agent)
        if row is None or row.user != request.user:
            return 404, {"error": "not_found"}
        row.state = "revoked"
        return 200, {}

    def revoke_agents(request: Request, body: Any) -> tuple[int, Any]:
        rows = [row for row in hub.agents.values() if row.user == request.user and row.state != "revoked"]
        for row in rows:
            row.state = "revoked"
        return 200, {"revoked": len(rows)}

    def inviter(request: Request) -> bool:
        state = hub.platform_state()
        return state.has_role(cast(str, request.user), "inviter") or state.policy("open_platform_invites")

    def create_invite(request: Request, body: Any) -> tuple[int, Any]:
        if not inviter(request):
            return 403, {"error": "forbidden"}
        if not isinstance(body, dict) or set(body) != {"invite_id", "pk_inv", "kind", "expires_at"}:
            return 400, {"error": "request_invalid"}
        try:
            expires = parse_time(body["expires_at"])
            b64u_decode(body["pk_inv"], 32)
        except CryptoError:
            return 400, {"error": "request_invalid"}
        now = datetime.now(UTC)
        if body["kind"] != "user" or not now < expires <= now + timedelta(days=30):
            return 400, {"error": "request_invalid"}
        known = found.platform_invites.get(body["invite_id"])
        if known is not None:
            return (200, {}) if known["body"] == body else (409, {"error": "id_conflict"})
        row = {
            "invite_id": body["invite_id"],
            "kind": "user",
            "state": "open",
            "created_by": request.user,
            "created_at": now_ms_text(),
            "expires_at": body["expires_at"],
            "registered": False,
            "used_at": None,
            "revoked_at": None,
        }
        found.platform_invites[body["invite_id"]] = {"row": row, "body": body}
        return 200, {}

    def list_invites(request: Request, body: Any) -> tuple[int, Any]:
        if not inviter(request):
            return 403, {"error": "forbidden"}
        return 200, {"invites": [item["row"] for item in reversed(found.platform_invites.values())]}

    def revoke_invite(request: Request, body: Any, invite: str) -> tuple[int, Any]:
        if not inviter(request):
            return 403, {"error": "forbidden"}
        stored = found.platform_invites.get(invite)
        if stored is None:
            return 404, {"error": "not_found"}
        if stored["row"]["state"] == "used":
            return 400, {"error": "invite_consumed"}
        stored["row"] |= {"state": "revoked", "revoked_at": stored["row"]["revoked_at"] or now_ms_text()}
        return 200, {}

    def catch_up(request: Request, body: Any, org: str) -> tuple[int, Any]:
        if not member_org(request, org):
            return 404, {"error": "not_found"}
        user = cast(str, request.user)
        log = hub.orgs[org].log
        have = [WrapSlot(w["scope"], w["scope_id"], w["epoch"], w["recipient_id"]) for w in hub.wraps]
        skip = hub.platform_state().excluded(log.state)
        slots = missing_wraps(log, user, have, skip)
        logs = [o.log for o in hub.orgs.values() if o.log.state.member(user) is not None]
        epoch = hub.users[user].cred_epoch
        slots += missing_cred_wraps(logs, user, user, current_epoch=epoch, existing=have, excluded=skip)
        return 200, {"missing": [slot.to_json() for slot in slots]}

    def wraps_post(request: Request, body: Any) -> tuple[int, Any]:
        user = cast(str, request.user)
        logs = [o.log for o in hub.orgs.values() if o.log.state.member(user) is not None]
        platform = hub.platform_state()
        for wrap in body["wraps"]:
            if wrap["scope"] == "cred":
                check_cred_wrap(wrap, user, logs, hub.users[user].cred_epoch, require_active=True)
                continue
            org = wrap["scope_id"] if wrap["scope"] == "org" else hub.org_of_wg(wrap["scope_id"])
            log = hub.orgs[cast(str, org)].log
            check_wrap(wrap, user, log, platform.excluded(log.state), require_active=True)
        for wrap in body["wraps"]:
            hub.wraps.append({**wrap, "wrapped_by": user, "created_at": now_ms_text()})
        return 200, {"stored": len(body["wraps"]), "ignored": 0}

    hub.override("r_me", me)
    hub.override("r_wraps_post", wraps_post)
    hub.add_route("GET", r"/v1/notifications", notifications)
    hub.add_route("POST", r"/v1/notifications/read", read)
    hub.add_route("GET", r"/v1/me/quotas", quotas)
    hub.add_route("GET", r"/v1/monitoring/overview", overview)
    hub.add_route("GET", r"/v1/monitoring/series", series)
    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/audit", audit)
    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/audit/verify", verify)
    hub.add_route("GET", r"/v1/me/audit", account_audit)
    hub.add_route("DELETE", r"/v1/passkeys/(?P<credential>[A-Za-z0-9_-]+)", remove_passkey)
    hub.add_route("GET", r"/v1/me/bundles/recovery", get_bundle)
    hub.add_route("PUT", r"/v1/me/bundles/recovery", put_bundle)
    hub.add_route("GET", r"/v1/me/agents", agents)
    hub.add_route("DELETE", r"/v1/me/agents/(?P<agent>[0-9a-f-]+)", revoke_agent)
    hub.add_route("DELETE", r"/v1/me/agents", revoke_agents)
    hub.add_route("POST", r"/v1/platform/invites", create_invite)
    hub.add_route("GET", r"/v1/platform/invites", list_invites)
    hub.add_route("POST", r"/v1/platform/invites/(?P<invite>[A-Za-z0-9_-]+)/revoke", revoke_invite)
    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/catch-up", catch_up)
