// Platform devices on the device screen (06-web.md section 7.3, 04-crypto.md section 5.9, ADR-026) against the real
// hub and the device double: the admin of a private org assigns a certified platform device (candidate verified
// against the compiled root key; the batch wraps WK and OK for it), the device shows kind, presence, capacity and the
// capacity-low badge and is unassigned and unpaired with rotation; a non-private org shows the reason until the
// operator grants platform access, then assigns a platform device to its own workgroup, which shows as no longer
// certified once the operator decertifies it.
import { type Page } from '@playwright/test';
import { ORG, WG } from '../../src/core/log/state';
import { expect, test } from '../fixtures/test.ts';
import { newDevice } from '../helpers/director.ts';
import { appendPlatform } from '../helpers/platform.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

const device = (page: Page, id: string) => page.locator(`[data-testid="devices.item"][data-device="${id}"]`);

/** Assigns the device `id` to the workgroup `wg` through the dialog. */
async function assign(page: Page, wg: string, id: string): Promise<void> {
  await page.getByTestId('devices.assign.open').click();
  await page.getByTestId('devices.assign.wg').selectOption(wg);
  const candidate = page.locator(`[data-testid="devices.assign.candidate"][data-device="${id}"]`);
  await expect(candidate).toHaveAttribute('data-verified', 'true');
  await expect(page.getByTestId('devices.assign.confirm')).toBeDisabled();
  await candidate.getByRole('radio').check();
  await page.getByTestId('devices.assign.confirm').click();
  await expect(page.getByTestId('devices.assign')).toHaveCount(0);
}

test('a platform device is assigned from the verified candidates; access follows the platform log @R-3.3 @R-P.12', async ({
  page,
  baseURL,
}) => {
  // a device with one free slot of eight: capacity low (03-api.md section 4.6.2)
  const ada = await freshUser(page, baseURL!, { free: 0, poolReady: 0 });
  const user = await PageUser.open(page, ada.recoveryCode);
  const defaultWg = (await user.log(ada.org)).state.defaultWg;
  await page.getByTestId('shell.nav.devices').click();
  await expect(page.getByTestId('devices.empty')).toBeVisible();
  await assign(page, defaultWg, ada.device);
  const item = device(page, ada.device);
  await expect(item.getByTestId('devices.item.kind')).toHaveText('Platform device');
  await expect(item.getByTestId('devices.item.online')).toHaveText('Online');
  await expect(item.getByTestId('devices.item.capacity-low')).toBeVisible();
  await expect(item.getByTestId('devices.item.capacity')).toContainText('8 of 8 VM slots in use');
  let log = await user.log(ada.org);
  expect(log.state.assigned(ada.device, defaultWg)).toBe(true);
  expect(log.state.member(ada.device)!.platformSeq).not.toBeNull();
  // the device can read the workgroup and the org (ADR-007): its wraps exist
  const missing = await user.request('GET', `/v1/orgs/${ada.org}/catch-up`);
  expect(JSON.stringify(missing)).not.toContain(ada.device);

  // unassign (WK of the default workgroup and OK rotate: the device has no workgroup left) and unpair
  const wgEpoch = log.state.epoch(WG, defaultWg);
  const orgEpoch = log.state.epoch(ORG, ada.org);
  await item.getByTestId('devices.item.unassign').click();
  await page.getByTestId('devices.unassign.confirm').click();
  await expect(item.getByTestId('devices.item.wgs')).toHaveText('Not assigned to a workgroup');
  log = await user.log(ada.org);
  expect(log.state.epoch(WG, defaultWg)).toBe(wgEpoch + 1);
  expect(log.state.epoch(ORG, ada.org)).toBe(orgEpoch + 1);
  await item.getByTestId('devices.item.unpair').click();
  await page.getByTestId('devices.unpair.confirm').click();
  await expect(item).toHaveCount(0);
  expect((await user.log(ada.org)).state.member(ada.device)).toBeNull();

  // a non-private org: the reason instead of the button, until the operator grants platform access
  await page.getByTestId('org-switcher.current').click();
  await page.getByTestId('org-switcher.create.open').click();
  await page.getByTestId('org-switcher.create.name').fill('Acme AG');
  await page.getByTestId('org-switcher.create.confirm').click();
  await expect(page.getByTestId('org-switcher.current')).toHaveText('Acme AG');
  const acme = /\/o\/([0-9a-f-]{36})$/.exec(page.url())![1]!;
  await page.getByTestId('shell.nav.devices').click();
  await expect(page.getByTestId('devices.platform.denied')).toBeVisible();
  await appendPlatform(baseURL!, [{ type: 'org_platform_access', org: acme, granted: true }]);
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('devices.assign.open')).toBeVisible();
  const second = await newDevice({ free: 6, poolReady: 1 });
  const acmeWg = (await user.log(acme)).state.defaultWg;
  await assign(page, acmeWg, second);
  await expect(device(page, second).getByTestId('devices.item.capacity')).toContainText('2 of 8 VM slots in use');
  await expect(device(page, second).getByTestId('devices.item.capacity-low')).toHaveCount(0);
  log = await user.log(acme);
  expect(log.state.assigned(second, acmeWg)).toBe(true);
  expect(log.state.epoch(ORG, acme)).toBe(1);
  expect(log.state.epoch(WG, acmeWg)).toBe(1);

  // the operator decertifies the device: the list marks it (the admin browser owes the unassignment batch)
  await appendPlatform(baseURL!, [{ type: 'device_decertify', device: second }]);
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(device(page, second).getByTestId('devices.item.decertified')).toBeVisible();
});
