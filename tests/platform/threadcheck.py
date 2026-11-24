"""Class check of HOT-15 (rule E-19): a thread a test starts has ended when the test's teardown is over.

A thread that outlives its test writes into the next tests of the same worker: to their captured stderr, to a journal
or a database that is already closed, to a fake that a later test configured. That later test then fails, and it is
not the one at fault. This plugin compares the threads after the teardown of every test (all of its function
fixtures are finalized by then) with the threads before its setup. Each new thread gets `GRACE_S` in total to end.
If one is still alive, the teardown of the test that started it fails, naming the thread and its target.

Exempt are the workers of the process-wide executors: a `ThreadPoolExecutor` held by a module attribute (for example
the signal workers of the hub, `thread_name_prefix="scheduler"`). It starts its single worker on first use and keeps
it for the life of the process by design, whichever test used it first.

Registered by `tests/platform/conftest.py`; `tests/platform/test_threadcheck.py` checks it.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor

import pytest

GRACE_S = 2.0
BEFORE = pytest.StashKey[frozenset[threading.Thread]]()


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
    deadline = time.monotonic() + grace_s
    for thread in started:
        thread.join(max(0.0, deadline - time.monotonic()))
    alive = [thread for thread in started if thread.is_alive()]
    if not alive:
        return []
    exempt = process_wide_workers()
    return [thread for thread in alive if thread not in exempt]


def describe(thread: threading.Thread) -> str:
    target = getattr(thread, "_target", None)
    where = getattr(target, "__qualname__", None) or type(thread).__qualname__
    module = getattr(target, "__module__", None) or type(thread).__module__
    return f"{thread.name!r} ({module}.{where})"


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
