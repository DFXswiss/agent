"""The orchestrator work of the reconcile step (05-runner.md section 17.1; ADR-082 sections 6.2-6.3 and 9.6-9.8).

`GET /v1/device/state` lists `orchestrators: [{session_id, step: "keys" | "handover" | "revoked", pairing_id}]`
(`work_items`; malformed items are left out, a missing list is no work). The hub is untrusted (00-overview.md section
3): each item only names a session; the runner decides from its own rows and its verified logs.

`fit` - a session may become the main orchestrator here only when it is a local session that runs (`working` or
`idle`) as a main session (`depth` 0) on a running VM of this **platform** device, in an organization that is acting,
**private** in the verified log, and whose task owner is an active user there and its **only** active user; else the
reason (log line `orchestrator_skipped`). The relay checks the organization part (`org_reason`) on every request.

`keys(env, session_id)` - the hub lists `keys` only while the session has no live pairing there, so every item asks the
guest for fresh keys (also after a registered pairing expired or a handed-over one was not claimed in time; the guest
refuses while its orchestrator is claimed). The guest draws the keys (`orchestrator_keys` with the hub's origin, its
pinned key and the platform root key); the runner checks the guest's answer (well-formed keys, the fingerprint of these
keys, the proof of possession for this pairing), signs `orchestrator_key_text(fp, pairing_id, session)` with the
device key and registers the pairing (`POST /v1/device/orchestrator-pairings`); the hub must name the same pairing.
The row is `registered`.

`handover(env, session_id, pairing_id)` - only for this device's own `registered` pairing of the session: the
session view of the hub (`GET /v1/device/sessions/{id}/orchestrator`) must name this session, its task owner and this
pairing with a stored handover `{ct, sig}`, and `sig` must verify under the task owner's signing key **from the
verified log** over `orchestrator_handover_text(ct, pairing, session)`; anything else is `handover_refused` and nothing
reaches the guest. The handover goes into the VM unopened with the owner's public keys from the log; the row is
`handed_over`.

`revoked(env, session_id, pairing_id)` - the hub revoked this device's pairing while the session goes on (replaced, or
revoked by its user): the row ends (`ended`: no relay, still no checkpoint) and the guest ends its orchestrator.

`sweep(env)` - at every step: the rows of sessions that ended (or are unknown) are deleted with a checkpoint still due
for them; the guest of a VM that still runs ends the orchestrator first.

Each item ends in an outcome code (also the log line); guest and hub failures of one item never stop the others.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner.crypto import agent as agent_format
from agent_runner.crypto import fields
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import USER
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.orchestrator import store
from agent_runner.orchestrator.env import OrchestratorEnv
from agent_runner.sessions import records
from agent_runner.sessions.records import LocalSession
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.vm import records as vm_records
from agent_runner.vm.records import VmRow

STEPS = ("keys", "handover", "revoked")
RUNNING = frozenset({"working", "idle"})
HUB_KEY_ID = "pinned"
"""The key ID handed to the tool with the hub key: the device pins the key itself, not the hub's name for it."""
REGISTER = "/v1/device/orchestrator-pairings"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FAILURES = (GuestError, HubRefusal, HubTransportError, RunnerError)


@dataclass(frozen=True)
class Item:
    session_id: str
    step: str
    pairing_id: str | None


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def work_items(desired: Mapping[str, Any]) -> list[Item]:
    """The well-formed items of `orchestrators` (a `handover` needs its pairing), in order."""
    found = desired.get("orchestrators")
    items: list[Item] = []
    for item in found if isinstance(found, list) else []:
        if not isinstance(item, Mapping) or not _uuid(item.get("session_id")) or item.get("step") not in STEPS:
            continue
        pairing = item.get("pairing_id")
        if not (_uuid(pairing) or (pairing is None and item["step"] == "keys")):  # handover, revoked: a pairing
            continue
        items.append(Item(session_id=item["session_id"], step=item["step"], pairing_id=pairing))
    return items


def _shape(session: LocalSession) -> str | None:
    if session.depth != 0:
        return "not_main"
    if session.state not in RUNNING or session.vm_id is None:
        return "not_running"
    return None


