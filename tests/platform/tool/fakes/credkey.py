"""The credential-key routes of the hub fake as `agent_hub.credentials.records` runs them:

- `POST /v1/me/credentials/rotate` `{epoch, wraps, records}`: `epoch` is the current `CK` epoch + 1 (`409
  version_conflict`); every wrap is of `cred`/the caller/`epoch` and one is for the caller (`400 request_invalid`);
  every stored record comes back exactly once, signed by the caller as author and owner under the new epoch, same ID,
  `version + 1` (`409 version_conflict` for a missing record or a wrong version); every wrap passes W3 for the `cred`
  scope against the caller's logs with the decertified devices excluded. All or nothing.
- `DELETE /v1/me/credentials/{kind}` (`404 not_found` when there is none).
"""

from __future__ import annotations

from typing import Any, cast

from agent_runner.crypto.credential import verify_credential
from agent_runner.crypto.log.wraps import CRED, check_cred_wrap

from ..hubfake import HubFake, Json, Request, User, now_ms_text


class Refused(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def require(condition: bool, status: int, code: str) -> None:
    if not condition:
        raise Refused(status, code)


def install(hub: HubFake) -> None:
    def checked(user: User, body: Json) -> dict[str, Json]:
        """The records of a rotation request by kind (module docstring); `Refused` otherwise."""
        require(set(body) == {"epoch", "wraps", "records"} and bool(body["wraps"]), 400, "request_invalid")
        epoch = body["epoch"]
        require(epoch == user.cred_epoch + 1, 409, "version_conflict")
        require(
            all((w["scope"], w["scope_id"], w["epoch"]) == (CRED, user.id, epoch) for w in body["wraps"]),
            400,
            "request_invalid",
        )
        require(any(w["recipient_id"] == user.id for w in body["wraps"]), 400, "request_invalid")
        stored = {kind: row for (owner, kind), row in hub.credentials.items() if owner == user.id}
        updates: dict[str, Json] = {}
        for record in body["records"]:
            header = verify_credential(record, user.identity.sig_public())
            require(
                (header["owner"], header["author"], header["epoch"]) == (user.id, user.id, epoch),
                400,
                "request_invalid",
            )
            previous = stored.pop(cast(str, header["kind"]), None)
            require(previous is not None and header["version"] == previous["version"] + 1, 409, "version_conflict")
            require(header["id"] == cast(Json, previous)["record"]["header"]["id"], 400, "request_invalid")
            updates[cast(str, header["kind"])] = {"record": record, "version": header["version"]}
        require(not stored, 409, "version_conflict")
        return updates

    def rotate(request: Request, body: Json) -> tuple[int, Any]:
        user = hub.users[cast(str, request.user)]
        try:
            updates = checked(user, body)
        except Refused as refused:
            return refused.status, {"error": refused.code}
        epoch = body["epoch"]
        logs = [org.log for org in hub.orgs.values() if org.log.state.member(user.id) is not None]
        platform = hub.platform_state()
        excluded = [device for log in logs for device in platform.excluded(log.state)]
        for wrap in body["wraps"]:
            check_cred_wrap(wrap, user.id, logs, epoch, excluded, require_active=True)
        user.cred_epoch = epoch
        for wrap in body["wraps"]:
            hub.wraps.append({**wrap, "wrapped_by": user.id, "created_at": now_ms_text()})
        for kind, row in updates.items():
            hub.credentials[(user.id, kind)] = row
        return 200, {}

    def delete(request: Request, body: Any, kind: str) -> tuple[int, Any]:
        if hub.credentials.pop((cast(str, request.user), kind), None) is None:
            return 404, {"error": "not_found"}
        return 200, {}

    hub.add_route("POST", r"/v1/me/credentials/rotate", rotate)
    hub.add_route("DELETE", r"/v1/me/credentials/(?P<kind>[a-z_]+)", delete)
