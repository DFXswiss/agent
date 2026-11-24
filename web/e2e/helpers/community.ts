// The community in the Playwright suite (14-community.md sections 4.2 and 6).
//
// - `startKeeper`: every spec shares one hub, so the community key of a new user has to come from a holder that is
//   online at that moment, and the hub announces new key requests at most once per 5 s. The global setup therefore
//   onboards one "keeper" user in its own browser before any spec runs and keeps it until the end of the suite: its
//   identity (opened in Node from its recovery code) creates the community key – it is the first member – and Node
//   seals the key to every open request once a second, exactly as a holder's browser does on the `request` frame.
//   The specs then see the real flow of a newcomer (request, wrap by another user, self-wrap) without waiting.
// - Screen helpers: open the community from the navigation (desktop row or the phone's "More" sheet), write a message,
//   find a message by its text.
import { chromium, expect, type Page } from '@playwright/test';
import { b64uDecode, b64uEncode } from '../../src/core/encoding/b64u';
import * as C from '../../src/core/crypto/community';
import { Secret } from '../../src/core/crypto/secret';
import { addAuthenticator } from './authenticator.ts';
import { navigateInApp, ORG_PATH } from './onboard.ts';
import { platformInvite } from './platform.ts';
import { PageUser } from './user.ts';

interface CommunityState {
  readonly epoch: { readonly epoch: number; readonly key_check: string } | null;
  readonly wrap: { readonly epoch: number; readonly wrap: string } | null;
}

/** The keeper's key: creates epoch 1 if there is none yet, else opens the keeper's self-wrap. */
async function keeperKey(user: PageUser): Promise<{ epoch: number; secret: Secret }> {
  await user.request('POST', '/v1/community/join', {});
  const state = (await user.request('GET', '/v1/community')) as CommunityState;
  if (state.epoch !== null && state.wrap !== null) {
    const secret = await C.unwrapCommunityKey(user.identity.kem, state.epoch.epoch, user.userId, state.wrap.wrap);
    return { epoch: state.epoch.epoch, secret };
  }
  if (state.epoch !== null) throw new Error('the community key was created by another user before the keeper');
  const secret = Secret.random(32);
  const wrap = await C.wrapCommunityKey(user.identity.kemPublic(), 1, user.userId, secret);
  await user.request('POST', '/v1/community/epochs', {
    epoch: 1,
    key_check: b64uEncode(await C.keyCheck(secret, 1)),
    wrap,
    sig: await C.signCommunityWrap(user.identity.sig.privateKey, 1, user.userId, wrap),
  });
  return { epoch: 1, secret };
}

/** Seals the key to every open request (`GET /v1/community/requests`, `POST …/requests/wraps`). */
async function serve(user: PageUser, key: { epoch: number; secret: Secret }): Promise<void> {
  const body = (await user.request('GET', '/v1/community/requests?limit=100')) as {
    requests: { id: string; kem_pub: string }[];
  };
  if (body.requests.length === 0) return;
  const wraps = [];
  for (const request of body.requests) {
    const wrap = await C.wrapCommunityKey(b64uDecode(request.kem_pub, 32), key.epoch, request.id, key.secret);
    const sig = await C.signCommunityWrap(user.identity.sig.privateKey, key.epoch, request.id, wrap);
    wraps.push({ request: request.id, epoch: key.epoch, wrap, sig });
  }
  await user.request('POST', '/v1/community/requests/wraps', { wraps });
}

/** Onboards the keeper in its own browser and serves key requests until `stop()`. */
export async function startKeeper(origin: string): Promise<{ stop: () => Promise<void> }> {
  const browser = await chromium.launch();
  try {
    const page = await (await browser.newContext({ baseURL: origin })).newPage();
    await addAuthenticator(page);
    const invite = await platformInvite(origin);
    await page.goto(`/invite#${invite.code}`);
    await page.getByTestId('invite.display-name').fill('Community keeper');
    await page.getByTestId('invite.create-passkey').click();
    await page.getByTestId('invite.unlock').click();
    await page.waitForURL(ORG_PATH);
    await navigateInApp(page, '/settings');
    await page.getByTestId('passkeys.recovery.create').click();
    const code = (await page.getByTestId('recovery-code.code').textContent())!;
    await page.getByTestId('recovery-code.continue').click();
    await page.getByTestId('passkeys.recovery.done').waitFor();
    const user = await PageUser.open(page, code);
    // the keeper's own browser creates the key at the same time (its community job): read again after a conflict
    let key: { epoch: number; secret: Secret } | null = null;
    for (let attempt = 0; key === null; attempt += 1) {
      key = await keeperKey(user).catch(async (error: unknown) => {
        if (attempt >= 5) throw error;
        await new Promise((resolve) => setTimeout(resolve, 1000));
        return null;
      });
    }
    const held = key;
    let running: Promise<void> = Promise.resolve();
    const timer = setInterval(() => {
      // one round at a time; a failed round is tried again a second later
      running = running.then(() => serve(user, held)).catch(() => undefined);
    }, 1000);
    return {
      stop: async () => {
        clearInterval(timer);
        await running;
        await browser.close();
      },
    };
  } catch (error) {
    await browser.close();
    throw error;
  }
}

/** Opens the community from the navigation: the row on wide screens, "More" on phones. */
export async function openCommunity(page: Page): Promise<void> {
  const more = page.getByTestId('shell.bottom.more');
  if (await more.isVisible()) {
    await more.click();
    await page.getByTestId('shell.more.community').click();
  } else {
    await page.getByTestId('shell.nav.community').click();
  }
  await expect(page).toHaveURL(/\/community$/);
  await expect(page.getByTestId('community.screen')).toHaveAttribute('data-phase', 'ready');
}

/** Waits until the browser holds the community key (the composer or the muted notice is shown). */
export async function unlocked(page: Page): Promise<void> {
  await expect(page.getByTestId('community.composer').or(page.getByTestId('community.muted'))).toBeVisible({
    timeout: 60_000,
  });
}

/** Writes `text` (lines joined with Shift+Enter) and sends it with Enter; `name` fills the first-post name. */
export async function write(page: Page, text: string, name?: string): Promise<void> {
  if (name !== undefined) await page.getByTestId('community.name').fill(name);
  const field = page.getByTestId('community.composer.text');
  const lines = text.split('\n');
  await field.fill(lines[0]!);
  for (const line of lines.slice(1)) {
    await field.press('Shift+Enter');
    await field.pressSequentially(line);
  }
  await field.press('Enter');
  // sent: the field is empty again (or the post was refused and says why)
  const failure = page.getByTestId('community.composer.error');
  await expect.poll(async () => (await failure.isVisible()) || (await field.inputValue()) === '').toBe(true);
  await expect(failure).toHaveCount(0);
}

/** The message whose text contains `text`. */
export const messageWith = (page: Page, text: string) =>
  page.getByTestId('community.message').filter({ hasText: text });
