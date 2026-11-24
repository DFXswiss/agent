// Shared steps of the whiteboard and notification specs: a team in Ada's org – Ada (admin) and Bob (member, joined
// with an invitation code through the API like the member spec), a workgroup "Research" that Ada writes and Bob only
// reads – each in its own browser context, both unlocked on a page of Ada's org.
import { type BrowserContext, expect, type Page } from '@playwright/test';
import { addAuthenticator } from './authenticator.ts';
import { joinWithCode } from './members.ts';
import { navigateInApp, onboard, type Onboarded } from './onboard.ts';
import { PageUser } from './user.ts';
import { bodyJson } from '../../src/core/encoding/json';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatTime } from '../../src/core/encoding/time';
import { newHeader, sealEvent } from '../../src/core/crypto/event';
import { epochKey, ORG, WG } from '../../src/core/log/state';
import type { VerifiedLog } from '../../src/core/log/verify';

export interface Member {
  readonly page: Page;
  readonly onboarded: Onboarded;
  readonly user: PageUser;
}

export interface Team {
  readonly org: string;
  readonly research: string;
  readonly ada: Member;
  readonly bob: Member;
}

async function person(page: Page, origin: string, name: string): Promise<Member> {
  await addAuthenticator(page);
  const onboarded = await onboard(page, { origin, name });
  return { page, onboarded, user: await PageUser.open(page, onboarded.recoveryCode) };
}

/** Opens `path` with a reload: the identity is locked again and unlocks with the passkey. */
export async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path.replace(/[?]/g, '\\?')}$`));
}

export { navigateInApp };

/** Ada invites through the member screen; returns the code. */
async function invite(page: Page, org: string): Promise<string> {
  await navigateInApp(page, `/o/${org}/members`);
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.name').fill('Invited person');
  await page.getByTestId('members.invite.submit').click();
  const code = (await page.getByTestId('members.invite.code').textContent())!;
  await page.getByTestId('members.invite.done').click();
  return code;
}

/** Ada's org with Bob as a member who reads (not writes) "Research"; both pages end on the org's home. */
export async function team(page: Page, newContext: () => Promise<BrowserContext>, origin: string): Promise<Team> {
  const ada = await person(page, origin, 'Ada Admin');
  const org = ada.onboarded.org;
  const research = await ada.user.createWorkgroup(org, 'Research');
  const code = await invite(page, org);
  const bob = await person(await (await newContext()).newPage(), origin, 'Bob Member');
  await joinWithCode(bob.user, origin, code, 'Bob Member');
  // Ada grants Bob read access to Research through the member screen
  const row = page.getByTestId('members.item').filter({ hasText: 'Bob Member' });
  await row.getByTestId('members.item.grant').click();
  await page.getByTestId('members.grant.access').selectOption('read');
  await page.getByTestId('members.grant.confirm').click();
  await expect(row.getByTestId('members.item.access')).toContainText('Research');
  await visit(bob.page, `/o/${org}`);
  return { org, research, ada, bob };
}

/** A whiteboard event sealed in Node by `author` with its own keys (`log` verified by a founder's page). */
export async function boardEvent(
  author: PageUser,
  log: VerifiedLog,
  board: { org: string; wg: string | null; task: string | null },
  kind: string,
  ref: string,
  body: unknown,
): Promise<unknown> {
  const keys = await author.keys(board.org, log);
  const scope = board.wg === null ? ORG : WG;
  const scopeId = board.wg ?? board.org;
  const epoch = log.state.epoch(scope, scopeId);
  const header = newHeader({
    id: uuid7(),
    org: board.org,
    scope,
    scope_id: scopeId,
    epoch,
    author: author.userId,
    kind,
    ts: formatTime(Date.now()),
    lh: log.head.seq,
    wg: board.wg ?? '',
    task: board.task ?? '',
    ref,
  });
  return sealEvent(keys.get(epochKey(scope, scopeId, epoch))!, header, bodyJson(body), author.identity.sig.privateKey);
}
