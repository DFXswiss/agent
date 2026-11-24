"""U-RUN-model: the operator's pool of subscription token files (KOR-23, ADR-052; 05-runner.md sections 1.3, 10.1,
10.2): rights, owner, symbolic links and format of the directory and the files, the cache over the file identity,
the choice, blocking and deferring, the summary of the metrics and `doctor`.
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner.login import setup_token
from agent_runner.model import operator
from agent_runner.model.operator import BLOCKED, DEFERRED, IGNORED, USABLE, Pool

ORG = "0b9c6a52-4a35-4c1e-9d8e-8f3a0e8d5b11"
MARKER = "OPERATORMARKER"


def token(n: int = 1) -> str:
    return operator.TOKEN_PREFIX + "01-" + f"{MARKER}{n:04d}" + "a1B2_c3-D4" * 3


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def pool_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "pool"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def put(directory: Path, name: str, content: str, mode: int = 0o600) -> Path:
    """Replace `<name>.token` atomically, as the operator's renewal does."""
    path = directory / f"{name}.token"
    temporary = directory / f".{name}.tmp"
    temporary.write_text(content)
    temporary.chmod(mode)
    os.replace(temporary, path)
    return path


def make(directory: Path, lines: list[str] | None = None, clock: Clock | None = None) -> Pool:
    log = lines.append if lines is not None else (lambda _text: None)
    return Pool(directory=directory, orgs=frozenset({ORG}), clock=clock or Clock(), wall=lambda: 5000.0, log=log)


def test_the_token_format_is_the_one_of_the_subscription_login() -> None:
    assert operator.TOKEN_RE.pattern == setup_token.TOKEN_RE.pattern
    assert operator.parse_token(token().encode() + b"\n") == token()
    assert operator.parse_token(b"  " + token().encode() + b"\r\n") == token()
    for bad in (
        b"",
        b"sk-" + b"ant-ort01-" + b"x" * 40,  # a refresh token is not an access token
        token().encode() + b"\n" + token(2).encode(),  # two lines
        token().encode() + b" trailing",
        token().encode()[:-1] + b"\xff",
        operator.TOKEN_PREFIX.encode() + b"01-" + b"a" * operator.MAX_BYTES,
    ):
        assert operator.parse_token(bad) is None


def test_usable_files_are_read_and_other_names_are_not_part_of_the_pool(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "alpha", token(1) + "\n")
    put(directory, "beta-2.x", token(2))
    (directory / "README").write_text("not a token file")
    (directory / ".hidden.token").write_text(token(3))
    (directory / "gamma.token.bak").write_text(token(4))
    pool = make(directory)
    pool.scan()
    assert pool.problem is None
    assert sorted(pool.files) == ["alpha", "beta-2.x"]
    assert [pool.state(name) for name in ("alpha", "beta-2.x", "missing")] == [USABLE, USABLE, IGNORED]
    assert pool.current("alpha") is not None
    assert pool.current("alpha")[0] == token(1)  # type: ignore[index]
    assert pool.current("missing") is None


