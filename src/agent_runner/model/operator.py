"""Operator subscriptions: the operator's Claude subscriptions for approved organizations (05-runner.md sections 1.3,
10.1, 10.2; ADR-052; KOR-23).

The operator puts access tokens of Claude subscriptions into `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR`, one file
`<name>.token` per subscription holding one line. The files are replaced from outside (atomically, `rename`) whenever
the token is renewed. Sessions of the organizations the verified platform log grants the operator's subscriptions
(`org_operator_subscriptions`, root-signed, ADR-079) may use this pool; an organization without such an entry may use
it while it is listed in `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS` (the list of ADR-052, kept for the move into the
log; an explicit entry of the log wins over it). A change of the log takes effect at the runner's next refresh of
the platform log, without a restart; for every other organization nothing changes. Towards the hub and the web
client the access kind is `claude_subscription` (`subscription`); that the operator's pool was used is only in the
runner log and the metrics.

- **Files** (`Pool.scan`): the directory must be a directory of the runner account, not a symbolic link, with no
  rights for group and others (0700); otherwise the whole pool is unusable. A token file must be a regular file of the
  runner account with no rights for group and others (0600); it is opened with `O_NOFOLLOW` and checked with `fstat`
  before a single byte is read, so a symbolic link, a file of another owner, a wider mode or anything else than a
  regular file is ignored, reported once in the log (`runner.model operator_file_ignored file=<name>
  reason=<reason>`) and never read. The content must be one access token of a Claude subscription (`sk-ant-oatNN-…`,
  the format check of the subscription login); a refresh token or anything else is ignored with `reason=format`.
  A file is read again only when its identity (device, inode, modification time, size) changed, so a renewed token is
  picked up at the next scan and an unchanged file costs one `open`/`fstat`.
- **Choice** (`Pool.choose`): among the usable files (not used up, ADR-081) the one with the most room in its quota
  (the lowest `used_percent` of its fullest window of the last reading, `quota.operator`; files without a known quota
  after those with one), then the one with the fewest bound sessions, then the one unused the longest (bound or
  answered last), then by name.
- **Binding** (`bind`, table `runner.operator_binding`): a session is bound to a file, never to a value. The gateway
  of the guest holds the token it was provisioned with; whenever the identity of the bound file changes the runner
  provisions the current content again (`model.gateways`, the task `operator_subscriptions` checks every
  `tasks.TICK_S`), so a short-lived token replaced in the file never ends the session. The binding survives a restart
  of the daemon.
- **Refusals** (`observed`, from the gateway's `usage` record of each request): `401`/`403` for the content the
  session was provisioned with blocks the file until its next change (not within `GRACE_S` after the session's gateway
  got a token, so a late record of a replaced token blocks nothing; at most one block or deferral per session within
  `LIMIT_S`, so a forged record of an untrusted guest cannot take the whole pool); the session's next renewal moves it
  to another usable file when there is one. `429` defers the file for `DEFER_S` for new choices only (the runner sees
  the status, not the `retry-after` header; a running session stays on its file, ADR-052 section 6). A `429` that is
  held against the file also counts towards its streak (`observe` answers True; `model.exhaustion`, ADR-081 section
  2): further `429` of the session that deferred the file count against the same file, never against another one
  within `LIMIT_S`. The blocks are kept in `runner.access_limit` with the file's identity (`model.limits`), so a
  restart keeps them until the file changes.
- **Used up** (ADR-081): a file whose access key `operator:<name>` is marked used up (`model.limits`) is
  `exhausted`: no new binding, and a bound session is moved to another usable file by its renewal (in place, the
  model stays); without one the gateway keeps what it holds and the reconcile step `model_fallback` pauses the
  session.
- **Models not in the plan** (HOT-26): an entry whose provider refused a model ("usage credits are required for this
  model", `model.limits.Limits.refuses`, for the content that was refused) is not chosen for a session on that model
  (`choose`/`pick`/`peek`/`used_up` with `model`); a session bound to it moves to another entry that is not known to
  refuse the model, by its renewal (`model.gateways.operator_target`); without one it keeps what it holds and waits
  (`model.fallback.renew`). It still serves other models.
- **Shared subscriptions** (KOR-69, ADR-090 section 3): further Claude subscriptions of users that the desired state
  lists (`subscriptions.shared`) join the pool as entries named `sub-<record ID>` (`refresh_shared`): the record is
  fetched and verified like every record of its owner (`credentials.subscriptions`) and used only for a session of an
  organization its **sealed** `use` allows - `orgs` names it, or `pool` and the owner holds the platform role
  `operator` while the verified platform log grants the organization the operator's subscriptions. A record without a
  token, without a use or that does not open is no entry. The entry's identity is its record version, so a new login
  ends a block like a renewed file; the choice, binding, blocks, deferrals and exhaustion are those of the files. A
  pool exists without the directory once the desired state lists a shared subscription (`ensure`); metrics, `doctor`
  and `operator_quota` show the token files only; the quota of a shared entry is read like a file's (`quota.operator`)
  for the choice and the exhaustion. A token file named `sub-...` is no part of the pool (the prefix is reserved).
- Tokens are kept in memory only (the cache of the current content of each file) and handed to the guest in the
  provisioning body; they never appear in a log line, a metric, an error or the store.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runner.credentials import subscriptions
from agent_runner.credentials.cache import CredentialUnavailable
from agent_runner.crypto.log.platform import PlatformState
from agent_runner.model import limits
from agent_runner.orgs.book import book_for
from agent_runner.orgs.db import session as store_session

ATTRIBUTE = "model_operator_pool"
SUFFIX = ".token"
NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})\.token$")
TOKEN_PREFIX = "sk-" + "ant-oat"
TOKEN_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[0-9]{2}-[A-Za-z0-9_-]{16,4000}")
"""The access token of a Claude subscription (the format check of `login.setup_token.TOKEN_RE`)."""
MAX_BYTES = 4096
DEFER_S = 300.0
GRACE_S = 60.0
"""A refusal within this time after the session's gateway got a token is not held against the file: the record may
belong to a request sent with the token before (a late record of an expired, already replaced token)."""
LIMIT_S = 600.0
"""One session blocks or defers at most one file within this time (the guest is untrusted: a forged refusal cannot
take the whole pool away)."""
REFUSED = frozenset({401, 403})
LIMITED = 429
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

USABLE = "usable"
BLOCKED = "blocked"
EXHAUSTED = "exhausted"
DEFERRED = "deferred"
IGNORED = "ignored"
STATES = (USABLE, BLOCKED, EXHAUSTED, DEFERRED, IGNORED)

Identity = tuple[int, int, int, int]
"""Device, inode, modification time (ns) and size of a token file (a shared subscription: `(0, 0, version, 0)`)."""
SHARED_PREFIX = "sub-"
OPERATOR_ROLE = "operator"
FETCH_RETRY_S = 60.0
"""A shared subscription whose record could not be fetched is not asked for again within this time."""
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Use:
    """The sealed use of a shared subscription: its owner, `pool` and the organizations."""

    owner: str
    pool: bool
    orgs: frozenset[str]
    version: int


def identity_text(identity: Identity) -> str:
    return ":".join(str(part) for part in identity)


def parse_identity(text: str) -> Identity:
    first, second, third, fourth = (int(part) for part in text.split(":"))
    return first, second, third, fourth


@dataclass(frozen=True)
class Entry:
    """One `<name>.token` of the last scan: its identity and token, or the reason it is ignored."""

    identity: Identity | None
    token: str | None
    reason: str | None

    def __repr__(self) -> str:
        return f"Entry(identity={self.identity!r}, token=<redacted>, reason={self.reason!r})"


@dataclass(frozen=True)
class Summary:
    """What the metrics and `doctor` show: no token, no organization."""

    problem: str | None
    states: dict[str, int]
    sessions: dict[str, int]
    newest_age_s: float | None


def parse_token(data: bytes) -> str | None:
    """The token of a file's content (one line), or None."""
    if len(data) > MAX_BYTES:
        return None
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return text if TOKEN_RE.fullmatch(text) else None


