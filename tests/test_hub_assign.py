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
        sent = json.loads(request.content)
        assert sent["repo"] == "Example/Project"
        assert sent["deviation"] == {"declared": False}
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
        "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "Example/Project",
        "--title", "Fix widget", "--body", "Detailed mandate",
    ])
    captured = capsys.readouterr()
    assert captured.out == (
        "assigned Example/Project#42 https://github.com/Example/Project/issues/42\n"
    )
    assert "secret-device-token" not in captured.out + captured.err
    client.close()


def test_assign_body_file_reaches_assignment_api(monkeypatch, capsys, tmp_path):
    body_path = tmp_path / "mandate.md"
    body_path.write_text("Mandate from file\nwith details.\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/assignments"
        assert json.loads(request.content)["body"] == "Mandate from file\nwith details.\n"
        return httpx.Response(
            201,
            json={
                "repo": "Example/Project",
                "number": 43,
                "url": "https://github.com/Example/Project/issues/43",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", "secret-device-token", client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)

    main([
        "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "Example/Project",
        "--title", "Fix widget", "--body-file", str(body_path),
    ])

    assert "assigned Example/Project#43" in capsys.readouterr().out
    client.close()


def test_assign_unreadable_body_file_fails_before_store_or_http(monkeypatch, tmp_path):
    missing = tmp_path / "missing.md"
    monkeypatch.setattr(
        "agent_cli.main.open_store",
        lambda: (_ for _ in ()).throw(AssertionError("store/HTTP must not be reached")),
    )

    with pytest.raises(SystemExit, match="cannot read body file"):
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "Example/Project",
            "--title", "Fix widget", "--body-file", str(missing),
        ])


def test_assign_refuses_body_and_body_file_before_store_or_http(monkeypatch, tmp_path):
    body_path = tmp_path / "mandate.md"
    body_path.write_text("file body", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.open_store",
        lambda: (_ for _ in ()).throw(AssertionError("store/HTTP must not be reached")),
    )

    with pytest.raises(SystemExit, match="exactly one of --body or --body-file"):
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "Example/Project",
            "--title", "Fix widget", "--body", "inline body",
            "--body-file", str(body_path),
        ])


def test_assign_failure_prints_detail_and_recovery_url_without_token(monkeypatch, capsys):
    token = "secret-device-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "detail": (
                    f"assignee verification failed for Authorization: Bearer {token}; "
                    f"see https://hub.example/recover?token={token}\x1b[0m"
                ),
                "url": f"https://github.com/x/y/issues/9?device_token={token}",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", "secret-device-token", client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)
    with pytest.raises(SystemExit) as info:
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "x/y",
            "--title", "Fix", "--body", "Details",
        ])
    assert info.value.code == 1
    captured = capsys.readouterr()
    emitted = captured.out + captured.err
    assert "assignee verification failed" in captured.err
    assert "https://github.com/x/y/issues/9" in emitted
    assert "[redacted]" in emitted
    assert "\x1b" not in emitted
    assert token not in emitted
    client.close()


def test_assignment_failure_value_removes_echoed_bearer_and_control_chars() -> None:
    from agent_cli.hub_assign import AssignmentFailure, sanitized_assignment_text

    token = "secret-device-token"
    detail = sanitized_assignment_text(
        f"Bearer {token} at https://hub.example/retry?token={token}\x07\x1b[31m",
        token,
        limit=200,
    )
    url = sanitized_assignment_text(
        f"https://github.com/x/y/issues/9?token={token}", token, limit=200
    )
    failure = AssignmentFailure(detail, url)
    assert token not in failure.detail
    assert token not in str(failure.url)
    assert "\x07" not in failure.detail
    assert "\x1b" not in failure.detail
    assert "[redacted]" in failure.detail


def test_assignment_failure_removes_control_interleaved_bearer_before_sanitizing(
    nested_terminal_string_control: str,
) -> None:
    from agent_cli.hub_assign import sanitized_assignment_text

    token = "secret-device-token"
    emitted = sanitized_assignment_text(
        f"hub rejected secret-dev{nested_terminal_string_control}ice-token",
        token,
        limit=200,
    )

    assert token not in emitted
    assert "secret-dev" not in emitted
    assert "ice-token" not in emitted
    assert emitted == "hub rejected [redacted]"
    encoded = "".join(f"%{byte:02x}" for byte in token.encode("utf-8"))
    split_encoded = f"%{nested_terminal_string_control}{encoded[1:]}"
    assert sanitized_assignment_text(
        f"retry {split_encoded}",
        token,
        limit=200,
    ) == "retry [redacted]"
    assert sanitized_assignment_text(
        "retry Bearer%20secret-device-token",
        token,
        limit=200,
    ) == "retry [redacted]"


def test_assignment_sanitizer_uses_one_bearer_helper_for_both_passes(
    monkeypatch,
) -> None:
    import agent_cli.hub_assign as hub_assign

    calls: list[str] = []
    original = hub_assign._without_bearer

    def recording_without_bearer(text: str, token: str | None) -> str:
        calls.append(text)
        return original(text, token)

    monkeypatch.setattr(hub_assign, "_without_bearer", recording_without_bearer)

    emitted = hub_assign.sanitized_assignment_text(
        "retry Bearer secret-device-token",
        "secret-device-token",
        limit=200,
    )

    assert emitted == "retry [redacted]"
    assert len(calls) == 2


