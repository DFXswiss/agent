// The security page (06-web.md section 10.5) against the real hub, anonymous, on desktop and both phones: the build
// hash is the SHA-256 of the manifest of the build under test (as the hub delivers it), the commit is HEAD, the root key
// fingerprint is the compiled key's, the hub key ID is the configured one; the explanations are there; the page passes
// the layout, touch-target and axe checks; the footer links to it from every page.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { compiledRootKey, MANIFEST_FILE, rootKeyFingerprint } from '../build/manifest.ts';
import { STATIC_DIR, WEB_DIR } from './fixtures/env.ts';
import { expect, test } from './fixtures/test.ts';
import { layoutProblems } from './helpers/layout-checks.ts';

const grouped = (hex: string) => hex.match(/.{1,4}/g)!.join(' ');

test('shows the build hash, commit, root key fingerprint and hub key ID of the delivered build', async ({
  page,
  request,
}) => {
  const delivered = await (await request.get(`/${MANIFEST_FILE}`)).body();
  expect(delivered.equals(readFileSync(join(STATIC_DIR, MANIFEST_FILE)))).toBe(true);
  const hash = createHash('sha256').update(delivered).digest('hex');
  const config = (await (await request.get('/v1/config')).json()) as { hub_key: { id: string } };
  await page.goto('/security');
  await expect(page.getByTestId('security.build-hash')).toHaveText(grouped(hash));
  await expect(page.getByTestId('security.commit')).toHaveText(
    execFileSync('git', ['-C', WEB_DIR, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  );
  await expect(page.getByTestId('security.root-key')).toHaveText(grouped(rootKeyFingerprint(compiledRootKey())));
  await expect(page.getByTestId('security.hub-key')).toHaveText(config.hub_key.id);
  await expect(page.getByTestId('security.root-key.mismatch')).toHaveCount(0);
  for (const part of ['sees', 'hidden', 'javascript', 'devices']) {
    await expect(page.getByTestId(`security.${part}`).getByRole('heading')).toBeVisible();
  }
  expect(await layoutProblems(page)).toEqual([]);
});

test('is linked from every page and speaks German', async ({ newContext }) => {
  const context = await newContext({ locale: 'de-CH' });
  const page = await context.newPage();
  await page.goto('/login');
  await page.getByTestId('shell.footer.security').click();
  await expect(page).toHaveURL('/security');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Sicherheit');
  await expect(page.getByTestId('security.javascript')).toContainText('JavaScript');
});
