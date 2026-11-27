"""`agent-ci setup`: the GitHub App "agent CI" created and installed by agent.dfx.swiss itself (spec 16 section 17.9,
decision D-4).

The steps in the browser run in a session of the platform browser (ADR-085) of the repositories' fixed task, driven
by the controller through the runner's CI area (`Browser`, ops `browser` of `area.py`); the steps at GitHub's API run
in the controller (`AppApi`):

1. **Create** in the owning organisation through GitHub's app-manifest flow: open
   `https://github.com/organizations/<org>/settings/apps/new`, post the manifest (`manifest`) to it with a random
   `state`, confirm "Create GitHub App". GitHub redirects to `redirect_url?code=<one-time code>&state=<state>`.
2. **Convert**: the controller reads the code from the browser's answer (it reaches the controller over the local
   socket only; the browser record masks `code` and `state`, `workvm.browser.redact_url`), checks `state` and
   exchanges the code itself: `POST /app-manifests/{code}/conversions`. The answer's private key goes straight into
   the key file of the controller's account (mode 0600, written atomically) - never into a VM, to the hub, into a
   chat or a log. Client secret and webhook secret of the answer are not kept.
3. **Install** in every organisation of the configuration (the organisations of stage 0): open
   `https://github.com/apps/<slug>/installations/new/permissions?target_id=<organisation ID>` and confirm "Install";
   the installation ID is then read with the app's JWT (`GET /app/installations`), not from the page.
4. The configuration gets the app's client ID and each repository's installation ID (`update_config`).

The manifest asks only for the organisation permission **Self-hosted runners: write**
(`organization_self_hosted_runners`, what an organisation scale set needs; `Metadata: read` is granted to every
app), no events, the webhook off.

**A person** is needed where GitHub demands one: signing in as an owner of the organisation, two-factor, passkey,
"Confirm access" (sudo mode). `person_needed` recognises these pages by URL and title; the flow then stops, reports
`ci.setup person_needed step=<step> reason=<reason>` (`notify`, the owner takes over the browser per remote control) and
looks again every `POLL_S` seconds until the page no longer needs a person, at most `PERSON_WAIT_S` seconds
(`SetupFailed("person_timeout")`). No credential of the owner is ever typed by the flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from agent_runner.ci.github import app_jwt

APP_NAME = "agent CI"
APP_URL = "https://agent.dfx.swiss"
WEB = "https://github.com"
POLL_S = 10.0
PERSON_WAIT_S = 1800.0
PERMISSIONS = {"organization_self_hosted_runners": "write"}
PERSON_PATHS = ("/login", "/session", "/sessions/", "/sudo", "/password_reset")
PERSON_TITLES = ("sign in to github", "two-factor", "passkey", "device verification")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,98}")
CODE = re.compile(r"[A-Za-z0-9_-]{8,128}")


class SetupFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    ok: bool = True


class Browser(Protocol):
    async def act(self, tool: str, arguments: Mapping[str, Any]) -> Page: ...


def manifest(owner: str, *, name: str = APP_NAME, public: bool) -> dict[str, Any]:
    """The app manifest (module docstring); `public` when it is installed in other organisations too."""
    return {
        "name": name,
        "url": APP_URL,
        "redirect_url": f"{WEB}/organizations/{owner}/settings/apps",
        "public": public,
        "hook_attributes": {"url": APP_URL, "active": False},
        "default_permissions": dict(PERMISSIONS),
        "default_events": [],
    }


def post_script(owner: str, state: str, document: Mapping[str, Any]) -> str:
    """JavaScript that posts the manifest to GitHub's form (the manifest flow wants a form POST)."""
    action = f"{WEB}/organizations/{owner}/settings/apps/new?state={state}"
    return (
        "(() => { const form = document.createElement('form'); form.method = 'post';"
        f" form.action = {json.dumps(action)}; const field = document.createElement('input');"
        f" field.type = 'hidden'; field.name = 'manifest'; field.value = {json.dumps(json.dumps(document))};"
        " form.appendChild(field); document.body.appendChild(form); form.submit(); return true; })()"
    )


def person_needed(page: Page) -> str | None:
    """Why the page needs a person (`sign_in`, `confirm`), None when the flow can go on."""
    parts = urlsplit(page.url)
    title = page.title.lower()
    if "confirm access" in title:
        return "confirm"
    on_github = parts.hostname == "github.com"
    if (on_github and parts.path.startswith(PERSON_PATHS)) or any(word in title for word in PERSON_TITLES):
        return "sign_in"
    return None


def code_of(url: str, state: str) -> str:
    """The one-time code of the manifest redirect; `state_mismatch` or `code_missing` otherwise."""
    query = parse_qs(urlsplit(url).query)
    if query.get("state") != [state]:
        raise SetupFailed("state_mismatch" if "state" in query else "code_missing")
    codes = query.get("code", [])
    if len(codes) != 1 or CODE.fullmatch(codes[0]) is None:
        raise SetupFailed("code_missing")
    return codes[0]


