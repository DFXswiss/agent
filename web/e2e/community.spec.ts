// The community (12-community.md sections 4.2, 6 and 8) against the real hub, on the desktop and the phones: users of
// different orgs chat live (no reload) with the first-post notice, line breaks and links, the unread badge in the
// navigation and on the phone's "More"; the history after signing in again; a freshly invited user reads the earlier
// messages and writes; an author's deletion reaches the open client of another user; a moderator (granted through the
// operator's root-signed request with the user ID from the settings section "Account") removes a message, mutes and
// unmutes a user, which the muted user's open client follows; the rate limit names the wait time. Every user of the
// suite is in the same community, so each test finds its own messages by a unique tag.
import type { BrowserContextOptions, Page } from '@playwright/test';
import { uuid7 } from '../src/core/encoding/ids';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { messageWith, openCommunity, unlocked, write } from './helpers/community.ts';
import { navigateInApp, onboard } from './helpers/onboard.ts';
import { setModerator } from './helpers/platform.ts';

interface Person {
  readonly page: Page;
  readonly name: string;
  readonly org: string;
}

const tag = () => uuid7().slice(-12);

// several users sign up per test and a newcomer waits for a key holder (a request stays leased to one holder for up to
// 30 s, 12-community.md section 4.1): more time than the default under a loaded test device
test.describe.configure({ timeout: 90_000 });

/** A new user (own platform invitation, own private org) on `page`, in the community with the key. */
async function person(page: Page, origin: string, name: string): Promise<Person> {
  await addAuthenticator(page);
  const { org } = await onboard(page, { origin, name, code: false });
  await openCommunity(page);
  await unlocked(page);
  return { page, name, org };
}

/** A second browser with the device profile of the project (desktop or phone). */
async function browserPage(
  newContext: (options?: BrowserContextOptions) => Promise<import('@playwright/test').BrowserContext>,
) {
  const context = await newContext(test.info().project.use as BrowserContextOptions);
  return context.newPage();
}

