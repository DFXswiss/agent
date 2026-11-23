# CRY-02 – contract changes

## `device_certify` carries the whole enrollment body (defect fix)

`04-crypto.md` section 3 defines the enrollment text as `P || "enroll\0" || canonical(enrollment body without
enroll_sig and inv_sig)`, where the body of `POST /v1/device/enroll` (`03-api.md` section 6.1) contains
`device_id`, `kind`, `invite_id`, `pub`, `agent_version`, `os`, `arch` and `capacity`. Section 4.8 requires that
`device_certify` copies `enroll_sig` and `inv_sig` "so that browsers can verify them", but the entry had only
`device`, `pub` and `invite_id`: the signed text could not be rebuilt from the platform log.

- `common.json` `platform_body_device_certify`: new required fields `agent_version` (`version_text`), `os`, `arch`
  (`^[a-z0-9_]{1,32}$` as in `api/device-enroll.request.json`) and `capacity` (`capacity`), copied unchanged from the
  enrollment body. The `device_certify` fixtures of `fixtures/log/platform/` (CTR-03) carry the new fields. The verifier rebuilds the
  text from `{device_id: device, kind: "platform", invite_id, pub, agent_version, os, arch, capacity}`
  (`agent_hub.log.platform`, vectors `contracts/vectors/platform-log.json`).
- `api/platform-enrollments.response.json`: each pending enrollment also returns `capacity`, so that the operator CLI
  (`agent-hub platform certify-pending`) can build the entry. Fixtures updated.

The enrollment request schema itself (`device-enroll`, owner CTR-03) must keep exactly these eight signed fields for
platform enrollments.
