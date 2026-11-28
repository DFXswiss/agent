"""Model access of a session start: selection, credential and gateway provisioning body (05-runner.md sections 6.1
step 3, 10.1-10.3, 2.6; 03-api.md section 7.1; ADR-010, ADR-026, ADR-052; KOR-50, ADR-081 sections 1 and 3).

`resolve(env, desired)` is called by the session start (and the resume) of the session area. It walks the steps the
owner's preference allows (`plan`: `auto` = own subscription -> own API key -> operator pool -> platform; the explicit
`subscription` = own subscription -> operator pool; an explicit `api_key` or `platform` only its own kind, without any
fallback) and takes the first access this device can serve that is not used up (`model.limits`):

- `subscription` / `api_key`: the owner's `claude_subscription` / `anthropic_api_key` credential opens on this device
  (`credentials.cache.opened`: cached record, else fetched on demand; verified, decrypted for this call only). A
  missing record skips the step; a record this device cannot open (no `CK`), a malformed body or a hub/transport
  failure while fetching makes it unusable for now (`owner_status`). The owner's own subscription is skipped while
  `own:<owner_id>` is used up;
- operator subscription (`model.operator`, KOR-23, ADR-052): a session of an organization the verified platform log
  (or `OPERATOR_SUBSCRIPTION_ORGS`) grants the pool takes a usable file that is not used up, the one with the most
  room first (`operator.Pool.choose`). It is reported as `subscription` like the owner's own; the session is bound to
  the file (`operator.bind`) and the stamp carries the file's name and identity, so the renewal follows the file;
- `platform`: a usable `OPENROUTER_KEY_FILE` of a platform device, or the limited DFX key of an org device
  (`SessionEnv.platform_key(org_id)`, ADR-081 section 7; skipped while `dfxkey:<device_id>` is limited). When the
  organization has spent its daily platform budget (`platform_budget` of `GET /v1/device/state`) the start is refused
  with `budget_exceeded` (the gateway refuses further requests of running sessions itself with `429 budget_exceeded`).

**Cost classes** (ADR-081 section 1): the owner's own subscription and API key are `own`; the operator pool and
`platform` are `dfx`. An owner **belongs to `own`** when a step of the preference names an own credential and the
owner has such a record (`owner_status` is not `MISSING`); then a `dfx` step is taken only when the owner's setting
`fallback.own_to_dfx` or the task's consent allows it (`model.policy`), never silently - neither because the own
access is used up nor because it cannot be used right now. `platform` after a used-up operator pool needs
`fallback.operator_to_platform` (default on). The result depends only on the stored limits, the latest quota
readings, the bindings and the settings. A session the desired state does not list yet (a subagent right after its
`subagent.request`) follows the settings of its parent's entry (`policy.settings`, HOT-31), so it crosses to a `dfx`
access exactly when its main session would. A subagent that asked for a `model_id` (`Desired.model_required`) takes
only steps whose catalog allows that model (`serves`): a DeepSeek model therefore goes to `platform` instead of
silently running a Claude model on a subscription, under the same class rules; `refusal` names why a probe found no
access for it.

Nothing allowed is left: with a release time among the skipped accesses (an own credential that cannot be used now:
`now + limits.RECHECK_S`) the result is a **wait** (`Wait`: `until` = the earliest release, `choice` = a `dfx` access
is barred only by the crossing rule, never while the organization's credit is exhausted). A new **main session** then
starts on the skipped access with the earliest release (`Resolved.wait`; its pause is recorded by `hold`, and
`model.session_request` keeps its first prompt back); without one that can be started (an own credential that does
not open) and without any release time the start fails with `model_access_unavailable` as before. A **subagent**
fails with `model_quota_exhausted` (towards a hub without `model_fallback.api`: `model_access_unavailable`).
`decide(env, desired)` answers what a start or resume would get now without binding anything (`model.fallback`).

A resume that moves a session to another access because its access was used up, could not be used, or was released
again writes the audit `session.model_switched` (towards a hub with `model_fallback.api`).

The result carries the `Choice` (kind, model, model environment of 05 section 10.3) and the body of `PUT
.../credential` (`model.provision`), which holds the secret: it is handed to the guest and dropped, never logged or
stored. What was provisioned (kind, owner, record version, budget, a digest of the platform key - never the key) is
remembered per daemon (`Provisions`) so the refresh of `model.gateways` sees changes.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from agent_runner.billing import state as billing
from agent_runner.credentials import cache
from agent_runner.errors import RunnerError
from agent_runner.model import dfxkey, exhaustion, limits, operator, pauses, policy, provision, selection, uncounted
from agent_runner.model.limits import Limits
from agent_runner.model.selection import Choice
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions import records
from agent_runner.sessions.desired import Desired
from agent_runner.sessions.records import time_text

UNAVAILABLE = "model_access_unavailable"
QUOTA = "model_quota_exhausted"
UNKNOWN_VERSION = -1
"""Stamp version when the record changed while it was opened: the next renewal provisions again."""
BUDGET = "budget_exceeded"
DEFAULT_UPSTREAM_ANTHROPIC = "https://api.anthropic.com"
ATTRIBUTE = "model_provisions"
OPERATOR = "operator"
"""The step of the operator's pool in the order of `resolve` (reported as `subscription`)."""


