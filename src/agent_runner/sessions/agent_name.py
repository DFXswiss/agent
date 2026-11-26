"""The automatic name of a main agent (ADR-095 section 1, rule and cases `contracts/tasks/agent-name.json`).

A main agent without a verified `task.agent_name` event, or whose newest one names null, is called after its task's
current title: white space runs collapsed to one space, the first five words, at most 40 code points cut at a space,
`Agent` when nothing is left. The browser, the agent tool and the runner apply the same rule; the agent tool imports
this function (it already builds on `agent_runner`), so there is one Python copy of the rule.
"""

from __future__ import annotations

import re

MAX_WORDS = 5
MAX_CODE_POINTS = 40
EMPTY = "Agent"
# The Unicode White_Space property, the set of `contracts/tasks/title.json`.
_SPACES = re.compile("[\t\n\x0b\x0c\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")


def automatic_name(title: str) -> str:
    """The automatic name of a main agent whose task is titled `title`."""
    text = _SPACES.sub(" ", title).strip(" ")
    name = " ".join(text.split(" ")[:MAX_WORDS])
    if len(name) > MAX_CODE_POINTS:
        head = name[: MAX_CODE_POINTS + 1]
        cut = head.rfind(" ")
        name = head[:cut] if cut > 0 else name[:MAX_CODE_POINTS]
    return name or EMPTY
