"""`pr link|list|show|unlink|fix-conflicts` against the real hub (KOR-48, ADR-084 sections 1, 3 and 6;
11-agent-access.md section 10): the sealed link the tool builds is stored and served by `agent_hub`'s own routes and
opened again by the tool, the hub never sees the repository or the number, and the platform instruction reaches the
task as an ordinary prompt."""

from __future__ import annotations

from agent_platform import pulls

from .support import RealHub, pair, person, run_ok


def test_link_list_show_fix_and_unlink(real: RealHub) -> None:
    ada = person(real, "Ada")
    pair(real, ada)
    org = str(run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")["org_id"])
    wg = str(run_ok(ada.tool, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    task = str(run_ok(ada.tool, "task", "create", "--wg", wg, "--text", "Fix the login bug")["task_id"])
    other = str(run_ok(ada.tool, "task", "create", "--wg", wg, "--text", "Other work")["task_id"])
    address = "https://github.com/Octo-Org/Login.Service/pull/42"
    linked = run_ok(ada.tool, "pr", "link", address, "--task", task)
    assert linked["created"] is True
    link = linked["link"]
    assert (link["task_id"], link["linked_by"], link["origin"]) == (task, ada.id, "person")
    assert (link["repo"], link["number"], link["epoch"]) == ("Octo-Org/Login.Service", 42, 1)
    assert (link["title"], link["details"]) == ("Fix the login bug", "available")
    with real.harness.pool.connection() as connection:
        stored = connection.execute("SELECT sealed, linked_by FROM hub.pr_link WHERE wg_id = %s", (wg,)).fetchall()
    assert len(stored) == 1
    assert "Login" not in str(stored)
    # linked already: reported with its task, also for the short form in another spelling
    again = run_ok(ada.tool, "pr", "link", "octo-org/login.service#42", "--task", other)
    assert (again["created"], again["link"]["task_id"]) == (False, task)
    listed = run_ok(ada.tool, "pr", "list", "--wg", wg, "--no-status")
    assert [(item["repo"], item["number"], item["task_id"]) for item in listed["links"]] == [
        ("Octo-Org/Login.Service", 42, task)
    ]
    assert listed["unverified"] == 0
    assert run_ok(ada.tool, "pr", "list", "--task", other, "--no-status")["links"] == []
    # without a device the state is not read, the list still comes
    assert run_ok(ada.tool, "pr", "list", "--org", org)["links"][0]["status_error"] == "no_device"
    shown = run_ok(ada.tool, "pr", "show", address, "--no-status")
    assert [item["task_id"] for item in shown["links"]] == [task]
    assert run_ok(ada.tool, "pr", "show", "Octo-Org/Login.Service#43", "--no-status")["links"] == []
    # the instruction as a prompt of the linked task
    sent = run_ok(ada.tool, "pr", "fix-conflicts", address, "--anyway")
    assert sent["task_id"] == task
    text = pulls.instruction("Octo-Org/Login.Service", 42)
    events = run_ok(ada.tool, "task", "show", "--task", task)["events"]
    assert events[-1]["body"]["text"] == text
    # removed again
    unlinked = run_ok(ada.tool, "pr", "unlink", address)
    assert ([item["task_id"] for item in unlinked["unlinked"]], unlinked["kept"]) == ([task], [])
    assert run_ok(ada.tool, "pr", "list", "--wg", wg, "--no-status")["links"] == []
    code, missing = ada.tool.run("pr", "unlink", address)
    assert (code, missing["error"]) == (1, "pr_not_linked")
