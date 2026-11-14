from __future__ import annotations

from pathlib import Path

import pytest

import agent_cli.main as main_module
import agent_cli.store as store_module
from agent_cli.main import main
from agent_cli.preconditions import REGISTRY_KEYS
from agent_cli.store import Store


@pytest.mark.no_pg
@pytest.mark.parametrize("key", sorted(REGISTRY_KEYS))
def test_checklist_set_refuses_registry_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))

    with pytest.raises(
        SystemExit,
        match=(
            rf"{key} is a coordinator precondition check; "
            r"its resolver or agent order override closes it"
        ),
    ):
        main(
            [
                "checklist",
                "set",
                "--task",
                "t",
                "--key",
                key,
                "--status",
                "ja",
                "--source",
                "script",
                "--evidence",
                "x",
            ]
        )


@pytest.mark.no_pg
@pytest.mark.parametrize("key", sorted(REGISTRY_KEYS))
def test_close_step_refuses_registry_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))

    with pytest.raises(
        SystemExit,
        match=(
            rf"{key} is a coordinator precondition check; "
            r"its resolver or agent order override closes it"
        ),
    ):
        main(
            [
                "close-step",
                "--task",
                "t",
                "--key",
                key,
                "--source",
                "script",
                "--evidence",
                "x",
            ]
        )


@pytest.mark.no_pg
def test_order_types_are_script_only_and_not_executable() -> None:
    order_types = {"order.event", "order.reply"}

    assert order_types <= main_module.ACTIVITY_TYPES
    assert order_types <= main_module.SCRIPT_ONLY_ACTIVITY
    assert order_types.isdisjoint(store_module.WAKE_ACTIVITY_TYPES)
    assert order_types.isdisjoint(store_module.EXECUTABLE_ACTIVITY_TYPES)


@pytest.mark.parametrize("typ", ["order.event", "order.reply"])
def test_activity_add_refuses_order_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typ: str
) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    main(["init"])
    main(["session", "register", "--id", "sess-1", "--kind", "human"])
    payload = tmp_path / "order.json"
    payload.write_text("{}", encoding="utf-8")

    with pytest.raises(
        SystemExit,
        match=rf"{typ} is written by a script, not agent activity add",
    ):
        main(
            [
                "activity",
                "add",
                "--session",
                "sess-1",
                "--type",
                typ,
                "--payload-file",
                str(payload),
            ]
        )

    store = Store(tmp_path)
    try:
        assert all(row.get("type") != typ for row in store.rows("activity"))
    finally:
        store.close()
