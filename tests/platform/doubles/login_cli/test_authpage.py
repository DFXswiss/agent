"""U-DBL-login: the auth-page double over HTTPS (HTTP level): flow, junk delivery, errors, faults, control routes."""

from __future__ import annotations

import contextlib
import http.client
import json
import smtplib
import socket
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_doubles.login_cli import codes
from agent_doubles.login_cli.authpage import AuthPageDouble, Scenario, error_page, page, redact_body, redact_query
from agent_doubles.login_cli.cli import authorize_url

from .conftest import USER, FakeClock, Login


def _token_request(login: Login, body: Any) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        login.auth.page_url + codes.TOKEN_PATH,
        data=body if isinstance(body, bytes) else json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    context = login.ca.client_context() if login.ca else None
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _exchange_body(login: Login, pasted: str, seed: str = "s") -> dict[str, Any]:
    code, _, state = pasted.partition("#")
    return {
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "client_id": codes.CLIENT_ID,
        "redirect_uri": login.auth.redirect_uri,
        "code_verifier": codes.pkce(seed)[0],
    }


def test_full_flow_with_the_link_in_junk(start_login: Callable[..., Login]) -> None:
    login = start_login(scenario=Scenario(junk=True, expect=("mail_sent", "signed_in", "authorized", "token")))
    assert login.auth.page_url.startswith("https://127.0.0.1:")
    pasted = login.code_for(authorize_url(login.auth.page_url, "s"))
    code, _, state = pasted.partition("#")
    assert code == codes.auth_code(codes.DEFAULT_SEED, 1)
    assert state == codes.oauth_state("s")
    assert login.mail.store.counts()[USER] == {"INBOX": 0, "Junk": 1}
    (mail,) = login.mail.store.summary(USER)
    assert mail["sender"] == "no-reply@auth.double.test"
    assert mail["subject"] == "Secure link to log in to Claude"
    assert login.auth.state()["done"] is False
    status, answer = _token_request(login, _exchange_body(login, pasted))
    assert status == 200
    assert answer["access_token"] == codes.access_token(codes.DEFAULT_SEED, 1)
    assert answer["account"]["email_address"] == USER
    assert (answer["token_type"], answer["expires_in"], answer["scope"]) == ("Bearer", 365 * 86_400, "user:inference")
    state_doc = login.auth.state()
    assert state_doc["milestones"] == [
        "email_page",
        "mail_sent",
        "signed_in",
        "authorize_page",
        "authorized",
        "code_page",
        "token",
    ]
    assert (state_doc["ok"], state_doc["failures"]) == (True, [])
    # The code is single use.
    assert _token_request(login, _exchange_body(login, pasted)) == (
        400,
        {"error": "invalid_grant", "error_description": "Invalid 'code' in request."},
    )
    journal = login.auth.journal.records()
    cookies = [r["headers"]["cookie"] for r in journal if "cookie" in r["headers"]]
    assert cookies
    assert all(c.startswith("<redacted") for c in cookies)


def test_inbox_delivery_without_junk(login: Login) -> None:
    login.code_for(authorize_url(login.auth.page_url, "s"))
    assert login.mail.store.counts()[USER] == {"INBOX": 1, "Junk": 0}


@pytest.mark.parametrize(
    ("change", "verifier_seed"),
    [({"code": "wrong"}, "s"), ({"state": "other"}, "s"), ({"redirect_uri": "https://x.test/cb"}, "s"), ({}, "t")],
)
def test_wrong_codes_are_refused_without_failure(login: Login, change: dict[str, str], verifier_seed: str) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s"))
    body = {**_exchange_body(login, pasted, verifier_seed), **change}
    assert _token_request(login, body)[0] == 400
    assert login.auth.state()["failures"] == []


def test_codes_and_links_expire(start_login: Callable[..., Login]) -> None:
    clock = FakeClock()
    login = start_login(clock=clock)
    pasted = login.code_for(authorize_url(login.auth.page_url, "s"))
    clock.now += 601
    assert _token_request(login, _exchange_body(login, pasted))[0] == 400
    browser = login.browser()
    browser.open(authorize_url(login.auth.page_url, "s"))
    page = browser.open(login.auth.page_url + "/login", {"email": USER, "next": "/oauth/authorize?x"})
    assert page.kind == "check-email"
    token = codes.magic_token(codes.DEFAULT_SEED, 2)
    clock.now += 901
    page = browser.open(login.auth.page_url + "/magic-link/verify", {"token": token})
    assert (page.status, page.kind, page.forms.error) == (400, "error", "invalid_magic_link")
    assert login.auth.state()["failures"] == []


