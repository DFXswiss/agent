// Devices (06-web.md section 7.3) against the real hub: an admin adds an org device (code and enrollment command shown
// once), the device enrolls (as `agent-runner enroll` would) and appears live, the admin renames it, assigns it to a
// further workgroup, unassigns it (rotation batch) and unpairs it (rotation batch, the device is refused); a used code
// enrolls nothing; a plain member sees the list without controls; an org without platform access shows the reason
// (ADR-026) instead of the platform assignment. Platform devices with the device double: e2e/devices/platform.spec.ts.
import type { Page } from '@playwright/test';
import { ORG, WG } from '../src/core/log/state';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { deviceStateStatus, enrollOrgDevice, enrollRefusal } from './helpers/devices.ts';
import { setOnline } from './helpers/director.ts';
import { joinWithCode } from './helpers/members.ts';
import { onboard } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

const device = (page: Page, id: string) => page.locator(`[data-testid="devices.item"][data-device="${id}"]`);

/** Creates a device invitation through the screen; returns the code shown once. */
async function addDevice(page: Page): Promise<string> {
  await page.getByTestId('devices.add.open').click();
  await expect(page.getByTestId('devices.add.expiry')).toHaveValue('24');
  await page.getByTestId('devices.add.submit').click();
  await expect(page.getByTestId('devices.add.warning')).toBeVisible();
  const code = (await page.getByTestId('devices.add.code').textContent())!;
  const origin = new URL(page.url()).origin;
  await expect(page.getByTestId('devices.add.command')).toHaveText(
    `agent-runner enroll --hub ${origin} --code ${code}`,
  );
  return code;
}

test('an org device enrolls live, is renamed, assigned, unassigned and unpaired @R-3.2 @R-1.12', async ({
  page,
  baseURL,
}) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL!, name: 'Ada Admin' });
  const user = await PageUser.open(page, ada.recoveryCode);
  await page.getByTestId('shell.nav.devices').click();
  await expect(page.getByTestId('devices.screen')).toBeVisible();
  await expect(page.getByTestId('devices.empty')).toBeVisible();
  // a private org has platform access: the platform assignment is offered
  await expect(page.getByTestId('devices.assign.open')).toBeVisible();
  const code = await addDevice(page);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL! });
  await page.getByTestId('devices.add.copy-command').click();
  await expect(page.getByTestId('devices.add.copy-command')).toHaveText('Copied');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(`--code ${code}`);
  await expect(page.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'false');
  await expect(page.getByTestId('devices.invites.item')).toHaveCount(1);

  // the device enrolls: the screen follows the log live (no reload)
  const enrolled = await enrollOrgDevice(baseURL!, code);
  expect(await deviceStateStatus(baseURL!, enrolled)).toBe(200);
  await expect(page.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'true');
  await expect(device(page, enrolled.deviceId)).toBeVisible();
  await expect(device(page, enrolled.deviceId).getByTestId('devices.item.kind')).toHaveText('Own device');
  await expect(page.getByTestId('devices.invites.empty')).toBeVisible();
  await page.getByTestId('devices.add.done').click();
  // the used code enrolls nothing
  expect(await enrollRefusal(baseURL!, code)).toBe('invite_unknown');

  // presence and version come from the hub (read again on the next log change)
  await setOnline(enrolled.deviceId, true);
  const wg = await user.createWorkgroup(ada.org, 'Research');
  const item = device(page, enrolled.deviceId);
  await expect(item.getByTestId('devices.item.online')).toHaveText('Online');
  await expect(item.getByTestId('devices.item.version')).toHaveText('Version 0.1.0');
  await expect(item.getByTestId('devices.item.capacity')).toContainText('1 of 8 VM slots in use');

  // rename
  await item.getByTestId('devices.item.rename').click();
  await page.getByTestId('devices.rename.input').fill('Build box');
  await page.getByTestId('devices.rename.confirm').click();
  await expect(item.getByTestId('devices.item.name')).toHaveText('Build box');

  // assign to the new workgroup (WK of Research wrapped for the device)
  await item.getByTestId('devices.item.to-wg').click();
  await page.getByTestId('devices.to-wg.wg').selectOption(wg);
  await page.getByTestId('devices.to-wg.confirm').click();
  await expect(item.locator(`[data-wg="${wg}"]`)).toContainText('Research');
  let log = await user.log(ada.org);
  expect(log.state.assigned(enrolled.deviceId, wg)).toBe(true);

  // unassign: the workgroup key rotates in the same batch
  const before = log.state.epoch(WG, wg);
  await item.locator(`[data-wg="${wg}"]`).getByTestId('devices.item.unassign').click();
  await page.getByTestId('devices.unassign.confirm').click();
  await expect(item.locator(`[data-wg="${wg}"]`)).toHaveCount(0);
  log = await user.log(ada.org);
  expect(log.state.assigned(enrolled.deviceId, wg)).toBe(false);
  expect(log.state.epoch(WG, wg)).toBe(before + 1);

  // unpair: the device leaves, the org key and the default workgroup key rotate, the hub refuses the device
  const orgEpoch = log.state.epoch(ORG, ada.org);
  await item.getByTestId('devices.item.unpair').click();
  await page.getByTestId('devices.unpair.confirm').click();
  await expect(item).toHaveCount(0);
  log = await user.log(ada.org);
  expect(log.state.member(enrolled.deviceId)).toBeNull();
  expect(log.state.epoch(ORG, ada.org)).toBe(orgEpoch + 1);
  await expect(page.getByTestId('devices.empty')).toBeVisible();
  // the hub refuses the unpaired device at once (04-crypto.md section 5.10)
  expect([401, 403]).toContain(await deviceStateStatus(baseURL!, enrolled));
});

