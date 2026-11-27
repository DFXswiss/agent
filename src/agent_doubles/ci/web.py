"""The web side of GitHub's app-manifest flow and app installation, as the platform browser sees it (standard library
only; `agent_runner.ci.setup.Browser`).

`GitHubWeb.act(tool, arguments)` answers a `Page` (URL, title, ok) like the platform browser:

- every GitHub page needs a signed-in person first: the sign-in page (`/login?return_to=...`, "Sign in to GitHub")
  stays until `signs_in_after` further looks (`browser_snapshot`) passed - the person signing in per remote control -,
  then the page that was asked for opens;
- `/organizations/<org>/settings/apps/new` opens for an owner of `<org>` (`owner_of`), else GitHub's 404;
- `browser_evaluate` of the controller's form script posts the manifest (read from the script) with its `state`:
  "Create GitHub App for <org>";
- `browser_click` "text=Create GitHub App" asks for "Confirm access" (sudo mode) when `confirm` is set, until
  `confirms_after` looks passed, then creates the app in `CiGitHub` and redirects to `redirect_url?code=...&state=...`;
- `/apps/<slug>/installations/new/permissions?target_id=<id>` and `browser_click` "text=Install" install the app in
  that organisation and show its installation page;
- anything else is GitHub's 404 page; `fail` lists tools answered `ok: false`.

`seen` keeps every URL the browser was on (the tests check what the record could show).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from agent_doubles.ci.github import CiGitHub

WEB = "https://github.com"
FORM = re.compile(r'form\.action = "([^"]+)".*?field\.value = ("(?:[^"\\]|\\.)*");', re.S)


@dataclass
class Page:
    url: str
    title: str
    ok: bool = True


@dataclass
class GitHubWeb:
    api: CiGitHub
    owner_of: set[str] = field(default_factory=set)
    signs_in_after: int = 1
    confirm: bool = True
    confirms_after: int = 1
    fail: set[str] = field(default_factory=set)
    signed_in: bool = False
    page: Page = field(default_factory=lambda: Page("about:blank", ""))
    wanted: str = ""
    looks: int = 0
    pending: dict[str, Any] | None = None
    confirming: bool = False
    installing: int | None = None
    seen: list[str] = field(default_factory=list)

    def _show(self, url: str, title: str) -> Page:
        self.page = Page(url, title)
        self.seen.append(url)
        return self.page

    def _open(self, url: str) -> Page:
        parts = urlsplit(url)
        if not self.signed_in:
            self.wanted = url
            return self._show(f"{WEB}/login?{urlencode({'return_to': parts.path})}", "Sign in to GitHub · GitHub")
        match = re.fullmatch(r"/organizations/([^/]+)/settings/apps/new", parts.path)
        if match and match.group(1).lower() in self.owner_of:
            return self._show(url, "Register new GitHub App")
        match = re.fullmatch(r"/apps/([^/]+)/installations/new/permissions", parts.path)
        target = parse_qs(parts.query).get("target_id", [""])[0]
        if match and self.api.app is not None and match.group(1) == self.api.app["slug"] and target.isdigit():
            self.installing = int(target)
            return self._show(url, f"Install {self.api.app['manifest']['name']}")
        return self._show(f"{WEB}/404", "Page not found · GitHub")

    def _snapshot(self) -> Page:
        self.looks += 1
        if not self.signed_in and self.looks >= self.signs_in_after:
            self.signed_in = True
            self.looks = 0
            return self._open(self.wanted)
        if self.confirming and self.looks >= self.confirms_after:
            self.confirming = False
            self.looks = 0
            return self._created()
        return self.page

    def _created(self) -> Page:
        assert self.pending is not None
        owner, manifest, state = self.pending["owner"], self.pending["manifest"], self.pending["state"]
        code = self.api.create_app(owner, manifest)
        self.pending = None
        return self._show(f"{manifest['redirect_url']}?{urlencode({'code': code, 'state': state})}", "GitHub Apps")

    def _evaluate(self, expression: str) -> Page:
        found = FORM.search(expression)
        if found is None:
            return Page(self.page.url, self.page.title, ok=False)
        action = urlsplit(found.group(1))
        owner = action.path.split("/")[2]
        manifest = json.loads(json.loads(found.group(2)))
        self.pending = {"owner": owner, "manifest": manifest, "state": parse_qs(action.query)["state"][0]}
        return self._show(f"{WEB}/organizations/{owner}/settings/apps/new", f"Create GitHub App for {owner}")

    def _click(self, target: str) -> Page:
        if target == "text=Create GitHub App" and self.pending is not None:
            if self.confirm:
                self.confirming = True
                self.looks = 0
                return self._show(f"{WEB}/sessions/sudo", "Confirm access")
            return self._created()
        if target == "text=Install" and self.installing is not None:
            ident = self.api.install(self.installing)
            self.installing = None
            login = self.api.installations[ident].account
            return self._show(f"{WEB}/organizations/{login}/settings/installations/{ident}", "Installed GitHub App")
        return Page(self.page.url, self.page.title, ok=False)

    async def act(self, tool: str, arguments: Mapping[str, Any]) -> Page:
        if tool in self.fail:
            return Page(self.page.url, self.page.title, ok=False)
        if tool == "browser_navigate":
            return self._open(str(arguments["url"]))
        if tool == "browser_evaluate":
            return self._evaluate(str(arguments["expression"]))
        if tool == "browser_click":
            return self._click(str(arguments["target"]))
        return self._snapshot()