test('users of different orgs chat live, with the notice before the first post and the unread badge', async ({
  page,
  baseURL,
  newContext,
}) => {
  const id = tag();
  const ada = await person(page, baseURL!, `Ada ${id}`);
  const bob = await person(await browserPage(newContext), baseURL!, `Bob ${id}`);
  expect(ada.org).not.toBe(bob.org);
  // before the first own post: the notice and the editable name
  await expect(ada.page.getByTestId('community.notice.text')).toContainText(
    'Your name is visible to all users of agent.dfx.swiss.',
  );
  await expect(ada.page.getByTestId('community.notice.text')).toContainText('do not share confidential information');
  await write(ada.page, `Hello ${id}\nsecond line https://example.com/${id}`, ada.name);
  const atBob = messageWith(bob.page, `Hello ${id}`);
  await expect(atBob.getByTestId('community.message.name')).toHaveText(ada.name);
  await expect(atBob.getByTestId('community.message.mark')).toHaveText(/^#[A-Z2-7]{4}$/);
  await expect(atBob.getByTestId('community.message.time')).toHaveText(/^\d{2}:\d{2}$/);
  await expect(atBob.getByTestId('community.message.text')).toContainText(`Hello ${id}\nsecond line`);
  const link = atBob.getByTestId('community.message.link');
  await expect(link).toHaveAttribute('href', `https://example.com/${id}`);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
  await expect(atBob).toHaveAttribute('data-own', 'false');
  await expect(messageWith(ada.page, `Hello ${id}`)).toHaveAttribute('data-own', 'true');
  // after the first post: "You write as …"
  await expect(ada.page.getByTestId('community.name.as')).toContainText(ada.name);
  await write(bob.page, `Hi Ada ${id}`, bob.name);
  await expect(messageWith(ada.page, `Hi Ada ${id}`).getByTestId('community.message.name')).toHaveText(bob.name);
  // Bob elsewhere: a new message shows the badge (phones: in "More" and as a dot), opening the community clears it
  await navigateInApp(bob.page, `/o/${bob.org}`);
  await write(ada.page, `Unread ${id}`);
  const phone = await bob.page.getByTestId('shell.bottom.more').isVisible();
  if (phone) {
    await expect(bob.page.getByTestId('shell.bottom.more.dot')).toBeVisible();
    await bob.page.getByTestId('shell.bottom.more').click();
    await expect(bob.page.getByTestId('shell.more.community.badge')).toHaveText(/^([1-9]\d?|99\+)$/);
    await bob.page.getByTestId('shell.more.close').click();
  } else {
    await expect(bob.page.getByTestId('shell.nav.community.badge')).toHaveText(/^([1-9]\d?|99\+)$/);
  }
  await openCommunity(bob.page);
  await expect(messageWith(bob.page, `Unread ${id}`)).toBeVisible();
  await expect(bob.page.getByTestId(phone ? 'shell.bottom.more.dot' : 'shell.nav.community.badge')).toHaveCount(0);
});

test('an own deletion reaches the open client; the history is there after signing in again', async ({
  page,
  baseURL,
  newContext,
}) => {
  const id = tag();
  const ada = await person(page, baseURL!, `Ada ${id}`);
  const bob = await person(await browserPage(newContext), baseURL!, `Bob ${id}`);
  await write(ada.page, `Keep ${id}`, ada.name);
  await write(ada.page, `Oops ${id}`);
  const oops = messageWith(bob.page, `Oops ${id}`);
  await expect(oops).toBeVisible();
  const seq = await oops.getAttribute('data-seq');
  // the author deletes it (confirmed); Bob's open client drops the text at once
  await messageWith(ada.page, `Oops ${id}`).getByTestId('community.message.menu').click();
  await ada.page.getByTestId('community.actions.delete').click();
  await expect(ada.page.getByTestId('community.actions.question')).toHaveText('Delete this message for everyone?');
  await ada.page.getByTestId('community.actions.confirm').click();
  const removed = bob.page.locator(`[data-testid="community.message"][data-seq="${seq}"]`);
  await expect(removed).toHaveAttribute('data-status', 'removed');
  await expect(removed.getByTestId('community.message.removed')).toHaveText('Message removed');
  await expect(bob.page.getByText(`Oops ${id}`)).toHaveCount(0);
  // a new sign-in: the history (and the deletion) is still there, the name is remembered
  await navigateInApp(ada.page, '/settings');
  await ada.page.getByTestId('settings.sign-out').click();
  await expect(ada.page).toHaveURL(/\/login$/);
  await ada.page.getByTestId('login.submit').click();
  await expect(ada.page).toHaveURL(/\/o\//);
  await openCommunity(ada.page);
  await unlocked(ada.page);
  await expect(messageWith(ada.page, `Keep ${id}`)).toHaveAttribute('data-own', 'true');
  await expect(ada.page.locator(`[data-testid="community.message"][data-seq="${seq}"]`)).toHaveAttribute(
    'data-status',
    'removed',
  );
  await expect(ada.page.getByTestId('community.name.as')).toContainText(ada.name);
});

test('a freshly invited user reads the earlier messages and writes', async ({ page, baseURL, newContext }) => {
  const id = tag();
  const ada = await person(page, baseURL!, `Ada ${id}`);
  await write(ada.page, `Before Cleo ${id}`, ada.name);
  await expect(messageWith(ada.page, `Before Cleo ${id}`)).toBeVisible();
  // Cleo is invited only now: she gets the key from another user and reads what was written before she joined
  const cleo = await person(await browserPage(newContext), baseURL!, `Cleo ${id}`);
  await expect(messageWith(cleo.page, `Before Cleo ${id}`).getByTestId('community.message.text')).toHaveText(
    `Before Cleo ${id}`,
  );
  await expect(cleo.page.getByTestId('community.notice')).toBeVisible();
  await write(cleo.page, `Cleo here ${id}`, cleo.name);
  await expect(messageWith(ada.page, `Cleo here ${id}`).getByTestId('community.message.name')).toHaveText(cleo.name);
});

test('a moderator removes a message and mutes a user; the muted user sees it live', async ({
  page,
  baseURL,
  newContext,
}) => {
  const id = tag();
  const mod = await person(page, baseURL!, `Mo ${id}`);
  const bob = await person(await browserPage(newContext), baseURL!, `Bob ${id}`);
  // the operator grants the role with the user ID from the settings section "Account"
  await navigateInApp(mod.page, '/settings');
  const userId = (await mod.page.getByTestId('settings.account.user-id').textContent())!;
  expect(userId).toMatch(/^[0-9a-f-]{36}$/);
  await setModerator(baseURL!, userId);
  // the role is read at the next start of the client: sign in again
  await mod.page.goto('/community');
  await mod.page.getByTestId('login.submit').click();
  await expect(mod.page).toHaveURL(/\/community$/);
  await unlocked(mod.page);
  await write(mod.page, `House rules ${id}`, mod.name);
  await expect(messageWith(bob.page, `House rules ${id}`).getByTestId('community.message.moderator')).toHaveText(
    'Moderator',
  );
  // removal reaches Bob's open client
  await write(bob.page, `Spam ${id}`, bob.name);
  const spam = messageWith(mod.page, `Spam ${id}`);
  await expect(spam).toBeVisible();
  const seq = await spam.getAttribute('data-seq');
  await spam.getByTestId('community.message.menu').click();
  await mod.page.getByTestId('community.actions.remove').click();
  await mod.page.getByTestId('community.actions.confirm').click();
  await expect(bob.page.locator(`[data-testid="community.message"][data-seq="${seq}"]`)).toHaveAttribute(
    'data-status',
    'removed',
  );
  // mute: Bob's composer gives way to the muted notice
  await write(bob.page, `More ${id}`);
  await messageWith(mod.page, `More ${id}`).getByTestId('community.message.menu').click();
  await mod.page.getByTestId('community.actions.mute').click();
  await expect(mod.page.getByTestId('community.actions.question')).toContainText(bob.name);
  await mod.page.getByTestId('community.actions.confirm').click();
  await expect(mod.page.getByTestId('community.status')).toHaveText(`${bob.name} is muted.`);
  await expect(bob.page.getByTestId('community.muted')).toBeVisible();
  await expect(bob.page.getByTestId('community.composer')).toHaveCount(0);
  // the list of muted users names Bob; unmuting gives Bob the composer back
  await mod.page.getByTestId('community.mutes.open').click();
  const row = mod.page.getByTestId('community.mutes.item').filter({ hasText: bob.name });
  await expect(row.getByTestId('community.mutes.mark')).toHaveText(/^#[A-Z2-7]{4}$/);
  await row.getByTestId('community.mutes.unmute').click();
  await expect(row).toHaveCount(0);
  await mod.page.getByTestId('community.mutes.close').click();
  await expect(bob.page.getByTestId('community.composer')).toBeVisible();
});

test('the rate limit names the wait time', async ({ page, baseURL }) => {
  const id = tag();
  const ada = await person(page, baseURL!, `Ada ${id}`);
  await ada.page.getByTestId('community.name').fill(ada.name);
  const field = ada.page.getByTestId('community.composer.text');
  const failure = ada.page.getByTestId('community.composer.error');
  // a burst of posts, each sent once the previous one is through, until the hub refuses one
  for (let index = 0; index < 12 && !(await failure.isVisible()); index += 1) {
    await field.fill(`Burst ${index} ${id}`);
    await field.press('Enter');
    await expect.poll(async () => (await failure.isVisible()) || (await field.inputValue()) === '').toBe(true);
  }
  await expect(failure).toHaveAttribute('data-code', 'rate_limited');
  await expect(failure).toHaveText(/You are writing very fast\. Please wait \d+ seconds?, then send again\./);
  // the text is kept for the next try
  await expect(field).not.toHaveValue('');
});
