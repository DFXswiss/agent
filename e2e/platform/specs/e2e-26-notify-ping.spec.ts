// E2E-26 (07-testing.md section 2.1; 03-api.md sections 4.9, 6.2, 7.1, 8.1, 8.2; 05-runner.md section 9; ADR-020): a
// whiteboard entry with "Notify participants" reaches exactly the entitled users, and the active session in its scope
// gets only the content-free notice. Ada (admin) has a workgroup "Research"; Rita reads it, Wendy writes it, Carol is a
// member without access to it. Ada's quiet task in the default workgroup is idle; her working task in Research holds
// its turn on a pending model request while Ada writes a Research entry with notify. Asserted: Rita and Wendy get the
// notification live (badge, toast, row), Carol and Ada (the author) none; the hub keeps a content-free `session_ping`
// for the Research session only; the turn continues without a new prompt; the guest attaches the fixed notice to the
// next PreToolUse answer (journal: `ping.delivered` via hook right after that `session.hook`), never to Stop; the model
// receives the notice in the request after it and nothing of the entry before the session's own `get_whiteboard`; the
// session calls `get_notifications` and `get_whiteboard` and answers from the entry; the tmux pane receives no input
// bytes meanwhile (pane input log, with a positive control); no content in pings at the guest or the hub. Runs on T1
// and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  auditRows,
  expectNoPlaintext,
  runnerStore,
  sessionCast,
  sessionState,
  typeIntoPane,
} from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  openChat,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  transitions,
  uuid,
} from '../fixtures/platform.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { inviteMember, joinAsNewUser, writeEntry } from '../fixtures/team.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-26-notify-ping.json', import.meta.url), 'utf8'),
);
const NOTICE =
  'agent.dfx.swiss notice: 1 new item(s) available (whiteboard.entry). Use mcp__platform__get_notifications to see them.';
const WG_NAME = 'Research kz26-wg-g4j9n2e6';
const QUIET = 'E2E26-QUIET kz26-prompt-q-u8c3l5z1: a quick question';
const WORK = 'E2E26-WORK kz26-prompt-w-a2y7d4o9: write the plan and keep going';
const ENTRY = 'Release moved to Friday kz26-entry-b7r4t9x2';
const CANARIES = [
  'kz26-wg-g4j9n2e6',
  'kz26-prompt-q-u8c3l5z1',
  'kz26-prompt-w-a2y7d4o9',
  'kz26-entry-b7r4t9x2',
  'kz26-answer-q3n8v1d5',
  'kz26-answer-f6k1p3s8',
  'kz26-file-w5c2h8m4',
  'kz26-name-a1',
  'kz26-name-r2',
  'kz26-name-c3',
  'kz26-name-w4',
];
const NOTICE_EVENTS = ['PreToolUse', 'PostToolUse', 'UserPromptSubmit'];

interface PaneLog {
  readonly vm: string;
  readonly fd: number;
  readonly writes: number;
  readonly bytes: number;
  readonly data: string;
}

interface GuestRecord {
  readonly seq: number;
  readonly kind: string;
  readonly body: Record<string, unknown>;
}

interface GuestJournal {
  readonly vm: string;
  readonly records: readonly GuestRecord[];
  readonly pings: readonly Record<string, unknown>[];
}

interface ModelRequest {
  readonly path: string;
  readonly step?: string;
  readonly body: { readonly messages?: readonly { role: string; content: unknown }[] } | null;
}

const paneInput = (orchestrator: Orchestrator, session: string, action: 'start' | 'stop') =>
  orchestrator.call<PaneLog>('POST', `/sessions/${uuid(session)}/pane-input/${action}`, {});

const guestJournal = (orchestrator: Orchestrator, session: string) =>
  orchestrator.call<GuestJournal>('POST', `/sessions/${uuid(session)}/guest-journal`, {
    kinds: ['session.hook', 'ping.delivered'],
  });

