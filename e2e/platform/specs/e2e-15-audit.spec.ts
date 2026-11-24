// E2E-15 (07-testing.md section 2.1; 02-data-model.md section 2.9; 03-api.md sections 1, 4.11, 8.3; 06-web.md
// section 9.2): audit and read log. The admin writes an organization whiteboard entry; one session turn renames its
// task (operation task.set_title), reads the organization whiteboard (platform read), is denied a file outside its
// workspace (PreToolUse hook) and requests an unknown operation type (rejected). The admin opens the audit screen:
// rows of the task carry actor, time, task, session, workgroup, device, target and result; `read.*` rows exist for API
// reads (the browser's) and for the session's platform read; "Verify chain" is ok and so is `agent-hub audit verify`;
// a row changed in a copy of the hub database breaks the chain there and only there. Every change row's `event_id`
// references a stored encrypted event that the admin decrypts. Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { openEvent } from '../../../web/src/core/crypto/event';
import { epochKey, ORG, WG } from '../../../web/src/core/log/state';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  type Onboarded,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { navTo } from '../fixtures/nav.ts';

const scenario: unknown = JSON.parse(readFileSync(new URL('../scenarios/e2e-15-audit.json', import.meta.url), 'utf8'));
const PROMPT = 'E2E15-AUDIT kz15-prompt-b6t3v9: rename the task, read the board, then finish';
const NAME = 'Ada kz15-name-j5w8e1';
const BOARD = 'Release checklist kz15-board-p3x9q6';
const TITLE = 'Audit trail kz15-title-r4m8w2';
const ANSWER = 'The audit trail is complete kz15-answer-h7c2n5.';
const CANARIES = [
  'kz15-prompt-b6t3v9',
  'kz15-name-j5w8e1',
  'kz15-board-p3x9q6',
  'kz15-title-r4m8w2',
  'kz15-answer-h7c2n5',
];

interface ApiRow {
  readonly seq: number;
  readonly at: string;
  readonly actor_id: string | null;
  readonly actor_kind: string;
  readonly action: string;
  readonly result: string;
  readonly org_id: string | null;
  readonly wg_id: string | null;
  readonly task_id: string | null;
  readonly session_id: string | null;
  readonly device_id: string | null;
  readonly target_kind: string | null;
  readonly target_id: string | null;
  readonly detail: Record<string, unknown>;
  readonly event_id: string | null;
}

interface ScopeEvent {
  readonly event: { readonly header: { readonly id: string; readonly epoch: number } } & Record<string, unknown>;
}

/** Every audit row of the org the admin's API returns for `query` (all pages, newest first). */
async function auditApi(user: PageUser, org: string, query: Record<string, string>): Promise<ApiRow[]> {
  const rows: ApiRow[] = [];
  let before: string | null = null;
  for (;;) {
    const params = new URLSearchParams({ ...query, ...(before !== null && { before }) });
    const search = params.toString();
    const page = (await user.request('GET', `/v1/orgs/${org}/audit${search ? `?${search}` : ''}`)) as {
      rows: ApiRow[];
      next_before?: string | null;
    };
    rows.push(...page.rows);
    if (!page.next_before) return rows;
    before = page.next_before;
  }
}

/**
 * The events of a scope (org or workgroup) the admin decrypts, by event ID: the epoch key from the admin's wraps, the
 * author's signing key (device or user) from the hub (public keys only).
 */
