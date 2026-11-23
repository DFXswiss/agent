"""Prompt registration on the control socket and the prompt-origin op of ``api.sock`` (05-runner.md section 5.4).

* ``POST /v1/sessions/{id}/prompts {prompt_id, text}`` -> ``{"state": "registered"}``: the only way a prompt hash
  enters ``prompt_registry`` (05 section 4.6 no. 7). Unknown session ``404 not_found``; stopped or failed session,
  or a ``prompt_id`` that was already accepted, ``409 state_conflict``; the same ``prompt_id`` with another text
  ``409 id_conflict``. The registered prompt is then queued for delivery into the TUI (``delivery.py``, GST-06); the
  result arrives through the journal (``session.state`` ``prompt_accepted`` / ``prompt_refused``).
* op ``prompt.check {session_id, prompt: string | null} -> {accepted}`` - the UserPromptSubmit decision of the API
  process (``prompts.PromptRegistry.check``); ``null`` stands for a hook body that could not be read (refused).
* ``SESSION_EVENTS["agent_starting"]`` - the TUI-ready watcher of ``delivery.py`` (``prompt_delivering`` and the
  deferred initial prompt).
* ``STARTUP`` - after a restart of the control process the TUI-ready watcher of every ``agent_starting`` session is
  started again (KOR-02). Queued prompts are lost with the process; the runner sends a prompt that was handed over but
  has no result again (RUN-05 decision 9), and the registry's ``prompt_id`` keying makes that safe.
"""

from __future__ import annotations

from typing import Any

from agent_guest import delivery
from agent_guest.control import ControlContext
from agent_guest.prompts import PromptRegistry
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import StateError


def register(context: ControlContext, request: Request) -> Response:
    registry = PromptRegistry(context.journal, context.state)
    try:
        registry.register(request.params["id"], request.data["prompt_id"], request.data["text"])
    except StateError as error:
        raise HttpError(error.code) from None
    delivery.manager(context).submit(request.params["id"], request.data["prompt_id"], request.data["text"])
    return json_response({"state": "registered"})


def check(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id, prompt = args.get("session_id"), args.get("prompt")
    if set(args) != {"session_id", "prompt"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    if prompt is not None and not isinstance(prompt, str):
        raise StateError("request_invalid")
    return {"accepted": PromptRegistry(context.journal, context.state).check(session_id, prompt)}


ROUTES = [
    Route(
        "POST",
        "/v1/sessions/{id}/prompts",
        register,
        request_schema="guest/session-prompts.request.json",
        path_params={"id": "common.json#/$defs/uuid"},
    )
]
API_OPS = {"prompt.check": check}


def agent_starting(context: ControlContext, session_id: str) -> None:
    delivery.manager(context).agent_starting(session_id)


def startup(context: ControlContext) -> None:
    for session in context.state.sessions():
        if session.state == "agent_starting":
            delivery.manager(context).agent_starting(session.id)


STARTUP = startup
SESSION_EVENTS = {"agent_starting": agent_starting}