@pytest.mark.parametrize(
    "query",
    [
        {"client_id": "other"},
        {"response_type": "token"},
        {"code_challenge_method": "plain"},
        {"redirect_uri": "https://evil.test/cb"},
        {"scope": ""},
        {"code_challenge": "short"},
    ],
)
def test_invalid_authorize_requests_are_failures(login: Login, query: dict[str, str]) -> None:
    url = authorize_url(login.auth.page_url, "s")
    parts = urlsplit(url)
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    params.update(query)
    page = login.browser().open(f"{login.auth.page_url}{parts.path}?{urllib.parse.urlencode(params)}")
    assert (page.status, page.kind, page.forms.error) == (400, "error", "invalid_request")
    assert len(login.auth.state()["failures"]) == 1


def test_page_errors(login: Login) -> None:
    base = login.auth.page_url
    browser = login.browser()
    page = browser.open(base + "/login", {"email": "not-an-address", "next": "/oauth/authorize?x"})
    assert page.forms.error == "invalid_request"
    page = browser.open(base + "/login", {"email": USER, "next": "https://evil.test/"})
    assert page.forms.error == "invalid_request"
    page = browser.open(base + "/magic-link/verify", {"token": "unknown"})
    assert (page.status, page.forms.error) == (400, "invalid_magic_link")
    page = browser.open(base + "/oauth/authorize", {"decision": "authorize"})
    assert (page.status, page.forms.error) == (401, "not_signed_in")
    page = browser.open(base + "/oauth/code/callback?code=x")
    assert page.forms.error == "invalid_request"
    page = browser.open(base + "/elsewhere")
    assert (page.status, page.forms.error) == (404, "not_found")
    assert browser.open(base + "/favicon.ico").status == 404
    page = browser.open(base + "/magic-link")
    assert page.kind == "magic-link"
    failures = login.auth.state()["failures"]
    assert len(failures) == 4
    assert "GET /elsewhere: not_found: unknown page" in failures


def test_decline_and_invalid_decision(login: Login) -> None:
    browser = login.browser()
    url = authorize_url(login.auth.page_url, "s")
    login.code_for(url, browser)
    page = browser.open(url)
    assert page.kind == "authorize"
    fields = page.forms.fields
    declined = browser.open(login.auth.page_url + "/oauth/authorize", {**fields, "decision": "decline"})
    assert (declined.status, declined.forms.error) == (400, "access_denied")
    assert "declined" in login.auth.state()["milestones"]
    odd = browser.open(login.auth.page_url + "/oauth/authorize", {**fields, "decision": "maybe"})
    assert (odd.status, odd.forms.error) == (400, "invalid_request")
    tampered = browser.open(
        login.auth.page_url + "/oauth/authorize", {**fields, "client_id": "x", "decision": "authorize"}
    )
    assert tampered.forms.error == "invalid_request"
    assert len(login.auth.state()["failures"]) == 2


def test_bad_session_cookies_are_no_session(login: Login) -> None:
    assert login.auth.session_email(None) is None
    assert login.auth.session_email("double_session=unknown") is None
    assert login.auth.session_email("other=1") is None
    assert login.auth.session_email('bad"cookie=;;') is None


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (b"not json", "body is not a JSON object"),
        ([], "body is not a JSON object"),
        ({"grant_type": "authorization_code"}, "missing code"),
        (
            {
                "grant_type": "password",
                "code": "c",
                "state": "s",
                "client_id": "x",
                "redirect_uri": "r",
                "code_verifier": "v",
            },
            "unsupported grant_type",
        ),
        (
            {
                "grant_type": "authorization_code",
                "code": "c",
                "state": "s",
                "client_id": "x",
                "redirect_uri": "r",
                "code_verifier": "v",
            },
            "unknown client_id",
        ),
    ],
)
def test_malformed_token_requests_are_failures(login: Login, body: Any, problem: str) -> None:
    assert _token_request(login, body) == (400, {"error": "invalid_request", "error_description": problem})
    assert login.auth.state()["failures"] == [f"POST /v1/oauth/token: {problem}"]


def test_a_refused_token_request_is_recorded_before_the_answer(login: Login, monkeypatch: pytest.MonkeyPatch) -> None:
    # a slow recording (a busy device) must still be visible once the client has the answer
    record = login.auth.fail

    def slow(text: str) -> None:
        time.sleep(0.3)
        record(text)

    monkeypatch.setattr(login.auth, "fail", slow)
    assert _token_request(login, b"not json")[0] == 400
    assert len(login.auth.state()["failures"]) == 1