def test_rights_owner_links_and_format_make_a_file_ignored_reported_once_and_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "good", token(1))
    put(directory, "wide", token(2), mode=0o640)
    put(directory, "closed", token(3), mode=0o000)
    put(directory, "format", "sk-" + "ant-ort01-" + "x" * 40)
    put(directory, "foreign", token(5))
    target = put(tmp_path, "elsewhere", token(6))
    (directory / "link.token").symlink_to(target)
    os.mkfifo(directory / "fifo.token", 0o600)
    real_fstat, real_read = os.fstat, os.read
    foreign = (directory / "foreign.token").stat().st_ino

    def fstat(fd: int) -> os.stat_result:
        found = real_fstat(fd)
        if found.st_ino != foreign:
            return found
        values = list(found)
        values[4] = found.st_uid + 1  # st_uid: a file of another account
        return os.stat_result(values)

    read: list[int] = []

    def tracked_read(fd: int, size: int) -> bytes:
        read.append(real_fstat(fd).st_ino)
        return real_read(fd, size)

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "read", tracked_read)
    lines: list[str] = []
    pool = make(directory, lines)
    pool.scan()
    reasons = {name: entry.reason for name, entry in pool.files.items()}
    assert reasons == {
        "closed": "unreadable",
        "fifo": "not_regular",
        "foreign": "owner",
        "format": "format",
        "good": None,
        "link": "symlink",
        "wide": "mode",
    }
    readable = {(directory / f"{name}.token").stat().st_ino for name in ("good", "format")}
    assert set(read) == readable  # nothing else was read
    assert {name: pool.state(name) for name in reasons} == {
        name: USABLE if reason is None else IGNORED for name, reason in reasons.items()
    }
    assert pool.current("wide") is None
    ignored = [line for line in lines if "operator_file_ignored" in line]
    assert sorted(ignored) == sorted(
        f"runner.model operator_file_ignored file={name} reason={reason}"
        for name, reason in reasons.items()
        if reason is not None
    )
    pool.scan()
    assert len([line for line in lines if "operator_file_ignored" in line]) == 6  # once per change, not per scan
    (directory / "wide.token").chmod(0o600)
    (directory / "link.token").unlink()
    pool.scan()
    assert pool.state("wide") == USABLE
    assert "link" not in pool.files
    (directory / "wide.token").chmod(0o644)
    pool.scan()
    assert lines[-1] == "runner.model operator_file_ignored file=wide reason=mode"
    assert all(MARKER not in line for line in lines)


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (lambda d: d.rmdir(), "missing"),
        (lambda d: d.chmod(0o750), "mode"),
        (lambda d: d.chmod(0o000), "unreadable"),
        (lambda d: (d.rmdir(), d.write_text("x")), "not_directory"),
        (lambda d: (d.rmdir(), d.symlink_to(d.parent / "real", target_is_directory=True)), "symlink"),
    ],
)
def test_an_unusable_directory_makes_the_whole_pool_unusable(tmp_path: Path, prepare: Any, reason: str) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    put(real, "a", token())
    directory = pool_dir(tmp_path)
    put(directory, "a", token())
    lines: list[str] = []
    pool = make(directory, lines)
    pool.scan()
    assert pool.files
    for child in directory.iterdir():
        child.unlink()
    prepare(directory)
    try:
        pool.scan()
        pool.scan()
    finally:
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o700)
    assert (pool.problem, pool.files, pool.choose()) == (reason, {}, None)
    assert lines == [f"runner.model operator_dir_unusable reason={reason}"]
    assert pool.summary().problem == reason


def test_a_directory_of_another_account_is_unusable(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token())
    pool = make(directory)
    pool.uid = os.geteuid() + 1
    pool.scan()
    assert pool.summary().problem == "owner"
    pool.uid = os.geteuid()
    pool.scan()
    assert (pool.problem, pool.state("a")) == (None, USABLE)


def test_a_file_is_read_again_only_when_it_was_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token(1))
    reads: list[int] = []
    real_read = os.read

    def counted(fd: int, size: int) -> bytes:
        reads.append(fd)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", counted)
    pool = make(directory)
    pool.scan()
    first = pool.current("a")
    pool.scan()
    pool.scan()
    assert len(reads) == 1
    assert pool.current("a") == first
    put(directory, "a", token(2))
    pool.scan()
    assert len(reads) == 2
    found = pool.current("a")
    assert found is not None
    assert first is not None
    assert found[0] == token(2)
    assert found[1] != first[1]


