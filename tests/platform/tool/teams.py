"""Helpers of the stage-a suites: a team org with further members on their own computers, the key ring a seeded user
holds (every wrap ever addressed to it, unwrapped as that user could), opening an event with such a ring, and
decertifying a platform device in the platform log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_runner.crypto.encoding import parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.crypto.log.entries import entry_hash
from agent_runner.crypto.wrap import unwrap_key

from .conftest import Tool, make_tool, pair_user
from .hubfake import HubFake, User, now_ms_text

Ring = dict[tuple[str, str, int], bytes]


def team(tool: Tool, name: str = "Acme") -> str:
    code, document = tool.run("org", "create", "--name", name)
    assert code == 0, document
    return str(document["org_id"])


def wg_create(tool: Tool, org: str, name: str = "Research") -> str:
    code, document = tool.run("wg", "create", "--org", org, "--name", name)
    assert code == 0, document
    return str(document["wg_id"])


def join(
    hub: HubFake, admin: Tool, org: str, root: Path, name: str, *options: str, role: str = "member"
) -> tuple[Tool, User]:
    """A new user with an agent on its own computer that accepts an invitation of `admin` (`options`: `--wg ...`)."""
    invited = admin.run("org", "invite", "--org", org, "--role", role, *options)[1]
    assert invited["ok"] is True, invited
    other = make_tool(root / name.lower())
    user = hub.add_user(display_name=name)
    pair_user(other, hub, user)
    joined = other.run("org", "join", stdin=invited["invite_link"])[1]
    assert joined["ok"] is True, joined
    return other, user


def ring(hub: HubFake, user: User) -> Ring:
    """Every scope key the hub ever offered `user` (as far as the wraps are still stored)."""
    found: Ring = {}
    for wrap in hub.wraps:
        if wrap["recipient_id"] != user.id or wrap["scope"] == "cred":
            continue
        slot = (wrap["scope"], wrap["scope_id"], wrap["epoch"])
        found[slot] = unwrap_key(
            user.identity.kem, scope=slot[0], scope_id=slot[1], epoch=slot[2], recipient=user.id, wrap=wrap["wrap"]
        )
    return found


def open_with(keys: Ring, event: dict[str, Any], author: User) -> object | None:
    """The body if any key of `keys` opens the event (the key of its epoch first, then every other one)."""
    header = event["header"]
    own = keys.get((header["scope"], header["scope_id"], header["epoch"]))
    for key in [*([own] if own is not None else []), *keys.values()]:
        try:
            return parse_json(open_event(key, event, author.identity.sig_public()))
        except CryptoError:
            continue
    return None


def decertify(hub: HubFake, device: str) -> None:
    """A `device_decertify` of `device` in the platform log."""
    last = hub.platform_entries[-1]
    body = {
        "seq": last["body"]["seq"] + 1,
        "prev": entry_hash(last),
        "type": "device_decertify",
        "by": "platform-root",
        "ts": now_ms_text(),
        "v": 1,
        "device": device,
    }
    hub._platform_append(body)


def event_of(hub: HubFake, scope: str, scope_seq: int) -> dict[str, Any]:
    return dict(next(item["event"] for item in hub.events[scope] if item["scope_seq"] == scope_seq))


def private_org(hub: HubFake, user: User) -> str:
    return next(org.id for org in hub.orgs.values() if org.private and org.log.state.member(user.id))


def assign(tool: Tool, hub: HubFake, wg: str, device: str | None = None, seq: int | None = None) -> tuple[str, int]:
    if device is None or seq is None:
        device, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": device, "platform_seq": seq}]
    document = tool.run("device", "assign", "--wg", wg)[1]
    assert document["device_id"] == device, document
    return device, seq


def grant_platform_access(hub: HubFake, org: str) -> None:
    last = hub.platform_entries[-1]
    hub._platform_append(
        {
            "seq": last["body"]["seq"] + 1,
            "prev": entry_hash(last),
            "type": "org_platform_access",
            "by": "platform-root",
            "ts": now_ms_text(),
            "v": 1,
            "org": org,
            "granted": True,
        }
    )
    hub.orgs[org].platform_access = True
