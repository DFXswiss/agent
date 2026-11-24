"""Load the hardcoded team roster. Fail closed on a broken file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class TeamsError(ValueError):
    pass


@dataclass(frozen=True)
class Roster:
    teams: dict[str, list[str]]
    agents: dict[str, frozenset[str]]

    def is_agent(self, login: str) -> bool:
        return login.strip().lower() in self.agents

    def owns_agent(self, owner: str, agent: str) -> bool:
        return owner.strip().lower() in self.agents.get(agent.strip().lower(), frozenset())

    def owned_agents(self, owner: str) -> list[str]:
        login = owner.strip().lower()
        return sorted(agent for agent, owners in self.agents.items() if login in owners)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that refuses a mapping key written twice instead of keeping the last."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    keys: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        keys.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_roster(path: Path) -> Roster:
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise TeamsError(f"teams file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict) or "teams" not in data:
        raise TeamsError(f"teams file must have a top-level 'teams' map: {path}")
    teams_raw = data["teams"]
    if not isinstance(teams_raw, dict):
        raise TeamsError(f"'teams' must be a map: {path}")
    out: dict[str, list[str]] = {}
    for name, spec in teams_raw.items():
        if not isinstance(name, str) or name == "":
            raise TeamsError("team name must be a non-empty string")
        if not isinstance(spec, dict) or "members" not in spec:
            raise TeamsError(f"team {name!r} must have a 'members' list")
        members = spec["members"]
        if not isinstance(members, list):
            raise TeamsError(f"team {name!r} members must be a list")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in members:
            if not isinstance(item, str) or item.strip() == "":
                raise TeamsError(f"team {name!r} has a non-string or empty member")
            login = item.strip().lower()
            if login in seen:
                raise TeamsError(f"team {name!r} lists {login} twice")
            seen.add(login)
            cleaned.append(login)
        out[name] = cleaned
    agents_raw = data.get("agents", {})
    if not isinstance(agents_raw, dict):
        raise TeamsError(f"'agents' must be a map: {path}")
    agents: dict[str, frozenset[str]] = {}
    seen_agents: set[str] = set()
    all_members = {member for members in out.values() for member in members}
    for raw_login, spec in agents_raw.items():
        if not isinstance(raw_login, str) or raw_login.strip() == "":
            raise TeamsError("agent login must be a non-empty string")
        login = raw_login.strip().lower()
        if login in seen_agents:
            raise TeamsError(f"agents map lists {login} twice")
        seen_agents.add(login)
        if login not in all_members:
            raise TeamsError(f"agent {login!r} must be a member of at least one team")
        if not isinstance(spec, dict) or "owners" not in spec:
            raise TeamsError(f"agent {login!r} must have an 'owners' list")
        owners_raw = spec["owners"]
        if not isinstance(owners_raw, list):
            raise TeamsError(f"agent {login!r} owners must be a list")
        owners: set[str] = set()
        for raw_owner in owners_raw:
            if not isinstance(raw_owner, str) or raw_owner.strip() == "":
                raise TeamsError(f"agent {login!r} has a non-string or empty owner")
            owner = raw_owner.strip().lower()
            if owner in owners:
                raise TeamsError(f"agent {login!r} lists owner {owner} twice")
            if owner == login:
                raise TeamsError(f"agent {login!r} cannot own itself")
            if not any(login in members and owner in members for members in out.values()):
                raise TeamsError(f"owner {owner!r} must share a team with agent {login!r}")
            owners.add(owner)
        agents[login] = frozenset(owners)
    return Roster(teams=out, agents=agents)


def visible_logins(login: str, teams: dict[str, list[str]]) -> set[str]:
    self = login.strip().lower()
    if self == "":
        raise TeamsError("login must be a non-empty string")
    visible = {self}
    for members in teams.values():
        if self in members:
            visible.update(members)
    return visible


def dump_shape(data: Any) -> str:
    return type(data).__name__
