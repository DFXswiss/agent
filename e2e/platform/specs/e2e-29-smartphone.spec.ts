// E2E-29 (07-testing.md sections 2.1 and 5.4; 06-web.md section 12; R-6.6): the smartphone. In the mobile project
// (Pixel 7 emulation on Chromium, touch) a new user registers from a platform invitation, signs in again after a
// reload, sends the first task, follows the countdown and the chat, sends a follow-up, opens the read-only terminal of
// the session (live and history), writes a whiteboard entry and opens the notification the finished turn left
// (`task.idle`: the session worked longer than 10 s) - every step by tapping and through the phone's own navigation
// (bottom bar and "More" sheet). Every screen fits the phone: no horizontal scrolling of the document at the
// emulated width, at 360 px and at the iPhone 15 width (393 px), touch targets of at least 44 × 44 px and no serious
// accessibility violation (`web/e2e/helpers/layout-checks.ts`). Event times (chat items, terminal position, whiteboard
// entry, notification) are absolute `HH:MM:SS.mmm`. Runs on T1 and T2; the desktop project leaves it out.
import { readFileSync } from 'node:fs';
import type { Locator, Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  mainSessions,
  ORG_PATH,
  onboard,
  platformDevice,
  poolReady,
  TASK_PATH,
  uuid,
} from '../fixtures/platform.ts';
import { horizontalOverflow, layoutProblems, smallTargets } from '../../../web/e2e/helpers/layout-checks.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-29-smartphone.json', import.meta.url), 'utf8'),
);
const NAME = 'Ada kz29-name-q1x7';
const PROMPT = 'E2E29-PHONE kz29-prompt-j5f2: note this on the phone';
const ANSWER = 'Noted on the phone: kz29-answer-h7d3s9m1.';
const FOLLOW = 'E2E29-FOLLOW kz29-follow-prompt-z3k8: and once more';
const FOLLOW_ANSWER = 'Follow-up on the phone: kz29-follow-b2w6r5t8.';
const ENTRY = 'Phone note kz29-entry-c9g4';
const CANARIES = [
  'kz29-name-q1x7',
  'kz29-prompt-j5f2',
  'kz29-answer-h7d3s9m1',
  'kz29-follow-prompt-z3k8',
  'kz29-follow-b2w6r5t8',
  'kz29-file-v4p8n2c6',
  'kz29-entry-c9g4',
];
/** The iPhone 15 viewport (Playwright's device descriptor), checked in addition to the emulated Pixel 7 and 360 px. */
const IPHONE_15 = { width: 393, height: 659 } as const;
/** Absolute local time `HH:MM:SS.mmm`, prefixed with the date when not today (06-web.md section 12.4). */
const EVENT_TIME = /(^|\s)\d{2}:\d{2}:\d{2}\.\d{3}$/;

/** The layout checks of 06-web.md section 12 on the current screen, also at the iPhone 15 width. */
async function expectFits(page: Page, screen: string): Promise<void> {
  const problems = await layoutProblems(page);
  const own = page.viewportSize()!;
  await page.setViewportSize(IPHONE_15);
  const overflow = await horizontalOverflow(page);
  if (overflow !== null) problems.push(`scrolls horizontally at 393 px: ${overflow.join(', ')}`);
  problems.push(...(await smallTargets(page)).map((target) => `target@393 ${target}`));
  await page.setViewportSize(own);
  expect(problems, `${screen} on the phone`).toEqual([]);
}

async function expectEventTimes(times: Locator, minimum: number): Promise<void> {
  await expect.poll(() => times.count()).toBeGreaterThanOrEqual(minimum);
  for (const text of await times.allTextContents()) expect(text.trim()).toMatch(EVENT_TIME);
}

/** An entry of the "More" sheet of the bottom navigation. */
async function more(page: Page, id: string): Promise<void> {
  await page.getByTestId('shell.bottom.more').tap();
  await page.getByTestId(`shell.more.${id}`).tap();
}

test.describe.configure({ mode: 'serial' });

