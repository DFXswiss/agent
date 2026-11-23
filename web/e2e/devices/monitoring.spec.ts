// The monitoring screen (06-web.md section 9.1, 08-monitoring.md section 6) against the real hub and the device double:
// samples, VMs and usage are pushed through the hub's outbox as the device (scripts/hub_test_metrics.py), rollups seeded
// for the long ranges. Device → VM → session with units and live `metrics` frames (requested only while the screen is
// open), stale values greyed with their age, charts with legend, tooltip, range switch and data table (uPlot under the
// hub CSP: the fixture fails on any violation), the capacity-low badge, the rights of another org on a shared platform
// device, and the phone layout with sparklines.
import { type Page, type Request } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, newDevice, walk } from '../helpers/director.ts';
import { seedRollups, seedSamples, seedUsage, seedVm } from '../helpers/monitoring.ts';
import { addAuthenticator } from '../helpers/authenticator.ts';
import { onboard } from '../helpers/onboard.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

const GIB = 1024 ** 3;
const device = (page: Page, id: string) => page.locator(`[data-testid="monitoring.device"][data-device="${id}"]`);
const vm = (page: Page, id: string) => page.locator(`[data-testid="monitoring.vm"][data-vm="${id}"]`);
const session = (page: Page, id: string) => page.locator(`[data-testid="monitoring.session"][data-session="${id}"]`);

/** A user whose task runs on a fresh platform device, with a VM and samples of all three levels. */
async function running(page: Page, origin: string, prompt: string) {
  const user = await freshUser(page, origin, { poolReady: 2 });
  const task = await sendPrompt(page, prompt);
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  const vmId = await seedVm(user.device, main.session_id);
  return { ...user, task, session: main.session_id, vm: vmId };
}

/** Where the monitoring screen's stream requests went (`monitoring=1` or not). */
function streams(page: Page): string[] {
  const urls: string[] = [];
  page.on('request', (request: Request) => {
    if (request.url().includes('/v1/stream')) urls.push(request.url());
  });
  return urls;
}

