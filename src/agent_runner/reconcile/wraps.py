"""Reconcile step 3: fetch, verify, unwrap and store key wraps (05-runner.md section 2.2, 04-crypto.md sections 4.2
and 6.3, 03-api.md section 6.1 `GET /v1/device/wraps`).

For every wrap addressed to this device whose slot `(scope, scope_id, epoch)` the device does not hold yet:

1. shape (`header_invalid`) and recipient = this device (`wrap_unauthorized`);
2. the scope's org must be acting (`OrgBook`): an `org` scope is the org itself, a `wg` scope the acting org that
   has the workgroup; a wrap whose org is not (yet) verified or is stopped is left for a later run, not refused;
3. W1-W3 against the verified log (`crypto.log.wraps.check_wrap`; the wrapper's signature, its right to hold the
   epoch, this device's entitlement now, decertified platform devices excluded). `cred` wraps are checked against
   every acting org's log (`check_cred_wrap`); the device does not learn the owner's current `CK` epoch from the
   logs, so the bound is the wrap's own epoch (decisions/RUN-03.md). A `cred` wrap that a former agent of the owner
   made (an agent of the owner in a verified log, removed or superseded since, so no reader any more; its
   signature under its key from the log) to a device that reads the owner's `CK` now is kept with source
   `former_agent`: it opens records sealed under that epoch, never the epoch a new record is sealed under (ADR-082
   section 9.5); a wrap of anybody else stays refused;
4. unwrap with the device's X25519 key (`decrypt_failed`, `hpke_short`);
5. store sealed with the storage key (`orgs.keys`, source `wrap`).

A refused wrap is ignored, audited (`runner.inbound_rejected`, source `wrap`) and counted once per wrap. A
`wrap_unauthorized` of a wrap addressed to this device is judged only against fresh logs: the browser stores a
`device_assign` and the device's wrap of that workgroup in one request, so the device can fetch the wrap before it has
verified the entry that entitles it. The step then first refreshes the platform log and the acting orgs' logs from
the hub (once per run, as the inbox's L10 refresh) and checks the wrap again; only a wrap still refused against the
hub's current logs is audited. While the logs cannot be refreshed the wrap is neither stored nor audited but tried
again later, and judged against the verified copy alone after `REFRESH_TRIES` such reconcile runs. A refusal of this
kind holds only for the verified log state it was judged against (`_position`): the wrap is checked again whenever
the logs advanced, without a second audit (decisions/HOT-04.md, replacing decisions/SCN-08.md items 3 and 4). A new
key wakes the outbox push, whose scope queue may be waiting for exactly that key (ADR-047).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import AGENT
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import CRED, WRAP_KEYS, check_cred_wrap, check_wrap, cred_readers
from agent_runner.crypto.wrap import unwrap_key, verify_wrap
from agent_runner.orgs import audit
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import FORMER_AGENT, held_slots, store_scope_key
from agent_runner.reconcile import logs, wire
from agent_runner.reconcile.engine import Run

TARGET = "/v1/device/wraps"
STORED_KEYS = WRAP_KEYS | {"wrapped_by", "created_at"}
ORG_SCOPES = frozenset({"org", "wg"})
LOG_DEPENDENT = frozenset({"wrap_unauthorized"})
"""Refusal codes that depend on the verified log state (W2/W3: wrapper or recipient not entitled yet)."""
REFRESH_TRIES = 3
"""Runs whose log refresh failed before a log-dependent refusal is judged against the verified copy alone."""


class Pending(Exception):
    """The wrap's org is not acting (yet): try again in a later run."""


def _slot(wrap: Mapping[str, Any]) -> tuple[str, str, int]:
    """`(scope, scope_id, epoch)` of a stored wrap (`header_invalid` for anything malformed)."""
    epoch = wrap.get("epoch")
    try:
        scope_id = wire.uuid_text(wrap.get("scope_id"), "wrap")
        wire.uuid_text(wrap.get("recipient_id"), "wrap")
        wire.uuid_text(wrap.get("wrapped_by"), "wrap")
    except wire.ResponseInvalid:
        scope_id = ""
    valid = (
        set(wrap) == STORED_KEYS
        and wrap.get("scope") in (*ORG_SCOPES, CRED)
        and scope_id != ""
        and isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and epoch >= 1
    )
    if not valid:
        raise CryptoError("header_invalid")
    return str(wrap["scope"]), scope_id, cast(int, epoch)


def former_agent(
    logs: list[VerifiedLog], item: Mapping[str, Any], wrapped_by: str, device: str, excluded: frozenset[str]
) -> bool:
    """A `cred` wrap a former agent of the owner made to this device while the device reads the owner's `CK` (module
    docstring); `wrap_unauthorized` for a forged signature."""
    owner = str(item["scope_id"])
    states = [log.state for log in logs]
    agent = next((p for s in states if (p := s.principals.get(wrapped_by)) is not None and p.kind == AGENT), None)
    if agent is None or agent.parent != owner or device not in cred_readers(states, owner, excluded):
        return False
    verify_wrap(
        b64u_decode(agent.sig, 32),
        scope=CRED,
        scope_id=owner,
        epoch=int(item["epoch"]),
        recipient=device,
        wrap=str(item["wrap"]),
        signature=str(item["sig"]),
    )
    return True