test('@E2E-29 @R-6.6 @R-P.5 @needs-vm smartphone: login, first task, chat, terminal, whiteboard and notifications', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  expect(test.info().project.use.isMobile, 'E2E-29 runs in the mobile project').toBe(true);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');

  // Registration from the platform invitation, then a new sign-in with the passkey after a reload.
  const onboarded = await onboard(page, orchestrator, NAME);
  await expect(page.getByTestId('shell.bottom')).toBeVisible();
  await expect(page.getByTestId('shell.nav')).toBeHidden();
  await expectFits(page, 'home');
  await page.reload();
  await expect(page.getByTestId('login.submit')).toBeVisible({ timeout: 30_000 });
  await expectFits(page, 'login');
  await page.getByTestId('login.submit').tap();
  await expect(page).toHaveURL(ORG_PATH, { timeout: 30_000 });

  // First task: composer on the home screen, countdown, answer with a tool summary in the chat.
  await page.getByTestId('composer.prompt').fill(PROMPT);
  await page.getByTestId('composer.send').tap();
  await expect(page).toHaveURL(TASK_PATH, { timeout: 30_000 });
  const task = TASK_PATH.exec(page.url())![1]!;
  await expect(page.getByTestId('countdown.panel').first()).toBeVisible({ timeout: 30_000 });
  await expectFits(page, 'countdown');
  await expectAnswerInChat(page, ANSWER);
  await expect(page.getByTestId('chat.file').first()).toBeVisible();

  // Follow-up in the same task; the chat lists every item with its absolute time.
  await page.getByTestId('chat.composer.prompt').fill(FOLLOW);
  await page.getByTestId('chat.composer.send').tap();
  await expectAnswerInChat(page, FOLLOW_ANSWER);
  expect(page.url()).toContain(`/t/${task}`);
  await expectEventTimes(page.getByTestId('chat.item.time'), 4);
  await expectFits(page, 'chat');

  // Terminal: the session's read-only view follows live; the history position shows its absolute time.
  const [session] = await mainSessions(orchestrator, task);
  await page.getByTestId('task.tabs.sessions').tap();
  await page.getByTestId('sessions.link').first().tap();
  await expect(page).toHaveURL(new RegExp(`/s/${uuid(session!.id)}$`));
  const terminal = page.getByTestId('terminal');
  await expect(terminal).toHaveAttribute('data-mode', 'follow', { timeout: 30_000 });
  await expect
    .poll(async () => Number(await terminal.getAttribute('data-chunks')), { timeout: 30_000 })
    .toBeGreaterThan(0);
  await expectFits(page, 'terminal');
  await page.getByTestId('terminal.mode.history').tap();
  await expect(page.getByTestId('terminal.position.time')).toHaveText(EVENT_TIME, { timeout: 30_000 });
  await expectFits(page, 'terminal history');

  // Whiteboard of the org through the "More" sheet: a new entry with its absolute time.
  await more(page, 'whiteboard');
  await expect(page).toHaveURL(new RegExp(`/o/${onboarded.org}/whiteboard$`));
  await page.getByTestId('whiteboard.create').tap();
  await expectFits(page, 'whiteboard editor');
  await page.getByTestId('whiteboard.editor.text').fill(ENTRY);
  await page.getByTestId('whiteboard.editor.save').tap();
  const entry = page.getByTestId('whiteboard.item').filter({ hasText: ENTRY });
  await expect(entry).toBeVisible();
  await expectEventTimes(entry.getByTestId('whiteboard.item.time'), 1);
  await expectFits(page, 'whiteboard');

  // Notifications from the bottom bar: the finished first turn ("task is waiting for you") leads back to the task.
  await page.getByTestId('shell.bottom.notifications').tap();
  const item = page.locator('[data-testid="notifications.item"][data-kind="task.idle"]');
  await expect(item).toHaveCount(1, { timeout: 30_000 });
  await expectEventTimes(page.getByTestId('notifications.item.time'), 1);
  await expectFits(page, 'notifications');
  await item.tap();
  await expect(page).toHaveURL(new RegExp(`/o/${onboarded.org}/t/${task}$`));

  await expectNoPlaintext(orchestrator, CANARIES);
});