test('shows device, VM and session with units, live values, charts and the capacity badge @R-7.1 @R-7.2', async ({
  page,
  baseURL,
}) => {
  const urls = streams(page);
  const user = await running(page, baseURL!, 'Watch the load');
  await seedSamples(user.device, [
    // a dense history of the last ten minutes for the charts (raw samples every 15 s) and the newest values
    ...Array.from({ length: 40 }, (_, index) => ({
      entity_kind: 'device' as const,
      entity_id: user.device,
      age_s: 15 * (index + 1),
      cpu_ratio: [1, 1.25, 1.75, 2][index % 4],
      net_rx_bytes: 4_000_000 - 75_000 * (index + 1),
      net_tx_bytes: 400_000 - 7_500 * (index + 1),
    })),
    {
      entity_kind: 'device',
      entity_id: user.device,
      cpu_ratio: 1.5,
      mem_used_bytes: 24 * GIB,
      mem_limit_bytes: 64 * GIB,
      disk_used_bytes: 200 * GIB,
      disk_limit_bytes: 1000 * GIB,
      uptime_seconds: 3 * 86_400 + 4 * 3600,
      free_vm_slots: 6,
      free_mem_bytes: 40 * GIB,
      net_rx_bytes: 4_000_000,
      net_tx_bytes: 400_000,
    },
    // the VM's newest value is two minutes old: stale
    { entity_kind: 'vm', entity_id: user.vm, age_s: 120, cpu_ratio: 0.75, uptime_seconds: 7500 },
    { entity_kind: 'session', entity_id: user.session, cpu_ratio: 0.25, disk_used_bytes: 3 * GIB },
  ]);
  await seedUsage(user.device, user.session);
  await seedRollups(user.device, [{ entity_kind: 'device', entity_id: user.device, age_h: 72, cpu_ratio_avg: 3 }]);

  await page.getByTestId('shell.nav.monitoring').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/monitoring$`));
  const item = device(page, user.device);
  await expect(item.getByTestId('monitoring.device.online')).toHaveAttribute('data-online', 'true');
  await expect(item.getByTestId('monitoring.device.cpu')).toContainText('1.50 cores');
  await expect(item.getByTestId('monitoring.device.memory')).toContainText('24 GiB / 64 GiB (37.5%)');
  await expect(item.getByTestId('monitoring.device.disk')).toContainText('200 GiB / 1,000 GiB (20%)');
  await expect(item.getByTestId('monitoring.device.uptime')).toContainText('3 d 4 h');
  await expect(item.getByTestId('monitoring.device.capacity')).toContainText('6 of 8 VM slots, 40 GiB memory');
  await expect(item.getByTestId('monitoring.device.reachable')).toHaveAttribute('data-reachable', 'true');
  await expect(item.getByTestId('monitoring.device.state')).toHaveText('ready');
  await expect(item).toHaveAttribute('data-stale', 'false');
  await expect(item.getByTestId('monitoring.device.capacity-low')).toHaveCount(0);
  await expect(item.getByTestId('monitoring.device.age')).toContainText(/Updated \d+ s ago/);
  // metrics frames only while the screen is open
  await expect.poll(() => urls.at(-1) ?? '').toContain('monitoring=1');

  // drill-down: the VM (stale, greyed with its age), then its session with usage
  await item.getByTestId('monitoring.device.vms-toggle').click();
  const vmItem = vm(page, user.vm);
  await expect(vmItem.getByTestId('monitoring.vm.state')).toHaveText('Running');
  await expect(vmItem.getByTestId('monitoring.vm.uptime')).toContainText('2 h 5 min');
  await expect(vmItem).toHaveAttribute('data-stale', 'true');
  await expect(vmItem.getByTestId('monitoring.vm.age')).toContainText(/Stale – last value 2 min/);
  await vmItem.getByTestId('monitoring.vm.sessions-toggle').click();
  const sessionItem = session(page, user.session);
  await expect(sessionItem.getByTestId('monitoring.session.state')).toHaveText('Working');
  await expect(sessionItem.getByTestId('monitoring.session.cpu')).toContainText('0.25 cores');
  await expect(sessionItem.getByTestId('monitoring.session.disk')).toContainText('3.0 GiB');
  await expect(sessionItem.getByTestId('monitoring.session.tokens')).toContainText('1,000 in / 200 out');
  await expect(sessionItem.getByTestId('monitoring.session.cost')).toContainText('$0.0125');
  await expect(sessionItem.getByTestId('monitoring.session.context')).toContainText('25%');
  await expect(sessionItem.getByTestId('monitoring.session.task')).toHaveAttribute(
    'href',
    `/o/${user.org}/t/${user.task}`,
  );

  // a new sample arrives live (the hub's frame job runs every 15 s)
  await seedSamples(user.device, [{ entity_kind: 'device', entity_id: user.device, cpu_ratio: 2.25 }]);
  await expect(item.getByTestId('monitoring.device.cpu')).toContainText('2.25 cores', { timeout: 40_000 });

  // charts: uPlot with legend and tooltip, the data table, the range switch to the rollups
  await item.getByTestId('monitoring.device.charts-toggle').click();
  const charts = item.getByTestId('monitoring.charts');
  const cpu = charts.locator('[data-testid="monitoring.chart"][data-chart="cpu"]');
  await expect(cpu.locator('canvas')).toBeVisible();
  await expect(cpu.locator('.u-legend')).toContainText('CPU');
  // the tooltip: an hour ago there is no value, the newest buckets have one; leaving the chart hides it
  const box = (await cpu.locator('.u-over').boundingBox())!;
  const tooltip = cpu.getByTestId('monitoring.chart.tooltip');
  await page.mouse.move(box.x + 3, box.y + box.height / 2);
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText(/\d{2}:\d{2}:\d{2}\.\d{3}\nCPU: –/);
  await page.mouse.move(box.x + box.width - 3, box.y + box.height / 2);
  await expect(tooltip).toContainText(/CPU: \d\.\d{2} cores/);
  await page.mouse.move(box.x + box.width / 2, box.y - 40);
  await expect(tooltip).toBeHidden();
  await cpu.getByTestId('monitoring.chart.table-toggle').click();
  const table = cpu.getByTestId('monitoring.chart.table');
  await expect(table.getByRole('columnheader', { name: 'Time' })).toBeVisible();
  await expect(table).toContainText('1.25 cores');
  await expect(charts.locator('[data-chart="network"]')).toContainText('Received');
  await expect(charts.getByTestId('monitoring.range.source')).toHaveText('Raw samples, one point per 15 s');
  const weekly = page.waitForRequest(
    (request) => request.url().includes('/v1/monitoring/series') && request.url().includes('step=1800'),
  );
  await charts.getByTestId('monitoring.range.7d').click();
  await weekly;
  await expect(charts.getByTestId('monitoring.range.7d')).toHaveAttribute('aria-checked', 'true');
  await expect(charts.getByTestId('monitoring.range.source')).toHaveText('5-minute averages, one point per 30 min');
  await cpu.getByTestId('monitoring.chart.table-toggle').click();
  await expect(cpu.getByTestId('monitoring.chart.table')).toContainText('3.00 cores');
  await expect(charts.locator('[data-chart="disk"]').getByTestId('monitoring.chart.empty')).toHaveCount(0);
  await charts.getByTestId('monitoring.range.90d').click();
  await expect(charts.getByTestId('monitoring.range.90d')).toHaveAttribute('aria-checked', 'true');
  await item.getByTestId('monitoring.device.charts-toggle').click();
  await expect(item.getByTestId('monitoring.charts')).toHaveCount(0);

  // a new device takes the slots away from this one: offline, capacity low (< 10 % free)
  await newDevice();
  await page.getByTestId('shell.nav.home').click();
  await expect.poll(() => urls.at(-1) ?? '').not.toContain('monitoring=1');
  await page.getByTestId('shell.nav.monitoring').click();
  await expect(item.getByTestId('monitoring.device.capacity-low')).toBeVisible();
  await expect(item.getByTestId('monitoring.device.online')).toHaveAttribute('data-online', 'false');
  await expect(page.getByTestId('monitoring.capacity-warning')).toContainText('1 device');
});

test('a member of another org sees the shared device but none of its VMs and sessions @R-7.6', async ({
  page,
  baseURL,
  newContext,
}) => {
  const ada = await running(page, baseURL!, 'Ada works here');
  await seedSamples(ada.device, [
    { entity_kind: 'device', entity_id: ada.device },
    { entity_kind: 'vm', entity_id: ada.vm },
    { entity_kind: 'session', entity_id: ada.session },
  ]);
  // Bob's private org gets the same platform device (still the first candidate) for his own task
  const other = await (await newContext()).newPage();
  await addAuthenticator(other);
  const bob = await onboard(other, { origin: baseURL!, name: 'Bob' });
  const bobTask = await sendPrompt(other, 'Bob works too');
  const bobSession = await mainSession(bobTask);
  expect(bobSession.device_id).toBe(ada.device);
  await walk(bobSession.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  const bobVm = await seedVm(ada.device, bobSession.session_id);
  await seedSamples(ada.device, [{ entity_kind: 'vm', entity_id: bobVm }]);

  await other.getByTestId('shell.nav.monitoring').click();
  await expect(other).toHaveURL(new RegExp(`/o/${bob.org}/monitoring$`));
  const shared = device(other, ada.device);
  await expect(shared.getByTestId('monitoring.device.cpu')).toContainText('0.50 cores');
  await shared.getByTestId('monitoring.device.vms-toggle').click();
  await expect(other.getByTestId('monitoring.vm')).toHaveCount(1);
  await expect(vm(other, bobVm)).toBeVisible();
  await expect(vm(other, ada.vm)).toHaveCount(0);
  await vm(other, bobVm).getByTestId('monitoring.vm.sessions-toggle').click();
  await expect(other.getByTestId('monitoring.session')).toHaveCount(1);
  await expect(session(other, bobSession.session_id)).toBeVisible();
  await expect(session(other, ada.session)).toHaveCount(0);
  // the hub refuses Ada's series to Bob (not found, never revealed)
  const api = await PageUser.open(other, bob.recoveryCode);
  const now = Date.now();
  const query = new URLSearchParams({
    entity_kind: 'vm',
    entity_id: ada.vm,
    metric: 'cpu_ratio',
    from: new Date(now - 900_000).toISOString(),
    to: new Date(now).toISOString(),
    step: '15',
  });
  await expect(api.request('GET', `/v1/monitoring/series?${query.toString()}`)).rejects.toThrow(/404/);
});

test.describe('on a phone', () => {
  test.use({ viewport: { width: 412, height: 839 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2.625 });

  test('shows one column with sparklines instead of charts', async ({ page, baseURL }) => {
    const user = await running(page, baseURL!, 'Small screen');
    await seedSamples(user.device, [
      { entity_kind: 'device', entity_id: user.device, age_s: 120, cpu_ratio: 1 },
      { entity_kind: 'device', entity_id: user.device, cpu_ratio: 2 },
    ]);
    await page.goto(`/o/${user.org}/monitoring`);
    await page.getByTestId('login.submit').click();
    const item = device(page, user.device);
    await expect(item.getByTestId('monitoring.device.cpu')).toContainText('2.00 cores');
    await item.getByTestId('monitoring.device.charts-toggle').click();
    const cpu = item.locator('[data-testid="monitoring.chart"][data-chart="cpu"]');
    await expect(cpu.getByTestId('monitoring.chart.sparkline')).toContainText('CPU: 2.00 cores');
    await expect(item.locator('canvas')).toHaveCount(0);
    await expect(item.locator('[data-chart="disk"]').getByTestId('monitoring.chart.sparkline')).toBeVisible();
    await cpu.getByTestId('monitoring.chart.table-toggle').click();
    await expect(cpu.getByTestId('monitoring.chart.table')).toContainText('2.00 cores');
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(width).toBeLessThanOrEqual(412);
  });
});
