"""The objects of the team record (RECORD-PROTOCOL.md sections 2 and 5): every field form at its limits,
the canonical text of lists and numbers, and the object values. Pure functions, no database."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from agent_core.record.objects import (
    REFUSED_TASK_FIELDS,
    REQUIRED_TASK_FIELDS,
    Comment,
    Stamp,
    Task,
    links_value,
    multi_line_text,
    parse_links,
    parse_urgency,
    single_line_text,
    urgency_value,
    valid_commit_reference,
    valid_pull_request_reference,
    valid_value,
)

UUID = "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
REF = "example-org/example-repo#42"


def ref(number: int) -> str:
    return f"example-org/example-repo#{number}"


VALID = [
    ("task", "kind", b"work"),
    ("task", "kind", b"security"),
    ("task", "team", b"t"),
    ("task", "team", "é".encode() * 50),
    ("task", "team", b"t" * 100),
    ("task", "title", b"x"),
    ("task", "title", b"x" * 200),
    ("task", "title", "Fix login – ü".encode()),
    ("task", "title", b"a b"),
    ("task", "description", b"line\nline\twith tab "),
    ("task", "description", b" " * 16384),
    ("task", "assignee", b"example-staff"),
    ("task", "assignee", b"a" * 39),
    ("task", "status", b"ready_for_review"),
    ("task", "completion", b"verified"),
    ("task", "links", REF.encode()),
    ("task", "links", " ".join(ref(n) for n in range(10, 30)).encode()),
    ("task", "links", f"{ref(10)} {ref(9)}".encode()),
    ("task", "links", ref(2147483647).encode()),
    ("task", "repository", b"example-org/example-repo"),
    ("task", "short_name", b"a" * 30),
    ("task", "short_name", b"login-bypass"),
    ("task", "severity", b"critical"),
    ("task", "urgency", b"1"),
    ("task", "urgency", b"10"),
    ("task", "source", b"!" * 200),
    ("task", "fix", REF.encode()),
    ("task", "fix", b"example-org/example-repo@" + b"a" * 40),
    ("task", "fix", b"example-org/example-repo@" + b"0" * 64),
    ("task", "verification", b"x" * 2000),
    ("comment", "task", UUID.encode()),
    ("comment", "body", b"x" * 16384),
    ("comment", "body", b"\n"),
]

INVALID = [
    ("task", "kind", b"Work"),
    ("task", "team", b"t" * 101),
    ("task", "team", b"\xff"),
    ("task", "team", b"\xed\xa0\x80"),  # a surrogate
    ("task", "team", b"\xc0\xaf"),  # an overlong form
    ("task", "title", b"x" * 201),
    ("task", "title", b" x"),
    ("task", "title", b"x "),
    ("task", "title", b"a\nb"),
    ("task", "title", b"a\tb"),
    ("task", "title", b"a\x7fb"),
    ("task", "title", "a\u0085b".encode()),
    ("task", "title", "a b".encode()),
    ("task", "title", "a b".encode()),
    ("task", "title", b"\xff"),
    ("task", "title", b""),
    ("task", "description", b"x" * 16385),
    ("task", "description", b"a\rb"),
    ("task", "description", b"a\x00b"),
    ("task", "description", b"a\x0bb"),
    ("task", "description", b"\xff"),
    ("task", "assignee", b"Example"),
    ("task", "assignee", b"a" * 40),
    ("task", "assignee", b"a--b"),
    ("task", "assignee", "ä".encode()),
    ("task", "status", b"in progress"),
    ("task", "completion", b"verified in production"),
    ("task", "links", " ".join(ref(n) for n in range(10, 31)).encode()),
    ("task", "links", f"{ref(9)} {ref(10)}".encode()),
    ("task", "links", f"{ref(10)} {ref(10)}".encode()),
    ("task", "links", f"{ref(10)}  {ref(11)}".encode()),
    ("task", "links", f" {ref(10)}".encode()),
    ("task", "links", ref(0).encode()),
    ("task", "links", ref(2147483648).encode()),
    ("task", "links", ref(1).replace("#1", "#01").encode()),
    ("task", "links", ref(1).replace("#1", "#+1").encode()),
    ("task", "links", b"example-org/example-repo"),
    ("task", "links", b"Example-org/example-repo#1"),
    ("task", "links", "example-org/example-repo#١".encode()),
    ("task", "repository", b"example-org"),
    ("task", "repository", "exämple/x".encode()),
    ("task", "short_name", b"a" * 31),
    ("task", "short_name", b"-a"),
    ("task", "short_name", b"a--b"),
    ("task", "short_name", "ä".encode()),
    ("task", "severity", b"severe"),
    ("task", "urgency", b"0"),
    ("task", "urgency", b"11"),
    ("task", "urgency", b"07"),
    ("task", "urgency", b"+1"),
    ("task", "source", b"!" * 201),
    ("task", "source", b"a b"),
    ("task", "source", "ä".encode()),
    ("task", "fix", b"example-org/example-repo@" + b"a" * 39),
    ("task", "fix", b"example-org/example-repo@" + b"a" * 41),
    ("task", "fix", b"example-org/example-repo@" + b"a" * 63),
    ("task", "fix", b"example-org/example-repo@" + b"a" * 65),
    ("task", "fix", b"example-org/example-repo@" + b"A" * 40),
    ("task", "fix", b"example-org/example-repo"),
    ("task", "fix", "ä".encode()),
    ("task", "verification", b"x" * 2001),
    ("task", "verification", b"a\nb"),
    ("comment", "task", UUID.upper().encode()),
    ("comment", "task", b"5d1c2b3a-4e5f-1a6b-8c7d-9e0f1a2b3c4d"),
    ("comment", "task", "ä".encode()),
    ("comment", "body", b"x" * 16385),
    ("comment", "body", b"a\rb"),
]


@pytest.mark.parametrize(("object_type", "name", "value"), VALID)
def test_a_value_in_its_form(object_type: str, name: str, value: bytes) -> None:
    assert valid_value(object_type, name, value)


@pytest.mark.parametrize(("object_type", "name", "value"), INVALID)
def test_a_value_outside_its_form(object_type: str, name: str, value: bytes) -> None:
    assert not valid_value(object_type, name, value)


def test_a_field_the_object_type_does_not_have_is_no_form() -> None:
    with pytest.raises(KeyError):
        valid_value("comment", "title", b"x")
    with pytest.raises(KeyError):
        valid_value("discussion", "title", b"x")


def test_text_helpers_at_their_limits() -> None:
    assert single_line_text(b"abc", 3)
    assert not single_line_text(b"abcd", 3)
    assert multi_line_text(b"a\n", 2)
    assert not multi_line_text(b"", 2)


def test_references() -> None:
    assert valid_pull_request_reference(REF)
    assert not valid_pull_request_reference("example-org/example-repo#42#1")
    assert not valid_pull_request_reference("example-org/example-repo@42")
    assert valid_commit_reference("example-org/example-repo@" + "f" * 40)
    assert not valid_commit_reference(REF)


def test_the_canonical_text_of_links() -> None:
    assert links_value([ref(1), ref(2)]) == b"example-org/example-repo#1 example-org/example-repo#2"
    assert parse_links(links_value([ref(1), ref(2)])) == (ref(1), ref(2))
    with pytest.raises(ValueError):
        links_value([ref(2), ref(1)])
    with pytest.raises(ValueError):
        links_value([])
    assert parse_links(b"\xff") is None


def test_the_canonical_text_of_urgency() -> None:
    assert urgency_value(1) == b"1"
    assert urgency_value(10) == b"10"
    assert parse_urgency(b"10") == 10
    assert parse_urgency(b"07") is None
    for wrong in (0, 11, True, 1.0, "1"):
        with pytest.raises(ValueError):
            urgency_value(wrong)  # type: ignore[arg-type]


def test_the_field_table_of_section_5_2() -> None:
    assert REQUIRED_TASK_FIELDS["work"] == {"kind", "team", "title", "status", "completion"}
    assert REQUIRED_TASK_FIELDS["security"] == {
        "kind", "team", "title", "status", "completion", "repository", "short_name", "severity", "urgency"
    }
    assert REFUSED_TASK_FIELDS["work"] == {"repository", "short_name", "severity", "urgency", "source", "fix"}
    assert REFUSED_TASK_FIELDS["security"] == frozenset()


STAMP = Stamp(position=16, received_at=0, key="example-staff-laptop-1")


def task(values: dict[str, bytes], completed_links: bytes | None) -> Task:
    return Task(
        object_id=UUID,
        values=MappingProxyType(values),
        completed_links=completed_links,
        comments=0,
        created=STAMP,
        changed=STAMP,
    )


def test_a_task_value() -> None:
    work = task(
        {"kind": b"work", "team": b"example", "title": b"Fix login", "status": b"open", "completion": b"merged",
         "links": REF.encode()},
        REF.encode(),
    )
    assert work.object_type == "task"
    assert (work.kind, work.team, work.status, work.completion) == ("work", b"example", "open", "merged")
    assert work.assignee is None
    assert work.repository is None
    assert work.short_name is None
    assert work.links == (REF,)
    assert work.completed_link_references == (REF,)
    assert work.value("title") == b"Fix login"
    assert work.value("description") is None
    assert repr(work) == "Task()"
    with pytest.raises(AttributeError):
        work.comments = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        work.values["title"] = b"x"  # type: ignore[index]
    plain = task({"kind": b"work"}, None)
    assert plain.links == ()
    assert plain.completed_link_references == ()
    assert repr(STAMP) == "Stamp(position=16, key='example-staff-laptop-1')"


def test_a_stored_links_value_outside_its_form_fails_loudly() -> None:
    broken = task({"links": b"not a link"}, None)
    with pytest.raises(ValueError):
        broken.links


def test_a_comment_value() -> None:
    comment = Comment(object_id=UUID, task=UUID, body=b"Reproduced.", created=STAMP)
    assert comment.object_type == "comment"
    assert comment.value("task") == UUID.encode()
    assert comment.value("body") == b"Reproduced."
    assert comment.value("title") is None
    assert repr(comment) == "Comment()"
