"""The Community (14-community.md, ADR-069): `community status|key|serve|messages|post|delete|mutes|mute|unmute`
against the hub fake, whose community routes check signatures, wraps and messages with the hub's reference code.
The tool's formats are the twin of the reference: the file is compared and the community vectors open with it."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.community import crypto as reference
from agent_platform import community
from agent_platform import communityfmt as fmt
from agent_platform.account import Account
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import Identity, x25519_private

from .conftest import Tool, make_tool, pair_user
from .fakes.community import Routes, state
from .hubfake import HubFake, Request, User, now_ms_text

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA = json.loads((REPO_ROOT / "contracts" / "community" / "vectors.json").read_text(encoding="ascii"))


def person(hub: HubFake, root: Path, name: str) -> tuple[Tool, User]:
    other = make_tool(root / name.lower())
    user = hub.add_user(display_name=name)
    pair_user(other, hub, user)
    return other, user


def ok(tool: Tool, *argv: str, stdin: str = "") -> dict[str, Any]:
    code, document = tool.run(*argv, stdin=stdin)
    assert (code, document["ok"]) == (0, True), document
    return document


def fails(tool: Tool, *argv: str, stdin: str = "") -> dict[str, Any]:
    code, document = tool.run(*argv, stdin=stdin)
    assert (code, document["ok"]) == (1, False), document
    return document


def local(tool: Tool) -> dict[str, Any]:
    found: dict[str, Any] = json.loads((tool.home / "state.json").read_text()).get("community", {})
    return found


def set_local(tool: Tool, value: object) -> None:
    path = tool.home / "state.json"
    document = json.loads(path.read_text())
    document["community"] = value
    path.write_text(json.dumps(document))


def holder(tool: Tool) -> None:
    """Create the community key with the tool's user (the first client of an empty community)."""
    assert ok(tool, "community", "key") == {"ok": True, "key": "created", "epoch": 1}


def key_of(hub: HubFake, user: User) -> bytes:
    """The community key from the user's self-wrap at the fake."""
    wrap = state(hub).wraps[user.id]
    return reference.unwrap_community_key(user.identity.kem, epoch=1, recipient=user.id, wrap_text=wrap["wrap"])


def served(hub: HubFake, user: User, body: dict[str, Any]) -> Any:
    """`GET /v1/community` as the fake answers it to `user`, changed by `body`."""
    request = Request("GET", "/v1/community", {}, user.id, None, b"")
    return {**Routes(hub).get(request, None)[1], **body}


def sealed(key: bytes, author: str, identity: Identity, text: str, *, epoch: int = 1, id: str | None = None) -> Any:
    header = reference.new_header(id=id or uuid7(), author=author, epoch=epoch, ts=now_ms_text())
    return reference.seal_message(key, header, reference.body_bytes("Eve", text), identity.sig)


def obtain(hub: HubFake, tool: Tool, other: Tool) -> None:
    """`other` requests the key, `tool` (a holder) serves, `other` opens it."""
    assert ok(other, "community", "key")["key"] == "waiting"
    assert ok(tool, "community", "serve")["stored"] >= 1
    assert ok(other, "community", "key")["key"] == "opened"


# --- the formats ---


def test_the_formats_are_the_twin_of_the_hub_reference() -> None:
    def code(path: str) -> str:
        text = (REPO_ROOT / "src" / path).read_text(encoding="utf-8")
        return text[text.index('"""', 3) + 3 :]

    expected = code("agent_hub/community/crypto.py").replace("agent_hub.crypto", "agent_runner.crypto")
    assert code("agent_platform/communityfmt.py") == expected