class AccessRefused(Exception):
    """No model access for the session: `reason` is a session failure reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Stamp:
    """What a provisioning was built from (no secret): compared by the refresh."""

    access: str
    owner_id: str
    version: int | None = None
    key_digest: str | None = None
    budget: tuple[float, float] | None = None
    operator: tuple[str, operator.Identity] | None = None
    """Name and identity of the operator's token file the provisioning was built from (KOR-23)."""


@dataclass
class Provisions:
    """Session ID -> `Stamp` of its last provisioning, per daemon."""

    stamps: dict[str, Stamp] = field(default_factory=dict)
    subscribed: bool = False
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    """One lock per session: renewal and withdrawal of a session never interleave (`model.gateways`)."""
    withdrawn: set[str] = field(default_factory=set)

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())


def provisions(context: Any) -> Provisions:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Provisions):
        return found
    created = Provisions()
    setattr(context, ATTRIBUTE, created)
    return created


@dataclass(frozen=True)
class Wait:
    """No allowed access is left (ADR-081 section 3): the earliest release `until` among the skipped accesses and
    `choice` = a DFX access would be available but the crossing rule bars it."""

    until: float
    choice: bool


@dataclass(frozen=True)
class Resolved:
    choice: Choice
    credential: dict[str, Any]
    stamp: Stamp
    label: str = ""
    """`subscription`, `api_key` (the owner's own), `operator` (a file of the operator's pool) or `platform`."""
    key: str | None = None
    """The access key of an access that can run out (`model.limits`), else None."""
    wait: Wait | None = None
    """Set when the session starts on the access with the earliest release and waits (ADR-081 section 3)."""

    def __repr__(self) -> str:
        return f"Resolved(access={self.choice.access!r}, model_id={self.choice.model_id!r}, credential=<redacted>)"


