# WEB-07 – decisions

Device screen, platform invitations and the platform-access rule in the UI (06-web.md §1.2, §2.4, §7.3, §7.4, §13;
03-api.md §4.2, §4.5, §4.6.2; 04-crypto.md §5.8, §5.9, §5.10, §6.2; ADR-007, ADR-023, ADR-026).

1. **Flows in the core.** `web/src/core/org/devices.ts` (rows, labels, rights, platform candidates, planners, assign,
   rename) and `web/src/core/platform/invites.ts` (inviter right, platform invitations) are framework-free and covered
   by Vitest alone; the screens in `web/src/ui/screens/{devices,platform-invites}/` only render them. Unassign and
   unpair go through `submitBatch` of WEB-06 with their own planners (`device_unassign`, `remove` of the device); the
   rotation set R and the new wraps come from `buildBatch`.
2. **Who may change devices.** `device_assign`, `device_unassign` and `remove` are admin entries of the membership log
   (04-crypto.md §4.7), so every device control is rendered for admins only. 06-web.md §7.3 "any writer in a private
   org" is the same person: the single member of a private org is its founder and admin; a later non-admin member of a
   private org would be refused by the log rules (`log_rights`), so the control is not shown to it.
3. **The list is the verified log's.** Rows are the active device members of the verified log (newest first) with their
   kind (`platform_seq` present → platform device), assigned workgroups and, for platform devices, whether the verified
   platform log still certifies them ("No longer certified" badge; the cleanup batch is the pending-rotation job of
   WEB-06). The hub's `GET /v1/orgs/{org}/devices` only adds non-confidential metadata (online, last seen, agent
   version, capacity, `capacity_low`); a device the hub lists but the log does not know is not shown, a device of the
   log the hub does not list shows "Status unknown".
4. **Live without a presence frame.** The browser live channel has no device frame (03-api.md §5). The hub list is read
   again on every change of the verified log head (the `log` frame of an enrollment, an assignment, a removal) and
   every 15 s for presence (the hub's online window is 30 s). The "Add org device" result follows the verified log and
   switches to "The device has enrolled" when the device's `accept` arrives.
5. **Labels.** `device.profile` events (org scope, `ref` = device, admin author) are pulled once
   (`kinds=device.profile`) and read by a selector of `core/org/devices.ts`; `core/tasks/directory.ts` (WEB-04) is not
   changed. Labels are 1–100 characters on one line (`contracts/events/objects.json`).
6. **Assigning.** "Assign platform device" (admin, platform access): workgroup (only workgroups whose current `WK` and
   `OK` the admin holds: the batch wraps both for the device, ADR-007), then the candidates of
   `GET /v1/workgroups/{wg}/device-candidates` in the hub's order. Every candidate is checked against the verified
   platform log (certification valid, same device, same keys; one platform-log refresh when one does not verify);
   unverified ones are listed as "Not certified in the platform log" and cannot be chosen. There is no preselection:
   the admin picks the device. Org devices get a per-device "Assign to workgroup" (a further `device_assign` without
   `platform_seq`, same batch rules) – the natural counterpart of unassign. After an accepted assignment the browser
   wraps the user's own `CK` for the device (`wrapCredentialKey` of WEB-04, best effort; catch-up offers the slot again).
7. **Platform access (ADR-026).** Without platform access (non-private org without a granted `org_platform_access` in
   the verified platform log) an admin sees the `platform_access_denied` entry of the error catalog (title, body,
   action) instead of the button; `assignDevice` refuses with the same code before any request, and a refusal of the
   hub shows the same catalog entry in the dialog. Own devices can always be added.
8. **Device invitations.** `createInvite` of WEB-06 with `kind: 'device'`, 24 h default (1 h – 30 d), the default
   workgroup preselected; the code and the command `agent-runner enroll --hub <origin> --code <code>` are shown once
   with copy buttons and the confidentiality warning. Open device invitations (checked against the verified log) can be
   revoked; the list is re-read on every log change.
9. **Platform invitations.** The right `platform:inviter` is read from the verified platform log (`role_grant` /
   `role_revoke`), never from `GET /v1/me`. Navigation entries cannot depend on a right, so the link is a header item
   (`ui/shell/header/platform-invites.tsx`) rendered only for inviters; anyone else opening `/platform/invites` gets a
   neutral "This page is not available" (06-web.md §12.6). The code is drawn in the browser and kept with its expiry
   until the hub took it: a retry after a failure sends the identical request (same `invite_id`, same `expires_at`).
   The list shows every state the hub reports (open, used, revoked, expired, "Passkey registered" for a registered but
   not yet used invitation); only open ones can be revoked.
10. **Shared parts.** `ActionDialog`, the button classes and `expiryLabel` are reused from the member screen (exported
    for WEB-07 by WEB-06); `CopyButton` and `ExpirySelect` live in `ui/screens/devices/parts.tsx` and are used by both
    new screens.
11. **Tests.** Vitest: core 100 % alone (devices and platform invitations against the simulated org hub of WEB-06),
    jsdom screen tests cover every screen branch. Playwright against the real hub: `web/e2e/devices.spec.ts` (org
    device added, enrolled live, renamed, assigned, unassigned and unpaired with rotation, used and revoked codes
    refused, member view without controls, org without platform access), `web/e2e/devices/platform.spec.ts` (device
    double: platform device assignment from verified candidates, capacity-low badge, access granted by the operator),
    `web/e2e/platform-invites.spec.ts` (route and link only for inviters, invitation shown once, used by a new browser,
    revoked and refused). The org-device side of the enrollment is played by `web/e2e/helpers/devices.ts` (what
    `agent-runner enroll` does: lookup, package, log check, `accept` signed by device and `sk_inv`, signed
    `POST /v1/device/enroll`); presence comes from the device double.
12. **E2E.** E2E-01 now creates the platform invitation on the new screen. E2E-04, E2E-05 and E2E-38 do not exist yet
    (SCN-03); the device screen's test IDs (`devices.*`) are ready for them.
13. **Platform log on screen entry.** The browser verifies the platform log inside the log job of an org; right after
    onboarding it may not have run yet. The device screen (and the platform-invitation screen, with an active org) asks
    for that job when no verified platform log is in memory; until then a non-private org shows neither the button nor
    the refusal (no claim either way).
14. **Independent review – handled.** No high findings. Medium, fixed: the unpair spec now proves the hub refuses the
    device (`GET /v1/device/state` signed by it: 200 before, 401/403 after); Playwright now also unassigns and unpairs a
    platform device and shows the "No longer certified" badge after `device_decertify`. Medium, kept with reason: the
    hub's own `platform_access_denied` refusal cannot be provoked in the browser suite (without access the hub lists no
    candidates), it is covered by the jsdom and core tests against the simulated hub; E2E-04/05/38 follow in SCN-03
    (item 12). Low, fixed: malformed candidates are listed as unverifiable rows instead of being dropped; the expiry is
    fixed after the first attempt so a retry can never create a second platform invitation; the 30-day choice keeps a
    2-minute margin for clock skew; older answers of the device list are ignored; `assignDevice` refuses with
    `log_rights` instead of a TypeError without a platform log; a wrong spec comment. Low, kept: polling continues in
    hidden tabs (one small signed GET every 15 s); the `CK` wrap stays a separate best-effort request as in WEB-04
    (item 6); the new core files are new files in new areas (ADR-027 §4), no shared list is edited.
