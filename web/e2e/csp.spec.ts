// The CSP listener fixture itself, the hub headers, and the absence of window globals (06-web.md sections 10.2, 13).
import { CONTENT_SECURITY_POLICY, SECURITY_HEADERS } from './fixtures/headers.ts';
import { CSP_BINDING, expect, test } from './fixtures/test.ts';

test('the listener records an inline style and an inline script', async ({ page, cspViolations }) => {
  await page.goto('/nowhere');
  await expect(page.getByTestId('notfound.title')).toBeVisible();
  await page.evaluate(() => {
    const style = document.createElement('style');
    style.textContent = 'body { color: red; }';
    document.head.append(style);
    const script = document.createElement('script');
    script.textContent = 'void 0';
    document.head.append(script);
  });
  await expect
    .poll(() => cspViolations.map((violation) => violation.directive).sort())
    .toEqual(['script-src-elem', 'style-src-elem']);
  cspViolations.splice(0);
});

test('every response carries the hub security headers', async ({ request }) => {
  for (const path of ['/', '/v1/config', '/v1/unknown', '/deep/link']) {
    const headers = (await request.get(path)).headers();
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) expect(headers[name.toLowerCase()]).toBe(value);
  }
  expect(CONTENT_SECURITY_POLICY).toContain("script-src 'self';");
});

test('the bundle adds no globals to window', async ({ page }) => {
  const own = () => Object.getOwnPropertyNames(window).sort();
  await page.goto('/v1/health');
  const baseline = new Set(await page.evaluate(own));
  await page.goto('/nowhere');
  await expect(page.getByTestId('notfound.title')).toBeVisible();
  const added = (await page.evaluate(own)).filter((name) => !baseline.has(name) && name !== CSP_BINDING);
  expect(added).toEqual([]);
});

test('contexts from newContext report violations too', async ({ newContext, cspViolations }) => {
  const page = await (await newContext()).newPage();
  await page.goto('/nowhere');
  await expect(page.getByTestId('notfound.title')).toBeVisible();
  await page.evaluate(() =>
    document.head.append(Object.assign(document.createElement('style'), { textContent: 'p{}' })),
  );
  await expect.poll(() => cspViolations.map((violation) => violation.directive)).toEqual(['style-src-elem']);
  cspViolations.splice(0);
});
