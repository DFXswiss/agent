"""`GET /v1/device/state`: the desired state of a device (03-api.md section 6.1, ADR-027 section 4).

A device waiting for its certification gets `{state:"pending_certification", hub_time}`. An active device gets the
full form. This module writes the base fields

- `state` (`active`), `hub_time` (the hub clock),
- `last_origin_seq` (`device_stream`, 0 before the first outbox item),
- `orgs` (`{org_id, log_head}` of every org in which the device is an active member, `agent_hub.device.access`),
- `assigned` (`{org_id, wg_id}` of every assigned workgroup),
- `rotation_pending` (the scope IDs of `org.rotation_pending` that the device reads: the admin browser still owes
  their rotation; the device keeps re-wrapping per W2 and never rotates itself),

and composes every other field from the sections other areas register as `<area>/device_state.py:SECTION`, a
`Section(fields, provide)`: `provide(request)` returns exactly its `fields` (`sessions`, `pending_prompts`,
`delivered_prompt_event_ids`, `prompt_queue` from the sessions area, `requests`/`credentials_changed` from requests and
credentials, `pings` from notifications, `quota_poll_owners`/`platform_budget` from usage, `github_defaults` from
github, `orchestrators` from agents, KOR-57). A field may be
provided by one area only. A field no area provides is sent empty: `[]`, `{}` for
`delivered_prompt_event_ids`, `null` for `platform_budget`, empty lists in `prompt_queue`. The optional fields of KOR-50
(`model_fallback`, `dfx_keys`, `dfx_key_held`, from the models area) are left out while their value is `OMIT`:
`dfx_keys` goes to the elected issuer only, `dfx_key_held` to org devices only; so are `limits` (the subagent settings
of ADR-088, from the operations area) and `subscriptions` (KOR-69, from the subscriptions area) while no area provides
them. Every provider reads in the same
transaction as the base fields.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.config import HubConfig
from agent_hub.crypto.encoding import format_time
from agent_hub.device.access import DeviceView, device_view
from agent_hub.registry import DEVICE_STATE_SECTIONS, RegistryError, discover

Connection = psycopg.Connection[Any]
OMIT: Any = object()
"""The value of an optional field that is left out of the answer."""
SECTION_FIELDS: Mapping[str, Callable[[], Any]] = {
    "sessions": list,
    "pending_prompts": list,
    "delivered_prompt_event_ids": dict,
    "requests": list,
    "pings": list,
    "credentials_changed": list,
    "quota_poll_owners": list,
    "platform_budget": lambda: None,
    "billing": lambda: {"api": 1, "active": False, "orgs": {}},
    "github_defaults": list,
    "model_fallback": lambda: OMIT,
    "dfx_keys": lambda: OMIT,
    "dfx_key_held": lambda: OMIT,
    "prompt_queue": lambda: {"via_agent": [], "withdrawn": []},
    "failover_seconds": lambda: OMIT,
    "failovers": list,
    "limits": lambda: OMIT,
    "orchestrators": list,
    "subscriptions": lambda: OMIT,
    "vm_switch": list,
    "vm_switch_at": dict,
    "vm_upgrade_version": lambda: OMIT,
    "agent_profiles": lambda: OMIT,
}
"""The fields sections provide, with the value of a field no area provides."""


@dataclass(frozen=True)
class StateRequest:
    """What a section provider may use: the transaction, the device, its verified view, the hub clock and config."""

    connection: Connection
    device_id: UUID
    view: DeviceView
    now: datetime
    config: HubConfig


@dataclass(frozen=True)
class Section:
    """The fields one area provides and its provider."""

    fields: tuple[str, ...]
    provide: Callable[[StateRequest], Mapping[str, Any]]


_lock = threading.Lock()
_state: dict[str, tuple[Section, ...]] = {}


def collect(found: Iterable[tuple[str, object]]) -> tuple[Section, ...]:
    """Check the discovered sections: a `Section` each, known fields, every field by one area only."""
    owners: dict[str, str] = {}
    sections: list[Section] = []
    for area, value in found:
        if not isinstance(value, Section) or not callable(value.provide) or not value.fields:
            raise RegistryError(f"{area}/device_state.py SECTION must be a Section with fields and a provider")
        for name in value.fields:
            if name not in SECTION_FIELDS:
                raise RegistryError(f"{area}/device_state.py: {name!r} is no section field of GET /v1/device/state")
            if name in owners:
                raise RegistryError(f"device-state field {name!r} is provided by {owners[name]} and {area}")
            owners[name] = area
        sections.append(value)
    return tuple(sections)


def configure(sections: Iterable[tuple[str, object]] | None = None) -> tuple[Section, ...]:
    """Set the sections (None discovers them) and return them."""
    found = collect(discover(DEVICE_STATE_SECTIONS) if sections is None else sections)
    with _lock:
        _state["sections"] = found
    return found


def sections() -> tuple[Section, ...]:
    """The current sections (discovered on first use)."""
    current = _state.get("sections")
    return current if current is not None else configure()


def last_origin_seq(connection: Connection, device: UUID) -> int:
    """The last accepted outbox `origin_seq` of the device (0 before the first item)."""
    row = connection.execute("SELECT last_origin_seq FROM hub.device_stream WHERE device_id = %s", (device,)).fetchone()
    return 0 if row is None else int(row[0])


def pending_state(now: datetime) -> dict[str, Any]:
    """The answer to a device waiting for its certification."""
    return {"state": "pending_certification", "hub_time": format_time(now)}


def active_state(connection: Connection, device: UUID, config: HubConfig, now: datetime) -> dict[str, Any]:
    """The desired state of an active device (module docstring)."""
    view = device_view(connection, device, config.platform_root_key)
    answer: dict[str, Any] = {
        "state": "active",
        "last_origin_seq": last_origin_seq(connection, device),
        "orgs": [
            {"org_id": item.org_id, "log_head": {"seq": item.org.head_seq, "hash": item.org.head_hash}}
            for item in view.orgs
        ],
        "assigned": [{"org_id": org, "wg_id": wg} for org, wg in view.assigned()],
        "rotation_pending": sorted({scope_id for item in view.orgs for scope_id in item.rotation_pending()}),
    }
    answer.update({name: empty() for name, empty in SECTION_FIELDS.items()})
    request = StateRequest(connection=connection, device_id=device, view=view, now=now, config=config)
    for section in sections():
        provided = dict(section.provide(request))
        if set(provided) != set(section.fields):
            raise RegistryError("a device-state section returned other fields than it declares")
        answer.update(provided)
    answer["hub_time"] = format_time(now)
    return {name: value for name, value in answer.items() if value is not OMIT}
