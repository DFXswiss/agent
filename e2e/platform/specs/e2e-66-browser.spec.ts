// E2E-66 (KOR-44; docs/platform/spec/13-work-vm.md section 13, ADR-085): the platform browser. The runner offers work
// VMs in self mode (TEST_MODE, three slots, a warm pool of two, browser idle time 60 s); the orchestrator serves a
// small sign-in site on the device's loopback, which a browser VM reaches as `host.lima.internal` (self mode has no
// host filter; the outside boundary of a browser VM is the work VM's and is checked by the attack suite, WVM).
//
// Task 1: the session opens the platform browser on the site, signs in through the form (the password is masked in
// the record), waits while the user watches the page live (desktop or phone, by the project), downloads the report
// and closes the browser: every action is in the record with the picture after it, the file came down once, the VM
// is deleted with `browser_closed`, and no live frame is posted before anybody watches or after the viewer left.
// Task 2: a tool call opens the browser implicitly, the browser VM is stopped by force, the next action answers that
// the browser was started again (a fresh VM, `restarted`), and the browser is left alone until the idle teardown
// (`browser_idle`). Start times go to the iteration's artefacts (`e2e-63/browser.json`). T1 only.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const PASSWORD = 'kz63-secret-w8e2r6t4';
const REPORT = 'day,amount\nkz63-report-a1s5d9f3,42\n';
const URL_MARK = 'kz63-url-m2n6b4v8';
const CANARIES = [PASSWORD, 'kz63-report-a1s5d9f3', URL_MARK, 'kz63-prompt-g3h7j1k5', 'kz63-answer-p4s8d2f6'];
const PROMPT = 'E2E63-BROWSER kz63-prompt-g3h7j1k5: sign in to the example site and download the report';
const FAILOVER = 'E2E63-FAILOVER: open the example site in the browser';

