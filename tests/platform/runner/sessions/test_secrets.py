"""U-RUN-hygiene for the session area: the platform key never reaches a log, the outbox, the hub or the store
(05-runner.md sections 10.2, 10.6, 1.2; repository rule: no secrets in output).

A full start (success and a refused credential), a journal with usage and errors, and the push to the rig's hub
are run with a key that carries a marker; afterwards every log line, every outbox body (opened), every item the
hub stored and every row of every table of the runner schema are scanned for the marker.
"""

from __future__ import annotations

import asyncio
import json

from agent_runner.guestlink.errors import GuestRefused
from agent_runner.model.journal import HANDLERS as MODEL_HANDLERS
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import HANDLERS as SESSION_HANDLERS
from agent_runner.sessions.service import SessionService
from agent_runner.vm import records as vm_records

from ..reconcile.rig import Rig, standard_world
from .kit import KEY, TASK, desired, kit, session_id

MARKER = "SECRETMARKER"
AT = "2026-11-21T12:00:05.000Z"
HANDLERS = {**SESSION_HANDLERS, **MODEL_HANDLERS}


def test_the_platform_key_appears_nowhere_but_in_the_credential_request(rig: Rig) -> None:
    assert MARKER in KEY

    async def main() -> None:
        async with rig.env(standard_world()) as env:
            found = await kit(env)
            svc = SessionService(found.session_env, handlers=HANDLERS)
            assert svc.schedule_start(desired(1))
            await svc.idle()
            found.guest.fail["put_credential"] = GuestRefused("credential", 400, "credential_invalid")
            assert svc.schedule_start(desired(2))
            await svc.idle()
            assert (await found.stored(session_id(2))).state == "failed"
            row = await vm_records.get(env.pool, "vm-00000001")
            assert row is not None
            sid = session_id(1)
            guest = found.guest
            guest.emit(sid, "session.state", {"state": "prompt_delivering", "at": AT})
            guest.emit(sid, "session.state", {"state": "prompt_accepted", "prompt_id": TASK, "at": AT})
            guest.emit(sid, "usage", {"request_id": "r", "model": "deepseek/deepseek-v4.1-flash-1017",
                                      "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                                      "cache_write_tokens": 0, "thinking_tokens": 0, "cost_usd": 0.001,
                                      "context_window_tokens": 1048576, "request_bytes": 1, "response_bytes": 1,
                                      "status": 200})  # fmt: skip
            guest.emit(None, "guest.error", {"code": "gateway_upstream"})
            stopping = asyncio.Event()
            task = asyncio.ensure_future(VmJournal(found.session_env, row, handlers=HANDLERS).run(stopping))
            for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
                if guest.acked == guest.records[-1].seq:
                    break
                await asyncio.sleep(0.005)
            stopping.set()
            await task
            await env.sync.pusher.drain()

            # the key went to the guest gateway (and only there)
            sent = [value for name, value in guest.calls if name == "put_credential"]
            assert all(KEY in json.dumps(value) for value in sent)

            texts = [*found.lines, env.log()]
            texts += [json.dumps(body) for _kind, body in await found.outbox()]
            texts += [json.dumps(item) for item in env.hub.items()]
            texts += [request.raw.decode("utf-8", "replace") + json.dumps(request.body) for request in env.hub.requests]
            tables = [row[0] for row in await env.rows(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'runner'")]  # fmt: skip
            for table in tables:
                texts += [str(row[0]) for row in await env.rows(f"SELECT row_to_json(t)::text FROM runner.{table} t")]
            assert len(texts) > 20
            assert not [text for text in texts if MARKER in text]
            requests = [value for name, value in guest.calls if name == "create_session"]
            assert all(MARKER not in json.dumps(value) for value in requests)

    asyncio.run(main())
