// Playwright fixtures for every web spec (and for SCN-00):
// - V8 coverage of every page, added to the monocart cache with the build's source maps;
// - a CSP listener: every `securitypolicyviolation` of any page fails the test (06-web.md section 10.2).
// Both apply to the default `context` and to every context created with the `newContext` fixture; specs that need
// more contexts (other locales, several users) use `newContext` instead of `browser.newContext`.
import { test as base, expect, type BrowserContext, type BrowserContextOptions, type Page } from '@playwright/test';
import { CoverageReport } from 'monocart-coverage-reports';
import { e2eCoverage } from './coverage-options.ts';
import { withSourceMaps } from './coverage.ts';

export interface CspViolation {
  readonly page: string;
  readonly directive: string;
  readonly blocked: string;
  readonly sample: string;
}

export const CSP_BINDING = '__agentCspViolation';

const listener = `document.addEventListener('securitypolicyviolation', (e) => {
  window['${CSP_BINDING}']({ page: location.href, directive: e.effectiveDirective, blocked: e.blockedURI, sample: e.sample });
});`;

/** Adds the CSP listener and page coverage to `context`; the returned function stores the coverage. */
async function instrument(context: BrowserContext, violations: CspViolation[]): Promise<() => Promise<void>> {
  await context.exposeBinding(CSP_BINDING, (_source, violation: CspViolation) => {
    violations.push(violation);
  });
  await context.addInitScript({ content: listener });
  const started: Promise<Page>[] = [];
  const track = (page: Page) =>
    started.push(page.coverage.startJSCoverage({ resetOnNavigation: false }).then(() => page));
  context.pages().forEach(track);
  context.on('page', track);
  return async () => {
    const report = new CoverageReport(e2eCoverage);
    for (const page of await Promise.all(started)) {
      if (!page.isClosed()) await report.add(withSourceMaps(await page.coverage.stopJSCoverage()));
    }
  };
}

interface Fixtures {
  cspViolations: CspViolation[];
  newContext: (options?: BrowserContextOptions) => Promise<BrowserContext>;
}

export const test = base.extend<Fixtures>({
  // Playwright needs a destructuring pattern; `baseURL` is an unused built-in fixture.
  cspViolations: async ({ baseURL: _baseURL }, use) => {
    const violations: CspViolation[] = [];
    await use(violations);
    expect(violations, 'Content-Security-Policy violations').toEqual([]);
  },
  context: async ({ context, cspViolations }, use) => {
    const finish = await instrument(context, cspViolations);
    await use(context);
    await finish();
  },
  newContext: async ({ browser, cspViolations }, use) => {
    const opened: [BrowserContext, () => Promise<void>][] = [];
    await use(async (options) => {
      const context = await browser.newContext(options);
      opened.push([context, await instrument(context, cspViolations)]);
      return context;
    });
    for (const [context, finish] of opened) {
      await finish();
      await context.close();
    }
  },
});

export { expect };
