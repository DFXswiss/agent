"""One page of a GitHub list for the listing operation types (KOR-25; 03-api.md section 9, `contracts/ops/objects.json`
`result_github_list_issues`, `result_github_list_pull_requests`, `result_github_get_issue`).

The listing types read exactly **one** GitHub page per operation (`per_page`, `page`), never the whole list, so the
result stays bounded and the session pages on with `next_page`:

- `rows(answer)`: the JSON array of the answer (anything else is `upstream_error`);
- `next_page(headers)`: the page number of the `Link: <...>; rel="next"` header - only the number is taken from the
  link, never its URL; a missing link, a link without a decimal `page` or a page outside 2-1000 (the contract's range)
  is `None`;
- `fit(result, key)`: drops the last entries of `result[key]` while the result is larger than `RESULT_BUDGET` bytes
  of body JSON (the result travels in one encrypted event, as `github.get_pull_request`), each drop sets `truncated`.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from agent_runner.crypto.encoding import body_json
from agent_runner.github import ops
from agent_runner.github.client import GitHubResponse

RESULT_BUDGET = 200 * 1024
MAX_PAGE = 1000
_LINK_NEXT_RE = re.compile(r'<([^>]*)>\s*;\s*rel="next"')
_PAGE_RE = re.compile(r"^[0-9]{1,4}$")


def rows(answer: GitHubResponse) -> list[Any]:
    if not isinstance(answer.data, list):
        raise ops.invalid()
    return answer.data


def next_page(headers: httpx.Headers) -> int | None:
    match = _LINK_NEXT_RE.search(headers.get("link", ""))
    if match is None:
        return None
    values = parse_qs(urlsplit(match.group(1)).query).get("page", [])
    if len(values) != 1 or not _PAGE_RE.fullmatch(values[0]):
        return None
    page = int(values[0])
    return page if 2 <= page <= MAX_PAGE else None


def fit(result: dict[str, Any], key: str) -> None:
    entries: list[Any] = result[key]
    while entries and len(body_json(result)) > RESULT_BUDGET:
        entries.pop()
        result["truncated"] = True
