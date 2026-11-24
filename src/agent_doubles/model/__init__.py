"""The model double: a deterministic stand-in for the Anthropic Messages API (docs/platform/spec/07-testing.md §9).

Production version of the Spike C prototype. Claude Code (directly with ``ANTHROPIC_BASE_URL``, or behind the guest
model gateway) is pointed at the double; each ``POST /v1/messages`` is checked against the next step of a scenario
script and answered with that step's reply, streamed as server-sent events exactly as the real API does, or with a
scripted error. Every request is journaled; every deviation from the script, unknown route and wrong credential is a
failure, so ``GET /_double/state`` reports ``ok`` only for a run that went exactly as scripted.

- `scenario`: the script format, loading (files and packaged scenarios), request matching and template variables.
- `wire`: response bodies: messages, server-sent events, errors, usage and OpenRouter extras.
- `access`: the credential check per access kind (API key, OpenRouter, subscription).
- `keys`: the OpenRouter key management API (``/v1/keys``) and the limited keys it creates (ADR-081 section 7).
- `server`: the HTTP server and the control routes under ``/_double/``.
- `journal`: the per-route summary of what Claude Code sent (Claude Code upgrade guard).
- ``scenarios/``: example scenarios and the adversary scenarios ``atk-01`` … ``atk-32``.

Standard library only, so the double runs anywhere Claude Code runs, including inside a VM.
"""

from __future__ import annotations

from agent_doubles.model.access import Access
from agent_doubles.model.scenario import Scenario, ScenarioError, load_scenario, packaged_scenario, parse_scenario
from agent_doubles.model.server import ModelDouble

__all__ = ["Access", "ModelDouble", "Scenario", "ScenarioError", "load_scenario", "packaged_scenario", "parse_scenario"]
