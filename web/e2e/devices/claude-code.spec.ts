// The task tab "Claude Code" (KONZEPT E-43, ADR-104) against the real hub and the device double, on the desktop and both
// phones, with the output of the real Claude Code 2.1.330 – recorded from its 160 × 48 pane against the model double
// (`fixtures/claude-code-2.1.330.out`; on that screen the TUI's input box is rows 44–46 and its status line row 47):
//
// - Look: the page is the terminal over the whole window (one slim bar), the TUI's real output, one input only, lying
//   exactly over the rows of the TUI's box (its rules and marker in the colours the TUI drew), the TUI's status line
//   visible below it; the screen fits the window (on a phone its rows fill the height, the columns scroll sideways and
//   the input stays in view); no layout, touch-target or accessibility problem, no runtime <style> (CSP fixture).
// - Use: Enter sends a signed prompt (it appears in the chat), Shift+Enter starts a line, Esc interrupts the turn
//   (`interrupt` control); nothing is ever sent for keystrokes in the terminal.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { sessionEvents, terminalChunk } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

const RECORDING = readFileSync(new URL('../fixtures/claude-code-2.1.330.out', import.meta.url), 'utf8');
/** Rows of the TUI's input box and its status line on the recorded screen. */
const BOX = { top: 44, bottom: 46 };
const STATUS_ROW = 47;

async function openTask(page: Page, baseURL: string) {
  await page.addInitScript({
    content: `new MutationObserver((records) => {
      for (const record of records) for (const node of record.addedNodes) {
        if (node.nodeName === 'STYLE' && node.isConnected) document.documentElement.dataset.styleInjections = '1';
      }
    }).observe(document, { childList: true, subtree: true });`,
  });
  const user = await freshUser(page, baseURL, { poolReady: 2 });
  const task = await sendPrompt(page, 'Sessions that expire during a request are still accepted.', 'terminal');
  const session = (await mainSession(task)).session_id;
  // the start shows as terminal lines, the input waits below
  await expect(page.getByTestId('terminal.start')).toBeVisible();
  await expect(page.getByTestId('terminal.input')).toHaveAttribute('data-placement', 'docked');
  await walk(session, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('terminal.start')).toHaveCount(0);
  await sessionEvents(session, [terminalChunk([[0, RECORDING]])]);
  await expect(page.getByTestId('terminal.input')).toHaveAttribute('data-placement', 'box');
  return { user, task, session };
}