def _identity(info: os.stat_result) -> Identity:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def problem_of(info: os.stat_result, uid: int) -> str | None:
    """Why an opened directory or token file must not be used (owner, rights), or None."""
    if info.st_uid != uid:
        return "owner"
    if info.st_mode & 0o077:
        return "mode"
    return None


DIRECTORY_ERRORS = {errno.ENOENT: "missing", errno.ENOTDIR: "not_directory"}


@dataclass
class Pool:
    """The operator's pool of token files of one daemon (module docstring)."""

    directory: Path | None
    orgs: frozenset[str]
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    log: Callable[[str], None] = lambda _text: None
    uid: int = field(default_factory=os.geteuid)
    files: dict[str, Entry] = field(default_factory=dict)
    problem: str | None = None
    blocked: dict[str, Identity] = field(default_factory=dict)
    deferred: dict[str, float] = field(default_factory=dict)
    last_used: dict[str, float] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    """Session ID -> file name (mirror of `runner.operator_binding`)."""
    loaded: bool = False
    delivered_at: dict[str, float] = field(default_factory=dict)
    """Session ID -> time its gateway last got a token of the pool."""
    acted_at: dict[str, float] = field(default_factory=dict)
    """Session ID -> time a refusal of its requests last blocked or deferred a file."""
    acted_on: dict[str, str] = field(default_factory=dict)
    """Session ID -> the file of that last block or deferral."""
    exhausted: Callable[[str], bool] = lambda _name: False
    """The file's access is marked used up (`model.limits`; wired by `pool_for`)."""
    refuses: Callable[[str, str | None], bool] = lambda _name, _model: False
    """The file's current content refused the model ("not in the plan", `model.limits.Limits.refuses`; HOT-26)."""
    quota: dict[str, float] = field(default_factory=dict)
    """File name -> `used_percent` of its fullest window of the last reading (`model.limits.fullest`)."""
    reported: dict[str, object] = field(default_factory=dict)
    platform: Callable[[], PlatformState | None] = lambda: None
    """The verified platform state of the daemon (`orgs.book`), None before the first verification."""
    shared: dict[str, Entry] = field(default_factory=dict)
    """`sub-<record ID>` -> the entry of a shared subscription (`refresh_shared`)."""
    uses: dict[str, Use] = field(default_factory=dict)
    failed: dict[str, float] = field(default_factory=dict)
    """Record ID -> time its last fetch failed."""

    def __repr__(self) -> str:
        return f"Pool(files={sorted(self.files)!r}, shared={sorted(self.shared)!r}, problem={self.problem!r})"

    def entries(self) -> dict[str, Entry]:
        """The token files and the shared subscriptions."""
        return {**self.files, **self.shared}

    def allowed(self, name: str, org_id: str | None) -> bool:
        """`name` may serve a session of `org_id`: a file when the organization may use the pool (`allows`), a shared
        subscription when its sealed use allows the organization (module docstring)."""
        use = self.uses.get(name)
        if use is None:
            return name in self.files and self.allows(org_id)
        if org_id is None:
            return False
        if org_id in use.orgs:
            return True
        state = self.platform()
        return (
            use.pool
            and state is not None
            and state.has_role(use.owner, OPERATOR_ROLE)
            and org_id in state.operator_subscriptions
            and state.operator_subscribed(org_id)
        )

    def serves(self, org_id: str | None) -> bool:
        """The organization may use the files or a shared subscription of the pool."""
        return self.allows(org_id) or any(self.allowed(name, org_id) for name in self.shared)

    def allows(self, org_id: str | None) -> bool:
        """The organization may use the pool: by its latest `org_operator_subscriptions` in the verified platform log,
        and without one by the environment list (ADR-079 section 3)."""
        if org_id is None:
            return False
        state = self.platform()
        if state is not None and org_id in state.operator_subscriptions:
            return state.operator_subscribed(org_id)
        return org_id in self.orgs

    def note(self, key: str, value: object, text: str) -> None:
        """Log `text` when `key` changes to `value` (one line per change, not per scan)."""
        if self.reported.get(key) != value:
            self.reported[key] = value
            if value is not None:
                self.log(f"runner.model {text}")

    # -- files -------------------------------------------------------------------------------------------------------

    def scan(self) -> None:
        """Read the directory again (module docstring); without a directory there are no files."""
        if self.directory is None:
            self.files = {}
            known = self.shared
            self.blocked = {n: i for n, i in self.blocked.items() if n in known and known[n].identity == i}
            self.deferred = {n: until for n, until in self.deferred.items() if n in known and until > self.clock()}
            return
        try:
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            link = self.directory.is_symlink()  # macOS answers ENOTDIR for O_DIRECTORY | O_NOFOLLOW on a link
            self._unusable("symlink" if link else DIRECTORY_ERRORS.get(exc.errno or 0, "unreadable"))
            return
        try:
            problem = problem_of(os.fstat(descriptor), self.uid)
            if problem is not None:
                self._unusable(problem)
                return
            found: dict[str, Entry] = {}
            for entry in sorted(os.listdir(descriptor)):
                match = NAME_RE.fullmatch(entry)
                if match is not None and not match.group(1).startswith(SHARED_PREFIX):
                    found[match.group(1)] = self._entry(descriptor, entry, self.files.get(match.group(1)))
        finally:
            os.close(descriptor)
        self.problem = None
        self.note("dir", None, "")
        self.files = found
        for name in [*self.reported]:
            if name.startswith("file:") and name[5:] not in found:
                del self.reported[name]
        for name, item in found.items():
            self.note(f"file:{name}", item.reason, f"operator_file_ignored file={name} reason={item.reason}")
        now = self.clock()
        known = {**found, **self.shared}
        self.blocked = {n: i for n, i in self.blocked.items() if n in known and known[n].identity == i}
        self.deferred = {n: until for n, until in self.deferred.items() if n in known and until > now}

    def _unusable(self, problem: str) -> None:
        self.problem = problem
        self.files = {}
        self.note("dir", problem, f"operator_dir_unusable reason={problem}")

    def _entry(self, directory: int, entry: str, cached: Entry | None) -> Entry:
        try:
            descriptor = os.open(entry, FLAGS, dir_fd=directory)
        except OSError as exc:
            return Entry(None, None, "symlink" if exc.errno == errno.ELOOP else "unreadable")
        try:
            info = os.fstat(descriptor)
            identity = _identity(info)
            problem = "not_regular" if not stat.S_ISREG(info.st_mode) else problem_of(info, self.uid)
            if problem is not None:
                return Entry(identity, None, problem)
            if cached is not None and cached.identity == identity and cached.reason is None:
                return cached
            data = os.read(descriptor, MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        token = parse_token(data)
        return Entry(identity, token, None if token is not None else "format")

    def state(self, name: str) -> str:
        entry = self.entries().get(name)
        if entry is None or entry.token is None:
            return IGNORED
        if self.blocked.get(name) == entry.identity:
            return BLOCKED
        if self.exhausted(name):
            return EXHAUSTED
        if self.deferred.get(name, 0.0) > self.clock():
            return DEFERRED
        return USABLE

    def current(self, name: str) -> tuple[str, Identity] | None:
        """The current token of a file a session may stay on (usable, or deferred by a `429`), else None."""
        entry = self.entries().get(name)
        if self.state(name) not in (USABLE, DEFERRED):
            return None
        assert entry is not None
        assert entry.token is not None
        assert entry.identity is not None
        return entry.token, entry.identity

    def choose(self, exclude: Iterable[str] = (), org_id: str | None = None, model: str | None = None) -> str | None:
        """The usable entry for a new binding of a session of `org_id` (module docstring), or None; without an
        organization the token files only (their organizations were checked by the caller). With `model` an entry
        that refused that model is left out (HOT-26)."""
        skipped = set(exclude)
        usable = [
            name
            for name in self.entries()
            if name not in skipped
            and self.state(name) == USABLE
            and (name in self.files if org_id is None else self.allowed(name, org_id))
            and not self.refuses(name, model)
        ]
        if not usable:
            return None
        counts = Counter(self.bindings.values())

        def order(name: str) -> tuple[bool, float, int, float, str]:
            room = self.quota.get(name)
            used = (room is None, room or 0.0)
            return (*used, counts[name], self.last_used.get(name, float("-inf")), name)

        return min(usable, key=order)

    # -- refusals ----------------------------------------------------------------------------------------------------

    def observe(self, name: str, status: object, provisioned: Identity | None, session_id: str) -> bool:
        """The upstream answered a request of `session_id`, bound to `name`, with `status`; `provisioned` is the
        identity of the content that session's gateway holds (module docstring: refusals). True when a `429` is held
        against the file (it counts towards the file's streak)."""
        now = self.clock()
        self.last_used[name] = now
        if isinstance(status, bool) or not isinstance(status, int) or (status not in REFUSED and status != LIMITED):
            return False
        if now - self.delivered_at.get(session_id, float("-inf")) < GRACE_S:
            return False
        last = self.acted_at.get(session_id)
        acting = last is not None and now - last < LIMIT_S
        repeated = acting and status == LIMITED and self.acted_on.get(session_id) == name
        if acting and not repeated:
            self.note(f"limited:{session_id}", last, f"operator_refusal_ignored session_id={session_id} file={name}")
            return False
        self.scan()
        entry = self.entries().get(name)
        if entry is None or entry.identity is None:
            return False
        if status == LIMITED:
            if not repeated:
                self.deferred[name] = now + DEFER_S
                self.acted_at[session_id] = now
                self.acted_on[session_id] = name
                self.log(
                    f"runner.model operator_file_deferred file={name} seconds={int(DEFER_S)} session_id={session_id}"
                )
            return True
        if entry.identity == provisioned:
            self.blocked[name] = entry.identity
            self.acted_at[session_id] = now
            self.acted_on[session_id] = name
            self.log(f"runner.model operator_file_blocked file={name} status={status} session_id={session_id}")
        return False

    # -- summary -----------------------------------------------------------------------------------------------------

    def summary(self) -> Summary:
        states = dict.fromkeys(STATES, 0)
        for name in self.files:
            states[self.state(name)] += 1
        sessions = Counter({name: 0 for name in self.files if self.files[name].token is not None})
        sessions.update(self.bindings.values())
        stamps = [entry.identity[2] for entry in self.files.values() if entry.identity is not None]
        newest = None if not stamps else max(self.wall() - max(stamps) / 1e9, 0.0)
        return Summary(problem=self.problem, states=states, sessions=dict(sessions), newest_age_s=newest)


def configured(config: Any) -> tuple[Path, frozenset[str]] | None:
    """The directory and the environment's organizations of a configuration with the pool on (a directory), else
    None."""
    directory = getattr(config, "operator_subscription_dir", None)
    if not isinstance(directory, Path):
        return None
    return directory, frozenset(getattr(config, "operator_subscription_orgs", None) or ())


def platform_of(context: Any) -> Callable[[], PlatformState | None]:
    """The verified platform state of the daemon's book (`orgs.book`); always None for a context without identity."""
    if getattr(context, "identity", None) is None:
        return lambda: None
    return lambda: book_for(context).platform_state


def pool_for(context: Any) -> Pool | None:
    """The pool of a daemon context, built at first use; None when it is not configured."""
    if context is None:
        return None
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Pool):
        return found
    settings = configured(getattr(context, "config", None))
    if settings is None:
        return None

    def log(text: str) -> None:
        context.log.write(text + "\n")
        context.log.flush()

    created = Pool(directory=settings[0], orgs=settings[1], log=log, platform=platform_of(context))
    attach(context, created)
    setattr(context, ATTRIBUTE, created)
    return created