def test_the_vectors_open_with_the_tool() -> None:
    key = b64u_decode(DATA["community_key"], 32)
    assert b64u_encode(fmt.key_check(key, 1)) == DATA["key_check"]
    author = b64u_decode(DATA["author_sig_pk"], 32)
    request = DATA["request"]
    fmt.verify_request(
        author, id=request["id"], kem_pub=request["kem_pub"], user=DATA["user"], signature=request["sig"]
    )
    assert fmt.request_text(id=request["id"], kem_pub=request["kem_pub"], user=DATA["user"]) == b64u_decode(
        request["text"]
    )
    wrapped = DATA["request_wrap"]
    kem = x25519_private(b64u_decode(request["kem_sk"], 32))
    assert fmt.unwrap_community_key(kem, epoch=1, recipient=request["id"], wrap_text=wrapped["wrap"]) == key
    fmt.verify_community_wrap(
        b64u_decode(wrapped["wrapper_sig_pk"], 32),
        epoch=1,
        recipient=request["id"],
        wrap_text=wrapped["wrap"],
        signature=wrapped["sig"],
    )
    own = DATA["self_wrap"]
    author_kem = x25519_private(b64u_decode(DATA["author_kem_sk"], 32))
    assert fmt.unwrap_community_key(author_kem, epoch=1, recipient=DATA["user"], wrap_text=own["wrap"]) == key
    assert fmt.open_message(key, DATA["message"]["sealed"], author) == DATA["message"]["body"]
    assert fmt.body_bytes(**{k: DATA["message"]["body"][k] for k in ("name", "text")}) == DATA["message"][
        "body_json"
    ].encode("utf-8")


@pytest.mark.parametrize("case", DATA["invalid_messages"], ids=lambda case: case["name"])
def test_invalid_messages(case: dict[str, Any]) -> None:
    with pytest.raises(CryptoError, match=str(case["error"])):
        fmt.verify_message(case["sealed"], b64u_decode(DATA["author_sig_pk"], 32))


@pytest.mark.parametrize("case", DATA["invalid_bodies"], ids=lambda case: case["name"])
def test_invalid_bodies(case: dict[str, Any]) -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        fmt.check_body(case["body"])


def test_what_the_tool_seals_opens_with_the_reference() -> None:
    key, identity, user = os.urandom(32), Identity.generate(), uuid7()
    header = fmt.new_header(id=uuid7(), author=user, epoch=1, ts=now_ms_text())
    message = fmt.seal_message(key, header, fmt.body_bytes("Ada", "Hi"), identity.sig)
    assert reference.open_message(key, message, identity.sig_public()) == {"name": "Ada", "text": "Hi", "v": 1}
    broken = fmt.seal_message(key, header, b"\xff", identity.sig)
    with pytest.raises(CryptoError, match="header_invalid"):
        fmt.open_message(key, broken, identity.sig_public())
    kem = Identity.generate()
    wrap = fmt.wrap_community_key(kem.kem_public(), epoch=1, recipient=user, key=key)
    signature = fmt.sign_community_wrap(identity.sig, epoch=1, recipient=user, wrap_text=wrap)
    reference.verify_community_wrap(identity.sig_public(), epoch=1, recipient=user, wrap_text=wrap, signature=signature)
    assert reference.unwrap_community_key(kem.kem, epoch=1, recipient=user, wrap_text=wrap) == key
    request_id, kem_pub = uuid7(), kem.public()["kem"]
    signature = fmt.sign_request(identity.sig, id=request_id, kem_pub=kem_pub, user=user)
    reference.verify_request(identity.sig_public(), id=request_id, kem_pub=kem_pub, user=user, signature=signature)


def test_mark_and_cleaning() -> None:
    assert community.mark("01a1ee56-5e00-77be-8cf3-1f27bb0e92a2") == "#6DQF"  # web/tests/core/community/text.test.ts
    assert community.clean_name(" Zoe\u0308\u202e ") == "Zo\u00eb"
    assert community.clean_text("\ufeff a\r\nb\rc\u0007\td\u2066 \n") == "a\nb\nc\td"


# --- the key ---


