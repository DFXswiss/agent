"""Write routes whose audit is the request pipeline's generic row `request.write` (ADR-063 section 1).

A route belongs here only when a success may leave nothing worth a row of its own:

* it creates a short-lived challenge: the WebAuthn `begin` ceremonies and `POST /v1/recovery/finish` (the proof opens
  the registration ceremony; the recovery itself is audited when that registration finishes, `auth.recovery finish`);
* it carries data whose own records are their trace: `POST /v1/device/outbox` (telemetry items - usage, metrics,
  quotas, VM states - stored with the outbox receipt; events, transitions and audit items write their own rows) and
  `POST /v1/device/requests/{id}/responses` (encrypted response parts; the state changes write `device_request.*`).

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
    }
)