def test_the_choice_spreads_sessions_and_prefers_the_file_unused_longest(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    for name in ("a", "b", "c"):
        put(directory, name, token())
    clock = Clock()
    pool = make(directory, clock=clock)
    pool.scan()
    assert pool.choose() == "a"  # nothing used yet: by name
    pool.bindings = {"s1": "a", "s2": "b"}
    assert pool.choose() == "c"  # fewest sessions
    pool.bindings["s3"] = "c"
    pool.last_used = {"a": 10.0, "b": 5.0, "c": 20.0}
    assert pool.choose() == "b"  # all equal: unused the longest
    assert pool.choose(exclude=("b",)) == "a"
    assert pool.choose(exclude=("a", "b", "c")) is None


def test_401_and_403_block_the_provisioned_content_until_the_file_changes(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token(1))
    put(directory, "b", token(2))
    lines: list[str] = []
    clock = Clock()
    pool = make(directory, lines, clock)
    pool.scan()
    held = pool.current("a")
    assert held is not None
    clock.now = 1001.0
    pool.observe("a", 401, held[1], "s1")
    assert pool.state("a") == BLOCKED
    assert pool.current("a") is None
    assert pool.choose() == "b"
    assert pool.last_used["a"] == 1001.0
    assert lines[-1] == "runner.model operator_file_blocked file=a status=401 session_id=s1"
    pool.scan()
    assert pool.state("a") == BLOCKED  # still the same content
    put(directory, "a", token(3))
    pool.scan()
    assert pool.state("a") == USABLE  # renewed: the block is gone
    assert pool.blocked == {}
    renewed = pool.current("a")
    assert renewed is not None
    pool.observe("a", 403, held[1], "s2")  # a refusal of the content before the renewal
    assert pool.state("a") == USABLE
    pool.observe("a", 403, renewed[1], "s3")
    assert pool.state("a") == BLOCKED
    pool.observe("gone", 401, renewed[1], "s4")
    assert "gone" not in pool.blocked
    assert all(MARKER not in line for line in lines)


def test_429_defers_a_file_for_new_choices_but_keeps_its_sessions(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token(1))
    put(directory, "b", token(2))
    lines: list[str] = []
    clock = Clock()
    pool = make(directory, lines, clock)
    pool.scan()
    pool.observe("a", 429, None, "s1")
    assert pool.state("a") == DEFERRED
    assert pool.current("a") is not None  # a running session stays
    assert pool.choose() == "b"
    assert lines[-1] == f"runner.model operator_file_deferred file=a seconds={int(operator.DEFER_S)} session_id=s1"
    clock.now += operator.DEFER_S - 1
    pool.scan()
    assert pool.state("a") == DEFERRED
    clock.now += 2
    pool.scan()
    assert (pool.state("a"), pool.deferred) == (USABLE, {})
    pool.observe("gone", 429, None, "s2")
    assert "gone" not in pool.deferred


@pytest.mark.parametrize("status", [200, 400, 500, 503, True, "401", None, {"x": 1}, [401]])
def test_other_statuses_only_mark_the_file_used(tmp_path: Path, status: object) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token())
    pool = make(directory)
    pool.scan()
    held = pool.current("a")
    assert held is not None
    pool.observe("a", status, held[1], "s1")
    assert pool.state("a") == USABLE
    assert "a" in pool.last_used


