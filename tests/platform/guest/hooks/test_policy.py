"""U-GST-policy: the PreToolUse rules of 05-runner.md section 5.3 with path resolution of section 4.6 no. 2.

Table-driven against a real directory tree in tmp: the workspace, a stand-in for /etc ("outside"), a foreign session,
the harness directory, symlinks (absolute, relative, chained, dangling, looping), hard links and case variants.
ATK-13..ATK-20, ATK-34 and ATK-35 appear here as unit cases.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_guest import policy
from agent_guest.policy import Decision, Policy, Unresolvable, lexical, resolve
from agent_guest.schema import SchemaSet

SESSION = "01a1ee56-5e00-7003-8f1b-340691d99f03"


class Tree:
    """Paths of the test tree; ``ws`` is the workspace, ``out`` plays /etc, ``other`` a foreign session."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ws = root / "sessions" / SESSION / "work"
        self.out = root / "etc"
        self.other = root / "sessions" / "other" / "work"
        self.harness_dir = root / "harness"
        self.harness = self.harness_dir / f"{SESSION}.md"


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    t = Tree(tmp_path.resolve())
    for directory in (t.ws / "sub" / "deeper", t.out / "dir", t.other, t.harness_dir, t.ws / ".claude"):
        directory.mkdir(parents=True)
    (t.ws / "notes.txt").write_text("n")
    (t.ws / "sub" / "file.txt").write_text("f")
    (t.out / "passwd").write_text("root")
    (t.other / "secret.txt").write_text("s")
    t.harness.write_text("rules")
    (t.harness_dir / "other.md").write_text("rules")
    os.symlink(str(t.out), t.ws / "etc")  # absolute symlink to "/etc"
    os.symlink("../../../etc/passwd", t.ws / "pw")  # relative symlink leaving the workspace
    os.symlink(str(t.other), t.ws / "foreign")  # foreign session
    os.symlink("sub", t.ws / "inner")  # inside
    os.symlink("inner/deeper", t.ws / "chain")  # chain of two links, inside
    os.symlink(str(t.out / "dir"), t.ws / "escape")  # for "..'" after a symlink
    os.symlink(str(t.out / "new.conf"), t.ws / "dangling")  # target does not exist yet
    os.symlink("loop", t.ws / "loop")
    os.symlink(".claude", t.ws / "cfg")  # inside, but to a protected directory
    os.symlink(str(t.harness), t.ws / "harness-link")
    os.link(t.out / "passwd", t.ws / "hard")  # ATK-35: hard link to a file outside
    return t


def make(tree: Tree) -> Policy:
    return Policy(str(tree.ws), str(tree.harness))


DENY_WRITE = ("deny", 5, "write_outside_workspace")
DENY_PROTECTED = ("deny", 5, "write_protected")
DENY_READ = ("deny", 6, "read_outside_workspace")
DENY_PATH = ("deny", 4, "path_unresolvable")
DENY_PATTERN = ("deny", 3, "pattern_outside_workspace")
ALLOW = ("allow", 7, None)

