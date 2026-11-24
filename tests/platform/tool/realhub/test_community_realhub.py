"""The community commands against the real hub (14-community.md, ADR-069): the key is created, requested, served and
opened by two agents, messages are posted, read, deleted and moderated, and the post limit answers with its wait, all
checked by the hub's own code."""

from __future__ import annotations

from agent_platform import community

from .support import RealHub, pair, person, run_ok


def test_two_agents_use_the_community(real: RealHub) -> None:
    ada, bob = person(real, "Ada"), person(real, "Bob")
    for who in (ada, bob):
        pair(real, who)
    assert run_ok(ada.tool, "community", "key")["key"] == "created"
    assert run_ok(bob.tool, "community", "key")["key"] == "waiting"
    served = run_ok(ada.tool, "community", "serve")
    assert (served["offered"], served["stored"]) == (1, 1)
    assert run_ok(bob.tool, "community", "key")["key"] == "opened"
    assert run_ok(bob.tool, "community", "status")["key_held"] is True
    posted = run_ok(bob.tool, "community", "post", "--text", "Hello\nworld", "--name", "Bob")
    read = run_ok(ada.tool, "community", "messages", "--mark-read")
    item = read["messages"][-1]
    assert (item["status"], item["name"], item["text"], item["author"], item["mark"]) == (
        "verified",
        "Bob",
        "Hello\nworld",
        bob.id,
        community.mark(bob.id),
    )
    assert read["last_read_seq"] == posted["seq"]
    assert run_ok(ada.tool, "community", "status")["unread"] == 0
    # moderation: the operator grants the role, the moderator mutes, unmutes and removes
    granted = real.harness.root.post("/v1/community/moderators", json={"user": ada.id, "granted": True})
    assert granted.status_code == 200, granted.text
    assert run_ok(ada.tool, "community", "mute", "--user", bob.id)["muted"] is True
    assert [row["user"] for row in run_ok(ada.tool, "community", "mutes")["mutes"]] == [bob.id]
    code, refused = bob.tool.run("community", "post", "--text", "more")
    assert (code, refused["hub_error"]) == (1, "community_muted")
    assert run_ok(ada.tool, "community", "unmute", "--user", bob.id)["muted"] is False
    assert run_ok(ada.tool, "community", "delete", "--id", posted["id"])["deleted"] == "moderator"
    assert run_ok(bob.tool, "community", "messages")["messages"][-1]["status"] == "removed"
    # the post limit (5, then one every 6 s) answers with the wait; authors delete their own messages
    ids: list[str] = []
    for number in range(10):
        code, document = bob.tool.run("community", "post", "--text", f"message {number}")
        if code:
            break
        ids.append(document["id"])
    assert (document["hub_error"], document["status"]) == ("rate_limited", 429), document
    assert isinstance(document["retry_after"], int)
    assert document["retry_after"] > 0
    assert ids
    assert run_ok(bob.tool, "community", "delete", "--id", ids[0])["deleted"] == "author"