def attach(context: Any, pool: Pool) -> None:
    """Wire the pool to the daemon's limits (`model.limits`): a file marked used up is `exhausted`."""
    found = limits.limits_for(context)
    pool.exhausted = lambda name: found.active(limits.operator_key(name)) is not None

    def refuses(name: str, model: str | None) -> bool:
        entry = pool.entries().get(name)
        identity = None if entry is None or entry.identity is None else identity_text(entry.identity)
        return found.refuses(limits.operator_key(name), model, identity)

    pool.refuses = refuses


# -- bindings (`runner.operator_binding`) ------------------------------------------------------------------------------


async def load(context: Any, pool: Pool) -> None:
    """The bindings and the persisted blocks of the store, once per daemon."""
    if pool.loaded:
        return
    async with store_session(context.pool) as conn:
        cursor = await conn.execute("SELECT session_id::text, file_name FROM runner.operator_binding")
        rows = await cursor.fetchall()
    pool.bindings.update({row[0]: row[1] for row in rows})
    found = await limits.load(context)
    for name, identity in found.blocks().items():
        pool.blocked.setdefault(name, parse_identity(identity))
    pool.loaded = True


async def keep_blocks(context: Any, pool: Pool) -> None:
    """Persist the pool's blocks (`runner.access_limit`): new ones are written, ended ones (a renewed or removed
    file, `scan`) are deleted."""
    found = await limits.load(context)
    stored = found.blocks()
    for name, identity in pool.blocked.items():
        text = identity_text(identity)
        if stored.get(name) != text:
            limit = limits.Limit(
                key=limits.operator_key(name),
                kind=limits.BLOCKED,
                until=None,
                readable=False,
                limited_at=found.wall(),
                identity=text,
            )
            await limits.mark(context, limit)
    await limits.release(context, [limits.operator_key(name) for name in stored if name not in pool.blocked])