def test_assignment_failure_redacts_fully_and_mixed_percent_encoded_path_bearers() -> None:
    from agent_cli.hub_assign import sanitized_assignment_text

    token = "secret-device-token"
    fully_encoded = "".join(f"%{byte:02X}" for byte in token.encode("utf-8"))
    mixed_encoded = "s%65c%72et%2Ddevice%2dtoken"

    assert sanitized_assignment_text(
        f"/recover/{fully_encoded}", token, limit=200
    ) == "/recover/[redacted]"
    assert sanitized_assignment_text(
        f"/recover/{mixed_encoded}", token, limit=200
    ) == "/recover/[redacted]"


def test_assign_failure_prints_fully_percent_encoded_recovery_path_without_bearer(
    monkeypatch, capsys
) -> None:
    token = "secret-device-token"
    encoded = "".join(f"%{byte:02x}" for byte in token.encode("utf-8"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "detail": "assignment recovery required",
                "url": f"https://hub.example/recover/{encoded}",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", token, client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)

    with pytest.raises(SystemExit):
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "x/y",
            "--title", "Fix", "--body", "Details",
        ])

    emitted = capsys.readouterr().err
    assert emitted == (
        "assignment recovery required\n"
        "https://hub.example/recover/[redacted]\n"
    )
    assert token not in emitted
    assert encoded not in emitted
    client.close()


def test_assignment_failure_redacts_generic_token_before_terminal_sanitizing() -> None:
    from agent_cli.hub_assign import sanitized_assignment_text

    split_token = "github_\x1b[31pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    emitted = sanitized_assignment_text(
        f"hub rejected {split_token}", None, limit=2000
    )

    assert emitted == "hub rejected [redacted]"


_ISSUE_URL = "https://github.com/Example/Project/issues/{number}"


@pytest.mark.parametrize(
    ("number", "url"),
    [
        (0, _ISSUE_URL.format(number=0)),
        (-1, _ISSUE_URL.format(number=1)),
        (42, ""),
        (42, "not-a-url"),
        (42, "http://github.com/Example/Project/issues/42"),
        (42, "https://github.com/x/y/issues/42"),
        (42, "https://github.com/Example/Project/issues/42?x=1"),
        (42, "https://github.com/Example/Project/issues/42/"),
        (42, "https://github.com/Example/Project/pull/42"),
        (43, _ISSUE_URL.format(number=42)),
        (True, _ISSUE_URL.format(number=42)),
    ],
)
def test_assign_rejects_invalid_success_assignment(monkeypatch, number, url):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"repo": "Example/Project", "number": number, "url": url}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", "secret-device-token", client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)
    with pytest.raises(SystemExit, match="invalid assignment response"):
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "Example/Project",
            "--title", "Fix widget", "--body", "Detailed mandate",
        ])
    client.close()


def test_assign_requires_no_deviation_before_store_or_http(monkeypatch):
    monkeypatch.setattr(
        "agent_cli.main.open_store",
        lambda: (_ for _ in ()).throw(AssertionError("store/HTTP must not be reached")),
    )

    with pytest.raises(
        SystemExit,
        match=r"assign requires --no-deviation \(deviations need an explicit grant on the issue\)",
    ):
        main([
            "assign", "--agent", "worker-bot", "--repo", "Example/Project",
            "--title", "Fix widget", "--body", "Detailed mandate",
        ])


def test_assign_unpaired_device_has_clear_error(monkeypatch):
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore(paired=False))
    with pytest.raises(SystemExit, match="device is not paired"):
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "x/y",
            "--title", "Fix", "--body", "Details",
        ])


@pytest.mark.parametrize(
    "argv,error",
    [
        (["--repo", "bad", "--title", "Fix", "--body", "Details"], "--agent is required"),
        (["--agent", "bot", "--repo", "bad", "--title", "Fix", "--body", "Details"], "Owner/Name"),
        (["--agent", "bot", "--repo", "x/y", "--title", "", "--body", "Details"], "--title is required"),
        (["--agent", "bot", "--repo", "x/y", "--title", "Fix", "--body", ""], "body must"),
        (["--agent", "bot", "--repo", "x/y", "--title", "T" * 257, "--body", "Details"], "title must"),
        (["--agent", "bot", "--repo", "x/y", "--title", "Fix", "--body", "B" * 60001], "body must"),
    ],
)
def test_assign_validates_before_http(monkeypatch, argv, error):
    monkeypatch.setattr(
        "agent_cli.main.open_store",
        lambda: (_ for _ in ()).throw(AssertionError("HTTP/store must not be reached")),
    )
    with pytest.raises(SystemExit, match=error):
        main(["assign", "--no-deviation", *argv])


def test_assign_refuses_a_malformed_success_response(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"repo": "Example/Project", "number": "42"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hub = Hub("https://hub.example", "secret-device-token", client=client)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: FakeStore())
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)
    with pytest.raises(SystemExit, match="invalid assignment response"):
        main([
            "assign", "--no-deviation", "--agent", "worker-bot", "--repo", "Example/Project",
            "--title", "Fix widget", "--body", "Detailed mandate",
        ])
    client.close()
