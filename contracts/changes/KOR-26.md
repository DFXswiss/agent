# KOR-26 – contract changes

ADR-027 section 3 (additive): the community of all users (`docs/platform/spec/12-community.md`, ADR-061).

- `api/INDEX.json`: 17 endpoints `community-*` (section `12.4`, owner `KOR-26`) with their request and response schemas
  and fixtures; `api/objects.json`: `community_message_row`.
- `common.json`: `community_header`, `community_message` (sealed message, ciphertext at most 16 KiB),
  `community_wrap_text`; the codes `community_muted`, `community_not_member`, `community_epoch_exists`,
  `community_no_key` in `hub_error_code`.
- `enums/error-codes.json`: the four codes above (403, 409, 409, 409).
- `enums/audit-actions.json` and `audit/`: `community.key_create`, `community.key_request`, `community.key_wrap`,
  `community.key_reject`, `community.post`, `community.delete`, `community.mute`, `community.unmute`,
  `community.moderator_grant`, `community.moderator_revoke` and the reads `read.community`, `read.community.messages`,
  `read.community.requests`, `read.community.mutes`, `read.community.moderators` (metadata only, never names or text).
- `ws/INDEX.json`, `ws/sse/objects.json`, `ws/sse/community.json`: the frame `community {change, seq}`.
- `community/vectors.json`: test vectors of the community formats (key check, key request, wraps, message, invalid
  bodies). They are **not** in `vectors/`: runners and VMs never take part in the community (ADR-061 decision 9), so
  no runner section is needed (CON-02 stays unchanged).

Nothing existing changes meaning: old clients ignore the new frame; the runner uses none of the new files.
