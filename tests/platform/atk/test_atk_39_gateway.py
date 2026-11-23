"""ATK-39: gateway abuse (07-testing.md section 2.3; 05-runner.md section 10.2; ADR-010).

With the session's valid token the session-side client tries other upstream paths (``/v1/complete``,
``/v1/models``, ``..``, ``//``, encoded slashes, OAuth, an absolute-form request target), models outside the
allow-list and OpenRouter routing fields, two different credentials, chunked bodies and header injection (``Host``,
cookies, proxy credentials, forwarding headers, ``Connection``-listed headers, a bare CR inside a header value,
provider options in the query). Only the two well-formed probe requests may reach the provider: on
``/v1/messages`` with at most ``?beta=true``, with the provisioned credential and without any of the injected
headers. ``test_atk_39[mode]`` is the real run (marker ``atk``).
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from agent_doubles.atk import catalog, extended
from tests.platform.atk.fakes import FakeHost, assert_attack, context, record, run, run_of


@pytest.mark.atk
@pytest.mark.parametrize("mode", catalog.MODES)
def test_atk_39(atk_results: dict[str, Any], mode: str) -> None:
    assert_attack(atk_results, "ATK-39", mode)


def test_the_abusive_requests() -> None:
    requests = {item["label"]: item for item in extended.abuse_39()}
    assert set(requests) == set(extended.EXPECTED_39)
    raw = base64.b64decode(requests["path-absolute"]["raw"])
    assert raw.startswith(b"POST http://evil.example/v1/messages HTTP/1.1\r\n")
    assert raw.count(b"\r\n\r\n") == 1
    cr = base64.b64decode(requests["header-cr"]["raw"])
    assert b"X-Note: a\rCookie: atk39-injected\r" in cr
    headers = {name.lower(): value for name, value in requests["headers"]["headers"]}
    assert headers[extended.PROBE_HEADER] == "headers"
    assert requests["headers"]["path"].endswith("&provider=atk39")
    assert requests["model-other"]["json"]["model"] != extended.MODEL_ID
    assert requests["two-credentials"]["headers"][-1] == ["x-api-key", "sk-" + "ant-atk39-other-credential"]


def test_the_client_sends_them_with_the_session_token() -> None:
    host = FakeHost(answers={"session-token": {"token": "t"}, "client": {"answers": [{"label": "x"}]}})
    ctx = context("ATK-39", host)
    run(extended.while_39(ctx))
    assert ctx.data["answers"] == [{"label": "x"}]


def good_answers() -> list[dict[str, Any]]:
    return [{"label": label, "status": codes[0]} for label, codes in extended.EXPECTED_39.items()]


def forwarded(label: str, **changes: Any) -> dict[str, Any]:
    headers = {extended.PROBE_HEADER: label, "authorization": "Bearer <redacted 32 chars>", "host": "127.0.0.1:7790"}
    exchange: dict[str, Any] = {"path": "/v1/messages", "query": "beta=true", "headers": headers}
    for name, value in changes.items():
        if name in ("path", "query"):
            exchange[name] = value
        else:
            headers[name.replace("_", "-")] = value
    return exchange


ROUTES = [record("guest.error", code="gateway_route") for _ in range(6)] + [
    record("guest.error", code="auth_failed"),
    record("usage", status=400, model="anthropic/claude-opus-4.5"),
]


def test_check_39_passes_on_the_expected_behaviour() -> None:
    ctx = context("ATK-39")
    ctx.data["answers"] = good_answers()
    exchanges = [forwarded("headers"), forwarded("header-cr", query=""), {"headers": None}]
    assert extended.check_39(ctx, run_of("ATK-39", exchanges=exchanges, journal=ROUTES)) == []


def test_check_39_names_each_failure() -> None:
    assert extended.check_39(context("ATK-39"), run_of("ATK-39")) == ["the session-side client did not run"]
    ctx = context("ATK-39")
    answers = [a for a in good_answers() if a["label"] != "chunked"]
    answers[0]["status"] = 200
    answers[-1]["status"] = 400  # header-cr refused: it must then not reach the provider
    ctx.data["answers"] = answers
    exchanges = [
        forwarded("headers", path="/v1/complete", query="beta=true&api_key=x",
                  authorization="Bearer <redacted 43 chars>", cookie="c", host="evil.example"),
        forwarded("header-cr"),
    ]  # fmt: skip
    found = extended.check_39(ctx, run_of("ATK-39", exchanges=exchanges, journal=ROUTES[:2]))
    assert found == [
        "path-complete answered 200, expected (404,)",
        "chunked answered nothing, expected (400,)",
        "requests that reached the provider: ['header-cr', 'headers'], answered 200: ['headers']",
        "headers: forwarded to /v1/complete?beta=true&api_key=x",
        "headers: the provider got another credential: Bearer <redacted 43 chars>",
        "headers: header cookie reached the provider",
        "headers: the Host header reached the provider",
        "2 gateway_route record(s) for the 6 other upstream paths",
        "the request with two credentials was not journaled",
        "the model outside the allow-list was not journaled (usage with status 400)",
    ]
