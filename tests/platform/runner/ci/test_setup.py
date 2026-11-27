"""`agent-ci setup`: the GitHub App "agent CI" created through GitHub's app-manifest flow in the platform browser and
installed in every organisation, against doubles of GitHub's web pages (`agent_doubles.ci.web`) and API
(`agent_doubles.ci.github`) (spec 16 section 17.9)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_doubles.ci.github import CiGitHub
from agent_doubles.ci.web import GitHubWeb
from agent_runner.ci import controller as controller_module
from agent_runner.ci.controller import main
from agent_runner.ci.link import LinkDown
from agent_runner.ci.repos import load
from agent_runner.ci.setup import (
    PERMISSIONS,
    App,
    AppApi,
    AppSetup,
    Page,
    RunnerBrowser,
    SetupFailed,
    code_of,
    manifest,
    person_needed,
    update_config,
    write_private,
)
from agent_runner.workvm.browser import redact_url

from .kit import config_data, write_config

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
SECOND = "second-org"


class Browser:
    """The `Browser` port over the web double."""

    def __init__(self, web: GitHubWeb) -> None:
        self.web = web
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def act(self, tool: str, arguments: Mapping[str, Any]) -> Page:
        self.calls.append((tool, dict(arguments)))
        page = await self.web.act(tool, arguments)
        return Page(page.url, page.title, page.ok)


def _github() -> CiGitHub:
    double = CiGitHub("unset", pem=PEM.decode())
    double.orgs = {"example-org": 11, SECOND: 22}
    return double


def _api(double: CiGitHub, fail: Exception | None = None) -> AppApi:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail is not None:
            raise fail
        status, headers, body = double.handle(request.method, request.url.path, dict(request.headers))
        return httpx.Response(status, headers=headers, content=body)

    return AppApi("https://api.github.test", transport=httpx.MockTransport(handler))


def _flow(tmp_path: Path, double: CiGitHub, web: GitHubWeb, **changes: Any) -> tuple[AppSetup, list[str], list[float]]:
    lines: list[str] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    flow = AppSetup(Browser(web), _api(double), tmp_path / "app.pem", lines.append, sleep=sleep, **changes)
    return flow, lines, slept


def test_the_manifest_asks_only_for_the_runner_permission() -> None:
    document = manifest("example-org", public=True)
    assert document["default_permissions"] == PERMISSIONS == {"organization_self_hosted_runners": "write"}
    assert document["default_events"] == []
    assert document["hook_attributes"]["active"] is False
    assert document["redirect_url"] == "https://github.com/organizations/example-org/settings/apps"


def test_the_app_is_created_and_installed_with_a_person_where_github_wants_one(tmp_path: Path) -> None:
    double = _github()
    web = GitHubWeb(double, owner_of={"example-org"}, signs_in_after=2, confirms_after=1)
    flow, lines, slept = _flow(tmp_path, double, web)

    async def main_() -> dict[str, int]:
        app = await flow.create("example-org", public=True)
        assert app == App(4242, "agent-ci", "Iv23-created")
        return await flow.install(app, ["example-org", SECOND])

    installations = asyncio.run(main_())
    assert installations == {"example-org": 7000, SECOND: 7001}
    key = tmp_path / "app.pem"
    assert key.read_bytes() == PEM
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert lines[0] == "ci.setup person_needed step=open reason=sign_in"
    assert "ci.setup person_needed step=create reason=confirm" in lines
    assert "ci.setup app_created app_id=4242 slug=agent-ci" in lines
    assert lines[-1] == "ci.setup installed organisations=2"
    assert slept == [10.0, 10.0, 10.0]
    assert not any("PRIVATE KEY" in line or "code" in line.split("step=")[0] for line in lines)
    assert double.app is not None
    assert double.app["manifest"]["default_permissions"] == PERMISSIONS
    assert double.codes == {}  # the one-time code is used up
    redirect = next(url for url in web.seen if "code=" in url)
    assert "code=redacted" in redact_url(redirect)
    assert "state=redacted" in redact_url(redirect)
    assert redact_url("https://github.com/apps/x?tab=1") == "https://github.com/apps/x?tab=1"
    assert ("POST", "/app-manifests/code0001x/conversions") in double.journal


def test_the_configuration_gets_the_app_and_the_installations(tmp_path: Path) -> None:
    second = dict(config_data()["repositories"][0], repo=f"{SECOND}/other", scale_set="agent-ci-other")
    path = write_config(
        tmp_path / "ci.json",
        app={"key_file": str(tmp_path / "app.pem")},
        repositories=[{k: v for k, v in config_data()["repositories"][0].items() if k != "installation_id"}, second],
    )
    before = load(path)
    assert not before.ready
    assert before.organisations == ["example-org", SECOND]
    update_config(path, App(1, "agent-ci", "Iv23-new"), {"example-org": 5, SECOND: 6})
    after = load(path)
    assert after.ready
    assert after.client_id == "Iv23-new"
    assert [entry.installation_id for entry in after.repositories] == [5, 6]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_person_who_does_not_come_ends_the_setup(tmp_path: Path) -> None:
    double = _github()
    web = GitHubWeb(double, owner_of={"example-org"}, signs_in_after=99)
    flow, lines, _slept = _flow(tmp_path, double, web, person_wait_s=20.0)
    with pytest.raises(SetupFailed, match="person_timeout"):
        asyncio.run(flow.create("example-org", public=False))
    assert lines == ["ci.setup person_needed step=open reason=sign_in"]


def test_a_person_who_is_no_owner_and_a_failed_installation(tmp_path: Path) -> None:
    double = _github()
    web = GitHubWeb(double, owner_of=set(), signs_in_after=0, confirm=False)
    flow, _lines, _slept = _flow(tmp_path, double, web)
    with pytest.raises(SetupFailed, match="not_an_owner"):
        asyncio.run(flow.create("example-org", public=False))
    web.owner_of.add("example-org")
    app = asyncio.run(flow.create("example-org", public=False))
    web.fail.add("browser_click")
    with pytest.raises(SetupFailed, match="not_installed"):
        asyncio.run(flow.install(app, ["example-org"]))


def test_the_redirect_must_carry_the_own_state_and_a_code() -> None:
    url = "https://github.com/organizations/o/settings/apps"
    assert code_of(f"{url}?code=abcdefgh&state=s1", "s1") == "abcdefgh"
    for bad, code in (
        (f"{url}?code=abcdefgh&state=other", "state_mismatch"),
        (f"{url}?code=abcdefgh", "code_missing"),
        (f"{url}?state=s1", "code_missing"),
        (f"{url}?code=a%20b&state=s1", "code_missing"),
    ):
        with pytest.raises(SetupFailed, match=code):
            code_of(bad, "s1")


def test_pages_that_need_a_person() -> None:
    assert person_needed(Page("https://github.com/login?return_to=x", "x")) == "sign_in"
    assert person_needed(Page("https://github.com/sessions/two-factor", "x")) == "sign_in"
    assert person_needed(Page("https://github.com/x", "Confirm access")) == "confirm"
    assert person_needed(Page("https://github.com/x", "Use your passkey")) == "sign_in"
    assert person_needed(Page("https://example.test/login", "Example")) is None
    assert person_needed(Page("https://github.com/apps/x", "Install")) is None


@pytest.mark.parametrize(
    ("path", "fault"),
    [
        ("/app-manifests/code0001x/conversions", (500, {"message": "boom"})),
        ("/app-manifests/code0001x/conversions", (201, "not json")),
        ("/app-manifests/code0001x/conversions", (201, ["no object"])),
        ("/app-manifests/code0001x/conversions", (201, {"id": 1, "slug": "x", "client_id": "c", "pem": "no key"})),
    ],
)
def test_a_conversion_that_does_not_fit_writes_no_key(tmp_path: Path, path: str, fault: tuple[int, Any]) -> None:
    double = _github()
    double.create_app("example-org", manifest("example-org", public=False))
    double.faults[path] = [fault]
    with pytest.raises(SetupFailed):
        asyncio.run(_api(double).convert("code0001x", tmp_path / "app.pem"))
    assert not (tmp_path / "app.pem").exists()


def test_api_answers_that_do_not_fit(tmp_path: Path) -> None:
    double = _github()
    with pytest.raises(SetupFailed, match="github_unreachable"):
        asyncio.run(_api(double, httpx.ConnectError("down")).organisation_id("example-org"))
    with pytest.raises(SetupFailed, match="github_404"):
        asyncio.run(_api(double).organisation_id("unknown-org"))
    double.faults["/orgs/example-org"] = [(200, {"id": "x"})]
    with pytest.raises(SetupFailed, match="github_answer_invalid"):
        asyncio.run(_api(double).organisation_id("example-org"))
    double.client_id = "Iv23-test"
    double.faults["/app/installations"] = [(200, {"no": "list"})]
    with pytest.raises(SetupFailed, match="github_answer_invalid"):
        asyncio.run(_api(double).installations("Iv23-test", PEM))
    double.faults["/app/installations"] = [(200, [{"id": 1, "account": {"login": "Example-Org"}}, {"id": "x"}, 5])]
    assert asyncio.run(_api(double).installations("Iv23-test", PEM)) == {"example-org": 1}
    assert double.handle("POST", "/app-manifests/nope/conversions", {"Authorization": "x"})[0] == 400
    assert double.handle("POST", "/app-manifests/nope/conversions", {})[0] == 404
    assert double.handle("GET", "/app/installations", {})[0] == 401


def test_a_failed_write_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_source: Any, _target: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="read-only"):
        write_private(tmp_path / "key", b"x")
    assert list(tmp_path.iterdir()) == []


def test_the_browser_port_over_the_runner() -> None:
    class Link:
        async def call(self, op: str, **body: Any) -> dict[str, Any]:
            assert op == "browser"
            assert body["owner"] == {"task_id": "t"}
            return {"url": "https://github.com/x", "title": 5, "ok": True}

    page = asyncio.run(RunnerBrowser(Link(), {"task_id": "t"}).act("browser_snapshot", {}))
    assert page == Page("https://github.com/x", "", True)

    class Empty:
        async def call(self, op: str, **body: Any) -> dict[str, Any]:
            return {}

    assert asyncio.run(RunnerBrowser(Empty(), {}).act("browser_snapshot", {})) == Page("", "", False)


def test_the_web_double_refuses_what_github_refuses(tmp_path: Path) -> None:
    double = _github()
    web = GitHubWeb(double, owner_of={"example-org"}, signs_in_after=0)
    assert asyncio.run(web.act("browser_snapshot", {})).url.startswith("https://github.com/organizations") is False
    web.signed_in = True
    assert asyncio.run(web.act("browser_navigate", {"url": "https://github.com/nowhere"})).title.startswith("Page")
    assert asyncio.run(web.act("browser_evaluate", {"expression": "1"})).ok is False
    assert asyncio.run(web.act("browser_click", {"target": "text=Other"})).ok is False


def test_controller_setup_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    double = _github()
    web = GitHubWeb(double, owner_of={"example-org"}, confirm=False, signed_in=True)
    entry = {k: v for k, v in config_data()["repositories"][0].items() if k != "installation_id"}
    path = write_config(tmp_path / "ci.json", app={"key_file": str(tmp_path / "app.pem")}, repositories=[entry])
    config = load(path)
    lines: list[str] = []

    class Link:
        async def call(self, op: str, **body: Any) -> dict[str, Any]:
            page = await web.act(body["tool"], body["arguments"])
            return {"url": page.url, "title": page.title, "ok": page.ok}

    assert asyncio.run(controller_module.setup(config, path, Link(), lines.append, _api(double))) == 0
    assert lines[-1] == "ci.setup done"
    assert load(path).ready

    class Down:
        async def call(self, op: str, **body: Any) -> dict[str, Any]:
            raise LinkDown("FileNotFoundError")

    failed: list[str] = []
    assert asyncio.run(controller_module.setup(config, path, Down(), failed.append, _api(double))) == 1
    assert failed == ["ci.setup failed code=LinkDown"]
    # the command line: `setup` runs the flow, a configuration without the app does not serve
    unready = write_config(tmp_path / "unready.json", app={"key_file": str(tmp_path / "app.pem")}, repositories=[entry])
    assert main(["--config", str(unready)]) == 2
    assert "is not set up" in capsys.readouterr().out
    seen: list[Any] = []

    def run(coroutine: Any) -> int:
        seen.append(coroutine.__qualname__)
        coroutine.close()
        return 0

    assert main(["--config", str(unready), "setup"], run=run) == 0
    assert seen == ["setup"]
    assert json.loads(unready.read_text(encoding="utf-8"))["app"] == {"key_file": str(tmp_path / "app.pem")}
