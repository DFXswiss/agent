"""The controller's reads at GitHub with the GitHub App "agent CI" (decision D-4, spec 16 section 17.3).

The app key stays in the controller's account: `app_jwt` signs a short app JWT (RS256, `iat` 60 s back for clock
skew, `exp` 9 minutes ahead, `iss` the app's client ID) and `AppRuns` exchanges it for an installation token of the
repository's installation (`POST /app/installations/{id}/access_tokens`), kept until 5 minutes before it expires. The
JWT and the tokens are never written anywhere and never leave the controller; only the encoded JIT configuration of
an accepted job goes on, to that job's VM.

`AppRuns` is the `Runs` port of `attribution` for `push` jobs (only with `push_attribution`: the app then needs
`Actions: read` and `Pull requests: read`):

- `head_sha`: `GET /repos/{repo}/actions/runs/{run_id}` -> `head_sha`;
- `pulls_of`: `GET /repos/{repo}/commits/{sha}/pulls` -> the numbers, merged pull requests first, then by number.

Every failure (transport, refusal, an answer of the wrong shape) is `RunsUnavailable`: the job is not taken.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from agent_runner.ci.attribution import RunsUnavailable
from agent_runner.ci.repos import CiConfig
from agent_runner.github.client import GitHubClient, GitHubError, RateGate

SHA = re.compile(r"[0-9a-f]{40}")
JWT_BACK_S = 60
JWT_AHEAD_S = 540
TOKEN_MARGIN_S = 300.0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(client_id: str, key_pem: bytes, now: float) -> str:
    """The app JWT (module docstring); the key must be an RSA private key in PEM."""
    key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("the app key is not an RSA key")
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": int(now) - JWT_BACK_S, "exp": int(now) + JWT_AHEAD_S, "iss": client_id}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


@dataclass
class AppRuns:
    config: CiConfig
    read_key: Callable[[], bytes]
    transport: httpx.AsyncBaseTransport | None = None
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    tokens: dict[int, tuple[str, float]] = field(default_factory=dict)
    gate: RateGate = field(default_factory=RateGate)

    def _client(self, token: str) -> GitHubClient:
        return GitHubClient(
            api_url=self.config.api_url,
            web_url=self.config.api_url,
            token=token,
            wall=self.wall,
            sleep=self.sleep,
            gate=self.gate,
            transport=self.transport,
        )

    async def token(self, installation_id: int) -> str:
        """An installation token (module docstring)."""
        cached = self.tokens.get(installation_id)
        if cached is not None and cached[1] - TOKEN_MARGIN_S > self.wall():
            return cached[0]
        jwt = app_jwt(str(self.config.client_id), self.read_key(), self.wall())  # set: the controller runs `ready`
        async with self._client(jwt) as client:
            answer = await client.request("POST", f"/app/installations/{installation_id}/access_tokens")
        data = answer.data
        token, expires = (data.get("token"), data.get("expires_at")) if isinstance(data, dict) else (None, None)
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise GitHubError("invalid_response", status=answer.status)
        try:
            until = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise GitHubError("invalid_response", status=answer.status) from None
        self.tokens[installation_id] = (token, until)
        return token

    async def _get(self, repo: str, path: str) -> Any:
        repository = self.config.repository(repo)
        if repository is None:
            raise RunsUnavailable(repo)
        if repository.installation_id is None:
            raise RunsUnavailable("not_installed")
        try:
            token = await self.token(repository.installation_id)
            async with self._client(token) as client:
                return (await client.request("GET", f"/repos/{repository.repo}{path}")).data
        except GitHubError as error:
            if error.kind == "unauthorized":
                self.tokens.pop(repository.installation_id, None)
            raise RunsUnavailable(error.kind) from None

    async def head_sha(self, repo: str, run_id: int) -> str:
        data = await self._get(repo, f"/actions/runs/{run_id}")
        sha = data.get("head_sha") if isinstance(data, dict) else None
        if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
            raise RunsUnavailable("invalid_response")
        return sha

    async def pulls_of(self, repo: str, sha: str) -> Sequence[int]:
        data = await self._get(repo, f"/commits/{sha}/pulls")
        if not isinstance(data, list):
            raise RunsUnavailable("invalid_response")
        found: list[tuple[bool, int]] = []
        for item in data:
            number = item.get("number") if isinstance(item, dict) else None
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                found.append((item.get("merged_at") is None, number))
        return [number for _open, number in sorted(found)]