# (id, tool, input built from the tree, expected (decision, rule, reason id))
CASES: list[tuple[str, str, Callable[[Tree], dict[str, Any]], tuple[str, int, str | None]]] = [
    # rule 4/6/7 - reads
    ("read-relative", "Read", lambda t: {"file_path": "notes.txt"}, ALLOW),
    ("read-absolute-inside", "Read", lambda t: {"file_path": str(t.ws / "sub/file.txt")}, ALLOW),
    ("read-workspace-dir", "Glob", lambda t: {"pattern": "*", "path": str(t.ws)}, ALLOW),
    ("glob-default-path", "Glob", lambda t: {"pattern": "**/*.py"}, ALLOW),
    ("grep-default-path", "Grep", lambda t: {"pattern": "../../etc"}, ALLOW),
    ("read-missing-inside", "Read", lambda t: {"file_path": "no/such/file"}, ALLOW),
    ("read-symlink-inside", "Read", lambda t: {"file_path": "inner/file.txt"}, ALLOW),
    ("read-symlink-chain-inside", "Read", lambda t: {"file_path": "chain"}, ALLOW),
    ("read-own-harness", "Read", lambda t: {"file_path": str(t.harness)}, ALLOW),
    ("read-own-harness-via-link", "Read", lambda t: {"file_path": "harness-link"}, DENY_READ),
    ("read-foreign-harness", "Read", lambda t: {"file_path": str(t.harness_dir / "other.md")}, DENY_READ),
    ("read-harness-dir", "Glob", lambda t: {"pattern": "*", "path": str(t.harness_dir)}, DENY_READ),
    ("read-protected-inside", "Read", lambda t: {"file_path": ".claude/settings.json"}, ALLOW),
    ("read-etc", "Read", lambda t: {"file_path": str(t.out / "passwd")}, DENY_READ),
    ("read-dotdot", "Read", lambda t: {"file_path": "../../../etc/passwd"}, DENY_READ),
    ("read-dotdot-absolute", "Read", lambda t: {"file_path": f"{t.ws}/sub/../../work2"}, DENY_READ),
    ("read-double-slash", "Read", lambda t: {"file_path": "/" + str(t.out / "passwd")}, DENY_READ),
    ("read-upper-case-etc", "Read", lambda t: {"file_path": str(t.root / "ETC" / "passwd")}, DENY_READ),
    ("read-trailing-slash", "Glob", lambda t: {"pattern": "*", "path": str(t.out) + "/"}, DENY_READ),
    # ATK-34: symlink escape
    ("read-symlink-to-etc", "Read", lambda t: {"file_path": "etc/passwd"}, DENY_READ),
    ("read-relative-symlink-out", "Read", lambda t: {"file_path": "pw"}, DENY_READ),
    ("read-symlink-foreign-session", "Read", lambda t: {"file_path": "foreign/secret.txt"}, DENY_READ),
    ("glob-path-symlink", "Glob", lambda t: {"pattern": "*", "path": "etc"}, DENY_READ),
    ("grep-path-symlink", "Grep", lambda t: {"pattern": "x", "path": "foreign"}, DENY_READ),
    ("read-foreign-session", "Read", lambda t: {"file_path": str(t.other / "secret.txt")}, DENY_READ),
    # ATK-35: ".." after a symlink and hard links
    ("read-dotdot-after-symlink", "Read", lambda t: {"file_path": "escape/../passwd"}, DENY_READ),
    ("read-dotdot-after-inner-link", "Read", lambda t: {"file_path": "inner/../../../etc/passwd"}, DENY_READ),
    ("read-hard-link", "Read", lambda t: {"file_path": "hard"}, DENY_READ),
    ("write-hard-link", "Edit", lambda t: {"file_path": "hard", "old_string": "a", "new_string": "b"}, DENY_WRITE),
    # rule 5 - writes
    ("write-relative", "Write", lambda t: {"file_path": "out.txt", "content": "x"}, ALLOW),
    ("write-new-dirs", "Write", lambda t: {"file_path": "a/b/c.txt", "content": "x"}, ALLOW),
    ("edit-inside", "Edit", lambda t: {"file_path": str(t.ws / "notes.txt")}, ALLOW),
    ("write-workspace-itself", "Write", lambda t: {"file_path": str(t.ws)}, DENY_WRITE),
    ("write-no-path", "Write", lambda t: {"content": "x"}, DENY_WRITE),
    ("write-etc", "Write", lambda t: {"file_path": str(t.out / "passwd")}, DENY_WRITE),
    ("write-dotdot", "Write", lambda t: {"file_path": "../escape.txt"}, DENY_WRITE),
    ("write-missing-then-dotdot", "Write", lambda t: {"file_path": "new/../../../etc/x"}, DENY_WRITE),
    ("write-missing-dotdot-symlink", "Write", lambda t: {"file_path": "new/../etc/x"}, DENY_WRITE),
    ("write-through-symlink", "Write", lambda t: {"file_path": "etc/passwd"}, DENY_WRITE),
    ("write-dangling-symlink", "Write", lambda t: {"file_path": "dangling"}, DENY_WRITE),
    ("write-foreign", "Write", lambda t: {"file_path": "foreign/secret.txt"}, DENY_WRITE),
    ("write-harness", "Write", lambda t: {"file_path": str(t.harness)}, DENY_WRITE),
    ("write-dotdot-after-symlink", "Write", lambda t: {"file_path": "escape/../passwd"}, DENY_WRITE),
    ("write-lexically-out-physically-in", "Write", lambda t: {"file_path": "chain/../../../x"}, DENY_WRITE),
    # ATK-13..ATK-20: settings, hooks, harness tamper and protected names (case-insensitive)
    ("write-claude-settings", "Write", lambda t: {"file_path": ".claude/settings.json"}, DENY_PROTECTED),
    ("write-claude-upper", "Write", lambda t: {"file_path": ".CLAUDE/settings.json"}, DENY_PROTECTED),
    ("write-claude-dir-name", "Write", lambda t: {"file_path": "sub/.claude"}, DENY_PROTECTED),
    ("write-claude-via-symlink", "Write", lambda t: {"file_path": "cfg/settings.json"}, DENY_PROTECTED),
    ("write-claude-dot-segments", "Edit", lambda t: {"file_path": f"{t.ws}//./.claude//settings.json"}, DENY_PROTECTED),
    ("write-claude-trailing-slash", "Write", lambda t: {"file_path": ".claude/"}, DENY_PROTECTED),
    ("write-claude-json", "Write", lambda t: {"file_path": ".claude.json"}, DENY_PROTECTED),
    ("write-mcp-json", "Write", lambda t: {"file_path": "sub/.mcp.json"}, DENY_PROTECTED),
    ("write-mcp-json-upper", "Write", lambda t: {"file_path": ".MCP.JSON"}, DENY_PROTECTED),
    ("write-claude-md", "Write", lambda t: {"file_path": "CLAUDE.md"}, DENY_PROTECTED),
    ("write-claude-md-lower", "Edit", lambda t: {"file_path": "sub/claude.md"}, DENY_PROTECTED),
    ("write-claude-local-md", "Write", lambda t: {"file_path": "CLAUDE.local.md"}, DENY_PROTECTED),
    ("write-agents-md", "Write", lambda t: {"file_path": "AGENTS.md"}, DENY_PROTECTED),
    ("write-agents-md-nested", "Edit", lambda t: {"file_path": "repos/o/r/agents.md"}, DENY_PROTECTED),
    ("write-git-hooks", "Write", lambda t: {"file_path": ".git/hooks/pre-commit"}, DENY_PROTECTED),
    ("write-git-upper", "Write", lambda t: {"file_path": "sub/.GIT/config"}, DENY_PROTECTED),
    ("write-git-file", "Write", lambda t: {"file_path": "sub/.git"}, DENY_PROTECTED),
    ("write-gitconfig", "Write", lambda t: {"file_path": ".gitconfig"}, DENY_PROTECTED),
    ("write-not-protected-similar", "Write", lambda t: {"file_path": "x.claude/a.gitconfig.txt"}, ALLOW),
    # rule 4 - unresolvable values
    ("read-tilde", "Read", lambda t: {"file_path": "~/.claude/settings.json"}, DENY_PATH),
    ("read-nul", "Read", lambda t: {"file_path": "notes.txt\x00"}, DENY_PATH),
    ("read-empty", "Read", lambda t: {"file_path": ""}, DENY_PATH),
    ("read-not-string", "Read", lambda t: {"file_path": ["notes.txt"]}, DENY_PATH),
    ("glob-path-number", "Glob", lambda t: {"pattern": "*", "path": 7}, DENY_PATH),
    ("read-loop", "Read", lambda t: {"file_path": "loop/x"}, DENY_PATH),
    ("read-file-as-dir", "Read", lambda t: {"file_path": "notes.txt/x"}, DENY_PATH),
    ("both-keys-one-outside", "Read", lambda t: {"file_path": "notes.txt", "path": str(t.out)}, DENY_READ),
    ("both-keys-inside", "Grep", lambda t: {"pattern": "x", "file_path": "notes.txt", "path": "sub"}, ALLOW),
    # whitespace that Claude Code trims (JavaScript trim) never passes
    ("read-leading-space-absolute", "Read", lambda t: {"file_path": " " + str(t.out / "passwd")}, DENY_PATH),
    ("read-bom-absolute", "Read", lambda t: {"file_path": "\ufeff" + str(t.out / "passwd")}, DENY_PATH),
    ("grep-path-space", "Grep", lambda t: {"pattern": "root", "path": " " + str(t.out)}, DENY_PATH),
    ("read-space-tilde", "Read", lambda t: {"file_path": " ~/.claude/.credentials.json"}, DENY_PATH),
    ("write-trailing-space-protected", "Edit", lambda t: {"file_path": "CLAUDE.md "}, DENY_PATH),
    ("write-trailing-newline-protected", "Write", lambda t: {"file_path": f"{t.ws}/.mcp.json\n"}, DENY_PATH),
    ("write-git-space", "Write", lambda t: {"file_path": ".git "}, DENY_PATH),
    ("write-nbsp", "Write", lambda t: {"file_path": "a.txt\u00a0"}, DENY_PATH),
    ("read-lone-surrogate", "Read", lambda t: {"file_path": "\ud800/../notes.txt"}, DENY_PATH),
    ("glob-pattern-space", "Glob", lambda t: {"pattern": " /etc/*"}, DENY_PATTERN),
    ("grep-glob-bom", "Grep", lambda t: {"pattern": "x", "glob": "\ufeff/etc/*"}, DENY_PATTERN),
    ("read-inner-space-ok", "Read", lambda t: {"file_path": "my notes.txt"}, ALLOW),
    # rule 3 - patterns
    ("glob-absolute", "Glob", lambda t: {"pattern": "/etc/*"}, DENY_PATTERN),
    ("glob-tilde", "Glob", lambda t: {"pattern": "~/.ssh/*"}, DENY_PATTERN),
    ("glob-dotdot", "Glob", lambda t: {"pattern": "../*"}, DENY_PATTERN),
    ("glob-inner-dotdot", "Glob", lambda t: {"pattern": "sub/../../x"}, DENY_PATTERN),
    ("glob-trailing-dotdot", "Glob", lambda t: {"pattern": "sub/.."}, DENY_PATTERN),
    ("glob-brace-absolute", "Glob", lambda t: {"pattern": "{/etc,x}/*"}, DENY_PATTERN),
    ("glob-brace-dotdot", "Glob", lambda t: {"pattern": "{a,..}/*"}, DENY_PATTERN),
    ("glob-escaped-dotdot", "Glob", lambda t: {"pattern": "\\.\\./*"}, DENY_PATTERN),
    ("glob-escaped-slash", "Glob", lambda t: {"pattern": "\\/etc/*"}, DENY_PATTERN),
    ("glob-nul", "Glob", lambda t: {"pattern": "a\x00"}, DENY_PATTERN),
    ("glob-not-string", "Glob", lambda t: {"pattern": None}, DENY_PATTERN),
    ("grep-glob-absolute", "Grep", lambda t: {"pattern": "x", "glob": "/etc/**"}, DENY_PATTERN),
    ("grep-glob-dotdot", "Grep", lambda t: {"pattern": "x", "glob": "**/../*.txt"}, DENY_PATTERN),
    ("glob-dots-in-names", "Glob", lambda t: {"pattern": "src/..foo/a..b/*.."}, ALLOW),
    ("grep-glob-ok", "Grep", lambda t: {"pattern": "x", "glob": "*.{py,md}"}, ALLOW),
    ("glob-pattern-ok-path-out", "Glob", lambda t: {"pattern": "*", "path": "/"}, DENY_READ),
]