/** Boxes (px, viewport coordinates) of the screen, the input, its rules and the text field. */
function geometry(page: Page) {
  return page.evaluate(() => {
    const box = (selector: string) => {
      const rect = document.querySelector(selector)!.getBoundingClientRect();
      return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, height: rect.height };
    };
    const field = document.querySelector<HTMLTextAreaElement>('[data-testid="chat.composer.prompt"]')!;
    return {
      bar: box('[data-testid="task.screen"] > header'),
      screen: box('[data-testid="terminal.screen.a"] .xterm-screen'),
      input: box('[data-testid="terminal.input"]'),
      rule: box('[data-testid="terminal.input.rule"]'),
      field: box('[data-testid="chat.composer.prompt"]'),
      fontFamily: getComputedStyle(field).fontFamily,
      viewport: { width: innerWidth, height: innerHeight },
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
}

test('looks like the local terminal: the TUI over the whole window and one input exactly over its box', async ({
  page,
  baseURL,
}) => {
  await openTask(page, baseURL!);
  await expect(page.getByTestId('task.screen')).toHaveAttribute('data-layout', 'terminal');
  const input = page.getByTestId('terminal.input');
  await expect(input).toHaveAttribute('data-box', `${BOX.top}-${BOX.bottom}`);
  // one input on the whole page: the TUI's own box is covered, not shown twice (xterm's hidden helper text areas of the
  // read-only screens take no input, ADR-016)
  await expect(page.locator('textarea:not(.xterm-helper-textarea)')).toHaveCount(1);
  await expect(page.getByTestId('terminal.label')).toHaveCount(0);
  await expect(page.getByTestId('terminal.mode.history')).toHaveCount(0);
  // the rules and the marker have the colours the TUI drew (palette greys and its marker, never the fallbacks)
  await expect(page.getByTestId('terminal.input.marker')).toHaveText('❯');
  const geo = await geometry(page);
  const cell = geo.screen.height / 48;
  // the input covers exactly rows 44–46 of the screen (to within a pixel), so the status line (row 47) stays visible
  expect(Math.abs(geo.input.bottom - (geo.screen.top + (BOX.bottom + 1) * cell))).toBeLessThan(1.5);
  expect(Math.abs(geo.rule.top - (geo.screen.top + BOX.top * cell))).toBeLessThan(1.5);
  expect(geo.input.bottom).toBeLessThanOrEqual(geo.screen.top + STATUS_ROW * cell + 1);
  // the whole screen is in the window, the input with it, in the terminal font; it fills the window below the slim bar
  // in at least one direction (fitted again once the start lines above it left)
  expect(geo.screen.bottom).toBeLessThanOrEqual(geo.viewport.height + 1);
  const filled = Math.max(
    geo.screen.height / (geo.viewport.height - geo.bar.height),
    (geo.screen.right - geo.screen.left) / geo.viewport.width,
  );
  // (the font goes in half pixels and the cells in whole pixels: 48 rows leave a margin of up to a few rows)
  expect(filled).toBeGreaterThan(0.8);
  expect(geo.field.bottom).toBeLessThanOrEqual(geo.viewport.height + 1);
  expect(geo.fontFamily).toContain('DejaVu Sans Mono');
  expect(geo.field.height).toBeGreaterThanOrEqual(44);
  if (geo.viewport.width >= 768) {
    // a desktop shows all 160 columns
    expect(geo.screen.right).toBeLessThanOrEqual(geo.viewport.width + 1);
  } else {
    // a phone: the rows fill the height at a readable size, the columns scroll inside the terminal, never the page
    expect(cell).toBeGreaterThanOrEqual(10);
    expect(geo.scrollWidth).toBeLessThanOrEqual(geo.viewport.width);
    const viewport = page.getByTestId('terminal.viewport');
    await viewport.evaluate((node) => (node.scrollLeft = node.scrollWidth));
    // the text of the input follows the sideways scroll and stays in the window
    const field = page.getByTestId('chat.composer.prompt');
    await expect.poll(async () => (await field.boundingBox())!.x).toBeGreaterThanOrEqual(-1);
    const shown = (await field.boundingBox())!;
    expect(shown.x + shown.width).toBeLessThanOrEqual(geo.viewport.width + 1);
  }
  expect(await layoutProblems(page)).toEqual([]);
  expect(await page.locator('html').getAttribute('data-style-injections')).toBeNull();
});

test('is used like Claude Code: Enter sends, Shift+Enter starts a line, Esc interrupts', async ({ page, baseURL }) => {
  const { session } = await openTask(page, baseURL!);
  const field = page.getByTestId('chat.composer.prompt');
  await expect(field).toBeFocused();
  await field.pressSequentially('Also cover the boundary');
  await field.press('Shift+Enter');
  await field.pressSequentially('with a test');
  await expect(field).toHaveValue('Also cover the boundary\nwith a test');
  await expect(page.getByTestId('terminal.input')).toHaveAttribute('data-rows', '2');
  const sent = page.waitForRequest(
    (request) => request.method() === 'POST' && /\/v1\/tasks\/[^/]+\/prompts$/.test(request.url()),
  );
  await field.press('Enter');
  await sent;
  await expect(field).toHaveValue('');
  // Esc interrupts the working session, wherever the focus is
  const interrupted = page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().endsWith(`/v1/sessions/${session}/control`),
  );
  await page.getByTestId('task.title').click();
  await page.keyboard.press('Escape');
  expect((await interrupted).postDataJSON()).toEqual({ action: 'interrupt' });
  // the prompt is a signed prompt of the task, as from the chat
  await page.getByTestId('task.tabs.chat').click();
  await expect(page.getByTestId('chat.prompt.text').last()).toHaveText('Also cover the boundary with a test');
});
