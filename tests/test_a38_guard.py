"""End-to-end tests for dfx pr guard with a fake GitHub API.

Does not execute network I/O or pull-request code. Safe YAML only.
"""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import parse_qs, urlparse
import unittest
from typing import Any
from unittest import mock

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass


from agent_cli import a38_guard  # noqa: E402
from agent_cli.a38_guard import (  # noqa: E402
    GUARD_MARKER,
    GITHUB_ACTIONS_BOT_ID,
    GitHubApi,
    GuardError,
    LOCAL_CI_BEGIN,
    LOCAL_CI_END,
    assess_pull,
    event_should_ignore,
    looks_like_report,
    main,
    pick_latest_author_report,
    publish_assessment,
    reconcile_event,
    reconcile_pull,
    status_context_enforce,
    status_context_observe,
)


HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
BASE2 = "cccccccccccccccccccccccccccccccccccccccc"
REPO = "example/public-app"
AUTHOR_ID = 1001
BOT_ID = GITHUB_ACTIONS_BOT_ID
OUTSIDER_ID = 2002


def _policy(*, mode: str = "enforce", jobs: list | None = None, exclusions: list | None = None) -> dict:
    return {
        "schema": "a38/v1",
        "standard": "A38",
        "documentation": "docs/a38.md",
        "mode": mode,
        "jobs": jobs
        or [
            {
                "id": "pytest",
                "name": "Pytest",
                "command": "pytest",
                "timeout_s": 600,
                "workflow": ".github/workflows/test.yml",
                "job": "pytest",
            }
        ],
        "exclusions": exclusions or [],
    }


def _workflow_yaml(jobs: list[str]) -> bytes:
    lines = ["name: test", "on: [push]", "jobs:"]
    for job in jobs:
        lines.append(f"  {job}:")
        lines.append("    runs-on: ubuntu-latest")
        lines.append("    steps:")
        lines.append("      - run: echo ok")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _report_comment(
    *,
    head: str = HEAD,
    repo: str = REPO,
    private: bool = False,
    result: str = "pass",
    exit_code: int = 0,
) -> str:
    payload = {
        "schema": "dfx-local-ci/v1",
        "repo": repo,
        "head": head,
        "private": private,
        "recorded_at": "2026-09-05T12:00:00Z",
        "required": ["pytest"],
        "runs": [
            {
                "id": "pytest",
                "name": "Pytest",
                "command": "pytest",
                "result": result,
                "exit_code": exit_code,
                "duration_s": 1.0,
                "timeout_s": 600,
            }
        ],
    }
    return (
        "EN: ready\n"
        f"{LOCAL_CI_BEGIN}\n```json\n{json.dumps(payload)}\n```\n{LOCAL_CI_END}\n"
    )


def _b64(data: bytes) -> dict:
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
    }