@pytest.mark.parametrize(("tool", "build", "expected"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_policy_table(
    tree: Tree, tool: str, build: Callable[[Tree], dict[str, Any]], expected: tuple[Any, ...]
) -> None:
    decision = make(tree).evaluate(tool, build(tree))
    reason = None if expected[2] is None else policy.deny_reason(expected[2])
    assert (decision.decision, decision.rule, decision.reason) == (expected[0], expected[1], reason)


def test_case_ids_are_unique() -> None:
    assert len({case[0] for case in CASES}) == len(CASES)


@pytest.mark.parametrize("name", sorted(policy.PLATFORM_TOOLS))
def test_rule_1_platform_tools_are_allowed(tree: Tree, name: str) -> None:
    assert make(tree).evaluate(f"mcp__platform__{name}", {"file_path": "/etc/shadow"}) == Decision("allow", 1)


@pytest.mark.parametrize(
    "name",
    [
        "Bash",
        "PowerShell",
        "Agent",
        "Task",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "Skill",
        "TaskOutput",
        "KillShell",
        "mcp__evil__shell",
        "mcp__platform__shell",
        "mcp__platform__",
        "read",
        "mcp__platform__get_context ",
    ],
)
def test_rule_2_other_tools_are_denied_by_name(tree: Tree, name: str) -> None:
    decision = make(tree).evaluate(name, {})
    assert (decision.decision, decision.rule) == ("deny", 2)
    if policy.TOOL_NAME_RE.fullmatch(name):
        assert decision.reason == policy.TOOL_FORBIDDEN.format(name=name)
    else:
        assert decision.reason == policy.REASONS["tool_forbidden_unnamed"]


@pytest.mark.parametrize("name", ["Bash; id", "a" * 129, "", "Bä"])
def test_rule_2_never_echoes_odd_names(tree: Tree, name: str) -> None:
    assert make(tree).evaluate(name, {}).reason == policy.REASONS["tool_forbidden_unnamed"]


def test_decision_carries_the_resolved_path(tree: Tree) -> None:
    assert make(tree).evaluate("Read", {"file_path": "inner/file.txt"}).path == str(tree.ws / "sub" / "file.txt")
    denied = make(tree).evaluate("Read", {"file_path": "etc/passwd"})
    assert denied.path == str(tree.out / "passwd")
    assert make(tree).evaluate("Glob", {"pattern": "*"}).path == str(tree.ws)


def test_hard_links_inside_the_workspace_are_denied_too(tree: Tree) -> None:
    os.link(tree.ws / "notes.txt", tree.ws / "notes-copy.txt")
    assert make(tree).evaluate("Read", {"file_path": "notes.txt"}).rule == 6


def test_hard_link_behind_dot_dot_is_denied(tree: Tree) -> None:
    # every resolved candidate is checked for hard links, not only the physical one
    os.link(tree.out / "passwd", tree.ws / "x")
    assert make(tree).evaluate("Read", {"file_path": "inner/../x"}).rule == 6


def test_unresolvable_workspace_denies(tree: Tree) -> None:
    def lstat(path: str) -> os.stat_result:
        raise PermissionError(errno.EACCES, path)

    decision = Policy(str(tree.ws), str(tree.harness), lstat=lstat).evaluate("Read", {"file_path": "x"})
    assert (decision.decision, decision.rule, decision.reason) == ("deny", 4, policy.REASONS["path_unresolvable"])


def test_permission_error_inside_the_path_denies(tree: Tree) -> None:
    def lstat(path: str) -> os.stat_result:
        if path.endswith("/sub"):
            raise PermissionError(errno.EACCES, path)
        return os.lstat(path)

    assert Policy(str(tree.ws), str(tree.harness), lstat=lstat).evaluate("Read", {"file_path": "sub/x"}).rule == 4


def test_readlink_error_denies(tree: Tree) -> None:
    def readlink(path: str) -> str:
        raise OSError(errno.EIO, path)

    decision = Policy(str(tree.ws), str(tree.harness), readlink=readlink).evaluate("Read", {"file_path": "inner/x"})
    assert decision.rule == 4


def test_too_many_symlinks_deny(tree: Tree) -> None:
    previous = "notes.txt"
    for index in range(policy.MAX_SYMLINKS + 1):
        os.symlink(previous, tree.ws / f"l{index}")
        previous = f"l{index}"
    assert make(tree).evaluate("Read", {"file_path": f"l{policy.MAX_SYMLINKS - 1}"}).decision == "allow"
    assert make(tree).evaluate("Read", {"file_path": previous}).rule == 4


def test_hard_link_check_ignores_missing_files(tree: Tree) -> None:
    calls: list[str] = []

    def lstat(path: str) -> os.stat_result:
        calls.append(path)
        if path == str(tree.ws / "gone") and calls.count(path) > 2:
            raise FileNotFoundError(path)
        return os.lstat(path) if path != str(tree.ws / "gone") else os.lstat(tree.ws / "notes.txt")

    # the file disappears between resolution and the hard-link check: treated as not linked
    assert Policy(str(tree.ws), str(tree.harness), lstat=lstat).evaluate("Read", {"file_path": "gone"}).rule == 7


# ---------------------------------------------------------------- resolve and lexical


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/", "/"),
        ("//etc//passwd/", "/etc/passwd"),
        ("/a/./b/../c", "/a/c"),
        ("/../../x", "/x"),
        ("/a/b/..", "/a"),
    ],
)
def test_lexical(raw: str, expected: str) -> None:
    assert lexical(raw) == expected


