"""The screens of this device that wait for their owner (ADR-101 section 2).

A process that needs a human offers the screen of its sandbox while it waits: the check for humans of a login
(`agent_runner.login.requests.HumanCheck`) offers the login VM's screen under `("login", <request ID>)` from the
moment the check shows until it is gone or the login ends. `vnc.open` finds an offer only for its owner; `ended` is set
when the offer is withdrawn (the process went on or ended), which closes an open connection, and `viewers` counts the
connections open now (a check for humans waits for the owner as long as one is open).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from agent_runner.vnc.screen import Screen

ATTRIBUTE = "vnc_screens"


@dataclass(eq=False)
class Offer:
    kind: str
    target: str
    owner: str
    screen: Screen
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    viewers: int = 0


class Screens:
    def __init__(self) -> None:
        self.offers: dict[tuple[str, str], Offer] = {}

    def offer(self, kind: str, target: str, owner: str, screen: Screen) -> Offer:
        """Offer `screen` of `owner` under `(kind, target)`; a former offer of the same target ends."""
        former = self.offers.get((kind, target))
        if former is not None:
            self.withdraw(former)
        made = Offer(kind, target, owner, screen)
        self.offers[(kind, target)] = made
        return made

    def withdraw(self, offer: Offer) -> None:
        offer.ended.set()
        if self.offers.get((offer.kind, offer.target)) is offer:
            del self.offers[(offer.kind, offer.target)]

    def find(self, kind: str, target: str, owner: str) -> Offer | None:
        """The offer of `(kind, target)` when `owner` owns it."""
        found = self.offers.get((kind, target))
        return found if found is not None and found.owner == owner else None


def screens_for(context: Any) -> Screens:
    """The screens of this daemon (built on first use)."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Screens):
        return found
    made = Screens()
    setattr(context, ATTRIBUTE, made)
    return made
