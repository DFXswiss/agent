"""Write routes whose audit is the request pipeline's generic row `request.write` (ADR-063 section 1).

A route belongs here only when a success may leave nothing worth a row of its own:

* it creates a short-lived challenge: the WebAuthn `begin` ceremonies and `POST /v1/recovery/finish` (the proof opens
  the registration ceremony; the recovery itself is audited when that registration finishes, `auth.recovery finish`);
* it carries data whose own records are their trace: `POST /v1/device/outbox` (telemetry items - usage, metrics,
  quotas, VM states - stored with the outbox receipt; events, transitions and audit items write their own rows) and
  `POST /v1/device/requests/{id}/responses` (encrypted response parts; the state changes write `device_request.*`), and
  `PUT /v1/device/contexts/{object}/chunks/{index}` (sealed chunks of a context checkpoint, possibly thousands per
  checkpoint; the commit writes `session.context_saved`, a quota refusal `session.context_refused`, ADR-072),
  `PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}` (sealed chunks of a file attached to a prompt, up to
  32 per file; the commit writes `attachment.stored`, ADR-102), and
  `POST /v1/device/sessions/{session}/browser/live` (the latest live frame of a watched platform browser, up to five
  per second and replaced by the next; who watches is audited as `read.stream.browser`, KOR-44);
* it only marks the caller: joining the community and its read position (`POST /v1/community/join`,
  `PUT /v1/community/read`, KOR-26); the posts, deletions, keys and moderation write their own `community.*` rows;
* it is anonymous UI health telemetry: `POST /v1/telemetry/ui` adds to daily counters that name nobody (ADR-086
  section 5); a row of its own would add nothing but a trace of the sender.

An idempotent repeat of any other route is declared by its handler (`agent_hub.audit.recorder.unchanged`). For every
other successful write a request without a row of its own is a defect (`agent_hub.http.writelog`); the pipeline's
generic row still records it.
"""

from __future__ import annotations

GENERIC: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/passkeys/register/begin"),
        ("POST", "/v1/passkeys/login/begin"),
        ("POST", "/v1/recovery/finish"),
        ("POST", "/v1/device/outbox"),
        ("POST", "/v1/device/requests/{id}/responses"),
        ("POST", "/v1/community/join"),
        ("PUT", "/v1/community/read"),
        ("PUT", "/v1/device/contexts/{object}/chunks/{index}"),
        ("PUT", "/v1/workgroups/{wg}/attachments/{object}/chunks/{index}"),
        ("POST", "/v1/device/sessions/{session}/browser/live"),
        ("POST", "/v1/telemetry/ui"),
    }
)