def test_resolve_matches_realpath_for_existing_paths(tree: Tree) -> None:
    for relative in ("notes.txt", "inner/file.txt", "chain", "etc/passwd", "pw", "foreign/secret.txt", "escape/.."):
        path = f"{tree.ws}/{relative}"
        assert resolve(path) == os.path.realpath(path), relative


def test_resolve_follows_dangling_links_and_keeps_missing_tails(tree: Tree) -> None:
    assert resolve(f"{tree.ws}/dangling") == str(tree.out / "new.conf")
    assert resolve(f"{tree.ws}/new/a/../b") == f"{tree.ws}/new/b"
    assert resolve(f"{tree.ws}/new/../inner/x") == str(tree.ws / "sub" / "x")
    assert resolve("/..") == "/"


@pytest.mark.parametrize("relative", ["loop", "notes.txt/x"])
def test_resolve_errors(tree: Tree, relative: str) -> None:
    with pytest.raises(Unresolvable):
        resolve(f"{tree.ws}/{relative}")


def test_harness_path() -> None:
    assert policy.harness_path(SESSION) == f"/run/agent-guest/harness/{SESSION}.md"
    assert policy.harness_path(SESSION, "/x") == f"/x/{SESSION}.md"


# ---------------------------------------------------------------- contract


def test_reasons_are_the_contract_sentences(contracts_dir: Path, schemas: SchemaSet) -> None:
    contract = json.loads((contracts_dir / "mcp" / "hook-deny-reasons.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in contract["pre_tool_use"]}
    assert by_id.pop("tool_forbidden")["template"] == policy.TOOL_FORBIDDEN
    assert {key: row["text"] for key, row in by_id.items()} == policy.REASONS
    assert contract["user_prompt_submit"]["text"] == policy.BLOCK_REASON
    rules = {row["id"]: row["rule"] for row in contract["pre_tool_use"]}
    for *_, expected in CASES:
        if expected[2] is not None:
            assert rules[expected[2]] == expected[1], expected
    for text in [*policy.REASONS.values(), policy.deny_reason("tool_forbidden", "Bash")]:
        assert schemas.errors("mcp/objects.json#/$defs/deny_reason", text) == []


def test_platform_tools_are_the_contract_tools(contracts_dir: Path) -> None:
    index = json.loads((contracts_dir / "mcp" / "INDEX.json").read_text(encoding="utf-8"))
    assert {row["tool"] for row in index["tools"]} == policy.PLATFORM_TOOLS