def upstream_anthropic(context: Any) -> str:
    """`AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC` of the daemon configuration."""
    config = getattr(context, "config", None)
    value = getattr(config, "model_upstream_anthropic", None)
    return value if isinstance(value, str) else DEFAULT_UPSTREAM_ANTHROPIC


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def budget_stamp(budget: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """What a renewal compares: the limit and the amount (`journal_through` alone does not renew)."""
    return None if budget is None else (budget["usd_per_day"], budget["spent_usd"])


def _line(env: Any, text: str) -> None:
    env.log(f"runner.model {text}")


MISSING = "missing"
UNUSABLE = "unusable"


async def owner_secret(env: Any, owner: str, credential_kind: str) -> tuple[str, int] | None:
    """`(secret, record version)` of the owner's credential when it opens on this device, else None (`owner_status`)."""
    found = await owner_status(env, owner, credential_kind)
    return None if isinstance(found, str) else found


async def owner_status(env: Any, owner: str, credential_kind: str) -> tuple[str, int] | str:
    """`(secret, record version)` of the owner's credential when it opens on this device, `MISSING` when the owner has
    no such record (or there is no daemon context to ask), else `UNUSABLE` (no `CK`, a malformed body, the hub or
    the transport failed while fetching). The version is the cached one before and after opening; when they differ
    (a new version arrived meanwhile) it is `UNKNOWN_VERSION`, so the stamp never claims a version the gateway does
    not hold."""
    if env.context is None:
        return MISSING
    return await _opened(env, owner, credential_kind)


async def _opened(env: Any, owner: str, credential_kind: str) -> tuple[str, int] | str:
    context = env.context
    try:
        async with store_session(context.pool) as conn:
            before = await cache.cached(conn, owner, credential_kind)
        async with cache.opened(context, owner, credential_kind) as body:
            secret = provision.owner_secret(credential_kind, body)
        async with store_session(context.pool) as conn:
            found = await cache.cached(conn, owner, credential_kind)
    except cache.CredentialUnavailable as exc:
        if exc.reason == "missing":
            return MISSING
        _line(env, f"credential_unusable owner={owner} kind={credential_kind} reason={exc.reason}")
        return UNUSABLE
    except Exception as exc:
        # hub refusal or transport error while fetching on demand: unavailable for this start
        _line(env, f"credential_fetch_failed owner={owner} kind={credential_kind} error={type(exc).__name__}")
        return UNUSABLE
    if secret is None:
        _line(env, f"credential_unusable owner={owner} kind={credential_kind} reason=body")
        return UNUSABLE
    if found is None or (before is not None and before[0] != found[0]):
        return secret, UNKNOWN_VERSION
    return secret, found[0]


def _select(env: Any, request: selection.Request) -> Choice | None:
    try:
        return selection.select(request, catalog=env.catalog, strategies=env.strategies)
    except RunnerError:
        return None


async def platform_body(
    env: Any, org_id: str, key: str, vm_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The platform provisioning with the budget of the organization: the hub's amount plus what it has not counted
    yet, and the journal position of the session's VM (`model.uncounted`, KOR-10)."""
    desired = env.desired  # one snapshot: the reconcile step may replace it while the store is read
    budget = await uncounted.budget(env, desired, provision.platform_budget(desired, org_id), org_id, vm_id)
    body = provision.platform_credential(key=key, upstream=env.upstream_openrouter, catalog=env.catalog, budget=budget)
    return body, budget


def plan(kinds: tuple[str, ...]) -> list[str]:
    """The steps of `resolve`: the access kinds of the preference plus the operator's pool after the subscription
    (and the API key), before `platform`."""
    steps = list(kinds)
    if "subscription" in steps:
        steps.insert(steps.index("platform") if "platform" in steps else len(steps), OPERATOR)
    return steps


# -- the walk over the steps (ADR-081 section 3) ------------------------------------------------------------------

Build = Callable[[], Awaitable[Resolved]]
PROBE_KEY = "probe"
"""The key a probe (`decide`) builds with: nothing it builds is ever provisioned."""
DFX_STEPS = frozenset({OPERATOR, "platform"})
KIND_OF = {"subscription": "subscription", "api_key": "api_key", OPERATOR: OPERATOR, "platform": "platform"}
"""The access value of the audit `session.model_switched` of each label (the operator's pool as `operator`)."""


@dataclass
class Walk:
    """What the walk over the steps found: an access, or the skipped accesses with their release time (and how to
    start on them), whether a DFX access is barred only by the crossing rule, and whether an own credential could
    not be used."""

    found: Resolved | None = None
    releases: list[tuple[float, Build | None]] = field(default_factory=list)
    choice: bool = False
    unusable: bool = False
    fitting: list[str] = field(default_factory=list)
    """The steps whose access serves the required model (`Desired.model_required`; every step without one)."""
    barred: bool = False
    """A step that serves the required model was barred by the crossing rule."""
    budget: bool = False
    """A probe found the organization's platform budget spent."""

    def wait(self, state: Any, org_id: str) -> Wait | None:
        if not self.releases:
            return None
        until = min(when for when, _build in self.releases)
        return Wait(until=until, choice=self.choice and not billing.exhausted(state, org_id))

    def earliest(self) -> Build | None:
        """How to start on the skipped access with the earliest release (an own credential that does not open: none)."""
        startable = [(when, index, build) for index, (when, build) in enumerate(self.releases) if build is not None]
        return min(startable, key=lambda item: item[:2])[2] if startable else None


async def _limits(env: Any) -> Limits | None:
    return None if env.context is None else await limits.load(env.context)


def _active(known: Limits | None, key: str | None) -> limits.Limit | None:
    return None if known is None or key is None else known.active(key)


def _now(env: Any, known: Limits | None) -> float:
    return env.wall() if known is None else known.wall()


async def _operator_resolved(
    env: Any, desired: Desired, base: selection.Request, name: str, held: tuple[str, operator.Identity], *, bind: bool
) -> Resolved:
    token, identity = held
    choice = _select(env, replace(base, preference="subscription", credentials=frozenset({"subscription"})))
    assert choice is not None  # the owner's subscription strategy always serves a credential it is given
    body = provision.owner_credential(
        kind="subscription", secret=token, upstream=upstream_anthropic(env.context), catalog=env.catalog
    )
    if bind:
        pool = operator.pool_for(env.context)
        assert pool is not None
        await operator.bind(env.context, pool, desired.session_id, name)
        _line(env, f"operator_subscription session_id={desired.session_id} file={name}")
    stamp = Stamp(access="subscription", owner_id=desired.owner_id, operator=(name, identity))
    return Resolved(choice=choice, credential=body, stamp=stamp, label=OPERATOR, key=limits.operator_key(name))


async def _own_step(
    env: Any, desired: Desired, base: selection.Request, kind: str, status: tuple[str, int] | str, *, walk: Walk
) -> None:
    known = await _limits(env)
    if isinstance(status, str):
        walk.unusable = True
        assert known is not None  # without a daemon context no own credential is known (`owner_status`)
        walk.releases.append((known.recheck(f"{limits.own_key(desired.owner_id)}:{kind}", known.wall()), None))
        return
    secret, version = status
    choice = _select(env, replace(base, preference=kind, credentials=frozenset({kind})))
    if choice is None:
        return
    key = limits.own_key(desired.owner_id) if kind == "subscription" else None

    async def build() -> Resolved:
        upstream = upstream_anthropic(env.context)
        body = provision.owner_credential(kind=kind, secret=secret, upstream=upstream, catalog=env.catalog)
        stamp = Stamp(access=kind, owner_id=desired.owner_id, version=version)
        return Resolved(choice=choice, credential=body, stamp=stamp, label=kind, key=key)

    limit = _active(known, key)
    if limit is not None:
        assert limit.until is not None
        walk.releases.append((limit.until, build))
        return
    walk.found = await build()


async def _operator_step(env: Any, desired: Desired, base: selection.Request, walk: Walk, *, probe: bool) -> bool:
    """The operator's pool; True when the organization may use it and every file is used up."""
    if probe:
        name = await operator.peek(env.context, desired.org_id)
        pool = operator.pool_for(env.context)
        held = None if name is None or pool is None else pool.current(name)
        if name is not None and held is not None:
            walk.found = await _operator_resolved(env, desired, base, name, held, bind=False)
            return False
    else:
        picked = await operator.pick(env.context, desired.org_id)
        if picked is not None:
            name, token, identity = picked
            walk.found = await _operator_resolved(env, desired, base, name, (token, identity), bind=True)
            return False
    used = operator.used_up(env.context, desired.org_id)
    if not used:
        return False
    until, first = used[0]

    async def build() -> Resolved:
        held = operator.held(env.context, first)
        assert held is not None  # a used-up file stays in the pool while the walk runs
        return await _operator_resolved(env, desired, base, first, held, bind=not probe)

    walk.releases.append((until, build))
    return True


def _dfx_key(env: Any) -> str | None:
    return limits.dfx_key(env.device_id) if env.device_kind == "org" else None


def _key_held(env: Any, org_id: str) -> bool:
    """A key of the `platform` access exists for the organization, without opening a DFX envelope (an org device asks
    `model.dfxkey.has_key`)."""
    if env.device_kind == "org" and env.context is not None:
        return dfxkey.has_key(env.context, org_id)
    return env.platform_key(org_id) is not None


async def _platform_step(env: Any, desired: Desired, base: selection.Request, walk: Walk, *, probe: bool) -> None:
    # a probe builds no credential that is ever sent: it only learns whether a key exists
    key = (PROBE_KEY if _key_held(env, desired.org_id) else None) if probe else env.platform_key(desired.org_id)
    if key is None:
        return
    choice = _select(env, replace(base, preference="platform", platform_key_available=True))
    if choice is None:
        return
    dfx = _dfx_key(env)

    async def build() -> Resolved:
        body, budget = await platform_body(env, desired.org_id, key)
        if provision.budget_exhausted(budget):
            raise AccessRefused(BUDGET)
        stamp = Stamp(
            access="platform", owner_id=desired.owner_id, key_digest=key_digest(key), budget=budget_stamp(budget)
        )
        return Resolved(choice=choice, credential=body, stamp=stamp, label="platform", key=dfx)

    limit = _active(await _limits(env), dfx)
    if limit is not None:
        assert limit.until is not None
        walk.releases.append((limit.until, build))
        return
    if not probe:
        walk.found = await build()
        return
    try:
        walk.found = await build()
    except AccessRefused:
        walk.budget = True


async def _offered(env: Any, desired: Desired, step: str) -> bool:
    """A `dfx` step that the crossing rule bars would have an access now (the `choice` of a wait)."""
    if step == OPERATOR:
        return await operator.peek(env.context, desired.org_id) is not None
    return _key_held(env, desired.org_id) and _active(await _limits(env), _dfx_key(env)) is None


def serves(env: Any, desired: Desired, step: str) -> bool:
    """The access of `step` may run the session's model: always, unless the model is required
    (`Desired.model_required`) and the catalog does not allow it for the step's access kind."""
    if not desired.model_required:
        return True
    kind = "subscription" if step == OPERATOR else step
    return desired.model_id_pref in env.catalog.policy(kind).allowed


def refusal(env: Any, desired: Desired, found: Walk) -> tuple[str, str] | None:
    """Why a subagent that requires its model gets no access for it (operation error code and sentence), None when it
    gets one or only waits for a used-up access (the start then fails `model_quota_exhausted` as for any subagent).
    Only a probe's walk (`walk(probe=True)`) is judged."""
    if not desired.model_required or found.found is not None or found.releases:
        return None
    model = desired.model_id_pref
    if found.budget:
        return BUDGET, f"The model {model} needs DFX model access and the organization's platform budget is spent."
    if found.barred:
        return "model_not_allowed", (
            f"The model {model} needs DFX model access: the owner's setting fallback.own_to_dfx or the task's"
            " consent to DFX access must allow it."
        )
    if not found.fitting:
        return "model_not_allowed", (
            f"The model {model} is not served by any model access of the owner's preference"
            f" {desired.model_access_pref}."
        )
    return "model_not_allowed", f"No model access that serves the model {model} is available on this device."


async def walk(env: Any, desired: Desired, *, probe: bool = False) -> Walk:
    """The steps of the preference (`plan`) in order, skipping used-up accesses; a step after the owner's own
    credential that is a `dfx` step only with the crossing rule (`policy.Settings.crossing`), `platform` after a
    used-up pool only with `operator_to_platform` (module docstring of `model.fallback`). `probe` changes nothing
    (no binding, no budget refusal)."""
    try:
        kinds = selection.order(desired.model_access_pref)
    except RunnerError:
        raise AccessRefused(UNAVAILABLE) from None
    base = selection.Request(
        preference=desired.model_access_pref,
        model_pref=desired.model_id_pref,
        device_kind=env.device_kind,
        owner_id=desired.owner_id,
    )
    settings = policy.settings(
        env.desired,
        desired.session_id,
        task_id=desired.task_id,
        owner_id=desired.owner_id,
        parent_id=desired.parent_session_id,
    )
    found = Walk()
    own = pool_used_up = False
    for step in plan(kinds):
        fits = serves(env, desired, step)
        if step not in DFX_STEPS:
            status = await owner_status(env, desired.owner_id, provision.CREDENTIAL_KINDS[step])
            if status == MISSING:
                continue
            own = True  # the owner belongs to `own` even when the required model needs another access
            if fits:
                found.fitting.append(step)
                await _own_step(env, desired, base, step, status, walk=found)
        elif not fits:
            continue
        elif own and not settings.crossing:
            found.fitting.append(step)
            found.barred = True
            found.choice = found.choice or await _offered(env, desired, step)
            continue
        elif step == OPERATOR:
            found.fitting.append(step)
            pool_used_up = await _operator_step(env, desired, base, found, probe=probe)
        else:
            if pool_used_up and not settings.operator_to_platform:
                continue
            found.fitting.append(step)
            await _platform_step(env, desired, base, found, probe=probe)
        if found.found is not None:
            break
    return found


async def decide(env: Any, desired: Desired) -> Resolved | Wait | None:
    """What a start or resume of the session would get now, without binding anything: an access, a wait, or None
    (no access and no release time)."""
    try:
        found = await walk(env, desired, probe=True)
    except AccessRefused:
        return None
    return found.found or found.wait(env.desired, desired.org_id)


async def resolve(env: Any, desired: Desired) -> Resolved:
    """The model access of a session start or resume (module docstring); `AccessRefused` when there is none. With a
    wait a main session starts on the skipped access with the earliest release and its pause is recorded (`hold`); a
    subagent fails with `model_quota_exhausted` (towards a hub without `model_fallback.api`:
    `model_access_unavailable`)."""
    previous = None if env.context is None else await _previous(env, desired.session_id)
    found = await walk(env, desired)
    resolved = found.found
    if resolved is None:
        wait = found.wait(env.desired, desired.org_id)
        if wait is None:
            raise AccessRefused(UNAVAILABLE)
        if desired.depth > 0:
            raise AccessRefused(QUOTA if policy.api(env.desired) else UNAVAILABLE)
        build = found.earliest()
        if build is None:
            raise AccessRefused(UNAVAILABLE)
        resolved = replace(await build(), wait=wait)
    if env.context is not None:
        provisions(env.context).stamps[desired.session_id] = resolved.stamp
        if resolved.stamp.operator is None:
            await operator.unbind(env.context, desired.session_id)
        await _audit(env, desired, previous, resolved, found.unusable)
        if resolved.wait is not None:
            await hold(env, desired, resolved)
    return resolved


async def _previous(env: Any, session_id: str) -> tuple[str, str | None, str | None] | None:
    """`(label, access key, model)` the session ran on before this resolution, None for a new session."""
    async with store_session(env.context.pool) as conn:
        session = await records.get(conn, session_id)
    if session is None or session.model_access is None:
        return None
    name = await operator.bound(env.context, session_id) if session.model_access == "subscription" else None
    label = session.model_access if name is None else OPERATOR
    return label, exhaustion.access_key(env, session, name), session.model_id


async def _audit(
    env: Any, desired: Desired, previous: tuple[str, str | None, str | None] | None, found: Resolved, unusable: bool
) -> None:
    """`session.model_switched` when a resume moves the session to another access because its access was used up,
    could not be used, or was released again (ADR-081 section 5); only towards a hub with `model_fallback.api`."""
    if previous is None or previous[0] == found.label:
        return
    label, key, model = previous
    limit = _active(await _limits(env), key)
    async with store_session(env.context.pool) as conn:
        paused = await pauses.ever(conn, desired.session_id)
    if limit is not None:
        cause, until = "exhausted", limit.until
    elif paused:
        cause, until = "released", None
    elif unusable and label in ("subscription", "api_key"):
        cause, until = "unusable", None
    else:
        return
    _line(env, f"access_switched session_id={desired.session_id} from={label} to={found.label} cause={cause}")
    if not policy.api(env.desired):
        return
    detail = {
        "from_access": KIND_OF[label],
        "to_access": KIND_OF[found.label],
        "from_model": model,
        "to_model": found.choice.model_id,
        "cause": cause,
        "until": None if until is None else time_text(until),
    }
    audit = {
        "action": "session.model_switched",
        "result": "ok",
        "task_id": desired.task_id,
        "session_id": desired.session_id,
        "target_kind": "session",
        "target_id": desired.session_id,
        "detail": detail,
    }
    async with env.transaction() as conn:
        await env.outbox.enqueue(conn, "audit", audit)


async def hold(env: Any, desired: Desired, found: Resolved) -> None:
    """Record the wait of a main session that starts (or resumes) on a used-up access: paused before its first
    prompt (`model.pauses`, `model.fallback`)."""
    wait = found.wait
    assert wait is not None
    async with env.transaction() as conn:
        await pauses.record(
            conn,
            desired.session_id,
            kind=pauses.WAIT,
            interrupted=False,
            at=_now(env, await _limits(env)),
            until=wait.until,
            choice=wait.choice,
            from_access=found.label,
            from_model=found.choice.model_id,
        )
        await pauses.report(conn, env, desired.session_id, wait.until, wait.choice)
    pauses.pend(env.context, desired.session_id)
    _line(
        env,
        f"model_wait session_id={desired.session_id} until={time_text(wait.until)} choice={str(wait.choice).lower()}",
    )
