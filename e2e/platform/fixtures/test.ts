// Fixtures of every platform E2E spec: the WEB-01 fixtures (CSP violation listener, browser V8 coverage of every
// page mapped onto web/src – the run writes it below the iteration's web-coverage directory, informative per
// 07-testing.md section 3) plus the orchestrator client and virtual authenticators.
import { test as web, expect } from '../../../web/e2e/fixtures/test.ts';
import {
  addAuthenticator,
  type AuthenticatorOptions,
  type Variant,
  type VirtualAuthenticator,
} from './authenticator.ts';
import { readEnv, type E2eEnv } from './env.ts';
import { Orchestrator } from './orchestrator.ts';

interface Fixtures {
  e2e: E2eEnv;
  orchestrator: Orchestrator;
  authenticator: (variant?: Variant, overrides?: Partial<AuthenticatorOptions>) => Promise<VirtualAuthenticator>;
}

export const test = web.extend<Fixtures>({
  // Playwright needs a destructuring pattern; `baseURL` is an unused built-in fixture.
  e2e: async ({ baseURL: _baseURL }, use) => {
    await use(readEnv());
  },
  orchestrator: async ({ e2e }, use) => {
    await use(new Orchestrator(e2e.controlUrl, e2e.controlToken));
  },
  authenticator: async ({ page }, use) => {
    const added: VirtualAuthenticator[] = [];
    await use(async (variant, overrides) => {
      const authenticator = await addAuthenticator(page, variant, overrides);
      added.push(authenticator);
      return authenticator;
    });
    for (const authenticator of added) await authenticator.remove().catch(() => undefined);
  },
});

export { expect };
