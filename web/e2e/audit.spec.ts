// The audit screen (06-web.md section 9.2, 03-api.md section 4.11) against the real hub: the admin of a private org
// sees the org's rows newest first with times as HH:MM:SS.mmm and the actor's display name, filters by action group,
// actor and time (the time filter is the client's: the hub has no time parameter) and verifies the audit chain.
// Paging is covered by the task tab (e2e/devices/audit.spec.ts), where the device double writes many rows.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

const TIME_FORMAT = /^(\d{2}\.\d{2}\.\d{4} |\d{2}\/\d{2}\/\d{4} )?\d{2}:\d{2}:\d{2}\.\d{3}$/;

test('lists, filters, pages and verifies the audit rows of the org @R-7.6', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL!, name: 'Ada Lovelace' });
  const api = await PageUser.open(page, ada.recoveryCode);

  await page.getByTestId('shell.nav.audit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}/audit$`));
  const rows = page.getByTestId('audit.row');
  await expect(page.locator('[data-testid="audit.row"][data-action="log.genesis"]')).toBeVisible();
  const seqs = await rows.evaluateAll((items) => items.map((item) => Number(item.getAttribute('data-seq'))));
  expect(seqs).toEqual([...seqs].sort((a, b) => b - a));
  await expect(rows.first().getByTestId('audit.row.time')).toHaveText(TIME_FORMAT);
  await expect(page.getByTestId('audit.more')).toHaveCount(0);

  // the action group "log": only membership-log rows, with the founder's name
  await page.getByTestId('audit.filter.action').fill('log');
  await page.getByTestId('audit.filter.apply').click();
  await expect(page).toHaveURL(/\?action=log$/);
  const genesis = page.locator('[data-testid="audit.row"][data-action="log.genesis"]');
  const actions = () => rows.evaluateAll((items) => items.map((item) => item.getAttribute('data-action')));
  await expect.poll(async () => (await actions()).every((action) => action!.startsWith('log.'))).toBe(true);
  await expect(genesis.getByTestId('audit.row.actor')).toHaveText('Ada Lovelace');
  await expect(genesis.getByTestId('audit.row.result')).toHaveText('OK');
  await expect(page.getByTestId('audit.more')).toHaveCount(0);

  // a malformed action blocks the filter
  await page.getByTestId('audit.filter.action').fill('Log Genesis');
  await expect(page.getByTestId('audit.filter.invalid')).toBeVisible();
  await expect(page.getByTestId('audit.filter.apply')).toBeDisabled();

  // the actor filter and a time window in the future (nothing can match; paging stops)
  await page.getByTestId('audit.filter.reset').click();
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}/audit$`));
  await expect(page.getByTestId('audit.filter.action')).toHaveValue('');
  await page.getByTestId('audit.filter.actor').selectOption({ label: 'Ada Lovelace' });
  await page.getByTestId('audit.filter.apply').click();
  await expect(page).toHaveURL(new RegExp(`actor=${api.userId}`));
  await expect(rows.first().getByTestId('audit.row.actor')).toHaveText('Ada Lovelace');
  const later = new Date(Date.now() + 3600_000);
  const local = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:00`;
  await page.getByTestId('audit.filter.from').fill(local(later));
  await page.getByTestId('audit.filter.apply').click();
  await expect(page.getByTestId('audit.empty')).toBeVisible();
  await expect(page).toHaveURL(/from=/);
  await page.getByTestId('audit.filter.from').fill('');
  await page.getByTestId('audit.filter.to').fill(local(later));
  await page.getByTestId('audit.filter.apply').click();
  await expect(rows.first()).toBeVisible();

  // the chain verification (admin)
  await page.getByTestId('audit.verify.run').click();
  const result = page.getByTestId('audit.verify.result');
  await expect(result).toHaveAttribute('data-ok', 'true');
  await expect(result).toContainText('The audit chain is intact.');
  await expect(page.getByTestId('audit.verify.head')).toContainText(/Head: row \d+, hash [0-9a-f]{16}…/);
  await expect(page.getByTestId('audit.verify.checkpoint')).toBeVisible();
});