@pytest.mark.parametrize(
    ("fault", "where", "kind"),
    [
        ("email_page", "login", "challenge"),
        ("authorize_page", "authorize", "error"),
        ("code_page", "callback", "error"),
    ],
)
def test_page_faults(start_login: Callable[..., Login], fault: str, where: str, kind: str) -> None:
    login = start_login(scenario=Scenario(fault=fault))
    browser = login.browser()
    url = authorize_url(login.auth.page_url, "s")
    page = browser.open(url)
    page = browser.open(login.auth.page_url + "/login", {"email": USER, **page.forms.fields})
    if where == "login":
        assert page.kind == kind
        assert login.mail.store.counts()[USER] == {"INBOX": 0, "Junk": 0}
        return
    token = login.magic_link(0)
    assert token is not None
    page = browser.open(login.auth.page_url + "/magic-link/verify", {"token": token.split("#")[1]})
    if where == "authorize":
        assert (page.status, page.kind) == (500, kind)
        return
    page = browser.open(login.auth.page_url + "/oauth/authorize", {**page.forms.fields, "decision": "authorize"})
    assert (page.status, page.kind) == (500, kind)
    assert login.auth.state()["failures"] == []


def test_no_mail_and_token_faults(start_login: Callable[..., Login]) -> None:
    login = start_login(scenario=Scenario(fault="no_mail"))
    browser = login.browser()
    page = browser.open(authorize_url(login.auth.page_url, "s"))
    page = browser.open(login.auth.page_url + "/login", {"email": USER, **page.forms.fields})
    assert page.kind == "check-email"
    assert login.mail.store.counts()[USER] == {"INBOX": 0, "Junk": 0}
    login.auth.set_scenario(Scenario(fault="token"))
    assert _token_request(login, _exchange_body(login, "c#" + codes.oauth_state("s")))[0] == 500


def test_mail_failures_are_failures(start_login: Callable[..., Login], login_ca: Any) -> None:
    login = start_login()
    login.auth.smtp = None
    browser = login.browser()
    page = browser.open(authorize_url(login.auth.page_url, "s"))
    page = browser.open(login.auth.page_url + "/login", {"email": USER, **page.forms.fields})
    assert (page.status, page.forms.error) == (502, "mail_failed")
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    login.auth.smtp = ("127.0.0.1", port)
    page = browser.open(login.auth.page_url + "/login", {"email": USER, "next": "/oauth/authorize?x"})
    assert page.forms.error == "mail_failed"
    login.auth.smtp = (login.mail.smtp.host, login.mail.smtp.port)
    page = browser.open(
        login.auth.page_url + "/login", {"email": "stranger@example.test", "next": "/oauth/authorize?x"}
    )
    assert page.forms.error == "mail_failed"
    assert len(login.auth.state()["failures"]) == 3
    assert smtplib.SMTPRecipientsRefused.__name__ in page.text


def test_plain_http_pages(start_login: Callable[..., Login]) -> None:
    login = start_login(tls=False)
    assert login.auth.page_url.startswith("http://")
    pasted = login.code_for(authorize_url(login.auth.page_url, "s"))
    assert "#" in pasted
    # The legacy poll ignores links that are not https://, so mailbox automation needs the TLS pages.
    assert login.magic_link(0) is None
    assert (login.stored_link() or "").startswith(login.auth.page_url + "/magic-link#")
    cookie = next(r for r in login.auth.journal.records() if r["status"] == 303)
    assert cookie["path"] == "/magic-link/verify"


def test_secure_cookie_over_tls(login: Login) -> None:
    browser = login.browser()
    login.code_for(authorize_url(login.auth.page_url, "s"), browser)
    (cookie,) = list(browser.jar)
    assert (cookie.name, cookie.secure, cookie.path) == ("double_session", True, "/")


