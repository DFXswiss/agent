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
    fetch_pull,
    find_tool_attribution,
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
# Immutable tip of the trusted default branch used to locate pr-guard.json.
DEFAULT_TIP = "dddddddddddddddddddddddddddddddddddddddd"
REPO = "example/public-app"
AUTHOR_ID = 1001
BOT_ID = GITHUB_ACTIONS_BOT_ID
OUTSIDER_ID = 2002
AI_COMMIT_MESSAGE = (
    "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>\n"
    "Claude-Session: https://claude.ai/code/session_01ELTd6i5zDSC7RzsVV8DsxK"
)
GENERATED_WITH_BANNER = (
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
)


def _clean_commit(
    *,
    sha: str = HEAD,
    message: str = "feat: ok\n",
    author_name: str = "author",
    author_email: str = "author@example.com",
    login: str = "author",
) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"name": author_name, "email": author_email},
            "committer": {"name": author_name, "email": author_email},
        },
        "author": {"id": AUTHOR_ID, "login": login},
        "committer": {"id": AUTHOR_ID, "login": login},
    }


def _pr_guard_config(
    *,
    enforce: list[str] | None = None,
    exclude: list[str] | None = None,
    default: str = "enforce",
) -> dict:
    return {
        "schema": "pr-guard/v1",
        "a38": {
            "enforce": list(enforce or []),
            "exclude": list(exclude or []),
            "default": default,
        },
    }


