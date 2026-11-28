// Steps of the full-chain specs (SCN-01, reused by later scenario packages): the operator CLI through the
// orchestrator, the platform device (invite-device → `agent-runner enroll --platform` → certify-pending), the warm
// pool, the platform-invitation onboarding in the browser and the signed-in user's view of task events (decrypted
// in Node with the identity the recovery code opens, exactly what the browser can read).
import { expect, type Page } from '@playwright/test';
import { openEvent } from '../../../web/src/core/crypto/event';
import { epochKey, WG } from '../../../web/src/core/log/state';
import { createRecoveryCode } from '../../../web/e2e/helpers/onboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import type { Orchestrator } from './orchestrator.ts';
import { navTo } from './nav.ts';

export const ORG_PATH = /\/o\/([0-9a-f-]{36})$/;
export const TASK_PATH = /\/o\/[0-9a-f-]{36}\/t\/([0-9a-f-]{36})$/;
/** Start states of a session in their order (`02-data-model.md` section 2.4); `prompt_delivering` may be skipped. */
export const START_STATES = [
  'requested',
  'vm_assigning',
  'vm_ready',
  'sandbox_ready',
  'agent_starting',
  'prompt_delivering',
  'working',
] as const;

/** `agent-hub platform <args>` must succeed; returns its stdout. */
export async function operator(orchestrator: Orchestrator, args: string[]): Promise<string> {
  const done = await orchestrator.operator(args);
  if (done.rc !== 0) throw new Error(`agent-hub platform ${args[0]} exited with ${done.rc}: ${done.stderr.trim()}`);
  return done.stdout;
}

export interface Invitation {
  readonly code: string;
  readonly inviteId: string;
}

export function parseInvitation(stdout: string): Invitation {
  const code = /^invitation code (\S+)$/m.exec(stdout)?.[1];
  const inviteId = /^invite_id (\S+) /m.exec(stdout)?.[1];
  if (code === undefined || inviteId === undefined) throw new Error(`no invitation in: ${stdout}`);
  return { code, inviteId };
}

/** A platform invitation (`user_invite`) or device invitation (`device_invite`) by the operator CLI. */
export async function invite(orchestrator: Orchestrator, kind: 'user' | 'device'): Promise<Invitation> {
  return parseInvitation(await operator(orchestrator, [`invite-${kind}`]));
}

interface DeviceRow {
  readonly id: string;
  readonly state: string;
  readonly online: boolean;
}

async function platformDevices(orchestrator: Orchestrator): Promise<DeviceRow[]> {
  return orchestrator.hubQuery<DeviceRow>(
    `SELECT d.id, d.state, p.device_id IS NOT NULL AS online FROM hub.device d
       LEFT JOIN hub.device_presence p ON p.device_id = d.id WHERE d.kind = 'platform' ORDER BY d.enrolled_at`,
  );
}

/**
 * The run's platform device: `runner` is enrolled with an operator device invitation and certified, unless an
 * earlier spec of the iteration did that already. Waits until the hub sees it active and connected.
 */
export async function platformDevice(orchestrator: Orchestrator, runner = 'runner-1'): Promise<string> {
  let device = (await platformDevices(orchestrator)).find((row) => row.state === 'active')?.id;
  if (device === undefined) {
    const invitation = await invite(orchestrator, 'device');
    const enrolled = await orchestrator.enroll(runner, invitation.code, true);
    expect(enrolled.rc, enrolled.stderr).toBe(0);
    const id = /device_id=([0-9a-f-]{36})/.exec(enrolled.stdout)?.[1];
    if (id === undefined) throw new Error(`no device ID in: ${enrolled.stdout}`);
    expect(await operator(orchestrator, ['certify-pending', '--device', id])).toContain(`certified ${id}`);
    device = id;
  }
  await expect
    .poll(async () => (await platformDevices(orchestrator)).find((row) => row.id === device), { timeout: 60_000 })
    .toMatchObject({ state: 'active', online: true });
  return device;
}

