"""Looking things up instead of reading everything (KOR-63, owner rule E-26): `search` over the command tree and
`guide --topic` over the sections of the guide.

The command tree is the only source: every command carries its `commands.Doc` (purpose, example, search words, next
step), so the lookup cannot drift from the commands. The guide's topics are its `## ` sections, so a section added
later is a topic at once.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from typing import Any, cast

from agent_platform.commands import Doc, Parser
from agent_platform.errors import CODES, NEXT

MAX_RESULTS = 5
# A match scoring less than this share of the best one is left out (fewer, better hits).
RELEVANT = 0.5
PROG = "agent-platform"
# Words that say nothing about a command, in English and German.
STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "my",
        "our",
        "your",
        "his",
        "her",
        "their",
        "it",
        "is",
        "be",
        "do",
        "how",
        "what",
        "which",
        "i",
        "we",
        "me",
        "can",
        "want",
        "new",
        "please",
        "ein",
        "eine",
        "einen",
        "einem",
        "einer",
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "zu",
        "zum",
        "zur",
        "fur",
        "von",
        "mit",
        "und",
        "oder",
        "mein",
        "meine",
        "dein",
        "deine",
        "ich",
        "wir",
        "wie",
        "was",
        "welche",
        "kann",
        "will",
        "mochte",
        "bitte",
        "im",
        "in",
        "am",
        "an",
        "auf",
    ]
)
# The weight of a match in each part of a command (its name counts most).
WEIGHTS = (("name", 6.0), ("words", 4.0), ("summary", 3.0), ("usage", 1.0))
NOISE = re.compile(r"\s*\[-h\]|\s*\[--platform-root-key PLATFORM_ROOT_KEY\]")


@dataclass(frozen=True)
class Entry:
    command: str
    doc: Doc
    usage: str
    tokens: dict[str, frozenset[str]]

    def hit(self) -> dict[str, Any]:
        found = {"command": self.command, "summary": self.doc.summary, "usage": self.usage, "example": self.doc.example}
        return {**found, "next": self.doc.then} if self.doc.then else found


def fold(text: str) -> str:
    """Lower case without accents (`Gerät` → `gerat`)."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in plain if not unicodedata.combining(char))


def tokens(text: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", fold(text)) if len(word) > 1 and word not in STOP]


def similar(query: str, word: str) -> float:
    """1 for the same word, 0.7 for the same stem (`einladen`/`einladung`, `task`/`tasks`), else 0."""
    if query == word:
        return 1.0
    common = len(_prefix(query, word))
    shorter = min(len(query), len(word))
    if shorter >= 4 and (common == shorter or common >= max(5, shorter - 2)):
        return 0.7
    return 0.0


def _prefix(left: str, right: str) -> str:
    size = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        size += 1
    return left[:size]


def walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> Iterator[tuple[str, Parser]]:
    """Every command of the tree as (`group command`, its parser), in the order of the tree."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from walk(sub, (*path, name))
            return
    yield " ".join(path), cast(Parser, parser)


def usage(parser: Parser) -> str:
    text = NOISE.sub("", " ".join(parser.format_usage().split()))
    return text.removeprefix("usage: ")


def entries(root: argparse.ArgumentParser) -> list[Entry]:
    found = []
    for command, parser in walk(root):
        doc = parser.doc
        assert doc is not None, command
        parts = {"name": command, "words": doc.words, "summary": doc.summary, "usage": usage(parser)}
        found.append(Entry(command, doc, parts["usage"], {key: frozenset(tokens(text)) for key, text in parts.items()}))
    return found


def score(entry: Entry, query: list[str], phrase: str) -> float:
    total = 0.0
    matched = 0
    for word in query:
        best = max(similar(word, known) * weight for key, weight in WEIGHTS for known in entry.tokens[key])
        total += best
        matched += best > 0
    if matched == len(query):
        total *= 1.5
    if f" {fold(entry.command)} " in f" {phrase} ":
        total += 10.0
    return total


def search(root: argparse.ArgumentParser, query: str) -> dict[str, Any]:
    """At most five commands for `query`, the best first and none far behind it, and the next step of an error code
    the query names (`no_device`); no hit gives the groups to look further."""
    words = tokens(query)
    phrase = " ".join(re.findall(r"[a-z0-9-]+", fold(query)))
    listed = entries(root)
    ranked = sorted(
        ((score(entry, words, phrase), index, entry) for index, entry in enumerate(listed)),
        key=lambda found: (-found[0], found[1]),
    )
    best = ranked[0][0]
    results = [entry.hit() for value, _index, entry in ranked if value > 0 and value >= best * RELEVANT][:MAX_RESULTS]
    named = [code for code in dict.fromkeys(re.findall(r"[a-z_]+", query.casefold())) if code in CODES]
    errors = {"errors": [{"error": code, "next": NEXT[code]} for code in named]} if named else {}
    if results:
        return {"query": query, "results": results, **errors}
    groups = sorted({entry.command.split(" ", 1)[0] for entry in listed})
    return {
        "query": query,
        "results": [],
        "hint": (
            "No command matches. Search again with one plain word (for example task, invite, member, device, cost, "
            "github, whiteboard), or read a topic of the guide: `agent-platform guide --topic <topic>`."
        ),
        "commands": groups,
        "topics": [name for name, _text in sections()],
        **errors,
    }


def _slug(heading: str) -> str:
    return "-".join(tokens(re.sub(r"^\d+[a-z]?\.\s*", "", heading))) or "intro"


def sections() -> list[tuple[str, str]]:
    """The sections of the full guide as (topic, text); the text before the first section is `intro`."""
    text = resources.files("agent_platform").joinpath("guide.md").read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", text)
    return [(_slug(part.splitlines()[0][3:]) if part.startswith("## ") else "intro", part) for part in parts]


def guide(topic: str | None) -> dict[str, Any]:
    """The short start (the guide's text before its first section), the sections matching `topic` (`all` for the
    whole guide), or the topics when none matches."""
    listed = sections()
    names = [name for name, _text in listed]
    if topic is None:
        return {"guide": listed[0][1], "topics": names}
    if fold(topic.strip()) == "all":
        return {"topic": "all", "guide": "".join(text for _name, text in listed), "topics": names}
    wanted = tokens(topic)
    chosen = [(name, text) for name, text in listed if wanted and all(_known(word, name) for word in wanted)]
    if not chosen:
        return {"topic": topic, "guide": None, "topics": names, "hint": "Name one of `topics`, or search a command."}
    return {"topic": ",".join(name for name, _t in chosen), "guide": "".join(t for _n, t in chosen), "topics": names}


def _known(word: str, topic: str) -> bool:
    return any(similar(word, part) > 0 for part in topic.split("-"))
