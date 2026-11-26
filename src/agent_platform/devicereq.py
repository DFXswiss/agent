"""Device requests: the user asks one execution device to act (11-agent-access.md section 8.4, 03-api.md section 4.10,
04-crypto.md section 4.5; twin of `web/src/core/requests/client.ts`, `requests/bodies.ts`, `github/connection.ts`
and `credentials/login.ts`). The hub relays opaque envelopes only; everything is checked here.

- The device's keys come from the verified membership logs (an active device that may read the user's `CK`,
  decertified platform devices excluded), never from the hub; anything else is `device_not_assigned`.
- Without a device (a fresh private org) the tool assigns one to the default workgroup first, as the web does: a
  missing connection is started, not explained.
- A request is sealed with HPKE to the device and signed by the actor (`POST /v1/device-requests`): the agent with
  its own key under its own ID (`header.user`; the device acts for the agent's user, ADR-082), or the user's
  identity. A `github.connect` for a company account also names its organization in plaintext (`org_id`, the hub's
  admin check, ADR-078 section 1).
- Every response of `GET /v1/device-requests/{id}` must be signed by the request's device, name this request, the
  actor, device, kind and its `seq`, open with the actor's key and hold a body the contracts allow for the kind;
  anything else is `unverifiable` and never shown as content.
- The tool waits (polling) up to `--wait` seconds for the next step a person would act on, then returns; a later call
  with `--request` continues. A device code or authorization link is opened in the user's browser.
- `github.pull_status` (KOR-48, ADR-084 section 2) reads the fresh state of pull requests; its answer is checked by
  `pullstate.valid_response`, its failures add `github_not_connected` and `forbidden` (`pulls` sends it).
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from typing import Any, cast

from agent_platform.account import Account, unverified
from agent_platform.credentials import EMAIL_RE, LOGIN_RE, SOURCES, valid_github, valid_scopes
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_platform.orgs import device_assign, now_text
from agent_platform.pairing import open_browser
from agent_platform.pullstate import valid_response
from agent_platform.reader import excluded
from agent_runner.crypto.encoding import b64u_decode, body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.state import DEVICE
from agent_runner.crypto.log.wraps import cred_readers
from agent_runner.crypto.request import ENC_ALG, open_response, seal_request, verify_response

KINDS = ("github.connect", "claude.login", "credential.check", "mailbox.check")
ENDED = frozenset({"answered", "failed", "expired"})
FAILURE_CODES = frozenset(
    {
        "timeout",
        "busy",
        "credential_missing",
        "code_invalid",
        "login_cli_failed",
        "token_invalid",
        "upstream_error",
        "upstream_unreachable",
        "device_shutdown",
        "token_refused",
        "scope_missing",
        "sso_required",
        "github_not_connected",
        "forbidden",
    }
)
REQUEST_FAILED = "request_failed"
SIGNING_OUTCOMES = ("registered", "scope_missing", "refused")
PROGRESS_STAGES = frozenset(
    {
        "browser_started",
        "email_entered",
        "magic_link_received",
        "authorized",
        "mail_code_received",
        "registered",
        "verified",
    }
)
FALLBACK_REASONS = frozenset(
    {
        "disabled",
        "no_sandbox",
        "human_check",
        "consent_missing",
        "account_missing",
        "unknown_page",
        "mail_timeout",
        "browser_failed",
    }
)
"""Why a login's automation gave up (`authorize.reason`, KOR-73)."""
IMAGE_DATA_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
IMAGE_DATA_MAX = 136536
MAX_FRAMES = 100
MAX_SIDE = 4096
URL_RE = re.compile(r"https://[!-~]{1,2040}|http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(/[!-~]{0,2000})?")
USER_CODE_RE = re.compile(r"[A-Z0-9-]{4,16}")
CODE_RE = re.compile(r"[!-~]{1,512}")
POLL_S = 2.0
WAIT_MAX_S = 600

Keys = tuple[bytes, bytes]


def _keys(value: dict[str, Any], required: set[str], optional: frozenset[str] = frozenset()) -> bool:
    return required <= set(value) <= required | optional


def _match(value: object, pattern: re.Pattern[str], most: int = 2048) -> bool:
    return isinstance(value, str) and len(value) <= most and pattern.fullmatch(value) is not None


def _github(body: dict[str, Any]) -> bool:
    if body.get("step") == "device_code":
        seconds = body.get("expires_in")
        return (
            _keys(body, {"step", "user_code", "verification_uri", "expires_in"})
            and _match(body.get("user_code"), USER_CODE_RE)
            and _match(body.get("verification_uri"), URL_RE)
            and isinstance(seconds, int)
            and not isinstance(seconds, bool)
            and 1 <= seconds <= 3600
        )
    # with an existing token (KOR-25) `done` names the token's scopes and the source; with GitHub identities (KOR-47)
    # the record, the outcome of `signing_key` and, for a company account, the credential body for the admin's client
    return (
        body.get("step") == "done"
        and _keys(body, {"step", "login"}, frozenset({"scopes", "source", "identity_id", "signing", "body"}))
        and _match(body.get("login"), LOGIN_RE)
        and ("scopes" not in body or valid_scopes(body["scopes"]))
        and body.get("source", SOURCES[0]) in SOURCES
        and ("identity_id" not in body or is_uuid7(body["identity_id"]))
        and body.get("signing", SIGNING_OUTCOMES[0]) in SIGNING_OUTCOMES
        and ("body" not in body or (isinstance(body["body"], dict) and valid_github(body["body"])))
    )


