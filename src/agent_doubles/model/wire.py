"""Response bodies of the Messages API: messages, server-sent events, errors, usage and OpenRouter extras.

Ported unchanged in its byte output from the Spike C prototype (``modeldouble/wire.py``), whose shapes Claude Code
2.1.330 accepted in the recorded runs. Shapes follow the public Anthropic Messages API
(``anthropic-version: 2023-06-01``). Streaming emits ``message_start``, ``ping``, per block
``content_block_start`` / ``content_block_delta`` / ``content_block_stop``, then ``message_delta`` (stop reason and
cumulative usage) and ``message_stop``. Text arrives as ``text_delta`` chunks, tool input as ``input_json_delta``
chunks of the serialised JSON, thinking as ``thinking_delta`` plus one ``signature_delta``.

OpenRouter extras (Spike C, live recordings): the message carries ``provider``; the final usage carries ``cost``,
``is_byok`` and ``cost_details``; the start usage carries no cost.

Pure module: no clock, no network.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

from agent_doubles.model.scenario import ErrorReply, Reply

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
REQUEST_TOKEN_FIELDS = ("system", "messages", "tools")
# Deterministic OpenRouter-like prices in USD per token (the double's own, not any provider's).
PRICE_INPUT = 1e-7
PRICE_CACHE_READ = 1e-8
PRICE_OUTPUT = 4e-7
COST_DIGITS = 12

Event = tuple[str, dict[str, Any]]


def estimate_tokens(value: Any) -> int:
    """A deterministic token estimate: one token per four characters of compact JSON."""
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return max(1, math.ceil(len(text) / 4))


def request_tokens(body: dict[str, Any]) -> int:
    """The estimated input tokens of a request (system, messages and tools)."""
    return estimate_tokens({k: body.get(k) for k in REQUEST_TOKEN_FIELDS})


def resolve_blocks(reply_content: list[dict[str, Any]], tool_id_prefix: str) -> list[dict[str, Any]]:
    """The blocks as the API returns them: tool uses get ids and an input object."""
    out: list[dict[str, Any]] = []
    for index, block in enumerate(reply_content):
        kind = block["type"]
        if kind == "text":
            out.append({"type": "text", "text": block["text"]})
        elif kind == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.get("id") or f"{tool_id_prefix}_{index}",
                    "name": block["name"],
                    "input": block.get("input", {}),
                }
            )
        else:
            out.append(
                {
                    "type": "thinking",
                    "thinking": block["thinking"],
                    "signature": block.get("signature") or f"double-signature-{index}",
                }
            )
    return out


def stop_reason(reply: Reply, blocks: list[dict[str, Any]]) -> str:
    """The scripted stop reason, else ``tool_use`` when a tool is called, else ``end_turn``."""
    if reply.stop_reason is not None:
        return reply.stop_reason
    return "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"


def usage_for(reply: Reply, request: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """The scripted usage; missing fields are estimated from the request and the reply."""
    given: dict[str, Any] = dict(reply.usage or {})
    usage: dict[str, Any] = {
        "input_tokens": given.pop("input_tokens", request_tokens(request)),
        "cache_creation_input_tokens": given.pop("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": given.pop("cache_read_input_tokens", 0),
        "output_tokens": given.pop("output_tokens", estimate_tokens(blocks)),
    }
    usage.update(given)
    return usage


def openrouter_usage(usage: dict[str, Any], cost: float | None) -> dict[str, Any]:
    """`usage` with the OpenRouter cost fields; `cost` fixes the total, else it follows the double's prices."""
    prompt = (usage["input_tokens"] + usage["cache_creation_input_tokens"]) * PRICE_INPUT
    prompt += usage["cache_read_input_tokens"] * PRICE_CACHE_READ
    completion = usage["output_tokens"] * PRICE_OUTPUT
    if cost is not None:
        prompt, completion = cost, 0.0
    total = round(prompt + completion, COST_DIGITS)
    return {
        **usage,
        "cost": total,
        "is_byok": False,
        "cost_details": {
            "upstream_inference_cost": total,
            "upstream_inference_prompt_cost": round(prompt, COST_DIGITS),
            "upstream_inference_completions_cost": round(completion, COST_DIGITS),
        },
    }


def message(
    message_id: str,
    model: str,
    blocks: list[dict[str, Any]],
    stop: str | None,
    usage: dict[str, Any],
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    """A complete ``message`` object; `provider` only for OpenRouter-shaped answers."""
    out: dict[str, Any] = {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }
    if provider is not None:
        out["provider"] = provider
    return out


def _chunks(text: str, size: int) -> Iterator[str]:
    if not text:
        yield ""
        return
    for start in range(0, len(text), size):
        yield text[start : start + size]


def _block_events(index: int, block: dict[str, Any], chunk_size: int) -> list[Event]:
    kind = block["type"]
    deltas: list[dict[str, Any]]
    if kind == "text":
        empty: dict[str, Any] = {"type": "text", "text": ""}
        deltas = [{"type": "text_delta", "text": c} for c in _chunks(block["text"], chunk_size)]
    elif kind == "tool_use":
        empty = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        payload = json.dumps(block["input"], separators=(",", ":"))
        deltas = [{"type": "input_json_delta", "partial_json": c} for c in _chunks(payload, chunk_size)]
    else:
        empty = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [{"type": "thinking_delta", "thinking": c} for c in _chunks(block["thinking"], chunk_size)]
        deltas.append({"type": "signature_delta", "signature": block["signature"]})
    events: list[Event] = [
        ("content_block_start", {"type": "content_block_start", "index": index, "content_block": empty})
    ]
    events.extend(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": d}) for d in deltas)
    events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    return events


def stream_events(
    message_id: str,
    model: str,
    blocks: list[dict[str, Any]],
    stop: str,
    usage: dict[str, Any],
    *,
    chunk_size: int,
    start_usage: dict[str, Any] | None = None,
    provider: str | None = None,
) -> list[Event]:
    """The events of one streamed message, in order.

    `start_usage` is the usage of ``message_start`` (default: `usage` with ``output_tokens`` 1).
    """
    first = dict(start_usage if start_usage is not None else usage, output_tokens=1)
    events: list[Event] = [
        (
            "message_start",
            {"type": "message_start", "message": message(message_id, model, [], None, first, provider=provider)},
        ),
        ("ping", {"type": "ping"}),
    ]
    for index, block in enumerate(blocks):
        events.extend(_block_events(index, block, chunk_size))
    events.append(
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": usage},
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return events


def encode_event(name: str, data: dict[str, Any]) -> bytes:
    """One server-sent event."""
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def error_body(error_type: str, text: str, request_id: str) -> dict[str, Any]:
    """The JSON body of an API error."""
    return {"type": "error", "error": {"type": error_type, "message": text}, "request_id": request_id}


def error_for(error: ErrorReply, request_id: str) -> dict[str, Any]:
    """The body of a scripted error."""
    return error_body(error.type, error.message, request_id)