def test_two_users_chat(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    status = ok(tool, "community", "status")
    assert (status["epoch"], status["key_held"], status["request"], status["joined"]) == (None, False, None, True)
    assert (status["user_id"], status["mark"], status["unread"], status["head_seq"]) == (
        account.id,
        community.mark(account.id),
        0,
        0,
    )
    holder(tool)
    assert ok(tool, "community", "key") == {"ok": True, "key": "held", "epoch": 1}
    assert ok(tool, "community", "status")["key_held"] is True
    posted = ok(tool, "community", "post", "--text", "Hello all")  # the name of the user's member profile
    assert (posted["name"], posted["seq"], posted["replayed"]) == ("Ada", 1, False)
    bob_tool, bob = person(hub, tmp_path, "Bob")
    waiting = ok(bob_tool, "community", "key")
    assert (waiting["key"], waiting["epoch"]) == ("waiting", 1)
    request = waiting["request_id"]
    assert ok(bob_tool, "community", "status")["request"] == {"id": request, "wrapped": False, "this_agent": True}
    assert ok(bob_tool, "community", "key")["request_id"] == request  # the same request, no new one
    locked = ok(bob_tool, "community", "messages")
    assert (locked["key"], [item["status"] for item in locked["messages"]]) == ("waiting", ["locked"])
    assert locked["messages"][0]["text"] is None
    missing = fails(bob_tool, "community", "post", "--text", "x")
    assert (missing["error"], missing["key"], missing["request_id"]) == ("community_key_missing", "waiting", request)
    assert ok(bob_tool, "community", "serve") == {
        "ok": True,
        "key": "waiting",
        "epoch": 1,
        "request_id": request,
        "offered": 0,
        "stored": 0,
        "skipped": 0,
    }
    serving = ok(tool, "community", "serve")
    assert (serving["offered"], serving["stored"], serving["skipped"]) == (1, 1, 0)
    assert ok(bob_tool, "community", "status")["request"]["wrapped"] is True
    assert ok(bob_tool, "community", "key") == {"ok": True, "key": "opened", "epoch": 1}
    assert "request" not in local(bob_tool)
    assert key_of(hub, bob) == key_of(hub, account)
    read = ok(bob_tool, "community", "messages", "--mark-read")
    item = read["messages"][0]
    assert (item["status"], item["name"], item["text"], item["author"], item["own"]) == (
        "verified",
        "Ada",
        "Hello all",
        account.id,
        False,
    )
    assert (item["mark"], item["moderator"], item["deleted"], item["id"]) == (
        community.mark(account.id),
        False,
        None,
        posted["id"],
    )
    assert (read["last_read_seq"], read["key"], read["next_before"]) == (1, "held", None)
    assert local(bob_tool)["pins"] == {account.id: account.identity.public()["sig"]}
    reply = ok(bob_tool, "community", "post", "--text", "-", "--name", " Bob ", stdin="Hi Ada\r\n")
    assert (reply["name"], reply["seq"]) == ("Bob", 2)
    assert local(bob_tool)["name"] == "Bob"
    assert ok(tool, "community", "status")["unread"] == 1
    seen = ok(tool, "community", "messages")["messages"]
    assert [(m["seq"], m["name"], m["text"], m["own"]) for m in seen] == [
        (1, "Ada", "Hello all", True),
        (2, "Bob", "Hi Ada", False),
    ]
    # authors delete their own messages, nobody else's
    assert fails(tool, "community", "delete", "--id", reply["id"])["hub_error"] == "forbidden"
    assert ok(bob_tool, "community", "delete", "--id", reply["id"]) == {
        "ok": True,
        "id": reply["id"],
        "deleted": "author",
    }
    gone = ok(tool, "community", "messages")["messages"][1]
    assert (gone["status"], gone["deleted"], gone["text"], gone["name"]) == ("removed", "author", None, None)
    assert fails(tool, "community", "delete", "--id", "nope")["error"] == "invalid_argument"


def test_paging_and_arguments(tool: Tool, hub: HubFake, account: User) -> None:
    holder(tool)
    for text in ("one", "two", "three"):
        ok(tool, "community", "post", "--text", text)
    newest = ok(tool, "community", "messages", "--limit", "2")
    assert ([m["text"] for m in newest["messages"]], newest["next_before"]) == (["two", "three"], "2")
    older = ok(tool, "community", "messages", "--before", "2")
    assert ([m["text"] for m in older["messages"]], older["next_before"]) == (["one"], None)
    after = ok(tool, "community", "messages", "--after", "1", "--mark-read")
    assert ([m["text"] for m in after["messages"]], after["last_read_seq"]) == (["two", "three"], 3)
    assert "last_read_seq" not in ok(tool, "community", "messages", "--after", "9", "--mark-read")
    for argv in (
        ("--limit", "0"),
        ("--limit", "101"),
        ("--before", "-1"),
        ("--after", "-1"),
        ("--before", "1", "--after", "1"),
    ):
        assert fails(tool, "community", "messages", *argv)["error"] == "invalid_argument"
    assert fails(tool, "community", "key", "--wait", "-1")["error"] == "invalid_argument"
    assert fails(tool, "community", "post", "--text", " \u200b ")["reason"] == "community_body"


def test_names_of_posts(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    holder(tool)
    assert ok(tool, "community", "post", "--text", "a", "--name", "Captain")["name"] == "Captain"
    assert ok(tool, "community", "post", "--text", "b")["name"] == "Captain"  # remembered
    set_local(tool, {})
    assert ok(tool, "community", "post", "--text", "c")["name"] == "Captain"  # the newest own message
    state(hub).messages.clear()
    set_local(tool, "damaged")
    monkeypatch.setattr(Account, "display_name", lambda self: " " + "N" * 70 + "\u200b")
    assert ok(tool, "community", "post", "--text", "d")["name"] == "N" * 64  # the profile, cut to the limit
    state(hub).messages.clear()
    set_local(tool, {})
    monkeypatch.setattr(Account, "display_name", lambda self: None)
    assert fails(tool, "community", "post", "--text", "e")["reason"] == "name_missing"


def test_rate_limit_and_kept_posts(tool: Tool, hub: HubFake, account: User) -> None:
    holder(tool)
    route = ("POST", "/v1/community/messages")
    hub.hooks[route] = lambda body, request: (429, {"error": "rate_limited"}, {"Retry-After": "4"})
    limited = fails(tool, "community", "post", "--text", "again")
    assert (limited["hub_error"], limited["status"], limited["retry_after"]) == ("rate_limited", 429, 4)
    kept = local(tool)["kept"]
    assert (kept["name"], kept["text"], kept["epoch"]) == ("Ada", "again", 1)
    hub.hooks[route] = lambda body, request: (429, {"error": "rate_limited"})
    assert fails(tool, "community", "post", "--text", "again")["retry_after"] == community.RETRY_FALLBACK_S
    assert local(tool)["kept"] == kept  # sent byte for byte again
    del hub.hooks[route]
    stored = ok(tool, "community", "post", "--text", "again")
    assert (stored["id"], stored["replayed"]) == (kept["message"]["header"]["id"], False)
    assert "kept" not in local(tool)
    set_local(tool, {**local(tool), "kept": kept})  # the answer of the first attempt was lost
    assert ok(tool, "community", "post", "--text", "again")["replayed"] is True
    set_local(tool, {**local(tool), "kept": kept})
    other = ok(tool, "community", "post", "--text", "other")  # another text: sealed anew
    assert other["id"] != kept["message"]["header"]["id"]
    set_local(tool, {**local(tool), "kept": kept})
    hub.hooks[route] = lambda body, request: (409, {"error": "id_conflict"})
    assert fails(tool, "community", "post", "--text", "again")["hub_error"] == "id_conflict"
    assert "kept" not in local(tool)


def test_moderation(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    holder(tool)
    bob_tool, bob = person(hub, tmp_path, "Bob")
    obtain(hub, tool, bob_tool)
    message = ok(bob_tool, "community", "post", "--text", "spam")
    assert fails(tool, "community", "mutes")["hub_error"] == "forbidden"
    state(hub).moderators.add(account.id)
    assert ok(tool, "community", "status")["moderator"] is True
    muted = ok(tool, "community", "mute", "--user", bob.id)
    assert muted == {"ok": True, "user": bob.id, "mark": community.mark(bob.id), "muted": True}
    listed = ok(tool, "community", "mutes")["mutes"]
    assert [(row["user"], row["mark"]) for row in listed] == [(bob.id, community.mark(bob.id))]
    assert ok(bob_tool, "community", "status")["muted"] is True
    assert fails(bob_tool, "community", "post", "--text", "more")["hub_error"] == "community_muted"
    assert fails(tool, "community", "mute", "--user", account.id)["hub_error"] == "forbidden"
    assert ok(tool, "community", "unmute", "--user", bob.id)["muted"] is False
    assert ok(tool, "community", "delete", "--id", message["id"])["deleted"] == "moderator"
    own = ok(tool, "community", "post", "--text", "Be nice")
    shown = ok(bob_tool, "community", "messages")["messages"]
    assert [(m["status"], m["deleted"], m["moderator"]) for m in shown] == [
        ("removed", "moderator", False),
        ("verified", None, True),
    ]
    assert shown[1]["id"] == own["id"]
    assert fails(tool, "community", "mute", "--user", "x")["error"] == "invalid_argument"
    hub.hooks[("GET", "/v1/community/mutes")] = lambda body, request: (
        200,
        {"mutes": [{"user": bob.id, "muted_at": "t"}, "junk", {"user": 1}]},
    )
    assert ok(tool, "community", "mutes")["mutes"] == [
        {"user": bob.id, "mark": community.mark(bob.id), "muted_at": "t"}
    ]
    hub.hooks[("GET", "/v1/community/mutes")] = lambda body, request: (200, {"mutes": "junk"})
    assert fails(tool, "community", "mutes")["reason"] == "response_invalid"


def test_what_does_not_verify_shows_no_content(tool: Tool, hub: HubFake, account: User) -> None:
    holder(tool)
    key = key_of(hub, account)
    eve, mallory = Identity.generate(), Identity.generate()
    eve_id, mallory_id, ghost = uuid7(), uuid7(), uuid7()
    good = sealed(key, eve_id, eve, "fine")
    tampered = {**sealed(key, eve_id, eve, "x"), "ct": good["ct"]}
    other_id = sealed(key, eve_id, eve, "y")
    stale = sealed(key, eve_id, eve, "z", epoch=2)
    wrong_key = sealed(os.urandom(32), eve_id, eve, "w")
    impostor = sealed(key, mallory_id, mallory, "me")
    rows = [
        (1, uuid7(), ghost, sealed(key, ghost, eve, "no key")),
        (2, tampered["header"]["id"], eve_id, tampered),
        (3, uuid7(), eve_id, other_id),
        (4, stale["header"]["id"], eve_id, stale),
        (5, wrong_key["header"]["id"], eve_id, wrong_key),
        (6, impostor["header"]["id"], mallory_id, impostor),
        (7, uuid7(), eve_id, None),
        (8, good["header"]["id"], eve_id, good),
    ]
    page = {
        "messages": [
            {"seq": s, "id": i, "author": a, "created_at": now_ms_text(), "deleted": None, "message": m}
            for s, i, a, m in rows
        ],
        "authors": {
            eve_id: {"sig_pub": b64u_encode(eve.sig_public()), "moderator": False},
            mallory_id: {"sig_pub": b64u_encode(mallory.sig_public()), "moderator": False},
            uuid7(): "junk",
        },
        "next_before": None,
    }
    set_local(tool, {"pins": {mallory_id: b64u_encode(eve.sig_public())}})
    route = ("GET", "/v1/community/messages")
    hub.hooks[route] = lambda body, request: (200, page)
    shown = ok(tool, "community", "messages")["messages"]
    assert [m["status"] for m in shown] == ["unverifiable"] * 6 + ["removed", "verified"]
    assert [m["text"] for m in shown] == [None] * 7 + ["fine"]
    assert local(tool)["pins"][eve_id] == b64u_encode(eve.sig_public())
    set_local(tool, {"pins": "damaged"})
    assert ok(tool, "community", "messages")["messages"][7]["status"] == "verified"
    pages: list[dict[str, Any]] = [
        {"messages": "x", "authors": {}, "next_before": None},
        {"messages": [], "authors": {}, "next_before": 3},
        {"messages": [{"seq": "1"}], "authors": {}, "next_before": None},
    ]
    for broken in pages:

        def answer(body: Any, request: Request, page: dict[str, Any] = broken) -> tuple[int, Any]:
            return 200, page

        hub.hooks[route] = answer
        assert fails(tool, "community", "messages")["reason"] == "response_invalid"


def test_a_broken_self_wrap_and_answers_that_do_not_verify(tool: Tool, hub: HubFake, account: User) -> None:
    holder(tool)
    ok(tool, "community", "post", "--text", "hi")
    route = ("GET", "/v1/community")
    wrap = state(hub).wraps[account.id]
    forged = reference.sign_community_wrap(
        Identity.generate().sig, epoch=1, recipient=account.id, wrap_text=wrap["wrap"]
    )
    hub.hooks[route] = lambda body, request: (200, served(hub, account, {"wrap": {**wrap, "sig": forged}}))
    failed = ok(tool, "community", "key")
    assert (failed["key"], failed["key_code"]) == ("failed", "signature_invalid")
    assert ok(tool, "community", "messages")["messages"][0]["status"] == "locked"
    assert fails(tool, "community", "post", "--text", "x")["key"] == "failed"
    hub.hooks[route] = lambda body, request: (200, {"epoch": "x"})
    assert fails(tool, "community", "status")["reason"] == "response_invalid"


def test_two_first_clients_and_a_failing_creation(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    bob_tool, _bob = person(hub, tmp_path, "Bob")
    holder(bob_tool)
    route = ("GET", "/v1/community")
    reads: list[int] = []

    def first_without_epoch(body: Any, request: Request) -> Any:
        reads.append(1)
        return (200, served(hub, account, {"epoch": None, "wrap": None})) if len(reads) == 1 else None

    hub.hooks[route] = first_without_epoch
    assert ok(tool, "community", "key")["key"] == "waiting"  # 409 community_epoch_exists, read again, request
    hub.hooks[route] = lambda body, request: (200, served(hub, account, {"epoch": None, "wrap": None}))
    assert fails(tool, "community", "key")["hub_error"] == "community_epoch_exists"
    hub.hooks[("POST", "/v1/community/epochs")] = lambda body, request: (409, {"error": "community_not_member"})
    assert fails(tool, "community", "key")["hub_error"] == "community_not_member"


def test_wrong_keys_are_rejected(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    holder(tool)
    bob_tool, bob = person(hub, tmp_path, "Bob")
    data = state(hub)
    request = ok(bob_tool, "community", "key")["request_id"]
    kem_pub = b64u_decode(data.requests[bob.id]["kem_pub"], 32)

    def offer(key: bytes, epoch: int) -> None:
        wrap = reference.wrap_community_key(kem_pub, epoch=epoch, recipient=request, key=key)
        data.requests[bob.id]["wrapped"] = {
            "epoch": epoch,
            "wrap": wrap,
            "sig": reference.sign_community_wrap(account.identity.sig, epoch=epoch, recipient=request, wrap_text=wrap),
            "wrapped_by": account.id,
            "wrapper_sig": b64u_encode(account.identity.sig_public()),
        }

    offer(os.urandom(32), 1)
    rejected = ok(bob_tool, "community", "key")
    assert (rejected["key"], rejected["key_code"], rejected["request_id"]) == ("waiting", "decrypt_failed", request)
    assert (account.id, bob.id) in data.rejects
    assert data.requests[bob.id]["wrapped"] is None
    assert ok(tool, "community", "serve")["offered"] == 0  # the rejected holder is not offered the request again
    offer(key_of(hub, account), 2)
    assert ok(bob_tool, "community", "key")["key_code"] == "epoch_stale"
    offer(key_of(hub, account), 1)
    hub.hooks[("POST", "/v1/community/request/reject")] = lambda body, request: (404, {"error": "not_found"})
    data.key_check = b64u_encode(os.urandom(32))
    assert ok(bob_tool, "community", "key")["key_code"] == "decrypt_failed"
    hub.hooks[("POST", "/v1/community/request/reject")] = lambda body, request: (403, {"error": "forbidden"})
    assert fails(bob_tool, "community", "key")["hub_error"] == "forbidden"


def test_requests_of_other_clients_and_damaged_state(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder(tool)
    bob_tool, _bob = person(hub, tmp_path, "Bob")
    first = ok(bob_tool, "community", "key")["request_id"]
    set_local(bob_tool, {})  # the request now belongs to "another client" of Bob
    assert ok(tool, "community", "serve")["stored"] == 1
    waiting = ok(bob_tool, "community", "key")
    assert (waiting["key"], waiting["key_code"], waiting["request_id"]) == ("waiting", "other_client", first)
    assert local(bob_tool)["foreign"]["id"] == first
    assert ok(bob_tool, "community", "status")["request"]["this_agent"] is False
    assert ok(bob_tool, "community", "key")["key_code"] == "other_client"  # still within its time
    monkeypatch.setattr(community, "FOREIGN_S", 0.0)
    replaced = ok(bob_tool, "community", "key")
    assert replaced["request_id"] != first
    assert "foreign" not in local(bob_tool)
    set_local(bob_tool, {"request": {"id": replaced["request_id"], "kem_sk": "!"}})
    assert ok(bob_tool, "community", "key")["request_id"] not in (first, replaced["request_id"])


def test_waiting_for_the_key(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder(tool)
    bob_tool, _bob = person(hub, tmp_path, "Bob")
    assert ok(bob_tool, "community", "key", "--wait", "0")["key"] == "waiting"
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:
            ok(tool, "community", "serve")

    clock = SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=pause)
    monkeypatch.setattr(community, "time", clock)
    assert ok(bob_tool, "community", "key", "--wait", "60")["key"] == "opened"
    assert pauses == [community.POLL_S, community.POLL_S]


def test_serving_many_and_unusable_requests(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder(tool)
    others = [person(hub, tmp_path, name) for name in ("Bob", "Carol")]
    for other, _user in others:
        ok(other, "community", "key")
    monkeypatch.setattr(community, "REQUESTS", 1)
    serving = ok(tool, "community", "serve")
    assert (serving["offered"], serving["stored"], serving["skipped"]) == (2, 2, 0)
    for other, _user in others:
        assert ok(other, "community", "key")["key"] == "opened"
    monkeypatch.setattr(community, "REQUESTS", 3)
    junk = ["junk", {"id": "x", "kem_pub": "y"}, {"id": uuid7(), "kem_pub": b64u_encode(bytes(32))}]
    route = ("GET", "/v1/community/requests")
    hub.hooks[route] = lambda body, request: (200, {"requests": junk})
    unusable = ok(tool, "community", "serve")
    assert (unusable["offered"], unusable["stored"], unusable["skipped"]) == (15, 0, 15)
    hub.hooks[route] = lambda body, request: (200, {"requests": None})
    assert fails(tool, "community", "serve")["reason"] == "response_invalid"


def test_key_summary() -> None:
    assert community.Key("waiting", 1, request="r", code="c").summary() == {
        "key": "waiting",
        "epoch": 1,
        "request_id": "r",
        "key_code": "c",
    }
