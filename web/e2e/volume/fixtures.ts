// Fixtures of the volume specs: one browser user per volume project (worker), onboarded with its own passkey and
// joined as admin to the volume org, kept for every view of the worker (the projects run one worker each). Each view
// is opened with a full page load and the passkey unlock, so every measurement starts from a fresh page. The phone
// project runs with 4× CPU throttling (ADR-063 §4). No browser coverage here: the precise V8 counters would distort
// the measurements (the gate measures the build as it is served); the CSP listener of the suite stays.
import { type BrowserContext, type Page, test as base } from '@playwright/test';
import { CSP_BINDING, type CspViolation } from '../fixtures/test.ts';
import { addAuthenticator } from '../helpers/authenticator.ts';
import { joinWithCode } from '../helpers/members.ts';
import { onboard } from '../helpers/onboard.ts';
import { PageUser } from '../helpers/user.ts';
import { instrumentation } from './budget.ts';
import { director, type VolumeData, volumeData } from './data.ts';

export interface VolumeUser {
  readonly page: Page;
  readonly userId: string;
  readonly data: VolumeData;
  readonly violations: CspViolation[];
  /** Opens `path` with a full page load; returns the moment of the unlock gesture (the user's action). */
  open(path: string): Promise<number>;
}

const listener = `document.addEventListener('securitypolicyviolation', (e) => {
  window['${CSP_BINDING}']({ page: location.href, directive: e.effectiveDirective, blocked: e.blockedURI, sample: e.sample });
});`;

/** Context options of the project's device (viewport, touch, user agent …); `baseURL` from the config. */
export function contextOptions(use: Record<string, unknown>) {
  const keys = ['viewport', 'screen', 'userAgent', 'deviceScaleFactor', 'isMobile', 'hasTouch', 'baseURL', 'locale'];
  return Object.fromEntries(keys.filter((key) => use[key] !== undefined).map((key) => [key, use[key]]));
}

export const test = base.extend<{ volume: VolumeUser }, { volumeUser: VolumeUser }>({
  volumeUser: [
    async ({ browser }, use, info) => {
      const data = volumeData();
      const use_ = info.project.use as Record<string, unknown>;
      const context: BrowserContext = await browser.newContext(contextOptions(use_));
      const violations: CspViolation[] = [];
      await context.exposeBinding(CSP_BINDING, (_source, violation: CspViolation) => {
        violations.push(violation);
      });
      await context.addInitScript({ content: listener });
      await context.addInitScript(instrumentation);
      const page = await context.newPage();
      await addAuthenticator(page);
      const origin = String(use_.baseURL);
      const onboarded = await onboard(page, { origin, name: 'Volume tester' });
      const user = await PageUser.open(page, onboarded.recoveryCode);
      const { code } = (await director('POST', '/volume/invite', {})) as { code: string };
      await joinWithCode(user, origin, code, 'Volume tester');
      await director('POST', '/volume/joined', { user_id: user.userId });
      if (info.project.name.endsWith('-mobile')) {
        const cdp = await context.newCDPSession(page);
        await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });
      }
      const open = async (path: string): Promise<number> => {
        await page.goto(path);
        await page.getByTestId('login.submit').waitFor({ timeout: 60_000 });
        const since = Date.now();
        await page.getByTestId('login.submit').click();
        return since;
      };
      await use({ page, userId: user.userId, data, violations, open });
      await context.close();
    },
    { scope: 'worker', timeout: 300_000 },
  ],
  volume: async ({ volumeUser }, use) => {
    const before = volumeUser.violations.length;
    await use(volumeUser);
    const added = volumeUser.violations.slice(before);
    if (added.length) throw new Error(`Content-Security-Policy violations: ${JSON.stringify(added)}`);
  },
});

export { expect } from '@playwright/test';

/** The path of the volume org. */
export const orgPath = (data: VolumeData, rest = ''): string => `/o/${data.org}${rest}`;
