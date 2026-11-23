"""Model credentials of the sessions, in the memory of the API process only (05-runner.md §10.2, 03-api.md §7.1).

Path of a credential: the runner sends ``PUT /v1/sessions/{id}/credential`` to the control process, which checks it
and keeps it in a hand-off slot (``control_routes/credential.py``) until the API process takes it over ``api.sock``
(op ``credential.take``) on the session's next model request. From then on it lives only in ``CredentialStore``;
nothing is written to disk, journaled or logged, and ``repr`` never shows a secret. A new provisioning replaces the
old credential (refresh). Credentials of sessions that are no longer active are dropped whenever the control process
reports the active sessions (``retain``).

Observed cost across a renewal (decisions KOR-03, AUD-01 A-29). The gateway refuses once ``spent_usd`` of the
provisioning plus the cost it observed since reaches the daily limit. The runner renews a platform credential when the
organization's spent amount changed; that amount lags behind the cost this gateway observed and the hub has not
counted yet (usage journal -> runner -> outbox -> hub). Restarting the observed cost at 0 lost that part. Both
``old spent + observed`` and ``new spent`` are lower bounds of the true spending, so the renewal keeps the larger one:
it carries ``max(0, old spent + observed - new spent)``. A smaller new amount means a new day began: the cost observed
so far belongs (almost entirely) to the day before, so nothing is carried; carrying it would stay as a phantom amount
of yesterday until today's spending passes it. A request that ends after its credential was replaced is
charged to the session's current entry: its cost cannot be in the new amount yet.

The spent amount is the organization's, so the cost of other sessions and devices that the hub counted meanwhile
displaced this session's own uncounted cost in that bound (KOR-10, AUD-02 NA-10: old 4, own 3, others 5, new 9 carried
0). The runner therefore sends the spent amount the hub counted plus the cost of its own ``usage`` items the hub had not
counted yet, and ``journal_through``: the last journal record of this guest it had taken into its outbox when it built
the provisioning (``budget.journal_through``). Each entry keeps the session's charges (``Charge``) with the journal
``seq`` of their ``usage`` record. A charge above ``journal_through`` (or not journaled yet) is in no part of the new
amount: the runner had not read its record, so it is neither in the hub's count nor in the runner's addition. Those
parts are disjoint, and the renewal carries the larger of the two lower bounds: ``max(own charges above
journal_through, old spent + observed - new spent)``. A charge whose record could not be journaled is dropped from the
own part (whether it reached the journal is unknown; dropping it never counts anything twice); a cost the gateway
writes no ``usage`` record for reaches no one and stays the session's own. At most ``MAX_CHARGES`` charges are kept per
session (the oldest go first; dropping is always safe). Without ``journal_through`` only the old bound applies. A new
day clears both.

Interface:

* ``RESERVED_HEADERS`` - ``guest/INDEX.json#/reserved_headers``; an ``extra_headers`` entry must not use these nor
  the other ``STRIPPED_HEADERS`` (hop-by-hop, cookie), nor a name twice
* ``parse_credential(body) -> Credential`` - raises ``CredentialInvalid(pointer)`` for what the schema cannot state:
  reserved or repeated extra header names, user information, query or fragment in the upstream URL, a bad port, an
  ``auth_header`` that does not fit the access kind (``x-api-key`` only for ``api_key``, else ``Bearer``)
* ``Credential.upstream_headers(incoming) -> list`` - the request headers for the upstream with the injection of the
  access kind (``x-api-key`` / ``Authorization: Bearer``; the subscription also appends ``oauth-2025-04-20`` to
  ``anthropic-beta``) after removing the session's credential, hop-by-hop and transport headers
* ``CredentialStore`` - ``provide``, ``lookup -> (credential, observed_usd) | None``, ``charge -> Charge | None``,
  ``journaled`` (the journal ``seq`` of a charge's ``usage`` record, None when it was not journaled), ``retain``
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from agent_guest.catalog import ModelPolicy, policy_from

OAUTH_BETA = "oauth-2025-04-20"
RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "transfer-encoding",
        "anthropic-beta",
    }
)
# Never forwarded from the session: its own credential, transport and hop-by-hop headers (RFC 9110 section 7.6.1).
STRIPPED_HEADERS = RESERVED_HEADERS | frozenset(
    {"keep-alive", "te", "trailer", "upgrade", "proxy-authorization", "proxy-connection", "expect", "cookie"}
)


class CredentialInvalid(ValueError):
    def __init__(self, pointer: str) -> None:
        super().__init__(pointer)
        self.pointer = pointer


@dataclass(frozen=True, repr=False)
class Credential:
    scheme: str
    host: str
    port: int | None
    base_path: str
    auth_header: str
    auth_value: str
    extra_headers: tuple[tuple[str, str], ...]
    policy: ModelPolicy

    def __repr__(self) -> str:
        return f"Credential(access={self.policy.access!r}, upstream={self.scheme}://{self.host}, auth=<redacted>)"

    def upstream_headers(self, incoming: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        pairs = list(incoming)
        extra = {name for name, _ in self.extra_headers}
        # Headers listed in Connection are hop-by-hop too (RFC 9110 section 7.6.1).
        extra |= {
            token.strip().lower() for name, value in pairs if name.lower() == "connection" for token in value.split(",")
        }
        betas: list[str] = []
        out: list[tuple[str, str]] = []
        for name, value in pairs:
            lower = name.lower()
            if "\r" in value or "\n" in value:
                continue
            if lower == "anthropic-beta":
                betas.extend(part.strip() for part in value.split(",") if part.strip())
            elif lower not in STRIPPED_HEADERS and lower not in extra:
                out.append((name, value))
        if self.policy.access == "subscription" and OAUTH_BETA not in betas:
            betas.append(OAUTH_BETA)
        if betas:
            out.append(("anthropic-beta", ",".join(betas)))
        out.extend(self.extra_headers)
        out.append((self.auth_header, self.auth_value))
        return out


def parse_credential(body: Mapping[str, Any]) -> Credential:
    """A ``Credential`` from a provisioning body that matched ``guest/session-credential.request.json``."""
    target = urlsplit(str(body["upstream_base_url"]))
    try:
        port = target.port
    except ValueError:
        raise CredentialInvalid("/upstream_base_url") from None
    if target.username is not None or target.query or target.fragment or not target.hostname:
        raise CredentialInvalid("/upstream_base_url")
    access, auth_header, auth_value = str(body["access"]), str(body["auth_header"]), str(body["auth_value"])
    if (auth_header == "x-api-key") != (access == "api_key") or (
        auth_header == "authorization" and not auth_value.startswith("Bearer ")
    ):
        raise CredentialInvalid("/auth_header")
    seen: set[str] = set()
    extra: list[tuple[str, str]] = []
    for index, item in enumerate(body["extra_headers"]):
        name = str(item["name"])
        if name in STRIPPED_HEADERS or name in seen:
            raise CredentialInvalid(f"/extra_headers/{index}/name")
        seen.add(name)
        extra.append((name, str(item["value"])))
    return Credential(
        scheme=target.scheme,
        host=target.hostname,
        port=port,
        base_path=target.path.rstrip("/"),
        auth_header=auth_header,
        auth_value=auth_value,
        extra_headers=tuple(extra),
        policy=policy_from(body),
    )


MAX_CHARGES = 10_000


@dataclass
class Charge:
    """One charged cost of a session and the journal ``seq`` of the ``usage`` record that reports it (None until it is
    journaled); ``dropped`` when the record could not be journaled."""

    cost_usd: float
    seq: int | None = None
    dropped: bool = False


@dataclass
class _Entry:
    credential: Credential
    observed_usd: float = 0.0
    charges: list[Charge] = field(default_factory=list)


def _renewed(old: _Entry | None, new: Credential) -> _Entry:
    """The entry of a renewal: the observed cost it keeps and the charges that may still be the session's own
    uncounted part (module docstring)."""
    if old is None:
        return _Entry(new)
    before, after = old.credential.policy.budget, new.policy.budget
    if before is None or after is None or after.spent_usd < before.spent_usd:
        return _Entry(new)
    through = after.journal_through
    kept = [c for c in old.charges if not c.dropped and (through is None or c.seq is None or c.seq > through)]
    own = 0.0 if through is None else sum(charge.cost_usd for charge in kept)
    carried = max(own, before.spent_usd + old.observed_usd - after.spent_usd, 0.0)
    return _Entry(new, carried, kept)


class CredentialStore:
    """Session ID -> credential and the cost observed since its provisioning; thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def provide(self, session_id: str, credential: Credential) -> None:
        with self._lock:
            self._entries[session_id] = _renewed(self._entries.get(session_id), credential)

    def lookup(self, session_id: str) -> tuple[Credential, float] | None:
        with self._lock:
            entry = self._entries.get(session_id)
            return None if entry is None else (entry.credential, entry.observed_usd)

    def charge(self, session_id: str, credential: Credential, cost_usd: float) -> Charge | None:
        """Add ``cost_usd`` to the session's current entry, also when ``credential`` was replaced meanwhile (module
        docstring), as one of its charges; nothing (None) for a session without credential."""
        del credential
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            entry.observed_usd += cost_usd
            charge = Charge(cost_usd)
            if entry.credential.policy.budget is not None:  # without a budget nothing is carried: kept for nothing
                entry.charges.append(charge)
                del entry.charges[:-MAX_CHARGES]
            return charge

    def journaled(self, charge: Charge, seq: int | None) -> None:
        """Record the journal ``seq`` of the ``usage`` record that reports ``charge``; None: it was not journaled."""
        with self._lock:
            charge.seq = seq
            charge.dropped = seq is None

    def retain(self, active: Iterable[str]) -> None:
        keep = set(active)
        with self._lock:
            for session_id in [name for name in self._entries if name not in keep]:
                del self._entries[session_id]

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)
