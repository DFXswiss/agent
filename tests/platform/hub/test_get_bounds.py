"""U-HUB-bounds (ADR-086 section 2, KOR-35): no `GET` route of the hub answers without a bound.

Every `GET` route of the app - as the app discovers them, so a later package's route is checked here as well - needs
an entry in `BOUNDS`:

- `paged`: the caller pages with a cursor and a `limit` of the contract (`after`, `before` or `cursor`; the request
  schema of `contracts/api/INDEX.json` declares `limit`);
- `capped:<n>`: the answer holds at most n items of its largest list;
- `structural:<reason>`: the size follows from the structure (one object, a closed list, key material or the
  membership that must stay complete, an administrative set) - the reason is part of the entry.

A new `GET` route without an entry, or an entry without a route, fails. Routes outside `/v1/` are the static files and
pages of the web client (`agent_hub.static_routes`) and the download area (`agent_hub.downloads.routes`, ADR-074
section 7), whose routes need an entry in `FILES`.
"""

from __future__ import annotations

import json
import re

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from agent_hub.http.routing import API_PREFIX, HubRouter, route_spec
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import HubHarness

KEYS = "structural: key material, complete by design (a cut would lose keys; ADR-086 section 2)"
ONE = "structural: one object"
BOUNDS: dict[str, str] = {
    "/v1/agent-pairings/{pairing_id}": ONE,
    "/v1/agent/handover": "structural: the handover of one remote pairing",
    "/v1/agent/me": ONE,
    "/v1/community": "structural: the user's own community state (epoch, wrap, request, limits)",
    "/v1/community/messages": "paged",
    "/v1/community/moderators": "structural: the moderators the platform root granted (administrative set)",
    "/v1/community/mutes": "structural: the users moderators muted, each an explicit moderator action",
    "/v1/community/requests": "capped:100",
    "/v1/config": "structural: the hub configuration",
    "/v1/device-requests/{id}": "structural: one request and its responses until it ends (15 min)",
    "/v1/device/contexts/{object}": "structural: one manifest",
    "/v1/device/contexts/{object}/chunks/{index}": "structural: one chunk (at most CHUNK_CT_BYTES)",
    # ATT-01, ADR-102 section 3
    "/v1/device/attachments/{object}/chunks/{index}": "structural: one chunk of an attachment (at most 1 MiB + 28 B)",
    "/v1/workgroups/{wg}/attachments/{object}/chunks/{index}": (
        "structural: one chunk of an attachment (at most 1 MiB + 28 B)"
    ),
    "/v1/device/credentials/{owner}/{kind}": ONE,
    "/v1/device/github-identities": (
        "structural: the GitHub accounts of one owner, each one connected by an explicit sign-in at GitHub"
        " (administrative set, complete by design: the device needs every one of them)"
    ),
    "/v1/device/github-identities/{id}": ONE,
    "/v1/device/subscriptions": "capped:40",
    "/v1/device/subscriptions/{id}": ONE,
    "/v1/device/mailboxes": "capped:200",  # KOR-74, ADR-099 section 2
    "/v1/device/logs": "paged",
    "/v1/device/platform-log": "paged",
    "/v1/device/pull": "paged",
    "/v1/device/requests/{id}": ONE,
    "/v1/device/sessions/{session}/orchestrator": "structural: one session's owner, request and newest pairing",
    "/v1/device/state": "structural: the device's assignments, sessions and VMs, bounded by its capacity",
    "/v1/device/tasks/{task}": "capped:100",
    "/v1/device/workgroups/{wg}/tasks": "paged",
    # KOR-53 (ADR-097 section 4): the analysis reads of one workgroup; `group_by` answers at most 200 groups
    "/v1/device/workgroups/{wg}/usage": "paged",
    "/v1/device/workgroups/{wg}/audit": "paged",
    "/v1/device/workgroups/{wg}/operations": "paged",
    "/v1/device/workgroups/{wg}/metrics": "capped:1000",
    "/v1/device/wraps": KEYS,
    "/v1/github-identities/{id}": ONE,
    "/v1/subscriptions/{id}": ONE,
    "/v1/mailboxes/{id}": ONE,
    "/v1/workgroups/{wg}/agent-profiles": (
        "structural: the built-in roles and the own profiles of one organization and workgroup, each created by an"
        " explicit change of a writer or an admin (administrative set)"
    ),
    "/v1/health": ONE,
    "/v1/me": "structural: the user's memberships and passkeys (administrative actions)",
    "/v1/me/agents": "structural: the user's agents (each one a confirmed pairing)",
    "/v1/me/audit": "paged",
    "/v1/me/bundles/recovery": ONE,
    "/v1/me/credentials": "structural: one record per credential kind (closed list)",
    "/v1/me/credentials/{kind}": ONE,
    "/v1/me/devices": "structural: the devices assigned to the user's workgroups (administrative assignments)",
    "/v1/me/github-identities": (
        "structural: the GitHub accounts, each one connected by an explicit sign-in at GitHub (own ones and company"
        " ones of the user's organizations; administrative set), and the defaults admins or writers set"
    ),
    "/v1/me/invitees": "capped:1000",
    "/v1/me/quotas": "structural: one row per quota window",
    "/v1/me/subscriptions": "capped:40",
    "/v1/me/mailboxes": "capped:100",
    "/v1/monitoring/overview": "capped:2000",
    "/v1/tasks/{task}/logs/{object}": "structural: the metadata of one stored log object",
    "/v1/tasks/{task}/logs/{object}/chunks/{index}": "structural: one chunk of a log object (bounded size)",
    "/v1/orgs/{org}/quotas": "structural: own and operator subscriptions (contract caps)",
    "/v1/orgs/{org}/support": "structural: one release per workgroup at most, the support roster (administrative)",
    "/v1/monitoring/series": "capped:20000",
    "/v1/notifications": "paged",
    "/v1/operations/{id}": "capped:100",
    "/v1/orgs/{org}": "structural: the membership and the workgroups, complete by design (ADR-086 section 2)",
    "/v1/orgs/{org}/attention": "paged",
    "/v1/orgs/{org}/attention/summary": "structural: five counters of the org's attention categories",
    "/v1/orgs/{org}/main-agents": "paged",
    "/v1/orgs/{org}/people/agents": "paged",
    "/v1/orgs/{org}/audit": "paged",
    "/v1/orgs/{org}/audit/verify": "structural: a summary of the chain",
    "/v1/orgs/{org}/billing": (
        "structural: the newest 100 credit entries (credits_truncated when cut), 30 days and one row per workgroup of"
        " the org"
    ),
    "/v1/orgs/{org}/catch-up": KEYS,
    "/v1/orgs/{org}/devices": "paged",
    "/v1/orgs/{org}/invites": "paged",
    "/v1/orgs/{org}/log": "paged",
    "/v1/platform/enrollments": "paged",
    "/v1/platform/invites": "paged",
    "/v1/platform/log": "paged",
    "/v1/support/releases": "capped:1000",
    "/v1/platform/ui-health": "capped:10000",
    "/v1/platform/users/{user}": ONE,
    "/v1/platform/waiting": "capped:10000",
    "/v1/scopes/{scope_id}/events": "paged",
    "/v1/sessions/{session}": "capped:1000",
    "/v1/sessions/{session}/orchestrator": "structural: one session's availability and newest pairing",
    "/v1/stream": "structural: the live channel, bounded per connection (4 096 frames / 8 MiB, then resync)",
    "/v1/tasks/{task}": "capped:100",
    "/v1/tasks/{task}/charges": "paged",
    "/v1/tasks/{task}/contexts": "capped:1000",
    "/v1/tasks/{task}/operations": "paged",
    "/v1/tasks/{task}/queue": "capped:10000",
    "/v1/users/lookup": ONE,
    "/v1/users/{user}/identity": ONE,
    "/v1/whiteboards": "paged",
    "/v1/whiteboards/{entry}/history": "paged",
    "/v1/workgroups/{wg}": "structural: the members and devices of one workgroup (administrative actions)",
    "/v1/workgroups/{wg}/charges": "paged",
    "/v1/workgroups/{wg}/placement": "structural: the workgroup's one default device and VM",
    "/v1/workgroups/{wg}/pr-links": "paged",
    "/v1/workgroups/{wg}/pr-links/{epoch}/{tag}": ONE,
    "/v1/workgroups/{wg}/device-candidates": "paged",
    "/v1/workgroups/{wg}/tasks": "paged",
    "/v1/wraps": KEYS,
}
FILES: dict[str, str] = {
    "/downloads/runner/{name}": "structural: one file of a closed list of names (agent_hub.downloads.routes)",
}
"""The `GET` routes of the download area: one file each, never a list."""
OTHER_MODULES = {"agent_hub.static_routes", "agent_hub.downloads.routes"}
_BOUND_RE = re.compile(r"paged|capped:[1-9][0-9]*|structural: \S.*")
CURSORS = {"after", "before", "cursor"}


