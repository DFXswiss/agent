"""The REST surface of the GitHub App "agent CI" as GitHub answers it (standard library only).

`CiGitHub.handle(method, path, headers)` answers `(status, headers, body)`:

- `POST /app/installations/{id}/access_tokens`: needs `Authorization: Bearer <app JWT>` - three base64url parts, header
  `{"alg": "RS256"}`, claims `iss` = the app's client ID, `exp - iat` at most 600 s (GitHub's limit). The signature
  is not checked here (the controller's tests verify it with the public key); an unknown installation is `404`, a bad
  JWT `401 A JSON web token could not be decoded`. Answers `201 {token, expires_at, permissions}` with the
  installation's permissions.
- `GET /repos/{owner}/{repo}/actions/runs/{run_id}`: needs an installation token of an installation that holds the
  repository and `actions: read`, else `403 Resource not accessible by integration`; an unknown run is `404`.
- `GET /repos/{owner}/{repo}/commits/{sha}/pulls`: the same with `pull_requests: read`; the pull requests GitHub
  associates with the commit (`[{number, merged_at}]`).

`faults` lists one-shot answers by route key (`token`, `run`, `pulls`): `(status, body)` returned once instead of the
normal answer (e.g. `(500, {...})`, `(200, "not json")`). Every request is journaled as `(method, path)`.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

Answer = tuple[int, dict[str, str], bytes]
TOKEN = re.compile(r"/app/installations/(\d+)/access_tokens")
RUN = re.compile(r"/repos/([^/]+/[^/]+)/actions/runs/(\d+)")
PULLS = re.compile(r"/repos/([^/]+/[^/]+)/commits/([0-9a-f]{40})/pulls")
NOT_ACCESSIBLE = {"message": "Resource not accessible by integration"}


CONVERSION = re.compile(r"/app-manifests/([A-Za-z0-9_-]+)/conversions")
ORG = re.compile(r"/orgs/([A-Za-z0-9-]+)")


@dataclass
class Installation:
    repos: set[str]
    permissions: dict[str, str]
    account: str = ""


@dataclass
class CiGitHub:
    client_id: str
    installations: dict[int, Installation] = field(default_factory=dict)
    orgs: dict[str, int] = field(default_factory=dict)
    """`organisation login (lower case) -> ID`."""
    pem: str = ""
    """The private key the manifest conversion hands out (a test key)."""
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    """One-time codes of created apps -> the app (`create_app`)."""
    app: dict[str, Any] | None = None
    runs: dict[tuple[str, int], str] = field(default_factory=dict)
    """`(repo, run_id) -> head_sha`."""
    pulls: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    """`(repo, sha) -> [{number, merged_at}]`."""
    expires_at: str = "2026-11-27T12:00:00Z"
    faults: dict[str, list[tuple[int, Any]]] = field(default_factory=dict)
    journal: list[tuple[str, str]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _json(status: int, body: Any) -> Answer:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        return status, {"content-type": "application/json"}, data

    def _fault(self, key: str) -> Answer | None:
        queued = self.faults.get(key)
        return self._json(*queued.pop(0)) if queued else None

    def _claims(self, authorization: str) -> dict[str, Any] | None:
        token = authorization.removeprefix("Bearer ")
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            header, claims = (json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts[:2])
        except ValueError:
            return None
        if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict):
            return None
        iat, exp = claims.get("iat"), claims.get("exp")
        if not isinstance(iat, int) or not isinstance(exp, int) or not 0 < exp - iat <= 600:
            return None
        return claims

    def _installation(self, headers: dict[str, str], repo: str, permission: str) -> Answer | None:
        """None when the token may read `permission` of `repo`, else GitHub's refusal."""
        found = self.tokens.get(headers.get("authorization", "").removeprefix("Bearer "))
        if found is None:
            return self._json(401, {"message": "Bad credentials"})
        installation = self.installations[found]
        if repo.lower() not in installation.repos:
            return self._json(404, {"message": "Not Found"})
        if installation.permissions.get(permission) not in ("read", "write"):
            return self._json(403, NOT_ACCESSIBLE)
        return None

    # -- the app-manifest flow (the web side is `agent_doubles.ci.web`) ----------------------------------------------

    def create_app(self, owner: str, manifest: dict[str, Any]) -> str:
        """GitHub creates the app of `manifest` in `owner` and hands out a one-time code."""
        code = f"code{len(self.codes) + 1:04d}x"
        slug = str(manifest["name"]).lower().replace(" ", "-")
        self.app = {"id": 4242, "slug": slug, "client_id": "Iv23-created", "owner": owner, "manifest": manifest}
        self.codes[code] = self.app
        return code

    def install(self, org_id: int) -> int:
        """The app is installed in the organisation with this ID."""
        assert self.app is not None
        login = next(name for name, ident in self.orgs.items() if ident == org_id)
        ident = 7000 + len(self.installations)
        self.installations[ident] = Installation(set(), dict(self.app["manifest"]["default_permissions"]), login)
        return ident

    def _app_routes(  # noqa: PLR0911 - one return per answer
        self, method: str, path: str, lowered: dict[str, str]
    ) -> Answer | None:
        if method == "POST" and (match := CONVERSION.fullmatch(path)):
            if "authorization" in lowered:
                return self._json(400, {"message": "the conversion takes no credential"})
            app = self.codes.pop(match.group(1), None)
            if app is None:
                return self._json(404, {"message": "Not Found"})
            self.client_id = app["client_id"]
            return self._json(
                201,
                {
                    "id": app["id"],
                    "slug": app["slug"],
                    "client_id": app["client_id"],
                    "pem": self.pem,
                    "client_secret": "not-kept",
                    "webhook_secret": None,
                    "owner": {"login": app["owner"]},
                },
            )
        if method == "GET" and (match := ORG.fullmatch(path)):
            ident = self.orgs.get(match.group(1).lower())
            return self._json(404, {"message": "Not Found"}) if ident is None else self._json(200, {"id": ident})
        if method == "GET" and path == "/app/installations":
            claims = self._claims(lowered.get("authorization", ""))
            if claims is None or claims.get("iss") != self.client_id:
                return self._json(401, {"message": "A JSON web token could not be decoded"})
            return self._json(
                200, [{"id": ident, "account": {"login": item.account}} for ident, item in self.installations.items()]
            )
        return None

    def handle(  # noqa: PLR0911, PLR0912 - one branch and return per route and answer
        self, method: str, path: str, headers: dict[str, str]
    ) -> Answer:
        self.journal.append((method, path))
        lowered = {key.lower(): value for key, value in headers.items()}
        fault = self._fault(path)
        if fault is not None:
            return fault
        answer = self._app_routes(method, path, lowered)
        if answer is not None:
            return answer
        if method == "POST" and (match := TOKEN.fullmatch(path)):
            fault = self._fault("token")
            if fault is not None:
                return fault
            installation = self.installations.get(int(match.group(1)))
            if installation is None:
                return self._json(404, {"message": "Not Found"})
            claims = self._claims(lowered.get("authorization", ""))
            if claims is None or claims.get("iss") != self.client_id:
                return self._json(401, {"message": "A JSON web token could not be decoded"})
            token = f"ghs_double{len(self.tokens) + 1:04d}"
            self.tokens[token] = int(match.group(1))
            body = {"token": token, "expires_at": self.expires_at, "permissions": installation.permissions}
            return self._json(201, body)
        if method == "GET" and (match := RUN.fullmatch(path)):
            refused = self._installation(lowered, match.group(1), "actions")
            if refused is not None:
                return refused
            fault = self._fault("run")
            if fault is not None:
                return fault
            sha = self.runs.get((match.group(1).lower(), int(match.group(2))))
            if sha is None:
                return self._json(404, {"message": "Not Found"})
            return self._json(200, {"id": int(match.group(2)), "head_sha": sha})
        if method == "GET" and (match := PULLS.fullmatch(path)):
            refused = self._installation(lowered, match.group(1), "pull_requests")
            if refused is not None:
                return refused
            fault = self._fault("pulls")
            if fault is not None:
                return fault
            return self._json(200, self.pulls.get((match.group(1).lower(), match.group(2)), []))
        return self._json(404, {"message": "Not Found"})
