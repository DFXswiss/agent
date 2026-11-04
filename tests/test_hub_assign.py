from __future__ import annotations

import json

import httpx
import pytest

from agent_cli.hub import Hub
from agent_cli.main import main

pytestmark = pytest.mark.no_pg


class FakeStore:
    def __init__(self, *, paired: bool = True) -> None:
        self.values = {
            "hub_url": "https://hub.example" if paired else None,
            "device_token": "secret-device-token" if paired else None,
        }

    def meta(self, key: str):
        return self.values.get(key)

    def close(self) -> None:
        return None


def test_assign_success_prints_issue_without_token(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-device-token"
        assert request.url.path == "/api/assignments"
        assert json.loads(request.content)["repo"] == "Example/Project"
        return httpx.Response(
            201,
            json={
                "repo": "Example/Project",
                "number": 42,
                "url": "https://github.com/Example/Project/issues/42",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", "secret-device-token", client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)
    main([
        "assign", "--agent", "worker-bot", "--repo", "Example/Project",
        "--title", "Fix widget", "--body", "Detailed mandate",
    ])
    captured = capsys.readouterr()
    assert captured.out == (
        "assigned Example/Project#42 https://github.com/Example/Project/issues/42\n"
    )
    assert "secret-device-token" not in captured.out + captured.err
    client.close()


def test_assign_failure_prints_detail_and_recovery_url_without_token(monkeypatch, capsys):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={"detail": "assignee verification failed", "url": "https://github.com/x/y/issues/9"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", "secret-device-token", client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)
    with pytest.raises(SystemExit) as info:
        main([
            "assign", "--agent", "worker-bot", "--repo", "x/y",
            "--title", "Fix", "--body", "Details",
        ])
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert "assignee verification failed" in captured.err
    assert "https://github.com/x/y/issues/9" in captured.err
    assert "secret-device-token" not in captured.out + captured.err
    client.close()


def test_assign_unpaired_device_has_clear_error(monkeypatch):
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore(paired=False))
    with pytest.raises(SystemExit, match="device is not paired"):
        main([
            "assign", "--agent", "worker-bot", "--repo", "x/y",
            "--title", "Fix", "--body", "Details",
        ])


@pytest.mark.parametrize(
    "argv,error",
    [
        (["--repo", "bad", "--title", "Fix", "--body", "Details"], "--agent is required"),
        (["--agent", "bot", "--repo", "bad", "--title", "Fix", "--body", "Details"], "Owner/Name"),
        (["--agent", "bot", "--repo", "x/y", "--title", "", "--body", "Details"], "--title is required"),
        (["--agent", "bot", "--repo", "x/y", "--title", "Fix", "--body", ""], "body must"),
    ],
)
def test_assign_validates_before_http(monkeypatch, argv, error):
    monkeypatch.setattr(
        "agent_cli.main.open_store",
        lambda: (_ for _ in ()).throw(AssertionError("HTTP/store must not be reached")),
    )
    with pytest.raises(SystemExit, match=error):
        main(["assign", *argv])