def _org(env: OrchestratorEnv, session: LocalSession) -> str | None:
    if env.book.org_device:
        return "not_platform"
    log = env.book.log(session.org_id)
    if log is None:
        return "org_not_acting"
    if not log.state.private:
        return "not_private"
    owner = None if session.owner_id is None else log.state.member(session.owner_id)
    if owner is None or owner.kind != USER:
        return "owner_unknown"
    users = [found.id for found in log.state.principals.values() if found.kind == USER and found.active]
    return None if users == [owner.id] else "not_sole_user"


def org_reason(env: OrchestratorEnv, session: LocalSession) -> str | None:
    """The organization part of `fit` (platform device, private, the owner its only user) for the relay."""
    return _org(env, session)


async def fit(env: OrchestratorEnv, session: LocalSession | None) -> str | None:
    """None when the session may be the orchestrator here, else the reason (module docstring)."""
    if session is None:
        return "unknown_session"
    reason = _shape(session) or _org(env, session)
    if reason is not None:
        return reason
    assert session.vm_id is not None  # _shape
    vm = await vm_records.get(env.db, session.vm_id)
    return None if vm is not None and vm.state == "running" else "not_running"


async def _session(env: OrchestratorEnv, session_id: str) -> tuple[LocalSession | None, store.Pairing | None]:
    async with env.sessions.transaction() as conn:
        return await records.get(conn, session_id), await store.get(conn, session_id)


async def _vm(env: OrchestratorEnv, session: LocalSession) -> VmRow:
    assert session.vm_id is not None  # fit
    vm = await vm_records.get(env.db, session.vm_id)
    assert vm is not None  # fit
    return vm


def _checked_keys(answer: Mapping[str, Any]) -> tuple[str, dict[str, str], str, str]:
    """`(pairing_id, pub, pop, fp)` of the guest's answer (`CryptoError` when they do not belong together)."""
    pairing_id = fields.uuid(answer.get("pairing_id"))
    pub = agent_format.public_keys(answer.get("pub"))
    kem, sig = b64u_decode(pub["kem"], 32), b64u_decode(pub["sig"], 32)
    fp, pop = answer.get("fp"), answer.get("pop")
    if fp != agent_format.fingerprint(kem, sig) or set(answer) != {"pairing_id", "pub", "pop", "fp"}:
        raise CryptoError("header_invalid")
    agent_format.verify_pop(pairing_id, kem, sig, pop)
    return pairing_id, pub, str(pop), str(fp)


async def keys(env: OrchestratorEnv, session_id: str) -> str:
    session, _row = await _session(env, session_id)
    reason = await fit(env, session) or (None if env.platform_root_key is not None else "root_key_missing")
    if reason is not None:
        return _skipped(env, session_id, reason)
    assert session is not None  # fit
    try:
        outcome = await _register(env, session)
    except FAILURES as error:
        outcome = f"keys_failed error={type(error).__name__}"
    return _done(env, session_id, outcome)


async def _register(env: OrchestratorEnv, session: LocalSession) -> str:
    """Keys from the guest, checked, registered at the hub with the device's signature; the outcome."""
    request = {
        "hub_url": env.hub_url,
        "hub_key": {"id": HUB_KEY_ID, "public_key": env.hub_key},
        "platform_root_key": env.platform_root_key,
    }
    async with await env.sessions.client(await _vm(env, session)) as guest:
        answer = await guest.orchestrator_keys(session.id, request)
    try:
        pairing_id, pub, pop, fp = _checked_keys(answer)
    except CryptoError:
        return "keys_invalid"
    text = agent_format.orchestrator_key_text(fp, pairing_id, session.id)
    body = {
        "session_id": session.id,
        "pairing_id": pairing_id,
        "pub": pub,
        "pop": pop,
        "fp": fp,
        "device_sig": agent_format.sign_text(env.signing_key, text),
    }
    registered = await env.hub.request("POST", REGISTER, body)
    if registered.get("pairing_id") != pairing_id:
        return "register_invalid"
    async with env.sessions.transaction() as conn:
        await store.register(conn, session.id, pairing_id, env.now())
    return "registered"


def _handover_of(view: Mapping[str, Any], session: LocalSession, pairing_id: str) -> tuple[str, str] | None:
    """`(ct, sig)` when the hub's view names this session, its owner and this pairing with a handover."""
    pairing = view.get("pairing")
    handover = pairing.get("handover") if isinstance(pairing, Mapping) else None
    if (
        view.get("session_id") != session.id
        or view.get("user_id") != session.owner_id
        or not isinstance(pairing, Mapping)
        or pairing.get("pairing_id") != pairing_id
        or not isinstance(handover, Mapping)
        or not isinstance(handover.get("ct"), str)
        or not isinstance(handover.get("sig"), str)
    ):
        return None
    return str(handover["ct"]), str(handover["sig"])


