// GitHub when a task needs it (KOR-25 part three, ADR-059, ADR-060) against the real hub with the device double, on the
// desktop and both phones: the composer recognises a GitHub reference and offers connecting while the task is sent
// anyway; the runner's held operations (`session.status` `waiting`, written by the device) are one card in the task's
// chat with the platform's text, the operation type and the checked repository only; the owner stores an existing
// token right in the card (sealed to the device, which answers `done`), the card turns into "connected", and the
// settings say "Connected as <login>" with the source and the scopes.
import { expect, test } from '../fixtures/test.ts';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatTime } from '../../src/core/encoding/time';
import { mainSession, TASK_PATH, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { answerRequest } from '../helpers/operations.ts';
import { sessionEvents } from '../helpers/sessions.ts';
import { freshUser, START_STEPS } from '../helpers/tasks.ts';

const PROMPT = 'schau dir das neuste issue in example-org/example-repo an';
const TOKEN = 'ghp_not_a_real_token_0123456789';

const waiting = (operation: string, type: string) => ({
  kind: 'session.status',
  body: {
    state: 'waiting',
    waiting_for: 'github_connection',
    operation_id: operation,
    operation_type: type,
    repo: 'example-org/example-repo',
    until: formatTime(Date.now() + 15 * 60_000),
  },
});

test('a task that needs GitHub asks for the connection in its chat and goes on with a stored token', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(120_000);
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  // the composer offers connecting for an obvious GitHub reference; the task is sent anyway
  const prompt = page.getByTestId('composer.prompt');
  await prompt.fill('Explain quantum computing');
  await expect(page.getByTestId('composer.github-hint')).toHaveCount(0);
  await prompt.fill(PROMPT);
  const hint = page.getByTestId('composer.github-hint');
  await expect(hint).toContainText('This task looks like it needs GitHub');
  await expect(hint.getByTestId('github.connect')).toBeVisible();
  await expect(hint.getByTestId('github.token')).toBeVisible();
  await page.getByTestId('composer.send').click();
  await expect(page).toHaveURL(TASK_PATH);
  const task = TASK_PATH.exec(page.url())![1]!;
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
  // two held operations of the session: one card
  await sessionEvents(session.session_id, [
    waiting(uuid7(), 'github.list_issues'),
    waiting(uuid7(), 'github.get_issue'),
  ]);
  const card = page.getByTestId('waiting.card');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-status', 'open');
  await expect(card).toHaveAttribute('data-owner', 'true');
  await expect(card.getByTestId('waiting.title')).toHaveText('Connect GitHub so that the task can go on');
  await expect(card.getByTestId('waiting.operation')).toHaveText('Read issue');
  await expect(card.getByTestId('waiting.repo')).toHaveText('example-org/example-repo');
  await expect(card.getByTestId('waiting.until')).toContainText('2 operations wait. Waits at most 15 min more.');
  // usable on a phone: no horizontal scrolling, touch targets, accessibility
  expect(await layoutProblems(page)).toEqual([]);
  // store an existing token right in the card
  await card.getByTestId('github.token').click();
  await expect(card.getByTestId('github.token.recommend')).toContainText('fine-grained personal access token');
  await expect(card.getByTestId('github.devices.select')).toHaveValue(user.device);
  await card.getByTestId('github.token.input').fill(TOKEN);
  await card.getByTestId('github.start').click();
  await expect(card.getByTestId('github.waiting')).toBeVisible();
  const opened = await answerRequest(user.device, [
    {
      body: { step: 'done', login: 'octo-cat', scopes: ['repo'], source: 'token' },
      final: true,
      store: { login: 'octo-cat', scopes: ['repo'], source: 'token' },
    },
  ]);
  // the token went only into the request sealed to the device
  expect(opened).toMatchObject({ kind: 'github.connect', body: { token: TOKEN } });
  await expect(card).toHaveAttribute('data-status', 'done');
  await expect(card).toHaveText(/GitHub is connected – the task goes on\./);
  await expect(page.getByTestId('github.token.input')).toHaveCount(0);
  // the settings show the login, the source and the scopes
  await page.goto('/settings');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('github.connected')).toHaveText('Connected as octo-cat');
  await expect(page.getByTestId('github.source')).toHaveText('Connected through: Stored token');
  await expect(page.getByTestId('github.scopes')).toHaveText('Scopes: repo');
  await expect(page.getByTestId('github.scopes.classic')).toContainText('A fine-grained token is recommended.');
  await expect(page.getByTestId('github.connect')).toHaveCount(0);
});
