// The read-only terminal (06-web.md section 5.5, ADR-016) against the real hub: the history and live chunks in Follow
// mode, History with the slider (off-screen replay up to the offset) and playback, the fixed label, no request for
// keystrokes or paste (network capture), no runtime <style> element and no CSP violation (fixture).
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { sessionEvents, terminalChunk } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

// Counts <style> elements that are in the document. A MutationObserver also reports additions inside a subtree that
// was just removed (transient observers): disposing a terminal after its view left the page (a tab switch) makes xterm
// install its DOM renderer in the detached element; those styles are never applied, so they are not counted.
const STYLE_OBSERVER = `new MutationObserver((records) => {
  for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeName === 'STYLE' && node.isConnected) document.documentElement.dataset.styleInjections =
      String(Number(document.documentElement.dataset.styleInjections ?? 0) + 1);
  }
}).observe(document, { childList: true, subtree: true });`;

async function openTerminal(page: Page, baseURL: string) {
  await page.addInitScript({ content: STYLE_OBSERVER });
  const user = await freshUser(page, baseURL, { poolReady: 2 });
  const task = await sendPrompt(page, 'Show me the terminal');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  return { user, task, session: session.session_id };
}

const chars = (page: Page, screen: 'a' | 'b') => page.getByTestId(`terminal.screen.${screen}`);

