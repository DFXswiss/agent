// Performance budget of 06-web.md section 12.2 on the production build (minified, as released), on desktop and both
// phones: the initial JavaScript fits the guard of the build (`INITIAL_JS_BUDGET`, 219 KiB gzip), and the first screen
// (sign-in) is interactive within 3 s on a mid-range phone profile – CPU throttled ×4 and the "Fast 3G" network of
// Chrome's DevTools. The build is served gzip
// compressed with the hub's headers and `/v1/config` (`fixtures/server.ts`), as the edge in front of the hub delivers
// it (decision WEB-13). This spec does not use the coverage fixture: the minified build is not the build under coverage.
import { type BrowserContextOptions, expect, type Page, test } from '@playwright/test';
import { mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { budgetProblems, INITIAL_JS_BUDGET, initialScripts } from '../build/budget.ts';
import { compiledRootKey } from '../build/manifest.ts';
import { buildWeb } from '../scripts/build.mjs';
import { PORT, WEB_DIR } from './fixtures/env.ts';
import { handler } from './fixtures/server.ts';
import { CSP_BINDING, type CspViolation } from './fixtures/test.ts';

/** Time to interactive of 06-web.md section 12.2. */
export const TTI_BUDGET_MS = 3000;
/** Chrome DevTools "Fast 3G": 1.6 Mbit/s down and 750 kbit/s up at 90 %, 150 ms latency × 3.75. */
export const FAST_3G = {
  offline: false,
  downloadThroughput: ((1.6 * 1000 * 1000) / 8) * 0.9,
  uploadThroughput: ((750 * 1000) / 8) * 0.9,
  latency: 150 * 3.75,
};
const PROJECTS = ['desktop', 'mobile', 'mobile-iphone'];

test.describe.configure({ mode: 'serial' });

let dir = '';
let server: Server;
let origin = '';

/** The hub test handler with gzip for compressible responses (`Accept-Encoding: gzip`). */
function gzipping(inner: (req: IncomingMessage, res: ServerResponse) => void) {
  return (req: IncomingMessage, res: ServerResponse) => {
    if (!/\bgzip\b/.test(String(req.headers['accept-encoding'] ?? ''))) return inner(req, res);
    const writeHead = res.writeHead.bind(res);
    const end = res.end.bind(res);
    let head: Parameters<ServerResponse['writeHead']> | null = null;
    res.writeHead = ((...args: Parameters<ServerResponse['writeHead']>) => {
      head = args;
      return res;
    }) as ServerResponse['writeHead'];
    res.end = ((body: string | Buffer) => {
      const [status, headers] = head as unknown as [number, Record<string, string>];
      const zipped = gzipSync(body, { level: 6 });
      writeHead(status, { ...headers, 'Content-Encoding': 'gzip', 'Content-Length': String(zipped.length) });
      return end(zipped);
    }) as ServerResponse['end'];
    inner(req, res);
  };
}

test.beforeAll(async ({ browserName: _browserName }, info) => {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  dir = mkdtempSync(join(base, 'perf-'));
  await buildWeb({ outDir: dir });
  const port = PORT + 3 + Math.max(0, PROJECTS.indexOf(info.project.name));
  origin = `http://localhost:${port}`;
  server = createServer(gzipping(handler({ port, origin, staticDir: dir, probeDir: dir, rootKey: compiledRootKey() })));
  await new Promise<void>((resolve) => server.listen(port, 'localhost', resolve));
});

test.afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

/** Milliseconds from navigation start until the sign-in button is rendered and enabled (the app is booted). */
async function timeToInteractive(page: Page): Promise<number> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  await cdp.send('Network.emulateNetworkConditions', FAST_3G);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });
  await page.goto(`${origin}/login`, { waitUntil: 'commit' });
  const handle = await page.waitForFunction(
    () => {
      const button = document.querySelector<HTMLButtonElement>('[data-testid="login.submit"]');
      return button !== null && !button.disabled ? performance.now() : null;
    },
    undefined,
    { polling: 'raf', timeout: 20_000 },
  );
  const at = (await handle.jsonValue()) as number;
  // the configuration came from the preload of index.html: one request, started before the scripts arrived
  const entry = /<script\b[^>]*\bsrc="([^"]+)"/.exec(readFileSync(join(dir, 'index.html'), 'utf8'))![1]!;
  const timing = await page.evaluate(
    (script) =>
      performance
        .getEntriesByType('resource')
        .filter((entry) => entry.name.endsWith('/v1/config') || entry.name.endsWith(script))
        .map((entry) => ({
          name: entry.name,
          start: entry.startTime,
          end: (entry as PerformanceResourceTiming).responseEnd,
        })),
    entry,
  );
  const config = timing.filter((entry) => entry.name.endsWith('/v1/config'));
  expect(config).toHaveLength(1);
  expect(config[0]!.start).toBeLessThan(timing.find((resource) => resource.name.endsWith(entry))!.end);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 1 });
  return Math.round(at);
}

test('the initial JavaScript of the production build fits its guard', () => {
  expect(budgetProblems(dir)).toEqual([]);
  const total = initialScripts(dir).reduce((sum, script) => sum + script.gzip, 0);
  expect(total).toBeLessThanOrEqual(INITIAL_JS_BUDGET);
  test.info().annotations.push({ type: 'initial JS', description: `${(total / 1024).toFixed(1)} KiB gzip` });
});

test('the first screen is interactive within 3 s with CPU ×4 and Fast 3G', async ({ browser }, info) => {
  const runs: number[] = [];
  for (let run = 0; run < 3; run += 1) {
    const context = await browser.newContext(info.project.use as BrowserContextOptions);
    const violations: CspViolation[] = [];
    await context.exposeBinding(CSP_BINDING, (_source, violation: CspViolation) => void violations.push(violation));
    await context.addInitScript({
      content: `document.addEventListener('securitypolicyviolation', (e) => window['${CSP_BINDING}']({ page: location.href, directive: e.effectiveDirective, blocked: e.blockedURI, sample: e.sample }));`,
    });
    const page = await context.newPage();
    runs.push(await timeToInteractive(page));
    expect(violations).toEqual([]);
    await context.close();
  }
  const median = [...runs].sort((a, b) => a - b)[1]!;
  const description = `median ${median} ms of ${runs.join(', ')} ms`;
  info.annotations.push({ type: 'time to interactive', description });
  console.log(`time to interactive (${info.project.name}): ${description}`);
  expect(median).toBeLessThanOrEqual(TTI_BUDGET_MS);
});