/** The warm pool size the runners are configured with (`E2E_POOL_SIZE`, else the orchestrator's default). */
export async function poolSize(orchestrator: Orchestrator): Promise<number> {
  return Math.min(...Object.values(await orchestrator.pool()).map((pool) => pool.size));
}

/**
 * Waits until every runner's warm pool holds at least `minimum` VMs (07-testing.md section 5.2) and the hub has the
 * capacity report saying so (the scheduler's estimate uses `capacity.pool_ready`, 03-api.md section 4.6.2).
 */
export async function poolReady(orchestrator: Orchestrator, minimum = 1, timeout = 240_000): Promise<void> {
  await expect
    .poll(async () => Math.min(...Object.values(await orchestrator.pool()).map((pool) => pool.ready)), {
      timeout,
      intervals: [1000],
    })
    .toBeGreaterThanOrEqual(minimum);
  await expect
    .poll(
      async () => {
        const rows = await orchestrator.hubQuery<{ ready: number | null }>(
          `SELECT min((capacity->>'pool_ready')::int) AS ready FROM hub.device WHERE kind = 'platform' AND state = 'active'`,
        );
        return rows[0]?.ready ?? 0;
      },
      { timeout: 120_000, intervals: [500] },
    )
    .toBeGreaterThanOrEqual(minimum);
}

export interface Onboarded extends Invitation {
  readonly org: string;
  readonly recoveryCode: string;
}

/**
 * The platform-invitation onboarding in the page (06-web.md sections 3.1, 3.1.1, 3.2) with a code from the operator
 * CLI: link → display name → passkey (PRF) → unlock → private org (no code screen, KOR-22), then a recovery code from
 * /settings (one passkey gesture), with which the Node side opens the identity. The page needs a virtual authenticator.
 */
export async function onboard(page: Page, orchestrator: Orchestrator, name = 'Ada Lovelace'): Promise<Onboarded> {
  const invitation = await invite(orchestrator, 'user');
  return { ...invitation, ...(await onboardWithCode(page, invitation.code, name)) };
}

/**
 * Spacing of onboardings: one onboarding makes about eight anonymous requests (config, invitation lookup, registration
 * and sign-in ceremonies); the hub allows 30 per minute and source address (03-api.md section 1), and every browser of
 * a run reaches the hub from the same address. Specs that onboard back to back (E2E-01 with its negative cases, then
 * E2E-06 and E2E-08 in T2) exceeded it. 15 s between onboardings refills 7.5 requests, so each onboarding costs the
 * bucket (capacity 30) about half a request: some 60 back-to-back onboardings fit, more than any spec or PERF series
 * makes (PERF: 30); a restarted Playwright worker starts with a full spacing again.
 */
export const ONBOARD_SPACING_MS = 15_000;
let lastOnboarding = 0;

async function spaceOnboarding(): Promise<void> {
  const wait = lastOnboarding + ONBOARD_SPACING_MS - Date.now();
  if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
  lastOnboarding = Date.now();
}

/** The onboarding UI with a given platform invitation code; returns the private org and the recovery code created in
 *  the settings. */
export async function onboardWithCode(
  page: Page,
  code: string,
  name: string,
): Promise<{ org: string; recoveryCode: string }> {
  await spaceOnboarding();
  await page.goto(`/invite#${code}`);
  await page.getByTestId('invite.display-name').fill(name);
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  await expect(page).toHaveURL(ORG_PATH, { timeout: 30_000 });
  const org = ORG_PATH.exec(page.url())![1]!;
  return { recoveryCode: await createRecoveryCode(page), org };
}

