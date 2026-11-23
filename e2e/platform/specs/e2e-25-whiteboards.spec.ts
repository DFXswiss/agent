// E2E-25 (07-testing.md section 2.1; 03-api.md sections 4.4, 4.9, 5; 04-crypto.md section 4.3; 06-web.md section
// 6.1): whiteboards on the three levels with rights, history, live sync and ciphertext only at the hub. Ada (admin)
// has a workgroup "Research"; Wendy writes it, Rita only reads it; both joined through Ada's invitations, each in a
// browser profile of its own. Ada writes the org board; Wendy writes, edits and deletes on the Research board and
// writes on the board of Ada's Research task. Rita's browser shows every change live without any write control, and
// the hub refuses her direct writes on all three levels (`author_not_writer`, 03-api.md section 11) as well as
// Wendy's on the org board. The history lists every version (UI and API), there is no project level, and no
// entry text, name or prompt is stored or sent in clear by the hub (canary scan). Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  hubTablesContaining,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { inviteMember, joinAsNewUser, type Member, writeEntry } from '../fixtures/team.ts';
import { boardEvent, navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { uuid7 } from '../../../web/src/core/encoding/ids';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-25-whiteboards.json', import.meta.url), 'utf8'),
);
const WG_NAME = 'Research kz25-wg-h3k8p1x6';
const ORG_ENTRY = 'Org rules kz25-org-t5n2w8f3';
const WG_ENTRY = 'Research notes kz25-wg-entry-c4r9j2s7';
const WG_EDIT = 'Research notes kz25-wg-edit-y6b1m5q3';
const TASK_ENTRY = 'Task checklist kz25-task-entry-e8d3u7z2';
const PROMPT = 'E2E25-TASK kz25-prompt-g1l6a9o4: prepare the release checklist';
const CANARIES = [
  'kz25-wg-h3k8p1x6',
  'kz25-org-t5n2w8f3',
  'kz25-wg-entry-c4r9j2s7',
  'kz25-wg-edit-y6b1m5q3',
  'kz25-task-entry-e8d3u7z2',
  'kz25-prompt-g1l6a9o4',
  'kz25-answer-m2v7q4c9',
  'kz25-forged-k7f2n4b8',
  'kz25-name-a1',
  'kz25-name-w2',
  'kz25-name-r3',
];

const item = (page: Page, text: string) => page.getByTestId('whiteboard.item').filter({ hasText: text });

/** The board at `path` in `page` shows no write control at all (a reader's view). */
async function expectReadOnly(page: Page, path: string): Promise<void> {
  await navigateInApp(page, path);
  await expect(page.getByTestId('whiteboard.readonly')).toBeVisible();
  await expect(page.getByTestId('whiteboard.create')).toHaveCount(0);
  await expect(page.getByTestId('whiteboard.item.edit')).toHaveCount(0);
  await expect(page.getByTestId('whiteboard.item.delete')).toHaveCount(0);
}

interface Board {
  readonly org: string;
  readonly wg: string | null;
  readonly task: string | null;
  readonly level: 'org' | 'workgroup' | 'task';
}

/**
 * `author` writes on `board` directly through the API (an event sealed with its own keys, signed by its own page): a new
 * entry, or with `edit` an edit of an existing entry. The hub must refuse it.
 */
async function expectRefusedWrite(
  author: Member,
  ada: PageUser,
  board: Board,
  edit?: { readonly entry: string; readonly base: string },
): Promise<void> {
  const log = await ada.log(board.org);
  const text = 'forged kz25-forged-k7f2n4b8';
  const event =
    edit === undefined
      ? await boardEvent(author.user, log, board, 'whiteboard.entry', uuid7(), { level: board.level, text })
      : await boardEvent(author.user, log, board, 'whiteboard.edit', edit.entry, { text, base_event_id: edit.base });
  await expect(author.user.request('POST', '/v1/events', { events: [{ event }] })).rejects.toThrow(
    /: 400 .*author_not_writer/,
  );
}

test.describe.configure({ mode: 'serial' });

