"""The class check of `threadcheck.py` (HOT-15, E-19): the test that leaves a thread running fails, no other test."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from tests.platform import threadcheck

SPEC = textwrap.dedent(
    """
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor

    STOP = threading.Event()
    POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shared")


    def test_leaves_a_thread() -> None:
        threading.Thread(target=STOP.wait, name="leaked-watcher", daemon=True).start()


    def test_after_the_leak_prints_nothing() -> None:
        assert True


    def test_a_thread_that_ends_in_the_grace_time() -> None:
        threading.Thread(target=STOP.wait, args=(0.2,), name="short", daemon=True).start()


    def test_a_joined_thread() -> None:
        thread = threading.Thread(target=lambda: None, name="joined")
        thread.start()
        thread.join()


    def test_the_worker_of_a_process_wide_executor() -> None:
        assert POOL.submit(lambda: 1).result() == 1
    """
)


def test_only_the_test_that_leaves_a_thread_running_fails(tmp_path: Path, repo_root: Path) -> None:
    (tmp_path / "test_spec.py").write_text(SPEC)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "COV_", "COVERAGE_"))}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.platform.threadcheck", "-p", "no:cacheprovider", "-q", "-rE",
         "--rootdir", str(tmp_path), str(tmp_path / "test_spec.py")],
        cwd=repo_root, env={**env, "PYTHONPATH": str(repo_root)}, capture_output=True, text=True, timeout=120,
        check=False,
    )  # fmt: skip
    assert "5 passed, 1 error" in result.stdout, result.stdout + result.stderr
    errors = [line for line in result.stdout.splitlines() if line.startswith("ERROR ")]
    assert len(errors) == 1, result.stdout
    assert "test_spec.py::test_leaves_a_thread" in errors[0]
    assert "'leaked-watcher' (threading.Event.wait)" in result.stdout


def test_the_workers_of_module_executors_are_process_wide() -> None:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hot15-module")
    try:
        pool.submit(lambda: None).result()
        (worker,) = pool._threads
        assert threadcheck.process_wide_workers([SimpleNamespace(pool=pool), object()]) == {worker}
        assert worker in threadcheck.process_wide_workers([SimpleNamespace(pool=pool)])
        before = frozenset(threading.enumerate()) - {worker}
        assert threadcheck.surviving(before, grace_s=0.01) == [worker]
    finally:
        pool.shutdown()


def test_describe_names_the_thread_and_its_target() -> None:
    thread = threading.Thread(target=test_describe_names_the_thread_and_its_target, name="named")
    assert threadcheck.describe(thread) == f"'named' ({__name__}.test_describe_names_the_thread_and_its_target)"

    class Custom(threading.Thread):
        pass

    assert threadcheck.describe(Custom(name="custom")) == (
        f"'custom' ({__name__}.test_describe_names_the_thread_and_its_target.<locals>.Custom)"
    )
