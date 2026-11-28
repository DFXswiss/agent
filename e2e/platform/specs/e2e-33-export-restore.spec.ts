// E2E-33 (07-testing.md sections 2.1 and 5.6; 02-data-model.md sections 1.3, 3 and 4; 03-api.md sections 5 and 6.1;
// 04-crypto.md section 6.1 L9/L10): disaster recovery. The flows of E2E-01 (platform invitations: Ada's and Bob's
// private orgs), E2E-06 (a first answer), E2E-21 (the GitHub operation chain) and E2E-25 (whiteboard entries by Ada
// and by Wendy, who joined through Ada's invitation) fill the hub. Then the orchestrator stops the hub, exports every
// org (`agent-hub export --org`), creates a fresh database, migrates it, restores all exports (`agent-hub restore`) and
// starts the hub again on it: log heads, per-scope event counts and hashes, the audit head and the newest checkpoint
// are identical, the projections equal their rebuild and the audit chain verifies. The browsers and the runner
// reconnect with their stored cursors and verified heads - no `log_rollback`, no `log_fork`, no reset cursor - and a
// follow-up prompt is answered. Last, the runner's store is wiped (identity kept) and the runner restarted: its outbox
// continues at `last_origin_seq + 1`, no delivered prompt comes again, no `origin_seq_conflict`, and a further
// follow-up is answered. Runs on T1.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { addAuthenticator } from '../fixtures/authenticator.ts';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  allowRepositories,
  connectGitHub,
  expectGitHubOk,
  hubOperations,
  repository,
  seedGitHub,
  worldOf,
} from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  home,
  invite,
  onboard,
  onboardWithCode,
  openChat,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { inviteMember, joinAsNewUser, writeEntry } from '../fixtures/team.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-33-export-restore.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz33-octo-b5m1';
const REPO = 'octo-org/hello';
const FIRST = 'E2E33-FIRST What is 2+2? kz33-prompt-a2k7';
const CHAIN = 'E2E33-CHAIN kz33-prompt-c8n4: add notes to octo-org/hello and open a draft pull request';
const AFTER_RESTORE = 'E2E33-AFTER-RESTORE kz33-prompt-r1x5: are you still there?';
const AFTER_WIPE = 'E2E33-AFTER-WIPE kz33-prompt-w3v6: and now?';
const ORG_ENTRY = 'Recovery rules kz33-org-entry-h7d2';
const WG_ENTRY = 'Recovery notes kz33-wg-entry-q5s9';
const ORG_AFTER = 'Restored rules kz33-org-after-m3j8';
const BOB_AFTER = 'Bob restored kz33-bob-after-p2x6';
const CANARIES = [
  'kz33-prompt-a2k7',
  'kz33-prompt-c8n4',
  'kz33-prompt-r1x5',
  'kz33-prompt-w3v6',
  'kz33-answer-f4w8',
  'kz33-answer-r6t2',
  'kz33-answer-w9p3',
  'kz33-org-entry-h7d2',
  'kz33-wg-entry-q5s9',
  'kz33-org-after-m3j8',
  'kz33-bob-after-p2x6',
  'kz33-answer-z1f5k9m3',
  'kz33-file-g3w8n5c1',
  'kz33-title-p9d4r7v2',
  'kz33-comment-t6j2h8q4',
  LOGIN,
  'kz33-name-a1',
  'kz33-name-b2',
  'kz33-name-w3',
];

interface Fingerprint {
  readonly orgs: readonly { id: string; seq: number; hash: string }[];
  readonly platform: { seq: number; hash: string } | null;
  readonly scopes: readonly { scope_id: string; events: number; last_seq: number; head: number; digest: string }[];
  readonly audit: { seq: number; hash: string };
  readonly checkpoint: { key: string; seq: number; hash: string } | null;
  readonly devices: readonly { id: string; last_origin_seq: number }[];
}

interface Recovery {
  readonly orgs: readonly string[];
  readonly exported: Readonly<Record<string, string>>;
  readonly plaintext: readonly string[];
  readonly restored: string;
  readonly before: Fingerprint;
  readonly after: Fingerprint;
  readonly rebuild: { ok: boolean; differences: readonly string[]; rebuilt: Readonly<Record<string, number>> };
  readonly audit: { ok: boolean; head: { seq: number; hash: string } };
}