async def bound(context: Any, session_id: str) -> str | None:
    """The file `session_id` is bound to; None when the pool is off or the session is not bound."""
    pool = pool_for(context)
    if pool is None:
        return None
    await load(context, pool)
    return pool.bindings.get(session_id)


async def bind(context: Any, pool: Pool, session_id: str, name: str) -> None:
    await load(context, pool)
    pool.bindings[session_id] = name  # before the store's round trip: a concurrent start sees it
    pool.last_used[name] = pool.delivered_at[session_id] = pool.clock()
    async with store_session(context.pool) as conn:
        await conn.execute(
            "INSERT INTO runner.operator_binding (session_id, file_name) VALUES (%s, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET file_name = EXCLUDED.file_name, bound_at = now()",
            (session_id, name),
        )
        # the origin outlives the binding: usage processed after the session ended is still charged (KOR-31)
        await conn.execute(
            "INSERT INTO runner.operator_origin (session_id) VALUES (%s) ON CONFLICT DO NOTHING", (session_id,)
        )


def delivered(context: Any, session_id: str) -> None:
    """The gateway of a bound session got the current content of its file (starts the grace time)."""
    pool = pool_for(context)
    assert pool is not None  # only a provisioning from the pool is reported
    pool.delivered_at[session_id] = pool.clock()


