"""HMAC device tokens. No expiry; revocation is a database row."""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class DeviceToken:
    login: str
    device_id: str
    token_id: str


def issue(secret: str, login: str, device_id: str, token_id: str) -> str:
    payload = json.dumps(
        {"login": login.lower(), "device": device_id, "tid": token_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    body = urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def parse(secret: str, token: str) -> DeviceToken:
    if "." not in token:
        raise TokenError("device token is malformed")
    body, sig = token.rsplit(".", 1)
    expect = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        raise TokenError("device token signature is invalid")
    try:
        payload = json.loads(urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("device token payload is not JSON") from exc
    for key in ("login", "device", "tid"):
        if key not in payload or not isinstance(payload[key], str) or payload[key] == "":
            raise TokenError(f"device token missing {key}")
    return DeviceToken(login=payload["login"].lower(), device_id=payload["device"], token_id=payload["tid"])
