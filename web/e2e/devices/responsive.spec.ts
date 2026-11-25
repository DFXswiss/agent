// Every signed-in route of 06-web.md section 2.4 (and every task and session tab) with content from the device double,
// on the desktop and both phone projects (06-web.md section 12, WEB-13): no horizontal scrolling at 360 px, touch
// targets ≥ 44 × 44 px, no serious or critical axe-core violation, no CSP violation (fixture). The anonymous routes and
// the error pages are swept by `e2e/responsive.spec.ts`.
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { childSession, sessionEvents, terminalChunk } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';
import { navigateInApp } from '../helpers/whiteboard.ts';

const LONG_OUTPUT =
  '# A long answer\n\nA line with a very long word: ' +
  'Donaudampfschifffahrtsgesellschaftskapitänswitwenrentenversicherungsanstalt'.repeat(2) +
  ' and a link to [the documentation](https://example.com/a/very/long/path/that/does/not/wrap/easily/at/all).\n\n' +
  '```\n' +
  'const x = "a code block with a long line that must scroll inside its own box and not the page";\n' +
  '```\n\n| Column one | Column two | Column three |\n|---|---|---|\n| a | b | c |';

/** Opens `path` in the app and waits for `ready` (false if it does not appear); then a short pause for late content
 *  (live channel, lazy chunks). */
async function open(page: Page, path: string, ready: string): Promise<boolean> {
  await navigateInApp(page, path);
  const shown = await page
    .getByTestId(ready)
    .first()
    .waitFor({ timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  await page.waitForTimeout(300);
  return shown;
}

test('every signed-in route passes the layout, touch-target and accessibility checks', async ({ page, baseURL }) => {
  test.setTimeout(300_000);
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const api = await PageUser.open(page, user.recoveryCode);
  const me = (await api.request('GET', '/v1/me')) as { orgs: { org_id: string; default_wg: string }[] };
  const wg = me.orgs.find((org) => org.org_id === user.org)!.default_wg;
  const task = await sendPrompt(page, 'Check every screen on a phone');
  const session = (await mainSession(task)).session_id;
  await walk(session, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('countdown.summary')).toBeVisible();
  await sessionEvents(session, [
    { kind: 'session.output', body: { message_id: 'm1', role: 'assistant', turn: 1, text: LONG_OUTPUT } },
    {
      kind: 'session.tool',
      body: {
        tool_use_id: 't1',
        name: 'Read',
        input_summary: 'src/a/very/long/path/to/some/module/with/a/long/name/component.tsx',
        input: '{"file_path":"src/app.py"}',
        result_summary: 'print("hi")',
        is_error: false,
        duration_ms: 1200,
      },
    },
    {
      kind: 'session.file',
      body: { path: 'src/app.py', op: 'edit', sha256: 'a'.repeat(64), size: 12, patch: `-old\n+${'new '.repeat(40)}` },
    },
    { kind: 'session.hook', body: { event: 'PreToolUse', tool: 'Bash', decision: 'deny', reason: 'network' } },
    terminalChunk([[0, `$ ${'wide terminal output '.repeat(8)}`]]),
  ]);
  await childSession(session);
  const routes: [string, string][] = [
    [`/o/${user.org}`, 'home.screen'],
    [`/o/${user.org}/w/${wg}`, 'tasks.screen'],
    [`/o/${user.org}/t/${task}`, 'chat.item'],
    [`/o/${user.org}/t/${task}?tab=sessions`, 'task.screen'],
    [`/o/${user.org}/t/${task}?tab=operations`, 'task.screen'],
    [`/o/${user.org}/t/${task}?tab=whiteboard`, 'task.screen'],
    [`/o/${user.org}/t/${task}?tab=audit`, 'task.screen'],
    [`/o/${user.org}/s/${session}`, 'terminal.viewport'],
    [`/o/${user.org}/s/${session}?tab=chat`, 'session.screen'],
    [`/o/${user.org}/s/${session}?tab=children`, 'session.screen'],
    [`/o/${user.org}/whiteboard`, 'whiteboard.screen'],
    [`/o/${user.org}/w/${wg}/whiteboard`, 'whiteboard.screen'],
    [`/o/${user.org}/members`, 'members.screen'],
    [`/o/${user.org}/workgroups`, 'workgroups.screen'],
    [`/o/${user.org}/w/${wg}/settings`, 'wg-settings.screen'],
    [`/o/${user.org}/devices`, 'devices.screen'],
    [`/o/${user.org}/monitoring`, 'monitoring.screen'],
    [`/o/${user.org}/audit`, 'audit.screen'],
    ['/notifications', 'notifications.screen'],
    ['/settings', 'settings.page'],
    ['/subscriptions', 'subscriptions.page'],
    ['/platform/invites', 'platform-invites.forbidden'],
    ['/security', 'security.screen'],
    [`/o/${user.org}/no/such/screen`, 'notfound.page'],
  ];
  const problems: Record<string, string[]> = {};
  for (const [path, ready] of routes) {
    const found = (await open(page, path, ready)) ? await layoutProblems(page) : [`${ready} not shown`];
    if (found.length > 0) problems[path.replaceAll(user.org, ':org').replaceAll(wg, ':wg')] = found;
  }
  // dialogs and sheets: the member invitation, and on phones the "More" sheet of the bottom navigation
  await open(page, `/o/${user.org}/members`, 'members.screen');
  await page.getByTestId('members.invite.open').click();
  const invite = await layoutProblems(page);
  if (invite.length > 0) problems['members invite dialog'] = invite;
  await page.keyboard.press('Escape');
  if (page.viewportSize()!.width < 768) {
    await page.getByTestId('shell.bottom.more').click();
    await expect(page.getByTestId('shell.more')).toBeVisible();
    const more = await layoutProblems(page);
    if (more.length > 0) problems['more sheet'] = more;
  }
  expect(problems).toEqual({});
});