def test_the_summary_counts_states_and_sessions_and_the_newest_change(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    empty = make(directory)
    empty.scan()
    assert empty.summary() == operator.Summary(
        problem=None, states=dict.fromkeys(operator.STATES, 0), sessions={}, newest_age_s=None
    )
    for name in ("a", "b", "c"):
        put(directory, name, token())
    put(directory, "bad", token(), mode=0o644)
    os.utime(directory / "a.token", ns=(4_000_000_000_000, 4_000_000_000_000))
    os.utime(directory / "b.token", ns=(4_900_000_000_000, 4_900_000_000_000))
    os.utime(directory / "c.token", ns=(4_500_000_000_000, 4_500_000_000_000))
    os.utime(directory / "bad.token", ns=(4_950_000_000_000, 4_950_000_000_000))
    pool = make(directory)
    pool.scan()
    held = pool.current("a")
    assert held is not None
    pool.observe("a", 401, held[1], "s1")
    pool.observe("b", 429, None, "s2")
    pool.bindings = {"s1": "b", "s2": "b"}
    summary = pool.summary()
    assert summary.states == {USABLE: 1, BLOCKED: 1, DEFERRED: 1, IGNORED: 1}
    assert summary.sessions == {"a": 0, "b": 2, "c": 0}
    assert summary.newest_age_s == pytest.approx(50.0)  # wall 5000 - newest change 4950
    pool.wall = lambda: 1.0
    assert pool.summary().newest_age_s == 0.0  # a clock behind the file never shows a negative age


def test_the_pool_of_a_daemon_is_built_once_from_its_configuration(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    assert operator.pool_for(None) is None
    off = SimpleNamespace(config=SimpleNamespace())
    assert operator.pool_for(off) is None
    alone = SimpleNamespace(config=SimpleNamespace(operator_subscription_dir=directory, operator_subscription_orgs=()))
    only_log = operator.pool_for(alone)  # ADR-078: the directory alone is a pool; the organizations come from the log
    assert only_log is not None
    assert only_log.orgs == frozenset()
    assert not only_log.allows(ORG)  # no identity: no verified platform log
    log = io.StringIO()
    context = SimpleNamespace(
        config=SimpleNamespace(operator_subscription_dir=directory, operator_subscription_orgs=frozenset({ORG})),
        log=log,
    )
    found = operator.pool_for(context)
    assert found is not None
    assert operator.pool_for(context) is found
    assert (found.directory, found.orgs) == (directory, frozenset({ORG}))
    assert found.allows(ORG)
    assert not found.allows("another")
    assert not found.allows(None)
    found.log("runner.model x")
    assert log.getvalue() == "runner.model x\n"


def test_the_verified_platform_log_decides_before_the_environment(tmp_path: Path) -> None:
    """ADR-078 section 3: the latest `org_operator_subscriptions` of an organization in the verified platform log
    decides, also a revocation of a listed organization; without an entry the environment list applies."""
    pool = make(pool_dir(tmp_path))
    listed, granted, revoked = ORG, str(uuid.uuid4()), str(uuid.uuid4())
    assert pool.allows(listed)  # no verified log yet: the environment list
    state = SimpleNamespace(operator_subscriptions={granted: True, revoked: False, listed: False})
    state.operator_subscribed = lambda org: state.operator_subscriptions.get(org, False)
    pool.platform = lambda: cast(Any, state)
    assert pool.allows(granted)
    assert not pool.allows(revoked)
    assert not pool.allows(listed)  # the log's revocation wins over the environment
    state.operator_subscriptions = {granted: True}
    assert pool.allows(listed)  # without an entry the environment list applies again
    assert not pool.allows(str(uuid.uuid4()))


def test_the_platform_state_comes_from_the_daemons_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert operator.platform_of(SimpleNamespace())() is None
    state = cast(Any, object())
    monkeypatch.setattr(operator, "book_for", lambda context: SimpleNamespace(platform_state=state))
    assert operator.platform_of(SimpleNamespace(identity=object()))() is state


def test_no_representation_shows_a_token(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token())
    pool = make(directory)
    pool.scan()
    shown = repr(pool) + repr(pool.files) + repr(pool.summary())
    assert MARKER not in shown
    assert "<redacted>" in repr(pool.files["a"])


def test_refusals_right_after_a_delivery_and_repeated_ones_of_one_session_are_not_held_against_the_pool(
    tmp_path: Path,
) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token(1))
    put(directory, "b", token(2))
    lines: list[str] = []
    clock = Clock()
    pool = make(directory, lines, clock)
    pool.scan()
    held_a, held_b = pool.current("a"), pool.current("b")
    assert held_a is not None
    assert held_b is not None
    pool.delivered_at["s1"] = clock.now
    clock.now += operator.GRACE_S - 1
    pool.observe("a", 401, held_a[1], "s1")  # may be the record of a request sent with the token before
    assert pool.state("a") == USABLE
    clock.now += 1
    pool.observe("a", 401, held_a[1], "s1")
    assert pool.state("a") == BLOCKED
    # the same session refused again on its next file: an untrusted guest cannot take the pool file by file
    clock.now += operator.GRACE_S
    pool.observe("b", 401, held_b[1], "s1")
    pool.observe("b", 429, None, "s1")
    assert pool.state("b") == USABLE
    ignored = "runner.model operator_refusal_ignored session_id=s1 file=b"
    assert lines.count(ignored) == 1
    pool.observe("b", 401, held_b[1], "s2")  # another session's refusal counts
    assert pool.state("b") == BLOCKED
    clock.now += operator.LIMIT_S
    put(directory, "b", token(3))
    pool.scan()
    renewed = pool.current("b")
    assert renewed is not None
    pool.observe("b", 401, renewed[1], "s1")
    assert pool.state("b") == BLOCKED
