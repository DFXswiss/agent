from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli.main import cmd_query, cmd_subscribe, main
from agent_cli.store import Store


class FakeHub:
    def __init__(self) -> None:
        self.put_calls: list[list[dict[str, Any]]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.get_calls = 0
        self.query_result: Any = {"rows": []}
        self.get_result: Any = {"subscriptions": []}
        self.put_result: Any = {"ok": True}

    def put_subscriptions(self, subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        self.put_calls.append(subscriptions)
        return self.put_result

    def get_subscriptions(self) -> Any:
        self.get_calls += 1
        return self.get_result

    def query(self, match: dict[str, Any]) -> Any:
        self.query_calls.append(match)
        return self.query_result

    def close(self) -> None:
        return None


def _pair(store: Store) -> None:
    store.set_meta("hub_url", "http://127.0.0.1:9")
    store.set_meta("device_token", "tok")


def test_query_prints_hub_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    _pair(store)
    store.close()
    hub = FakeHub()
    hub.query_result = {
        "rows": [
            {
                "table": "activity",
                "row_id": "r1",
                "payload": {"type": "message"},
            }
        ]
    }
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)
    match_path = tmp_path / "match.json"
    match_path.write_text(json.dumps({"type": "message"}), encoding="utf-8")
    cmd_query(["--match-file", str(match_path)])
    out = capsys.readouterr().out
    assert json.loads(out) == hub.query_result
    assert hub.query_calls == [{"type": "message"}]


def test_subscribe_set_clear_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    _pair(store)
    store.close()
    hub = FakeHub()
    hub.get_result = {"subscriptions": [{"match": {"type": "message"}}]}
    hub.put_result = {"ok": True, "n": 1}
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _store: hub)

    set_path = tmp_path / "subs.json"
    set_path.write_text(
        json.dumps({"subscriptions": [{"match": {"type": "pr.open"}}]}),
        encoding="utf-8",
    )
    cmd_subscribe(["set", "--file", str(set_path)])
    assert hub.put_calls == [[{"match": {"type": "pr.open"}}]]
    assert json.loads(capsys.readouterr().out) == hub.put_result

    list_path = tmp_path / "subs-list.json"
    list_path.write_text(
        json.dumps([{"match": {"type": "comment.post"}}]),
        encoding="utf-8",
    )
    cmd_subscribe(["set", "--file", str(list_path)])
    assert hub.put_calls[-1] == [{"match": {"type": "comment.post"}}]
    capsys.readouterr()

    cmd_subscribe(["clear"])
    assert hub.put_calls[-1] == []
    assert json.loads(capsys.readouterr().out)["ok"] is True

    cmd_subscribe(["list"])
    assert hub.get_calls == 1
    assert json.loads(capsys.readouterr().out) == hub.get_result


def test_query_subscribe_without_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Store(tmp_path).close()
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    match_path = tmp_path / "match.json"
    match_path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="not paired"):
        main(["query", "--match-file", str(match_path)])
    with pytest.raises(SystemExit, match="not paired"):
        main(["subscribe", "list"])


def test_query_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    _pair(store)
    store.close()
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid JSON"):
        cmd_query(["--match-file", str(bad)])
