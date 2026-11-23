// Workgroups (06-web.md section 7.2) against the real hub: an admin creates a workgroup with a member grant in one
// batch, renames and archives it; the default workgroup cannot be archived.
import { WG } from '../src/core/log/state';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { joinWithCode } from './helpers/members.ts';
import { onboard } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

test('an admin creates, renames and archives a workgroup @R-2.4 @R-8.2', async ({ page, baseURL, newContext }) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin, name: 'Ada Admin' });
  const adaUser = await PageUser.open(page, ada.recoveryCode);
  // a second member to grant access to (joined through the API; the member screen's invitation is used)
  await page.getByTestId('shell.nav.members').click();
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.submit').click();
  const code = (await page.getByTestId('members.invite.code').textContent())!;
  await page.getByTestId('members.invite.done').click();
  const other = await (await newContext()).newPage();
  await addAuthenticator(other);
  const bob = await onboard(other, { origin, name: 'Bob Member' });
  const bobUser = await PageUser.open(other, bob.recoveryCode);
  await joinWithCode(bobUser, origin, code, 'Bob Member');
  await expect(page.getByTestId('members.item')).toHaveCount(2);

  await page.getByTestId('shell.nav.workgroups').click();
  await expect(page.getByTestId('workgroups.item')).toHaveCount(1);
  const standard = page.getByTestId('workgroups.item').first();
  await expect(standard.getByTestId('workgroups.item.archive')).toHaveCount(0);
  await expect(standard.getByTestId('workgroups.item.access')).toHaveText('Write');

  await page.getByTestId('workgroups.create.open').click();
  await expect(page.getByTestId('workgroups.create.confirm')).toBeDisabled();
  await page.getByTestId('workgroups.create.name').fill('Research');
  await page.getByTestId(`workgroups.create.member.${bobUser.userId}`).selectOption('read');
  await page.getByTestId('workgroups.create.confirm').click();
  await expect(page.getByTestId('workgroups.create')).toHaveCount(0);
  const research = page.getByTestId('workgroups.item').filter({ hasText: 'Research' });
  await expect(research.getByTestId('workgroups.item.access')).toHaveText('Write');
  const wg = (await research.getAttribute('data-wg'))!;
  let log = await adaUser.log(ada.org);
  expect(log.state.wgAccess(bobUser.userId, wg)).toBe('read');
  expect(log.state.epoch(WG, wg)).toBe(1);
  // Bob received WK_1 in the same batch
  const wraps = (await bobUser.request('GET', `/v1/wraps?scope=wg&scope_id=${wg}`)) as { wraps: unknown[] };
  expect(wraps.wraps).toHaveLength(1);

  await research.getByTestId('workgroups.item.rename').click();
  await expect(page.getByTestId('workgroups.rename.name')).toHaveValue('Research');
  await page.getByTestId('workgroups.rename.name').fill('Research & Development');
  await page.getByTestId('workgroups.rename.confirm').click();
  const renamed = page.getByTestId('workgroups.item').filter({ hasText: 'Research & Development' });
  await expect(renamed).toHaveCount(1);

  await renamed.getByTestId('workgroups.item.archive').click();
  await page.getByTestId('workgroups.archive.confirm').click();
  await expect(renamed.getByTestId('workgroups.item.archived')).toBeVisible();
  await expect(renamed.getByTestId('workgroups.item.rename')).toHaveCount(0);
  log = await adaUser.log(ada.org);
  expect(log.state.writable(wg)).toBe(false);
  // the workgroup link leads to its task list
  await renamed.getByTestId('workgroups.item.name').click();
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}/w/${wg}$`));
});
