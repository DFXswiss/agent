"""Unit tests for the all-open sweep fingerprint and its state file.

The fingerprint must change for every input the guard reads before it would
reconcile a pull request, and every unreadable input must fail open to a full
reconciliation. The state file is untrusted input: only a bounded, validated
shape is used.
"""

from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli import sweep_state  # noqa: E402
from agent_cli.a38_guard import RateLimitError  # noqa: E402
from test_a38_guard import (  # noqa: E402
    AUTHOR_ID,
    BOT_ID,
    GUARD_MARKER,
    HEAD,
    OUTSIDER_ID,
    REPO,
    FakeAPI,
    _api_path,
)

RUNTIME = "e" * 40
OTHER_RUNTIME = "f" * 40
_KEY_RE = r"^[0-9a-f]{64}$"


def _ctx(runtime: str = RUNTIME) -> sweep_state.SweepContext:
    return sweep_state.SweepContext(runtime_env={"A38_RUNTIME_REVISION": runtime})


def _fake() -> FakeAPI:
    fake = FakeAPI()
    fake.pull_list_items = [fake.pull]
    fake.permissions["author"] = {
        "permission": "write",
        "role_name": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.permissions["outsider"] = {
        "permission": "read",
        "role_name": "read",
        "user": {"id": OUTSIDER_ID, "login": "outsider", "type": "User"},
    }
    return fake


def _key(
    fake: FakeAPI,
    item: object | None = None,
    ctx: sweep_state.SweepContext | None = None,
) -> str | None:
    return sweep_state.compute_sweep_key(
        fake.api(),
        REPO,
        fake.pull if item is None else item,
        ctx if ctx is not None else _ctx(),
    )


class ComputeSweepKeyTests(unittest.TestCase):
    def assertChanges(self, fake: FakeAPI, mutate: Callable[[], None], **kwargs: Any) -> None:
        before = _key(fake, **kwargs)
        self.assertRegex(before, _KEY_RE)
        mutate()
        after = _key(fake, **kwargs)
        self.assertNotEqual(before, after)

    def test_base_key_is_stable_and_well_formed(self) -> None:
        fake = _fake()
        first = _key(fake)
        second = _key(fake)
        self.assertRegex(first, _KEY_RE)
        self.assertEqual(first, second)

    def test_list_item_fields_change_the_key(self) -> None:
        fake = _fake()
        item = copy.deepcopy(fake.pull)
        mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("title", lambda i: i.__setitem__("title", "other title")),
            ("body", lambda i: i.__setitem__("body", "other body")),
            ("labels", lambda i: i.__setitem__("labels", [{"name": "bug"}])),
            ("draft", lambda i: i.__setitem__("draft", True)),
            ("head ref", lambda i: i["head"].__setitem__("ref", "other")),
            (
                "head repo",
                lambda i: i["head"]["repo"].__setitem__("full_name", "example/fork"),
            ),
            ("base ref", lambda i: i["base"].__setitem__("ref", "main")),
            ("base private", lambda i: i["base"]["repo"].__setitem__("private", True)),
            (
                "base default branch",
                lambda i: i["base"]["repo"].__setitem__("default_branch", "main"),
            ),
            ("user id", lambda i: i["user"].__setitem__("id", AUTHOR_ID + 1)),
            ("user login", lambda i: i["user"].__setitem__("login", "other")),
            ("user type", lambda i: i["user"].__setitem__("type", "Bot")),
            (
                "merge commit",
                lambda i: i.__setitem__("merge_commit_sha", "a" * 40),
            ),
        ]
        for name, mutate in mutations:
            with self.subTest(field=name):
                item = copy.deepcopy(fake.pull)
                self.assertChanges(
                    fake, lambda item=item, mutate=mutate: mutate(item), item=item
                )

    def test_head_or_base_sha_mismatch_returns_none(self) -> None:
        fake = _fake()
        item = copy.deepcopy(fake.pull)
        item["head"]["sha"] = "f" * 40
        self.assertIsNone(_key(fake, item=item))
        item = copy.deepcopy(fake.pull)
        item["base"]["sha"] = "f" * 40
        self.assertIsNone(_key(fake, item=item))

    def test_incomplete_list_item_returns_none(self) -> None:
        fake = _fake()
        for field in ("labels", "draft", "user", "base"):
            with self.subTest(field=field):
                item = copy.deepcopy(fake.pull)
                item.pop(field)
                self.assertIsNone(_key(fake, item=item))

    def test_absent_required_list_item_field_returns_none(self) -> None:
        fake = _fake()
        for field in ("title", "body", "merge_commit_sha"):
            with self.subTest(field=field):
                item = copy.deepcopy(fake.pull)
                item.pop(field)
                self.assertIsNone(_key(fake, item=item))

    def test_explicit_null_body_is_a_readable_input(self) -> None:
        fake = _fake()
        item = copy.deepcopy(fake.pull)
        item["body"] = None
        self.assertRegex(_key(fake, item=item), _KEY_RE)

    def test_branch_tip_change_changes_the_key(self) -> None:
        fake = _fake()
        self.assertChanges(
            fake,
            lambda: fake.ref_commits.__setitem__("develop", "f" * 40),
        )

    def test_runtime_revision_change_changes_the_key(self) -> None:
        fake = _fake()
        before = _key(fake)
        self.assertNotEqual(before, _key(fake, ctx=_ctx(OTHER_RUNTIME)))

    def test_full_pull_fields_change_the_key(self) -> None:
        fake = _fake()
        mutations: list[tuple[str, Callable[[], None]]] = [
            ("mergeable", lambda: fake.pull.__setitem__("mergeable", False)),
            (
                "merge commit",
                lambda: fake.pull.__setitem__("merge_commit_sha", "a" * 40),
            ),
            ("draft", lambda: fake.pull.__setitem__("draft", True)),
        ]
        for name, mutate in mutations:
            with self.subTest(field=name):
                fake = _fake()
                self.assertChanges(fake, mutate)

    def test_null_mergeable_returns_none(self) -> None:
        fake = _fake()
        fake.pull["mergeable"] = None
        self.assertIsNone(_key(fake))

    def test_comment_add_delete_and_edit_change_the_key(self) -> None:
        fake = _fake()
        added = {
            "id": 5001,
            "user": {"id": OUTSIDER_ID, "login": "outsider", "type": "User"},
            "updated_at": "2024-01-01T00:00:00Z",
            "body": "a plain comment",
        }
        self.assertChanges(fake, lambda: fake.comments.append(dict(added)))
        fake.comments.remove(added)

        guard_comment = {
            "id": 5002,
            "user": {"id": BOT_ID, "login": "github-actions[bot]", "type": "Bot"},
            "updated_at": "2024-01-01T00:00:00Z",
            "body": f"{GUARD_MARKER}\nA38 report",
        }
        fake.comments.append(dict(guard_comment))
        before = _key(fake)
        self.assertRegex(before, _KEY_RE)
        # An edit through the API also moves updated_at.
        fake.comments[-1]["body"] = f"{GUARD_MARKER}\nA38 report, edited"
        fake.comments[-1]["updated_at"] = "2024-01-02T00:00:00Z"
        self.assertNotEqual(before, _key(fake))

    def test_bot_comment_does_not_add_a_permission_role(self) -> None:
        fake = _fake()
        item = sweep_state._list_item_input(fake.pull)
        self.assertIsNotNone(item)
        before = sweep_state._roles_input(
            fake.api(), REPO, fake.pull["number"], item, [], [], _ctx()
        )
        fake.comments.append({
            "id": 5003,
            "user": {"id": BOT_ID, "login": "github-actions[bot]", "type": "Bot"},
            "updated_at": "2024-01-01T00:00:00Z",
            "body": f"{GUARD_MARKER}\nA38 report",
        })
        after = sweep_state._roles_input(
            fake.api(),
            REPO,
            fake.pull["number"],
            item,
            fake.comments,
            [],
            _ctx(),
        )
        self.assertIsNotNone(before)
        self.assertEqual(after, before)

    def test_deleted_comment_changes_the_key(self) -> None:
        fake = _fake()
        fake.comments.append({
            "id": 6001,
            "user": {"id": OUTSIDER_ID, "login": "outsider", "type": "User"},
            "updated_at": "2024-01-01T00:00:00Z",
            "body": "keep",
        })
        before = _key(fake)
        fake.comments.clear()
        self.assertNotEqual(before, _key(fake))

    def test_review_change_changes_the_key(self) -> None:
        fake = _fake()
        self.assertChanges(
            fake,
            lambda: fake.reviews.append({
                "id": 7001,
                "user": {
                    "id": OUTSIDER_ID,
                    "login": "outsider",
                    "type": "User",
                },
                "state": "APPROVED",
                "submitted_at": "2024-01-01T00:00:00Z",
                "commit_id": HEAD,
            }),
        )

    def test_absent_review_submitted_at_returns_none(self) -> None:
        fake = _fake()
        fake.reviews.append({
            "id": 7001,
            "user": {
                "id": OUTSIDER_ID,
                "login": "outsider",
                "type": "User",
            },
            "state": "APPROVED",
            "commit_id": HEAD,
        })
        self.assertIsNone(_key(fake))

    def test_comment_author_without_type_returns_none(self) -> None:
        fake = _fake()
        fake.comments.append({
            "id": 5001,
            "user": {"id": OUTSIDER_ID, "login": "outsider"},
            "updated_at": "2024-01-01T00:00:00Z",
            "body": "a plain comment",
        })
        self.assertIsNone(_key(fake))

    def test_review_thread_change_changes_the_key(self) -> None:
        fake = _fake()
        self.assertChanges(
            fake,
            lambda: fake.review_threads.append({
                "id": "thread-1",
                "isResolved": False,
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"body": "please fix"}],
                },
            }),
        )

    def test_malformed_review_thread_returns_none(self) -> None:
        fake = _fake()
        fake.review_threads.append({
            "id": "thread-1",
            "isResolved": "no",
            "comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"body": "please fix"}],
            },
        })
        self.assertIsNone(_key(fake))

    def test_ci_inputs_change_the_key(self) -> None:
        fake = _fake()
        self.assertChanges(
            fake,
            lambda: fake.check_runs.append({
                "id": 8001,
                "name": "pytest",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 42},
            }),
        )
        fake = _fake()
        self.assertChanges(
            fake,
            lambda: fake.statuses.append({
                "sha": HEAD,
                "context": "build",
                "state": "success",
                "creator": {"id": OUTSIDER_ID},
            }),
        )

    def test_check_run_without_conclusion_returns_none(self) -> None:
        fake = _fake()
        fake.check_runs.append({
            "id": 8001,
            "name": "pytest",
            "status": "completed",
            "app": {"id": 42},
        })
        self.assertIsNone(_key(fake))

    def test_check_run_with_null_conclusion_is_a_key(self) -> None:
        fake = _fake()
        fake.check_runs.append({
            "id": 8001,
            "name": "pytest",
            "status": "completed",
            "conclusion": None,
            "app": {"id": 42},
        })
        self.assertRegex(_key(fake), _KEY_RE)

    def test_check_run_without_app_returns_none(self) -> None:
        fake = _fake()
        fake.check_runs.append({
            "id": 8001,
            "name": "pytest",
            "status": "completed",
            "conclusion": "success",
        })
        self.assertIsNone(_key(fake))

    def test_workflow_run_without_conclusion_returns_none(self) -> None:
        fake = _fake()
        with mock.patch.object(
            sweep_state,
            "_list_runs",
            return_value=[{
                "id": 9001,
                "status": "completed",
                "run_attempt": 1,
            }],
        ):
            self.assertIsNone(_key(fake))

    def test_status_without_creator_returns_none(self) -> None:
        fake = _fake()
        fake.statuses.append({
            "sha": HEAD,
            "context": "build",
            "state": "success",
        })
        self.assertIsNone(_key(fake))

    def test_workflow_runs_change_the_key(self) -> None:
        fake = _fake()
        before = _key(fake)
        with mock.patch.object(
            sweep_state,
            "_list_runs",
            return_value=[
                {
                    "id": 9001,
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 1,
                }
            ],
        ):
            after = _key(fake)
        self.assertRegex(before, _KEY_RE)
        self.assertNotEqual(before, after)

    def test_truncated_workflow_run_inventory_returns_none(self) -> None:
        fake = _fake()
        original = fake.request_fn
        calls = 0

        def one_run_of_two(
            method: str, url: str, body: bytes | None = None
        ) -> tuple[int, Any, dict[str, str]]:
            nonlocal calls
            if (
                method.upper() == "GET"
                and _api_path(url) == f"/repos/{REPO}/actions/runs"
            ):
                calls += 1
                return 200, {
                    "total_count": 2,
                    "workflow_runs": [{
                        "id": 9001,
                        "status": "completed",
                        "conclusion": "success",
                        "run_attempt": 1,
                    }],
                }, {}
            return original(method, url, body)

        with mock.patch.object(fake, "request_fn", one_run_of_two):
            self.assertIsNone(_key(fake))
        self.assertGreater(calls, 0)

    def test_complete_workflow_run_inventory_is_a_key(self) -> None:
        fake = _fake()
        original = fake.request_fn
        calls = 0

        def one_run_of_one(
            method: str, url: str, body: bytes | None = None
        ) -> tuple[int, Any, dict[str, str]]:
            nonlocal calls
            if (
                method.upper() == "GET"
                and _api_path(url) == f"/repos/{REPO}/actions/runs"
            ):
                calls += 1
                return 200, {
                    "total_count": 1,
                    "workflow_runs": [{
                        "id": 9001,
                        "status": "completed",
                        "conclusion": "success",
                        "run_attempt": 1,
                    }],
                }, {}
            return original(method, url, body)

        with mock.patch.object(fake, "request_fn", one_run_of_one):
            self.assertRegex(_key(fake), _KEY_RE)
        self.assertGreater(calls, 0)

    def test_role_change_changes_the_key(self) -> None:
        fake = _fake()
        self.assertChanges(
            fake,
            lambda: fake.permissions["author"].__setitem__("role_name", "admin"),
        )

    def test_role_name_change_with_the_same_permission_changes_the_key(self) -> None:
        fake = _fake()
        before = _key(fake)
        fake.permissions["author"]["role_name"] = "deployer"
        after = _key(fake)
        self.assertRegex(before, _KEY_RE)
        self.assertNotEqual(before, after)

    def test_unreadable_role_returns_none(self) -> None:
        fake = _fake()
        fake.denied_prefixes.append(f"/repos/{REPO}/collaborators/")
        self.assertIsNone(_key(fake))

    def test_non_object_comment_returns_none(self) -> None:
        fake = _fake()
        fake.comments.append("not an object")  # type: ignore[arg-type]
        self.assertIsNone(_key(fake))

    def test_ready_event_without_actor_login_returns_none(self) -> None:
        fake = _fake()
        fake.timeline.append({
            "event": "ready_for_review",
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "actor": {"id": OUTSIDER_ID, "type": "User"},
        })
        self.assertIsNone(_key(fake))

    def test_ready_event_without_actor_type_returns_none(self) -> None:
        fake = _fake()
        fake.timeline.append({
            "event": "ready_for_review",
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "actor": {"id": OUTSIDER_ID, "login": "outsider"},
        })
        self.assertIsNone(_key(fake))

    def test_ready_event_without_kind_returns_none(self) -> None:
        fake = _fake()
        fake.timeline.append({
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "actor": {"id": OUTSIDER_ID, "login": "outsider", "type": "User"},
        })
        self.assertIsNone(_key(fake))

    def test_rate_limit_during_a_read_propagates(self) -> None:
        fake = _fake()
        with mock.patch.object(
            sweep_state,
            "collect_comments",
            side_effect=RateLimitError("rate limited", retry_at=None),
        ):
            with self.assertRaises(RateLimitError):
                _key(fake)


