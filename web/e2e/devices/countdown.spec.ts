// The countdown of a session start (06-web.md section 4.4, KONZEPT §3) against the real hub with scripted session
// transitions (device double): on time, delayed, device offline (and back), no free slot, failed with "Retry".
import { expect, test } from '../fixtures/test.ts';
import { mainSession, setOnline, walk } from '../helpers/director.ts';
import { expectNoSpinner, freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

test('on time: digits count down, the steps fill with device time and elapsed time, then "Ready in"', async ({
  page,
  baseURL,
}) => {
  await freshUser(page, baseURL!, { poolReady: 0 });
  const task = await sendPrompt(page, 'What is 2+2?');
  const digits = page.getByTestId('countdown.digits');
  await expect(digits).toBeVisible();
  const first = Number(await digits.textContent());
  expect(first).toBeGreaterThan(0);
  expect(first).toBeLessThanOrEqual(10);
  await expect(page.getByTestId('countdown.ring')).toBeVisible();
  await expectNoSpinner(page);
  await expect(page.getByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'done');
  await expect(page.getByTestId('countdown.step.vm-assigning')).toHaveAttribute('data-status', 'current');
  const session = await mainSession(task);
  await walk(session.session_id, ['vm_assigning', 'vm_ready']);
  const vmReady = page.getByTestId('countdown.step.vm-ready');
  await expect(vmReady).toHaveAttribute('data-status', 'done');
  await expect(vmReady.locator('time')).toHaveText(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  await expect(vmReady).toContainText(/\+\d+\.\d s/);
  await expect(page.getByTestId('countdown.step.sandbox-ready')).toHaveAttribute('data-status', 'current');
  await expect(page.getByTestId('countdown.announce')).toContainText('seconds left: Sandbox ready');
  await expect(digits).toBeVisible();
  await expectNoSpinner(page);
  await walk(session.session_id, ['sandbox_ready', 'agent_starting', 'prompt_delivering', 'working'], {
    intervalMs: 100,
  });
  await expect(page.getByTestId('countdown.summary')).toHaveText(/^Ready in \d+\.\d s$/);
  await expect(page.getByTestId('countdown.digits')).toHaveCount(0);
});

test('delayed: at 0 the digits give way to "Delayed" with the current step and a live elapsed time', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(60_000);
  await freshUser(page, baseURL!, { poolReady: 0 });
  const task = await sendPrompt(page, 'Summarize the current state of fusion research');
  const session = await mainSession(task);
  await walk(session.session_id, ['vm_assigning', 'vm_ready', 'sandbox_ready', 'agent_starting']);
  await expect(page.getByTestId('countdown.delayed')).toHaveText('Delayed', { timeout: 15_000 });
  await expect(page.getByTestId('countdown.digits')).toHaveCount(0);
  const elapsed = page.getByTestId('countdown.elapsed');
  await expect(elapsed).toContainText('Starting the agent –');
  const before = await elapsed.textContent();
  await expect(elapsed).not.toHaveText(before!);
  await expectNoSpinner(page);
  await walk(session.session_id, ['prompt_delivering', 'working']);
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
});

test('offline: the real reason instead of digits; the task starts when the device is back', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!, { online: false, poolReady: 2 });
  const task = await sendPrompt(page, 'Explain this repository: agent-core');
  const reason = page.getByTestId('countdown.reason');
  await expect(reason).toHaveAttribute('data-reason', 'device_offline');
  await expect(reason).toHaveText('The execution device is offline – the task starts as soon as it is back.');
  await expect(page.getByTestId('countdown.elapsed')).toContainText('Waiting for');
  await expect(page.getByTestId('countdown.digits')).toHaveCount(0);
  await expectNoSpinner(page);
  await setOnline(user.device, true);
  await expect(page.getByTestId('countdown.digits')).toBeVisible();
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 50 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
});

test('no free slot: "All execution slots are busy"', async ({ page, baseURL }) => {
  await freshUser(page, baseURL!, { free: 0 });
  await sendPrompt(page, 'Write a short plan for the migration');
  await expect(page.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'capacity_exhausted');
  await expect(page.getByTestId('countdown.reason')).toContainText('All execution slots are busy');
});

test('failed: the catalog error and "Retry" sends the prompt again as a follow-up', async ({ page, baseURL }) => {
  await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Retry me');
  const first = await mainSession(task);
  await walk(first.session_id, ['vm_assigning', 'failed'], { reason: 'vm_start_failed' });
  const failed = page.getByTestId('countdown.failed');
  await expect(failed).toHaveAttribute('data-reason', 'vm_start_failed');
  await expect(page.getByTestId('countdown.step.vm-assigning')).toHaveAttribute('data-status', 'failed');
  const prompts = page.waitForRequest((request) => request.url().endsWith(`/v1/tasks/${task}/prompts`));
  await page.getByTestId('countdown.retry').click();
  await prompts;
  await expect(page.getByTestId('countdown.digits')).toBeVisible();
  let second = first;
  await expect.poll(async () => (second = await mainSession(task)).session_id).not.toBe(first.session_id);
  await walk(second.session_id, [...START_STEPS, 'working'], { intervalMs: 50 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
});