def _count(value: object, most: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= most


def _image(value: object) -> bool:
    """The picture of a check for humans (`verify.image`, KOR-73): JPEG in base64 with its size."""
    return (
        isinstance(value, dict)
        and _keys(value, {"width", "height", "data"})
        and _count(value["width"], MAX_SIDE)
        and _count(value["height"], MAX_SIDE)
        and isinstance(value["data"], str)
        and 4 <= len(value["data"]) <= IMAGE_DATA_MAX
        and IMAGE_DATA_RE.fullmatch(value["data"]) is not None
    )


def _login(body: dict[str, Any]) -> bool:
    step = body.get("step")
    if step == "authorize":
        return (
            _keys(body, {"step", "url"}, frozenset({"automation", "reason"}))
            and _match(body.get("url"), URL_RE)
            and body.get("automation", "failed") == "failed"
            and body.get("reason", "human_check") in FALLBACK_REASONS
        )
    if step == "verify":
        return _keys(body, {"step", "frame", "image"}) and _count(body["frame"], MAX_FRAMES) and _image(body["image"])
    if step == "progress":
        return _keys(body, {"step", "stage"}) and body.get("stage") in PROGRESS_STAGES
    email_valid = "email" not in body or _match(body["email"], EMAIL_RE, 254)
    return step == "done" and _keys(body, {"step"}, frozenset({"email"})) and email_valid


def _check(body: dict[str, Any]) -> bool:
    return body.get("step") == "done" and _keys(body, {"step", "valid"}) and isinstance(body.get("valid"), bool)


CHECKS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "github.connect": _github,
    "claude.login": _login,
    "credential.check": _check,
    "mailbox.check": _check,
    "github.pull_status": valid_response,
}


def response_body(kind: str, body: object) -> dict[str, Any] | None:
    """The response body of a device for `kind`, or None when the contracts do not allow it."""
    if not isinstance(body, dict):
        return None
    if body.get("step") == "failed":
        return body if _keys(body, {"step", "code"}) and body.get("code") in FAILURE_CODES else None
    return body if CHECKS[kind](body) else None


# --- devices ---


def device_keys(account: Account, device: str) -> Keys | None:
    """`(kem, sig)` of a device the user may address, from the verified logs (module docstring)."""
    for item in account.me().get("orgs", []):
        try:
            log = account.org_log(str(item["org_id"]))
            skip = excluded(account, log)
        except (PlatformError, KeyError, TypeError):
            continue
        found = log.state.member(device)
        if found is None or found.kind != DEVICE:
            continue
        if device in cred_readers([log.state], account.user_id, skip):
            return b64u_decode(found.kem, 32), b64u_decode(found.sig, 32)
    return None


def _home(account: Account) -> str | None:
    """The default workgroup of the user's private org (else of the first org)."""
    orgs = [item for item in account.me().get("orgs", []) if isinstance(item, dict)]
    orgs.sort(key=lambda item: item.get("private") is not True)
    return str(orgs[0]["default_wg"]) if orgs else None


def eligible(account: Account) -> list[tuple[dict[str, Any], Keys]]:
    """The devices of `GET /v1/me/devices` whose keys the verified logs confirm, online first."""
    found: dict[str, tuple[dict[str, Any], Keys]] = {}
    for row in rows_of(account.call("GET", "/v1/me/devices"), "devices"):
        device = row.get("device_id")
        keys = device_keys(account, device) if isinstance(device, str) else None
        if keys is None:
            continue
        known = found.get(str(device))
        online = row.get("online") is True or (known is not None and known[0]["online"])
        found[str(device)] = ({"device_id": device, "online": online}, keys)
    return sorted(found.values(), key=lambda item: (not item[0]["online"], item[0]["device_id"]))


def choose_device(account: Account, device: str | None) -> tuple[str, Keys]:
    """The named device, else the first eligible one; without any, one is assigned to the default workgroup."""
    if device is not None:
        keys = device_keys(account, uuid_argument(device))
        if keys is None:
            raise PlatformError("no_device", reason="device_not_assigned")
        return device, keys
    devices = eligible(account)
    home = _home(account)
    if not devices and home is not None:
        print("agent-platform: no device yet; assigning one to the default workgroup", file=sys.stderr)
        device_assign(account, home)
        devices = eligible(account)
    if not devices:
        raise PlatformError("no_device")
    row, keys = devices[0]
    return str(row["device_id"]), keys


# --- requests ---