interface Wiped {
  readonly state: string;
  readonly device: string;
  readonly last_origin_seq: number;
}

interface Stored {
  readonly heads: Readonly<Record<string, unknown>>;
  readonly cursors: Readonly<Record<string, number>>;
}

/** What the page keeps in IndexedDB (06-web.md section 2.2): verified heads per org and the live cursors. */
async function stored(page: Page): Promise<Stored> {
  return page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('agent-platform');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const all = (name: string) =>
      new Promise<Record<string, unknown>>((resolve, reject) => {
        const store = db.transaction(name, 'readonly').objectStore(name);
        const keys = store.getAllKeys();
        const values = store.getAll();
        values.onsuccess = () =>
          resolve(Object.fromEntries(keys.result.map((key, index) => [String(key), values.result[index]])));
        values.onerror = () => reject(values.error);
      });
    const heads = await all('heads');
    const cursors = Object.values(await all('cursors'))[0] as Record<string, number> | undefined;
    db.close();
    return { heads, cursors: cursors ?? {} };
  });
}

/** The audit position before this spec: the shared platform device may carry audits of earlier specs. */
const auditHead = async (orchestrator: Orchestrator): Promise<number> =>
  (await orchestrator.hubQuery<{ seq: number }>(`SELECT coalesce(max(seq), 0)::int AS seq FROM hub.audit`))[0]!.seq;

/** The runner's audits of `device` after audit position `since` (only this spec's: the shared platform device may
 * carry audits of earlier specs; a request of a user the runner's log copy did not show yet is no longer one of
 * them, the runner judges it after refreshing its logs, KOR-17). */
const runnerAlerts = (orchestrator: Orchestrator, device: string, since: number) =>
  orchestrator.hubQuery<{ action: string }>(
    `SELECT action FROM hub.audit WHERE action LIKE 'runner.%' AND device_id = '${uuid(device)}' AND seq > ${since}`,
  );

const hubAcked = async (orchestrator: Orchestrator, device: string): Promise<number> =>
  (
    await orchestrator.hubQuery<{ seq: number }>(
      `SELECT coalesce(max(last_origin_seq), 0)::int AS seq FROM hub.device_stream WHERE device_id = '${uuid(device)}'`,
    )
  )[0]!.seq;

const pending = async (orchestrator: Orchestrator): Promise<number> =>
  (
    await runnerStore<{ n: number }>(
      orchestrator,
      'SELECT count(*)::int AS n FROM runner.outbox WHERE pushed_at IS NULL',
    )
  )[0]!.n;

/** A follow-up from the chat composer of the open task. */
async function followUp(page: Page, text: string): Promise<void> {
  await page.getByTestId('chat.composer.prompt').fill(text);
  await page.getByTestId('chat.composer.send').click();
  await expect(page.getByTestId('chat.prompt.text').filter({ hasText: text })).toBeVisible({ timeout: 30_000 });
}

async function expectUnblocked(page: Page): Promise<void> {
  await expect(page.getByTestId('shell.blocked')).toHaveCount(0);
}

test.describe.configure({ mode: 'serial' });