async def unbind(context: Any, session_id: str) -> None:
    """The session no longer uses the pool (a resume chose another access)."""
    pool = pool_for(context)
    if pool is None:
        return
    await load(context, pool)
    if pool.bindings.pop(session_id, None) is None:
        return
    async with store_session(context.pool) as conn:
        await conn.execute("DELETE FROM runner.operator_binding WHERE session_id = %s", (session_id,))


async def prune(context: Any, keep: set[str]) -> None:
    """Drop the bindings of sessions that are no longer live (they count no more for the choice)."""
    pool = pool_for(context)
    if pool is None:
        return
    await load(context, pool)
    await keep_blocks(context, pool)
    gone = [sid for sid in pool.bindings if sid not in keep]
    if not gone:
        return
    async with store_session(context.pool) as conn:
        await conn.execute("DELETE FROM runner.operator_binding WHERE session_id = ANY(%s::uuid[])", (gone,))
    for sid in gone:
        del pool.bindings[sid]
        pool.reported.pop(f"exhausted:{sid}", None)
        pool.reported.pop(f"limited:{sid}", None)
        pool.delivered_at.pop(sid, None)
        pool.acted_at.pop(sid, None)
        pool.acted_on.pop(sid, None)


async def observed(
    context: Any, session_id: str, status: object, provisioned: Identity | None
) -> tuple[str, bool] | None:
    """The gateway's `usage` record of a request of `session_id` (module docstring: refusals): the file the session
    is bound to and whether a `429` counts towards its streak; None when the session is not bound."""
    name = await bound(context, session_id)
    if name is None:
        return None
    pool = pool_for(context)
    assert pool is not None
    blocked = dict(pool.blocked)
    counts = pool.observe(name, status, provisioned, session_id)
    if pool.blocked != blocked:
        await keep_blocks(context, pool)
    return name, counts


