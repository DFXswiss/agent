// Fixtures of the volume specs: the data set of the project's lane (seeded by the first worker of the lane,
// decisions/HOT-21.md), one browser user per volume project (worker), onboarded with its own passkey and
// joined as admin to the volume org, kept for every view of the worker (the projects run one worker each). It signs
// in once after the join (the slow first verification of the org's logs); each view is then opened with a page load
// and the (then fast) passkey unlock. The phone
// project runs with 4× CPU throttling (ADR-086 §4). No browser coverage here: the precise V8 counters would distort
// the measurements (the gate measures the build as it is served); the CSP listener of the suite stays.
import { type BrowserContext, type Page, test as base } from '@playwright/test';
import { CSP_BINDING, type CspViolation, enterLane, stayOff } from '../fixtures/test.ts';
import { addAuthenticator } from '../helpers/authenticator.ts';
import { joinWithCode } from '../helpers/members.ts';
import { onboard } from '../helpers/onboard.ts';
import { PageUser } from '../helpers/user.ts';
import { appliedThrottle, calibrate, frozenPages, instrumentation, useCdp } from './budget.ts';
import { director, seed, type VolumeData } from './data.ts';

export interface VolumeUser {
  readonly page: Page;
  readonly userId: string;
  readonly data: VolumeData;
  readonly violations: CspViolation[];
  /** The recovery code of the user (specs that act through the API open its identity with it). */
  readonly recoveryCode: string;
  /** Opens `path` with a page load and the unlock; returns the moment of the unlock gesture (the user's action). */
  open(path: string, unlock?: boolean): Promise<number>;
}

const listener = `document.addEventListener('securitypolicyviolation', (e) => {
  window['${CSP_BINDING}']({ page: location.href, directive: e.effectiveDirective, blocked: e.blockedURI, sample: e.sample });
});`;

/** Context options of the project's device (viewport, touch, user agent …); `baseURL` from the config. */
export function contextOptions(use: Record<string, unknown>) {
  const keys = ['viewport', 'screen', 'userAgent', 'deviceScaleFactor', 'isMobile', 'hasTouch', 'baseURL', 'locale'];
  return Object.fromEntries(keys.filter((key) => use[key] !== undefined).map((key) => [key, use[key]]));
}

export const test = base.extend<{ volume: VolumeUser }, { lane: string; volumeUser: VolumeUser }>({
  // every test of a volume project waits for its lane, also one without a signed-in user (`anonymous.spec.ts`)
  lane: [
    async ({ browserName: _browserName }, use, info) => {
      await use(await enterLane(info.project.name));
    },
    { scope: 'worker', auto: true, timeout: 26 * 60_000 },
  ],
  volumeUser: [
    async ({ browser, lane: _lane }, use, info) => {
      const data = await seed(info.project.name);
      const use_ = info.project.use as Record<string, unknown>;
      const context: BrowserContext = await browser.newContext(contextOptions(use_));
      // the measured sign-in flows of the volume gate are those of a browser that does not stay signed in (rule E-40)
      await stayOff(context);
      const violations: CspViolation[] = [];
      await context.exposeBinding(CSP_BINDING, (_source, violation: CspViolation) => {
        violations.push(violation);
      });
      await context.addInitScript({ content: listener });
      await context.addInitScript(instrumentation);
      let page = await context.newPage();
      let authenticator = await addAuthenticator(page);
      useCdp(page, authenticator.cdp);
      const origin = String(use_.baseURL);
      const onboarded = await onboard(page, { origin, name: 'Volume tester' });
      const user = await PageUser.open(page, onboarded.recoveryCode);
      const { code } = (await director('POST', '/volume/invite', {})) as { code: string };
      await joinWithCode(user, origin, code, 'Volume tester');
      await director('POST', '/volume/joined', { user_id: user.userId });
      // the first sign-in after the join (the app learns the new org and the platform role) on a light page
      await page.goto('/settings');
      const started = Date.now();
      await page.getByTestId('login.submit').click({ timeout: 60_000 });
      await page.getByTestId('settings.page').waitFor({ timeout: 300_000 });
      console.log(`KOR-35 ${info.project.name} first sign-in after the join: ${Date.now() - started} ms`);
      const throttle = async () => {
        // measurements only: the set-up above runs at full speed
        const rate = appliedThrottle(info);
        if (rate > 1) await authenticator.cdp.send('Emulation.setCPUThrottlingRate', { rate });
      };
      await throttle();
      /** A frozen tab cannot navigate: a new tab with the same passkey (copied into its own authenticator); if that
       *  passkey gives no PRF there, the recovery code unlocks. */
      let replaced = false;
      const replace = async () => {
        replaced = true;
        const { credentials } = await authenticator.cdp
          .send('WebAuthn.getCredentials', { authenticatorId: authenticator.id })
          .catch(() => ({ credentials: [] }));
        const old = page;
        page = await context.newPage();
        void old.close({ runBeforeUnload: false }).catch(() => undefined);
        authenticator = await addAuthenticator(page);
        for (const credential of credentials)
          await authenticator.cdp.send('WebAuthn.addCredential', { authenticatorId: authenticator.id, credential });
        useCdp(page, authenticator.cdp);
        await throttle();
      };
      // each view: a page load and the passkey unlock (fast once the logs are verified and cached), so a view that
      // breaks the page cannot spoil the next one
      const open = async (path: string, unlock = true): Promise<number> => {
        if (frozenPages.has(page)) await replace();
        if (!unlock) {
          const since = Date.now();
          await page.goto(path, { timeout: 60_000 }).catch(() => page.goto(path));
          return since;
        }
        await page.goto(path, { timeout: 60_000 }).catch(() => page.goto(path));
        await page.getByTestId('login.submit').waitFor({ timeout: 60_000 });
        await calibrate(page, info);
        const since = Date.now();
        await page.getByTestId('login.submit').click();
        const recovery = page.getByTestId('prf-missing.recovery.input');
        const needsCode =
          replaced &&
          (await Promise.race([
            recovery.waitFor({ timeout: 15_000 }).then(() => true),
            page
              .getByTestId('login.page')
              .waitFor({ state: 'detached', timeout: 15_000 })
              .then(() => false),
          ]).catch(() => false));
        if (needsCode) {
          await recovery.fill(onboarded.recoveryCode);
          await page.getByTestId('prf-missing.recovery.submit').click();
        }
        return since;
      };
      await use({
        get page() {
          return page;
        },
        userId: user.userId,
        data,
        violations,
        recoveryCode: onboarded.recoveryCode,
        open,
      });
      await context.close();
    },
    // the seed (about 3 minutes alone, longer beside the other lanes) and the first sign-in after the join
    { scope: 'worker', timeout: 40 * 60_000 },
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
