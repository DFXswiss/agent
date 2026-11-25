"""Test kit of the KOR-69 suites (Claude subscriptions): the hub's `/v1/device/subscriptions` installed on the rig's
hub (answers and requests validated against the contract), subscription records sealed in the owner's `cred` scope."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import parse_json
from agent_runner.crypto.ids import uuid7

from .kit import WALL, CredHub, ck, make_record, ms

Json = dict[str, Any]
KIND = "claude_subscription"


def subscription_body(token: str | None = "sk-ant-oat01-double-subscription-token-1", **extra: Any) -> Json:
    body: Json = {"email": "team@example.com", "kind": "setup_token", **extra}
    if token is not None:
        body["token"] = token
    return body


@dataclass
class Served:
    owner_id: str
    primary: bool
    record: Json


@dataclass
class SubscriptionHub:
    """The device endpoints of subscriptions (03-api.md section 6.1), installed on a rig hub."""

    hub: CredHub
    served: dict[str, Served] = field(default_factory=dict)
    refusals: dict[str, tuple[int, Json]] = field(default_factory=dict)
    list_refusals: dict[str, tuple[int, Json]] = field(default_factory=dict)
    raw_lists: dict[str, Json] = field(default_factory=dict)
    put_refusals: list[tuple[int, Json]] = field(default_factory=list)
    puts: list[Json] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def install(self) -> SubscriptionHub:
        original = self.hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            split = urlsplit(target)
            parts = split.path.strip("/").split("/")
            if parts[:3] == ["v1", "device", "subscriptions"] and len(parts) == 4:
                return self.answer(parts[3]) if method == "GET" else self.put(parts[3], body)
            if method == "GET" and parts == ["v1", "device", "subscriptions"]:
                return self.listing(parse_qs(split.query))
            answer: tuple[int, dict[str, Any]] = original(method, target, body)
            return answer

        self.hub.route = route  # type: ignore[method-assign]
        return self

    def answer(self, record_id: str) -> tuple[int, Json]:
        self.asked.append(record_id)
        if record_id in self.refusals:
            return self.refusals[record_id]
        found = self.served.get(record_id)
        if found is None:
            return 404, {"error": "not_found"}
        answer = {"owner_id": found.owner_id, "primary": found.primary, "record": found.record}
        self.hub.check("api/device-subscription-get.response.json", answer)
        return 200, answer

    def listing(self, query: dict[str, list[str]]) -> tuple[int, Json]:
        self.hub.check("api/device-subscriptions-list.request.json", {k: v[0] for k, v in query.items()})
        owner = query["owner_id"][0]
        if owner in self.list_refusals:
            return self.list_refusals[owner]
        if owner in self.raw_lists:
            return 200, self.raw_lists[owner]
        items = [
            {
                "id": record_id,
                "owner_id": found.owner_id,
                "primary": found.primary,
                "version": found.record["header"]["version"],
                "epoch": found.record["header"]["epoch"],
                "created_at": ms(WALL),
                "updated_at": ms(WALL),
            }
            for record_id, found in sorted(self.served.items())
            if found.owner_id == owner
        ]
        answer = {"subscriptions": items}
        self.hub.check("api/device-subscriptions-list.response.json", answer)
        return 200, answer

    def put(self, record_id: str, body: Any) -> tuple[int, Json]:
        self.hub.check("api/device-subscription-put.request.json", body)
        self.puts.append(body)
        if self.put_refusals:
            return self.put_refusals.pop(0)
        record = body["record"]
        header = credential_format.verify_credential(record, self.hub.device_public)
        owner = body["owner_id"]
        if body.get("request_id") is not None:
            request = self.hub.stored_requests.get(body["request_id"])
            if request is None or request.closed or request.envelope["header"]["user"] != owner:
                return 403, {"error": "forbidden"}
        if (header["id"], header["owner"], header["kind"]) != (record_id, owner, KIND):
            return 400, {"error": "header_invalid"}
        if header["epoch"] != self.hub.ck_epoch:
            return 400, {"error": "epoch_stale"}
        current = self.served.get(record_id)
        version = 0 if current is None else int(current.record["header"]["version"])
        if body["expected_version"] != version:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        self.served[record_id] = Served(owner, False, record)
        return 200, {"version": version + 1}

    def add(
        self,
        body: Json,
        *,
        owner: str,
        author: str = "alice",
        primary: bool = False,
        version: int = 1,
        epoch: int = 1,
        record_id: str | None = None,
    ) -> str:
        found = record_id or uuid7()
        record = make_record(body, owner=owner, kind=KIND, version=version, epoch=epoch, author=author, record_id=found)
        self.served[found] = Served(owner, primary, record)
        return found

    def opened(self, record_id: str, epoch: int | None = None) -> Json:
        """The body of a served record (written by the device), opened with the owner's key."""
        found = self.served[record_id]
        header = found.record["header"]
        key = ck(found.owner_id, epoch or int(header["epoch"]))
        return cast(Json, parse_json(credential_format.open_credential(key, found.record, self.hub.device_public)))
