"""Choose the account of a pool role when a coordinator lane starts.

A role in ``ai-accounts.json`` may name ``accounts``, a pool of accounts of one
provider, instead of one ``account``. Each fresh coordinator lane start for
such a role chooses one member here; the launched process is isolated to that
member like any single-account role. The chosen account name is recorded on
the lane's agent row (``account``), and every later step of that lane resolves
the recorded account through :func:`recorded_lane_role`; it never chooses
again.

Selection reads only local data: the login file in each member's
configuration directory and the newest own ``usage.snapshot`` that usage
polling recorded for that login. It makes no network request.

Choice key, smallest first: ``(running_lanes, load, configuration_index)``.
Lanes started close together spread across the pool first, because a new
lane's usage shows up in the snapshots only after the next poll; among
members with equally many running lanes the least-loaded one wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .ai_accounts import AccountError, AIAccount, AIAccountPool, AIAccounts, AIRole
from .store import Store, StoreError
from .usage import last_usage_snapshot, load_login_account, window_snapshot_match

# Agent-row field that records the account a lane was launched with.
LANE_ACCOUNT_FIELD = "account"
# Agent-row field with the one-line selection decision of a pool lane.
LANE_ACCOUNT_CHOICE_FIELD = "account_choice"
# Windows whose ``used_percent`` make up a member's load. ``weekly_models`` is
# not used: its names are display names that do not map to role models.
LOAD_WINDOWS = ("five_hour", "weekly")
EXHAUSTED_PERCENT = 100.0


class _InvalidSnapshot(Exception):
    """A stored usage snapshot does not have the windows shape."""


@dataclass(frozen=True)
class LaneAccountChoice:
    """The launchable role of a fresh lane and how its account was chosen.

    ``load`` and ``running_lanes`` are ``None`` for a role that names one
    account: nothing was read for it.
    """

    role: AIRole
    load: float | None
    running_lanes: int | None

    @property
    def account(self) -> AIAccount:
        return self.role.account

    def summary(self) -> str | None:
        """One line for the lane record; ``None`` when no pool was involved.

        It names the configured account name and its load, never a login
        identity or a credential.
        """
        if self.load is None:
            return None
        return (
            f"account {self.role.account.name} chosen from the pool of role "
            f"{self.role.name} at load {self.load:g} % with "
            f"{self.running_lanes} running lanes"
        )

    def record_fields(self) -> dict[str, str]:
        """Fields added to the lane's agent row at launch."""
        fields = {LANE_ACCOUNT_FIELD: self.role.account.name}
        summary = self.summary()
        if summary is not None:
            fields[LANE_ACCOUNT_CHOICE_FIELD] = summary
        return fields


def select_lane_account(
    store: Store, accounts: AIAccounts, role: AIRole, *, now: datetime
) -> LaneAccountChoice:
    """Choose the account a fresh coordinator lane of ``role`` launches with.

    A single-account role returns that account without reading anything. A
    pool role returns the eligible member with the smallest
    ``(running_lanes, load, configuration_index)``. A member without a login,
    without a usage snapshot, with an unreadable snapshot, or whose load is at
    least 100 % is not eligible. No eligible member raises ``AccountError``
    naming the role and every member's reason; there is no fallback account.
    """
    if not role.is_pool:
        return LaneAccountChoice(role=role, load=None, running_lanes=None)
    if now.tzinfo is None:
        raise AccountError("pool selection time must carry a time zone")
    members = accounts.pool_members(role)
    running = _running_lanes(store)
    eligible: list[tuple[int, float, int, AIAccount]] = []
    reasons: list[str] = []
    for index, account in enumerate(members):
        load, reason = _member_load(store, accounts, account, now=now)
        if load is None:
            reasons.append(f"{account.name}: {reason}")
            continue
        eligible.append((running.get(account.name, 0), load, index, account))
    if not eligible:
        raise AccountError(
            f"Role {role.name} has no usable pool account ({'; '.join(reasons)})"
        )
    lanes, load, _index, chosen = min(eligible, key=lambda item: item[:3])
    return LaneAccountChoice(
        role=replace(role, account=chosen), load=load, running_lanes=lanes
    )