def write_private(path: Path, data: bytes) -> None:
    """`data` into `path` with mode 0600, atomically (a temporary file in the same directory, then rename)."""
    descriptor, temporary = tempfile.mkstemp(prefix=".agent-ci.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class App:
    app_id: int
    slug: str
    client_id: str


@dataclass
class AppApi:
    """GitHub's API for the setup: the manifest conversion, organisation IDs and the app's installations."""

    api_url: str
    transport: httpx.AsyncBaseTransport | None = None
    wall: Callable[[], float] = time.time

    async def _request(self, method: str, path: str, token: str | None = None) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(transport=self.transport, timeout=30.0, trust_env=False) as client:
            try:
                answer = await client.request(method, self.api_url + path, headers=headers)
            except httpx.HTTPError:
                raise SetupFailed("github_unreachable") from None
        if not 200 <= answer.status_code < 300:
            raise SetupFailed(f"github_{answer.status_code}")
        try:
            return answer.json()
        except ValueError:
            raise SetupFailed("github_answer_invalid") from None

    async def convert(self, code: str, key_file: Path) -> App:
        """Exchange the one-time code; the private key goes into `key_file` only (module docstring step 2)."""
        data = await self._request("POST", f"/app-manifests/{code}/conversions")
        app_id, slug = (
            data.get("id") if isinstance(data, dict) else None,
            data.get("slug") if isinstance(data, dict) else None,
        )
        client_id, pem = (data.get("client_id"), data.get("pem")) if isinstance(data, dict) else (None, None)
        if (
            not isinstance(app_id, int)
            or not isinstance(slug, str)
            or SLUG.fullmatch(slug) is None
            or not isinstance(client_id, str)
            or not isinstance(pem, str)
            or "PRIVATE KEY" not in pem
        ):
            raise SetupFailed("github_answer_invalid")
        write_private(key_file, pem.encode())
        return App(app_id, slug, client_id)

    async def organisation_id(self, org: str) -> int:
        data = await self._request("GET", f"/orgs/{org}")
        ident = data.get("id") if isinstance(data, dict) else None
        if not isinstance(ident, int):
            raise SetupFailed("github_answer_invalid")
        return ident

    async def installations(self, client_id: str, key: bytes) -> dict[str, int]:
        """`{organisation login (lower case): installation ID}` of the app."""
        data = await self._request("GET", "/app/installations", app_jwt(client_id, key, self.wall()))
        if not isinstance(data, list):
            raise SetupFailed("github_answer_invalid")
        found: dict[str, int] = {}
        for item in data:
            account = item.get("account") if isinstance(item, dict) else None
            login = account.get("login") if isinstance(account, dict) else None
            ident = item.get("id") if isinstance(item, dict) else None
            if isinstance(login, str) and isinstance(ident, int):
                found[login.lower()] = ident
        return found


@dataclass
class AppSetup:
    browser: Browser
    api: AppApi
    key_file: Path
    notify: Callable[[str], None]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_s: float = POLL_S
    person_wait_s: float = PERSON_WAIT_S

    async def _act(self, step: str, tool: str, arguments: Mapping[str, Any]) -> Page:
        page = await self.browser.act(tool, arguments)
        return await self._person(step, page)

    async def _person(self, step: str, page: Page) -> Page:
        """Wait while the page needs a person (module docstring)."""
        reason = person_needed(page)
        waited = 0.0
        while reason is not None:
            if waited == 0.0:
                self.notify(f"ci.setup person_needed step={step} reason={reason}")
            if waited >= self.person_wait_s:
                raise SetupFailed("person_timeout")
            await self.sleep(self.poll_s)
            waited += self.poll_s
            page = await self.browser.act("browser_snapshot", {"snapshot": False})
            reason = person_needed(page)
            if reason is None:
                self.notify(f"ci.setup person_done step={step}")
        return page

    async def create(self, owner: str, *, public: bool) -> App:
        """Module docstring steps 1 and 2."""
        state = secrets.token_urlsafe(24)
        new = f"{WEB}/organizations/{owner}/settings/apps/new"
        page = await self._act("open", "browser_navigate", {"url": new, "snapshot": False})
        if not urlsplit(page.url).path.startswith(f"/organizations/{owner}/settings/apps"):
            raise SetupFailed("not_an_owner")
        await self._act(
            "manifest", "browser_evaluate", {"expression": post_script(owner, state, manifest(owner, public=public))}
        )
        page = await self._act("create", "browser_click", {"target": "text=Create GitHub App", "snapshot": False})
        app = await self.api.convert(code_of(page.url, state), self.key_file)
        self.notify(f"ci.setup app_created app_id={app.app_id} slug={app.slug}")
        return app

    async def install(self, app: App, orgs: Sequence[str]) -> dict[str, int]:
        """Module docstring step 3: `{organisation (lower case): installation ID}`."""
        for org in orgs:
            target = await self.api.organisation_id(org)
            url = f"{WEB}/apps/{app.slug}/installations/new/permissions?target_id={target}"
            await self._act(f"install_{org.lower()}", "browser_navigate", {"url": url, "snapshot": False})
            await self._act(f"install_{org.lower()}", "browser_click", {"target": "text=Install", "snapshot": False})
        found = await self.api.installations(app.client_id, self.key_file.read_bytes())
        missing = [org for org in orgs if org.lower() not in found]
        if missing:
            raise SetupFailed("not_installed")
        self.notify(f"ci.setup installed organisations={len(orgs)}")
        return {org.lower(): found[org.lower()] for org in orgs}


def update_config(path: Path, app: App, installations: Mapping[str, int]) -> None:
    """Write the app's client ID and each repository's installation ID into the controller's configuration."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["app"]["client_id"] = app.client_id
    for entry in data["repositories"]:
        entry["installation_id"] = installations[entry["repo"].split("/")[0].lower()]
    write_private(path, (json.dumps(data, indent=2) + "\n").encode())


@dataclass
class RunnerBrowser:
    """The `Browser` port over the runner's CI area (`browser` op) for the fixed task of `owner`."""

    link: Any
    owner: Mapping[str, str]

    async def act(self, tool: str, arguments: Mapping[str, Any]) -> Page:
        answer = await self.link.call("browser", owner=dict(self.owner), tool=tool, arguments=dict(arguments))
        url, title = answer.get("url"), answer.get("title")
        return Page(
            url if isinstance(url, str) else "", title if isinstance(title, str) else "", answer.get("ok") is True
        )
