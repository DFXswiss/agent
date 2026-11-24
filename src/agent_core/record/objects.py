"""The objects of the team record (RECORD-PROTOCOL.md section 5).

Pure values and pure checks: the fields of a task and of a comment, their forms and limits, the
canonical text of lists and numbers, and the state of one object as an immutable value. Section 13
keeps every value of a field, every object id and every field name out of a log line, so the
`repr` of an object shows nothing of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import Callable, ClassVar, Iterable, Mapping

from ..keys import valid_handle, valid_subject
from .event import valid_uuid

TASK = "task"
COMMENT = "comment"

KIND_WORK = "work"
KIND_SECURITY = "security"
KINDS = (KIND_WORK, KIND_SECURITY)

OPEN = "open"
IN_PROGRESS = "in_progress"
READY_FOR_REVIEW = "ready_for_review"
MERGED = "merged"
DONE = "done"
DROPPED = "dropped"
STATUSES = (OPEN, IN_PROGRESS, READY_FOR_REVIEW, MERGED, DONE, DROPPED)

COMPLETION_MERGED = "merged"
COMPLETION_VERIFIED = "verified"
COMPLETIONS = (COMPLETION_MERGED, COMPLETION_VERIFIED)

SEVERITIES = ("low", "medium", "high", "critical")

TASK_FIELDS = (
    "kind",
    "team",
    "title",
    "description",
    "assignee",
    "status",
    "completion",
    "links",
    "repository",
    "short_name",
    "severity",
    "urgency",
    "source",
    "fix",
    "verification",
)
COMMENT_FIELDS = ("task", "body")
FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {TASK: frozenset(TASK_FIELDS), COMMENT: frozenset(COMMENT_FIELDS)}
)

# Section 5.2, the columns "Kind work" and "Kind security".
_COMMON_REQUIRED = frozenset({"kind", "team", "title", "status", "completion"})
REQUIRED_TASK_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        KIND_WORK: _COMMON_REQUIRED,
        KIND_SECURITY: _COMMON_REQUIRED | {"repository", "short_name", "severity", "urgency"},
    }
)
REFUSED_TASK_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        KIND_WORK: frozenset({"repository", "short_name", "severity", "urgency", "source", "fix"}),
        KIND_SECURITY: frozenset(),
    }
)
IMMUTABLE_TASK_FIELDS = frozenset({"kind", "team"})
REQUIRED_COMMENT_FIELDS = frozenset(COMMENT_FIELDS)

# Section 5.2, "Defaults at create". They are part of the computed state, never of an event.
DEFAULT_STATUS = OPEN
DEFAULT_COMPLETION: Mapping[str, str] = MappingProxyType(
    {KIND_WORK: COMPLETION_MERGED, KIND_SECURITY: COMPLETION_VERIFIED}
)

TEAM_MAX_BYTES = 100
TITLE_MAX_BYTES = 200
DESCRIPTION_MAX_BYTES = 16384
BODY_MAX_BYTES = 16384
VERIFICATION_MAX_BYTES = 2000
SOURCE_MAX_BYTES = 200
SHORT_NAME_MAX = 30
MAX_LINKS = 20
PULL_REQUEST_MAX = 2147483647
LINK_SEPARATOR = " "

# Section 2: single-line text refuses U+0000 to U+001F, U+007F to U+009F, U+2028 and U+2029;
# multi-line text allows U+0009 and U+000A of them (U+000D never).
_SINGLE_LINE_REFUSED = re.compile("[\x00-\x1f\x7f-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]")
_MULTI_LINE_REFUSED = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]")
_NUMBER_RE = re.compile(r"[1-9][0-9]{0,9}")
_COMMIT_ID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_URGENCY_RE = re.compile(rb"[1-9]|10")
_SOURCE_RE = re.compile(rb"[\x21-\x7e]{1,200}")


def _utf8(value: bytes) -> str | None:
    """The text of valid UTF-8 (no surrogates, no overlong forms); None otherwise."""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _ascii(value: bytes) -> str | None:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None


def single_line_text(value: bytes, max_bytes: int) -> bool:
    """Section 2, single-line text of 1 to `max_bytes` UTF-8 bytes."""
    text = _utf8(value)
    return (
        text is not None
        and 1 <= len(value) <= max_bytes
        and _SINGLE_LINE_REFUSED.search(text) is None
        and not text.startswith(" ")
        and not text.endswith(" ")
    )


def multi_line_text(value: bytes, max_bytes: int) -> bool:
    """Section 2, multi-line text of 1 to `max_bytes` UTF-8 bytes."""
    text = _utf8(value)
    return text is not None and 1 <= len(value) <= max_bytes and _MULTI_LINE_REFUSED.search(text) is None


def valid_pull_request_reference(text: str) -> bool:
    """Section 2: `<repository>#<number>`, number 1 to 2147483647 without sign or leading zero."""
    repository, sign, number = text.partition("#")
    return (
        sign == "#"
        and valid_subject(repository)
        and _NUMBER_RE.fullmatch(number) is not None
        and int(number) <= PULL_REQUEST_MAX
    )


def valid_commit_reference(text: str) -> bool:
    """Section 2: `<repository>@<commit id>` with 40 or 64 lowercase hex characters."""
    repository, sign, commit = text.partition("@")
    return sign == "@" and valid_subject(repository) and _COMMIT_ID_RE.fullmatch(commit) is not None


def reference_repository(reference: str) -> str:
    """The repository of a pull-request reference of a valid `links` value."""
    return reference.partition("#")[0]


def parse_links(value: bytes) -> tuple[str, ...] | None:
    """The references of a `links` value of section 5.2; None when it breaks the form."""
    text = _ascii(value)
    if text is None:
        return None
    references = tuple(text.split(LINK_SEPARATOR))
    if not 1 <= len(references) <= MAX_LINKS:
        return None
    if not all(valid_pull_request_reference(reference) for reference in references):
        return None
    if any(first >= second for first, second in zip(references, references[1:])):
        return None
    return references


def links_value(references: Iterable[str]) -> bytes:
    """The canonical text of a list of references: joined by one space, strictly ascending.

    `ValueError` when the list breaks the form of section 5.2, so that no other text is written.
    """
    value = LINK_SEPARATOR.join(references).encode("utf-8")
    if parse_links(value) is None:
        raise ValueError("links break the form of section 5.2")
    return value


def urgency_value(urgency: int) -> bytes:
    """The canonical decimal text of an `urgency`; `ValueError` outside 1 to 10."""
    if type(urgency) is not int or not 1 <= urgency <= 10:
        raise ValueError("urgency is an int from 1 to 10")
    return str(urgency).encode("ascii")


def parse_urgency(value: bytes) -> int | None:
    """The number of an `urgency` value; None when it breaks the form."""
    if _URGENCY_RE.fullmatch(value) is None:
        return None
    return int(value)


def _one_of(choices: tuple[str, ...]) -> frozenset[bytes]:
    return frozenset(choice.encode("ascii") for choice in choices)


_KIND_VALUES = _one_of(KINDS)
_STATUS_VALUES = _one_of(STATUSES)
_COMPLETION_VALUES = _one_of(COMPLETIONS)
_SEVERITY_VALUES = _one_of(SEVERITIES)


def _short_name(value: bytes) -> bool:
    text = _ascii(value)
    return text is not None and len(text) <= SHORT_NAME_MAX and valid_handle(text)


def _fix(value: bytes) -> bool:
    text = _ascii(value)
    return text is not None and (valid_pull_request_reference(text) or valid_commit_reference(text))


def _team(value: bytes) -> bool:
    return 1 <= len(value) <= TEAM_MAX_BYTES and _utf8(value) is not None


def _ascii_form(check: Callable[[str], bool]) -> Callable[[bytes], bool]:
    def form(value: bytes) -> bool:
        text = _ascii(value)
        return text is not None and check(text)

    return form


_TASK_FORMS: dict[str, Callable[[bytes], bool]] = {
    "kind": lambda value: value in _KIND_VALUES,
    "team": _team,
    "title": lambda value: single_line_text(value, TITLE_MAX_BYTES),
    "description": lambda value: multi_line_text(value, DESCRIPTION_MAX_BYTES),
    "assignee": _ascii_form(valid_handle),
    "status": lambda value: value in _STATUS_VALUES,
    "completion": lambda value: value in _COMPLETION_VALUES,
    "links": lambda value: parse_links(value) is not None,
    "repository": _ascii_form(valid_subject),
    "short_name": _short_name,
    "severity": lambda value: value in _SEVERITY_VALUES,
    "urgency": lambda value: parse_urgency(value) is not None,
    "source": lambda value: _SOURCE_RE.fullmatch(value) is not None,
    "fix": _fix,
    "verification": lambda value: single_line_text(value, VERIFICATION_MAX_BYTES),
}
_COMMENT_FORMS: dict[str, Callable[[bytes], bool]] = {
    "task": _ascii_form(valid_uuid),
    "body": lambda value: multi_line_text(value, BODY_MAX_BYTES),
}
_FORMS = {TASK: _TASK_FORMS, COMMENT: _COMMENT_FORMS}
NO_FORM = "the object type has no such field"


def valid_value(object_type: str, name: str, value: bytes) -> bool:
    """Whether `value` has the form of the field `name` of `object_type` (sections 5.2 and 5.3).

    `ValueError` with a fixed message, which names neither the object type nor the field (section
    13), for an object type or a field that does not exist: the caller checks first that the object
    type has the field (check 14 of section 6.7 checks only such fields).
    """
    forms = _FORMS.get(object_type)
    form = None if forms is None else forms.get(name)
    if form is None:
        raise ValueError(NO_FORM)
    return form(value)


@dataclass(frozen=True)
class Stamp:
    """An event of an object: its position, its `received_at` and its `actor_key_id` (section 7.5)."""

    position: int
    received_at: int = dataclass_field(repr=False)
    key: str


@dataclass(frozen=True)
class Task:
    """The state of one task after an event (section 5.2), an immutable value.

    `values` holds every field that has a value, as the bytes of its value token; the defaults of
    `status` and `completion` are part of it. `completed_links` is the derived value of section 5.2.
    """

    object_type: ClassVar[str] = TASK

    object_id: str = dataclass_field(repr=False)
    values: Mapping[str, bytes] = dataclass_field(repr=False)
    completed_links: bytes | None = dataclass_field(repr=False)
    comments: int = dataclass_field(repr=False)
    created: Stamp = dataclass_field(repr=False)
    changed: Stamp = dataclass_field(repr=False)

    def value(self, name: str) -> bytes | None:
        """The value of a field, None when the field has no value."""
        return self.values.get(name)

    def _text(self, name: str) -> str | None:
        value = self.values.get(name)
        return None if value is None else value.decode("utf-8")

    @property
    def kind(self) -> str:
        return self.values["kind"].decode("ascii")

    @property
    def team(self) -> bytes:
        return self.values["team"]

    @property
    def status(self) -> str:
        return self.values["status"].decode("ascii")

    @property
    def completion(self) -> str:
        return self.values["completion"].decode("ascii")

    @property
    def assignee(self) -> str | None:
        return self._text("assignee")

    @property
    def repository(self) -> str | None:
        return self._text("repository")

    @property
    def short_name(self) -> str | None:
        return self._text("short_name")

    @property
    def links(self) -> tuple[str, ...]:
        """The references of `links`; empty when the field has no value."""
        return _references(self.values.get("links"))

    @property
    def completed_link_references(self) -> tuple[str, ...]:
        """The references of `completed_links`; empty when there is none."""
        return _references(self.completed_links)


def _references(value: bytes | None) -> tuple[str, ...]:
    if value is None:
        return ()
    references = parse_links(value)
    if references is None:
        raise ValueError("a stored links value breaks its form")
    return references


@dataclass(frozen=True)
class Comment:
    """A comment (section 5.3), written once and never changed: an immutable value."""

    object_type: ClassVar[str] = COMMENT

    object_id: str = dataclass_field(repr=False)
    task: str = dataclass_field(repr=False)
    body: bytes = dataclass_field(repr=False)
    created: Stamp = dataclass_field(repr=False)

    @property
    def values(self) -> Mapping[str, bytes]:
        return MappingProxyType({"task": self.task.encode("ascii"), "body": self.body})

    def value(self, name: str) -> bytes | None:
        """The value of a field, None for a field a comment does not have."""
        return self.values.get(name)


RecordObject = Task | Comment