test('a revoked device invitation enrolls nothing; a member sees the devices without controls', async ({
  page,
  baseURL,
  newContext,
}) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL!, name: 'Ada Admin' });
  await page.getByTestId('shell.nav.devices').click();
  const revoked = await addDevice(page);
  await page.getByTestId('devices.add.done').click();
  await page.getByTestId('devices.invites.revoke').click();
  await expect(page.getByTestId('devices.invites.empty')).toBeVisible();
  expect(await enrollRefusal(baseURL!, revoked)).toBe('invite_unknown');
  const code = await addDevice(page);
  const enrolled = await enrollOrgDevice(baseURL!, code);
  await expect(device(page, enrolled.deviceId)).toBeVisible();

  // Bob joins as a member and opens the device screen
  await page.getByTestId('shell.nav.members').click();
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.submit').click();
  const bobCode = (await page.getByTestId('members.invite.code').textContent())!;
  const context = await newContext();
  const bobPage = await context.newPage();
  await addAuthenticator(bobPage);
  const bob = await onboard(bobPage, { origin: baseURL!, name: 'Bob Member' });
  await joinWithCode(await PageUser.open(bobPage, bob.recoveryCode), baseURL!, bobCode, 'Bob Member');
  await bobPage.goto(`/o/${ada.org}/devices`);
  await bobPage.getByTestId('login.submit').click();
  await expect(bobPage.getByTestId('devices.screen')).toBeVisible();
  await expect(device(bobPage, enrolled.deviceId)).toBeVisible();
  await expect(bobPage.getByTestId('devices.add.open')).toHaveCount(0);
  await expect(bobPage.getByTestId('devices.assign.open')).toHaveCount(0);
  await expect(bobPage.getByTestId('devices.platform.denied')).toHaveCount(0);
  await expect(bobPage.getByTestId('devices.item.unpair')).toHaveCount(0);
  await expect(bobPage.getByTestId('devices.item.unassign')).toHaveCount(0);
  await expect(bobPage.getByTestId('devices.item.rename')).toHaveCount(0);
});

test('an org without platform access shows the reason instead of the platform assignment @R-P.12', async ({
  page,
  baseURL,
}) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, name: 'Ada Lovelace' });
  await page.getByTestId('org-switcher.current').click();
  await page.getByTestId('org-switcher.create.open').click();
  await page.getByTestId('org-switcher.create.name').fill('Acme AG');
  await page.getByTestId('org-switcher.create.confirm').click();
  await expect(page.getByTestId('org-switcher.current')).toHaveText('Acme AG');
  await page.getByTestId('shell.nav.devices').click();
  const denied = page.getByTestId('devices.platform.denied');
  await expect(denied).toContainText('No platform devices');
  await expect(denied).toContainText('This organization may not use platform execution devices.');
  await expect(page.getByTestId('devices.assign.open')).toHaveCount(0);
  // own devices can still be added
  await expect(page.getByTestId('devices.add.open')).toBeVisible();
});
