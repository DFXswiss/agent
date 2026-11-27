"""Attachments of prompts (ADR-102, 03-api.md sections 4.6 and 6.1).

The hub stores the encrypted files that members attach to prompts (`hub.attachment_object`, `hub.attachment_chunk`)
without any key: `store` holds the rules, `routes` the browser and device API, `jobs` removes stale uploads. The
format and its vectors are `agent_hub.crypto.attachment` (04-crypto.md section 4.14).
"""