def test_failed_tls_handshake_ends_only_that_connection(login: Login) -> None:
    port = int(login.auth.page_url.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        raw.sendall(b"GET / HTTP/1.1\r\n\r\n")
        with contextlib.suppress(ConnectionResetError):
            assert raw.recv(100) == b""
    assert login.browser().open(authorize_url(login.auth.page_url, "s")).kind == "email"


def test_control_routes(login: Login) -> None:
    info = login.control.request("GET", "/_double/info").json()
    assert info == login.auth.info()
    assert info["client_id"] == codes.CLIENT_ID
    assert info["redirect_uri"] == login.auth.page_url + "/oauth/code/callback"
    assert info["ca_file"] == str(login.ca.ca_file)  # type: ignore[union-attr]
    login.browser().open(login.auth.page_url + "/nowhere")
    assert len(login.control.journal()) == 1
    state = login.control.scenario({"junk": True, "fault": "no_mail", "expect": ["token"], "seed": "x"}).json()
    assert state["scenario"] == {"junk": True, "fault": "no_mail", "expect": ["token"], "seed": "x"}
    assert (state["failures"], state["requests"], state["done"], state["ok"]) == ([], 0, False, False)
    assert login.control.journal() == []
    login.browser().open(login.auth.page_url + "/nowhere")
    state = login.control.reset().json()
    assert (state["failures"], state["scenario"]["seed"]) == ([], "x")
    for bad, message in (
        ({"other": 1}, "unknown scenario keys"),
        ({"junk": "yes"}, "junk must be a boolean"),
        ({"fault": "boom"}, "fault must be one of"),
        ({"expect": ["nothing"]}, "expect must list"),
        ({"expect": "token"}, "expect must list"),
        ({"seed": ""}, "seed must be"),
    ):
        response = login.control.scenario(bad)
        assert response.status == 400
        assert message in response.json()["error"]
    assert login.control.request("POST", "/_double/scenario", [1]).json() == {"error": "body must be an object"}
    assert login.control.request("GET", "/_double/nothing").status == 404


def test_construction(login_ca: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid port"):
        AuthPageDouble(port=70000)
    busy = socket.create_server(("127.0.0.1", 0))
    try:
        with pytest.raises(OSError, match="in use"):
            AuthPageDouble(control_port=busy.getsockname()[1])
    finally:
        busy.close()
    with AuthPageDouble(host="::1", public_host="localhost", ca=login_ca) as double:
        assert double.page_url.startswith("https://localhost:")
        assert double.url.startswith("http://[::1]:")
    double = AuthPageDouble(host="::1")
    assert double.page_url.startswith("http://[::1]:")
    double.stop()


def test_page_markup_escapes() -> None:
    text = page("x", "<T>", "<p>body</p>").decode()
    assert '<body data-page="x">' in text
    assert "&lt;T&gt;" in text


def test_unreadable_bodies(login: Login) -> None:
    assert login.ca is not None
    port = int(login.auth.page_url.rsplit(":", 1)[1])
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=login.ca.client_context(), timeout=10)
    try:
        connection.request("POST", "/login", headers={"content-length": "x"})
        response = connection.getresponse()
        assert (response.status, b"bad_request" in response.read()) == (400, True)
    finally:
        connection.close()
    assert login.auth.state()["failures"] == ["POST /login: bad_request: invalid content-length"]
    answer = login.control.request("POST", "/_double/scenario", headers={"content-length": "x"})
    assert (answer.status, answer.json()) == (400, {"error": "invalid content-length"})


def test_error_codes_are_escaped(login: Login) -> None:
    page = login.browser().open(login.auth.page_url + "/oauth/code/callback?error=%3Cscript%3Ex%3C/script%3E")
    assert (page.status, page.forms.error) == (400, "invalid_request")
    assert "<script>" not in page.text
    text = error_page("<b>", "<i>").decode()
    assert "&lt;b&gt;" in text
    assert "&lt;i&gt;" in text


def test_the_journal_holds_no_login_secrets(login: Login) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s"))
    status, answer = _token_request(login, _exchange_body(login, pasted))
    assert status == 200
    journal = json.dumps(login.auth.journal.records())
    code, _, state = pasted.partition("#")
    secrets = [code, state, codes.pkce("s")[0], codes.magic_token(codes.DEFAULT_SEED, 1), answer["access_token"]]
    for secret in secrets:
        assert secret not in journal
    assert "<redacted 43 chars>" in journal


def test_redaction_helpers() -> None:
    assert redact_query("a=1&b=2") == "a=1&b=2"
    assert redact_query("code=abc&scope=x") == "code=<redacted 3 chars>&scope=x"
    assert redact_body({"code": "abcd", "grant_type": "x"}) == {"code": "<redacted 4 chars>", "grant_type": "x"}
    assert redact_body("token=xyz") == "token=<redacted 3 chars>"
    assert redact_body(None) is None


def test_invalid_verifier_is_a_failure(login: Login) -> None:
    body = _exchange_body(login, "c#s")
    body["code_verifier"] = "ü" * 43
    assert _token_request(login, body) == (
        400,
        {"error": "invalid_request", "error_description": "invalid code_verifier"},
    )