async def pick(context: Any, org_id: str, model: str | None = None) -> tuple[str, str, Identity] | None:
    """A usable entry for a new session of `org_id` on `model`: `(name, token, identity)`; None when the organization
    may use neither the files nor a shared subscription, or none is usable (an entry that refused the model is
    none)."""
    pool = await refreshed(context)
    if pool is None or not pool.serves(org_id):
        return None
    name = pool.choose(org_id=org_id, model=model)
    if name is None:
        pool.log(f"runner.model operator_pool_empty problem={pool.problem or 'none'}")
        return None
    current = pool.current(name)
    assert current is not None
    return name, *current


async def peek(context: Any, org_id: str, model: str | None = None) -> str | None:
    """The entry `pick` would take for a session of `org_id` on `model` now, without binding or logging (ADR-081: what
    a resume would get)."""
    pool = await refreshed(context)
    if pool is None or not pool.serves(org_id):
        return None
    return pool.choose(org_id=org_id, model=model)


def used_up(context: Any, org_id: str, model: str | None = None) -> list[tuple[float, str]]:
    """`(release time, name)` of the used-up entries of the pool for a session of `org_id` on `model`, earliest first;
    empty when the organization may use none of them (ADR-081 section 3: the pool's step waits for the earliest
    release; an entry that refused the model is no release, HOT-26)."""
    pool = pool_for(context)
    if pool is None:
        return []
    found = limits.limits_for(context)
    released: list[tuple[float, str]] = []
    for name in pool.entries():
        limit = found.active(limits.operator_key(name))
        allowed = pool.allowed(name, org_id) and not pool.refuses(name, model)
        if allowed and pool.state(name) == EXHAUSTED and limit is not None and limit.until is not None:
            released.append((limit.until, name))
    return sorted(released)


