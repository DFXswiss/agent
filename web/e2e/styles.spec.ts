// xterm.js (WebGL and canvas renderer) and uPlot must not inject <style> elements at run time (ADR-019, 06-web.md
// section 10.2): the probe page mounts both under the hub CSP; a MutationObserver installed before any script runs
// records every <style> element that is ever added.
import { PROBE_ORIGIN } from './fixtures/env.ts';
import { expect, test } from './fixtures/test.ts';

const observer = `new MutationObserver((records) => {
  for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeName === 'STYLE') document.documentElement.dataset.styleInjections =
      String(Number(document.documentElement.dataset.styleInjections ?? 0) + 1);
  }
}).observe(document, { childList: true, subtree: true });`;

for (const renderer of ['webgl', 'canvas']) {
  test(`terminal (${renderer}) and chart inject no style elements`, async ({ page }) => {
    await page.addInitScript({ content: observer });
    await page.goto(`${PROBE_ORIGIN}/probe/?renderer=${renderer}`);
    await expect(page.locator('body')).toHaveAttribute('data-ready', '1');
    await expect(page.locator('body')).toHaveAttribute('data-renderer', renderer);
    expect(Number(await page.locator('body').getAttribute('data-canvases'))).toBeGreaterThan(0);
    await expect(page.locator('.uplot')).toHaveCount(1);
    expect(await page.locator('html').getAttribute('data-style-injections')).toBeNull();
    await expect(page.locator('style')).toHaveCount(0);
  });
}
