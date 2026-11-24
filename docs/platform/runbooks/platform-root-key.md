# Runbook: platform root key (init, safekeeping, use, loss)

Scope: the offline Ed25519 key that signs the platform log ([ADR-023](../adr/023-platform-root-key.md),
[04 §4.8](../spec/04-crypto.md#48-platform-log-new), [04 §5.9](../spec/04-crypto.md#59-platform-device-certification-and-assignment)).
Commands: `agent-hub platform …` (`src/agent_hub/platform/cli.py`). Roles: **operator** (holds the key),
**hub device** (runs the hub), **runner device** (execution device).

## What the key controls

The platform log is the only place where platform devices, platform roles (`inviter`), bootstrap user invitations
and platform access of organizations are decided. Every entry is signed by the root key; the hub stores and enforces
the log but cannot extend it. The public key is configured in three places that must agree:

| Where | Setting |
|---|---|
| Web build | `VITE_PLATFORM_ROOT_KEY` (compiled in; shown on `/security` as fingerprint) |
| Hub | `AGENT_HUB_PLATFORM_ROOT_KEY` (published in `GET /v1/config`) |
| Runner | `AGENT_RUNNER_PLATFORM_ROOT_KEY` |

A mismatch is fatal: the browser stops with `platform_root_mismatch`, the runner refuses to enroll or sync, the
operator CLI refuses the hub.

## 1. Create the key (once, offline)

1. On an operator machine that is not the hub device, with a checkout of `DFXswiss/agent` and its `.venv`:
   `agent-hub platform init --key-file <dir>/platform-root.json` (`python -m agent_hub platform init …` works too).
   The command never overwrites an existing file, writes it with mode 0600 and prints only the **public** key.
2. Put the printed public key into the release configuration: `VITE_PLATFORM_ROOT_KEY` of the web build,
   `AGENT_HUB_PLATFORM_ROOT_KEY` of the hub, `AGENT_RUNNER_PLATFORM_ROOT_KEY` of every runner.
3. After the hub runs with that configuration, append the genesis entry:
   `agent-hub platform init --key-file <dir>/platform-root.json --hub https://<hub>` (appends `root` at seq 0).
   The CLI pins the hub key named by `GET /v1/config` in the operator record `<key file>.state`.

## 2. Safekeeping

- The key file holds the private seed. Keep it **offline** (encrypted removable medium or a password-manager vault
  with hardware-key protection), never on the hub device, never in a repository, ticket or chat.
- Keep **two** encrypted copies in separate places; record who holds them. Losing both is the loss case of §5.
- Keep the operator record `<key file>.state` next to the key. It pins the hub key and the last verified head of the
  platform log, so a hub cannot make the operator sign a fork of a shortened log (`log_rollback`) or talk to a
  replaced hub (`hub_key_changed`). If it is lost, the next command pins the hub key again on first use and the rollback protection starts anew from the log it then sees: verify the hub's key ID and the platform-log head against your records before continuing.
- Copy the key to the operator machine only for a session of commands and delete the working copy afterwards.

## 3. Use (every operator command)

Every command except `init` takes `--key-file` and `--hub`; `--hub` must be `https://` (plain `http://` only for
`localhost`). Each run fetches `/v1/config` (root key must match), fetches and verifies the platform log, appends
the new entries on the verified head and prints one line per result. Exit codes: 0 done, 1 refused (the line names the
code, e.g. `refused: platform_root_mismatch`), 2 usage or key-file error.

| Task | Command | Entry |
|---|---|---|
| Bootstrap user invitation | `invite-user [--expires-hours H]` – prints the code | `user_invite` |
| Platform device invitation | `invite-device [--expires-hours H]` – prints the code | `device_invite` |
| Certify enrolled platform devices | `certify-pending [--device ID …]` | `device_certify` |
| Decertify a platform device | `decertify --device ID` | `device_decertify` |
| Platform access of an org (ADR-026) | `grant-org-access --org ID [--revoke]` | `org_platform_access` |
| Inviter role | `grant-role inviter --user ID`, `revoke-role inviter --user ID` | `role_grant`, `role_revoke` |

The invitation code is printed because the operator hands it on through a confidential channel; the hub sees only
the derived `invite_id`. Device workflows: [device-certification.md](device-certification.md).

### Bootstrap user invitations

Since KOR-01 ([ADR-043](../adr/043-invitation-proof-at-registration.md)) every `user_invite` carries `pk_inv` and
registration needs a proof signed with the key derived from the code, so knowing the `invite_id` (visible in the
platform log) no longer lets anyone else redeem the invitation (AUD-01 A-02 closed). Still hand codes over through a
confidential channel – whoever holds the code is the invitee – and keep the lifetime short (`--expires-hours`, default
72 h). Invitations created by a CLI before KOR-01 carry no `pk_inv` and cannot be used (`invite_unknown`); issue new
ones. There is no revoke command for a platform invitation; an unused one expires.

## 4. Compromise of the key

Treat as an incident. Whoever holds the key can certify rogue platform devices (which then receive workgroup keys of
private organizations), grant `inviter` and grant platform access. Immediately: decertify every platform device you
did not certify yourself (compare the certified devices of the platform log with your records), revoke roles you did not grant, then follow
§5 "re-keying" – a compromised key must not stay in use.

## 5. Loss of the key

There is **no rotation entry** in the platform log (04 §4.8: `root` exists only at seq 0) and the root is compiled into
the web build. Consequences of a loss (no compromise):

- Everything already in the log keeps working: certified devices stay certified, organizations keep their platform
  access, inviters keep their role, members keep working.
- Nothing can be added or taken back: no new platform device, no decertification, no new bootstrap invitation, no
  change of platform access or roles. A platform device that must be removed can then only be shut down physically
  (`agent-runner uninstall`, see [runner-installation.md](runner-installation.md)); organizations rotate their keys
  by removing it from their workgroups (`device_unassign` + rotation, 04 §5.10).

**Re-keying** (after loss or compromise) is a new platform, not a repair:

1. Create a new key (§1) and build a new web release with the new `VITE_PLATFORM_ROOT_KEY` (new build hash,
   [golden-image-release.md](golden-image-release.md) is unaffected).
2. Start a hub with a **new, empty database** and the new `AGENT_HUB_PLATFORM_ROOT_KEY`; append `root`. The old
   platform log cannot be continued under another root, and membership logs of existing organizations reference
   platform-log positions (`device_assign` with `platform_seq`), so existing organizations cannot be moved: users are
   invited again and re-create their organizations; data stays readable only in the old deployment for as long as it
   runs.
3. Re-enroll platform devices with `agent-runner uninstall --keep-identity` is **not** enough – the identity is bound
   to the old log; run `agent-runner uninstall` and enroll anew with the new root key configured.
4. Publish the new build hash and root fingerprint.

This procedure has not been rehearsed end to end (DOC-01 decision 6); rehearse it on a test hub before a production
cutover.