def gets(routes: list[BaseRoute]) -> tuple[set[str], set[str]]:
    """`(API paths, other paths)` of every declared `GET` route."""
    api, other = set(), set()
    for route in routes:
        spec = route_spec(route)
        if not isinstance(route, APIRoute) or spec is None or "GET" not in (route.methods or set()):
            continue
        if route.path.startswith(API_PREFIX):
            api.add(route.path)
        else:
            other.add(route.path)
            assert route.endpoint.__module__ in OTHER_MODULES, route.path
            assert route.endpoint.__module__ == "agent_hub.static_routes" or route.path in FILES, route.path
    return api, other


def test_every_get_route_has_a_bound(hub: HubHarness) -> None:
    api, other = gets(list(hub.app.router.routes))
    assert "/{rest:path}" in other
    missing = sorted(api - set(BOUNDS))
    assert missing == [], f"GET routes without a bound (ADR-086 section 2): {missing}"
    assert sorted(set(BOUNDS) - api) == []
    assert set(FILES) <= other
    for path, bound in {**BOUNDS, **FILES}.items():
        assert _BOUND_RE.fullmatch(bound), (path, bound)


def test_paged_routes_take_a_cursor_and_a_limit() -> None:
    rows = json.loads((CONTRACTS / "api" / "INDEX.json").read_text(encoding="utf-8"))["endpoints"]
    requests = {row["path"]: row["request"] for row in rows if row["method"] == "GET"}
    for path, bound in BOUNDS.items():
        if bound != "paged":
            continue
        schema = json.loads((CONTRACTS / requests[path]).read_text(encoding="utf-8"))
        names = set(schema["properties"])
        assert "limit" in names, path
        assert names & CURSORS, path


def test_a_new_route_without_a_bound_is_found() -> None:
    """The check itself: an undeclared route is reported, a static route is not an API route."""
    router = HubRouter()

    @router.get("/v1/unbounded", auth="anon", right="anon")
    async def unbounded() -> None: ...

    api, other = gets(list(router.routes))
    assert (api - set(BOUNDS), other) == ({"/v1/unbounded"}, set())