async function decryptScope(
  page: Page,
  orchestrator: Orchestrator,
  onboarded: Onboarded,
  kind: typeof ORG | typeof WG,
  scope: string,
): Promise<Map<string, Record<string, unknown>>> {
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const log = await user.log(onboarded.org);
  const keys = await user.keys(onboarded.org, log);
  // Every kind and page; `session.terminal` is listed only when asked for (03-api.md section 4.4).
  const events: ScopeEvent[] = [];
  for (const kinds of ['', '&kinds=session.terminal']) {
    for (let after = 0; ;) {
      const target = `/v1/scopes/${uuid(scope)}/events?after=${after}&limit=1000${kinds}`;
      const page = (await user.request('GET', target)) as { events: ScopeEvent[]; next_after: number | null };
      events.push(...page.events);
      if (page.next_after === null) break;
      after = page.next_after;
    }
  }
  const authors = await orchestrator.hubQuery<{ id: string; sig_pub: string }>(
    `SELECT e.id, encode(coalesce(d.sig_pub, u.sig_pub), 'hex') AS sig_pub FROM hub.event e
       LEFT JOIN hub.device d ON d.id = e.author_id LEFT JOIN hub.user_identity u ON u.user_id = e.author_id
      WHERE e.org_id = '${uuid(onboarded.org)}'`,
  );
  const authorOf = new Map(authors.map((author) => [author.id, hex(author.sig_pub)]));
  const bodies = new Map<string, Record<string, unknown>>();
  for (const item of events) {
    const header = item.event.header;
    const key = keys.get(epochKey(kind, scope, header.epoch));
    const author = authorOf.get(header.id);
    if (key === undefined || author === undefined) throw new Error(`no key or author for event ${header.id}`);
    const plain = await openEvent(key, item.event, author);
    bodies.set(header.id, JSON.parse(new TextDecoder().decode(plain)) as Record<string, unknown>);
  }
  return bodies;
}

function hex(text: string): Uint8Array {
  return Uint8Array.from(text.match(/../g)!.map((pair) => parseInt(pair, 16)));
}

/** The audit screen with the given filters applied through the filter form. */
async function filterAudit(page: Page, filters: Record<string, string>): Promise<void> {
  await page.getByTestId('audit.filter.reset').click();
  for (const [name, value] of Object.entries(filters)) await page.getByTestId(`audit.filter.${name}`).fill(value);
  await page.getByTestId('audit.filter.apply').click();
  await expect(page.getByTestId('audit.loading')).toHaveCount(0, { timeout: 30_000 });
}

