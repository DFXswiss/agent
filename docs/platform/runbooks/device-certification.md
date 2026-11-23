# Runbook: device enrollment, certification and decertification

Scope: execution devices (runner devices) of two kinds ([00 §9](../spec/00-overview.md#9-glossary)):
**platform devices** provided by DFX and certified in the platform log
([04 §5.9](../spec/04-crypto.md#59-platform-device-certification-and-assignment), ADR-023, ADR-026), and
**org devices** owned by an organization and enrolled with an org device invitation
([04 §5.8](../spec/04-crypto.md#58-org-device-enrollment), [05 §2.1](../spec/05-runner.md#21-enrollment)).
Prerequisite on the device: the runner is installed ([runner-installation.md](runner-installation.md)) with
`AGENT_RUNNER_HUB_URL` and `AGENT_RUNNER_PLATFORM_ROOT_KEY` configured.

## 1. Platform device

1. **Operator** (root key, [platform-root-key.md](platform-root-key.md)):
   `agent-hub platform invite-device --key-file <key> --hub https://<hub> [--expires-hours 24]` → prints the code.
   Hand it to the person installing the device through a confidential channel.
2. **Runner device**, as the runner account: `agent-runner enroll --platform --hub https://<hub> --code <code>`.
   Output `runner.enroll state=pending_certification kind=platform device_id=<id>`; the code is never printed. The
   device generates its identity (`device.json`, `keys/device.key` 0600 under `AGENT_RUNNER_HOME`), pins the hub key
   from `/v1/config` and verifies the platform log against its configured root key.
3. Start the daemon (`agent-runner service install --profile platform`, or `agent-runner run` in the foreground). Until
   certified it only polls `GET /v1/device/state` every 30 s.
4. **Operator:** `agent-hub platform certify-pending --key-file <key> --hub https://<hub>` certifies every pending
   enrollment whose invitation signature verifies; `--device <id>` (repeatable) limits it to named devices. Compare the
   `device_id` with the one the installer reported in step 2 before certifying.
5. **Check:** on the device `agent-runner doctor` reports
   `hub ok active`. Private organizations get the device assigned automatically when it is the best candidate; other
   organizations need platform access (`grant-org-access --org <id>`, ADR-026) and an admin who assigns it to a
   workgroup in the web UI (Devices).

## 2. Org device

1. **Org admin**, in the web UI: Devices → invite a device → copy the one-time code (the browser builds the
   invitation package; the hub sees only the derived `invite_id`).
2. **Runner device:** `agent-runner enroll --hub https://<hub> --code <code>` (without `--platform`). The device opens the
   package, verifies the membership log, pins the hub key from the package and appends its `accept`. A failure leaves
   no partial identity.
3. Start the daemon with `agent-runner service install --profile org` (mailbox login automation off).
4. **Org admin:** assign the device to workgroups (Devices); the browser wraps the workgroup and org keys for it.

## 3. Decertification and removal

| Case | Action | Effect |
|---|---|---|
| Platform device retired or suspected compromised | `agent-hub platform decertify --key-file <key> --hub https://<hub> --device <id>` | appends `device_decertify`; the hub refuses the device at once; the runner, once the refusal is confirmed for 60 s, stops all sessions, deletes its workgroup VMs and all scope keys and records `device_revoked` (doctor `sync device_revoked`) |
| Org device removed by an org | org admin unassigns or removes the device (Devices); browsers then send the rotation batch automatically ([06 §11](../spec/06-web.md#11-background-jobs-corejobs) job 4) | the device loses its workgroups; it deletes the VMs and the scope keys of that org |
| Device taken out of service | after decertification/removal, on the device: `agent-runner uninstall` | removes the service, every VM the runner created, the store and (without `--keep-identity`) the identity |

After a decertification every organization that had the device assigned must rotate the keys the device held
(`rotation_pending`); admins' browsers do it at the next sign-in. Content the device already read stays known to it
(04 §9). Do not re-certify a decertified identity: uninstall and enroll the device anew with a new invitation.

## 4. Faults

| Symptom | Cause | Remedy |
|---|---|---|
| `enroll` refused `invite_code_invalid` / `invite_expired` | wrong or expired code | new invitation |
| `certify-pending` certifies nothing | enrollment not received, or invitation signature invalid | check `agent-runner status` on the device; re-enroll with a new code |
| doctor `hub fail platform_root_mismatch` | the device's `AGENT_RUNNER_PLATFORM_ROOT_KEY` differs from the hub's | fix the configuration (never the hub's key) |
| doctor `sync fail platform_stopped` | the platform log failed verification (rollback or fork) | treat as hub incident; after the cause is understood `agent-runner doctor --clear platform_stopped`, restart the daemon |
| doctor `sync fail device_revoked` | device decertified or key revoked | uninstall; not clearable |
