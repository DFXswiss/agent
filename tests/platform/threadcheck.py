"""Class check of HOT-15 (rule E-19): a thread a test starts has ended when the test's teardown is over.

A thread that outlives its test writes into the next tests of the same worker: to their captured stderr, to a journal
or a database that is already closed, to a fake that a later test configured. That later test then fails, and it is
not the one at fault. This plugin compares the threads after the teardown of every test (all of its function
fixtures are finalized by then) with the threads before its setup. The new threads get `GRACE_S` in total to end
(a thread told to stop but not joined still ends on a loaded device; a test without such threads does not wait). If
one is still alive, the teardown of the test that started it fails, naming the thread and its target.

Exempt are the workers of the process-wide executors: a `ThreadPoolExecutor` held by a module attribute (for example
the signal workers of the hub, `thread_name_prefix="scheduler"`). It starts its single worker on first use and keeps
it for the life of the process by design, whichever test used it first.

Not the test's either are the threads of fixtures wider than a function (module, package, session): the threads
started while such a fixture sets up belong to it (`pytest_fixture_setup`), whichever test set it up first. A shared
thread that a test restarts for such a fixture (the stand-in listener of the Linux suites) is handed over with
`share`.

Limits: work still queued on a process-wide executor after a test is not seen here (the conftests that use those
signals drain them, HOT-10); the threads of wider fixtures are not checked at their own teardown.

The hooks are imported by `tests/platform/conftest.py` (not `pytest_plugins`: the legacy run loads that conftest
late); `tests/platform/test_threadcheck.py` checks them.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor

import pytest

GRACE_S = 10.0
BEFORE = pytest.StashKey[frozenset[threading.Thread]]()
# Threads owned by fixtures wider than a function, or handed over to them (`share`).
SHARED: set[threading.Thread] = set()


def share(thread: threading.Thread) -> None:
    """Hand `thread` to the module or session it serves: no single test owns it."""
    SHARED.add(thread)


def process_wide_workers(modules: Iterable[object] | None = None) -> set[threading.Thread]:
    """The worker threads of the `ThreadPoolExecutor`s that module attributes hold."""
    workers: set[threading.Thread] = set()
    for module in list(sys.modules.values()) if modules is None else modules:
        for value in list(getattr(module, "__dict__", {}).values()):
            if isinstance(value, ThreadPoolExecutor):
                workers.update(value._threads)
    return workers


def surviving(before: frozenset[threading.Thread], grace_s: float = GRACE_S) -> list[threading.Thread]:
    """The threads started since `before` that are still alive after waiting up to `grace_s` for all of them."""
    started = [thread for thread in threading.enumerate() if thread not in before]
    if not started:
        return []
    exempt = process_wide_workers() | SHARED
    started = [thread for thread in started if thread not in exempt]
    deadline = time.monotonic() + grace_s
    for thread in started:
        thread.join(max(0.0, deadline - time.monotonic()))
    return [thread for thread in started if thread.is_alive()]


def describe(thread: threading.Thread) -> str:
    target = getattr(thread, "_target", None)
    where = getattr(target, "__qualname__", None) or type(thread).__qualname__
    module = getattr(target, "__module__", None) or type(thread).__module__
    return f"{thread.name!r} ({module}.{where})"


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[object], request: pytest.FixtureRequest
) -> Generator[None, object, object]:
    """The threads a fixture wider than a function starts while it sets up belong to that fixture."""
    if fixturedef.scope == "function":
        return (yield)
    before = frozenset(threading.enumerate())
    try:
        return (yield)
    finally:
        SHARED.update(thread for thread in threading.enumerate() if thread not in before)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    item.stash[BEFORE] = frozenset(threading.enumerate())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, None, None]:
    yield  # the fixtures of the test are finalized
    before = item.stash.get(BEFORE, None)
    if before is None:
        return
    left = surviving(before)
    if left:
        names = ", ".join(describe(thread) for thread in left)
        pytest.fail(
            f"{item.nodeid} left running threads it started: {names}. Stop and join them before the test ends "
            "(in the test or its fixture), so that no later test sees their work (HOT-15, E-19).",
            pytrace=False,
        )