/** The row `seq` of the audit screen, loading further pages ("Load more", 100 rows each) until it is shown. */
async function showRow(page: Page, seq: number) {
  const item = page.locator(`[data-testid="audit.row"][data-seq="${seq}"]`);
  const more = page.getByTestId('audit.more');
  for (let pages = 0; pages < 20; pages += 1) {
    // the row on the loaded pages, else the next page (a complete list without the row fails at the click)
    const shown = await expect(item)
      .toBeVisible({ timeout: 10_000 })
      .then(
        () => true,
        () => false,
      );
    if (shown) break;
    await more.click({ timeout: 10_000 });
  }
  await expect(item).toBeVisible();
  return item;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-15 @R-6.7 @R-8.9 @R-9.5 @R-10.12 @R-11.7 @R-P.5 @needs-vm audit rows carry every field, reads are logged, the chain verifies and a tampered copy fails', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  await orchestrator.resetDouble('model');
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, NAME);
  const org = onboarded.org;

  // The admin's organization whiteboard entry: a change (event.append with the encrypted entry) the session reads.
  await navTo(page, 'whiteboard');
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(BOARD);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: BOARD })).toBeVisible({ timeout: 30_000 });
  await home(page);

  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, ANSWER);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);
  const sessionId = session!.id;
  const device = session!.device_id!;
  const [taskRow] = await orchestrator.hubQuery<{ wg_id: string }>(`SELECT wg_id FROM hub.task WHERE id = '${task}'`);
  const wg = taskRow!.wg_id;

  // The runner's rows arrive through its outbox: wait for the last of them.
  const admin = await PageUser.open(page, onboarded.recoveryCode);
  const expected = [
    'operation.accepted',
    'operation.succeeded',
    'operation.rejected',
    'hook.denied',
    'read.session.get_whiteboard',
    'task.create',
  ];
  await expect
    .poll(
      async () => {
        const actions = new Set((await auditApi(admin, org, { task })).map((row) => row.action));
        return expected.filter((action) => !actions.has(action));
      },
      { timeout: 60_000 },
    )
    .toEqual([]);
  const rows = await auditApi(admin, org, { task });

  // Every field of the session's rows (R-6.7): actor, time, task, session, workgroup, device, target, result.
  const pick = (action: string) => rows.filter((row) => row.action === action);
  const [succeeded] = pick('operation.succeeded');
  const [rejected] = pick('operation.rejected');
  const [denied] = pick('hook.denied');
  const [sessionRead] = pick('read.session.get_whiteboard');
  for (const row of [succeeded, rejected, denied, sessionRead]) {
    expect(row, JSON.stringify(rows.map((item) => item.action))).toBeDefined();
    expect(row).toMatchObject({ org_id: org, wg_id: wg, task_id: task, session_id: sessionId, device_id: device });
    expect(Number.isNaN(Date.parse(row!.at))).toBe(false);
    expect(row!.actor_id).not.toBeNull();
  }
  expect(succeeded).toMatchObject({ result: 'ok', target_kind: 'operation', detail: { type: 'task.set_title' } });
  expect(rejected).toMatchObject({
    result: 'rejected',
    target_kind: 'operation',
    detail: { type: 'shell.run', error_code: 'unknown_type' },
  });
  expect(denied).toMatchObject({ result: 'denied', actor_kind: 'device', detail: { tool: 'Read' } });
  expect(JSON.stringify(denied!.detail)).not.toContain('/usr/bin');
  expect(sessionRead).toMatchObject({ result: 'ok', actor_kind: 'device' });

  // Change rows reference the encrypted change event (a summary of the session's own events its first one, ADR-063);
  // the admin decrypts every one of them.
  // The session's output is summarised by the job `audit.event_batch` (every 60 s): wait for its row.
  let orgRows: ApiRow[] = [];
  await expect
    .poll(
      async () => {
        orgRows = await auditApi(admin, org, {});
        return orgRows.some((row) => row.session_id === sessionId && row.action === 'event.append_batch');
      },
      { timeout: 150_000, intervals: [5_000] },
    )
    .toBe(true);
  const changes = orgRows.filter((row) => row.event_id !== null);
  expect(changes.length).toBeGreaterThan(5);
  expect(
    changes.every(
      (row) => ['event.append', 'event.append_batch'].includes(row.action) && row.target_id === row.event_id,
    ),
  ).toBe(true);
  const scopes = new Map<string, Map<string, Record<string, unknown>>>([
    [org, await decryptScope(page, orchestrator, onboarded, ORG, org)],
    [wg, await decryptScope(page, orchestrator, onboarded, WG, wg)],
  ]);
  for (const row of changes) {
    // the event's own scope (`wg.profile` lives in the org scope although its row names the workgroup)
    const scope = row.detail.scope === 'org' ? row.org_id! : row.wg_id!;
    const body = scopes.get(scope)?.get(row.event_id!);
    expect(body, `${row.action} ${String(row.detail.kind)} ${row.event_id}`).toBeDefined();
  }
  const bodies = [...scopes.values()].flatMap((events) => [...events.values()]);
  expect(bodies.map((body) => JSON.stringify(body)).join('\n')).toContain(BOARD);
  expect(bodies.map((body) => JSON.stringify(body)).join('\n')).toContain(TITLE);
  const titleRow = changes.find(
    (row) => row.task_id === task && row.actor_kind === 'device' && row.detail.kind === 'task.meta',
  );
  expect(titleRow, 'the task.meta event of task.set_title').toBeDefined();
  expect(JSON.stringify(scopes.get(wg)!.get(titleRow!.event_id!))).toContain(TITLE);

  // The audit screen: the task's rows with actor, time, result, target, workgroup, task and device; reads included.
  await navTo(page, 'audit');
  await expect(page).toHaveURL(new RegExp(`/o/${org}/audit`));
  await filterAudit(page, { task });
  const screenRows = page.getByTestId('audit.row');
  for (const row of [succeeded!, rejected!, denied!, sessionRead!]) {
    const item = await showRow(page, row.seq);
    await expect(item).toHaveAttribute('data-action', row.action);
    await expect(item.getByTestId('audit.row.time')).toHaveText(/\d{2}:\d{2}:\d{2}\.\d{3}/);
    await expect(item.getByTestId('audit.row.actor')).not.toBeEmpty();
    await expect(item.getByTestId('audit.row.result')).toHaveAttribute('data-result', row.result);
    await expect(item.getByTestId('audit.row.wg')).toBeVisible();
    await expect(item.getByTestId('audit.row.task')).toHaveAttribute('href', `/o/${org}/t/${task}`);
    await expect(item.getByTestId('audit.row.device')).toBeVisible();
    if (row.target_kind !== null) await expect(item.getByTestId('audit.row.target')).toBeVisible();
  }
  // The referenced event: the task page for the renamed title, the session page for the session's answer.
  const eventLink = (seq: number) =>
    page.locator(`[data-testid="audit.row"][data-seq="${seq}"]`).getByTestId('audit.row.event');
  await showRow(page, titleRow!.seq);
  await expect(eventLink(titleRow!.seq)).toHaveAttribute('href', `/o/${org}/t/${task}`);
  const outputRow = changes.find((row) => row.session_id === sessionId && row.action === 'event.append_batch');
  expect(outputRow, 'the summary row of the session output').toBeDefined();
  expect(Number(outputRow!.detail.count)).toBeGreaterThan(0);
  await showRow(page, outputRow!.seq);
  await expect(eventLink(outputRow!.seq)).toHaveAttribute('href', `/o/${org}/s/${sessionId}`);
  // The session filter shows exactly the session's rows: the same rows as the API's answer for that filter (the
  // screen's own `read.org.audit` rows, written while it loads, aside).
  await filterAudit(page, { session: sessionId });
  const others = (pairs: readonly (readonly [number, string])[]) =>
    pairs.filter(([, action]) => action !== 'read.org.audit').map(([seq]) => seq);
  const shownPairs = async () =>
    (await screenRows.evaluateAll((items) =>
      items.map((item) => [Number(item.getAttribute('data-seq')), item.getAttribute('data-action') ?? ''] as const),
    )) as (readonly [number, string])[];
  await expect.poll(async () => others(await shownPairs()).length, { timeout: 30_000 }).toBeGreaterThan(0);
  const shown = others(await shownPairs());
  const bySession = await auditApi(admin, org, { session: sessionId });
  expect(bySession.every((row) => row.session_id === sessionId)).toBe(true);
  const [low, high] = [Math.min(...shown), Math.max(...shown)];
  expect(shown).toEqual(
    others(bySession.map((row) => [row.seq, row.action] as const)).filter((seq) => seq >= low && seq <= high),
  );
  expect(shown).toEqual(
    expect.arrayContaining([succeeded!.seq, rejected!.seq, denied!.seq, sessionRead!.seq].filter((seq) => seq >= low)),
  );

  // Read log (R-8.9): the browser's API reads (written within a second) and the session's platform read are rows of
  // the org - also reads that name only a task or an event scope, placed by the hub (`read.task`, `read.scope.events`).
  await expect
    .poll(
      async () => {
        const actions = new Set((await auditApi(admin, org, { action: 'read' })).map((row) => row.action));
        return ['read.org.audit', 'read.session.get_whiteboard', 'read.task', 'read.scope.events'].filter(
          (action) => !actions.has(action),
        );
      },
      { timeout: 30_000 },
    )
    .toEqual([]);
  const reads = await auditApi(admin, org, { action: 'read' });
  const browserRead = (action: string) => reads.filter((row) => row.action === action && row.actor_kind === 'user');
  expect(browserRead('read.task').some((row) => row.task_id === task && row.wg_id === wg)).toBe(true);
  expect(browserRead('read.scope.events').some((row) => row.target_id === wg && row.wg_id === wg)).toBe(true);
  expect(sessionRead).toMatchObject({ actor_kind: 'device', session_id: sessionId, task_id: task, wg_id: wg });
  await filterAudit(page, { action: 'read' });
  await expect(screenRows.first()).toHaveAttribute('data-action', /^read\./);

  // Verify: the admin's "Verify chain", the operator CLI on the live database, and a tampered copy.
  await filterAudit(page, {});
  await page.getByTestId('audit.verify.run').click();
  await expect(page.getByTestId('audit.verify.result')).toHaveAttribute('data-ok', 'true', { timeout: 60_000 });
  const tamper = await orchestrator.call<{
    before: string;
    after: string;
    copy: { rc: number; ok: boolean | null };
    original: { rc: number; ok: boolean | null };
  }>('POST', '/hub-db/audit-tamper', { seq: denied!.seq });
  expect(tamper.before).toBe('denied');
  expect(tamper.after).toBe('ok');
  expect(tamper.copy).toEqual({ rc: 1, ok: false });
  expect(tamper.original).toEqual({ rc: 0, ok: true });

  await expectNoPlaintext(orchestrator, CANARIES);
});
