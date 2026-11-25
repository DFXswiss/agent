// The platform browser in the web (06-web.md section 5.4b, ADR-085) against the real hub, on the desktop and on the
// phones: the chat card of `browser.state` with "Watch live" into the session's Browser tab, the action record newest
// first (typed text as stored, masked by the runner), a live entry arriving while the tab watches, the newest recorded
// picture scaled to the width, the bandwidth choice kept in the browser and the note on the work VM's own browser.
// The live picture itself needs a device that posts frames: it is covered by the unit tests and E2E-66.
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

// a 1 × 1 JPEG (the record only stores and shows it)
const JPEG = Buffer.from(
  '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgy' +
    'PC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==',
  'base64',
).toString('base64url');
const BROWSER = 'br-0123456789ab';

const opened = {
  kind: 'browser.state',
  body: {
    browser_id: BROWSER,
    operation_id: null,
    implicit: true,
    state: 'open',
    workvm_id: 'wvm-0123abcd',
    reason: null,
    url: 'https://example.com/login',
    actions: 0,
    ready_ms: 3200,
    idle_seconds: 600,
    opened_at: new Date().toISOString(),
    closed_at: null,
  },
};

const action = (seq: number, tool: string, name: string, extra: Record<string, unknown> = {}) => ({
  kind: 'browser.action',
  body: {
    browser_id: BROWSER,
    seq,
    tool,
    action: name,
    target: null,
    text: null,
    url: 'https://example.com/login',
    title: 'Sign in',
    tab: 0,
    ok: true,
    error: null,
    notice: null,
    at: new Date().toISOString(),
    duration_ms: 80,
    image: { type: 'image/jpeg', width: 1, height: 1, data: JPEG },
    image_omitted: null,
    ...extra,
  },
});

test('shows the browser card, the record newest first and the recorded picture', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Sign in on the test page');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await sessionEvents(main.session_id, [
    opened,
    action(1, 'browser_navigate', 'navigate'),
    action(2, 'browser_type', 'type', { target: '#password', text: '••••' }),
    action(3, 'browser_click', 'click', { target: 'text=Sign in' }),
  ]);
  const card = page.getByTestId('chat.browser.state');
  await expect(card).toHaveAttribute('data-state', 'open');
  await expect(card.getByTestId('chat.browser.state.implicit')).toBeVisible();
  await card.getByTestId('browser.watch').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/s/${main.session_id}\\?tab=browser$`));
  await expect(page.getByTestId('session.tabs.browser')).toHaveAttribute('aria-current', 'page');
  const rows = page.getByTestId('browser.record.row');
  await expect(rows).toHaveCount(3);
  await expect(rows.first()).toHaveAttribute('data-tool', 'browser_click');
  await expect(rows.nth(1).getByTestId('browser.record.text')).toHaveText('••••');
  const picture = page.getByTestId('browser.live');
  await expect(picture).toHaveAttribute('data-source', 'record');
  await expect(picture).toHaveAttribute('data-seq', '3');
  await expect(picture).toHaveAttribute('data-browser', BROWSER);
  await expect(page.getByTestId('browser.live.badge')).toContainText('Recorded picture of');
  // scaled to the width of the page, phones included
  const box = (await picture.boundingBox())!;
  expect(box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await expect(page.getByTestId('browser.note.workvm')).toBeVisible();
  // a new entry arrives while the tab watches the session's browser
  await sessionEvents(main.session_id, [action(4, 'browser_press', 'press')]);
  await expect(rows).toHaveCount(4);
  await expect(rows.first()).toHaveAttribute('data-tool', 'browser_press');
  // the bandwidth choice stays in this browser
  await page.getByTestId('browser.kbps').selectOption('sharp');
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('browser.kbps')).toHaveValue('sharp');
  await expect(rows).toHaveCount(4);
});