class SweepStateFileTests(unittest.TestCase):
    def _write(self, path: str, payload: object) -> None:
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def _load(
        self, path: str, repo: str = REPO
    ) -> tuple[dict[int, sweep_state.StateEntry], str]:
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            entries = sweep_state.load_sweep_state(path, repo)
        return entries, err.getvalue()

    def _entry(self, key: str = "a" * 64, evaluated_at: int = 1, exit_code: int = 0):
        return sweep_state.StateEntry(
            key=key, evaluated_at=evaluated_at, exit_code=exit_code
        )

    def test_round_trip_write_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sweep_state.save_sweep_state(
                path, REPO, [1, 2], {1: self._entry(), 2: self._entry(key="b" * 64)},
                now=100,
            )
            entries, err = self._load(path)
            self.assertEqual(err, "")
            self.assertEqual(set(entries), {1, 2})
            self.assertEqual(entries[1].key, "a" * 64)
            self.assertEqual(entries[2].key, "b" * 64)
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["repository"], REPO)
            self.assertEqual(
                set(payload["entries"]), {"1", "2"}
            )

    def test_save_keeps_only_still_open_pulls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sweep_state.save_sweep_state(
                path,
                REPO,
                [2],
                {1: self._entry(), 2: self._entry(key="b" * 64)},
                now=100,
            )
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(set(payload["entries"]), {"2"})

    def test_save_drops_expired_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sweep_state.save_sweep_state(
                path,
                REPO,
                [1, 2],
                {
                    1: self._entry(evaluated_at=100 - sweep_state.ENTRY_TTL_S),
                    2: self._entry(key="b" * 64, evaluated_at=99),
                },
                now=100,
            )
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(set(payload["entries"]), {"2"})

    def test_save_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sweep_state.save_sweep_state(
                path, REPO, [1], {1: self._entry()}, now=100
            )
            self.assertEqual(
                [name for name in os.listdir(tmp) if name != "state.json"], []
            )

    def test_save_failure_logs_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            err = io.StringIO()
            with mock.patch(
                "agent_cli.sweep_state.os.replace",
                side_effect=OSError("disk full"),
            ), mock.patch("sys.stderr", err):
                sweep_state.save_sweep_state(
                    path, REPO, [1], {1: self._entry()}, now=100
                )
            self.assertIn("sweep state not saved", err.getvalue())
            self.assertFalse(os.path.exists(path))

    def test_entry_usability_is_bounded_to_24_hours(self) -> None:
        self.assertTrue(sweep_state.entry_usable(self._entry(evaluated_at=100), 100))
        self.assertTrue(
            sweep_state.entry_usable(
                self._entry(evaluated_at=100 - sweep_state.ENTRY_TTL_S + 1), 100
            )
        )
        self.assertFalse(
            sweep_state.entry_usable(
                self._entry(evaluated_at=100 - sweep_state.ENTRY_TTL_S), 100
            )
        )
        self.assertFalse(sweep_state.entry_usable(self._entry(evaluated_at=101), 100))

    def test_iso_utc(self) -> None:
        self.assertEqual(sweep_state.iso_utc(0), "1970-01-01T00:00:00Z")

    def test_missing_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entries, err = self._load(os.path.join(tmp, "absent.json"))
        self.assertEqual(entries, {})
        self.assertIn("a38-guard: sweep state ignored: missing file", err)

    def test_oversized_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            Path(path).write_bytes(b" " * (sweep_state.MAX_STATE_BYTES + 1))
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: file too large", err)

    def test_file_that_grows_after_the_size_check_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            Path(path).write_bytes(b" " * (sweep_state.MAX_STATE_BYTES + 1))
            # The bounded read, not the earlier size report, must reject it.
            with mock.patch.object(
                sweep_state.os,
                "fstat",
                return_value=mock.Mock(st_mode=stat.S_IFREG, st_size=0),
            ):
                entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: file too large", err)

    def test_non_regular_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entries, err = self._load(tmp)
        self.assertEqual(entries, {})
        self.assertIn("a38-guard: sweep state ignored:", err)
        if os.name != "nt":
            self.assertIn("not a regular file", err)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "platform cannot create a FIFO")
    def test_writerless_fifo_does_not_block_and_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            os.mkfifo(path)
            entries: dict[int, sweep_state.StateEntry] = {}
            err = io.StringIO()

            def load() -> None:
                with mock.patch("sys.stderr", err):
                    entries.update(sweep_state.load_sweep_state(path, REPO))

            thread = threading.Thread(target=load, daemon=True)
            thread.start()
            thread.join(timeout=5)
            finished_in_time = not thread.is_alive()
            if not finished_in_time:
                # Release a blocking open so the daemon thread can exit.
                try:
                    writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:
                    pass
                else:
                    os.close(writer)
                thread.join(timeout=5)
            self.assertTrue(finished_in_time)
            self.assertEqual(entries, {})
            self.assertIn("not a regular file", err.getvalue())

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW"), "platform cannot refuse a final symlink"
    )
    def test_symlink_to_a_valid_state_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "state.json")
            sweep_state.save_sweep_state(target, REPO, [1], {1: self._entry()}, now=100)
            link = os.path.join(tmp, "link.json")
            os.symlink(target, link)
            entries, err = self._load(link)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: unreadable file", err)

    def test_invalid_json_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            Path(path).write_text("{not json", encoding="utf-8")
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: invalid JSON", err)

    def test_wrong_version_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self._write(path, {"version": 2, "repository": REPO, "entries": {}})
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: unsupported version", err)

    def test_other_repository_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self._write(
                path, {"version": 1, "repository": "other/repo", "entries": {}}
            )
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: repository mismatch", err)

    def test_repository_comparison_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self._write(
                path,
                {
                    "version": 1,
                    "repository": REPO.upper(),
                    "entries": {
                        "1": {
                            "key": "a" * 64,
                            "evaluated_at": 10,
                            "exit_code": 1,
                        }
                    },
                },
            )
            entries, err = self._load(path)
        self.assertEqual(err, "")
        self.assertEqual(entries[1].exit_code, 1)

    def test_malformed_entry_discards_every_entry(self) -> None:
        valid = {"key": "a" * 64, "evaluated_at": 10, "exit_code": 0}
        invalid_entries = {
            "short key": {"key": "a" * 63, "evaluated_at": 10, "exit_code": 0},
            "uppercase key": {
                "key": "A" * 64,
                "evaluated_at": 10,
                "exit_code": 0,
            },
            "bool evaluated_at": {
                "key": "a" * 64,
                "evaluated_at": True,
                "exit_code": 0,
            },
            "bool exit_code": {
                "key": "a" * 64,
                "evaluated_at": 10,
                "exit_code": False,
            },
            "exit_code out of range": {
                "key": "a" * 64,
                "evaluated_at": 10,
                "exit_code": 2,
            },
            "not an object": "value",
        }
        for name, broken in invalid_entries.items():
            with self.subTest(entry=name):
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "state.json")
                    self._write(
                        path,
                        {
                            "version": 1,
                            "repository": REPO,
                            "entries": {"1": valid, "2": broken},
                        },
                    )
                    entries, err = self._load(path)
                self.assertEqual(entries, {})
                self.assertIn("sweep state ignored: malformed entry", err)

    def test_non_numeric_pull_number_discards_every_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self._write(
                path,
                {
                    "version": 1,
                    "repository": REPO,
                    "entries": {
                        "abc": {"key": "a" * 64, "evaluated_at": 10, "exit_code": 0}
                    },
                },
            )
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: malformed entry", err)

    def test_over_long_numeric_pull_number_is_ignored(self) -> None:
        # Longer than Python's integer-string conversion limit and than any
        # possible pull request number; it must not abort the sweep.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self._write(
                path,
                {
                    "version": 1,
                    "repository": REPO,
                    "entries": {
                        "9" * 5000: {
                            "key": "a" * 64,
                            "evaluated_at": 10,
                            "exit_code": 0,
                        }
                    },
                },
            )
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: malformed entry", err)

    def test_deeply_nested_json_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            depth = 100_000
            Path(path).write_bytes(b"[" * depth + b"]" * depth)
            self.assertLess(
                Path(path).stat().st_size, sweep_state.MAX_STATE_BYTES
            )
            entries, err = self._load(path)
        self.assertEqual(entries, {})
        self.assertIn("sweep state ignored: invalid JSON", err)

    def test_the_state_file_holds_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sweep_state.save_sweep_state(
                path, REPO, [1], {1: self._entry(key="c" * 64)}, now=100
            )
            text = Path(path).read_text(encoding="utf-8")
        self.assertEqual(
            set(json.loads(text)),
            {"version", "repository", "entries"},
        )
        self.assertEqual(
            set(json.loads(text)["entries"]["1"]),
            {"key", "evaluated_at", "exit_code"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