def recorded_lane_role(
    accounts: AIAccounts, role: AIRole, recorded: Any, *, lane: str
) -> AIRole:
    """The launchable role of a lane that already started, from its record.

    ``recorded`` is the lane record's ``account`` field. A pool role resolves
    to that member and never chooses again. A recorded account that is no
    longer the role's account, or no longer a member of its pool, raises. A
    record without the field, written before pools existed, resolves a
    single-account role to its account and raises for a pool role.
    """
    if recorded is None:
        if role.is_pool:
            raise AccountError(
                f"Lane {lane} of pool role {role.name} has no recorded account"
            )
        return role
    if not isinstance(recorded, str) or not recorded.strip():
        raise AccountError(f"Lane {lane} has an invalid recorded account")
    pool = role.account
    if isinstance(pool, AIAccountPool):
        if recorded not in pool.members or recorded not in accounts.accounts:
            raise AccountError(
                f"Lane {lane} ran on account {recorded}, which is no longer in "
                f"the pool of role {role.name}"
            )
        return replace(role, account=accounts.accounts[recorded])
    if recorded != role.account.name:
        raise AccountError(
            f"Lane {lane} ran on account {recorded}, which is no longer the "
            f"account of role {role.name}"
        )
    return role


def _running_lanes(store: Store) -> dict[str, int]:
    """Open lanes (agent rows still ``working``) of this device by recorded account."""
    device = store.device_id()
    counts: dict[str, int] = {}
    for row in store.rows("agent"):
        if row.get("status") != "working" or row.get("_origin_device_id") != device:
            continue
        name = row.get(LANE_ACCOUNT_FIELD)
        if isinstance(name, str):
            counts[name] = counts.get(name, 0) + 1
    return counts


def _member_load(
    store: Store, accounts: AIAccounts, account: AIAccount, *, now: datetime
) -> tuple[float | None, str]:
    """The member's load, or ``None`` and the reason it is not eligible."""
    # Load validated the provider's windows usage adapter for every pool member.
    spec = accounts.drivers[account.provider]
    try:
        email = load_login_account(spec, account.config_dir)
    except StoreError:
        # AuthStale (no login) and an unreadable account file alike.
        return None, "no login"
    snapshot = last_usage_snapshot(store, window_snapshot_match(account.provider, email))
    if snapshot is None:
        return None, "no usage snapshot"
    try:
        load = _snapshot_load(snapshot, now)
    except _InvalidSnapshot:
        return None, "unreadable usage snapshot"
    if load >= EXHAUSTED_PERCENT:
        return None, f"exhausted at {load:g} %"
    return load, ""


def _snapshot_load(snapshot: dict[str, Any], now: datetime) -> float:
    """Maximum ``used_percent`` of the load windows at ``now``.

    A ``null`` window counts 0, and so does a window whose ``resets_at`` is at
    or before ``now``: it has reset since the snapshot. The snapshot's
    ``fetched_at`` is not checked; a snapshot is stored only when a value
    changed, so an old one is still current.
    """
    windows = snapshot.get("windows")
    if not isinstance(windows, dict):
        raise _InvalidSnapshot
    load = 0.0
    for key in LOAD_WINDOWS:
        window = windows.get(key)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise _InvalidSnapshot
        used = window.get("used_percent")
        if (
            isinstance(used, bool)
            or not isinstance(used, (int, float))
            or not math.isfinite(used)
        ):
            raise _InvalidSnapshot
        resets_at = window.get("resets_at")
        if resets_at is not None and _reset_time(resets_at) <= now:
            continue
        load = max(load, float(used))
    return load


def _reset_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise _InvalidSnapshot
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise _InvalidSnapshot from None
    if when.tzinfo is None:
        raise _InvalidSnapshot
    return when
