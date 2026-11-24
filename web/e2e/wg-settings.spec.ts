// Workgroup settings (06-web.md section 8.1) against the real hub: the writer opens them from the task list, sees the
// defaults, gets line errors, saves the allow-list as a `settings.workgroup` event that survives a reload; a newer
// event written meanwhile shows the concurrent-edit warning; a reader (`wg:read` only) sees the values without
// controls.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';
import { writeSettings } from './helpers/operations.ts';
import { PageUser } from './helpers/user.ts';

test('a writer edits and saves the workgroup settings, with the concurrent-edit warning', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL! });
  const user = await PageUser.open(page, ada.recoveryCode);
  const me = (await user.request('GET', '/v1/me')) as { orgs: { org_id: string; default_wg: string }[] };
  const wg = me.orgs[0]!.default_wg;
  // client-side navigation (a reload would lock the identity again)
  await page.evaluate((target) => {
    history.pushState({}, '', target);
    dispatchEvent(new PopStateEvent('popstate'));
  }, `/o/${ada.org}/w/${wg}`);
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}/w/${wg}$`));
  await page.getByTestId('tasks.settings').click();
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}/w/${wg}/settings$`));
  await expect(page.getByTestId('wg-settings.defaults')).toBeVisible();
  await expect(page.getByTestId('wg-settings.protected')).toHaveValue('main\nmaster\ndevelop\nrelease/*');
  await expect(page.getByTestId('wg-settings.draft')).toBeChecked();
  await page.getByTestId('wg-settings.repositories').fill('DFXswiss/agent\nnot-a-repo');
  await page.getByTestId('wg-settings.save').click();
  await expect(page.getByTestId('wg-settings.error')).toHaveText(['Line 2: “not-a-repo” is not owner/name.']);
  await page.getByTestId('wg-settings.repositories').fill('DFXswiss/agent\nDFXswiss/agent-core');
  await page.getByTestId('wg-settings.draft').uncheck();
  await page.getByTestId('wg-settings.save').click();
  await expect(page.getByTestId('wg-settings.saved')).toBeVisible();
  await expect(page.getByTestId('wg-settings.repositories')).toHaveValue('dfxswiss/agent\ndfxswiss/agent-core');
  // another editor saves while this form is being edited: the warning, then "save mine anyway"
  await page.getByTestId('wg-settings.repositories').fill('mine/repo');
  await writeSettings(user, ada.org, wg, {
    repositories: ['their/repo'],
    protected_branches: ['main'],
    pr_draft_default: true,
  });
  await expect(page.getByTestId('wg-settings.conflict')).toBeVisible();
  await page.getByTestId('wg-settings.conflict.overwrite').click();
  await expect(page.getByTestId('wg-settings.saved')).toBeVisible();
  // after a reload (passkey again) the newest settings come from the decrypted event
  await page.goto(`/o/${ada.org}/w/${wg}/settings`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('wg-settings.repositories')).toHaveValue('mine/repo');
  await expect(page.getByTestId('wg-settings.draft')).not.toBeChecked();
  // the hub stored ciphertext only
  const events = (await user.request('GET', `/v1/scopes/${wg}/events?kinds=settings.workgroup`)) as {
    events: unknown[];
  };
  expect(events.events).toHaveLength(3);
  expect(JSON.stringify(events)).not.toContain('mine/repo');
});

test('a reader sees the settings without controls; an unknown workgroup is not found', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL! });
  const user = await PageUser.open(page, ada.recoveryCode);
  const wg = await user.createWorkgroup(ada.org, 'Readers', 'read');
  await page.goto(`/o/${ada.org}/w/${wg}/settings`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('wg-settings.read-only')).toBeVisible();
  await expect(page.getByTestId('wg-settings.title')).toContainText('Readers');
  await expect(page.getByTestId('wg-settings.view.repositories')).toHaveText(
    'Every repository of the GitHub connection is allowed.',
  );
  await expect(page.getByTestId('wg-settings.view.protected')).toContainText('release/*');
  await expect(page.getByTestId('wg-settings.view.draft')).toHaveText('Pull requests are drafts by default.');
  await expect(page.getByTestId('wg-settings.form')).toHaveCount(0);
  await expect(page.locator('textarea')).toHaveCount(0);
  await page.getByTestId('wg-settings.back').click();
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}/w/${wg}$`));
  await page.goto(`/o/${ada.org}/w/0190f000-0000-7000-8000-00000000ffff/settings`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('wg-settings.not-found')).toBeVisible();
});