def submit(
    account: Account,
    target: tuple[str, Keys],
    kind: str,
    body: dict[str, Any],
    follows: str = "",
    *,
    org: str | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    """Seal and post a request to the device `(id, keys)` (with `org`: the plaintext `org_id` of a company account;
    with `slot`: the plaintext subject of a root, the subscription of a `claude.login`, so logins of different
    subscriptions run side by side, ADR-098 section 2); the prepared request `{id, kind, device, keys}`."""
    device, keys = target
    header = {
        "v": 1,
        "id": uuid7(),
        "user": account.actor,
        "device": device,
        "kind": kind,
        "follows": follows,
        "ts": now_text(),
        "enc_alg": ENC_ALG,
    }
    envelope = seal_request(header, keys[0], body_json(body), account.signer.sig)
    plain: dict[str, Any] = {} if org is None else {"org_id": org}
    if slot is not None:
        plain["slot"] = slot
    answer = account.call("POST", "/v1/device-requests", {"envelope": envelope, **plain})
    if answer.get("request_id") != header["id"] or not isinstance(answer.get("state"), str):
        raise unverified("response_invalid")
    return {"id": header["id"], "kind": kind, "device": device, "keys": keys}


def _open(account: Account, prepared: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("seq")
    seq = raw if isinstance(raw, int) else 0
    sig = prepared["keys"][1]
    try:
        envelope = item["envelope"]
        header = verify_response(envelope, sig)
        expected = {
            "in_reply_to": prepared["id"],
            "user": account.actor,
            "device": prepared["device"],
            "kind": prepared["kind"],
            "seq": seq,
        }
        if seq < 1 or any(header[name] != value for name, value in expected.items()):
            return {"seq": seq, "status": "unverifiable", "code": "header_invalid"}
        body = response_body(prepared["kind"], parse_json(open_response(account.signer.kem, envelope, sig)))
        if body is None:
            return {"seq": seq, "status": "unverifiable", "code": "params_invalid"}
        return {"seq": seq, "status": "verified", "final": header["final"] is True, "body": body}
    except CryptoError as error:
        return {"seq": seq, "status": "unverifiable", "code": error.code}
    except (KeyError, TypeError):
        return {"seq": seq, "status": "unverifiable", "code": "header_invalid"}


def read(account: Account, prepared: dict[str, Any]) -> dict[str, Any]:
    """The request's state and its responses, every one checked and opened (module docstring)."""
    view = account.call("GET", f"/v1/device-requests/{segment(prepared['id'])}")
    if (
        view.get("request_id") != prepared["id"]
        or view.get("kind") != prepared["kind"]
        or view.get("device_id") != prepared["device"]
        or not isinstance(view.get("state"), str)
    ):
        raise unverified("response_invalid")
    responses = [_open(account, prepared, item) for item in rows_of(view, "responses")]
    responses.sort(key=lambda item: int(item["seq"]))
    return {"state": view["state"], "expires_at": view.get("expires_at"), "responses": responses}


def resume(account: Account, request: str, kinds: tuple[str, ...]) -> dict[str, Any]:
    """The prepared request of an earlier call, from the hub's view and the verified logs."""
    view = account.call("GET", f"/v1/device-requests/{segment(uuid_argument(request))}")
    kind, device = view.get("kind"), view.get("device_id")
    if kind not in kinds or not isinstance(device, str):
        raise PlatformError("invalid_argument", reason="request_kind")
    keys = device_keys(account, device)
    if keys is None:
        raise PlatformError("no_device", reason="device_not_assigned")
    return {"id": request, "kind": kind, "device": device, "keys": keys}


def latest(view: dict[str, Any]) -> dict[str, Any] | None:
    verified = [item for item in view["responses"] if item["status"] == "verified"]
    return cast(dict[str, Any], verified[-1]["body"]) if verified else None


def progress(view: dict[str, Any]) -> dict[str, Any]:
    """The flow's state: the newest verified response decides; a hub state that ended the request without a final
    answer is `failed` with `timeout` (expired) or `request_failed`."""
    body = latest(view)
    if body is not None and body["step"] in ("done", "failed"):
        return body
    if view["state"] == "expired":
        return {"step": "failed", "code": "timeout"}
    if view["state"] == "failed":
        return {"step": "failed", "code": REQUEST_FAILED}
    return body if body is not None else {"step": "waiting"}


def wait(account: Account, prepared: dict[str, Any], seconds: int, steps: frozenset[str]) -> dict[str, Any]:
    """Read the request until its progress reaches one of `steps` or `seconds` passed; the last progress."""
    if not 0 <= seconds <= WAIT_MAX_S:
        raise PlatformError("invalid_argument")
    deadline = time.monotonic() + seconds
    while True:
        found = progress(read(account, prepared))
        if found["step"] in steps or time.monotonic() >= deadline:
            return found
        time.sleep(POLL_S)


def finish(prepared: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """The result of a flow step; a failure is `device_request_failed` with its code."""
    if found["step"] == "failed":
        raise PlatformError("device_request_failed", failure=found["code"], request_id=prepared["id"])
    return {"request_id": prepared["id"], "device_id": prepared["device"], **found}


def show_in_browser(environ: Any, url: str, what: str) -> None:
    print(f"agent-platform: {what}: {url}", file=sys.stderr)
    open_browser(environ, url, f"agent-platform: open {url}")