test('@E2E-33 @R-6.8 @R-P.5 @needs-vm a restored hub serves identical logs, events and audit; clients and a wiped runner continue', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, 'octo-org', [repository(REPO)]));
  const device = await platformDevice(orchestrator);
  const since = await auditHead(orchestrator);
  await poolReady(orchestrator);

  // E2E-01: two private orgs from platform invitations (Ada in this page, Bob in a browser profile of his own).
  await authenticator('platform');
  const ada = await onboard(page, orchestrator, 'Ada kz33-name-a1');
  const bobContext = await browser.newContext({ baseURL: e2e.hubOrigin });
  const bob = await bobContext.newPage();
  await addAuthenticator(bob, 'platform');
  const bobOrg = (await onboardWithCode(bob, (await invite(orchestrator, 'user')).code, 'Bob kz33-name-b2')).org;

  // E2E-06: the first task is answered.
  const first = await sendPrompt(page, FIRST);
  await expectAnswerInChat(page, '2+2 is 4 kz33-answer-f4w8.');

  // E2E-21: the GitHub operation chain of a second task.
  await home(page);
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(ada.org)}'`,
  );
  await allowRepositories(page, ada.org, org!.default_wg, [REPO]);
  await home(page);
  const chain = await sendPrompt(page, CHAIN);
  await expectAnswerInChat(
    page,
    'Draft pull request #1 is open: https://github.example/octo-org/hello/pull/1 kz33-answer-z1f5k9m3.',
  );
  expect((await hubOperations(orchestrator, chain)).map((op) => op.state)).toEqual(Array(8).fill('succeeded'));

  // E2E-25: whiteboard entries on the org board (Ada) and the workgroup board (Wendy, who joined Ada's org).
  const wendy = await joinAsNewUser(browser, e2e, ada.org, await inviteMember(page, ada.org, {}), 'Wendy kz33-name-w3');
  await writeEntry(page, `/o/${ada.org}/whiteboard`, ORG_ENTRY);
  await writeEntry(wendy.page, `/o/${ada.org}/w/${org!.default_wg}/whiteboard`, WG_ENTRY);
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: ORG_ENTRY })).toBeVisible();

  // Every runner item reached the hub (the fingerprint compares the device's acknowledged position).
  await expect
    .poll(
      async () =>
        (
          await runnerStore<{ n: number }>(
            orchestrator,
            'SELECT count(*)::int AS n FROM runner.outbox WHERE pushed_at IS NULL',
          )
        )[0]!.n,
      { timeout: 60_000 },
    )
    .toBe(0);
  const pages = [page, bob, wendy.page];
  const kept = await Promise.all(pages.map(stored));

  // Export every org, fresh database, migrate, restore, hub restarted on the restored database. In a whole-suite run
  // the earlier specs' orgs are on the hub too: every org of the hub is exported, this spec's two among them.
  const orgs = (await orchestrator.hubQuery<{ id: string }>('SELECT id::text AS id FROM hub.org')).map((row) => row.id);
  expect(orgs).toEqual(expect.arrayContaining([ada.org, bobOrg]));
  const recovery = await orchestrator.call<Recovery>('POST', '/hub/export-restore', { canaries: CANARIES });
  expect([...recovery.orgs].sort()).toEqual(orgs.sort());
  for (const org of recovery.orgs) expect(recovery.exported[org]).toMatch(/^exported [0-9]+$/);
  expect(recovery.plaintext).toEqual([]);
  expect(recovery.restored).toMatch(/^restored [0-9]+$/);
  expect(recovery.after).toEqual(recovery.before);
  expect(recovery.before.orgs.map((row) => row.id).sort()).toEqual(orgs.sort());
  expect(recovery.before.scopes.map((row) => row.scope_id)).toEqual(expect.arrayContaining([ada.org, bobOrg]));
  for (const scope of recovery.before.scopes) expect([scope.events, scope.last_seq]).toEqual([scope.head, scope.head]);
  expect(recovery.before.devices.map((row) => row.id)).toContain(device);
  expect(recovery.rebuild).toMatchObject({ ok: true, differences: [] });
  expect(Object.keys(recovery.rebuild.rebuilt)).toEqual(expect.arrayContaining(['orgs', 'platform', 'whiteboards']));
  expect(recovery.audit.ok).toBe(true);
  expect(recovery.audit.head).toEqual(recovery.before.audit);

  // The browsers reconnect with their stored cursors: a new entry in each org reaches every page live through its
  // reconnected stream, no cursor went back, the verified heads stay, nothing is blocked.
  await expect.poll(async () => (await orchestrator.runners())['runner-1'], { timeout: 60_000 }).toBe('running');
  await writeEntry(page, `/o/${ada.org}/whiteboard`, ORG_AFTER);
  await writeEntry(bob, `/o/${bobOrg}/whiteboard`, BOB_AFTER);
  const orgOf = [ada.org, bobOrg, ada.org];
  for (const [index, current] of pages.entries()) {
    const scope = orgOf[index]!;
    await expect
      .poll(async () => (await stored(current)).cursors[scope] ?? 0, { timeout: 60_000 })
      .toBeGreaterThan(kept[index]!.cursors[scope] ?? 0);
    await expectUnblocked(current);
    const now = await stored(current);
    expect(now.heads).toEqual(kept[index]!.heads);
    for (const [cursorScope, cursor] of Object.entries(kept[index]!.cursors))
      expect(now.cursors[cursorScope] ?? 0).toBeGreaterThanOrEqual(cursor);
  }

  // A follow-up prompt in the first task works on the restored hub; the runner raised no log alarm.
  await navigateInApp(page, `/o/${ada.org}/t/${first}`);
  await openChat(page); // the chat is the second tab (KONZEPT E-43)
  await expect(page.getByTestId('chat.output').filter({ hasText: 'kz33-answer-f4w8' })).toBeVisible({
    timeout: 30_000,
  });
  await followUp(page, AFTER_RESTORE);
  await expectAnswerInChat(page, 'Still here after the restore kz33-answer-r6t2.');
  for (const current of pages) await expectUnblocked(current);
  expect(await runnerAlerts(orchestrator, device, since)).toEqual([]);
  expect(await runnerStore(orchestrator, `SELECT key FROM runner.sync_state WHERE key = 'sync_fatal'`)).toEqual([]);

  // Runner store wipe (identity kept) and restart: the outbox continues behind the hub's position (read by the route
  // once the runner stopped), every new item is acknowledged, no delivered prompt comes again (the scenario's next
  // step would not match), no conflict stops the sync.
  await expect.poll(() => pending(orchestrator), { timeout: 60_000 }).toBe(0);
  const wiped = await orchestrator.call<Wiped>('POST', '/runners/runner-1/wipe-store', {});
  expect([wiped.state, wiped.device]).toEqual(['running', device]);
  await expect
    .poll(
      async () =>
        (
          await runnerStore<{ first: number | null }>(
            orchestrator,
            'SELECT min(origin_seq)::int AS first FROM runner.outbox',
          )
        )[0]!.first,
      { timeout: 120_000 },
    )
    .toBe(wiped.last_origin_seq + 1);
  await expect.poll(() => hubAcked(orchestrator, device), { timeout: 120_000 }).toBeGreaterThan(wiped.last_origin_seq);
  await expect
    .poll(
      async () =>
        (
          await runnerStore<{ n: number }>(
            orchestrator,
            `SELECT count(*)::int AS n FROM runner.prompt_delivery WHERE state = 'accepted'`,
          )
        )[0]!.n,
      { timeout: 60_000 },
    )
    .toBeGreaterThanOrEqual(3);
  // The wiped store no longer knows the first task's session (VM and transcript are gone): the runner reports it
  // failed, so the hub starts a new main session for the follow-up (03-api.md section 4.6.3).
  await expect
    .poll(
      async () =>
        (
          await orchestrator.hubQuery<{ state: string }>(
            `SELECT state FROM hub.session WHERE task_id = '${uuid(first)}' AND depth = 0 ORDER BY created_at`,
          )
        ).map((row) => row.state),
      { timeout: 60_000 },
    )
    .toEqual(['failed']);
  await followUp(page, AFTER_WIPE);
  await expectAnswerInChat(page, 'Still here after the store wipe kz33-answer-w9p3.');
  const restarted = await orchestrator.hubQuery<{ state: string; reason: string | null }>(
    `SELECT state, state_reason AS reason FROM hub.session WHERE task_id = '${uuid(first)}' AND depth = 0
      ORDER BY created_at`,
  );
  expect(restarted[0]).toEqual({ state: 'failed', reason: 'device_shutdown' });
  expect(restarted.length).toBe(2);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expect.poll(() => pending(orchestrator), { timeout: 60_000 }).toBe(0);
  expect(await runnerAlerts(orchestrator, device, since)).toEqual([]);
  expect(await runnerStore(orchestrator, `SELECT key FROM runner.sync_state WHERE key = 'sync_fatal'`)).toEqual([]);
  const origins = await orchestrator.hubQuery<{ n: number; distinct_n: number }>(
    `SELECT count(*)::int AS n, count(DISTINCT origin_seq)::int AS distinct_n FROM hub.event
      WHERE origin_device_id = '${uuid(device)}'`,
  );
  expect(origins[0]!.n).toBe(origins[0]!.distinct_n);
  for (const current of pages) await expectUnblocked(current);

  await bobContext.close();
  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});
