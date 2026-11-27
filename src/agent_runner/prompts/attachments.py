"""Files and images attached to a prompt, put into the session's workspace before the prompt is typed (ADR-102
section 5; 04-crypto.md section 4.14; 03-api.md section 6.1 `GET /v1/device/attachments/{object}/chunks/{index}`).

The verified prompt body names each attachment (`agent_runner.crypto.attachment.check_refs`). Before the delivery
types such a prompt (`deliver.Deliverer._send`):

1. `AttachmentFetcher.files` downloads every chunk of every attachment from the hub, checks count, size and digest
   against the reference and decrypts with `WK` of the reference's epoch (`open_attachment`). A hub that cannot be
   reached or answers `unavailable`/`rate_limited` is `RunnerError(attachment_unavailable)`: the delivery backs off
   and tries again. An object the hub does not hold complete (`404`), any other refusal, a chunk that is not b64u, a
   key this device does not hold or an attachment that does not verify or open is `PromptRefused(prompt_unreadable)`:
   the prompt is refused and audited like an unreadable one.
2. `archive` writes the files as a tar into a temporary file: one regular file per attachment, named
   `workspace_name(n, name)` (`<n>-<name>` with the name reduced to `[A-Za-z0-9._-]`), so two files of one prompt
   never collide and no file takes a protected name of the guest's import (`.claude`, `CLAUDE.md`, ...).
3. The guest's file import (`POST /v1/sessions/{id}/files/import`, `replace=true`) puts them under
   `attachments/<event id>/` of the workspace; a repeat after a failed attempt replaces the directory.
4. `with_note` appends one line per file to the prompt's text: its path, media type and size. The agent opens the
   files with its own tools; images too.

Nothing of a file - name, type or content - is logged; the log lines name the event and the number of files.
"""

from __future__ import annotations

import asyncio
import re
import tarfile
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from io import BytesIO
from typing import IO, Any, cast

from agent_runner.crypto import attachment as crypto
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.errors import RunnerError
from agent_runner.prompts.verify import UNREADABLE, PromptRefused
from agent_runner.sync.http import HubRefusal, HubTransportError

ROOT = "attachments"
"""The workspace directory of every prompt's files."""
UNAVAILABLE = "attachment_unavailable"
TRANSIENT_CODES = frozenset({"unavailable", "rate_limited"})
NAME_MAX = 100
"""At most this many characters of a reduced file name (the extension is kept)."""
READ_BYTES = 1024 * 1024
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,10}$")


def directory(event_id: str) -> str:
    """The workspace directory of the files of one prompt."""
    return f"{ROOT}/{event_id}"


def workspace_name(number: int, name: str) -> str:
    """`<number>-<name>` with `name` reduced to `[A-Za-z0-9._-]` (runs of other characters become one `_`, leading
    and trailing dots and underscores go) and cut to `NAME_MAX` characters with its extension kept."""
    reduced = _UNSAFE_RE.sub("_", name).strip("._") or "file"
    if len(reduced) > NAME_MAX:
        found = _EXTENSION_RE.search(reduced)
        extension = "" if found is None else found.group(0)
        reduced = reduced[: NAME_MAX - len(extension)] + extension
    return f"{number}-{reduced}"


def size_text(size: int) -> str:
    """A file size for people: bytes below 1 KiB, else KB or MB with one decimal (1 KB = 1024 bytes)."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def with_note(text: str, event_id: str, refs: Sequence[Mapping[str, Any]]) -> str:
    """The text the agent receives: the prompt's text, then one line per attached file (none for no attachment)."""
    if not refs:
        return text
    lines = [
        f"- {directory(event_id)}/{workspace_name(number, str(ref['name']))} ({ref['type']}, "
        f"{size_text(int(ref['bytes']))})"
        for number, ref in enumerate(refs, start=1)
    ]
    heading = "Attached files (in your workspace):" if len(refs) > 1 else "Attached file (in your workspace):"
    return "\n".join([text.rstrip("\n"), "", heading, *lines])


def archive(files: Sequence[tuple[Mapping[str, Any], bytes]]) -> IO[bytes]:
    """The files as a tar in a temporary file (rewound; the caller closes it)."""
    out = tempfile.TemporaryFile()  # noqa: SIM115 - returned open, the caller closes it
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for number, (ref, data) in enumerate(files, start=1):
            member = tarfile.TarInfo(workspace_name(number, str(ref["name"])))
            member.size = len(data)
            member.mode = 0o600
            tar.addfile(member, BytesIO(data))
    out.seek(0)
    return out


async def stream(source: IO[bytes]) -> AsyncIterator[bytes]:
    """The bytes of an open file in pieces of `READ_BYTES`, read in a worker thread."""
    while data := await asyncio.to_thread(source.read, READ_BYTES):
        yield data


def hub_of(context: Any) -> Callable[[], Any | None]:
    """The device's signed hub client of a daemon context, looked up when it is needed (None while not enrolled)."""

    def found() -> Any | None:
        from agent_runner.sync.runtime import runtime  # noqa: PLC0415 - the sync runtime imports the prompt area

        sync = runtime(context)
        return None if sync is None else sync.client

    return found


class AttachmentFetcher:
    """Downloads, checks and opens the attachments of a prompt (module docstring, step 1). `hub()` is the device's
    signed hub client (`request(method, target)`), None while the device is not enrolled (then: try again later)."""

    def __init__(self, hub: Callable[[], Any | None]) -> None:
        self.hub = hub

    async def _chunk(self, object_id: str, index: int) -> bytes:
        client = self.hub()
        if client is None:
            raise RunnerError(UNAVAILABLE)
        try:
            answer = await client.request("GET", f"/v1/device/attachments/{object_id}/chunks/{index}")
        except HubRefusal as refused:
            if refused.code in TRANSIENT_CODES:
                raise RunnerError(UNAVAILABLE) from None
            raise PromptRefused(UNREADABLE) from None
        except HubTransportError:
            raise RunnerError(UNAVAILABLE) from None
        try:
            return b64u_decode(cast(Mapping[str, object], answer).get("ct"))
        except (CryptoError, AttributeError):
            raise PromptRefused(UNREADABLE) from None

    async def files(
        self, refs: Sequence[Mapping[str, Any]], keys: Mapping[int, bytes | None]
    ) -> list[tuple[Mapping[str, Any], bytes]]:
        """`(reference, file)` of every attachment; `keys` maps each epoch the references name to `WK` of it (None:
        not held by this device)."""
        found = []
        for ref in refs:
            key = keys.get(int(ref["epoch"]))
            if key is None:
                raise PromptRefused(UNREADABLE)
            object_id = str(ref["object"])
            chunks = [await self._chunk(object_id, index) for index in range(crypto.chunk_count(int(ref["bytes"])))]
            try:
                data = await asyncio.to_thread(crypto.open_attachment, key, dict(ref), chunks)
            except CryptoError:
                raise PromptRefused(UNREADABLE) from None
            found.append((ref, data))
        return found
