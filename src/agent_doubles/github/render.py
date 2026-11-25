"""The JSON forms of the double's answers, with the key sets of real GitHub answers.

Every object carries exactly the keys GitHub sends (checked against the recorded shapes in
``tests/platform/doubles/github/fixtures/``); values that the double does not model are fixed neutral values.
API URLs point at the double (``Urls.api``), web URLs at ``Urls.web`` (the same server unless configured otherwise),
so a client that follows ``url`` fields or ``Link`` headers stays on the double.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from agent_doubles.github.objects import TREE_MODE, Commit, Signature, api_mode
from agent_doubles.github.repos import (
    CheckRun,
    CommitStatus,
    FileChange,
    Installation,
    Issue,
    Pull,
    Repo,
    Review,
    SigningKey,
    User,
    Verification,
    World,
    diff_trees,
    iso,
)

DOCS = "https://docs.github.com/rest"
PULL_LIST_OMITS = frozenset(
    {
        "additions",
        "changed_files",
        "comments",
        "commits",
        "deletions",
        "maintainer_can_modify",
        "mergeable",
        "mergeable_state",
        "merged",
        "merged_by",
        "rebaseable",
        "review_comments",
    }
)
PULL_REPO_OMITS = frozenset({"network_count", "subscribers_count", "temp_clone_token", "permissions"})


@dataclass(frozen=True)
class Urls:
    """Base URLs the double writes into its answers."""

    api: str
    web: str

    @property
    def host(self) -> str:
        return urlsplit(self.web).netloc


def node_id(kind: str, value: object) -> str:
    """A deterministic GraphQL node id."""
    return base64.b64encode(f"{kind}:{value}".encode()).decode()


def error_body(status: int, message: str, *, docs: str = DOCS, **extra: Any) -> dict[str, Any]:
    """A REST error answer (``message``, ``documentation_url``, ``status`` as a string)."""
    return {"message": message, **extra, "documentation_url": docs, "status": str(status)}


def validation_error(message: str, resource: str, field: str | None = None, code: str = "custom") -> dict[str, Any]:
    """``422 Validation Failed`` with one error item."""
    item: dict[str, Any] = {"resource": resource, "code": code}
    if field is not None:
        item["field"] = field
    if code == "custom":
        item["message"] = message
    return error_body(422, "Validation Failed", errors=[item])


# --- users -------------------------------------------------------------------------------------------


def simple_user(urls: Urls, login: str, user_id: int, kind: str = "User") -> dict[str, Any]:
    api = f"{urls.api}/users/{login}"
    return {
        "login": login,
        "id": user_id,
        "node_id": node_id(kind, user_id),
        "avatar_url": f"{urls.web}/avatars/u/{user_id}?v=4",
        "gravatar_id": "",
        "url": api,
        "html_url": f"{urls.web}/{login}",
        "followers_url": f"{api}/followers",
        "following_url": f"{api}/following{{/other_user}}",
        "gists_url": f"{api}/gists{{/gist_id}}",
        "starred_url": f"{api}/starred{{/owner}}{{/repo}}",
        "subscriptions_url": f"{api}/subscriptions",
        "organizations_url": f"{api}/orgs",
        "repos_url": f"{api}/repos",
        "events_url": f"{api}/events{{/privacy}}",
        "received_events_url": f"{api}/received_events",
        "type": kind,
        "user_view_type": "public",
        "site_admin": False,
    }


def login_user(urls: Urls, world: World, login: str) -> dict[str, Any]:
    """The simple user of a seeded login."""
    user = world.users[login.lower()]
    return simple_user(urls, user.login, user.id)


def authenticated_user(urls: Urls, user: User, created: int) -> dict[str, Any]:
    """``GET /user``."""
    return {
        **simple_user(urls, user.login, user.id),
        "name": user.name,
        "company": None,
        "blog": "",
        "location": None,
        "email": user.email,
        "hireable": None,
        "bio": None,
        "twitter_username": None,
        "notification_email": user.email,
        "public_repos": 0,
        "public_gists": 0,
        "followers": 0,
        "following": 0,
        "created_at": iso(created),
        "updated_at": iso(created),
    }


def ssh_signing_key(key: SigningKey) -> dict[str, Any]:
    """An SSH signing key of ``GET``/``POST /user/ssh_signing_keys``."""
    return {"key": key.line, "id": key.id, "title": key.title, "created_at": iso(key.created)}


# --- repositories ------------------------------------------------------------------------------------


def repository(urls: Urls, repo: Repo, *, form: str = "full", push: bool = True) -> dict[str, Any]:
    """A repository; `form` ``full`` (``GET /repos/…``), ``installation`` (installation listing) or ``pull`` (inside
    ``head``/``base`` of a pull request)."""
    api = f"{urls.api}/repos/{repo.full_name}"
    web = f"{urls.web}/{repo.full_name}"
    stamp = iso(repo.created)
    data: dict[str, Any] = {
        "id": repo.id,
        "node_id": node_id("Repository", repo.id),
        "name": repo.name,
        "full_name": repo.full_name,
        "private": repo.private,
        "owner": simple_user(urls, repo.owner, repo.owner_id, repo.owner_type),
        "html_url": web,
        "description": repo.description,
        "fork": False,
        "url": api,
        "forks_url": f"{api}/forks",
        "keys_url": f"{api}/keys{{/key_id}}",
        "collaborators_url": f"{api}/collaborators{{/collaborator}}",
        "teams_url": f"{api}/teams",
        "hooks_url": f"{api}/hooks",
        "issue_events_url": f"{api}/issues/events{{/number}}",
        "events_url": f"{api}/events",
        "assignees_url": f"{api}/assignees{{/user}}",
        "branches_url": f"{api}/branches{{/branch}}",
        "tags_url": f"{api}/tags",
        "blobs_url": f"{api}/git/blobs{{/sha}}",
        "git_tags_url": f"{api}/git/tags{{/sha}}",
        "git_refs_url": f"{api}/git/refs{{/sha}}",
        "trees_url": f"{api}/git/trees{{/sha}}",
        "statuses_url": f"{api}/statuses/{{sha}}",
        "languages_url": f"{api}/languages",
        "stargazers_url": f"{api}/stargazers",
        "contributors_url": f"{api}/contributors",
        "subscribers_url": f"{api}/subscribers",
        "subscription_url": f"{api}/subscription",
        "commits_url": f"{api}/commits{{/sha}}",
        "git_commits_url": f"{api}/git/commits{{/sha}}",
        "comments_url": f"{api}/comments{{/number}}",
        "issue_comment_url": f"{api}/issues/comments{{/number}}",
        "contents_url": f"{api}/contents/{{+path}}",
        "compare_url": f"{api}/compare/{{base}}...{{head}}",
        "merges_url": f"{api}/merges",
        "archive_url": f"{api}/{{archive_format}}{{/ref}}",
        "downloads_url": f"{api}/downloads",
        "issues_url": f"{api}/issues{{/number}}",
        "pulls_url": f"{api}/pulls{{/number}}",
        "milestones_url": f"{api}/milestones{{/number}}",
        "notifications_url": f"{api}/notifications{{?since,all,participating}}",
        "labels_url": f"{api}/labels{{/name}}",
        "releases_url": f"{api}/releases{{/id}}",
        "deployments_url": f"{api}/deployments",
        "created_at": stamp,
        "updated_at": stamp,
        "pushed_at": stamp,
        "git_url": f"git://{urls.host}/{repo.full_name}.git",
        "ssh_url": f"git@{urls.host}:{repo.full_name}.git",
        "clone_url": f"{web}.git",
        "svn_url": web,
        "homepage": None,
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "language": None,
        "has_issues": repo.has_issues,
        "has_projects": True,
        "has_downloads": True,
        "has_wiki": True,
        "has_pages": False,
        "has_discussions": False,
        "forks_count": 0,
        "mirror_url": None,
        "archived": False,
        "disabled": False,
        "open_issues_count": repo.open_items,
        "license": None,
        "allow_forking": True,
        "is_template": False,
        "web_commit_signoff_required": False,
        "has_pull_requests": True,
        "pull_request_creation_policy": "all",
        "topics": [],
        "visibility": "private" if repo.private else "public",
        "forks": 0,
        "open_issues": repo.open_items,
        "watchers": 0,
        "default_branch": repo.default_branch,
        "permissions": {"admin": False, "maintain": False, "push": push, "triage": push, "pull": True},
        "temp_clone_token": "",
        "network_count": 0,
        "subscribers_count": 0,
    }
    if form == "full":
        return data
    omitted = PULL_REPO_OMITS if form == "pull" else PULL_REPO_OMITS - {"permissions"}
    return {key: value for key, value in data.items() if key not in omitted}


def installation(urls: Urls, world: World, inst: Installation) -> dict[str, Any]:
    """An entry of ``GET /user/installations``."""
    stamp = iso(inst.created)
    kind = inst.account_type
    path = f"organizations/{inst.account}/settings" if kind == "Organization" else "settings"
    return {
        "id": inst.id,
        "client_id": "Iv1.0000double0000",
        "account": simple_user(urls, inst.account, inst.account_id, kind),
        "repository_selection": inst.selection,
        "access_tokens_url": f"{urls.api}/app/installations/{inst.id}/access_tokens",
        "repositories_url": f"{urls.api}/installation/repositories",
        "html_url": f"{urls.web}/{path}/installations/{inst.id}",
        "app_id": 1,
        "app_slug": "agent-platform-double",
        "target_id": inst.account_id,
        "target_type": kind,
        "permissions": dict(sorted(inst.permissions.items())),
        "events": [],
        "created_at": stamp,
        "updated_at": stamp,
        "single_file_name": None,
        "has_multiple_single_files": False,
        "single_file_paths": [],
        "suspended_by": None,
        "suspended_at": None,
    }


# --- git data ----------------------------------------------------------------------------------------


def ref(urls: Urls, repo: Repo, name: str, sha: str) -> dict[str, Any]:
    api = f"{urls.api}/repos/{repo.full_name}"
    return {
        "ref": name,
        "node_id": node_id("Ref", f"{repo.id}:{name}"),
        "url": f"{api}/git/{name}",
        "object": {"sha": sha, "type": "commit", "url": f"{api}/git/commits/{sha}"},
    }


def tree(urls: Urls, repo: Repo, sha: str, recursive: bool) -> dict[str, Any]:
    """``GET``/``POST …/git/trees``: the entries (all levels when `recursive`)."""
    api = f"{urls.api}/repos/{repo.full_name}"
    objects = repo.objects
    listed = objects.walk(sha) if recursive else [(entry.name, entry) for entry in objects.tree(sha)]
    entries = []
    for path, entry in listed:
        item: dict[str, Any] = {"path": path, "mode": api_mode(entry.mode), "type": entry.kind, "sha": entry.sha}
        if entry.mode == TREE_MODE:
            item["url"] = f"{api}/git/trees/{entry.sha}"
        else:
            item["size"] = len(objects.blob(entry.sha))
            item["url"] = f"{api}/git/blobs/{entry.sha}"
        entries.append(item)
    return {"sha": sha, "url": f"{api}/git/trees/{sha}", "tree": entries, "truncated": False}


def blob(urls: Urls, repo: Repo, sha: str) -> dict[str, Any]:
    """``GET …/git/blobs/{sha}`` (content base64 with line breaks every 60 characters, like GitHub)."""
    data = repo.objects.blob(sha)
    encoded = base64.b64encode(data).decode()
    content = "".join(encoded[i : i + 60] + "\n" for i in range(0, len(encoded), 60))
    return {
        "sha": sha,
        "node_id": node_id("Blob", f"{repo.id}:{sha}"),
        "size": len(data),
        "url": f"{urls.api}/repos/{repo.full_name}/git/blobs/{sha}",
        "content": content,
        "encoding": "base64",
    }


def blob_created(urls: Urls, repo: Repo, sha: str) -> dict[str, Any]:
    return {"url": f"{urls.api}/repos/{repo.full_name}/git/blobs/{sha}", "sha": sha}


def _signature(signature: Signature) -> dict[str, Any]:
    return {"name": signature.name, "email": signature.email, "date": iso(signature.time)}


def verification(found: Verification) -> dict[str, Any]:
    """The ``verification`` object of a commit."""
    return {
        "verified": found.verified,
        "reason": found.reason,
        "signature": found.signature,
        "payload": found.payload,
        "verified_at": iso(found.verified_at) if found.verified_at is not None else None,
    }


def git_commit(urls: Urls, repo: Repo, sha: str) -> dict[str, Any]:
    """``GET``/``POST …/git/commits``."""
    api = f"{urls.api}/repos/{repo.full_name}"
    commit = repo.objects.commit(sha)
    return {
        "sha": sha,
        "node_id": node_id("Commit", f"{repo.id}:{sha}"),
        "url": f"{api}/git/commits/{sha}",
        "html_url": f"{urls.web}/{repo.full_name}/commit/{sha}",
        "author": _signature(commit.author),
        "committer": _signature(commit.committer),
        "tree": {"sha": commit.tree, "url": f"{api}/git/trees/{commit.tree}"},
        "message": commit.message,
        "parents": [
            {"sha": p, "url": f"{api}/git/commits/{p}", "html_url": f"{urls.web}/{repo.full_name}/commit/{p}"}
            for p in commit.parents
        ],
        "verification": verification(repo.verification(sha)),
    }


def _account(urls: Urls, world: World, signature: Signature) -> dict[str, Any] | None:
    user = world.user_for_email(signature.email)
    return simple_user(urls, user.login, user.id) if user is not None else None


def commit_summary(urls: Urls, world: World, repo: Repo, sha: str) -> dict[str, Any]:
    """A commit as listed by compare (the REST commit without ``stats`` and ``files``)."""
    api = f"{urls.api}/repos/{repo.full_name}"
    commit: Commit = repo.objects.commit(sha)
    return {
        "sha": sha,
        "node_id": node_id("Commit", f"{repo.id}:{sha}"),
        "commit": {
            "author": _signature(commit.author),
            "committer": _signature(commit.committer),
            "message": commit.message,
            "tree": {"sha": commit.tree, "url": f"{api}/git/trees/{commit.tree}"},
            "url": f"{api}/git/commits/{sha}",
            "comment_count": 0,
            "verification": verification(repo.verification(sha)),
        },
        "url": f"{api}/commits/{sha}",
        "html_url": f"{urls.web}/{repo.full_name}/commit/{sha}",
        "comments_url": f"{api}/commits/{sha}/comments",
        "author": _account(urls, world, commit.author),
        "committer": _account(urls, world, commit.committer),
        "parents": [
            {"sha": p, "url": f"{api}/commits/{p}", "html_url": f"{urls.web}/{repo.full_name}/commit/{p}"}
            for p in commit.parents
        ],
    }


def file_change(urls: Urls, repo: Repo, change: FileChange, commit: str) -> dict[str, Any]:
    """A changed file of a commit, a compare or a pull request; binary files carry no ``patch``."""
    item: dict[str, Any] = {
        "sha": change.sha,
        "filename": change.path,
        "status": change.status,
        "additions": change.additions,
        "deletions": change.deletions,
        "changes": change.additions + change.deletions,
        "blob_url": f"{urls.web}/{repo.full_name}/blob/{commit}/{change.path}",
        "raw_url": f"{urls.web}/{repo.full_name}/raw/{commit}/{change.path}",
        "contents_url": f"{urls.api}/repos/{repo.full_name}/contents/{change.path}?ref={commit}",
    }
    if change.patch is not None:
        item["patch"] = change.patch
    return item


def _changes(repo: Repo, old_commit: str | None, new_commit: str) -> list[FileChange]:
    objects = repo.objects
    old_tree = objects.commit(old_commit).tree if old_commit else objects.put_tree([])
    return diff_trees(objects, old_tree, objects.commit(new_commit).tree)


def commit(urls: Urls, world: World, repo: Repo, sha: str) -> dict[str, Any]:
    """``GET …/commits/{ref}``: the commit with ``stats`` and ``files`` against its first parent."""
    parents = repo.objects.commit(sha).parents
    changes = _changes(repo, parents[0] if parents else None, sha)
    additions = sum(change.additions for change in changes)
    deletions = sum(change.deletions for change in changes)
    return {
        **commit_summary(urls, world, repo, sha),
        "stats": {"total": additions + deletions, "additions": additions, "deletions": deletions},
        "files": [file_change(urls, repo, change, sha) for change in changes],
    }


def compare(urls: Urls, world: World, repo: Repo, *, base: str, head: str, basehead: str) -> dict[str, Any]:
    """``GET …/compare/{base}...{head}`` for two resolved commits (`basehead` is the text of the request)."""
    merge_base = repo.merge_base(base, head)
    base_set = repo.ancestors(base)
    head_set = repo.ancestors(head)
    ahead = repo.history(head, base_set)
    behind = len(base_set - head_set)
    if base == head:
        status = "identical"
    elif not behind:
        status = "ahead"
    elif not ahead:
        status = "behind"
    else:
        status = "diverged"
    changes = _changes(repo, merge_base, head)
    api = f"{urls.api}/repos/{repo.full_name}"
    web = f"{urls.web}/{repo.full_name}"
    return {
        "url": f"{api}/compare/{basehead}",
        "html_url": f"{web}/compare/{basehead}",
        "permalink_url": f"{web}/compare/{repo.owner}:{base[:7]}...{repo.owner}:{head[:7]}",
        "diff_url": f"{web}/compare/{basehead}.diff",
        "patch_url": f"{web}/compare/{basehead}.patch",
        "base_commit": commit_summary(urls, world, repo, base),
        "merge_base_commit": commit_summary(urls, world, repo, merge_base) if merge_base else None,
        "status": status,
        "ahead_by": len(ahead),
        "behind_by": behind,
        "total_commits": len(ahead),
        "commits": [commit_summary(urls, world, repo, sha) for sha in ahead],
        "files": [file_change(urls, repo, change, head) for change in changes],
    }


# --- pull requests and comments ----------------------------------------------------------------------


def pull_changes(repo: Repo, pull: Pull) -> tuple[str, str, list[FileChange], int]:
    """Head sha, base sha, changed files (merge base → head) and commit count of a pull request."""
    head = repo.branch(pull.head) or ""
    base = repo.branch(pull.base) or ""
    merge_base = repo.merge_base(base, head) if head and base else None
    changes = _changes(repo, merge_base, head) if head else []
    commits = len(repo.history(head, repo.ancestors(base))) if head and base else 0
    return head, base, changes, commits


def mergeability(repo: Repo, item: Pull, known: bool) -> tuple[bool | None, str]:
    """``mergeable`` and ``mergeable_state`` of a pull request: unknown (``null``, ``"unknown"``) while not computed
    and for a closed one; else whether the base merges into the head cleanly, with the state ``draft`` for a draft
    (whatever ``mergeable`` is), ``dirty`` for a conflict and ``clean`` otherwise (`agent_doubles.github.repos`)."""
    if item.state == "closed" or not known:
        return None, "unknown"
    clean = repo.merges_cleanly(repo.branch(item.base) or "", repo.branch(item.head) or "")
    return clean, "draft" if item.draft else "clean" if clean else "dirty"


def pull(
    urls: Urls, world: World, repo: Repo, item: Pull, *, listed: bool = False, known: bool = False
) -> dict[str, Any]:
    """A pull request (`listed`: the shorter form of ``GET …/pulls``; `known`: its mergeability is computed)."""
    api = f"{urls.api}/repos/{repo.full_name}"
    mergeable, mergeable_state = mergeability(repo, item, known)
    web = f"{urls.web}/{repo.full_name}"
    number = item.number
    head, base, changes, commits = pull_changes(repo, item)
    stamp = iso(item.created)
    owner = simple_user(urls, repo.owner, repo.owner_id, repo.owner_type)
    data: dict[str, Any] = {
        "url": f"{api}/pulls/{number}",
        "id": item.id,
        "node_id": node_id("PullRequest", item.id),
        "html_url": f"{web}/pull/{number}",
        "diff_url": f"{web}/pull/{number}.diff",
        "patch_url": f"{web}/pull/{number}.patch",
        "issue_url": f"{api}/issues/{number}",
        "commits_url": f"{api}/pulls/{number}/commits",
        "review_comments_url": f"{api}/pulls/{number}/comments",
        "review_comment_url": f"{api}/pulls/comments{{/number}}",
        "comments_url": f"{api}/issues/{number}/comments",
        "statuses_url": f"{api}/statuses/{head}",
        "number": number,
        "state": item.state,
        "locked": False,
        "title": item.title,
        "user": login_user(urls, world, item.user),
        "body": item.body,
        "labels": [label(urls, repo, name) for name in item.labels],
        "milestone": None,
        "active_lock_reason": None,
        "created_at": stamp,
        "updated_at": iso(item.updated),
        "closed_at": iso(item.closed) if item.closed is not None else None,
        "merged_at": None if item.merged_at is None else iso(item.merged_at),
        "merge_commit_sha": item.merged_sha,
        "assignee": login_user(urls, world, item.assignees[0]) if item.assignees else None,
        "assignees": [login_user(urls, world, login) for login in item.assignees],
        "requested_reviewers": [],
        "requested_teams": [],
        "head": {
            "label": f"{repo.owner}:{item.head}",
            "ref": item.head,
            "sha": head,
            "user": owner,
            "repo": repository(urls, repo, form="pull"),
        },
        "base": {
            "label": f"{repo.owner}:{item.base}",
            "ref": item.base,
            "sha": base,
            "user": owner,
            "repo": repository(urls, repo, form="pull"),
        },
        "_links": {
            "self": {"href": f"{api}/pulls/{number}"},
            "html": {"href": f"{web}/pull/{number}"},
            "issue": {"href": f"{api}/issues/{number}"},
            "comments": {"href": f"{api}/issues/{number}/comments"},
            "review_comments": {"href": f"{api}/pulls/{number}/comments"},
            "review_comment": {"href": f"{api}/pulls/comments{{/number}}"},
            "commits": {"href": f"{api}/pulls/{number}/commits"},
            "statuses": {"href": f"{api}/statuses/{head}"},
        },
        "author_association": _association(repo, item.user),
        "auto_merge": None,
        "draft": item.draft,
        "merged": item.merged_sha is not None,
        "mergeable": mergeable,
        "rebaseable": mergeable,
        "mergeable_state": mergeable_state,
        "merged_by": None,
        "comments": repo.comment_count(number),
        "review_comments": 0,
        "maintainer_can_modify": False,
        "commits": commits,
        "additions": sum(change.additions for change in changes),
        "deletions": sum(change.deletions for change in changes),
        "changed_files": len(changes),
    }
    if listed:
        return {key: value for key, value in data.items() if key not in PULL_LIST_OMITS}
    return data


def comment(urls: Urls, world: World, repo: Repo, item: Any) -> dict[str, Any]:
    """An issue comment."""
    api = f"{urls.api}/repos/{repo.full_name}"
    stamp = iso(item.created)
    url = f"{api}/issues/comments/{item.id}"
    kind = "pull" if item.number in repo.pulls else "issues"
    return {
        "url": url,
        "html_url": f"{urls.web}/{repo.full_name}/{kind}/{item.number}#issuecomment-{item.id}",
        "issue_url": f"{api}/issues/{item.number}",
        "id": item.id,
        "node_id": node_id("IssueComment", item.id),
        "user": login_user(urls, world, item.user),
        "created_at": stamp,
        "updated_at": stamp,
        "body": item.body,
        "author_association": _association(repo, item.user),
        "reactions": _reactions(url),
        "performed_via_github_app": None,
        "minimized": None,
    }


def _association(repo: Repo, login: str) -> str:
    return "OWNER" if login.lower() == repo.owner.lower() else "COLLABORATOR"


def _reactions(url: str) -> dict[str, Any]:
    return {
        "url": f"{url}/reactions",
        "total_count": 0,
        "+1": 0,
        "-1": 0,
        "laugh": 0,
        "hooray": 0,
        "confused": 0,
        "heart": 0,
        "rocket": 0,
        "eyes": 0,
    }


# --- issues ------------------------------------------------------------------------------------------


def label(urls: Urls, repo: Repo, name: str) -> dict[str, Any]:
    """A label of the repository (`name` as the repository writes it)."""
    found = repo.labels[name.lower()]
    return {
        "id": found.id,
        "node_id": node_id("Label", found.id),
        "url": f"{urls.api}/repos/{repo.full_name}/labels/{quote(found.name, safe='')}",
        "name": found.name,
        "color": found.color,
        "default": False,
        "description": found.description,
    }


# Issue ids of pull requests in issue form: an id space of their own, so the pull request id stays the counter's.
PULL_ISSUE_IDS = 2_000_000_000


def issue(urls: Urls, world: World, repo: Repo, item: Issue | Pull) -> dict[str, Any]:
    """An issue of ``GET``/``POST``/``PATCH …/issues``; a pull request in issue form carries ``pull_request`` (and
    ``draft``) and no ``state_reason``."""
    api = f"{urls.api}/repos/{repo.full_name}"
    web = f"{urls.web}/{repo.full_name}"
    number = item.number
    url = f"{api}/issues/{number}"
    is_pull = isinstance(item, Pull)
    issue_id = PULL_ISSUE_IDS + item.id if is_pull else item.id
    data: dict[str, Any] = {
        "url": url,
        "repository_url": api,
        "labels_url": f"{url}/labels{{/name}}",
        "comments_url": f"{url}/comments",
        "events_url": f"{url}/events",
        "html_url": f"{web}/pull/{number}" if is_pull else f"{web}/issues/{number}",
        "id": issue_id,
        "node_id": node_id("Issue", issue_id),
        "number": number,
        "title": item.title,
        "user": login_user(urls, world, item.user),
        "labels": [label(urls, repo, name) for name in item.labels],
        "state": item.state,
        "locked": False,
        "assignee": login_user(urls, world, item.assignees[0]) if item.assignees else None,
        "assignees": [login_user(urls, world, login) for login in item.assignees],
        "milestone": None,
        "comments": repo.comment_count(number),
        "created_at": iso(item.created),
        "updated_at": iso(item.updated),
        "closed_at": iso(item.closed) if item.closed is not None else None,
        "author_association": _association(repo, item.user),
        "active_lock_reason": None,
        "body": item.body,
        "reactions": _reactions(url),
        "timeline_url": f"{url}/timeline",
        "performed_via_github_app": None,
        "state_reason": item.state_reason,
    }
    if isinstance(item, Pull):
        data["draft"] = item.draft
        data["pull_request"] = {
            "url": f"{api}/pulls/{number}",
            "html_url": f"{web}/pull/{number}",
            "diff_url": f"{web}/pull/{number}.diff",
            "patch_url": f"{web}/pull/{number}.patch",
            "merged_at": None if item.merged_at is None else iso(item.merged_at),
        }
    return data


def review(urls: Urls, world: World, repo: Repo, item: Review) -> dict[str, Any]:
    """A pull request review (``GET …/pulls/{number}/reviews``, KOR-51)."""
    return {
        "id": item.id,
        "node_id": node_id("PullRequestReview", item.id),
        "user": login_user(urls, world, item.user),
        "body": item.body,
        "state": item.state,
        "html_url": f"{urls.web}/{repo.full_name}/pull/{item.number}#pullrequestreview-{item.id}",
        "pull_request_url": f"{urls.api}/repos/{repo.full_name}/pulls/{item.number}",
        "commit_id": item.commit_id,
        "submitted_at": iso(item.submitted),
        "author_association": _association(repo, item.user),
    }


def check_run(urls: Urls, repo: Repo, item: CheckRun) -> dict[str, Any]:
    """A check run (``GET …/commits/{ref}/check-runs``, KOR-51)."""
    return {
        "id": item.id,
        "name": item.name,
        "head_sha": item.head_sha,
        "status": item.status,
        "conclusion": item.conclusion,
        "url": f"{urls.api}/repos/{repo.full_name}/check-runs/{item.id}",
    }


def commit_status(urls: Urls, repo: Repo, item: CommitStatus) -> dict[str, Any]:
    """A commit status (``GET …/commits/{ref}/status``, KOR-51)."""
    return {
        "id": item.id,
        "state": item.state,
        "context": item.context,
        "url": f"{urls.api}/repos/{repo.full_name}/statuses/{item.sha}",
    }
