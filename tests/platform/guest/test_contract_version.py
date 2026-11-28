"""U-GST-contract-version: the contracts a guest validates against belong to its version (HOT-23).

A guest checks the runner's answers and requests against the contracts bundled into its golden and refuses what they
do not allow (`request_invalid`); the tool call of the session then times out. On 2026-11-26 `get_context` failed
that way on every VM of the previous golden: KOR-52 added `agent` and `children` to the answer, the guest version
stayed 0.4.0, and the runner could not tell the old guests from the new ones.

So a change to the guest's contracts raises `agent_guest.__version__`, and the runner gives new fields only to guests
from that version (`agent_runner.subagents.guest_features`). The guest's contracts are every bundled file below
`contracts/guest/` and `contracts/mcp/` and every part of another bundled file they reference (`$ref`, followed
transitively): a new definition in `common.json` that no guest contract uses changes nothing. `DIGESTS` holds the
digest of those contracts per guest version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import agent_guest
from agent_runner.vm import golden

DIGESTS = {
    "0.5.0": "65bdd75600df0600fde3252bfd8e0ad2ef1abe4d35defc0a8d36d67de11c0d3f",
    "0.6.0": "bec16234c889a7b4ebff0d81c67707186d46421cb99c719ff80924540d98a8ed",  # KOR-53: the analysis reads
    "0.7.0": "5e4bd6d934bc0900b8e8f15b0aa97cedcc4f35716af57ec4dd88be887e127c25",  # KOR-74: the operation mail.find
    "0.8.0": "42a9b683e8b84fe28145f2774e73ae4264bd91d3b41d0717eb882eb9c96d9ea0",  # HOT-33: repo_read_only, fork
}
"""Guest version -> SHA-256 over the sorted `<part> NUL <sha256 of its canonical JSON>` lines of `guest_contracts()`."""

WHOLE = ("guest", "mcp")


def _refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def _part(document: Any, pointer: str) -> Any:
    for raw in pointer.strip("/").split("/") if pointer.strip("/") else []:
        token = raw.replace("~1", "/").replace("~0", "~")
        document = document[int(token)] if isinstance(document, list) else document[token]
    return document


def guest_contracts() -> dict[str, bytes]:
    """`contracts/<file>` (a whole file below `WHOLE`) or `contracts/<file>#<pointer>` (a referenced part of another
    bundled file) -> its canonical JSON."""
    source = golden.load_source()
    bundled = {path.resolve(): name for name, path in golden._bundle_members(source) if name.startswith("contracts/")}
    whole = {path for path, name in bundled.items() if name.split("/")[1] in WHOLE}
    documents: dict[Path, Any] = {}
    parts: dict[str, bytes] = {}
    todo: list[tuple[Path, str]] = [(path, "") for path in whole]
    while todo:
        path, pointer = todo.pop()
        key = bundled[path] + (f"#{pointer}" if pointer else "")
        if key in parts:
            continue
        if path not in documents:
            documents[path] = json.loads(path.read_text(encoding="utf-8"))
        value = _part(documents[path], pointer)
        parts[key] = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        for ref in _refs(value):
            file_part, _, target_pointer = ref.partition("#")
            target = (path.parent / file_part).resolve() if file_part else path
            if target in bundled:
                todo.append((target, "" if target in whole else target_pointer))
    return parts


def contracts_digest() -> str:
    digest = hashlib.sha256()
    for key, data in sorted(guest_contracts().items()):
        digest.update(f"{key}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    return digest.hexdigest()


def test_the_guest_contracts_follow_references_into_other_files() -> None:
    keys = set(guest_contracts())
    assert {"contracts/guest/pending-answer.request.json", "contracts/mcp/objects.json"} <= keys
    assert "contracts/common.json#/$defs/uuid" in keys  # a referenced part of a file outside guest/ and mcp/
    assert "contracts/common.json" not in keys  # never the whole file: a new, unused definition changes nothing
    assert not any(key.startswith(("contracts/tools/", "contracts/changes/")) for key in keys)  # never bundled


def test_a_json_pointer_reads_objects_lists_and_escapes() -> None:
    document = {"$defs": {"a/b": [{"c~d": 1}]}}
    assert _part(document, "") is document
    assert _part(document, "/$defs/a~1b/0/c~0d") == 1


def test_the_guest_contracts_belong_to_the_guest_version() -> None:
    found = contracts_digest()
    assert DIGESTS.get(agent_guest.__version__) == found, (
        f"The contracts the guest validates against changed (digest {found}), agent_guest.__version__ is "
        f"{agent_guest.__version__}. A guest of an older golden refuses what its contracts do not allow, so: raise "
        "agent_guest.__version__, give new fields only to guests from that version "
        "(agent_runner.subagents.guest_features), and record the digest under the new version in DIGESTS."
    )
