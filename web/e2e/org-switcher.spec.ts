// Org switcher (06-web.md section 7.5) against the real hub: the user creates a non-private organization (ADR-026
// note), the switcher lists both with their decrypted names, and switching reconnects the live channel with the other
// `org` parameter.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

test('creating an organization and switching between organizations @R-2.4', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL!, name: 'Ada Lovelace' });
  const streams: string[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname === '/v1/stream') streams.push(url.searchParams.get('org')!);
  });
  await expect(page.getByTestId('org-switcher.current')).toHaveText('Private');
  await page.getByTestId('org-switcher.current').click();
  await expect(page.getByTestId('org-switcher.item')).toHaveCount(1);
  await page.getByTestId('org-switcher.create.open').click();
  await expect(page.getByTestId('org-switcher.create.platform-note')).toBeVisible();
  await expect(page.getByTestId('org-switcher.create.display-name')).toHaveValue('Ada Lovelace');
  await expect(page.getByTestId('org-switcher.create.confirm')).toBeDisabled();
  await page.getByTestId('org-switcher.create.name').fill('Acme AG');
  await page.getByTestId('org-switcher.create.display-name').fill('Ada L.');
  await page.getByTestId('org-switcher.create.confirm').click();
  await expect(page).not.toHaveURL(new RegExp(`/o/${ada.org}$`));
  const acme = /\/o\/([0-9a-f-]{36})$/.exec(page.url())![1]!;
  await expect(page.getByTestId('org-switcher.current')).toHaveText('Acme AG');
  await expect(page.getByTestId('home.org')).toHaveText('Acme AG');
  const user = await PageUser.open(page, ada.recoveryCode);
  const me = (await user.request('GET', '/v1/me')) as { orgs: { org_id: string; private: boolean; role: string }[] };
  expect(me.orgs).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ org_id: acme, private: false, role: 'admin' }),
      expect.objectContaining({ org_id: ada.org, private: true }),
    ]),
  );
  // switch back: the stream reconnects with the other org
  await page.getByTestId('org-switcher.current').click();
  await expect(page.getByTestId('org-switcher.item')).toHaveCount(2);
  await page.getByTestId('org-switcher.item').filter({ hasText: 'Private' }).click();
  await expect(page).toHaveURL(new RegExp(`/o/${ada.org}$`));
  await expect(page.getByTestId('org-switcher.current')).toHaveText('Private');
  await expect.poll(() => streams.at(-1)).toBe(ada.org);
  expect(streams).toContain(acme);
});
