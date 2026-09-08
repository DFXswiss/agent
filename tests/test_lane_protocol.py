import json

import pytest

from agent_cli.lane_protocol import Finished, ProtocolError, SourceSession, digest


def session(*, write=True, **kwargs):
    return SourceSession({"src/a.py": "a\nb\n"}, write=write,
                         max_requests=kwargs.get("max_requests", 20), max_total_bytes=5000)


def request(view, **kwargs):
    return view.request(json.dumps(kwargs))


def test_proposals_are_data_until_the_script_receives_a_finished_result():
    original = {"src/a.py": "a\nb\n"}
    view = SourceSession(original, write=True, max_requests=5, max_total_bytes=5000)
    read = request(view, action="read", path="src/a.py", offset=1, limit=1)
    assert read == {"path": "src/a.py", "content": "b\n", "offset": 1,
                    "sha256": digest(original["src/a.py"]), "total_lines": 2}
    request(view, action="write", path="src/a.py", expected_sha256=read["sha256"], content="new\n")
    assert original == {"src/a.py": "a\nb\n"}
    with pytest.raises(ProtocolError, match="unfinished"):
        view.changes()
    assert request(view, action="finish", text="STATUS: complete\nRESULT: done") == Finished("STATUS: complete\nRESULT: done")
    assert view.changes() == {"src/a.py": "new\n"}


@pytest.mark.parametrize("action", ["exec", "test", "github", "monitor", "sleep", "spawn_agent", "review", "shell"])
def test_external_work_never_becomes_an_executable_fallback(action):
    view = session()
    with pytest.raises(ProtocolError, match="unknown action"):
        request(view, action=action, command="anything")
    assert view.files == view.original


@pytest.mark.parametrize("path", ["/tmp/x", "../x", "src/../../x", "src/./x", "src//x", ".git/config",
                                   "src/.GiT/config", "C:/x", "src\\x", "src/\x00x"])
def test_host_and_git_paths_are_rejected(path):
    with pytest.raises(ProtocolError):
        request(session(), action="write", path=path, expected_sha256=None, content="x")


@pytest.mark.parametrize("action", ["write", "delete"])
def test_readonly_cannot_propose_changes(action):
    fields = dict(action=action, path="src/a.py", expected_sha256=digest("a\nb\n"))
    if action == "write":
        fields["content"] = "replacement"
    with pytest.raises(ProtocolError, match="read-only"):
        request(session(write=False), **fields)


def test_stale_or_missing_digest_does_not_overwrite_current_text():
    view = session()
    for expected in (None, digest("old"), 17, True):
        with pytest.raises(ProtocolError, match="digest"):
            request(view, action="write", path="src/a.py", expected_sha256=expected, content="replacement")
    assert view.files == view.original


def test_creation_deletion_and_reversion_are_precise_proposals():
    view = session()
    request(view, action="write", path="new.py", expected_sha256=None, content="print('data only')")
    request(view, action="delete", path="src/a.py", expected_sha256=digest("a\nb\n"))
    request(view, action="write", path="src/a.py", expected_sha256=None, content="a\nb\n")
    request(view, action="finish", text="done")
    assert view.changes() == {"new.py": "print('data only')"}
    with pytest.raises(ProtocolError, match="finished"):
        request(view, action="list", prefix="", offset=0)


@pytest.mark.parametrize("raw", ['{"action":"list","action":"exec"}', '{"action":NaN}', '[]', '{} trailing',
                                  '{"action":"finish","text":"x","command":"whoami"}'])
def test_malformed_or_ambiguous_protocol_fails_closed(raw):
    with pytest.raises(ProtocolError):
        session().request(raw)


def test_budget_exhaustion_cannot_produce_applicable_changes():
    view = session(max_requests=1)
    request(view, action="write", path="new", expected_sha256=None, content="new")
    with pytest.raises(ProtocolError, match="exhausted"):
        request(view, action="finish", text="done")
    with pytest.raises(ProtocolError, match="unfinished"):
        view.changes()


def test_size_failure_preserves_the_prior_snapshot():
    view = session()
    with pytest.raises(ProtocolError, match="byte limit"):
        request(view, action="write", path="new", expected_sha256=None, content="x" * 5000)
    assert view.files == view.original


def test_listing_is_paginated_and_does_not_interpret_prefix_as_code():
    view = SourceSession({f"src/{i:03}.py": "" for i in range(60)}, write=False,
                         max_requests=5, max_total_bytes=5000)
    page = request(view, action="list", prefix="src/", offset=0)
    assert len(page["paths"]) == 50 and page["next_offset"] == 50
    assert request(view, action="list", prefix="src/", offset=50)["next_offset"] is None
    assert request(view, action="list", prefix="$(anything)", offset=0)["paths"] == []


@pytest.mark.parametrize("offset", [True, -1, 1.5, "1"])
def test_offsets_are_bounded_integers(offset):
    with pytest.raises(ProtocolError):
        request(session(), action="read", path="src/a.py", offset=offset, limit=1)


def test_exact_replace_changes_only_one_digest_checked_occurrence():
    view = session()
    result = request(view, action="replace", path="src/a.py", expected_sha256=digest("a\nb\n"),
                     old="b\n", new="literal $(command)\n")
    assert result["sha256"] == digest("a\nliteral $(command)\n")
    assert view.original["src/a.py"] == "a\nb\n"
    with pytest.raises(ProtocolError, match="digest"):
        request(view, action="replace", path="src/a.py", expected_sha256=digest("a\nb\n"), old="a", new="x")


@pytest.mark.parametrize("old", ["", "absent", "\n"])
def test_replace_ambiguous_or_absent_text_preserves_source(old):
    view = session()
    with pytest.raises(ProtocolError, match="exactly one"):
        request(view, action="replace", path="src/a.py", expected_sha256=digest("a\nb\n"), old=old, new="x")
    assert view.files == view.original


def test_replace_is_denied_to_reviewers():
    with pytest.raises(ProtocolError, match="read-only"):
        request(session(write=False), action="replace", path="src/a.py",
                expected_sha256=digest("a\nb\n"), old="a", new="x")


def test_invalid_unicode_path_is_a_protocol_error():
    with pytest.raises(ProtocolError, match="UTF-8"):
        request(session(), action="write", path="\ud800", expected_sha256=None, content="x")
