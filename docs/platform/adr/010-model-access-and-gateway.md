# ADR-010: Model access and model gateway (KONZEPT §4, §14.4)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §4: Claude Code only; users may store a Claude subscription or an Anthropic API key; without own credentials and hardware DFX servers and DFX model access via OpenRouter with DeepSeek as default; compatibility to be checked.
- Spike C (Claude Code 2.1.330): all 12 tested DeepSeek models work via OpenRouter without proxy or patch (tools, MCP, multi-turn, 100k context, errors). `deepseek/deepseek-v4.1-flash-1017`: full tool turn 0.13 ¢, 100k question 1.5 ¢. Claude Code's own cost figures use Opus prices for unknown models (30–40× too high); real cost arrives as `usage.cost` in the OpenRouter stream. Subscription quotas only via `GET /api/oauth/usage` (unofficial).
- Spike B: the session sandbox has no network except port 7700; credentials must not enter the sandbox.

## Decision
- Access kinds per session: `subscription`, `api_key`, `platform` (DFX OpenRouter key, only on platform devices). Selection: user preference (`auto` = subscription → API key → platform), credential availability on the device.
- Default platform model: `deepseek/deepseek-v4.1-flash-1017` (fixed snapshot, not a `~latest` alias); all Claude Code model slots set to it.
- The guest **model gateway** (`/model/…` on 127.0.0.1:7700) authenticates the session token, injects the provider credential (API key → `x-api-key`; OpenRouter → Bearer; subscription → Bearer + `anthropic-beta: oauth-2025-04-20`), enforces the model allow-list (refused otherwise), checks the provider allow-list for OpenRouter by audit only (`model.provider_outside_allowlist`, never blocked), and records usage per request. Credentials live only in the gateway's memory.
- Cost: OpenRouter = stream `usage.cost` (`provider`); API key = list price from the runner model catalog; subscription = list-price equivalent, flagged. Context % = last request's input + cache tokens / catalog context window.
- Quotas: subscription usage endpoint polled every 900 s (≥ 300 s); OpenRouter key spend via `/api/v1/key`.
- OpenRouter upstream routing varies per request (11 providers observed): the platform key must carry an OpenRouter guardrail (ZDR providers / allow-list / budget); every response's `provider` is recorded and audited, out-of-list providers raise a warning (audited, counted, not blocked).
- Subscription setup tokens: whether the OAuth usage endpoint accepts them is unverified; quota polling and the account email are therefore optional and feature-detected (UI: "not available"). The gateway path with a subscription token is verified by a LIVE check already in M0 (read-only current access token, never refresh).

## Consequences
- Usage/cost/context are tamper-resistant (gateway, not the session).
- Subscription via gateway injection is unverified with a real subscription token through a proxy; verified in the LIVE test with a read-only access token.
- The usage endpoint may change; failure only hides quotas.

## Alternatives considered
Credentials in the session environment (leak into sandbox), stream-json mode as metrics source (ADR-025), Claude Code's cost fields (wrong for OpenRouter).

## Deviation from DESIGN.md
None (model traffic never passes through the hub; DESIGN §25.18 refusal respected). DESIGN §25.13 broker idea is replaced by a platform key on platform devices.
