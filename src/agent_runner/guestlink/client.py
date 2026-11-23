"""HTTP/1.1 client of the guest control protocol (docs/platform/spec/03-api.md section 7, 05-runner.md section 3.2).

Transport: the Unix socket `<lima instance dir>/sock/agent-guest.sock` that Lima forwards from the guest's
`/run/agent-guest/control.sock` (golden template). Only the host connects. Every request carries
`Authorization: Bearer <control token>`; bodies are JSON except the tar streams of the file endpoints.

| Method | Endpoint |
|---|---|
| `health()` | `GET /v1/health` -> `Health` |
| `assign(vm_id, org_id, wg_id)` | `POST /v1/assign` |
| `create_session(request)` | `POST /v1/sessions` (request built by the session start, RUN-04) |
| `put_credential(session_id, credential)` | `PUT /v1/sessions/{id}/credential` |
| `register_prompt(session_id, prompt_id, text)` | `POST /v1/sessions/{id}/prompts` |
| `control(session_id, action)` | `POST /v1/sessions/{id}/control` |
| `import_files(session_id, dir, data, replace=)` | `POST /v1/sessions/{id}/files/import` (tar body) |
| `export_files(session_id, dir, write, paths=)` | `POST /v1/sessions/{id}/files/export` (tar chunks to `write`) |
| `ping(session_id, ping)` | `POST /v1/sessions/{id}/pings` |
| `journal(after, limit=, wait=)` | `GET /v1/journal` (long poll) -> `JournalPage` |
| `ack(through)` | `POST /v1/journal/ack` |
| `answer_pending(pending_id, answer)` | `POST /v1/pending/{id}/answer` |
| `metrics()` | `GET /v1/metrics` |

Failures raise the classes of `guestlink.errors`. Path segments (session and pending IDs) are checked before they
are put into a URL. The token is only ever sent in the header, never logged and never part of an error.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from agent_runner.guestlink.errors import GuestProtocolError, GuestRefused, GuestUnauthorized, GuestUnreachable

DEFAULT_TIMEOUT_S = 10.0
# Long poll: the guest waits at most 25 s (03-api.md section 7.1); the read limit adds a margin for the answer.
JOURNAL_WAIT_MAX_S = 25
JOURNAL_MARGIN_S = 10.0
DEFAULT_JOURNAL_LIMIT = 200
FILES_TIMEOUT_S = 600.0
BASE_URL = "http://agent-guest"
HEALTH_STATES = frozenset({"unassigned", "assigned", "broken"})
CONTROL_ACTIONS = frozenset({"interrupt", "stop", "suspend"})
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class SessionState:
    session_id: str
    state: str


@dataclass(frozen=True)
class Health:
    """`GET /v1/health` (guest/health.response.json)."""

    state: str
    guest_version: str
    claude_version: str
    landlock_abi: int
    boot_id: str
    org_id: str | None
    wg_id: str | None
    sessions: tuple[SessionState, ...]

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> Health:
        state, sessions = data.get("state"), data.get("sessions")
        strings = [data.get(key) for key in ("guest_version", "claude_version", "boot_id")]
        optional = [data.get(key) for key in ("org_id", "wg_id")]
        abi = data.get("landlock_abi")
        if (
            state not in HEALTH_STATES
            or not all(isinstance(value, str) for value in strings)
            or not all(value is None or isinstance(value, str) for value in optional)
            or type(abi) is not int
            or not isinstance(sessions, list)
        ):
            raise GuestProtocolError("health")
        parsed = []
        for item in sessions:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("session_id", "state")):
                raise GuestProtocolError("health")
            parsed.append(SessionState(session_id=item["session_id"], state=item["state"]))
        return cls(
            state=str(state),
            guest_version=str(strings[0]),
            claude_version=str(strings[1]),
            boot_id=str(strings[2]),
            landlock_abi=abi,
            org_id=optional[0],
            wg_id=optional[1],
            sessions=tuple(parsed),
        )


@dataclass(frozen=True)
class JournalRecord:
    """One journal record (03-api.md section 7.2); `body` is the kind's JSON object."""

    seq: int
    ts: str
    session_id: str | None
    kind: str
    body: Mapping[str, Any]


@dataclass(frozen=True)
class JournalPage:
    records: tuple[JournalRecord, ...]
    head: int


def _record(item: object) -> JournalRecord:
    if not isinstance(item, dict):
        raise GuestProtocolError("journal")
    seq, ts, session, kind, body = (item.get(k) for k in ("seq", "ts", "session_id", "kind", "body"))
    if (
        type(seq) is not int
        or not isinstance(ts, str)
        or not (session is None or isinstance(session, str))
        or not isinstance(kind, str)
        or not isinstance(body, dict)
    ):
        raise GuestProtocolError("journal")
    return JournalRecord(seq=seq, ts=ts, session_id=session, kind=kind, body=body)


def _segment(value: str, operation: str) -> str:
    if not _SEGMENT_RE.fullmatch(value):
        raise GuestProtocolError(operation)
    return value