test('follows history and live chunks, replays up to an offset and plays back', async ({ page, baseURL }) => {
  const { user, session } = await openTerminal(page, baseURL!);
  // history before the view opens: two chunks (11 + 6 characters)
  await sessionEvents(session, [
    terminalChunk([
      [0, 'hello world'],
      [400, '\r\nline'],
    ]),
    terminalChunk([[1000, ' more!']]),
  ]);
  await page.goto(`/o/${user.org}/s/${session}`);
  await page.getByTestId('login.submit').click();
  const terminal = page.getByTestId('terminal');
  await expect(page.getByTestId('terminal.label')).toHaveText('Read-only view – prompts are entered in the chat');
  await expect(terminal).toHaveAttribute('data-mode', 'follow');
  await expect(terminal).toHaveAttribute('data-chunks', '2');
  await expect(page.getByTestId('terminal.viewport').locator('canvas').first()).toBeVisible();
  await expect(chars(page, 'a')).toHaveAttribute('data-chars', String('hello world\r\nline more!'.length));
  // live chunks arrive over the stream (`terminal=`) and are appended in order
  await sessionEvents(session, [terminalChunk([[2000, '\r\nlive']])]);
  await expect(terminal).toHaveAttribute('data-chunks', '3');
  await expect(chars(page, 'a')).toHaveAttribute('data-chars', String('hello world\r\nline more!\r\nlive'.length));
  // History: the slider replays up to the chosen offset into the hidden screen, then shows it
  await page.getByTestId('terminal.mode.history').click();
  await expect(terminal).toHaveAttribute('data-mode', 'history');
  await expect(page.getByTestId('terminal.position')).toHaveText('0:02.0 of 0:02.0');
  await page.getByTestId('terminal.slider').fill('500');
  await expect(chars(page, 'b')).toHaveAttribute('data-chars', String('hello world\r\nline'.length));
  await expect(chars(page, 'b')).not.toHaveClass(/invisible/);
  await expect(chars(page, 'a')).toHaveClass(/invisible/);
  await expect(page.getByTestId('terminal.position')).toHaveText('0:00.5 of 0:02.0');
  // the position as absolute local time (06-web.md section 12.4)
  await expect(page.getByTestId('terminal.position.time')).toHaveText(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  // playback at 16× runs to the end
  await page.getByTestId('terminal.speed.16').click();
  await expect(page.getByTestId('terminal.speed.16')).toHaveAttribute('aria-pressed', 'true');
  await page.getByTestId('terminal.play').click();
  await expect(page.getByTestId('terminal.position')).toHaveText('0:02.0 of 0:02.0');
  await expect(page.getByTestId('terminal.play')).toHaveAttribute('data-playing', 'false');
  await expect(chars(page, 'b')).toHaveAttribute('data-chars', String('hello world\r\nline more!\r\nlive'.length));
  // a chunk during History is not written; back to Follow, everything is replayed and the live tail continues
  await sessionEvents(session, [terminalChunk([[3000, '!']])]);
  await expect(terminal).toHaveAttribute('data-chunks', '4');
  await page.getByTestId('terminal.mode.follow').click();
  await expect(terminal).toHaveAttribute('data-mode', 'follow');
  await expect(chars(page, 'a')).toHaveAttribute('data-chars', String('hello world\r\nline more!\r\nlive!'.length));
  await expect(chars(page, 'a')).not.toHaveClass(/invisible/);
  expect(await page.locator('html').getAttribute('data-style-injections')).toBeNull();
});

test('a task opened afresh shows "Claude Code" first and keeps its terminal across tab switches (KONZEPT E-43)', async ({
  page,
  baseURL,
}) => {
  await page.addInitScript({ content: STYLE_OBSERVER });
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Show me Claude Code', 'terminal');
  const session = (await mainSession(task)).session_id;
  // the start as terminal lines until the session works
  await expect(page.getByTestId('terminal.start')).toBeVisible();
  await walk(session, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('terminal.start')).toHaveCount(0);
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-variant', 'task');
  const banner = '\x1b[1m* Welcome to Claude Code\x1b[0m\r\n\r\n> Show me Claude Code';
  await sessionEvents(session, [terminalChunk([[0, banner]])]);
  // opened afresh, the task page shows "Claude Code" first; the observer counts the style elements of this page only
  await page.goto(`/o/${user.org}/t/${task}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('task.tabs.terminal')).toHaveAttribute('aria-current', 'page');
  const terminal = page.getByTestId('terminal');
  await expect(terminal).toHaveAttribute('data-variant', 'task');
  await expect(terminal).toHaveAttribute('data-chunks', '1');
  await expect(page.getByTestId('terminal.label')).toHaveCount(0);
  await expect(page.getByTestId('terminal.viewport').locator('canvas').first()).toBeVisible();
  // this screen has no input box of the TUI: the one input is docked below it
  await expect(page.getByTestId('terminal.input')).toHaveAttribute('data-placement', 'docked');
  const prompt = page.getByTestId('chat.composer.prompt');
  await prompt.fill('First line');
  await prompt.press('Shift+Enter');
  await prompt.pressSequentially('second line');
  await expect(prompt).toHaveValue('First line\nsecond line');
  await prompt.press('Enter');
  await expect(prompt).toHaveValue('');
  await page.getByTestId('task.tabs.chat').click();
  await expect(page.getByTestId('chat.prompt.text').last()).toContainText('second line');
  // back on "Claude Code" the terminal is there again
  await page.getByTestId('task.tabs.terminal').click();
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-chunks', '1');
  expect(await page.locator('html').getAttribute('data-style-injections')).toBeNull();
});

test('keystrokes and paste in the terminal produce no request', async ({ page, baseURL, context }) => {
  const { user, session } = await openTerminal(page, baseURL!);
  await sessionEvents(session, [terminalChunk([[0, '$ ']])]);
  await page.goto(`/o/${user.org}/s/${session}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-chunks', '1');
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText('rm -rf /\n'));
  // settle: no pull or stream reconnect pending
  await page.waitForTimeout(1500);
  const requests: string[] = [];
  // The community engine serves keys on its own schedule: after the sign-in, and on frames after a random delay of up
  // to 3 s (`core/community/engine.ts`). Under load the start-up round can still run after the settle above, so those
  // requests are not counted; every other request is (HOT-15).
  const background = (url: string) => new URL(url).pathname.startsWith('/v1/community/');
  page.on('request', (request) => {
    if (!background(request.url())) requests.push(`${request.method()} ${request.url()}`);
  });
  await page.getByTestId('terminal.viewport').locator('.xterm').first().click();
  // the keys really reach the terminal: its (input-disabled) text area has the focus
  expect(await page.evaluate(() => document.activeElement?.classList.contains('xterm-helper-textarea'))).toBe(true);
  await page.keyboard.type('echo hi');
  await page.keyboard.press('Enter');
  await page.keyboard.press('Control+C');
  await page.keyboard.press('ControlOrMeta+V');
  await page.getByTestId('terminal.viewport').dispatchEvent('paste');
  await page.waitForTimeout(1500);
  expect(requests).toEqual([]);
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-chunks', '1');
  expect(await page.locator('html').getAttribute('data-style-injections')).toBeNull();
});

test('a lost WebGL context hands the terminal to the canvas renderer, without a style element', async ({
  page,
  baseURL,
}) => {
  const { user, session } = await openTerminal(page, baseURL!);
  await sessionEvents(session, [terminalChunk([[0, 'before the loss']])]);
  await page.goto(`/o/${user.org}/s/${session}`);
  await page.getByTestId('login.submit').click();
  const screenA = chars(page, 'a');
  await expect(screenA).toHaveAttribute('data-chars', String('before the loss'.length));
  // the WebGL renderer draws (no layer canvases of the canvas renderer yet)
  await expect(screenA.locator('canvas.xterm-text-layer')).toHaveCount(0);
  const lost = await screenA.locator('canvas').evaluateAll((canvases) =>
    canvases
      .map((canvas) => (canvas as HTMLCanvasElement).getContext('webgl2'))
      .filter((gl) => gl !== null)
      .map((gl) => {
        gl.getExtension('WEBGL_lose_context')!.loseContext();
        return true;
      }),
  );
  expect(lost).toEqual([true]);
  // the addon waits 3 s for a restore that never comes, then the canvas renderer takes over and keeps drawing
  await expect(screenA.locator('canvas.xterm-text-layer')).toHaveCount(1);
  await sessionEvents(session, [terminalChunk([[1000, ' and after']])]);
  await expect(screenA).toHaveAttribute('data-chars', String('before the loss and after'.length));
  expect(await page.locator('html').getAttribute('data-style-injections')).toBeNull();
});
