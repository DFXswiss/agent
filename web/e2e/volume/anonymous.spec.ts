// Volume gate (ADR-086 §4): the screens without a signed-in user (the start redirect, sign-in, invitation,
// recovery, device connection, not found) – no data volume reaches them; one cheap spec keeps them in the gate.
import { appliedThrottle, measure } from './budget.ts';
import { contextOptions, test } from './fixtures.ts';

export const COVERS = ['route:/', 'route:/login', 'route:/invite', 'route:/recover', 'route:/connect', 'route:*'];

const SCREENS: readonly [string, string, string][] = [
  ['route:/', '/', 'login.submit'],
  ['route:/login', '/login', 'login.submit'],
  ['route:/invite', '/invite', 'invite.page'],
  ['route:/recover', '/recover', 'recover.code'],
  ['route:/connect', '/connect', 'connect.page'],
  ['route:*', '/no-such-page', 'notfound.home'],
];

test('route:/ and the other anonymous screens', async ({ browser }, info) => {
  const context = await browser.newContext(contextOptions(info.project.use as Record<string, unknown>));
  const page = await context.newPage();
  const rate = appliedThrottle(info);
  if (rate > 1) {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Emulation.setCPUThrottlingRate', { rate });
  }
  for (const [id, path, control] of SCREENS) {
    const since = Date.now();
    await page.goto(path);
    await measure(page, info, id, { primary: page.getByTestId(control), since });
  }
  await context.close();
});