class GuestClient:
    """Control calls to one guest; use as `async with GuestClient(path, token) as guest:` or call `aclose()`."""

    def __init__(
        self,
        socket_path: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._client = httpx.AsyncClient(
            transport=transport if transport is not None else httpx.AsyncHTTPTransport(uds=socket_path),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def __aenter__(self) -> GuestClient:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- transport ---------------------------------------------------------------------------------------------

    @staticmethod
    async def _check(operation: str, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 401:
            raise GuestUnauthorized(operation)
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            body = None
        raise GuestRefused(operation, response.status_code, body.get("error") if isinstance(body, dict) else None)

    async def _request(self, operation: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise GuestUnreachable(operation) from None
        await self._check(operation, response)
        return response

    async def _json(self, operation: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(operation, method, path, **kwargs)
        try:
            value = response.json()
        except ValueError:
            raise GuestProtocolError(operation) from None
        if not isinstance(value, dict):
            raise GuestProtocolError(operation)
        return value

    # -- endpoints ---------------------------------------------------------------------------------------------

    async def health(self, *, timeout: float | None = None) -> Health:
        extra = {} if timeout is None else {"timeout": timeout}
        return Health.parse(await self._json("health", "GET", "/v1/health", **extra))

    async def assign(self, vm_id: str, org_id: str, wg_id: str, *, timeout: float | None = None) -> None:
        extra = {} if timeout is None else {"timeout": timeout}
        body = {"vm_id": vm_id, "org_id": org_id, "wg_id": wg_id}
        await self._json("assign", "POST", "/v1/assign", json=body, **extra)

    async def create_session(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._json("sessions", "POST", "/v1/sessions", json=dict(request))

    async def put_credential(self, session_id: str, credential: Mapping[str, Any]) -> None:
        path = f"/v1/sessions/{_segment(session_id, 'credential')}/credential"
        await self._json("credential", "PUT", path, json=dict(credential))

    async def register_prompt(self, session_id: str, prompt_id: str, text: str) -> dict[str, Any]:
        path = f"/v1/sessions/{_segment(session_id, 'prompts')}/prompts"
        return await self._json("prompts", "POST", path, json={"prompt_id": prompt_id, "text": text})

    async def control(self, session_id: str, action: str) -> None:
        if action not in CONTROL_ACTIONS:
            raise GuestProtocolError("control")
        path = f"/v1/sessions/{_segment(session_id, 'control')}/control"
        await self._json("control", "POST", path, json={"action": action})

    async def ping(self, session_id: str, ping: Mapping[str, Any]) -> None:
        await self._json("pings", "POST", f"/v1/sessions/{_segment(session_id, 'pings')}/pings", json=dict(ping))

    async def import_files(
        self,
        session_id: str,
        directory: str,
        data: bytes | AsyncIterable[bytes],
        *,
        replace: bool = False,
        size: int | None = None,
    ) -> dict[str, Any]:
        """Send a tar stream into `directory` of the session (the guest checks members and size).

        The guest takes no chunked request body: a stream (`AsyncIterable`, sent as it is read, never held in memory -
        decisions KOR-03, AUD-01 A-27) needs its `size` for `Content-Length`.
        """
        headers = {"Content-Type": "application/x-tar"}
        if not isinstance(data, bytes):
            if size is None or size < 0:
                raise ValueError("a streamed import needs its size")
            headers["Content-Length"] = str(size)
        path = f"/v1/sessions/{_segment(session_id, 'files_import')}/files/import"
        return await self._json(
            "files_import",
            "POST",
            path,
            params={"dir": directory, "replace": "true" if replace else "false"},
            content=data,
            headers=headers,
            timeout=FILES_TIMEOUT_S,
        )

    async def export_files(
        self,
        session_id: str,
        directory: str,
        write: Callable[[bytes], object],
        *,
        paths: Sequence[str] | None = None,
    ) -> str:
        """Stream the tar answer chunk by chunk to `write`; returns the `X-Manifest-Sha256` header."""
        path = f"/v1/sessions/{_segment(session_id, 'files_export')}/files/export"
        body: dict[str, Any] = {"dir": directory}
        if paths is not None:
            body["paths"] = list(paths)
        try:
            async with self._client.stream("POST", path, json=body, timeout=FILES_TIMEOUT_S) as response:
                await self._check("files_export", response)
                manifest = str(response.headers.get("X-Manifest-Sha256", ""))
                if not re.fullmatch(r"[0-9a-f]{64}", manifest):
                    raise GuestProtocolError("files_export")
                async for chunk in response.aiter_bytes():
                    write(chunk)
        except httpx.TransportError:
            raise GuestUnreachable("files_export") from None
        return manifest

    async def journal(
        self, after: int, *, limit: int = DEFAULT_JOURNAL_LIMIT, wait: int = JOURNAL_WAIT_MAX_S
    ) -> JournalPage:
        wait = max(0, min(wait, JOURNAL_WAIT_MAX_S))
        data = await self._json(
            "journal",
            "GET",
            "/v1/journal",
            params={"after": str(after), "limit": str(limit), "wait": str(wait)},
            timeout=wait + JOURNAL_MARGIN_S,
        )
        records, head = data.get("records"), data.get("head")
        if not isinstance(records, list) or type(head) is not int:
            raise GuestProtocolError("journal")
        return JournalPage(records=tuple(_record(item) for item in records), head=head)

    async def ack(self, through: int) -> None:
        await self._json("journal_ack", "POST", "/v1/journal/ack", json={"through": through})

    async def answer_pending(self, pending_id: str, answer: Mapping[str, Any]) -> None:
        path = f"/v1/pending/{_segment(pending_id, 'pending')}/answer"
        await self._json("pending", "POST", path, json=dict(answer))

    async def metrics(self) -> dict[str, Any]:
        return await self._json("metrics", "GET", "/v1/metrics")
