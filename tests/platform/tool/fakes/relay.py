"""Device requests of the hub fake as the hub runs them (`agent_hub.requests.routes` and `store`, 03-api.md section
4.10) and the device side for the tests:

- `POST /v1/device-requests {envelope}`: the envelope verifies with the caller's identity (crypto codes as the hub
  answers them), `header.user` is the caller (`403 forbidden`), the kind is a known one (`400 header_invalid`), a
  follow-up names an earlier request of the same user, device and kind (`400 params_invalid`), the device may read the
  user's `CK` by the logs (`403 device_not_assigned`); a plaintext `org_id` (a company account, KOR-47) is accepted
  for a `github.connect` of an admin of that org only (`403 forbidden`) and kept with the request; the same ID again
  is `replayed` when identical, else `409 id_conflict`; a new root request ends an active one of the same user, device
  and kind (`failed`);
- `GET /v1/device-requests/{id}` (owner, else `404`): state, expiry and the responses in `seq` order;
- `GET /v1/me/devices` without seeded rows lists the devices the logs make eligible (assigned to a workgroup the user
  can access), online.

`respond` seals a response of the request's device (the platform device's identity), `script` lets the device answer
a new request at once, `opened` opens a request as the device does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from agent_runner.crypto.encoding import body_json, canonical, parse_json
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.wraps import cred_readers
from agent_runner.crypto.request import ENC_ALG, open_request, seal_response, verify_request

from ..hubfake import HubFake, Json, Request, now_ms_text

KINDS = ("github.connect", "claude.login", "credential.check", "mailbox.check", "github.pull_status")
ACTIVE = ("pending", "delivered")


@dataclass
class Relay:
    requests: dict[str, Json] = field(default_factory=dict)
    offline: set[str] = field(default_factory=set)
    script: dict[str, list[list[tuple[object, bool]]]] = field(default_factory=dict)
    """Per kind, the answers the device gives at once to the next requests of that kind (one list per request); a
    callable answer is called with the opened request body."""


def relay(hub: HubFake) -> Relay:
    found = getattr(hub, "stage_b_relay", None)
    if found is None:
        found = Relay()
        setattr(hub, "stage_b_relay", found)  # noqa: B010
    return cast(Relay, found)


def respond(hub: HubFake, request: str, body: object, *, final: bool = False, sign_with: str | None = None) -> None:
    """A response of the request's device (or, to forge one, of the device `sign_with`)."""
    stored = relay(hub).requests[request]
    device = hub.platform_devices[sign_with or stored["device_id"]]
    user = hub.users[stored["user"]]
    header = {
        "v": 1,
        "id": uuid7(),
        "user": user.id,
        "device": stored["device_id"],
        "kind": stored["kind"],
        "in_reply_to": request,
        "seq": len(stored["responses"]) + 1,
        "final": final,
        "ts": now_ms_text(),
        "enc_alg": ENC_ALG,
    }
    envelope = seal_response(header, user.identity.kem_public(), body_json(body), device.sig)
    stored["responses"].append({"seq": header["seq"], "envelope": envelope, "created_at": now_ms_text()})
    stored["state"] = "answered" if final else "delivered"


def opened(hub: HubFake, request: str) -> object:
    """The body of a request as its device opens it."""
    stored = relay(hub).requests[request]
    user = hub.users[stored["user"]]
    return parse_json(
        open_request(hub.platform_devices[stored["device_id"]].kem, stored["envelope"], user.identity.sig_public())
    )


def eligible(hub: HubFake, user: str) -> list[Json]:
    rows = []
    for org in hub.orgs.values():
        state = org.log.state
        if state.member(user) is None:
            continue
        skip = hub.platform_state().excluded(state)
        for device in sorted(cred_readers([state], user, skip) - {user}):
            wgs = [wg for wg in state.device_wgs(device) if state.wg_access(user, wg) is not None]
            online = device not in relay(hub).offline
            rows.append(
                {
                    "device_id": device,
                    "kind": "platform",
                    "org_id": org.id,
                    "wg_ids": wgs,
                    "online": online,
                    "last_seen_at": None,
                }
            )
    return rows


def install(hub: HubFake) -> None:
    found = relay(hub)
    original_devices = hub.r_devices

    def devices(request: Request, body: Any) -> tuple[int, Any]:
        if request.user in hub.devices:
            return original_devices(request, body)
        return 200, {"devices": eligible(hub, cast(str, request.user))}

    def create(request: Request, body: Any) -> tuple[int, Any]:  # noqa: PLR0911, PLR0912
        if not isinstance(body, dict) or set(body) - {"org_id"} != {"envelope"}:
            return 400, {"error": "request_invalid"}
        user = hub.users[cast(str, request.user)]
        envelope = body["envelope"]
        header = verify_request(envelope, user.identity.sig_public())
        if header["user"] != user.id:
            return 403, {"error": "forbidden"}
        org = body.get("org_id")
        if org is not None and (
            header["kind"] != "github.connect" or org not in hub.orgs or not hub.orgs[org].log.state.is_admin(user.id)
        ):
            return 403, {"error": "forbidden"}
        if header["kind"] not in KINDS:
            return 400, {"error": "header_invalid"}
        known = found.requests.get(cast(str, header["id"]))
        if known is not None:
            if canonical(known["envelope"]) != canonical(envelope):
                return 409, {"error": "id_conflict"}
            return 200, {"request_id": header["id"], "state": known["state"], "replayed": True}
        follows = header["follows"]
        if follows:
            original = found.requests.get(cast(str, follows))
            place = (user.id, header["device"], header["kind"])
            if original is None or (original["user"], original["device_id"], original["kind"]) != place:
                return 400, {"error": "params_invalid"}
        if header["device"] not in {row["device_id"] for row in eligible(hub, user.id)}:
            return 403, {"error": "device_not_assigned"}
        if not follows:
            for other in found.requests.values():
                same = (other["user"], other["device_id"], other["kind"]) == (user.id, header["device"], header["kind"])
                if same and other["state"] in ACTIVE and not other["follows"]:
                    other["state"] = "failed"
        found.requests[cast(str, header["id"])] = {
            "request_id": header["id"],
            "kind": header["kind"],
            "device_id": header["device"],
            "state": "pending",
            "created_at": now_ms_text(),
            "expires_at": now_ms_text(timedelta(minutes=15)),
            "responses": [],
            "envelope": envelope,
            "user": user.id,
            "follows": follows,
            "org_id": org,
        }
        answers = found.script.get(header["kind"])
        for answer, final in answers.pop(0) if answers else []:
            given = answer(opened(hub, cast(str, header["id"]))) if callable(answer) else answer
            respond(hub, cast(str, header["id"]), given, final=final)
        return 200, {"request_id": header["id"], "state": found.requests[cast(str, header["id"])]["state"]}

    def view(request: Request, body: Any, request_id: str) -> tuple[int, Any]:
        stored = found.requests.get(request_id)
        if stored is None or stored["user"] != request.user:
            return 404, {"error": "not_found"}
        keys = ("request_id", "kind", "device_id", "state", "created_at", "expires_at", "responses")
        return 200, {key: stored[key] for key in keys}

    hub.override("r_devices", devices)
    hub.add_route("POST", r"/v1/device-requests", create)
    hub.add_route("GET", r"/v1/device-requests/(?P<request_id>[0-9a-f-]+)", view)