/** One unsigned request from inside the page (through the browser's link to the hub): status and JSON answer. */
export async function anonymous(
  page: Page,
  method: string,
  target: string,
  body?: unknown,
): Promise<{ status: number; body: Record<string, unknown> }> {
  return page.evaluate(
    async ({ method, target, text }) => {
      const response = await fetch(target, {
        method,
        headers: text === null ? {} : { 'content-type': 'application/json' },
        ...(text !== null && { body: text }),
      });
      return { status: response.status, body: (await response.json()) as Record<string, unknown> };
    },
    { method, target, text: body === undefined ? null : JSON.stringify(body) },
  );
}

/** Back to the home screen through the app's navigation (a reload would ask for the passkey again). */
export async function home(page: Page): Promise<void> {
  await navTo(page, 'home');
  await expect(page).toHaveURL(ORG_PATH);
}

/**
 * Sends `prompt` from the composer of the current page; returns the task ID of the task page it lands on. The page
 * opens on the tab "Claude Code" (KONZEPT E-43); with `tab` `chat` (the default) the step then opens the Chat tab, where
 * the specs read prompts, outputs and cards.
 */
export async function sendPrompt(page: Page, prompt: string, tab: 'chat' | 'terminal' = 'chat'): Promise<string> {
  await page.getByTestId('composer.prompt').fill(prompt);
  await page.getByTestId('composer.send').click();
  await expect(page).toHaveURL(TASK_PATH, { timeout: 30_000 });
  const task = TASK_PATH.exec(page.url())![1]!;
  await expect(page.getByTestId('task.tabs.terminal')).toHaveAttribute('aria-current', 'page');
  if (tab === 'chat') await openChat(page);
  return task;
}

/** Opens the Chat tab of the task page (the second tab since KONZEPT E-43). */
export async function openChat(page: Page): Promise<void> {
  await page.getByTestId('task.tabs.chat').click();
  await expect(page.getByTestId('chat')).toBeVisible();
}

export interface SessionRow {
  readonly id: string;
  readonly task_id: string;
  readonly device_id: string | null;
  readonly state: string;
  readonly agent_kind: string;
  readonly seconds: number | null;
}

/** The main session(s) of a task with `first_working_at − task.created_at` in seconds (hub clock, 07 section 5.3). */
export async function mainSessions(orchestrator: Orchestrator, task: string): Promise<SessionRow[]> {
  return orchestrator.hubQuery<SessionRow>(
    `SELECT s.id, s.task_id, s.device_id, s.state, s.agent_kind,
            extract(epoch FROM s.first_working_at - t.created_at)::float8 AS seconds
       FROM hub.session s JOIN hub.task t ON t.id = s.task_id
      WHERE s.task_id = '${uuid(task)}' AND s.depth = 0 ORDER BY s.created_at`,
  );
}

/** The session's transitions in order. */
export async function transitions(orchestrator: Orchestrator, session: string): Promise<string[]> {
  const rows = await orchestrator.hubQuery<{ state: string }>(
    `SELECT state FROM hub.session_transition WHERE session_id = '${uuid(session)}' ORDER BY seq`,
  );
  return rows.map((row) => row.state);
}

/** The start states of `states` (until `working`) are in their order and none of the required ones is missing. */
export function expectStartOrder(states: readonly string[]): void {
  const until = states.indexOf('working');
  expect(until, `no working transition in ${states.join(',')}`).toBeGreaterThan(0);
  const start = states.slice(0, until + 1);
  const indexes = start.map((state) => START_STATES.indexOf(state as (typeof START_STATES)[number]));
  expect(
    indexes.every((index) => index >= 0),
    start.join(','),
  ).toBe(true);
  expect(
    [...indexes].sort((a, b) => a - b),
    start.join(','),
  ).toEqual(indexes);
  for (const required of START_STATES.filter((state) => state !== 'prompt_delivering'))
    expect(start).toContain(required);
}

export function uuid(text: string): string {
  if (!/^[0-9a-f-]{36}$/.test(text)) throw new Error(`not a UUID: ${text}`);
  return text;
}