class FakeAPI:
    """In-memory GitHub API for guard tests."""

    def __init__(self) -> None:
        self.pull: dict[str, Any] = self._pull(HEAD, BASE, state="open")
        self.comments: list[dict[str, Any]] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.tree_paths: dict[str, list[str]] = {
            HEAD: [".github/workflows/test.yml"],
            BASE: [".github/workflows/test.yml"],
        }
        self.statuses: list[dict[str, Any]] = []
        self.writes: list[str] = []
        self.user: dict[str, Any] | None = {"id": BOT_ID, "login": "github-actions[bot]"}
        self.user_endpoint_denied = False
        self.denied_prefixes: list[str] = []
        self._comment_seq = 10
        self._status_seq = 50
        self.mutate_head_on_publish: str | None = None
        self.open_pulls: list[int] = [1]
        self.reviews: list[dict[str, Any]] = []
        self.permissions: dict[str, dict[str, Any]] = {}
        wf = _workflow_yaml(["pytest"])
        self.files[(BASE, ".github/a38.json")] = json.dumps(_policy()).encode()
        self.files[(BASE, ".github/workflows/test.yml")] = wf
        self.files[(HEAD, ".github/workflows/test.yml")] = wf

    def _pull(self, head: str, base: str, *, state: str = "open") -> dict[str, Any]:
        return {
            "number": 1,
            "state": state,
            "user": {"id": AUTHOR_ID, "login": "author"},
            "head": {"sha": head, "repo": {"full_name": REPO}},
            "base": {
                "sha": base,
                "ref": "develop",
                "repo": {"private": False, "full_name": REPO},
            },
        }

    def request_fn(self, method: str, url: str, body: bytes | None = None) -> tuple[int, Any, dict[str, str]]:
        method_u = method.upper()
        parsed = url
        if parsed.startswith("https://api.github.com"):
            path = parsed[len("https://api.github.com") :]
        else:
            path = parsed
        path_only = path.split("?", 1)[0]
        for prefix in self.denied_prefixes:
            if path_only.startswith(prefix) or path.startswith(prefix):
                return 403, {"message": "denied"}, {}

        if method_u == "GET" and path_only == "/user":
            if self.user_endpoint_denied or self.user is None:
                return 403, {"message": "denied"}, {}
            return 200, self.user, {}

        if method_u == "GET" and path_only == "/users/github-actions%5Bbot%5D":
            return 200, {"id": BOT_ID, "login": "github-actions[bot]"}, {}
        if method_u == "GET" and path_only == "/users/github-actions[bot]":
            return 200, {"id": BOT_ID, "login": "github-actions[bot]"}, {}

        if method_u == "GET" and path_only == f"/repos/{REPO}/pulls/1":
            return 200, self.pull, {}
        if method_u == "GET" and path_only == f"/repos/{REPO}/pulls/1/reviews":
            return 200, self.reviews, {}
        if method_u == "GET" and "/collaborators/" in path_only:
            login = path_only.split("/collaborators/")[1].split("/")[0]
            return 200, self.permissions.get(login, {"permission": "read", "user": {"id": OUTSIDER_ID}}), {}

        if method_u == "GET" and path_only == f"/repos/{REPO}/pulls":
            items = [{"number": n} for n in self.open_pulls]
            return 200, items, {}

        if method_u == "GET" and path_only.startswith(f"/repos/{REPO}/issues/1/comments"):
            # Simple single-page or multi-page via page= query
            page = int(parse_qs(urlparse(path).query).get("page", ["1"])[0])
            per_page = 100
            start = (page - 1) * per_page
            chunk = self.comments[start : start + per_page]
            headers: dict[str, str] = {}
            if start + per_page < len(self.comments):
                next_page = page + 1
                headers["link"] = (
                    f'<https://api.github.com/repos/{REPO}/issues/1/comments'
                    f"?per_page=100&page={next_page}>; rel=\"next\""
                )
            return 200, chunk, headers

        if method_u == "GET" and "/contents/" in path_only:
            # /repos/REPO/contents/PATH?ref=REF
            rel = path_only.split("/contents/", 1)[1]
            rel = rel  # already unquoted in our fake callers mostly
            from urllib.parse import unquote

            qs = parse_qs(urlparse(path).query)
            ref = (qs.get("ref") or [""])[0]
            file_path = unquote(rel)
            key = (ref, file_path)
            if key not in self.files:
                return 404, {"message": "Not Found"}, {}
            return 200, _b64(self.files[key]), {}

        if method_u == "GET" and "/git/trees/" in path_only:
            sha = path_only.rsplit("/", 1)[-1]
            paths = self.tree_paths.get(sha, [])
            tree = [{"path": p, "type": "blob"} for p in paths]
            return 200, {"tree": tree, "truncated": False}, {}

        if method_u == "GET" and "/commits/" in path_only and path_only.endswith("/statuses"):
            sha = path_only.split("/commits/")[1].split("/")[0]
            items = [s for s in self.statuses if s.get("sha") == sha]
            return 200, items, {}

        if method_u == "POST" and path_only == f"/repos/{REPO}/issues/1/comments":
            if self.mutate_head_on_publish:
                # Mid-publish mutation is applied on the *next* pulls GET via flag check in publish —
                # tests flip pull before second assess; here just record.
                pass
            payload = json.loads(body.decode()) if body else {}
            self._comment_seq += 1
            comment = {
                "id": self._comment_seq,
                "body": payload.get("body"),
                "user": {"id": BOT_ID, "login": "github-actions[bot]"},
                "updated_at": f"2026-09-05T13:00:{self._comment_seq:02d}Z",
                "created_at": f"2026-09-05T13:00:{self._comment_seq:02d}Z",
            }
            self.comments.append(comment)
            self.writes.append(f"comment:create:{comment['id']}")
            return 201, comment, {}

        if method_u == "PATCH" and "/issues/comments/" in path_only:
            cid = int(path_only.rsplit("/", 1)[-1])
            payload = json.loads(body.decode()) if body else {}
            for comment in self.comments:
                if comment["id"] == cid:
                    comment["body"] = payload.get("body")
                    comment["updated_at"] = "2026-09-05T14:00:00Z"
                    self.writes.append(f"comment:update:{cid}")
                    return 200, comment, {}
            return 404, {"message": "missing"}, {}

        if method_u == "POST" and "/statuses/" in path_only:
            sha = path_only.rsplit("/", 1)[-1]
            payload = json.loads(body.decode()) if body else {}
            self._status_seq += 1
            item = {
                "id": self._status_seq,
                "sha": sha,
                "state": payload.get("state"),
                "description": payload.get("description"),
                "context": payload.get("context"),
            }
            self.statuses.insert(0, item)
            self.writes.append(f"status:create:{item['context']}")
            return 201, item, {}

        return 404, {"message": f"unhandled {method_u} {path}"}, {}

    def api(self) -> GitHubApi:
        return GitHubApi("fake-token", request_fn=self.request_fn, sleep_fn=lambda _s: None)

    def add_author_report(self, body: str, *, updated_at: str, cid: int) -> None:
        self.comments.append(
            {
                "id": cid,
                "body": body,
                "user": {"id": AUTHOR_ID, "login": "author"},
                "updated_at": updated_at,
                "created_at": updated_at,
            }
        )