interface VmRow {
  readonly lima_name: string;
  readonly state: string;
  readonly delete_reason: string | null;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-66 @needs-vm @needs-workvm the platform browser signs in, downloads, is watched live, recorded and torn down', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-66 is a T1 test (browser VMs in self mode on the runner device)');
  test.setTimeout(1_500_000);
  const site = await orchestrator.call<{ url: string }>('POST', '/site', { password: PASSWORD, report: REPORT });
  const scenario = JSON.parse(readFileSync(new URL('../scenarios/e2e-66-browser.json', import.meta.url), 'utf8')) as {
    variables: Record<string, string>;
  };
  scenario.variables = { site: site.url, password: PASSWORD };
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await orchestrator.call('POST', '/runners/workvm', {
    slots: 3,
    max_per_task: 2,
    pool_size: 2,
    browser_idle_seconds: 60,
  });
  await poolReady(orchestrator);
  await expect
    .poll(
      async () =>
        (
          await runnerStore<{ n: number }>(
            orchestrator,
            "SELECT count(*)::int AS n FROM runner.workvm_pool WHERE state = 'ready'",
          )
        )[0]!.n,
      { timeout: 900_000, intervals: [5_000] },
    )
    .toBe(2);

  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada');

  // 1. Sign in and download through the platform browser.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expect(page.getByTestId('chat.browser.state').first()).toBeVisible({ timeout: 120_000 });
  // Nobody watches yet: no live frame reached the hub.
  const frames = async (): Promise<number> =>
    (await orchestrator.hubQuery<{ n: number }>('SELECT count(*)::int AS n FROM hub.browser_live'))[0]!.n;
  const leases = async (): Promise<number> =>
    (await orchestrator.hubQuery<{ n: number }>('SELECT count(*)::int AS n FROM hub.browser_watch'))[0]!.n;
  expect(await frames()).toBe(0);

  // The user watches: the live picture appears while the session waits on the signed-in page.
  await page.getByTestId('chat.browser.state').last().getByTestId('browser.watch').click();
  const live = page.getByTestId('browser.live');
  await expect(live).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('browser.live.badge')).toHaveText(/Live/, { timeout: 60_000 });
  await expect.poll(async () => Number(await live.getAttribute('data-seq')), { timeout: 60_000 }).toBeGreaterThan(0);
  const seen = Number(await live.getAttribute('data-seq'));
  expect(await leases()).toBeGreaterThan(0);
  await expect(page.getByTestId('browser.note.workvm')).toBeVisible();
  // Leaving the view ends the watch at once; the latest frame goes with the last lease.
  await page.goBack();
  await expect.poll(leases, { timeout: 30_000 }).toBe(0);
  await expect.poll(frames, { timeout: 30_000 }).toBe(0);

  await expectAnswerInChat(page, 'The report is in the workspace kz63-answer-p4s8d2f6.');
  expect(await orchestrator.call('GET', '/site')).toEqual({ logins: 1, failed_logins: 0, downloads: 1 });

  // The record: every action with the picture after it; the password masked.
  const actions = await taskBodies(page, orchestrator, onboarded, task, 'browser.action');
  const tools = actions.map((body) => body.tool);
  expect(tools).toEqual(expect.arrayContaining(['browser_type', 'browser_snapshot', 'browser_download']));
  expect(actions.map((body) => body.seq)).toEqual(
    [...actions.keys()].map((index) => (actions[0]!.seq as number) + index),
  );
  const typed = actions.filter((body) => body.tool === 'browser_type');
  expect(typed.map((body) => body.text)).toEqual(['ada', '••••']);
  expect(JSON.stringify(actions)).not.toContain(PASSWORD);
  for (const body of actions) {
    expect(body.ok).toBe(true);
    const image = body.image as { type: string; width: number; data: string } | null;
    expect(image?.type).toBe('image/jpeg');
    expect(image!.width).toBeLessThanOrEqual(960);
  }
  const download = actions.find((body) => body.tool === 'browser_download')!;
  expect(String(download.url)).toContain('/account');
  const states = await taskBodies(page, orchestrator, onboarded, task, 'browser.state');
  expect(states.map((body) => body.state)).toEqual(['starting', 'open', 'closed']);
  expect(states.at(-1)!.reason).toBe('browser_closed');
  const readyMs = states.find((body) => body.state === 'open')!.ready_ms as number;

  // The record in the browser tab (after the teardown): rows newest first with the masked password.
  await page.getByTestId('chat.browser.state').last().getByTestId('browser.record.open').click();
  await expect(page.getByTestId('browser.record.row')).toHaveCount(actions.length, { timeout: 60_000 });
  await expect(page.getByTestId('browser.record.text').filter({ hasText: '••••' })).toHaveCount(1);
  await expect(page.getByTestId('browser.live.badge')).not.toHaveText(/Live/);
  await page.goBack();

  // The browser VM went with the close.
  const vmsOf = (id: string): Promise<VmRow[]> =>
    runnerStore<VmRow>(
      orchestrator,
      `SELECT lima_name, state, delete_reason FROM runner.workvm WHERE scope = 'browser' AND task_id = '${uuid(id)}' ORDER BY created_at`,
    );
  await expect
    .poll(async () => (await vmsOf(task)).map((row) => [row.state, row.delete_reason]), { timeout: 120_000 })
    .toEqual([['deleted', 'browser_closed']]);

  // 2. Implicit open, a failed VM replaced on the next action, then the idle teardown.
  await home(page);
  const second = await sendPrompt(page, FAILOVER);
  await expect
    .poll(async () => (await vmsOf(second)).map((row) => row.state), { timeout: 120_000 })
    .toEqual(['running']);
  const [first] = await vmsOf(second);
  await orchestrator.call('POST', '/runners/workvm/kill', { lima_name: first!.lima_name });
  await expectAnswerInChat(page, 'The browser came back kz63-answer-h7j1k5l9.');
  const restarted = await taskBodies(page, orchestrator, onboarded, second, 'browser.state');
  expect(restarted[0]!.implicit).toBe(true);
  expect(restarted.map((body) => body.state)).toEqual(expect.arrayContaining(['open', 'restarted']));
  const failoverActions = await taskBodies(page, orchestrator, onboarded, second, 'browser.action');
  expect(failoverActions.some((body) => String(body.notice ?? '').includes('started again'))).toBe(true);
  expect(failoverActions.map((body) => body.seq)).toEqual(
    [...failoverActions.keys()].map((index) => (failoverActions[0]!.seq as number) + index),
  );
  await expect
    .poll(async () => (await vmsOf(second)).map((row) => [row.state, row.delete_reason]), {
      timeout: 300_000,
      intervals: [5_000],
    })
    .toEqual([
      ['deleted', 'vm_lost'],
      ['deleted', 'browser_idle'],
    ]);
  expect((await taskBodies(page, orchestrator, onboarded, second, 'browser.state')).at(-1)).toMatchObject({
    state: 'closed',
    reason: 'browser_idle',
  });

  const dir = join(e2e.iterationDir, 'e2e-63');
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, 'browser.json'),
    JSON.stringify(
      { ready_ms: readyMs, actions: actions.length, live_seq_seen: seen, project: test.info().project.name },
      null,
      1,
    ),
  );
  await expectNoPlaintext(orchestrator, CANARIES);
});