interface ScopeEvent {
  readonly scope_seq: number;
  readonly event: { readonly header: { readonly id: string; readonly epoch: number } } & Record<string, unknown>;
}

/**
 * The decrypted bodies of the task's events of `kind` (in scope order) as the signed-in user of `page` reads them:
 * identity from the recovery slot, wg key from the user's wraps, the author's signing key (device or user) from the
 * hub.
 */
export async function taskBodies(
  page: Page,
  orchestrator: Orchestrator,
  onboarded: Onboarded,
  task: string,
  kind: string,
): Promise<Record<string, unknown>[]> {
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const log = await user.log(onboarded.org);
  const keys = await user.keys(onboarded.org, log);
  const [row] = await orchestrator.hubQuery<{ wg_id: string }>(`SELECT wg_id FROM hub.task WHERE id = '${uuid(task)}'`);
  const listing = (await user.request(
    'GET',
    `/v1/scopes/${row!.wg_id}/events?task=${task}&kinds=${encodeURIComponent(kind)}`,
  )) as { events: ScopeEvent[] };
  const authors = await orchestrator.hubQuery<{ id: string; sig_pub: string }>(
    `SELECT e.id, encode(coalesce(d.sig_pub, u.sig_pub), 'hex') AS sig_pub FROM hub.event e
       LEFT JOIN hub.device d ON d.id = e.author_id LEFT JOIN hub.user_identity u ON u.user_id = e.author_id
      WHERE e.task_id = '${uuid(task)}' AND e.kind = '${kind.replace(/[^a-z.]/g, '')}'`,
  );
  const authorOf = new Map(authors.map((author) => [author.id, hex(author.sig_pub)]));
  const bodies: Record<string, unknown>[] = [];
  for (const item of listing.events) {
    const header = item.event.header;
    const key = keys.get(epochKey(WG, row!.wg_id, header.epoch));
    const author = authorOf.get(header.id);
    if (key === undefined || author === undefined) throw new Error(`no key or author for event ${header.id}`);
    bodies.push(
      JSON.parse(new TextDecoder().decode(await openEvent(key, item.event, author))) as Record<string, unknown>,
    );
  }
  return bodies;
}

function hex(text: string): Uint8Array {
  return Uint8Array.from(text.match(/../g)!.map((pair) => parseInt(pair, 16)));
}

/**
 * Tables of the hub schema in which any row (as JSON text, byte columns as hex) contains one of `needles`: the
 * zero-knowledge check of codes, recovery codes, names and prompts (07-testing.md sections 2.1, E2E-09).
 */
export async function hubTablesContaining(orchestrator: Orchestrator, needles: readonly string[]): Promise<string[]> {
  const found = new Set<string>();
  for (const needle of needles) {
    const literal = `'%${needle.replace(/'/g, "''").replace(/[%_\\]/g, (c) => `\\${c}`)}%'`;
    const rows = await orchestrator.hubQuery<{ table_name: string }>(
      `SELECT table_name FROM information_schema.tables WHERE table_schema = 'hub' AND table_type = 'BASE TABLE'
         AND query_to_xml(format('SELECT 1 FROM hub.%I x WHERE to_jsonb(x)::text LIKE %s LIMIT 1', table_name,
                                 quote_literal(${literal})), false, true, '')::text <> ''`,
    );
    for (const row of rows) found.add(`${row.table_name}: ${needle}`);
  }
  return [...found].sort();
}

/** The answer `text` shows in the task's chat (06-web.md section 5.2: a `session.output` item). */
export async function expectAnswerInChat(page: Page, text: string): Promise<void> {
  // on the task page the chat is the second tab (KONZEPT E-43): opened when another tab shows
  const chatTab = page.getByTestId('task.tabs.chat');
  if ((await chatTab.count()) > 0 && (await chatTab.getAttribute('aria-current')) !== 'page') await openChat(page);
  await expect(page.getByTestId('chat.output').filter({ hasText: text })).toBeVisible({ timeout: 60_000 });
}