class A38GuardUnitTests(unittest.TestCase):
    def test_looks_like_report_markers(self) -> None:
        self.assertTrue(looks_like_report(f"{LOCAL_CI_BEGIN} x {LOCAL_CI_END}"))
        self.assertTrue(looks_like_report("oops DFX-LOCAL-CI broken"))
        self.assertFalse(looks_like_report("ordinary comment"))

    def test_pick_latest_author_ignores_outsiders(self) -> None:
        comments = [
            {
                "id": 1,
                "updated_at": "2026-09-05T10:00:00Z",
                "user": {"id": OUTSIDER_ID},
                "body": _report_comment(),
            },
            {
                "id": 2,
                "updated_at": "2026-09-05T11:00:00Z",
                "user": {"id": AUTHOR_ID},
                "body": _report_comment(),
            },
            {
                "id": 3,
                "updated_at": "2026-09-05T12:00:00Z",
                "user": {"id": AUTHOR_ID},
                "body": "not a report",
            },
        ]
        picked = pick_latest_author_report(comments, AUTHOR_ID)
        assert picked is not None
        self.assertEqual(picked["id"], 2)

    def test_status_contexts_include_base(self) -> None:
        self.assertEqual(status_context_enforce("develop"), "A38 / report (develop)")
        self.assertEqual(
            status_context_observe("develop"), "A38 / report (observe: develop)"
        )
        self.assertNotEqual(status_context_enforce("develop"), status_context_enforce("main"))