def check(book: OrgBook, kem: Any, wrap: Mapping[str, Any]) -> tuple[tuple[str, str, int], bytes, str]:
    """Verify one stored wrap for this device; returns its slot, the unwrapped key and its source (`CryptoError`,
    `Pending`)."""
    scope, scope_id, epoch = _slot(wrap)
    if wrap["recipient_id"] != book.device_id:
        raise CryptoError("wrap_unauthorized")
    item = {key: wrap[key] for key in WRAP_KEYS}
    wrapped_by = cast(str, wrap["wrapped_by"])
    source = "wrap"
    if scope in ORG_SCOPES:
        org = scope_id if scope == "org" else book.org_of_wg(scope_id)
        log = None if org is None else book.log(org)
        if org is None or log is None:
            raise Pending
        check_wrap(item, wrapped_by, log, book.excluded(org))
    else:
        acting = book.acting_orgs()
        logs = [book.logs[org] for org in acting]
        if not logs:
            raise Pending
        excluded = frozenset().union(*(book.excluded(org) for org in acting))
        try:
            check_cred_wrap(item, wrapped_by, logs, epoch, excluded)
        except CryptoError as refused:
            former = refused.code == "wrap_unauthorized" and former_agent(
                logs, item, wrapped_by, book.device_id, excluded
            )
            if not former:
                raise
            source = FORMER_AGENT
    key = unwrap_key(kem, scope=scope, scope_id=scope_id, epoch=epoch, recipient=book.device_id, wrap=item["wrap"])
    return (scope, scope_id, epoch), key, source


def _position(book: OrgBook, wrap: Mapping[str, Any]) -> tuple[object, ...]:
    """The verified log heads a check of `wrap` depends on: its org's (all acting orgs' for `cred` wraps) and the
    platform log's (decertified devices)."""
    scope, scope_id = wrap.get("scope"), wrap.get("scope_id")
    if scope in ORG_SCOPES and isinstance(scope_id, str):
        org = scope_id if scope == "org" else book.org_of_wg(scope_id)
        orgs = [] if org is None else [org]
    else:
        orgs = sorted(book.acting_orgs())
    heads = tuple((org, None if (head := book.head(org)) is None else head.seq) for org in orgs)
    platform = book.platform_head
    return heads, None if platform is None else platform.seq


def _target(wrap: Mapping[str, Any]) -> str:
    return ":".join(str(wrap.get(name, "")) for name in ("scope", "scope_id", "epoch"))


def _log_dependent(book: OrgBook, wrap: Mapping[str, Any], refused: CryptoError) -> bool:
    """The refusal may only mean that the verified logs are behind the hub's (a wrap for this device)."""
    return refused.code in LOG_DEPENDENT and wrap.get("recipient_id") == book.device_id


def _out_of_tries(book: OrgBook, fingerprint: tuple[object, ...]) -> bool:
    """Count one more run without a log refresh for this wrap; True from the `REFRESH_TRIES`th on."""
    return all(not book.remember_refusal(("unrefreshed", n, *fingerprint)) for n in range(REFRESH_TRIES - 1))


async def sync_wraps(run: Run, *, counting: bool = False) -> int:
    """Fetch and store new wraps; returns the number of keys stored.

    `counting`: this call is a reconcile run, which counts towards `REFRESH_TRIES` (the inbox's calls never judge a
    wrap whose logs could not be refreshed)."""
    answer = await run.sync.client.request("GET", TARGET)
    wraps = wire.array(answer.get("wraps"), "wraps")
    book = run.book
    kem = run.context.identity.keys.kem_key()
    stored = 0
    refreshed: bool | None = None  # outcome of this run's log refresh (None: not needed yet)
    async with session(run.pool) as conn:
        held = await held_slots(conn)
        for raw in wraps:
            wrap = raw if isinstance(raw, dict) else {}
            fingerprint: tuple[object, ...] = ("wrap", _target(wrap), wrap.get("wrapped_by"), wrap.get("sig"))
            slot = (wrap.get("scope"), wrap.get("scope_id"), wrap.get("epoch"))
            if slot in held or fingerprint in book.refused or (*fingerprint, _position(book, wrap)) in book.refused:
                continue
            try:
                try:
                    slot, key, source = check(book, kem, wrap)
                except CryptoError as ahead:
                    # "not entitled" of a wrap for this device may only mean the verified logs are behind the hub:
                    # judge it again against the hub's current logs, never before
                    if not _log_dependent(book, wrap, ahead):
                        raise
                    if refreshed is None:
                        refreshed = await logs.refresh_acting(run, conn)
                    try:
                        slot, key, source = check(book, kem, wrap)
                    except CryptoError as again:
                        # the logs could not all be refreshed: judged against the copy only after REFRESH_TRIES runs
                        unsure = not refreshed and _log_dependent(book, wrap, again)
                        if unsure and not (counting and _out_of_tries(book, fingerprint)):
                            continue
                        raise
            except Pending:
                continue
            except CryptoError as refused:
                log_dependent = _log_dependent(book, wrap, refused)
                book.remember_refusal((*fingerprint, _position(book, wrap)) if log_dependent else fingerprint)
                if not book.remember_refusal(("audited", *fingerprint)):
                    continue  # checked again after the logs advanced, still refused: audited once per wrap
                async with conn.transaction():
                    await audit.reject(
                        run.context,
                        run.sync,
                        conn,
                        source="wrap",
                        code=refused.code,
                        target_kind="wrap",
                        target_id=_target(wrap),
                    )
                continue
            async with conn.transaction():
                await store_scope_key(conn, run.sync.storage_key, slot, key, source=source)
            held.add(slot)
            stored += 1
    if stored:
        run.sync.pusher.notify()
    return stored


async def step(run: Run) -> None:
    await sync_wraps(run, counting=True)