/** The hub's whiteboard notifications of `org` (content-free rows: recipient, workgroup, entry ID). */
const notificationRows = (orchestrator: Orchestrator, org: string) =>
  orchestrator.hubQuery<{ recipient_id: string; wg_id: string; ref: string }>(
    `SELECT recipient_id::text, wg_id::text, ref FROM hub.notification
      WHERE org_id = '${uuid(org)}' AND kind = 'whiteboard.entry' ORDER BY created_at`,
  );

/**
 * The texts a model request adds to the conversation: every text of the messages after the last assistant message
 * (the tool result and what Claude Code attaches to it; a hook's `additionalContext` arrives as a `system` message).
 */
function newTexts(request: ModelRequest): string[] {
  const messages = request.body?.messages ?? [];
  const last = messages.map((message) => message.role).lastIndexOf('assistant');
  return messages
    .slice(last + 1)
    .flatMap((message) =>
      typeof message.content === 'string'
        ? [message.content]
        : (message.content as { type: string; text?: string }[])
            .filter((block) => block.type === 'text')
            .map((block) => block.text ?? ''),
    );
}

test.describe.configure({ mode: 'serial' });

test('@E2E-26 @R-12.8 @R-12.9 @R-12.10 @R-P.5 @needs-vm a notified entry reaches exactly the entitled users; the working session gets only the notice and fetches the entry itself', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator, 2);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz26-name-a1');
  const org = onboarded.org;
  const ada = await PageUser.open(page, onboarded.recoveryCode);
  const research = await ada.createWorkgroup(org, WG_NAME);

  // The quiet task in the default workgroup: answered, its session stays (out of the Research scope).
  const quiet = await sendPrompt(page, QUIET);
  await expectAnswerInChat(page, 'Quiet task done kz26-answer-q3n8v1d5.');
  const [quietSession] = await mainSessions(orchestrator, quiet);
  await expect.poll(() => sessionState(orchestrator, quietSession!.id), { timeout: 60_000 }).toBe('idle');

  // Rita (reads Research), Carol (no access to it) and Wendy (writes it) join through Ada's invitations.
  const rita = await joinAsNewUser(
    browser,
    e2e,
    org,
    await inviteMember(page, org, { [research]: 'read' }),
    'Rita kz26-name-r2',
  );
  const carol = await joinAsNewUser(browser, e2e, org, await inviteMember(page, org, {}), 'Carol kz26-name-c3');
  const wendy = await joinAsNewUser(
    browser,
    e2e,
    org,
    await inviteMember(page, org, { [research]: 'write' }),
    'Wendy kz26-name-w4',
  );
  for (const member of [rita, carol, wendy]) await expect(member.page.getByTestId('bell.link')).toBeVisible();

  // The working task in Research, started right after the joins: its first prompt follows the membership changes
  // closely (the device verifies them while it routes the prompt; decisions/SCN-06.md item 16, SPC-01). The turn
  // waits on the held model request.
  await home(page);
  await expect(page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(1, { timeout: 30_000 });
  await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
  const task = await sendPrompt(page, WORK);
  const [session] = await mainSessions(orchestrator, task);
  try {
    await expect
      .poll(async () => (await orchestrator.doubleState('model')).consumed, { timeout: 120_000 })
      .toEqual(['quiet', 'write', 'hold']);
  } catch (error) {
    // evidence for a report: the device's prompt deliveries and the session's terminal
    const rows = await runnerStore(orchestrator, 'SELECT to_jsonb(p) AS row FROM runner.prompt_delivery p').catch(
      (e: unknown) => String(e),
    );
    const cast = await sessionCast(orchestrator, session!.id).catch((e: unknown) => String(e));
    console.log(`E2E-26 deliveries ${JSON.stringify(rows)}\nE2E-26 cast ${cast.slice(-6000)}`);
    throw error;
  }
  await expect.poll(() => sessionState(orchestrator, session!.id), { timeout: 30_000 }).toBe('working');
  const pane = await paneInput(orchestrator, session!.id, 'start');

  // Ada writes the Research entry with "Notify participants".
  await writeEntry(page, `/o/${org}/w/${research}/whiteboard`, ENTRY, true);
  const [entryRow] = await orchestrator.hubQuery<{ entry_id: string }>(
    `SELECT id::text AS entry_id FROM hub.whiteboard_entry WHERE wg_id = '${uuid(research)}' AND level = 'workgroup'`,
  );
  const entry = entryRow!.entry_id;

  // The ping reaches the guest while the turn still waits for the model (nothing after `hold` consumed yet).
  await expect
    .poll(async () => (await guestJournal(orchestrator, session!.id)).pings.length, { timeout: 30_000 })
    .toBe(1);
  expect((await orchestrator.doubleState('model')).consumed).toEqual(['quiet', 'write', 'hold']);
  expect(await sessionState(orchestrator, session!.id)).toBe('working');

  // Users: Rita live (badge, toast, row); Carol nothing; Ada (the author) no whiteboard notification.
  await expect(rita.page.getByTestId('bell.badge')).toHaveText('1');
  // The workgroup's name is decrypted in Rita's browser from the profiles she read when she joined (06-web.md section
  // 6.2); toast and row name it (the neutral fallback "…" once stuck here: KOR-05, decisions/KOR-05.md).
  await expect(rita.page.getByTestId('bell.toast.open')).toHaveText(`New whiteboard entry in workgroup “${WG_NAME}”`);
  await rita.page.getByTestId('bell.link').click();
  const row = rita.page.getByTestId('notifications.item');
  await expect(row).toHaveCount(1);
  await expect(row).toHaveAttribute('data-kind', 'whiteboard.entry');
  await expect(row.getByTestId('notifications.text')).toHaveText(`New whiteboard entry in workgroup “${WG_NAME}”`);
  await row.click();
  await expect(rita.page).toHaveURL(new RegExp(`/o/${org}/w/${research}/whiteboard$`));
  await expect(rita.page.getByTestId('whiteboard.item').filter({ hasText: ENTRY })).toBeVisible();
  await expect(carol.page.getByTestId('bell.badge')).toHaveCount(0);
  let notified: { recipient_id: string; wg_id: string; ref: string }[] = [];
  await expect
    .poll(async () => (notified = await notificationRows(orchestrator, org)).length, { timeout: 30_000 })
    .toBe(2);
  await expect(wendy.page.getByTestId('bell.badge')).toHaveText('1');
  expect(notified.map((n) => n.recipient_id).sort()).toEqual([rita.user.userId, wendy.user.userId].sort());
  expect(notified.every((n) => n.wg_id === research && n.ref === entry)).toBe(true);
  expect(notified.map((n) => n.recipient_id)).not.toContain(carol.user.userId);
  expect(notified.map((n) => n.recipient_id)).not.toContain(ada.userId);
  const listed = (await carol.user.request('GET', '/v1/notifications')) as { notifications: { kind: string }[] };
  expect(listed.notifications.filter((n) => n.kind === 'whiteboard.entry')).toEqual([]);

  // The session's turn continues without a new prompt and answers from the entry it fetched itself.
  await navigateInApp(page, `/o/${org}/t/${task}`);
  await openChat(page); // the chat is the second tab (KONZEPT E-43)
  await expect
    .poll(async () => (await orchestrator.doubleState('model')).consumed, { timeout: 180_000, intervals: [1000] })
    .toEqual(['quiet', 'write', 'hold', 'fetch', 'board', 'answer']);
  await expectAnswerInChat(page, 'The workgroup board says kz26-answer-f6k1p3s8.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expect.poll(() => sessionState(orchestrator, session!.id), { timeout: 60_000 }).toBe('idle');
  const log = await paneInput(orchestrator, session!.id, 'stop');
  expect(log).toMatchObject({ vm: pane.vm, fd: pane.fd, writes: 0, bytes: 0, data: '' });
  expect((await taskBodies(page, orchestrator, onboarded, task, 'prompt')).map((body) => body.text)).toEqual([WORK]);
  const states = await transitions(orchestrator, session!.id);
  expect(states.slice(states.indexOf('working'))).toEqual(['working', 'idle']);

  // The notice rode on the PreToolUse answer of Read (never on Stop) and reached the model in the next request;
  // the entry's text reached the model only as the result of the session's own get_whiteboard.
  const journal = await guestJournal(orchestrator, session!.id);
  const delivered = journal.records.filter((record) => record.kind === 'ping.delivered');
  expect(delivered.map((record) => record.body.via)).toEqual(['hook', 'tool']);
  const pingId = journal.pings[0]!.id;
  expect(delivered.map((record) => record.body.ping_id)).toEqual([pingId, pingId]);
  const viaHook = delivered[0]!;
  const hookBefore = journal.records.filter((r) => r.kind === 'session.hook' && r.seq < viaHook.seq).at(-1)!;
  expect(hookBefore.body).toMatchObject({ event: 'PreToolUse', tool: 'Read' });
  expect(NOTICE_EVENTS).toContain(hookBefore.body.event);
  const requests = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as ModelRequest[];
  const turn = requests.filter(
    (request) => request.path.startsWith('/v1/messages') && request.step !== undefined && request.step !== 'title',
  );
  const withNotice = turn.filter((request) => newTexts(request).some((text) => text.includes(NOTICE)));
  expect(withNotice.map((request) => request.step)).toEqual(['fetch']);
  expect(newTexts(withNotice[0]!).filter((text) => text.includes(NOTICE))).toEqual([
    expect.stringMatching(new RegExp(`^PreToolUse:Read hook additional context: ${NOTICE.replace(/[.()]/g, '\\$&')}`)),
  ]);
  const first = turn.findIndex((request) => JSON.stringify(request.body).includes('kz26-entry-b7r4t9x2'));
  expect(turn[first]?.step).toBe('answer');

  // No content in any ping: guest rows and journal, the hub's session pings, notifications and audit rows.
  expect(journal.pings).toEqual([
    {
      id: pingId,
      kind: 'whiteboard.entry',
      level: 'workgroup',
      ref: entry,
      created_at: expect.any(String),
      noticed_at: expect.any(String),
      delivered_at: expect.any(String),
    },
  ]);
  const hubPings = await orchestrator.hubQuery<Record<string, unknown>>(
    `SELECT to_jsonb(p) AS row FROM hub.session_ping p JOIN hub.session s ON s.id = p.session_id
      WHERE s.task_id IN ('${uuid(quiet)}', '${uuid(task)}')`,
  );
  // the whole row (every column), so no column can carry more than kind, level and IDs
  expect(hubPings).toEqual([
    {
      row: {
        id: expect.any(String),
        session_id: session!.id,
        kind: 'whiteboard.entry',
        level: 'workgroup',
        ref: entry,
        created_at: expect.any(String),
        delivered_at: expect.any(String),
      },
    },
  ]);
  const quietJournal = await guestJournal(orchestrator, quietSession!.id);
  expect(quietJournal.pings).toEqual([]);
  expect(quietJournal.records.filter((record) => record.kind === 'ping.delivered')).toEqual([]);
  const audited = await auditRows(orchestrator, task, 'ping.delivered');
  expect(audited.length).toBeGreaterThanOrEqual(1);
  const stored = JSON.stringify([journal, hubPings, notified, audited]);
  for (const canary of CANARIES) expect(stored).not.toContain(canary);
  await expectNoPlaintext(orchestrator, CANARIES);

  // Positive control of the pane input log: text typed into the pane (without Enter) is seen byte for byte.
  await paneInput(orchestrator, session!.id, 'start');
  await typeIntoPane(orchestrator, session!.id, 'kz26 typed', false);
  expect(await paneInput(orchestrator, session!.id, 'stop')).toMatchObject({ bytes: 10, data: 'kz26 typed' });
  for (const member of [rita, carol, wendy]) await member.page.context().close();
});
