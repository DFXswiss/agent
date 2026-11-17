"""GET /api/key/release (KEY-PROTOCOL.md section 8; its tests are listed in section 10).

Requests go straight to the ASGI app with the helpers of the signed-request tests, so the raw
request target reaches the hub exactly as signed. Every key comes from a fixed seed and the hub
clock is injected.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import yaml
from fastapi import FastAPI

from agent_core import app as app_module
from agent_core.app import (
    PULL_REQUEST_MAX,
    RELEASE_FAILURES,
    ReleaseQuery,
    create_app,
    parse_release_query,
    release_answer,
    release_body,
)
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub
from agent_core.keys import SCOPE_HOLDERS
from tests.conftest import HUB_KEY_ID
from tests.test_signed_requests import (
    NOW,
    STRANGER_SEED,
    WHOAMI,
    Answer,
    FixedClock,
    Wire,
    assert_refused,
    assert_signed,
    build_wire,
    fmt,
    key_entries,
    log_lines,  # noqa: F401 - a fixture, used by name
    nonce_rows,
    plain_wire,
    public_text,
    send,
)

RELEASE = "/api/key/release"
REPO = "example-org/example-repo"
REPO_QUERY = "repository=example-org%2Fexample-repo"
VALID_QUERY = f"{REPO_QUERY}&pull_request=42"
VALID = f"{RELEASE}?{VALID_QUERY}"
READER_SEED = 21
QUERY_INVALID = b'{"error":"query_invalid"}'


def released_bytes(repository: str, pull_request: int) -> bytes:
    return (
        b'{"repository":"' + repository.encode("ascii") + b'","pull_request":'
        + str(pull_request).encode("ascii")
        + b',"released":true,"failed":null,"hub_key":"' + HUB_KEY_ID.encode("ascii") + b'"}'
    )


def release_key_entries() -> list[dict[str, Any]]:
    """The keys of the signed-request tests plus a service key without `release:query`."""
    entries = key_entries()
    entries.append(
        {
            "id": "reader-one",
            "kind": "ed25519",
            "principal": "service",
            "handle": "example-reader",
            "team": "example",
            "device": "runner-9",
            "public_key": public_text(READER_SEED),
            "scopes": ["record:read"],
            "subjects": [REPO],
            "expires": fmt(NOW + 365 * 86400),
        }
    )
    return entries


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def app(cfg: Config, tmp_path: Path, clock: FixedClock) -> Iterator[FastAPI]:
    teams = tmp_path / "release-teams.yaml"
    teams.write_text(
        yaml.safe_dump({"teams": {"example": {"members": ["staff-a", "staff-b"]}}}),
        encoding="utf-8",
    )
    keys = tmp_path / "release-keys.yaml"
    keys.write_text(yaml.safe_dump({"keys": release_key_entries()}, sort_keys=False), encoding="utf-8")
    app_cfg = replace(
        cfg,
        public_url="http://testserver",
        teams_path=teams,
        keys_path=keys,
        database=str(tmp_path / "release.sqlite"),
    )
    store = Store(app_cfg.database)
    try:
        yield create_app(app_cfg, github=FakeGitHub({"code-staff-a": "staff-a"}), store=store, clock=clock)
    finally:
        store.close()


def guard(target: str, **changes: Any) -> Wire:
    return build_wire("guard-one", target=target, **changes)


def reader(target: str, **changes: Any) -> Wire:
    return build_wire("reader-one", seed=READER_SEED, target=target, **changes)


def assert_signed_error(wire: Wire, answer: Answer, status: int, error: str) -> None:
    assert_signed(wire, answer, status, NOW)
    assert answer.body == json.dumps({"error": error}, separators=(",", ":")).encode("ascii")


def assert_unsigned_error(answer: Answer, status: int, error: str) -> None:
    assert_refused(answer, status, error)
    assert answer.body == json.dumps({"error": error}, separators=(",", ":")).encode("ascii")


# Success


def test_service_key_with_scope_and_subject_gets_a_signed_200(app: FastAPI) -> None:
    wire = guard(VALID)
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.body == released_bytes(REPO, 42)
    assert answer.header("content-type") == "application/json"
    assert list(answer.json()) == ["repository", "pull_request", "released", "failed", "hub_key"]


@pytest.mark.parametrize(
    "query",
    [
        "repository=example-org%2Fexample-repo&pull_request=42",
        "repository=example-org%2fexample-repo&pull_request=42",
        "repository=example-org/example-repo&pull_request=42",
        "pull_request=42&repository=example-org%2Fexample-repo",
        "pull_request=42&repository=example-org%2fexample-repo",
        "pull_request=42&repository=example-org/example-repo",
    ],
)
def test_encoded_slash_and_both_orders_are_answered_with_a_slash(app: FastAPI, query: str) -> None:
    wire = guard(f"{RELEASE}?{query}")
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.body == released_bytes(REPO, 42)


@pytest.mark.parametrize("number", [1, 9, 10, PULL_REQUEST_MAX])
def test_pull_request_bounds_are_accepted(app: FastAPI, number: int) -> None:
    wire = guard(f"{RELEASE}?{REPO_QUERY}&pull_request={number}")
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.body == released_bytes(REPO, number)


# Invalid queries: a signed 400 query_invalid


INVALID_QUERIES = {
    "no query at all": "",
    "repository missing": "pull_request=42",
    "pull_request missing": REPO_QUERY,
    "repository repeated": f"{REPO_QUERY}&{REPO_QUERY}&pull_request=42",
    "pull_request repeated": f"{VALID_QUERY}&pull_request=42",
    "pull_request repeated with another value": f"{VALID_QUERY}&pull_request=43",
    "unknown third parameter": f"{VALID_QUERY}&extra=1",
    "unknown parameter instead of one": f"{REPO_QUERY}&number=42",
    "parameter name in uppercase": "Repository=example-org%2Fexample-repo&pull_request=42",
    "parameter name percent-encoded": f"{REPO_QUERY}&pull%5Frequest=42",
    "empty repository": "repository=&pull_request=42",
    "empty pull_request": f"{REPO_QUERY}&pull_request=",
    "parameter without equals sign": "repository&pull_request=42",
    "empty parameter in the middle": f"{REPO_QUERY}&&pull_request=42",
    "trailing ampersand": f"{VALID_QUERY}&",
    "leading ampersand": f"&{VALID_QUERY}",
    "semicolon as separator": f"{REPO_QUERY};pull_request=42",
    "pull_request zero": f"{REPO_QUERY}&pull_request=0",
    "pull_request negative": f"{REPO_QUERY}&pull_request=-42",
    "pull_request with leading zero": f"{REPO_QUERY}&pull_request=042",
    "pull_request with plus sign": f"{REPO_QUERY}&pull_request=+42",
    "pull_request with encoded plus sign": f"{REPO_QUERY}&pull_request=%2B42",
    "pull_request in hex": f"{REPO_QUERY}&pull_request=0x2a",
    "pull_request with a letter": f"{REPO_QUERY}&pull_request=42a",
    "pull_request with a fraction": f"{REPO_QUERY}&pull_request=4.2",
    "pull_request with an exponent": f"{REPO_QUERY}&pull_request=4e2",
    "pull_request percent-encoded": f"{REPO_QUERY}&pull_request=%342",
    "pull_request 2147483648": f"{REPO_QUERY}&pull_request=2147483648",
    "pull_request of eleven digits": f"{REPO_QUERY}&pull_request=21474836470",
    "repository without slash": "repository=example-org&pull_request=42",
    "repository with two slashes": "repository=example-org/example-repo/extra&pull_request=42",
    "repository with two encoded slashes": "repository=example-org%2Fexample-repo%2Fx&pull_request=42",
    "repository with a literal and an encoded slash": "repository=example-org/x%2Fexample-repo&pull_request=42",
    "repository with empty owner": "repository=%2Fexample-repo&pull_request=42",
    "repository with empty name": "repository=example-org%2F&pull_request=42",
    "owner starting with a hyphen": "repository=-example-org%2Fexample-repo&pull_request=42",
    "owner with a double hyphen": "repository=example--org%2Fexample-repo&pull_request=42",
    "owner with a dot": "repository=example.org%2Fexample-repo&pull_request=42",
    "owner of 40 characters": f"repository={'a' * 40}%2Fexample-repo&pull_request=42",
    "name dot": "repository=example-org%2F.&pull_request=42",
    "name dot dot": "repository=example-org%2F..&pull_request=42",
    "name with an exclamation mark": "repository=example-org%2Fexample!repo&pull_request=42",
    "name of 101 characters": f"repository=example-org%2F{'a' * 101}&pull_request=42",
    "encoded hyphen": "repository=example%2Dorg%2Fexample-repo&pull_request=42",
    "double-encoded slash": "repository=example-org%252Fexample-repo&pull_request=42",
    "truncated percent sign": "repository=example-org%2&pull_request=42",
    "lone percent sign": "repository=example-org%&pull_request=42",
    "plus in repository": "repository=example-org%2Fexample+repo&pull_request=42",
    "plus before the slash": "repository=example-org+%2Fexample-repo&pull_request=42",
    "repository in uppercase": "repository=Example-Org%2Fexample-repo&pull_request=42",
}


@pytest.mark.parametrize("case", sorted(INVALID_QUERIES))
def test_invalid_query_gets_a_signed_400(app: FastAPI, case: str) -> None:
    query = INVALID_QUERIES[case]
    wire = guard(f"{RELEASE}?{query}" if query else RELEASE)
    assert wire.query == query.encode("ascii")
    answer = send(app, wire)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == QUERY_INVALID


# Check 9: the scope comes first


def test_release_query_is_held_only_by_service_keys() -> None:
    assert SCOPE_HOLDERS["release:query"] == frozenset({"service"})


@pytest.mark.parametrize("key_id", ["agent-one", "agent-two"])
@pytest.mark.parametrize(
    "target",
    [VALID, f"{RELEASE}?pull_request=0", RELEASE, f"{RELEASE}?repository=example-org%2Fother-repo&pull_request=1"],
)
def test_agent_key_gets_scope_missing(app: FastAPI, key_id: str, target: str) -> None:
    wire = build_wire(key_id, target=target)
    assert_signed_error(wire, send(app, wire), 403, "scope_missing")


@pytest.mark.parametrize(
    "target",
    [VALID, f"{RELEASE}?pull_request=0", RELEASE, f"{RELEASE}?repository=example-org%2Fother-repo&pull_request=1"],
)
def test_service_key_without_the_scope_gets_scope_missing(app: FastAPI, target: str) -> None:
    wire = reader(target)
    assert_signed_error(wire, send(app, wire), 403, "scope_missing")


def test_staff_key_is_refused_before_the_scope(app: FastAPI) -> None:
    answer = send(app, build_wire("staff-pass", seed=STRANGER_SEED, target=VALID))
    assert_unsigned_error(answer, 401, "key_kind_not_allowed")


# Check 10: the subject


@pytest.mark.parametrize(
    "repository",
    [
        "example-org%2Fother-repo",
        "other-org%2Fexample-repo",
        "example-org%2Fexample-repo2",
        "example-or%2Fexample-repo",
    ],
)
def test_repository_outside_the_subjects_gets_subject_not_allowed(app: FastAPI, repository: str) -> None:
    wire = guard(f"{RELEASE}?repository={repository}&pull_request=42")
    assert_signed_error(wire, send(app, wire), 403, "subject_not_allowed")


@pytest.mark.parametrize(
    "repository",
    ["Example-Org%2Fexample-repo", "example-org%2FExample-Repo", "EXAMPLE-ORG%2FEXAMPLE-REPO"],
)
def test_repository_differing_only_in_case_is_an_invalid_query(app: FastAPI, repository: str) -> None:
    wire = guard(f"{RELEASE}?repository={repository}&pull_request=42")
    answer = send(app, wire)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == QUERY_INVALID


def test_query_is_validated_before_the_subject(app: FastAPI) -> None:
    wire = guard(f"{RELEASE}?repository=example-org%2Fother-repo&pull_request=0")
    answer = send(app, wire)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == QUERY_INVALID


# Another method, a trailing slash and HEAD


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("POST", VALID),
        ("PUT", VALID),
        ("DELETE", VALID),
        ("PATCH", VALID),
        ("GET", f"{RELEASE}/?{VALID_QUERY}"),
        ("GET", f"{RELEASE}/"),
        ("GET", f"/api/key/RELEASE?{VALID_QUERY}"),
    ],
)
def test_no_endpoint_gives_a_signed_404(app: FastAPI, method: str, target: str) -> None:
    wire = guard(target, method=method)
    answer = send(app, wire)
    assert_signed_error(wire, answer, 404, "not_found")
    assert answer.header("location") is None


def test_head_gives_a_signed_404_without_body(app: FastAPI) -> None:
    wire = guard(VALID, method="HEAD")
    answer = send(app, wire)
    assert answer.body == b""
    assert_signed(wire, answer, 404, NOW)


# Checks 1 to 8 refuse on this endpoint exactly as on whoami


def _without_signature(target: str) -> Wire:
    wire = guard(target)
    return replace(wire, headers=[(k, v) for k, v in wire.headers if k != b"agent-signature"])


def _no_agent_headers(target: str) -> Wire:
    raw_path, _, query = target.partition("?")
    return replace(
        plain_wire("GET", raw_path.encode("ascii"), raw_path, []), query=query.encode("ascii")
    )


CHECK_CASES: dict[str, tuple[Callable[[str], Wire], int, str]] = {
    "1 no agent headers": (_no_agent_headers, 401, "signature_missing"),
    "1 signature header missing": (_without_signature, 401, "signature_missing"),
    "1 method in lowercase": (lambda target: guard(target, method="get"), 401, "signature_missing"),
    "2 key unknown": (
        lambda target: build_wire("nobody-key", seed=STRANGER_SEED, target=target),
        401,
        "key_unknown",
    ),
    "3 key revoked": (lambda target: build_wire("revoked-past", target=target), 401, "key_revoked"),
    "4 key expired": (lambda target: build_wire("expired-past", target=target), 401, "key_expired"),
    "5 webauthn key": (
        lambda target: build_wire("staff-pass", seed=STRANGER_SEED, target=target),
        401,
        "key_kind_not_allowed",
    ),
    "6 time skew": (lambda target: guard(target, time_text=fmt(NOW - 121)), 401, "time_skew"),
    "7 signature invalid": (lambda target: guard(target, seed=STRANGER_SEED), 401, "signature_invalid"),
    "7 signed for another query": (
        lambda target: guard(target, sign_as={"target": target + "&x=1"}),
        401,
        "signature_invalid",
    ),
}


@pytest.mark.parametrize("target", [WHOAMI, VALID])
@pytest.mark.parametrize("case", sorted(CHECK_CASES))
def test_checks_1_to_7_refuse_as_on_whoami(app: FastAPI, target: str, case: str) -> None:
    make, status, error = CHECK_CASES[case]
    assert_unsigned_error(send(app, make(target)), status, error)
    assert nonce_rows(app) == 0


@pytest.mark.parametrize("target", [WHOAMI, VALID])
def test_check_8_replayed_query_is_refused(app: FastAPI, target: str) -> None:
    wire = guard(target)
    assert_signed(wire, send(app, wire), 200, NOW)
    assert_unsigned_error(send(app, wire), 401, "nonce_replayed")
    assert nonce_rows(app) == 1


def test_replayed_invalid_query_is_refused_at_check_8(app: FastAPI) -> None:
    wire = guard(f"{RELEASE}?pull_request=0")
    assert_signed(wire, send(app, wire), 400, NOW)
    assert_unsigned_error(send(app, wire), 401, "nonce_replayed")


# The service log line: raw path without the query, once per request


def test_one_log_line_per_service_request_without_the_query(
    app: FastAPI, log_lines: list[str]  # noqa: F811 - the imported fixture
) -> None:
    sent = [
        guard(VALID),
        guard(f"{RELEASE}?pull_request=0"),
        guard(f"{RELEASE}?repository=example-org%2Fother-repo&pull_request=42"),
        reader(VALID),
        # Not a service key: no line.
        build_wire("agent-one", target=VALID),
        # Not past check 8: no line.
        guard(VALID, seed=STRANGER_SEED),
    ]
    answers = [send(app, wire) for wire in sent]
    assert [answer.status for answer in answers] == [200, 400, 403, 403, 403, 401]
    prefix = "uvicorn.error INFO key request "
    service_lines = [line[len(prefix):] for line in log_lines if line.startswith(prefix)]
    assert service_lines == [
        "guard-one GET /api/key/release 200",
        "guard-one GET /api/key/release 400",
        "guard-one GET /api/key/release 403",
        "reader-one GET /api/key/release 403",
    ]
    for line in log_lines:
        assert "repository" not in line
        assert "pull_request" not in line


# The computation of the answer is one replaceable function


def test_route_uses_release_answer(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, int]] = []

    def not_ready(repository: str, pull_request: int) -> tuple[bool, str | None]:
        asked.append((repository, pull_request))
        return False, "task_not_ready"

    monkeypatch.setattr(app_module, "release_answer", not_ready)
    wire = guard(VALID)
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.body == (
        b'{"repository":"example-org/example-repo","pull_request":42,"released":false,'
        b'"failed":"task_not_ready","hub_key":"' + HUB_KEY_ID.encode("ascii") + b'"}'
    )
    assert asked == [(REPO, 42)]


def test_inconsistent_answer_gives_a_signed_500(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "release_answer", lambda _repository, _number: (True, "task_missing"))
    wire = guard(VALID)
    assert_signed_error(wire, send(app, wire), 500, "internal")


# The pure functions, directly


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (VALID_QUERY, ReleaseQuery(REPO, 42)),
        ("repository=example-org%2fexample-repo&pull_request=42", ReleaseQuery(REPO, 42)),
        ("pull_request=7&repository=example-org/example-repo", ReleaseQuery(REPO, 7)),
        (f"{REPO_QUERY}&pull_request={PULL_REQUEST_MAX}", ReleaseQuery(REPO, PULL_REQUEST_MAX)),
        ("repository=a%2Fb&pull_request=1", ReleaseQuery("a/b", 1)),
        ("repository=a-b%2F_.-x&pull_request=1", ReleaseQuery("a-b/_.-x", 1)),
        (f"repository={'a' * 39}%2F{'b' * 100}&pull_request=1", ReleaseQuery(f"{'a' * 39}/{'b' * 100}", 1)),
    ],
)
def test_parse_release_query_accepts(query: str, expected: ReleaseQuery) -> None:
    assert parse_release_query(query) == expected


@pytest.mark.parametrize("case", sorted(INVALID_QUERIES))
def test_parse_release_query_refuses(case: str) -> None:
    assert parse_release_query(INVALID_QUERIES[case]) is None


def test_parse_release_query_refuses_a_number_with_many_digits() -> None:
    assert parse_release_query(f"{REPO_QUERY}&pull_request={'9' * 5000}") is None


@pytest.mark.parametrize(("repository", "number"), [(REPO, 1), ("other-org/other-repo", PULL_REQUEST_MAX)])
def test_release_answer_is_yes_until_the_task_record_exists(repository: str, number: int) -> None:
    assert release_answer(repository, number) == (True, None)


def test_release_body_released() -> None:
    body = release_body(ReleaseQuery(REPO, 42), True, None, "hub-1")
    assert json.dumps(body, separators=(",", ":")) == (
        '{"repository":"example-org/example-repo","pull_request":42,'
        '"released":true,"failed":null,"hub_key":"hub-1"}'
    )


@pytest.mark.parametrize("failed", RELEASE_FAILURES)
def test_release_body_not_released_names_the_failed_condition(failed: str) -> None:
    body = release_body(ReleaseQuery(REPO, 42), False, failed, "hub-1")
    assert json.dumps(body, separators=(",", ":")) == (
        '{"repository":"example-org/example-repo","pull_request":42,'
        f'"released":false,"failed":"{failed}","hub_key":"hub-1"}}'
    )


def test_release_failures_are_the_three_conditions_in_order() -> None:
    assert RELEASE_FAILURES == ("task_missing", "task_not_ready", "question_unanswered")


@pytest.mark.parametrize(
    ("released", "failed"),
    [(True, "task_missing"), (False, None), (False, "other"), (False, "")],
)
def test_release_body_refuses_an_inconsistent_answer(released: bool, failed: str | None) -> None:
    with pytest.raises(ValueError):
        release_body(ReleaseQuery(REPO, 42), released, failed, "hub-1")