test('@E2E-25 @R-12.1 @R-12.2 @R-12.3 @R-12.4 @R-12.5 @R-12.6 @R-12.7 @R-P.5 @needs-vm whiteboards on three levels: rights, live sync, full history, ciphertext only', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz25-name-a1');
  const org = onboarded.org;
  const ada = await PageUser.open(page, onboarded.recoveryCode);
  const research = await ada.createWorkgroup(org, WG_NAME);
  const wendy = await joinAsNewUser(
    browser,
    e2e,
    org,
    await inviteMember(page, org, { [research]: 'write' }),
    'Wendy kz25-name-w2',
  );
  const rita = await joinAsNewUser(
    browser,
    e2e,
    org,
    await inviteMember(page, org, { [research]: 'read' }),
    'Rita kz25-name-r3',
  );
  const orgBoard = `/o/${org}/whiteboard`;
  const wgBoard = `/o/${org}/w/${research}/whiteboard`;

  // Org level: three levels only (no project level); Ada writes, Wendy and Rita read without controls, live.
  await navigateInApp(page, orgBoard);
  await expect(page.getByTestId('whiteboard.level')).toHaveText(['Organization', 'Default', WG_NAME]);
  await expectReadOnly(rita.page, orgBoard);
  await expectReadOnly(wendy.page, orgBoard);
  await writeEntry(page, orgBoard, ORG_ENTRY);
  await expect(item(rita.page, ORG_ENTRY).getByTestId('whiteboard.item.author')).toHaveText('Ada kz25-name-a1');
  await expect(item(wendy.page, ORG_ENTRY)).toBeVisible();
  await expect(rita.page.getByTestId('whiteboard.item.edit')).toHaveCount(0);

  // Workgroup level: Wendy writes, edits and deletes; Rita follows live and never gets a control.
  await expectReadOnly(rita.page, wgBoard);
  await writeEntry(wendy.page, wgBoard, WG_ENTRY);
  const ritaEntry = item(rita.page, 'Research notes');
  await expect(ritaEntry).toContainText(WG_ENTRY);
  await expect(ritaEntry.getByTestId('whiteboard.item.author')).toHaveText('Wendy kz25-name-w2');
  const wendyEntry = item(wendy.page, 'Research notes');
  await wendyEntry.getByTestId('whiteboard.item.edit').click();
  await wendy.page.getByTestId('whiteboard.editor.text').fill(WG_EDIT);
  await wendy.page.getByTestId('whiteboard.editor.save').click();
  await expect(ritaEntry).toContainText(WG_EDIT);
  await expect(ritaEntry.getByTestId('whiteboard.item.edited')).toHaveText('edited');
  await wendyEntry.getByTestId('whiteboard.item.delete').click();
  await wendyEntry.getByTestId('whiteboard.item.delete.confirm').click();
  await expect(rita.page.getByTestId('whiteboard.item.deleted')).toBeVisible();
  await expect(rita.page.getByTestId('whiteboard.item.edit')).toHaveCount(0);
  await expect(rita.page.getByTestId('whiteboard.item.delete')).toHaveCount(0);

  // History: every version, in the reader's dialog and from the hub.
  await rita.page.getByTestId('whiteboard.item.history').first().click();
  const versions = rita.page.getByTestId('whiteboard.history.version');
  await expect(versions).toHaveCount(3);
  await expect(rita.page.getByTestId('whiteboard.history.action')).toHaveText(['Created', 'Edited', 'Deleted']);
  await expect(rita.page.getByTestId('whiteboard.history.author')).toHaveText([
    'Wendy kz25-name-w2',
    'Wendy kz25-name-w2',
    'Wendy kz25-name-w2',
  ]);
  await expect(versions.nth(1).locator('[data-op="add"]')).toHaveText(`+ ${WG_EDIT}`);
  await rita.page.getByTestId('whiteboard.history.close').click();
  const listed = (await rita.user.request('GET', `/v1/whiteboards?org=${org}&wg=${research}&level=workgroup`)) as {
    entries: { entry_id: string; deleted: boolean; author_id: string }[];
  };
  expect(listed.entries).toHaveLength(1);
  expect(listed.entries[0]).toMatchObject({ deleted: true, author_id: wendy.user.userId });
  const wgEntry = listed.entries[0]!.entry_id;
  const history = (await rita.user.request('GET', `/v1/whiteboards/${wgEntry}/history`)) as {
    events: { event: { header: { kind: string; id: string; ref: string } } }[];
  };
  expect(history.events.map((row) => row.event.header.kind)).toEqual([
    'whiteboard.entry',
    'whiteboard.edit',
    'whiteboard.delete',
  ]);
  expect(history.events.every((row) => row.event.header.ref === wgEntry)).toBe(true);

  // Task level: Ada's task in Research (an admin's first task assigns the device to the workgroup) carries its own
  // board; Wendy writes on it, Rita reads it without controls.
  await home(page);
  await expect(page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(1, { timeout: 30_000 });
  await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'Release checklist ready kz25-answer-m2v7q4c9.');
  for (const member of [wendy, rita]) {
    await navigateInApp(member.page, `/o/${org}/t/${task}`);
    await member.page.getByTestId('task.tabs.whiteboard').click();
    await expect(member.page.getByTestId('whiteboard.board')).toHaveAttribute('data-level', 'task');
  }
  await expect(rita.page.getByTestId('whiteboard.readonly')).toBeVisible();
  await wendy.page.getByTestId('whiteboard.create').click();
  await wendy.page.getByTestId('whiteboard.editor.text').fill(TASK_ENTRY);
  await wendy.page.getByTestId('whiteboard.editor.save').click();
  await expect(item(rita.page, TASK_ENTRY)).toBeVisible();
  await expect(rita.page.getByTestId('whiteboard.create')).toHaveCount(0);
  await expect(rita.page.getByTestId('whiteboard.item.edit')).toHaveCount(0);
  // the task's entry stays off the workgroup board
  await navigateInApp(rita.page, wgBoard);
  await expect(rita.page.getByTestId('whiteboard.item')).toHaveCount(1);
  await expect(item(rita.page, TASK_ENTRY)).toHaveCount(0);

  // Rights in the backend: Rita's direct writes on all three levels and her edit of Wendy's entry are refused, as is
  // Wendy's write on the org board (org level: admins only).
  await expectRefusedWrite(rita, ada, { org, wg: null, task: null, level: 'org' });
  await expectRefusedWrite(rita, ada, { org, wg: research, task: null, level: 'workgroup' });
  await expectRefusedWrite(rita, ada, { org, wg: research, task, level: 'task' });
  const taskRows = (await rita.user.request('GET', `/v1/whiteboards?org=${org}&task=${task}&level=task`)) as {
    entries: { entry_id: string }[];
  };
  const taskHistory = (await rita.user.request('GET', `/v1/whiteboards/${taskRows.entries[0]!.entry_id}/history`)) as {
    events: { event: { header: { id: string } } }[];
  };
  const taskEntry = taskRows.entries[0]!.entry_id;
  await expectRefusedWrite(
    rita,
    ada,
    { org, wg: research, task, level: 'task' },
    {
      entry: taskEntry,
      base: taskHistory.events[0]!.event.header.id,
    },
  );
  // the refused edit left no event: the entry's history is still its creation only
  const after = (await rita.user.request('GET', `/v1/whiteboards/${taskEntry}/history`)) as { events: unknown[] };
  expect(after.events).toEqual(taskHistory.events);
  await expectRefusedWrite(wendy, ada, { org, wg: null, task: null, level: 'org' });

  // The hub's projection: the three levels, nothing else; the refused writes left no row.
  const rows = await orchestrator.hubQuery<{ level: string; author_id: string; deleted: boolean }>(
    `SELECT level, author_id::text, deleted FROM hub.whiteboard_entry WHERE org_id = '${uuid(org)}' ORDER BY created_at`,
  );
  expect(rows).toEqual([
    { level: 'org', author_id: ada.userId, deleted: false },
    { level: 'workgroup', author_id: wendy.user.userId, deleted: true },
    { level: 'task', author_id: wendy.user.userId, deleted: false },
  ]);
  await expect(rita.user.request('GET', `/v1/whiteboards?org=${org}&level=project`)).rejects.toThrow(
    /: 400 .*request_invalid/,
  );

  // Ciphertext only: no entry text, edit, name or prompt in the hub database, its logs or what it sent to browsers.
  expect(await hubTablesContaining(orchestrator, CANARIES)).toEqual([]);
  await expectNoPlaintext(orchestrator, CANARIES);
  for (const member of [wendy, rita]) await member.page.context().close();
});