def held(context: Any, name: str) -> tuple[str, Identity] | None:
    """The token and identity of a used-up entry a waiting session starts on (ADR-081 section 3), else None."""
    pool = pool_for(context)
    entry = None if pool is None else pool.entries().get(name)
    if pool is None or entry is None or pool.state(name) != EXHAUSTED:
        return None
    assert entry.token is not None
    assert entry.identity is not None
    return entry.token, entry.identity


# -- shared subscriptions (KOR-69, ADR-090 section 3) ------------------------------------------------------------


def listed_shared(desired: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """`(record ID, owner)` of `subscriptions.shared` of the desired state (malformed rows are left out)."""
    section = None if desired is None else desired.get("subscriptions")
    rows = section.get("shared") if isinstance(section, Mapping) else None
    found: list[tuple[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        record = row.get("subscription_id") if isinstance(row, Mapping) else None
        owner = row.get("owner_id") if isinstance(row, Mapping) else None
        if (
            isinstance(record, str)
            and isinstance(owner, str)
            and _UUID_RE.fullmatch(record)
            and _UUID_RE.fullmatch(owner)
        ):
            found.append((record, owner))
    return found


def _desired(context: Any) -> Mapping[str, Any] | None:
    from agent_runner.model.gateways import running_service  # noqa: PLC0415 - gateways imports this module

    service = running_service(context)
    return None if service is None else service.env.desired


def ensure(context: Any) -> Pool | None:
    """The pool of the daemon; one without the directory is made once the desired state lists a shared
    subscription (module docstring)."""
    found = pool_for(context)
    if found is not None or context is None or getattr(context, "identity", None) is None:
        return found
    if not listed_shared(_desired(context)):
        return None

    def log(text: str) -> None:
        context.log.write(text + "\n")
        context.log.flush()

    created = Pool(directory=None, orgs=frozenset(), log=log, platform=platform_of(context))
    attach(context, created)
    setattr(context, ATTRIBUTE, created)
    return created


def _use(owner: str, version: int, value: object) -> Use | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("pool"), bool):
        return None
    orgs = value.get("orgs")
    if not isinstance(orgs, list) or not all(isinstance(org, str) and _UUID_RE.fullmatch(org) for org in orgs):
        return None
    return Use(owner=owner, pool=value["pool"], orgs=frozenset(orgs), version=version)


async def _shared_entry(context: Any, pool: Pool, record_id: str, owner: str) -> bool:
    """Read one listed shared subscription into the pool; True when it is an entry now."""
    name = SHARED_PREFIX + record_id
    async with store_session(context.pool) as conn:
        found = await subscriptions.cached(conn, record_id)
    if found is None and pool.clock() - pool.failed.get(record_id, float("-inf")) >= FETCH_RETRY_S:
        try:
            found = await subscriptions.fetch(context, record_id)
        except Exception as exc:
            pool.failed[record_id] = pool.clock()
            pool.note(f"shared:{record_id}", type(exc).__name__, f"shared_unusable id={record_id} reason=fetch")
    if found is None or found.owner != owner or found.primary:
        return False
    known = pool.uses.get(name)
    if known is not None and known.version == found.version and name in pool.shared:
        return True
    try:
        async with subscriptions.opened(context, found) as body:
            token, use = parse_token(str(body.get("token", "")).encode()), _use(owner, found.version, body.get("use"))
    except CredentialUnavailable as exc:
        pool.note(f"shared:{record_id}", exc.reason, f"shared_unusable id={record_id} reason={exc.reason}")
        return False
    if token is None or use is None:
        pool.note(f"shared:{record_id}", "unused", f"shared_unusable id={record_id} reason=unused")
        return False
    pool.shared[name] = Entry((0, 0, found.version, 0), token, None)
    pool.uses[name] = use
    pool.reported.pop(f"shared:{record_id}", None)
    return True


async def refresh_shared(context: Any, pool: Pool) -> None:
    """The shared subscriptions of the desired state as entries of the pool (module docstring)."""
    kept = {
        SHARED_PREFIX + record_id
        for record_id, owner in listed_shared(_desired(context))
        if await _shared_entry(context, pool, record_id, owner)
    }
    for name in [name for name in pool.shared if name not in kept]:
        del pool.shared[name]
        pool.uses.pop(name, None)


async def refreshed(context: Any) -> Pool | None:
    """The pool (`ensure`) with its bindings and blocks loaded, the directory scanned and the shared subscriptions
    read again."""
    pool = ensure(context)
    if pool is None:
        return None
    await load(context, pool)
    await refresh_shared(context, pool)
    pool.scan()
    return pool
