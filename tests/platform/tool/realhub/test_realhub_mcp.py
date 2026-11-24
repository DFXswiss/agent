"""A chat program on the real hub (KOR-63): the tool's MCP server, spoken to over stdio as a separate process, finds
the command with `search`, creates a task with `run` (the prompt as the call's stdin text) and reads it back, with
the same keys and the same result as the command line."""

from __future__ import annotations

from ..test_mcp import INIT, call, stdio
from .support import RealHub, pair, person, run_ok


def test_a_chat_program_creates_a_task_and_reads_it(real: RealHub) -> None:
    ada = person(real, "Ada")
    pair(real, ada)
    org = str(run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")["org_id"])
    wg = str(run_ok(ada.tool, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    real.fleet.platform_device()
    real.fleet.platform.post({"type": "org_platform_access", "org": org, "granted": True})
    run_ok(ada.tool, "device", "assign", "--wg", wg)
    created_by_mcp = call(
        3, "run", {"args": ["task", "create", "--wg", wg, "--text", "-"], "stdin": "Fix the login bug"}
    )
    replies, _stderr, code = stdio(
        ada.tool.env,
        [INIT, call(2, "search", {"query": "Aufgabe anlegen"}), created_by_mcp],
    )
    assert code == 0
    assert replies[1]["result"]["structuredContent"]["results"][0]["command"] == "task create"
    created = replies[2]["result"]["structuredContent"]
    assert created["ok"] is True, created
    task = str(created["task_id"])
    shown_by_mcp = call(4, "run", {"args": ["task", "show", "--task", task, "--limit", "1", "--fields", "body"]})
    replies, _stderr, code = stdio(ada.tool.env, [INIT, shown_by_mcp])
    shown = replies[1]["result"]["structuredContent"]
    assert shown["events"] == [{"body": shown["events"][0]["body"]}]
    assert shown["events"][0]["body"]["text"] == "Fix the login bug"
    # the command line sees the same task, opened with the same keys
    assert "Fix the login bug" in str(run_ok(ada.tool, "task", "show", "--task", task))
