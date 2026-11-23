// The anonymous routes of 06-web.md section 2.4 and the error pages of section 12.6 on the desktop and both phone
// projects (WEB-13): no horizontal scrolling at 360 px, touch targets ≥ 44 × 44 px, no serious or critical axe-core
// violation, no CSP violation (fixture). The signed-in routes are swept by `e2e/devices/responsive.spec.ts`.
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { layoutProblems } from './helpers/layout-checks.ts';

const ANONYMOUS: [string, string][] = [
  ['/login', 'login.page'],
  ['/invite', 'invite.page'],
  ['/invite#not-a-code', 'invite.page'],
  ['/recover', 'recover.page'],
  ['/security', 'security.screen'],
  ['/no/such/page', 'notfound.page'],
];

async function problemsOf(page: Page, path: string, ready: string): Promise<string[]> {
  await page.goto(path);
  await expect(page.getByTestId(ready)).toBeVisible();
  return layoutProblems(page);
}

test('every anonymous route passes the layout, touch-target and accessibility checks', async ({ page }) => {
  const problems: Record<string, string[]> = {};
  for (const [path, ready] of ANONYMOUS) {
    const found = await problemsOf(page, path, ready);
    if (found.length > 0) problems[path] = found;
  }
  expect(problems).toEqual({});
});

test('the blocking error pages pass the checks', async ({ page }) => {
  // hub unavailable (503) at start
  await page.route('**/v1/config', (route) => route.fulfill({ status: 503, json: { error: 'unavailable' } }));
  expect(await problemsOf(page, '/login', 'error.page')).toEqual([]);
  await page.unroute('**/v1/config');
  // a browser without passkeys
  await page.addInitScript({ content: 'delete window.PublicKeyCredential;' });
  expect(await problemsOf(page, '/login', 'shell.unsupported')).toEqual([]);
});

test('the start page loads its configuration once, from the preload', async ({ page }) => {
  const requests: string[] = [];
  page.on('request', (request) => {
    if (request.url().endsWith('/v1/config')) requests.push(request.url());
  });
  await page.goto('/login');
  await expect(page.getByTestId('login.submit')).toBeEnabled();
  expect(requests).toHaveLength(1);
});
