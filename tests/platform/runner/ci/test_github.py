"""The controller's reads with the GitHub App "agent CI" against the GitHub double (`agent_doubles.ci.github`)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from agent_doubles.ci.github import CiGitHub, Installation
from agent_runner.ci.attribution import RunsUnavailable
from agent_runner.ci.github import AppRuns, app_jwt
from agent_runner.ci.repos import parse

from .kit import REPO, SHA, config, config_data

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
NOW = 1_800_000_000.0


def _part(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_the_app_jwt_is_signed_with_the_app_key() -> None:
    token = app_jwt("Iv23-test", PEM, NOW)
    header, claims, signature = token.split(".")
    assert json.loads(_part(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_part(claims)) == {"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": "Iv23-test"}
    KEY.public_key().verify(_part(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_a_key_that_is_not_rsa_is_refused() -> None:
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(ValueError, match="RSA"):
        app_jwt("Iv23-test", other, NOW)


def _github(**permissions: str) -> CiGitHub:
    double = CiGitHub("Iv23-test", expires_at="2027-01-15T09:00:00Z")
    double.installations[11] = Installation({REPO}, permissions or {"actions": "read", "pull_requests": "read"})
    double.runs[(REPO, 942)] = SHA
    items: list[Any] = [  # items of the wrong shape are skipped
        {"number": 3, "merged_at": None},
        {"number": 8, "merged_at": "2026-11-27T09:00:00Z"},
        {"number": "x"},
        "not an object",
    ]
    double.pulls[(REPO, SHA)] = items
    return double


def _runs(double: CiGitHub, clock: list[float] | None = None) -> AppRuns:
    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = double.handle(request.method, request.url.path, dict(request.headers))
        return httpx.Response(status, headers=headers, content=body)

    now = clock or [NOW]

    async def sleep(_seconds: float) -> None:
        return None

    return AppRuns(config(), lambda: PEM, transport=httpx.MockTransport(handler), wall=lambda: now[0], sleep=sleep)


def test_a_push_run_is_traced_to_its_pull_requests_merged_first() -> None:
    double = _github()
    runs = _runs(double)

    async def main() -> tuple[str, Any]:
        sha = await runs.head_sha(REPO.upper(), 942)
        return sha, await runs.pulls_of(REPO, sha)

    sha, numbers = asyncio.run(main())
    assert sha == SHA
    assert list(numbers) == [8, 3]
    assert [path for method, path in double.journal if method == "POST"] == ["/app/installations/11/access_tokens"]


def test_the_installation_token_is_kept_until_shortly_before_it_expires() -> None:
    double = _github()
    double.expires_at = "2027-01-15T08:20:00Z"  # NOW + 1200 s (NOW is 2027-01-15T08:00:00Z)
    clock = [NOW]
    runs = _runs(double, clock)
    asyncio.run(runs.head_sha(REPO, 942))
    asyncio.run(runs.head_sha(REPO, 942))
    clock[0] = NOW + 1000  # within the margin of 300 s before the expiry
    asyncio.run(runs.head_sha(REPO, 942))
    assert sum(1 for method, _path in double.journal if method == "POST") == 2


def test_without_the_read_rights_a_push_cannot_be_traced() -> None:
    runs = _runs(_github(administration="write"))
    with pytest.raises(RunsUnavailable, match="forbidden"):
        asyncio.run(runs.head_sha(REPO, 942))


def test_a_revoked_token_is_dropped() -> None:
    double = _github()
    runs = _runs(double)
    asyncio.run(runs.head_sha(REPO, 942))
    double.tokens.clear()
    with pytest.raises(RunsUnavailable, match="unauthorized"):
        asyncio.run(runs.head_sha(REPO, 942))
    assert runs.tokens == {}


@pytest.mark.parametrize(
    ("route", "fault", "kind"),
    [
        ("token", (500, {"message": "boom"}), "server_error"),
        ("token", (201, {"token": "", "expires_at": "x"}), "invalid_response"),
        ("token", (201, ["not", "an", "object"]), "invalid_response"),
        ("token", (201, {"token": "t", "expires_at": "not a time"}), "invalid_response"),
        ("run", (200, {"head_sha": "short"}), "invalid_response"),
        ("run", (200, ["no object"]), "invalid_response"),
        ("pulls", (200, {"not": "a list"}), "invalid_response"),
    ],
)
def test_answers_of_the_wrong_shape_are_unavailable(route: str, fault: tuple[int, Any], kind: str) -> None:
    double = _github()
    double.faults[route] = [fault]
    runs = _runs(double)

    async def main() -> None:
        await runs.pulls_of(REPO, await runs.head_sha(REPO, 942))

    with pytest.raises(RunsUnavailable, match=kind):
        asyncio.run(main())


def test_unknown_runs_installations_and_repositories() -> None:
    double = _github()
    runs = _runs(double)
    with pytest.raises(RunsUnavailable, match="not_found"):
        asyncio.run(runs.head_sha(REPO, 1))
    with pytest.raises(RunsUnavailable):
        asyncio.run(runs.head_sha("example-org/unserved", 942))
    del double.installations[11]
    runs.tokens.clear()
    with pytest.raises(RunsUnavailable, match="not_found"):
        asyncio.run(runs.head_sha(REPO, 942))


def test_a_repository_without_an_installation_cannot_be_read() -> None:
    entry = {k: v for k, v in config_data()["repositories"][0].items() if k != "installation_id"}
    runs = AppRuns(parse(config_data(repositories=[entry])), lambda: PEM)
    with pytest.raises(RunsUnavailable, match="not_installed"):
        asyncio.run(runs.head_sha(REPO, 942))


def test_the_double_refuses_what_github_refuses() -> None:
    double = _github()
    assert double.handle("GET", "/unknown", {})[0] == 404
    bad = [
        "Bearer x.y",
        "Bearer a.b.c",
        "Bearer "
        + ".".join(
            base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")
            for part in ({"alg": "HS256"}, {"iat": 1, "exp": 2})
        )
        + ".s",
        "Bearer "
        + ".".join(
            base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")
            for part in ({"alg": "RS256"}, {"iat": 1, "exp": 10_000})
        )
        + ".s",
    ]
    for authorization in bad:
        status, _headers, _body = double.handle(
            "POST", "/app/installations/11/access_tokens", {"Authorization": authorization}
        )
        assert status == 401
    status, _headers, _body = double.handle("GET", f"/repos/{REPO}/actions/runs/942", {"authorization": "Bearer nope"})
    assert status == 401
    double.installations[12] = Installation({"example-org/other"}, {"actions": "read"})
    double.tokens["t12"] = 12
    assert double.handle("GET", f"/repos/{REPO}/actions/runs/942", {"authorization": "Bearer t12"})[0] == 404
    assert double.handle("GET", f"/repos/{REPO}/commits/{SHA}/pulls", {"authorization": "Bearer t12"})[0] == 404
    double.tokens["t11"] = 11
    double.faults["pulls"] = [(200, "not json")]
    assert double.handle("GET", f"/repos/{REPO}/commits/{SHA}/pulls", {"authorization": "Bearer t11"})[2] == b"not json"
    assert double.handle("GET", f"/repos/{REPO}/commits/{'b' * 40}/pulls", {"authorization": "Bearer t11"})[2] == b"[]"