class A38GuardE2ETests(unittest.TestCase):
    def test_opened_no_report(self) -> None:
        fake = FakeAPI()
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertTrue(any("no author" in r for r in result.reasons))
        self.assertTrue(any(w.startswith("comment:") for w in result.writes))
        self.assertTrue(any(w.startswith("status:") for w in result.writes))
        self.assertEqual(result.state_for_status, "failure")
        self.assertIn("github.com/DFXswiss/agent/blob/", result.standard_url)
        self.assertIn("/docs/a38.md", result.standard_url)
        self.assertIn("github.com/DFXswiss/agent/blob/", result.guard_docs_url)
        self.assertEqual(
            result.policy_url,
            f"https://github.com/{REPO}/blob/{BASE}/.github/a38.json",
        )

    def test_author_valid_report(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=21)
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.state_for_status, "success")
        contexts = [s["context"] for s in fake.statuses]
        self.assertIn(status_context_enforce("develop"), contexts)

    def test_stale_sha_fails(self) -> None:
        fake = FakeAPI()
        stale = "dddddddddddddddddddddddddddddddddddddddd"
        fake.add_author_report(
            _report_comment(head=stale), updated_at="2026-09-05T12:00:00Z", cid=22
        )
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertTrue(any("head" in r for r in result.reasons))

    def test_malformed_newer_report_fails_despite_older_pass(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T10:00:00Z", cid=30
        )
        fake.add_author_report(
            f"broken {LOCAL_CI_BEGIN} not-json {LOCAL_CI_END}",
            updated_at="2026-09-05T12:00:00Z",
            cid=31,
        )
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)

    def test_comment_deletion_clears_pass(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=40
        )
        first = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(first.ok)
        fake.comments = [c for c in fake.comments if c.get("user", {}).get("id") == BOT_ID]
        second = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertFalse(second.ok)

    def test_edited_fail_replacing_pass(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=41
        )
        first = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(first.ok)
        # Author edits the same comment to failing runs.
        for c in fake.comments:
            if c.get("id") == 41:
                c["body"] = _report_comment(result="fail", exit_code=1)
                c["updated_at"] = "2026-09-05T13:00:00Z"
        second = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertFalse(second.ok)
        self.assertEqual(second.state_for_status, "failure")

    def test_outsider_spoof_ignored(self) -> None:
        fake = FakeAPI()
        fake.comments.append(
            {
                "id": 50,
                "body": _report_comment(),
                "user": {"id": OUTSIDER_ID, "login": "outsider"},
                "updated_at": "2026-09-05T12:00:00Z",
                "created_at": "2026-09-05T12:00:00Z",
            }
        )
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertTrue(any("no author" in r for r in result.reasons))

    def test_bot_marker_spoof_cannot_capture_update(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=60
        )
        fake.comments.append(
            {
                "id": 61,
                "body": f"{GUARD_MARKER}\nforged by human\n",
                "user": {"id": AUTHOR_ID, "login": "author"},
                "updated_at": "2026-09-05T12:30:00Z",
                "created_at": "2026-09-05T12:30:00Z",
            }
        )
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        # Human forged marker must remain; bot creates its own comment.
        human = next(c for c in fake.comments if c["id"] == 61)
        self.assertIn("forged by human", human["body"])
        bot_comments = [
            c
            for c in fake.comments
            if c.get("user", {}).get("id") == BOT_ID and GUARD_MARKER in (c.get("body") or "")
        ]
        self.assertEqual(len(bot_comments), 1)
        self.assertNotEqual(bot_comments[0]["id"], 61)

    def test_pagination_over_one_hundred(self) -> None:
        fake = FakeAPI()
        # 105 filler comments + one valid author report on the last page.
        for i in range(105):
            fake.comments.append(
                {
                    "id": 1000 + i,
                    "body": "noise",
                    "user": {"id": OUTSIDER_ID, "login": "x"},
                    "updated_at": f"2026-09-05T10:00:{i:02d}Z",
                    "created_at": f"2026-09-05T10:00:{i:02d}Z",
                }
            )
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=9999
        )
        result = assess_pull(fake.api(), REPO, 1)
        self.assertTrue(result.ok)

    def test_changed_head_midpublish_retries(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=70
        )
        api = fake.api()
        assessment = assess_pull(api, REPO, 1)
        self.assertTrue(assessment.ok)
        # Head moves after assessment, before publish.
        new_head = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        fake.pull = fake._pull(new_head, BASE)
        fake.tree_paths[new_head] = [".github/workflows/test.yml"]
        fake.files[(new_head, ".github/workflows/test.yml")] = fake.files[
            (HEAD, ".github/workflows/test.yml")
        ]
        with self.assertRaisesRegex(GuardError, "changed before publish"):
            publish_assessment(api, assessment)

    def test_unknown_policy_not_configured(self) -> None:
        fake = FakeAPI()
        del fake.files[(BASE, ".github/a38.json")]
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_configured")

    def test_invalid_policy_fails(self) -> None:
        fake = FakeAPI()
        fake.files[(BASE, ".github/a38.json")] = b'{"schema":"nope"}'
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertTrue(
            result.status in {"not_configured", "invalid_policy", "fail"}
            or any("maintainer config" in r for r in result.reasons)
        )

    def test_changed_workflow_bytes_fail(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=80
        )
        fake.files[(HEAD, ".github/workflows/test.yml")] = _workflow_yaml(["pytest"]) + b"\n# changed\n"
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertTrue(any("bytes changed" in r for r in result.reasons))

    def test_new_workflow_job_unclassified(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=81
        )
        # Same bytes at base and head but with an extra job → also byte change;
        # set both equal with extra job and expand policy? Prefer head-only new file.
        new_wf = _workflow_yaml(["pytest", "lint"])
        fake.files[(HEAD, ".github/workflows/test.yml")] = new_wf
        fake.files[(BASE, ".github/workflows/test.yml")] = new_wf
        result = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(result.ok)
        self.assertTrue(any("unclassified" in r for r in result.reasons))

    def test_same_sha_different_bases_contexts(self) -> None:
        c1 = status_context_enforce("develop")
        c2 = status_context_enforce("main")
        self.assertNotEqual(c1, c2)
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE2)
        fake.pull["base"]["ref"] = "main"
        fake.tree_paths[BASE2] = [".github/workflows/test.yml"]
        fake.files[(BASE2, ".github/a38.json")] = fake.files[(BASE, ".github/a38.json")]
        fake.files[(BASE2, ".github/workflows/test.yml")] = fake.files[
            (BASE, ".github/workflows/test.yml")
        ]
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=90
        )
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.context, status_context_enforce("main"))
        self.assertTrue(any("(main)" in (s.get("context") or "") for s in fake.statuses))

    def test_dry_run_no_writes(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=91
        )
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=True, publish=True)
        self.assertTrue(result.ok)
        self.assertIn("dry-run", result.writes)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.statuses, [])

    def test_own_event_ignored(self) -> None:
        payload = {
            "action": "created",
            "issue": {"number": 1, "pull_request": {"url": "x"}},
            "comment": {"user": {"id": BOT_ID}, "body": GUARD_MARKER},
            "repository": {"full_name": REPO},
        }
        reason = event_should_ignore("issue_comment", payload, own_id=BOT_ID)
        self.assertIsNotNone(reason)
        self.assertIn("own bot", reason or "")

        fake = FakeAPI()
        out = reconcile_event(
            fake.api(),
            event_name="issue_comment",
            payload=payload,
            dry_run=False,
            publish=True,
        )
        assert isinstance(out, dict)
        self.assertEqual(out["status"], "ignored")
        self.assertEqual(fake.writes, [])

    def test_issue_only_event_ignored(self) -> None:
        payload = {
            "action": "created",
            "issue": {"number": 9},
            "comment": {"user": {"id": AUTHOR_ID}, "body": "hi"},
            "repository": {"full_name": REPO},
        }
        reason = event_should_ignore("issue_comment", payload, own_id=BOT_ID)
        self.assertIsNotNone(reason)

    def test_api_denied_not_success(self) -> None:
        fake = FakeAPI()
        fake.denied_prefixes.append(f"/repos/{REPO}/pulls/1")
        with self.assertRaisesRegex(GuardError, "denied"):
            assess_pull(fake.api(), REPO, 1)

    def test_observe_mode_advisory_context(self) -> None:
        fake = FakeAPI()
        fake.files[(BASE, ".github/a38.json")] = json.dumps(_policy(mode="observe")).encode()
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.mode, "observe")
        self.assertEqual(result.state_for_status, "success")
        self.assertTrue(result.observe_context.startswith("A38 / report (observe:"))
        self.assertEqual(result.context, "")
        contexts = [s["context"] for s in fake.statuses]
        self.assertTrue(any("observe:" in c for c in contexts))
        self.assertFalse(any(c == status_context_enforce("develop") for c in contexts))

    def test_closed_pr_no_writes(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, state="closed")
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=92
        )
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertIn("skipped:closed", result.writes)
        self.assertEqual(fake.writes, [])

    @mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
    def test_actions_token_uses_github_actions_bot_id(self) -> None:
        fake = FakeAPI()
        fake.user_endpoint_denied = True
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=93
        )
        api = fake.api()
        own_id, login = api.resolve_own_user()
        self.assertEqual(own_id, BOT_ID)
        self.assertEqual(login, "github-actions[bot]")
        result = reconcile_pull(api, REPO, 1, publish=True)
        self.assertTrue(result.ok)

    def test_cli_dry_run(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=94
        )
        env = {"GH_TOKEN": "fake"}
        with mock.patch.object(a38_guard, "GitHubApi", return_value=fake.api()):
            code = main(
                ["reconcile", "--repo", REPO, "--pr", "1", "--dry-run", "--json"],
                env=env,
                api=fake.api(),
            )
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes, [])

    def test_foreign_pagination_url_rejected(self) -> None:
        api = GitHubApi("t", request_fn=lambda *a, **k: (200, [], {}), sleep_fn=lambda s: None)
        with self.assertRaisesRegex(GuardError, "refusing non-"):
            api.request("GET", "https://evil.example/repos/x")

    def test_pagination_bound_exceeded(self) -> None:
        fake = FakeAPI()
        # Exceed hard bound (>1000, guard uses 2000): refuse partial accept.
        fake.comments = [
            {
                "id": i,
                "body": "noise",
                "user": {"id": OUTSIDER_ID, "login": "x"},
                "updated_at": "2026-09-05T10:00:00Z",
                "created_at": "2026-09-05T10:00:00Z",
            }
            for i in range(2001)
        ]
        with self.assertRaisesRegex(GuardError, "pagination exceeded"):
            assess_pull(fake.api(), REPO, 1)


class A38GuardYamlTests(unittest.TestCase):
    def test_enumerate_jobs_safe_yaml(self) -> None:
        jobs = a38_guard.enumerate_workflow_jobs(
            ".github/workflows/test.yml", _workflow_yaml(["a", "b"])
        )
        self.assertEqual(jobs, ["a", "b"])

    def test_deduplicate_unchanged_comment_and_status(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=95
        )
        first = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(first.ok)
        writes_after_first = list(fake.writes)
        second = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(second.ok)
        self.assertTrue(any("unchanged" in w for w in second.writes))
        # No additional create/update mutations beyond the first publish.
        self.assertEqual(fake.writes, writes_after_first)


if __name__ == "__main__":
    unittest.main()