def _policy(
    *,
    mode: str = "enforce",
    jobs: list | None = None,
    exclusions: list | None = None,
    readme_only: dict | None = None,
) -> dict:
    payload = {
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
    if readme_only is not None:
        payload["readme_only"] = readme_only
    return payload


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
    readme_only: bool = False,
    markdown_only: bool = False,
    duration_s: float = 1.0,
) -> str:
    payload: dict[str, Any] = {
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
                "duration_s": duration_s,
                "timeout_s": 600,
            }
        ],
    }
    if readme_only:
        payload["readme_only"] = True
    if markdown_only:
        payload["markdown_only"] = True
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
        self.pull_files: list[dict[str, Any]] = []
        # Logins that return HTTP 404 from the collaborator permission endpoint.
        self.permission_404: set[str] = set()
        self.timeline: list[dict[str, Any]] = []
        self.timeline_404 = False
        # Branch/tag ref → immutable commit SHA for GET /commits/{ref}.
        self.ref_commits: dict[str, str] = {
            "develop": DEFAULT_TIP,
            "main": DEFAULT_TIP,
            "integration": DEFAULT_TIP,
            "release": DEFAULT_TIP,
        }
        self.commits: list[dict[str, Any]] = [_clean_commit()]
        wf = _workflow_yaml(["pytest"])
        self.files[(BASE, ".github/a38.json")] = json.dumps(_policy()).encode()
        self.files[(BASE, ".github/workflows/test.yml")] = wf
        self.files[(HEAD, ".github/workflows/test.yml")] = wf

    def set_pr_guard_config(self, config: dict | None, *, revision: str = DEFAULT_TIP) -> None:
        key = (revision, ".github/pr-guard.json")
        if config is None:
            self.files.pop(key, None)
            return
        self.files[key] = json.dumps(config).encode()

    def _pull(
        self,
        head: str,
        base: str,
        *,
        state: str = "open",
        draft: bool = False,
        title: str = "",
        body: str = "",
    ) -> dict[str, Any]:
        return {
            "number": 1,
            "state": state,
            "draft": draft,
            "title": title,
            "body": body,
            "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
            "head": {"sha": head, "repo": {"full_name": REPO, "default_branch": "feature"}},
            "base": {
                "sha": base,
                "ref": "develop",
                "repo": {
                    "private": False,
                    "full_name": REPO,
                    # Trusted default branch locates configuration only.
                    "default_branch": "develop",
                },
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
        if method_u == "GET" and path_only == f"/repos/{REPO}/pulls/1/commits":
            return 200, list(self.commits), {}
        if method_u == "GET" and path_only == f"/repos/{REPO}/pulls/1/files":
            return 200, list(self.pull_files), {}
        if method_u == "GET" and path_only == f"/repos/{REPO}/pulls/1/reviews":
            return 200, self.reviews, {}
        if method_u == "GET" and "/collaborators/" in path_only:
            from urllib.parse import unquote

            login = unquote(path_only.split("/collaborators/")[1].split("/")[0])
            if login in self.permission_404:
                return 404, {"message": "Not Found"}, {}
            return 200, self.permissions.get(
                login,
                {
                    "permission": "read",
                    "user": {"id": OUTSIDER_ID, "type": "User"},
                },
            ), {}

        if method_u == "GET" and path_only.startswith(f"/repos/{REPO}/issues/1/timeline"):
            if self.timeline_404:
                return 404, {"message": "Not Found"}, {}
            page = int(parse_qs(urlparse(path).query).get("page", ["1"])[0])
            per_page = 100
            start = (page - 1) * per_page
            chunk = self.timeline[start : start + per_page]
            headers: dict[str, str] = {}
            if start + per_page < len(self.timeline):
                next_page = page + 1
                headers["link"] = (
                    f'<https://api.github.com/repos/{REPO}/issues/1/timeline'
                    f"?per_page=100&page={next_page}>; rel=\"next\""
                )
            return 200, chunk, headers

        if (
            method_u == "GET"
            and path_only.startswith(f"/repos/{REPO}/commits/")
            and not path_only.endswith("/statuses")
        ):
            from urllib.parse import unquote

            ref = unquote(path_only[len(f"/repos/{REPO}/commits/") :])
            if ref in self.ref_commits:
                return 200, {"sha": self.ref_commits[ref]}, {}
            if a38_guard.HEAD_SHA_RE.fullmatch(ref.lower() if isinstance(ref, str) else ""):
                return 200, {"sha": ref.lower()}, {}
            return 404, {"message": "Not Found"}, {}

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

    def test_find_tool_attribution_coauthor_and_session(self) -> None:
        reasons = find_tool_attribution(AI_COMMIT_MESSAGE, source="commit abcdef0 message")
        self.assertTrue(any("AI co-author trailer" in r for r in reasons))
        self.assertTrue(any("AI session header" in r for r in reasons))
        self.assertTrue(any("commit abcdef0 message" in r for r in reasons))
        self.assertFalse(any("session_01ELTd6i5zDSC7RzsVV8DsxK" in r for r in reasons))
        self.assertFalse(any("https://claude.ai" in r for r in reasons))

    def test_find_tool_attribution_generated_with_banner(self) -> None:
        reasons = find_tool_attribution(GENERATED_WITH_BANNER, source="PR body")
        self.assertTrue(any("generated-with banner" in r for r in reasons))
        self.assertTrue(any(r.startswith("PR body:") for r in reasons))

    def test_find_tool_attribution_generated_with_token_no_brackets(self) -> None:
        reasons = find_tool_attribution("generated with Claude", source="PR body")
        self.assertTrue(any("generated-with banner" in r for r in reasons))

    def test_find_tool_attribution_human_coauthor_clean(self) -> None:
        text = (
            "Co-authored-by: TaprootFreakAI "
            "<315477232+TaprootFreakAI@users.noreply.github.com>"
        )
        self.assertEqual(find_tool_attribution(text, source="commit abcdef0 message"), [])
        human_vendor = (
            "Co-authored-by: Alice <alice@openai.com>"
        )
        self.assertEqual(
            find_tool_attribution(human_vendor, source="commit abcdef0 message"), []
        )
        self.assertEqual(
            find_tool_attribution(
                "Co-authored-by: alice@openai.com",
                source="commit abcdef0 message",
            ),
            [],
        )

    def test_find_tool_attribution_prose_negatives(self) -> None:
        self.assertEqual(
            find_tool_attribution("generated files", source="PR body"), []
        )
        self.assertEqual(
            find_tool_attribution("generated with a unique id", source="PR body"), []
        )
        self.assertEqual(
            find_tool_attribution("generated with [our makefile]", source="PR body"),
            [],
        )
        self.assertEqual(
            find_tool_attribution(
                "regenerated without Claude", source="PR body"
            ),
            [],
        )
        self.assertEqual(
            find_tool_attribution(
                "this change does not mention vendors", source="PR body"
            ),
            [],
        )
        self.assertEqual(
            find_tool_attribution("contact noreply@anthropic.com", source="PR body"),
            [],
        )

    def test_find_tool_attribution_identity_surfaces(self) -> None:
        reasons = find_tool_attribution("claude", source="commit abcdef0 author")
        self.assertTrue(any("AI author identity" in r for r in reasons))
        reasons = find_tool_attribution(
            "noreply@anthropic.com", source="commit abcdef0 committer"
        )
        self.assertTrue(any("AI author identity" in r for r in reasons))
        self.assertEqual(
            find_tool_attribution(
                "alice@openai.com", source="commit abcdef0 author"
            ),
            [],
        )
        reasons = find_tool_attribution(
            "Claude @ DFX", source="commit abcdef0 author"
        )
        self.assertTrue(any("AI author identity" in r for r in reasons))
        self.assertEqual(
            find_tool_attribution(
                "notnoreply@anthropic.com", source="commit abcdef0 author"
            ),
            [],
        )


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
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)

    def test_draft_no_report_omits_status_and_exits_zero(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.draft)
        self.assertTrue(any("no author" in r for r in result.reasons))
        self.assertTrue(any(w.startswith("comment:") for w in result.writes))
        self.assertTrue(
            any(w == "status:skipped:draft" for w in result.writes)
            or not any(w.startswith("status:create:") for w in result.writes)
        )
        self.assertNotIn(
            status_context_enforce("develop"),
            [s["context"] for s in fake.statuses],
        )
        self.assertEqual(a38_guard._assessment_exit_code(result), 0)
        body = result.comment_body
        self.assertIn("<!-- PR-GUARD:A38:v1 -->", body)
        self.assertIn("EN:", body)
        self.assertIn("DE:", body)
        self.assertIn("Thanks for your contribution!", body)
        self.assertIn("A38", body)
        self.assertIn("Qualitätsregeln", body)
        self.assertIn(result.standard_url, body)
        self.assertIn(f"[A38 quality rules]({result.standard_url})", body)
        self.assertIn(f"[A38-Qualitätsregeln]({result.standard_url})", body)
        self.assertNotIn(f"A38 quality rules: {result.standard_url}", body)
        self.assertNotIn(f"A38-Qualitätsregeln: {result.standard_url}", body)
        self.assertNotIn("<details>", body)
        self.assertNotIn("Problems:", body)
        self.assertNotIn("local-CI report", body)
        self.assertNotIn("no blocking A38 report status", body)
        self.assertNotIn("tool-attribution", body)
        self.assertNotIn("python -m agent_cli.a38 run", body)
        self.assertNotIn("A38-POLICY-APPROVAL", body)
        self.assertNotIn("missing or invalid", body)
        self.assertNotRegex(body, r"A38 fail:")
        self.assertNotRegex(body, r"A38 pass:")

    def test_draft_ai_commit_trailer_hard_fails_exit_one(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.commits = [_clean_commit(message=AI_COMMIT_MESSAGE)]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        body = result.comment_body
        self.assertIn("<!-- PR-GUARD:A38:v1 -->", body)
        self.assertIn("EN:", body)
        self.assertIn("DE:", body)
        self.assertIn("Thanks for your contribution!", body)
        self.assertIn("A38", body)
        self.assertIn("Qualitätsregeln", body)
        self.assertIn(result.standard_url, body)
        self.assertIn(f"[A38 quality rules]({result.standard_url})", body)
        self.assertIn(f"[A38-Qualitätsregeln]({result.standard_url})", body)
        self.assertNotIn(f"A38 quality rules: {result.standard_url}", body)
        self.assertNotIn(f"A38-Qualitätsregeln: {result.standard_url}", body)
        self.assertNotIn("<details>", body)
        self.assertNotIn("Problems:", body)
        self.assertNotIn("local-CI report", body)
        self.assertNotIn("no blocking A38 report status", body)
        self.assertNotIn("tool-attribution", body)
        self.assertNotIn("python -m agent_cli.a38 run", body)
        self.assertNotIn("A38-POLICY-APPROVAL", body)
        self.assertNotRegex(body, r"A38 fail:")
        self.assertNotRegex(body, r"A38 pass:")

    def test_draft_generated_with_banner_hard_fails_exit_one(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(
            HEAD,
            BASE,
            draft=True,
            title="Normal summary",
            body=f"Normal summary\n\n{GENERATED_WITH_BANNER}\n",
        )
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        body = result.comment_body
        self.assertIn("<!-- PR-GUARD:A38:v1 -->", body)
        self.assertIn("EN:", body)
        self.assertIn("DE:", body)
        self.assertIn("Thanks for your contribution!", body)
        self.assertIn("A38", body)
        self.assertIn("Qualitätsregeln", body)
        self.assertIn(result.standard_url, body)
        self.assertIn(f"[A38 quality rules]({result.standard_url})", body)
        self.assertIn(f"[A38-Qualitätsregeln]({result.standard_url})", body)
        self.assertNotIn(f"A38 quality rules: {result.standard_url}", body)
        self.assertNotIn(f"A38-Qualitätsregeln: {result.standard_url}", body)
        self.assertNotIn("<details>", body)
        self.assertNotIn("Problems:", body)
        self.assertNotIn("local-CI report", body)
        self.assertNotIn("no blocking A38 report status", body)
        self.assertNotIn("tool-attribution", body)
        self.assertNotIn("python -m agent_cli.a38 run", body)
        self.assertNotIn("A38-POLICY-APPROVAL", body)

    def test_draft_human_coauthor_only_exits_zero(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.commits = [
            _clean_commit(
                message=(
                    "feat: ok\n\n"
                    "Co-authored-by: TaprootFreakAI "
                    "<315477232+TaprootFreakAI@users.noreply.github.com>\n"
                )
            )
        ]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 0)
        self.assertTrue(
            any(w == "status:skipped:draft" for w in result.writes)
            or not any(w.startswith("status:create:") for w in result.writes)
        )
        self.assertNotIn(
            status_context_enforce("develop"),
            [s["context"] for s in fake.statuses],
        )

    def test_ready_ai_commit_trailer_posts_failure_status(self) -> None:
        fake = FakeAPI()
        fake.commits = [_clean_commit(message=AI_COMMIT_MESSAGE)]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertFalse(result.draft)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        self.assertTrue((matching[0].get("description") or "").startswith("hard_fail:"))
        body = result.comment_body
        self.assertTrue(
            "Tool-attribution" in body or "attribution markers" in body
            or "Attribution-Marker" in body or "Tool-Attribution" in body
        )
        self.assertNotIn("even while this pull request is a draft", body)
        self.assertNotIn("auch im Draft", body)

    def test_ready_ai_commit_trailer_with_valid_report(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=74
        )
        fake.commits = [_clean_commit(message=AI_COMMIT_MESSAGE)]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        body = result.comment_body
        self.assertNotIn("missing or invalid", body)
        self.assertNotIn("fehlt oder ist ungültig", body)
        self.assertIn("author local-CI report accepted for this head", body)
        self.assertIn("Autor-Local-CI-Report für diesen Head akzeptiert", body)
        self.assertTrue(
            "Tool-attribution" in body or "attribution markers" in body
            or "Attribution-Marker" in body or "Tool-Attribution" in body
        )
        self.assertNotIn("even while this pull request is a draft", body)
        self.assertNotIn("auch im Draft", body)

    def test_draft_title_only_generated_with_hard_fails(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(
            HEAD,
            BASE,
            draft=True,
            title="Generated with [Claude Code]",
            body="clean summary",
        )
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")

    def test_draft_hard_fail_status_clears_when_attribution_removed(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(
            HEAD,
            BASE,
            draft=True,
            title="Normal summary",
            body=f"Normal summary\n\n{GENERATED_WITH_BANNER}\n",
        )
        first = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(first.hard_fail)
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        self.assertTrue((matching[0].get("description") or "").startswith("hard_fail:"))
        fake.pull = fake._pull(
            HEAD,
            BASE,
            draft=True,
            title="Normal summary",
            body="clean summary",
        )
        second = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(second.hard_fail)
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "success")
        self.assertEqual(second.state_for_status, "success")
        self.assertIn("omitted until Ready", matching[0].get("description") or "")

    def test_draft_does_not_clear_unrelated_enforce_failure(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        enforce = status_context_enforce("develop")
        fake.statuses.insert(
            0,
            {
                "id": 1,
                "sha": HEAD,
                "state": "failure",
                "description": "fail: no author local-CI report",
                "context": enforce,
            },
        )
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(result.hard_fail)
        self.assertTrue(any(w == "status:skipped:draft" for w in result.writes))
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertEqual(matching[0]["state"], "failure")
        self.assertEqual(matching[0]["description"], "fail: no author local-CI report")

    def test_ready_hard_fail_then_draft_clean_clears(self) -> None:
        fake = FakeAPI()
        fake.commits = [_clean_commit(message=AI_COMMIT_MESSAGE)]
        first = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(first.draft)
        self.assertTrue(first.hard_fail)
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        self.assertTrue((matching[0].get("description") or "").startswith("hard_fail:"))
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.commits = [_clean_commit(message="feat: ok\n")]
        second = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(second.draft)
        self.assertFalse(second.hard_fail)
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertEqual(matching[0]["state"], "success")

    def test_denied_commits_list_raises(self) -> None:
        fake = FakeAPI()
        fake.denied_prefixes.append(f"/repos/{REPO}/pulls/1/commits")
        with self.assertRaisesRegex(GuardError, "denied"):
            reconcile_pull(fake.api(), REPO, 1, publish=True)

    def test_truncated_pull_commits_list_raises(self) -> None:
        fake = FakeAPI()
        fake.commits = [_clean_commit(sha=f"{i:040x}") for i in range(250)]
        with self.assertRaisesRegex(GuardError, "250-commit cap"):
            reconcile_pull(fake.api(), REPO, 1, publish=True)

    def test_just_under_pull_commits_cap_scans(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.commits = [_clean_commit(sha=f"{i:040x}") for i in range(249)]
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertFalse(result.hard_fail)
        self.assertEqual(a38_guard._assessment_exit_code(result), 0)

    def test_missing_commit_message_hard_fails(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.commits = [
            {
                "sha": HEAD,
                "commit": {
                    "author": {"name": "author", "email": "author@example.com"},
                    "committer": {"name": "author", "email": "author@example.com"},
                },
                "author": {"id": AUTHOR_ID, "login": "author"},
                "committer": {"id": AUTHOR_ID, "login": "author"},
            }
        ]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        self.assertTrue(any("message missing" in r for r in result.reasons))
        self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
        enforce = status_context_enforce("develop")
        matching = [s for s in fake.statuses if s.get("context") == enforce]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "failure")
        self.assertTrue((matching[0].get("description") or "").startswith("hard_fail:"))

    def test_blank_commit_message_hard_fails(self) -> None:
        for blank in ("", "   \n"):
            with self.subTest(message=blank):
                fake = FakeAPI()
                fake.pull = fake._pull(HEAD, BASE, draft=True)
                fake.commits = [_clean_commit(message=blank)]
                result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
                self.assertTrue(result.hard_fail)
                self.assertFalse(result.ok)
                self.assertEqual(a38_guard._assessment_exit_code(result), 1)
                self.assertTrue(any("message missing" in r for r in result.reasons))
                self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
                enforce = status_context_enforce("develop")
                matching = [s for s in fake.statuses if s.get("context") == enforce]
                self.assertTrue(matching)
                self.assertEqual(matching[0]["state"], "failure")
                self.assertTrue((matching[0].get("description") or "").startswith("hard_fail:"))

    def test_ai_author_identity_hard_fails(self) -> None:
        fake = FakeAPI()
        fake.commits = [
            _clean_commit(message="feat: ok\n", login="claude")
        ]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
        self.assertTrue(any("AI author identity" in r for r in result.reasons))

    def test_human_vendor_email_is_not_ai_identity(self) -> None:
        fake = FakeAPI()
        fake.commits = [
            _clean_commit(
                message="feat: ok\n",
                author_name="Alice",
                author_email="alice@openai.com",
            )
        ]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertFalse(result.hard_fail)

    def test_ready_blank_commit_message_comment(self) -> None:
        fake = FakeAPI()
        fake.commits = [_clean_commit(message="")]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.draft)
        body = result.comment_body
        self.assertIn("unscannable or empty commit message", body)
        self.assertNotIn("Remove those attribution markers", body)

    def test_ready_blank_message_and_attribution_comment(self) -> None:
        fake = FakeAPI()
        fake.commits = [_clean_commit(message="", login="claude")]
        result = reconcile_pull(fake.api(), REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        body = result.comment_body
        self.assertIn("Tool-attribution or an unscannable commit message", body)
        self.assertIn("non-empty commit message", body)

    def test_draft_valid_report_omits_enforce_status(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        fake.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=21)
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.draft)
        self.assertEqual(result.status, "pass")
        self.assertNotIn(
            status_context_enforce("develop"),
            [s["context"] for s in fake.statuses],
        )
        self.assertTrue(
            any(w == "status:skipped:draft" for w in result.writes)
            or not any(w.startswith("status:create:") for w in result.writes)
        )
        self.assertEqual(a38_guard._assessment_exit_code(result), 0)
        body = result.comment_body
        self.assertIn("<!-- PR-GUARD:A38:v1 -->", body)
        self.assertIn("EN:", body)
        self.assertIn("DE:", body)
        self.assertIn("Thanks for your contribution!", body)
        self.assertIn("A38", body)
        self.assertIn("Qualitätsregeln", body)
        self.assertIn(result.standard_url, body)
        self.assertIn(f"[A38 quality rules]({result.standard_url})", body)
        self.assertIn(f"[A38-Qualitätsregeln]({result.standard_url})", body)
        self.assertNotIn(f"A38 quality rules: {result.standard_url}", body)
        self.assertNotIn(f"A38-Qualitätsregeln: {result.standard_url}", body)
        self.assertNotIn("<details>", body)
        self.assertNotIn("Problems:", body)
        self.assertNotIn("local-CI report", body)
        self.assertNotIn("no blocking A38 report status", body)
        self.assertNotIn("tool-attribution", body)
        self.assertNotIn("python -m agent_cli.a38 run", body)
        self.assertNotIn("A38-POLICY-APPROVAL", body)
        self.assertNotRegex(body, r"A38 fail:")
        self.assertNotRegex(body, r"A38 pass:")

    def test_fetch_pull_draft_true_only_when_json_true(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        snap = fetch_pull(fake.api(), REPO, 1)
        self.assertTrue(snap.draft)

        fake.pull = fake._pull(HEAD, BASE, draft=False)
        self.assertFalse(fetch_pull(fake.api(), REPO, 1).draft)

        fake.pull = fake._pull(HEAD, BASE)
        del fake.pull["draft"]
        self.assertFalse(fetch_pull(fake.api(), REPO, 1).draft)

        fake.pull = fake._pull(HEAD, BASE)
        fake.pull["draft"] = None
        self.assertFalse(fetch_pull(fake.api(), REPO, 1).draft)

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

    def test_changed_title_midpublish_retries(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=71
        )
        api = fake.api()
        assessment = assess_pull(api, REPO, 1)
        self.assertTrue(assessment.ok)
        self.assertFalse(assessment.hard_fail)
        fake.pull = fake._pull(HEAD, BASE, title=GENERATED_WITH_BANNER)
        with self.assertRaisesRegex(GuardError, "changed before publish"):
            publish_assessment(api, assessment)
        result = reconcile_pull(api, REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)

    def test_changed_body_midpublish_retries(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=72
        )
        api = fake.api()
        assessment = assess_pull(api, REPO, 1)
        self.assertTrue(assessment.ok)
        self.assertFalse(assessment.hard_fail)
        fake.pull = fake._pull(HEAD, BASE, body=GENERATED_WITH_BANNER)
        with self.assertRaisesRegex(GuardError, "changed before publish"):
            publish_assessment(api, assessment)
        result = reconcile_pull(api, REPO, 1, dry_run=False, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertFalse(result.ok)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)

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
        fake.pull["base"]["repo"]["default_branch"] = "main"
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
        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.scope_decision, "exclude")
        self.assertEqual(result.context, status_context_enforce("main"))
        self.assertFalse(any(w.startswith("comment:") for w in result.writes))
        self.assertTrue(any("(main)" in (s.get("context") or "") for s in fake.statuses))

        # Same head SHA against an in-scope target still enforces with a distinct context.
        fake.pull["base"]["ref"] = "develop"
        fake.pull["base"]["repo"]["default_branch"] = "develop"
        fake.pull["base"]["sha"] = BASE
        enforced = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(enforced.ok)
        self.assertEqual(enforced.status, "pass")
        self.assertEqual(enforced.scope_decision, "enforce")
        self.assertEqual(enforced.context, status_context_enforce("develop"))
        self.assertNotEqual(enforced.context, result.context)

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

    def test_observe_mode_attribution_hard_fails_exit_one(self) -> None:
        fake = FakeAPI()
        fake.files[(BASE, ".github/a38.json")] = json.dumps(
            _policy(mode="observe")
        ).encode()
        fake.commits = [_clean_commit(message=AI_COMMIT_MESSAGE)]
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.hard_fail)
        self.assertEqual(a38_guard._assessment_exit_code(result), 1)
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


class A38PrGuardConfigScopeTests(unittest.TestCase):
    def _exclude_release_config(self, fake: FakeAPI) -> None:
        fake.set_pr_guard_config(
            _pr_guard_config(enforce=["integration"], exclude=["release", "main"], default="enforce")
        )

    def _release_pull(self, fake: FakeAPI) -> None:
        fake.pull["base"]["ref"] = "main"
        fake.pull["base"]["repo"]["default_branch"] = "develop"
        self._exclude_release_config(fake)

    def test_excluded_target_is_not_applicable_without_policy_or_report(self) -> None:
        fake = FakeAPI()
        self._release_pull(fake)
        del fake.files[(BASE, ".github/a38.json")]
        fake.statuses.insert(
            0,
            {
                "id": 1,
                "sha": HEAD,
                "state": "failure",
                "description": "fail: no author local-CI report comment on this pull request",
                "context": status_context_enforce("main"),
            },
        )
        human = {
            "id": 77,
            "body": "release notes from a maintainer",
            "user": {"id": AUTHOR_ID, "login": "author"},
            "updated_at": "2026-09-05T11:00:00Z",
            "created_at": "2026-09-05T11:00:00Z",
        }
        fake.comments.append(human)
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_applicable")
        self.assertFalse(result.closed)
        self.assertEqual(result.required_names, [])
        self.assertEqual(result.comment_body, "")
        self.assertFalse(result.skip_publish)
        self.assertEqual(result.trusted_default_branch, "develop")
        self.assertEqual(result.config_revision, DEFAULT_TIP)
        self.assertEqual(result.scope_decision, "exclude")
        self.assertEqual(result.base_ref, "main")
        self.assertEqual(result.context, status_context_enforce("main"))
        self.assertEqual(result.state_for_status, "success")
        self.assertEqual(result.description, a38_guard.NOT_APPLICABLE_DESCRIPTION)
        self.assertEqual(
            result.scope_reason, "target branch 'main' has nothing for A38 to check"
        )
        self.assertTrue(
            any("nothing for A38 to check" in reason for reason in result.reasons)
        )
        self.assertFalse(any(w.startswith("comment:") for w in result.writes))
        self.assertTrue(
            any(
                s["context"] == status_context_enforce("main") and s["state"] == "success"
                for s in fake.statuses
            )
        )
        self.assertEqual(
            next(s for s in fake.statuses if s["context"] == status_context_enforce("main"))[
                "description"
            ],
            a38_guard.NOT_APPLICABLE_DESCRIPTION,
        )
        self.assertFalse(any(s["context"] == status_context_enforce("develop") for s in fake.statuses))
        self.assertEqual([c["id"] for c in fake.comments], [77])
        self.assertEqual(fake.comments[0]["body"], human["body"])
        payload = result.to_json()
        self.assertEqual(payload["trusted_default_branch"], "develop")
        self.assertEqual(payload["config_revision"], DEFAULT_TIP)
        self.assertEqual(payload["scope_decision"], "exclude")

    def test_excluded_target_on_draft_still_posts_not_applicable_success(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, draft=True)
        self._release_pull(fake)
        del fake.files[(BASE, ".github/a38.json")]
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.draft)
        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.state_for_status, "success")
        self.assertFalse(any(w == "status:skipped:draft" for w in result.writes))
        self.assertTrue(
            any(
                s["context"] == status_context_enforce("main") and s["state"] == "success"
                for s in fake.statuses
            )
        )
        self.assertEqual(a38_guard._assessment_exit_code(result), 0)
        self.assertFalse(any(w.startswith("comment:") for w in result.writes))

    def test_invalidate_status_skips_drafts(self) -> None:
        fake = FakeAPI()
        snap = fetch_pull(fake.api(), REPO, 1)
        self.assertFalse(snap.draft)
        a38_guard.invalidate_status(fake.api(), snap)
        self.assertTrue(
            any(s.get("state") == "error" for s in fake.statuses)
        )
        fake_draft = FakeAPI()
        fake_draft.pull = fake_draft._pull(HEAD, BASE, draft=True)
        draft_snap = fetch_pull(fake_draft.api(), REPO, 1)
        self.assertTrue(draft_snap.draft)
        before = list(fake_draft.statuses)
        a38_guard.invalidate_status(fake_draft.api(), draft_snap)
        self.assertEqual(fake_draft.statuses, before)
        self.assertFalse(any(s.get("state") == "error" for s in fake_draft.statuses))

    def test_configurable_enforce_exclude_default_and_exact_match(self) -> None:
        fake = FakeAPI()
        fake.set_pr_guard_config(
            _pr_guard_config(
                enforce=["integration"],
                exclude=["release"],
                default="exclude",
            )
        )
        # Unlisted target follows default=exclude.
        fake.pull["base"]["ref"] = "hotfix"
        fake.ref_commits["hotfix"] = DEFAULT_TIP
        out = assess_pull(fake.api(), REPO, 1)
        self.assertEqual(out.status, "not_applicable")
        self.assertEqual(out.scope_decision, "exclude")
        self.assertTrue(any("a38.default" in reason for reason in out.reasons))

        # Exact case-sensitive enforce match.
        fake.pull["base"]["ref"] = "integration"
        blocked = assess_pull(fake.api(), REPO, 1)
        self.assertEqual(blocked.scope_decision, "enforce")
        self.assertFalse(blocked.ok)
        self.assertTrue(any("no author" in reason for reason in blocked.reasons))

        # Case differs from listed exclude name → default exclude, not the list entry.
        fake.set_pr_guard_config(
            _pr_guard_config(enforce=[], exclude=["Release"], default="enforce")
        )
        fake.pull["base"]["ref"] = "release"
        enforced = assess_pull(fake.api(), REPO, 1)
        self.assertEqual(enforced.scope_decision, "enforce")
        self.assertFalse(enforced.ok)

    def test_absent_config_legacy_enforce_all(self) -> None:
        fake = FakeAPI()
        # No .github/pr-guard.json at trusted tip: legacy enforce-all.
        fake.pull["base"]["ref"] = "release"
        blocked = assess_pull(fake.api(), REPO, 1)
        self.assertEqual(blocked.scope_decision, "enforce")
        self.assertFalse(blocked.ok)
        # Scope audit evidence stays separate from report failure reasons.
        self.assertIn("legacy enforce-all", blocked.scope_reason)
        self.assertEqual(
            blocked.to_json().get("scope_reason"),
            blocked.scope_reason,
        )
        self.assertEqual(blocked.config_revision, DEFAULT_TIP)
        self.assertEqual(blocked.config_fingerprint, "missing")

    def test_absent_config_main_is_built_in_exclude(self) -> None:
        fake = FakeAPI()
        fake.pull["base"]["ref"] = "main"
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.scope_decision, "exclude")
        self.assertEqual(
            result.scope_reason, "target branch 'main' has nothing for A38 to check"
        )
        self.assertTrue(
            any("nothing for A38 to check" in reason for reason in result.reasons)
        )
        self.assertEqual(result.lifecycle, {})
        self.assertFalse(any(w.startswith("comment:") for w in result.writes))
        self.assertFalse(
            any("PR-GUARD:LIFECYCLE" in (c.get("body") or "") for c in fake.comments)
        )
        self.assertFalse(fake.pull["draft"])
        self.assertEqual(result.config_fingerprint, "missing")

    def test_invalid_or_denied_config_cannot_exempt(self) -> None:
        fake = FakeAPI()
        fake.pull["base"]["ref"] = "main"
        fake.set_pr_guard_config({"schema": "pr-guard/v1", "a38": {"default": "exclude"}})
        with self.assertRaisesRegex(GuardError, "pr-guard"):
            assess_pull(fake.api(), REPO, 1)

        fake = FakeAPI()
        fake.pull["base"]["ref"] = "main"
        fake.set_pr_guard_config(
            _pr_guard_config(enforce=["main"], exclude=["main"], default="enforce")
        )
        with self.assertRaisesRegex(GuardError, "overlap"):
            assess_pull(fake.api(), REPO, 1)

        fake = FakeAPI()
        fake.pull["base"]["ref"] = "main"
        fake.set_pr_guard_config(_pr_guard_config(exclude=["main"]))
        fake.denied_prefixes.append(f"/repos/{REPO}/contents/.github/pr-guard.json")
        with self.assertRaisesRegex(GuardError, "denied"):
            assess_pull(fake.api(), REPO, 1)

        fake = FakeAPI()
        fake.pull["base"]["ref"] = "main"
        fake.denied_prefixes.append(f"/repos/{REPO}/commits/")
        with self.assertRaisesRegex(GuardError, "denied"):
            assess_pull(fake.api(), REPO, 1)

    def test_head_config_cannot_self_exempt(self) -> None:
        fake = FakeAPI()
        # Trusted tip has no config (legacy enforce). Head proposes exclude-all.
        fake.files[(HEAD, ".github/pr-guard.json")] = json.dumps(
            _pr_guard_config(exclude=["develop"], default="exclude")
        ).encode()
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=202
        )
        # Changing the file vs absent base requires migration approval.
        result = assess_pull(fake.api(), REPO, 1)
        self.assertEqual(result.scope_decision, "enforce")
        self.assertFalse(result.ok)
        self.assertTrue(any("pr-guard.json" in reason for reason in result.reasons))

        # Even with approval, proposed head config is not activated for scope.
        fake.permissions["maintainer"] = {
            "permission": "write",
            "user": {"id": 3030},
        }
        fake.reviews = [
            {
                "id": 100,
                "user": {"id": 3030, "login": "maintainer"},
                "state": "APPROVED",
                "commit_id": HEAD,
                "submitted_at": "2026-09-05T13:00:00Z",
                "body": f"{a38_guard.POLICY_APPROVAL_PREFIX} head={HEAD} base={BASE}",
            }
        ]
        fake.files[(HEAD, ".github/a38.json")] = fake.files[(BASE, ".github/a38.json")]
        passed = assess_pull(fake.api(), REPO, 1)
        self.assertTrue(passed.ok)
        self.assertEqual(passed.status, "pass")
        self.assertEqual(passed.scope_decision, "enforce")

    def test_enforced_target_still_requires_report_and_accepts_evidence(self) -> None:
        fake = FakeAPI()
        fake.set_pr_guard_config(
            _pr_guard_config(enforce=["develop"], exclude=["main"], default="enforce")
        )
        blocked = assess_pull(fake.api(), REPO, 1)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "fail")
        self.assertEqual(blocked.scope_decision, "enforce")
        self.assertEqual(blocked.trusted_default_branch, "develop")
        self.assertTrue(any("no author" in reason for reason in blocked.reasons))

        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=201
        )
        passed = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(passed.ok)
        self.assertEqual(passed.status, "pass")
        self.assertEqual(passed.config_revision, DEFAULT_TIP)
        self.assertTrue(any(w.startswith("comment:") for w in passed.writes))
        self.assertIn(status_context_enforce("develop"), [s["context"] for s in fake.statuses])

    def test_open_missing_or_invalid_default_branch_fails_closed(self) -> None:
        for raw in (None, "", 0, "..", "main.lock", "a" * 76):
            fake = FakeAPI()
            fake.pull["base"]["repo"]["default_branch"] = raw
            with self.assertRaisesRegex(GuardError, "default_branch"):
                assess_pull(fake.api(), REPO, 1)

    def test_closed_missing_default_branch_remains_noop_before_config_lookup(self) -> None:
        fake = FakeAPI()
        fake.pull = fake._pull(HEAD, BASE, state="closed")
        del fake.pull["base"]["repo"]["default_branch"]
        # Commits lookup would fail; closed path must not reach it.
        fake.ref_commits.clear()
        result = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "closed")
        self.assertTrue(result.closed)
        self.assertIn("skipped:closed", result.writes)
        self.assertEqual(fake.writes, [])

    def test_head_fork_metadata_cannot_affect_trusted_config_source(self) -> None:
        fake = FakeAPI()
        self._exclude_release_config(fake)
        fork = "contributor/fork"
        fake.pull["head"]["repo"] = {
            "full_name": fork,
            "default_branch": "main",
        }
        fake.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=203
        )
        result = assess_pull(fake.api(), REPO, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.trusted_default_branch, "develop")
        self.assertEqual(result.head_repo, fork)

        fake.pull["base"]["ref"] = "main"
        out = assess_pull(fake.api(), REPO, 1)
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "not_applicable")
        self.assertEqual(out.trusted_default_branch, "develop")

    def test_retarget_default_or_config_change_rejects_stale_publish(self) -> None:
        fake = FakeAPI()
        self._release_pull(fake)
        api = fake.api()
        assessment = assess_pull(api, REPO, 1)
        self.assertEqual(assessment.status, "not_applicable")

        fake.pull["base"]["ref"] = "develop"
        with self.assertRaisesRegex(GuardError, "changed before publish"):
            publish_assessment(api, assessment)

        fake.pull["base"]["ref"] = "main"
        assessment = assess_pull(api, REPO, 1)
        fake.pull["base"]["repo"]["default_branch"] = "main"
        with self.assertRaisesRegex(GuardError, "changed before publish"):
            publish_assessment(api, assessment)

        fake.pull["base"]["repo"]["default_branch"] = "develop"
        assessment = assess_pull(api, REPO, 1)
        # Config revision moves before success status.
        fake.ref_commits["develop"] = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        fake.files[
            ("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", ".github/pr-guard.json")
        ] = fake.files[(DEFAULT_TIP, ".github/pr-guard.json")]
        with self.assertRaisesRegex(GuardError, "configuration changed before publish"):
            publish_assessment(api, assessment)

        fake.ref_commits["develop"] = DEFAULT_TIP
        assessment = assess_pull(api, REPO, 1)
        # Adversarial inconsistent API response: same immutable SHA, different
        # bytes, via a fresh client. GitHub commit content cannot mutate; the
        # existing immutable cache on `api` correctly returns prior bytes and
        # must not be disabled to satisfy an impossible same-client fixture.
        fake.set_pr_guard_config(
            _pr_guard_config(enforce=["main"], exclude=[], default="enforce")
        )
        adversarial_api = fake.api()
        with self.assertRaisesRegex(GuardError, "configuration changed before publish"):
            publish_assessment(adversarial_api, assessment)

    def test_not_applicable_dry_run_all_open_and_event_routes(self) -> None:
        fake = FakeAPI()
        self._release_pull(fake)
        dry = reconcile_pull(fake.api(), REPO, 1, dry_run=True, publish=True)
        self.assertEqual(dry.status, "not_applicable")
        self.assertIn("dry-run", dry.writes)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.statuses, [])

        fake.open_pulls = [1]
        code = main(
            ["reconcile", "--repo", REPO, "--all-open", "--json"],
            env={"GH_TOKEN": "fake"},
            api=fake.api(),
        )
        self.assertEqual(code, 0)
        self.assertTrue(
            any(
                s["context"] == status_context_enforce("main") and s["state"] == "success"
                for s in fake.statuses
            )
        )
        self.assertFalse(any(w.startswith("comment:") for w in fake.writes))

        fake2 = FakeAPI()
        self._release_pull(fake2)
        payload = {
            "action": "synchronize",
            "pull_request": {"number": 1},
            "repository": {"full_name": REPO},
        }
        event = reconcile_event(
            fake2.api(),
            event_name="pull_request_target",
            payload=payload,
            dry_run=False,
            publish=True,
        )
        assert isinstance(event, a38_guard.Assessment)
        self.assertEqual(event.status, "not_applicable")
        self.assertTrue(
            any(
                s["context"] == status_context_enforce("main") and s["state"] == "success"
                for s in fake2.statuses
            )
        )
        self.assertFalse(any(w.startswith("comment:") for w in fake2.writes))

    def test_not_applicable_status_is_deduplicated(self) -> None:
        fake = FakeAPI()
        self._release_pull(fake)
        first = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertEqual(first.status, "not_applicable")
        writes_after_first = list(fake.writes)
        second = reconcile_pull(fake.api(), REPO, 1, publish=True)
        self.assertEqual(second.status, "not_applicable")
        self.assertTrue(any("unchanged" in w for w in second.writes))
        self.assertEqual(fake.writes, writes_after_first)

    def test_schema_rejects_unknown_keys_duplicates_and_globs(self) -> None:
        from agent_cli.pr_guard_config import PrGuardConfigError, load_pr_guard_config

        with self.assertRaises(PrGuardConfigError):
            load_pr_guard_config(
                '{"schema":"pr-guard/v1","a38":{"enforce":[],"exclude":[],'
                '"default":"enforce","extra":1}}'
            )
        with self.assertRaises(PrGuardConfigError):
            load_pr_guard_config(
                '{"schema":"pr-guard/v1","schema":"pr-guard/v1",'
                '"a38":{"enforce":[],"exclude":[],"default":"enforce"}}'
            )
        with self.assertRaises(PrGuardConfigError):
            load_pr_guard_config(
                '{"schema":"pr-guard/v1","a38":{"enforce":["feat/*"],'
                '"exclude":[],"default":"enforce"}}'
            )
        with self.assertRaises(PrGuardConfigError):
            load_pr_guard_config(
                '{"schema":"pr-guard/v1","a38":{"enforce":["develop","develop"],'
                '"exclude":[],"default":"enforce"}}'
            )

    def test_readme_only_omit_rejected_without_independent_files(self) -> None:
        fake = FakeAPI()
        fake.files[(BASE, ".github/a38.json")] = json.dumps(
            _policy(readme_only={"omit_jobs": ["pytest"]})
        ).encode()
        fake.add_author_report(
            _report_comment(
                result="not_applicable",
                exit_code=0,
                readme_only=True,
                duration_s=0.0,
            ),
            updated_at="2026-09-05T12:00:00Z",
            cid=301,
        )
        fake.pull_files = []
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertTrue(
            any("not independently confirmed" in r for r in result.reasons),
            msg=result.reasons,
        )

    def test_readme_only_omit_accepted_when_files_confirm(self) -> None:
        fake = FakeAPI()
        fake.files[(BASE, ".github/a38.json")] = json.dumps(
            _policy(readme_only={"omit_jobs": ["pytest"]})
        ).encode()
        fake.add_author_report(
            _report_comment(
                result="not_applicable",
                exit_code=0,
                readme_only=True,
                duration_s=0.0,
            ),
            updated_at="2026-09-05T12:00:00Z",
            cid=302,
        )
        fake.pull_files = [{"filename": "README.md", "status": "modified"}]
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertTrue(result.ok, msg=result.reasons)
        self.assertEqual(result.status, "pass")

    def test_markdown_only_waives_author_report(self) -> None:
        fake = FakeAPI()
        fake.pull_files = [{"filename": "docs/guide.md", "status": "modified"}]
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertTrue(result.ok, msg=result.reasons)
        self.assertEqual(result.status, "pass")
        self.assertTrue(result.write_ready)
        self.assertEqual(result.write_ready_reason, "markdown-only change set")
        self.assertIn("optional for this markdown-only waiver", result.comment_body)

    def test_empty_pull_files_do_not_waive_for_markdown(self) -> None:
        fake = FakeAPI()
        fake.pull_files = []
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertFalse(result.write_ready)
        self.assertTrue(
            any("no author local-CI report" in r for r in result.reasons),
            msg=result.reasons,
        )

    def test_mixed_files_do_not_waive_for_markdown(self) -> None:
        fake = FakeAPI()
        fake.pull_files = [
            {"filename": "docs/guide.md", "status": "modified"},
            {"filename": "app.py", "status": "modified"},
        ]
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertFalse(result.write_ready)

    def test_markdown_only_does_not_waive_policy_failure(self) -> None:
        fake = FakeAPI()
        fake.pull_files = [{"filename": "docs/guide.md", "status": "modified"}]
        fake.files[(BASE, ".github/a38.json")] = b'{"schema":"nope"}'
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertFalse(result.ok)
        self.assertTrue(
            result.status in {"not_configured", "invalid_policy", "fail"}
            or any("maintainer config" in r for r in result.reasons),
            msg=(result.status, result.reasons),
        )

    def test_markdown_only_omit_rejected_without_independent_files(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(
                result="not_applicable",
                exit_code=0,
                markdown_only=True,
                duration_s=0.0,
            ),
            updated_at="2026-09-05T12:00:00Z",
            cid=303,
        )
        fake.pull_files = []
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "fail")
        self.assertTrue(
            any(
                "markdown-only omission is not independently confirmed" in r
                for r in result.reasons
            ),
            msg=result.reasons,
        )

    def test_markdown_only_omit_accepted_when_files_confirm(self) -> None:
        fake = FakeAPI()
        fake.add_author_report(
            _report_comment(
                result="not_applicable",
                exit_code=0,
                markdown_only=True,
                duration_s=0.0,
            ),
            updated_at="2026-09-05T12:00:00Z",
            cid=304,
        )
        fake.pull_files = [{"filename": "docs/guide.md", "status": "modified"}]
        result = assess_pull(fake.api(), REPO, 1, dry_run=True)
        self.assertTrue(result.ok, msg=result.reasons)
        self.assertEqual(result.status, "pass")


if __name__ == "__main__":
    unittest.main()
