"""U-DBL-github: the device flow (05-runner.md §11.4, ADR-015) through the real HTTP server."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import pytest

from agent_doubles.github.deviceflow import DEFAULT_CLIENT_ID, GRANT_TYPE, DeviceFlow, DeviceFlowError

JSON = {"accept": "application/json"}


def code(gh: Any, client_id: str = DEFAULT_CLIENT_ID, **extra: str) -> Any:
    return gh.client.request("POST", "/login/device/code", {"client_id": client_id, **extra}, JSON)


def poll(gh: Any, device_code: str, client_id: str = DEFAULT_CLIENT_ID, grant: str = GRANT_TYPE) -> dict[str, Any]:
    body = {"client_id": client_id, "device_code": device_code, "grant_type": grant}
    response = gh.client.request("POST", "/login/oauth/access_token", body, JSON)
    assert response.status == 200
    result: dict[str, Any] = response.json()
    return result


def device(gh: Any, **body: Any) -> Any:
    return gh.control("POST", "device", body)


def test_happy_path_with_pending_and_slow_down(github: Any) -> None:
    started = code(github, scope="")
    assert started.status == 200
    grant = started.json()
    assert sorted(grant) == ["device_code", "expires_in", "interval", "user_code", "verification_uri"]
    assert grant["verification_uri"] == f"{github.double.url}/login/device"
    assert (grant["expires_in"], grant["interval"]) == (900, 5)
    assert len(grant["user_code"]) == 9
    assert grant["user_code"][4] == "-"
    assert poll(github, grant["device_code"])["error"] == "authorization_pending"
    github.clock.now += 1
    slow = poll(github, grant["device_code"])
    assert (slow["error"], slow["interval"]) == ("slow_down", 10)
    github.clock.now += 10
    assert poll(github, grant["device_code"])["error"] == "authorization_pending"
    assert (
        device(github, user_code=grant["user_code"], action="authorize", login="OCTO-USER").json()["login"]
        == "octo-user"
    )
    github.clock.now += 10
    token = poll(github, grant["device_code"])
    assert token["token_type"] == "bearer"
    assert token["scope"] == ""
    assert token["access_token"].startswith("ghu_")
    assert len(token["access_token"]) == 40
    assert github.get("/user", token=token["access_token"])["login"] == "octo-user"
    # A device code yields its token once.
    github.clock.now += 10
    assert poll(github, grant["device_code"])["error"] == "incorrect_device_code"
    assert github.control("GET", "device").json() == [
        {
            "user_code": grant["user_code"],
            "login": "octo-user",
            "denied": False,
            "expired": False,
            "used": True,
            "interval": 10,
        }
    ]
    assert github.state()["ok"] is True


def test_denied_expired_and_forced_slow_down(github: Any) -> None:
    denied, expired, slow, timed = (code(github).json() for _ in range(4))
    assert len({denied["user_code"], expired["user_code"], slow["user_code"], timed["user_code"]}) == 4
    device(github, user_code=denied["user_code"], action="deny")
    assert poll(github, denied["device_code"])["error"] == "access_denied"
    device(github, user_code=expired["user_code"], action="expire")
    assert poll(github, expired["device_code"])["error"] == "expired_token"
    device(github, user_code=slow["user_code"], action="slow_down", count=2)
    first = poll(github, slow["device_code"])
    github.clock.now += 100
    second = poll(github, slow["device_code"])
    github.clock.now += 100
    assert [first["interval"], second["interval"]] == [10, 15]
    assert poll(github, slow["device_code"])["error"] == "authorization_pending"
    github.clock.now += 900
    assert poll(github, timed["device_code"])["error"] == "expired_token"
    assert poll(github, timed["device_code"])["error"] == "expired_token"


def test_wrong_client_grant_and_code(github: Any) -> None:
    refused = code(github, client_id="other")
    assert (refused.status, refused.json()) == (404, {"error": "Not Found"})
    grant = code(github).json()
    assert poll(github, grant["device_code"], client_id="other")["error"] == "incorrect_client_credentials"
    assert poll(github, grant["device_code"], grant="password")["error"] == "unsupported_grant_type"
    assert poll(github, "unknown")["error"] == "incorrect_device_code"
    errors = poll(github, "unknown")
    assert sorted(errors) == ["error", "error_description", "error_uri"]


def test_form_encoding_and_seeded_settings(start_github: Any, seed: Any) -> None:
    data = seed()
    data["device_flow"] = {"client_id": "Iv1.custom", "interval": 0, "expires_in": 60}
    gh = start_github(data)
    form = b"client_id=Iv1.custom&scope=repo"
    started = gh.client.request(
        "POST", "/login/device/code", form, {"content-type": "application/x-www-form-urlencoded"}
    )
    assert started.headers["content-type"] == "application/x-www-form-urlencoded; charset=utf-8"
    fields = dict(parse_qsl(started.body.decode()))
    assert (fields["interval"], fields["expires_in"]) == ("0", "60")
    device(gh, user_code=fields["user_code"], action="authorize", login="octo-user")
    query = f"client_id=Iv1.custom&device_code={fields['device_code']}&grant_type={GRANT_TYPE}"
    token = gh.client.request("POST", f"/login/oauth/access_token?{query}", b"")
    answer = dict(parse_qsl(token.body.decode()))
    assert (answer["token_type"], answer["scope"]) == ("bearer", "repo")
    bad = gh.client.request("POST", "/login/device/code", b"[", {"content-type": "application/json"})
    assert bad.status == 400
    page = gh.client.request("GET", "/login/device")
    assert page.status == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert gh.state()["ok"] is True


def test_disabled_device_flow(start_github: Any) -> None:
    gh = start_github({"device_flow": {"enabled": False}})
    refused = code(gh)
    assert (refused.status, refused.json()["error"]) == (400, "device_flow_disabled")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "body must be an object"),
        ({"action": "explode"}, "action must be one of"),
        ({"action": "deny", "user_code": "NONE-0000"}, "unknown user code"),
        ({"action": "authorize", "login": "nobody"}, "unknown login"),
        ({"action": "slow_down", "count": 0}, "count must be a positive integer"),
        ({"action": "slow_down", "count": True}, "count must be a positive integer"),
    ],
)
def test_control_refusals(github: Any, body: Any, message: str) -> None:
    grant = code(github).json()
    if isinstance(body, dict) and "user_code" not in body:
        body["user_code"] = grant["user_code"]
    response = github.control("POST", "device", body)
    assert response.status == 400
    assert message in response.json()["error"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "device_flow must be an object"),
        ({"other": 1}, "unknown keys"),
        ({"client_id": ""}, "client_id must be a non-empty string"),
        ({"interval": -1}, "interval must be a non-negative integer"),
        ({"expires_in": True}, "expires_in must be a non-negative integer"),
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"oauth_client_id": ""}, "oauth_client_id must be a non-empty string"),
    ],
)
def test_configuration_refusals(raw: Any, message: str) -> None:
    flow = DeviceFlow(lambda: 0.0, lambda login, kind, scope: "t", lambda: "http://127.0.0.1:1")
    with pytest.raises(DeviceFlowError, match=message):
        flow.configure(raw)
    assert flow.client_id == DEFAULT_CLIENT_ID


def test_reset_restores_defaults(github: Any) -> None:
    github.double.seed({"device_flow": {"client_id": "Iv1.custom", "interval": 1}})
    code(github, client_id="Iv1.custom")
    github.control("POST", "reset")
    assert github.control("GET", "device").json() == []
    assert code(github).json()["interval"] == 5


def test_reset_restarts_the_codes_and_vnd_accept_gets_json(github: Any) -> None:
    first = code(github).json()
    github.control("POST", "reset")
    again = github.client.request(
        "POST", "/login/device/code", {"client_id": DEFAULT_CLIENT_ID}, {"accept": "application/vnd.github+json"}
    )
    assert again.headers["content-type"] == "application/json"
    assert again.json()["user_code"] == first["user_code"]