async def handover(env: OrchestratorEnv, session_id: str, pairing_id: str) -> str:
    session, row = await _session(env, session_id)
    if row is None or row.pairing_id != pairing_id:
        return _refused(env, session_id, "not_ours")
    if row.state == "handed_over":
        return "handed_over"
    reason = await fit(env, session)
    if reason is not None:
        return _skipped(env, session_id, reason)
    assert session is not None  # fit
    try:
        request = await _verified(env, session, pairing_id)
        if isinstance(request, str):
            return _refused(env, session_id, request)
        async with await env.sessions.client(await _vm(env, session)) as guest:
            await guest.orchestrator_handover(session_id, request)
    except FAILURES as error:
        return _done(env, session_id, f"handover_failed error={type(error).__name__}")
    async with env.sessions.transaction() as conn:
        await store.handed_over(conn, session_id, env.now())
    return _done(env, session_id, "handed_over")


async def _verified(env: OrchestratorEnv, session: LocalSession, pairing_id: str) -> dict[str, Any] | str:
    """The guest's handover request when the hub's handover is the task owner's signed one, else the refusal."""
    assert session.owner_id is not None  # fit
    owner = env.book.log(session.org_id).state.member(session.owner_id)
    view = await env.hub.request("GET", f"/v1/device/sessions/{session.id}/orchestrator")
    found = _handover_of(view, session, pairing_id)
    if found is None:
        return "view_invalid"
    ct, sig = found
    try:
        text = agent_format.orchestrator_handover_text(ct, pairing_id, session.id)
        agent_format.verify_text(b64u_decode(owner.sig, 32), sig, text)
    except CryptoError:
        return "signature_invalid"
    return {
        "pairing_id": pairing_id,
        "ct": ct,
        "user_id": session.owner_id,
        "user_pub": {"kem": owner.kem, "sig": owner.sig},
    }


def _done(env: OrchestratorEnv, session_id: str, outcome: str) -> str:
    env.line(f"{outcome} session_id={session_id}")
    return outcome.split(" ", 1)[0]


def _skipped(env: OrchestratorEnv, session_id: str, reason: str) -> str:
    env.line(f"orchestrator_skipped session_id={session_id} reason={reason}")
    return reason


def _refused(env: OrchestratorEnv, session_id: str, reason: str) -> str:
    env.line(f"handover_refused session_id={session_id} reason={reason}")
    return f"refused_{reason}"


async def _end_in_guest(env: OrchestratorEnv, session: LocalSession | None, pairing_id: str) -> str:
    """Ask the guest of the session's running VM to end its orchestrator; the outcome."""
    vm = None if session is None or session.vm_id is None else await vm_records.get(env.db, session.vm_id)
    if session is None or vm is None or vm.state != "running":
        return "ended"
    try:
        async with await env.sessions.client(vm) as guest:
            await guest.orchestrator_end(session.id, pairing_id)
    except FAILURES as error:
        return f"end_failed error={type(error).__name__}"
    return "ended"


async def revoked(env: OrchestratorEnv, session_id: str, pairing_id: str) -> str:
    async with env.sessions.transaction() as conn:
        session = await records.get(conn, session_id)
        changed = await store.ended(conn, session_id, pairing_id)
    if not changed:
        return "not_ours"
    return _done(env, session_id, await _end_in_guest(env, session, pairing_id))


async def sweep(env: OrchestratorEnv) -> int:
    """Delete the rows of ended sessions (module docstring); the number deleted."""
    async with env.sessions.transaction() as conn:
        rows = await store.of_ended_sessions(conn)
    for row in rows:
        async with env.sessions.transaction() as conn:
            session = await records.get(conn, row.session_id)
        await _end_in_guest(env, session, row.pairing_id)
        async with env.sessions.transaction() as conn:
            await store.forget(conn, row.session_id)
        env.line(f"orchestrator_forgotten session_id={row.session_id}")
    return len(rows)


async def run_item(env: OrchestratorEnv, item: Item) -> str:
    if item.step == "keys":
        return await keys(env, item.session_id)
    assert item.pairing_id is not None  # work_items
    if item.step == "revoked":
        return await revoked(env, item.session_id, item.pairing_id)
    return await handover(env, item.session_id, item.pairing_id)
