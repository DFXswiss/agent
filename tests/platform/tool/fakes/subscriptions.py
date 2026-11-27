"""Claude subscriptions in the hub fake as `agent_hub.subscriptions.routes` and `store` run them (ADR-090, 03-api.md
section 4.8):

- the primary record of a user is the `claude_subscription` row of `HubFake.credentials`; further records live in
  `records` by ID with their reading (`readings`, by ID; the primary one's under the user's ID);
- `GET /v1/me/subscriptions`: the caller's records, newest first, with their readings;
- `GET /v1/subscriptions/{id}`: an own record, else `404`;
- `PUT /v1/subscriptions/{id}`: an own further record (the primary one is `403 forbidden`) signed by the caller under
  the current `CK` epoch (`409 epoch_stale`), at the stored version (`409 version_conflict`);
- `DELETE /v1/subscriptions/{id}`: an own record (also the primary one), else `404`;
- `GET /v1/me/subscriptions/usage`: the answer in `usage` as it is (ADR-102), the queries in `usage_queries`;
- `GET /v1/me/devices` lists `features` once `announce` is on: `subscriptions` for every device but those in
  `outdated`, and `subscriptions.parallel` (KOR-73) for the devices in `parallel`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto.credential import verify_credential
from agent_runner.crypto.errors import CryptoError

from ..hubfake import HubFake, Json, Request, now_ms_text

KIND = "claude_subscription"


@dataclass
class Subscriptions:
    records: dict[str, Json] = field(default_factory=dict)
    readings: dict[str, Json | None] = field(default_factory=dict)
    announce: bool = False
    """`GET /v1/me/devices` lists `features` (on in the tests of this area; other suites see the rows as before)."""
    outdated: set[str] = field(default_factory=set)
    parallel: set[str] = field(default_factory=set)
    """Devices that announce `subscriptions.parallel` (ADR-100 section 7)."""
    puts: list[Json] = field(default_factory=list)
    conflicts: int = 0
    """The next PUTs answered `409 version_conflict` regardless."""
    raw_list: Json | None = None
    """An answer of `GET /v1/me/subscriptions` sent as it is (a hub that breaks the contract)."""
    refuse_delete: bool = False
    refuse_put: bool = False
    """Every PUT is answered `403 forbidden`."""
    usage: Json | None = None
    """The answer of `GET /v1/me/subscriptions/usage` (ADR-102), sent as it is; None answers no subscription."""
    usage_queries: list[dict[str, str]] = field(default_factory=list)


def state_of(hub: HubFake) -> Subscriptions:
    found = getattr(hub, "kor69_subscriptions", None)
    if found is None:
        found = Subscriptions()
        setattr(hub, "kor69_subscriptions", found)  # noqa: B010
    return cast(Subscriptions, found)


def add(hub: HubFake, owner: str, record: Json, reading: Json | None = None) -> str:
    """Seed a further record (as stored) and its reading."""
    header = record["header"]
    state_of(hub).records[header["id"]] = {"owner_id": owner, "record": record}
    state_of(hub).readings[header["id"]] = reading
    return str(header["id"])


def install(hub: HubFake) -> None:  # noqa: PLR0915
    found = state_of(hub)

    def rows(user: str) -> list[Json]:
        listed = []
        primary = hub.credentials.get((user, KIND))
        if primary is not None:
            listed.append((primary["record"], True, found.readings.get(user)))
        for record_id, row in found.records.items():
            if row["owner_id"] == user:
                listed.append((row["record"], False, found.readings.get(record_id)))
        return [
            {
                "id": record["header"]["id"],
                "owner_id": user,
                "primary": primary,
                "version": record["header"]["version"],
                "epoch": record["header"]["epoch"],
                "created_at": now_ms_text(),
                "updated_at": now_ms_text(),
                "reading": reading,
                "record": record,
            }
            for record, primary, reading in listed
        ]

    def own(user: str, record_id: str) -> Json | None:
        return next((row for row in rows(user) if row["id"] == record_id), None)

    def listing(request: Request, body: Any) -> tuple[int, Any]:
        if found.raw_list is not None:
            return 200, found.raw_list
        found_rows = rows(cast(str, request.user))
        return 200, {"subscriptions": [{k: v for k, v in row.items() if k != "record"} for row in found_rows]}

    def get(request: Request, body: Any, record_id: str) -> tuple[int, Any]:
        row = own(cast(str, request.user), record_id)
        if row is None:
            return 404, {"error": "not_found"}
        return 200, {"owner_id": row["owner_id"], "primary": row["primary"], "record": row["record"]}

    def put(request: Request, body: Json, record_id: str) -> tuple[int, Any]:  # noqa: PLR0911
        user = hub.users[cast(str, request.user)]
        found.puts.append(body)
        if found.refuse_put:
            return 403, {"error": "forbidden"}
        if found.conflicts:
            found.conflicts -= 1
            return 409, {"error": "version_conflict", "detail": {"expected": 0}}
        row = own(user.id, record_id)
        if row is not None and row["primary"]:
            return 403, {"error": "forbidden"}
        record = body["record"]
        header = record["header"]
        key = user.identity.sig_public() if header["author"] == user.id else hub.agents[header["author"]].sig
        try:
            checked = verify_credential(record, key)
        except CryptoError:
            return 400, {"error": "signature_invalid"}
        if (checked["owner"], checked["kind"], checked["id"]) != (user.id, KIND, record_id):
            return 400, {"error": "header_invalid"}
        if checked["epoch"] != user.cred_epoch:
            return 409, {"error": "epoch_stale"}
        version = 0 if row is None else int(row["version"])
        if body["expected_version"] != version:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        found.records[record_id] = {"owner_id": user.id, "record": record}
        return 200, {"version": checked["version"]}

    def delete(request: Request, body: Any, record_id: str) -> tuple[int, Any]:
        user = cast(str, request.user)
        row = own(user, record_id)
        if found.refuse_delete:
            return 403, {"error": "forbidden"}
        if row is None:
            return 404, {"error": "not_found"}
        if row["primary"]:
            hub.credentials.pop((user, KIND))
        else:
            del found.records[record_id]
        return 200, {}

    original_devices = hub.r_devices

    def devices(request: Request, body: Any) -> tuple[int, Any]:
        status, answer = original_devices(request, body)
        if status == 200 and found.announce:
            for row in answer["devices"]:
                if isinstance(row, dict) and "features" not in row:
                    device = row.get("device_id")
                    row["features"] = [] if device in found.outdated else ["subscriptions"]
                    if device in found.parallel:
                        row["features"].append("subscriptions.parallel")
        return status, answer

    def usage(request: Request, body: Any) -> tuple[int, Any]:
        found.usage_queries.append(dict(request.query))
        return 200, found.usage if found.usage is not None else {"period": "five_hour", "subscriptions": []}

    hub.override("r_devices", devices)
    path = r"/v1/subscriptions/(?P<record_id>[0-9a-f-]+)"
    hub.add_route("GET", r"/v1/me/subscriptions", listing)
    hub.add_route("GET", r"/v1/me/subscriptions/usage", usage)
    hub.add_route("GET", path, get)
    hub.add_route("PUT", path, put)
    hub.add_route("DELETE", path, delete)
