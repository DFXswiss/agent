"""The community routes of the hub fake as the hub runs them (`agent_hub.community.routes`, `distribution`, `store`;
14-community.md section 4). Signatures, wraps and messages are checked with the hub's own reference code
(`agent_hub.community.crypto`), so what the tool builds is refused as by the hub when it is wrong:

- `GET /v1/community`, `POST /v1/community/join`;
- key distribution: `POST /v1/community/epochs` (member, only while no epoch exists, the creator's self-wrap signed by
  its identity), `PUT /v1/community/request` (member without the key, signed request), `GET /v1/community/requests`
  (holder, not muted; open, unwrapped requests of others that did not reject the caller), `POST
  /v1/community/requests/wraps` (holder; signed wraps of the current epoch; gone, wrapped or rejecting requests
  skipped), `POST /v1/community/request/reject`, `POST /v1/community/wraps` (rule C1);
- messages: `GET /v1/community/messages` (`before`/`after`/`limit`, authors' keys of the rows that are not deleted),
  `POST /v1/community/messages` (member, not muted, holder, `author` = caller, current epoch, author signature;
  idempotent by ID), `DELETE /v1/community/messages/{id}` (author or moderator), `PUT /v1/community/read`;
- moderation: `GET /v1/community/mutes`, `PUT`/`DELETE /v1/community/mutes/{user}` (moderators; a moderator cannot be
  muted). Moderators are granted by the test (`Community.moderators`), as the operator does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_hub.community import crypto as reference
from agent_hub.crypto.encoding import canonical
from agent_hub.crypto.errors import CryptoError
from agent_runner.crypto.encoding import b64u_encode

from ..hubfake import HubFake, Json, Request, now_ms_text

PAGE, MAX_PAGE, REQUESTS = 50, 100, 20


@dataclass
class Community:
    epoch: int | None = None
    key_check: str | None = None
    wraps: dict[str, Json] = field(default_factory=dict)
    requests: dict[str, Json] = field(default_factory=dict)
    rejects: set[tuple[str, str]] = field(default_factory=set)
    members: dict[str, Json] = field(default_factory=dict)
    moderators: set[str] = field(default_factory=set)
    messages: list[Json] = field(default_factory=list)


def state(hub: HubFake) -> Community:
    found = getattr(hub, "community", None)
    if found is None:
        found = Community()
        setattr(hub, "community", found)  # noqa: B010
    return cast(Community, found)


class Routes:
    def __init__(self, hub: HubFake) -> None:
        self.hub = hub
        self.data = state(hub)

    def sig_public(self, user: str) -> bytes:
        return self.hub.users[user].identity.sig_public()

    def member(self, request: Request) -> tuple[str, Json] | tuple[int, Json]:
        user = cast(str, request.user)
        row = self.data.members.get(user)
        return (409, {"error": "community_not_member"}) if row is None else (user, row)

    def holder(self, user: str) -> bool:
        return self.data.epoch is not None and user in self.data.wraps

    # --- state ---

    def get(self, request: Request, body: Any) -> tuple[int, Any]:
        user = cast(str, request.user)
        row = self.data.members.get(user)
        pending = self.data.requests.get(user)
        wrapped = None if pending is None else pending["wrapped"]
        unread = [m for m in self.data.messages if m["seq"] > (row or {}).get("last_read", 0) and m["author"] != user]
        return 200, {
            "epoch": None if self.data.epoch is None else {"epoch": self.data.epoch, "key_check": self.data.key_check},
            "wrap": self.data.wraps.get(user),
            "request": None if pending is None else {"id": pending["id"], "wrapped": wrapped},
            "member": {
                "joined": row is not None,
                "muted": row is not None and row["muted"] is not None,
                "posted": any(m["author"] == user for m in self.data.messages),
                "last_read_seq": 0 if row is None else row["last_read"],
            },
            "moderator": user in self.data.moderators,
            "unread": len([m for m in unread if m["deleted"] is None]),
            "head_seq": len(self.data.messages),
            "limits": {"text_chars": 2000, "name_chars": 64, "burst": 5, "refill_seconds": 6},
        }

    def join(self, request: Request, body: Any) -> tuple[int, Any]:
        self.data.members.setdefault(cast(str, request.user), {"last_read": 0, "muted": None})
        return 200, {"joined": True}

    # --- key distribution ---

    def _self_wrap(self, user: str, body: Json) -> tuple[int, Json] | None:
        try:
            reference.verify_community_wrap(
                self.sig_public(user),
                epoch=body["epoch"],
                recipient=user,
                wrap_text=body["wrap"],
                signature=body["sig"],
            )
        except CryptoError as error:
            return 401, {"error": error.code}
        return None

    def epochs(self, request: Request, body: Json) -> tuple[int, Any]:
        found = self.member(request)
        if isinstance(found[0], int):
            return found
        user = found[0]
        if self.data.epoch is not None:
            return 409, {"error": "community_epoch_exists"}
        refused = self._self_wrap(user, body)
        if refused is not None or body["epoch"] != 1:
            return refused or (400, {"error": "request_invalid"})
        self.data.epoch, self.data.key_check = 1, body["key_check"]
        self.data.wraps[user] = {"epoch": 1, "wrap": body["wrap"], "sig": body["sig"]}
        self.data.requests.pop(user, None)
        return 200, {"epoch": 1}

    def put_request(self, request: Request, body: Json) -> tuple[int, Any]:
        found = self.member(request)
        if isinstance(found[0], int):
            return found
        user = found[0]
        if self.holder(user):
            return 403, {"error": "forbidden"}
        try:
            reference.verify_request(
                self.sig_public(user), id=body["id"], kem_pub=body["kem_pub"], user=user, signature=body["sig"]
            )
        except CryptoError as error:
            return 401, {"error": error.code}
        self.data.requests[user] = {"id": body["id"], "kem_pub": body["kem_pub"], "wrapped": None}
        return 200, {"id": body["id"]}

    def open_requests(self, request: Request, body: Any) -> tuple[int, Any]:
        user = cast(str, request.user)
        if not self.holder(user):
            return 409, {"error": "community_no_key"}
        if self.data.members[user]["muted"] is not None:
            return 403, {"error": "forbidden"}
        limit = int(request.query.get("limit", REQUESTS))
        found = [
            {"id": row["id"], "kem_pub": row["kem_pub"]}
            for owner, row in self.data.requests.items()
            if owner != user and row["wrapped"] is None and (user, owner) not in self.data.rejects
        ]
        return 200, {"requests": found[:limit]}

    def wrap_requests(self, request: Request, body: Json) -> tuple[int, Any]:
        user = cast(str, request.user)
        if not self.holder(user):
            return 409, {"error": "community_no_key"}
        for item in body["wraps"]:
            if item["epoch"] != self.data.epoch:
                return 400, {"error": "epoch_stale"}
            try:
                reference.verify_community_wrap(
                    self.sig_public(user),
                    epoch=item["epoch"],
                    recipient=item["request"],
                    wrap_text=item["wrap"],
                    signature=item["sig"],
                )
            except CryptoError as error:
                return 401, {"error": error.code}
        stored = 0
        for item in body["wraps"]:
            owner = next((o for o, row in self.data.requests.items() if row["id"] == item["request"]), None)
            if owner is None or owner == user or (user, owner) in self.data.rejects:
                continue
            row = self.data.requests[owner]
            if row["wrapped"] is not None:
                continue
            row["wrapped"] = {
                "epoch": item["epoch"],
                "wrap": item["wrap"],
                "sig": item["sig"],
                "wrapped_by": user,
                "wrapper_sig": b64u_encode(self.sig_public(user)),
            }
            stored += 1
        return 200, {"stored": stored, "skipped": len(body["wraps"]) - stored}

    def reject(self, request: Request, body: Any) -> tuple[int, Any]:
        user = cast(str, request.user)
        row = self.data.requests.get(user)
        if row is None or row["wrapped"] is None:
            return 404, {"error": "not_found"}
        self.data.rejects.add((row["wrapped"]["wrapped_by"], user))
        row["wrapped"] = None
        return 200, {"rejected": True}

    def self_wrap(self, request: Request, body: Json) -> tuple[int, Any]:
        found = self.member(request)
        if isinstance(found[0], int):
            return found
        user = found[0]
        if self.data.epoch is None:
            return 409, {"error": "community_no_key"}
        if body["epoch"] != self.data.epoch:
            return 400, {"error": "epoch_stale"}
        refused = self._self_wrap(user, body)
        if refused is not None:
            return refused
        row = self.data.requests.get(user)
        if user not in self.data.wraps and (row is None or row["wrapped"] is None):
            return 403, {"error": "forbidden"}
        self.data.wraps.setdefault(user, {"epoch": body["epoch"], "wrap": body["wrap"], "sig": body["sig"]})
        self.data.requests.pop(user, None)
        return 200, {"epoch": body["epoch"]}

    # --- messages ---

    def messages(self, request: Request, body: Any) -> tuple[int, Any]:
        limit = int(request.query.get("limit", PAGE))
        rows = self.data.messages
        following = None
        if "after" in request.query:
            rows = [m for m in rows if m["seq"] > int(request.query["after"])][:limit]
        else:
            bound = int(request.query.get("before", 2**62))
            older = [m for m in reversed(rows) if m["seq"] < bound]
            rows = older[:limit]
            following = str(rows[-1]["seq"]) if len(older) > limit else None
        authors = {m["author"] for m in rows if m["deleted"] is None}
        return 200, {
            "messages": [dict(m) for m in rows],
            "authors": {
                a: {"sig_pub": b64u_encode(self.sig_public(a)), "moderator": a in self.data.moderators} for a in authors
            },
            "next_before": following,
        }

    def post(self, request: Request, body: Json) -> tuple[int, Any]:  # noqa: PLR0911
        found = self.member(request)
        if isinstance(found[0], int):
            return found
        user, row = found
        if row["muted"] is not None:
            return 403, {"error": "community_muted"}
        if not self.holder(user):
            return 409, {"error": "community_no_key"}
        message = body["message"]
        try:
            header = reference.verify_message(message, self.sig_public(user))
        except CryptoError as error:
            return (401 if error.code == "signature_invalid" else 400), {"error": error.code}
        if header["author"] != user:
            return 403, {"error": "forbidden"}
        if header["epoch"] != self.data.epoch:
            return 400, {"error": "epoch_stale"}
        for stored in self.data.messages:
            if stored["id"] == header["id"]:
                same = stored["message"] is not None and canonical(stored["message"]) == canonical(message)
                return (200, {"seq": stored["seq"], "replayed": True}) if same else (409, {"error": "id_conflict"})
        seq = len(self.data.messages) + 1
        self.data.messages.append(
            {
                "seq": seq,
                "id": header["id"],
                "author": user,
                "created_at": now_ms_text(),
                "deleted": None,
                "message": message,
            }
        )
        return 200, {"seq": seq}

    def delete(self, request: Request, body: Any, message: str) -> tuple[int, Any]:
        user = cast(str, request.user)
        stored = next((m for m in self.data.messages if m["id"] == message), None)
        if stored is None:
            return 404, {"error": "not_found"}
        if stored["deleted"] is None:
            if stored["author"] == user:
                stored["deleted"] = "author"
            elif user in self.data.moderators:
                stored["deleted"] = "moderator"
            else:
                return 403, {"error": "forbidden"}
            stored["message"] = None
        return 200, {"deleted": stored["deleted"]}

    def read(self, request: Request, body: Json) -> tuple[int, Any]:
        found = self.member(request)
        if isinstance(found[0], int):
            return found
        row = found[1]
        row["last_read"] = max(row["last_read"], min(body["seq"], len(self.data.messages)))
        return 200, {"last_read_seq": row["last_read"]}

    # --- moderation ---

    def mutes(self, request: Request, body: Any) -> tuple[int, Any]:
        if request.user not in self.data.moderators:
            return 403, {"error": "forbidden"}
        rows = [{"user": u, "muted_at": row["muted"]} for u, row in self.data.members.items() if row["muted"]]
        return 200, {"mutes": rows}

    def mute(self, request: Request, body: Any, user: str) -> tuple[int, Any]:
        muted = request.method == "PUT"
        if request.user not in self.data.moderators or (muted and user in self.data.moderators):
            return 403, {"error": "forbidden"}
        row = self.data.members.get(user)
        if row is None:
            return 404, {"error": "not_found"}
        row["muted"] = now_ms_text() if muted else None
        return 200, {"muted": muted}


def install(hub: HubFake) -> None:
    routes = Routes(hub)
    uuid = r"[0-9a-f-]+"
    for method, pattern, handler in (
        ("GET", r"/v1/community", routes.get),
        ("POST", r"/v1/community/join", routes.join),
        ("POST", r"/v1/community/epochs", routes.epochs),
        ("PUT", r"/v1/community/request", routes.put_request),
        ("GET", r"/v1/community/requests", routes.open_requests),
        ("POST", r"/v1/community/requests/wraps", routes.wrap_requests),
        ("POST", r"/v1/community/request/reject", routes.reject),
        ("POST", r"/v1/community/wraps", routes.self_wrap),
        ("GET", r"/v1/community/messages", routes.messages),
        ("POST", r"/v1/community/messages", routes.post),
        ("DELETE", rf"/v1/community/messages/(?P<message>{uuid})", routes.delete),
        ("PUT", r"/v1/community/read", routes.read),
        ("GET", r"/v1/community/mutes", routes.mutes),
        ("PUT", rf"/v1/community/mutes/(?P<user>{uuid})", routes.mute),
        ("DELETE", rf"/v1/community/mutes/(?P<user>{uuid})", routes.mute),
    ):
        hub.add_route(method, pattern, handler)
