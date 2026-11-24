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
- **Choice** (`Pool.choose`): among the usable files the one with the fewest bound sessions, then the one unused the
  longest (bound or answered last), then by name.
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
  the status, not the `retry-after` header; a running session stays on its file, ADR-052 section 6).
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runner.crypto.log.platform import PlatformState
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
DEFERRED = "deferred"
IGNORED = "ignored"
STATES = (USABLE, BLOCKED, DEFERRED, IGNORED)

Identity = tuple[int, int, int, int]
"""Device, inode, modification time (ns) and size of a token file."""


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

    directory: Path
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
    reported: dict[str, object] = field(default_factory=dict)
    platform: Callable[[], PlatformState | None] = lambda: None
    """The verified platform state of the daemon (`orgs.book`), None before the first verification."""

    def __repr__(self) -> str:
        return f"Pool(files={sorted(self.files)!r}, problem={self.problem!r})"

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
        """Read the directory again (module docstring)."""
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
                if match is not None:
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
        self.blocked = {n: i for n, i in self.blocked.items() if n in found and found[n].identity == i}
        self.deferred = {n: until for n, until in self.deferred.items() if n in found and until > now}

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
        entry = self.files.get(name)
        if entry is None or entry.token is None:
            return IGNORED
        if self.blocked.get(name) == entry.identity:
            return BLOCKED
        if self.deferred.get(name, 0.0) > self.clock():
            return DEFERRED
        return USABLE

    def current(self, name: str) -> tuple[str, Identity] | None:
        """The current token of a file a session may stay on (usable, or deferred by a `429`), else None."""
        entry = self.files.get(name)
        if self.state(name) not in (USABLE, DEFERRED):
            return None
        assert entry is not None
        assert entry.token is not None
        assert entry.identity is not None
        return entry.token, entry.identity

    def choose(self, exclude: Iterable[str] = ()) -> str | None:
        """The usable file for a new binding (module docstring), or None."""
        skipped = set(exclude)
        usable = [name for name in self.files if name not in skipped and self.state(name) == USABLE]
        if not usable:
            return None
        counts = Counter(self.bindings.values())
        return min(usable, key=lambda name: (counts[name], self.last_used.get(name, float("-inf")), name))

    # -- refusals ----------------------------------------------------------------------------------------------------

    def observe(self, name: str, status: object, provisioned: Identity | None, session_id: str) -> None:
        """The upstream answered a request of `session_id`, bound to `name`, with `status`; `provisioned` is the
        identity of the content that session's gateway holds (module docstring: refusals)."""
        now = self.clock()
        self.last_used[name] = now
        if isinstance(status, bool) or not isinstance(status, int) or (status not in REFUSED and status != LIMITED):
            return
        if now - self.delivered_at.get(session_id, float("-inf")) < GRACE_S:
            return
        last = self.acted_at.get(session_id)
        if last is not None and now - last < LIMIT_S:
            self.note(f"limited:{session_id}", last, f"operator_refusal_ignored session_id={session_id} file={name}")
            return
        self.scan()
        entry = self.files.get(name)
        if entry is None or entry.identity is None:
            return
        if status == LIMITED:
            self.deferred[name] = now + DEFER_S
            self.acted_at[session_id] = now
            self.log(f"runner.model operator_file_deferred file={name} seconds={int(DEFER_S)} session_id={session_id}")
        elif entry.identity == provisioned:
            self.blocked[name] = entry.identity
            self.acted_at[session_id] = now
            self.log(f"runner.model operator_file_blocked file={name} status={status} session_id={session_id}")

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
    setattr(context, ATTRIBUTE, created)
    return created


# -- bindings (`runner.operator_binding`) ------------------------------------------------------------------------------


async def load(context: Any, pool: Pool) -> None:
    """The bindings of the store, once per daemon."""
    if pool.loaded:
        return
    async with store_session(context.pool) as conn:
        cursor = await conn.execute("SELECT session_id::text, file_name FROM runner.operator_binding")
        rows = await cursor.fetchall()
    pool.bindings.update({row[0]: row[1] for row in rows})
    pool.loaded = True


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


async def observed(context: Any, session_id: str, status: object, provisioned: Identity | None) -> None:
    """The gateway's `usage` record of a request of `session_id` (module docstring: refusals)."""
    name = await bound(context, session_id)
    if name is not None:
        pool = pool_for(context)
        assert pool is not None
        pool.observe(name, status, provisioned, session_id)


async def pick(context: Any, org_id: str) -> tuple[str, str, Identity] | None:
    """A usable file for a new session of `org_id`: `(name, token, identity)`; None when the organization is not
    approved or no file is usable."""
    pool = pool_for(context)
    if pool is None or not pool.allows(org_id):
        return None
    await load(context, pool)
    pool.scan()
    name = pool.choose()
    if name is None:
        pool.log(f"runner.model operator_pool_empty problem={pool.problem or 'none'}")
        return None
    current = pool.current(name)
    assert current is not None
    return name, *current
