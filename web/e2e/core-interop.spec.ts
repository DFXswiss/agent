// Runtime interop of the crypto core (WCR-01, CON-02): the vector suite of tests/core/crypto/vector-suite.ts – the
// same checks Vitest runs on Node's WebCrypto – is bundled and executed in Chromium on the browser's own WebCrypto
// (X25519, Ed25519, HKDF, AES-GCM, ECDSA P-256), so a difference between the two runtimes fails here. The log vectors
// of tests/core/log/vector-suite.ts (log, events, rotation, wraps, platform log; CON-02/CON-03) run the same way. The
// page is served by request interception under an https origin (a secure context, as WebCrypto requires).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { build, type Rolldown } from 'vite';
import { WEB_DIR } from './fixtures/env.ts';
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';

const ORIGIN = 'https://core-interop.test';
const SUITE = 'tests/core/crypto/vector-suite.ts';
const LOG_SUITE = 'tests/core/log/vector-suite.ts';

async function bundleSuite(entry: string): Promise<string> {
  const result = await build({
    configFile: false,
    root: WEB_DIR,
    logLevel: 'warn',
    build: {
      write: false,
      minify: false,
      target: 'es2022',
      lib: { entry, formats: ['es'], fileName: 'suite' },
    },
  });
  const outputs = (Array.isArray(result) ? result : [result]) as Rolldown.RolldownOutput[];
  const chunk = outputs.flatMap((out) => out.output).find((item) => item.type === 'chunk' && item.isEntry);
  return (chunk as Rolldown.OutputChunk).code;
}

interface Outcome {
  name: string;
  error: string | null;
}

/** Runs the bundled suite's `runAll` in Chromium on a secure origin and returns its outcomes. */
async function runInChromium(page: Page, code: string): Promise<Outcome[]> {
  await page.route(`${ORIGIN}/**`, (route) =>
    new URL(route.request().url()).pathname === '/suite.js'
      ? route.fulfill({ contentType: 'text/javascript', body: code })
      : route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>core interop</title>' }),
  );
  await page.goto(`${ORIGIN}/`);
  const result = await page.evaluate(async () => {
    const url = '/suite.js';
    const suite = await import(url);
    return { secure: isSecureContext, agent: navigator.userAgent, outcomes: await suite.runAll() };
  });
  expect(result.secure).toBe(true);
  expect(result.agent).toContain('Chrome');
  return result.outcomes as Outcome[];
}

const invalidCount = (dir: string, name: string): number =>
  (JSON.parse(readFileSync(join(WEB_DIR, '..', 'contracts', dir, `${name}.json`), 'utf8')).invalid as unknown[]).length;

test('Chromium reproduces every shared crypto vector with its own WebCrypto', async ({ page }) => {
  const outcomes = await runInChromium(page, await bundleSuite(SUITE));
  expect(outcomes.filter((outcome) => outcome.error !== null)).toEqual([]);
  // exactly the checks Vitest runs: one positive check per consumed file plus one per negative vector of those files
  const consumed = outcomes.filter((o) => o.name.endsWith(': positive vectors')).map((o) => o.name.split(':')[0]!);
  const negatives = consumed.reduce(
    (total, name) => total + invalidCount(name === 'title' ? 'tasks' : 'vectors', name),
    0,
  );
  expect(consumed.length).toBe(17);
  expect(outcomes.length).toBe(consumed.length + negatives);
});

test('Chromium reproduces every log vector with its own WebCrypto', async ({ page }) => {
  const outcomes = await runInChromium(page, await bundleSuite(LOG_SUITE));
  expect(outcomes.filter((outcome) => outcome.error !== null)).toEqual([]);
  // exactly the checks Vitest runs: one positive check per log file plus one per negative vector of those files
  const files = ['log', 'log-events', 'log-rotation', 'log-wraps', 'platform-log'];
  const consumed = outcomes.filter((o) => o.name.endsWith(': positive vectors')).map((o) => o.name.split(':')[0]!);
  expect(consumed.sort()).toEqual(files);
  const negatives = files.reduce((total, name) => total + invalidCount('vectors', name), 0);
  expect(negatives).toBeGreaterThan(0);
  expect(outcomes.length).toBe(files.length + negatives);
});
