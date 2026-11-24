// E2E-17 (07-testing.md section 2.1; 08-monitoring.md sections 1-7; ADR-018): monitoring device → VM → session. A
// session runs on the platform device; the runner writes its Prometheus textfile, which the local monitoring stack of
// the agent checkout (Prometheus, node_exporter and Grafana in the versions of the existing stack, a Lima VM with the
// run prefix, hermetic) scrapes. The monitoring screen shows all three levels with CPU, RAM, disk, network, uptime,
// reachability, state, free capacity and history, with the same values as the hub API; Prometheus has the series of
// device, VM and session with the documented labels; the stack's checks pass (promtool check metrics/rules, promtool
// test rules, Grafana dashboards with data). After the capacity is lowered the `capacity_low` badge shows, Grafana's
// capacity rule turns pending/firing and `promtool test rules` with the live value fires the alert. Runs on T1.
// The parent session asks the platform for a child session (subagent.request, as in E2E-24): the child appears on
// every level with its own samples (R-13.4) - in the hub API, under its VM on the screen and in Prometheus.
import { readFileSync } from 'node:fs';
import type { Locator, Page } from '@playwright/test';
import { formatBytes, formatCores, formatDuration, formatPercent, share } from '../../../web/src/core/monitoring/units';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { MD, navTo } from '../fixtures/nav.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-17-monitoring.json', import.meta.url), 'utf8'),
);
const PROMPT = 'E2E17-MONITOR kz17-prompt-k3r7t1: write the load report';
const NAME = 'Ada kz17-name-y8b2c5';
const ANSWER = 'The load report is written kz17-answer-w9d4m1.';
const CANARIES = [
  'kz17-prompt-k3r7t1',
  'kz17-name-y8b2c5',
  'kz17-answer-w9d4m1',
  'kz17-file-f2g6s8',
  'kz17-child-q4n7z2',
  'kz17-purpose-e3u5',
];
const LOCALE = 'en';
/** Labels the runner's series may carry (08-monitoring.md section 3) plus those of the scrape (server, job, instance). */
const DOCUMENTED_LABELS = new Set([
  '__name__',
  'server',
  'job',
  'instance',
  'vm',
  'wg',
  'session',
  'state',
  'direction',
  'window',
]);

interface Sample {
  readonly cpu_ratio: number | null;
  readonly mem_used_bytes: number | null;
  readonly mem_limit_bytes: number | null;
  readonly disk_used_bytes: number | null;
  readonly disk_limit_bytes: number | null;
  readonly uptime_seconds: number | null;
  readonly reachable: boolean;
  readonly state: string;
  readonly free_vm_slots: number | null;
}

interface Overview {
  readonly devices: readonly {
    device_id: string;
    online: boolean;
    capacity_low: boolean;
    capacity: { vm_slots_total: number; vm_slots_free: number };
    latest: Sample | null;
  }[];
  readonly vms: readonly {
    vm_id: string;
    device_id: string;
    wg_id: string | null;
    state: string;
    latest: Sample | null;
  }[];
  readonly sessions: readonly {
    session_id: string;
    task_id: string;
    wg_id: string;
    state: string;
    latest: Sample | null;
  }[];
}

interface Series {
  readonly status: string;
  readonly data: readonly Record<string, string>[];
}

const prometheus = <T>(orchestrator: Orchestrator, path: string, query: Record<string, string>) =>
  orchestrator.call<T>('POST', '/monitoring/stack/prometheus', { path, query });

/** The text the screen shows for a used/limit pair (MonitoringScreen `pair`). */
function pair(used: number | null, limit: number | null): string {
  const text = `${used === null ? '–' : formatBytes(used, LOCALE)} / ${limit === null ? '–' : formatBytes(limit, LOCALE)}`;
  const part = share(used, limit);
  return part === null ? text : `${text} (${formatPercent(part, LOCALE)})`;
}

/** The text of a CPU value (`monitoring.unit.cores`). */
const cores = (ratio: number): string => `${formatCores(ratio, LOCALE)} cores`;

/**
 * The screen and the hub API agree: read the API, then the screen, until a pair matches (values move every 15 s;
 * the screen gets them from `metrics` frames and its own reads of the same API).
 */
async function agrees(item: Locator, testId: string, expected: () => Promise<string>): Promise<void> {
  await expect
    .poll(
      async () => {
        const want = await expected();
        const shown = (await item.getByTestId(testId).textContent()) ?? '';
        return shown.includes(want) ? 'same' : `${shown} ≠ ${want}`;
      },
      { timeout: 90_000, intervals: [2000] },
    )
    .toBe('same');
}

/** Opens the charts of `item` and checks that the CPU history has points (raw samples of the last 15 minutes). */
async function history(page: Page, item: Locator, kind: string): Promise<void> {
  await item.getByTestId(`monitoring.${kind}.charts-toggle`).click();
  const cpu = item.getByTestId('monitoring.charts').locator('[data-testid="monitoring.chart"][data-chart="cpu"]');
  // the uPlot chart from md on, a sparkline on phones (06-web.md sections 9.1, 12.1)
  const drawn =
    page.viewportSize()!.width >= MD ? cpu.locator('canvas') : cpu.getByTestId('monitoring.chart.sparkline');
  await expect(drawn).toBeVisible({ timeout: 30_000 });
  await cpu.getByTestId('monitoring.chart.table-toggle').click();
  await expect
    .poll(async () => cpu.getByTestId('monitoring.chart.table').locator('tbody tr').count(), { timeout: 60_000 })
    .toBeGreaterThan(0);
  await item.getByTestId(`monitoring.${kind}.charts-toggle`).click();
}

test.describe.configure({ mode: 'serial' });

// E2E-17 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-17 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-17 @R-7.1 @R-7.2 @R-7.3 @R-7.4 @R-7.5 @R-7.6 @R-7.7 @R-13.4 @R-15.3 @R-P.5 @needs-vm monitoring shows device, VM, session and child session in the web, at the hub and in the local Prometheus/Grafana stack; low capacity warns', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', scenario);
  await orchestrator.resetDouble('model');
  await platformDevice(orchestrator);
  try {
    // The stack first: the runner restarts once to write its textfile, then the VM is created and provisioned.
    const stack = await orchestrator.call<{ vm: string; server: string; textfile_dir: string }>(
      'POST',
      '/monitoring/stack/up',
      {},
    );
    expect(stack.vm).toMatch(/-e2e-monitoring$/);
    await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, NAME);
    const org = onboarded.org;
    const task = await sendPrompt(page, PROMPT);
    await expectAnswerInChat(page, ANSWER);
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const [session] = await mainSessions(orchestrator, task);
    const sessionId = session!.id;
    const device = session!.device_id!;
    const [taskRow] = await orchestrator.hubQuery<{ wg_id: string }>(`SELECT wg_id FROM hub.task WHERE id = '${task}'`);
    const wg = taskRow!.wg_id;
    const [child] = await orchestrator.hubQuery<{ id: string; device_id: string }>(
      `SELECT id, device_id FROM hub.session WHERE task_id = '${uuid(task)}' AND parent_session_id = '${uuid(sessionId)}'`,
    );
    expect(child, 'the child session of subagent.request').toBeDefined();
    const childId = child!.id;
    expect(child!.device_id).toBe(device);

    // Hub side: samples of all three levels arrive through the runner's outbox (every 15 s).
    const api = await PageUser.open(page, onboarded.recoveryCode);
    const overview = async () => (await api.request('GET', `/v1/monitoring/overview?org=${org}`)) as Overview;
    await expect
      .poll(
        async () => {
          const now = await overview();
          const vm = now.vms.find((item) => item.wg_id === wg);
          const own = now.sessions.find((item) => item.session_id === sessionId);
          const theirs = now.sessions.find((item) => item.session_id === childId);
          return [
            now.devices.some((item) => item.device_id === device && item.latest !== null),
            vm?.latest != null,
            own?.latest != null,
            theirs?.latest != null,
          ];
        },
        { timeout: 120_000, intervals: [2000] },
      )
      .toEqual([true, true, true, true]);
    const first = await overview();
    const vmId = first.vms.find((item) => item.wg_id === wg)!.vm_id;
    const deviceOf = async () => (await overview()).devices.find((item) => item.device_id === device)!;
    const vmOf = async () => (await overview()).vms.find((item) => item.vm_id === vmId)!;
    const sessionOf = async () => (await overview()).sessions.find((item) => item.session_id === sessionId)!;
    const childOf = async () => (await overview()).sessions.find((item) => item.session_id === childId)!;

    // The monitoring screen: device → VM → session with every value, the same as the hub API.
    await navTo(page, 'monitoring');
    await expect(page).toHaveURL(new RegExp(`/o/${org}/monitoring$`));
    const deviceItem = page.locator(`[data-testid="monitoring.device"][data-device="${uuid(device)}"]`);
    await expect(deviceItem.getByTestId('monitoring.device.online')).toHaveAttribute('data-online', 'true');
    await expect(deviceItem.getByTestId('monitoring.device.reachable')).toHaveAttribute('data-reachable', 'true');
    // the badge follows the hub's flag (a small device may be low already before the capacity is lowered)
    const lowAtStart = (await deviceOf()).capacity_low;
    await expect(deviceItem.getByTestId('monitoring.device.capacity-low')).toHaveCount(lowAtStart ? 1 : 0);
    await agrees(deviceItem, 'monitoring.device.state', async () => (await deviceOf()).latest!.state);
    await agrees(deviceItem, 'monitoring.device.memory', async () => {
      const latest = (await deviceOf()).latest!;
      return pair(latest.mem_used_bytes, latest.mem_limit_bytes);
    });
    await agrees(deviceItem, 'monitoring.device.disk', async () => {
      const latest = (await deviceOf()).latest!;
      return pair(latest.disk_used_bytes, latest.disk_limit_bytes);
    });
    await agrees(deviceItem, 'monitoring.device.uptime', async () =>
      formatDuration((await deviceOf()).latest!.uptime_seconds!),
    );
    await agrees(deviceItem, 'monitoring.device.capacity', async () => {
      const item = await deviceOf();
      return `${item.latest!.free_vm_slots ?? item.capacity.vm_slots_free} of ${item.capacity.vm_slots_total} VM slots`;
    });
    await expect(deviceItem.getByTestId('monitoring.device.cpu')).toContainText(/\d\.\d{2} cores/);
    await expect(deviceItem.getByTestId('monitoring.device.network')).not.toContainText('–');
    await history(page, deviceItem, 'device');

    await deviceItem.getByTestId('monitoring.device.vms-toggle').click();
    const vmItem = page.locator(`[data-testid="monitoring.vm"][data-vm="${vmId}"]`);
    await expect(vmItem.getByTestId('monitoring.vm.state')).toHaveAttribute('data-state', (await vmOf()).state);
    await expect(vmItem.getByTestId('monitoring.vm.reachable')).toHaveAttribute('data-reachable', 'true');
    await expect(vmItem.getByTestId('monitoring.vm.cpu')).toContainText(/\d\.\d{2} cores/);
    await agrees(vmItem, 'monitoring.vm.memory', async () => {
      const latest = (await vmOf()).latest!;
      return pair(latest.mem_used_bytes, latest.mem_limit_bytes);
    });
    await agrees(vmItem, 'monitoring.vm.disk', async () => {
      const latest = (await vmOf()).latest!;
      return pair(latest.disk_used_bytes, latest.disk_limit_bytes);
    });
    await agrees(vmItem, 'monitoring.vm.uptime', async () => formatDuration((await vmOf()).latest!.uptime_seconds!));
    await expect(vmItem.getByTestId('monitoring.vm.network')).not.toContainText('–');
    await history(page, vmItem, 'vm');

    await vmItem.getByTestId('monitoring.vm.sessions-toggle').click();
    const sessionItem = page.locator(`[data-testid="monitoring.session"][data-session="${uuid(sessionId)}"]`);
    await expect(sessionItem.getByTestId('monitoring.session.state')).toBeVisible();
    // CPU is a delta of the cgroup counter: the first sample of a session has none yet.
    await expect
      .poll(async () => (await sessionOf()).latest?.cpu_ratio ?? null, { timeout: 60_000, intervals: [2000] })
      .not.toBeNull();
    await agrees(sessionItem, 'monitoring.session.cpu', async () => cores((await sessionOf()).latest!.cpu_ratio!));
    await agrees(sessionItem, 'monitoring.session.memory', async () => {
      const latest = (await sessionOf()).latest!;
      return pair(latest.mem_used_bytes, latest.mem_limit_bytes);
    });
    await agrees(sessionItem, 'monitoring.session.disk', async () => {
      const latest = (await sessionOf()).latest!;
      return pair(latest.disk_used_bytes, latest.disk_limit_bytes);
    });
    await expect(sessionItem.getByTestId('monitoring.session.task')).toHaveAttribute('href', `/o/${org}/t/${task}`);
    await history(page, sessionItem, 'session');
    // The child session: its own item under the same VM with its state, linked to the same task, and its own samples
    // of the time it worked (it ends after its answer; the hub keeps the history).
    const childItem = page.locator(`[data-testid="monitoring.session"][data-session="${uuid(childId)}"]`);
    await expect
      .poll(
        async () =>
          (await childItem.getByTestId('monitoring.session.state').getAttribute('data-state')) ===
          (await childOf()).state,
        { timeout: 90_000, intervals: [2000] },
      )
      .toBe(true);
    await expect(childItem.getByTestId('monitoring.session.task')).toHaveAttribute('href', `/o/${org}/t/${task}`);
    const [childSamples] = await orchestrator.hubQuery<{ n: number }>(
      `SELECT count(*)::int AS n FROM hub.metric_sample
        WHERE entity_kind = 'session' AND entity_id = '${uuid(childId)}' AND mem_used_bytes > 0`,
    );
    expect(childSamples!.n).toBeGreaterThan(0);
    await history(page, childItem, 'session');

    // Prometheus of the local stack: series of device, VM and session with the documented labels only.
    const series = async (match: string) =>
      (await prometheus<Series>(orchestrator, '/api/v1/series', { 'match[]': match })).data;
    await expect
      .poll(async () => (await series(`agent_runner_session_cpu_ratio{session="${sessionId}"}`)).length, {
        timeout: 120_000,
        intervals: [5000],
      })
      .toBeGreaterThan(0);
    const all = await series('{__name__=~"agent_runner_.+"}');
    for (const labels of all) {
      expect(labels.server).toBe(stack.server);
      for (const name of Object.keys(labels)) expect(DOCUMENTED_LABELS, `${labels.__name__}: ${name}`).toContain(name);
    }
    const names = new Set(all.map((labels) => labels.__name__));
    for (const name of [
      'agent_runner_device_up',
      'agent_runner_device_vm_slots',
      'agent_runner_device_vm_slots_free',
      'agent_runner_device_capacity_low',
      'agent_runner_vm_cpu_ratio',
      'agent_runner_vm_memory_bytes',
      'agent_runner_vm_disk_used_bytes',
      'agent_runner_vm_uptime_seconds',
      'agent_runner_vm_probe_ok',
      'agent_runner_session_cpu_ratio',
      'agent_runner_session_memory_bytes',
      'agent_runner_session_workspace_bytes',
    ])
      expect(names).toContain(name);
    const vmSeries = all.filter((labels) => labels.__name__ === 'agent_runner_vm_cpu_ratio' && labels.vm === vmId);
    // a pool VM has no workgroup yet (an empty label is no label): its series carry `wg` from the assignment on
    expect(vmSeries.filter((labels) => labels.wg !== undefined).map((labels) => labels.wg)).toEqual([wg]);
    const sessionSeries = all.filter(
      (labels) => labels.__name__ === 'agent_runner_session_cpu_ratio' && labels.session === sessionId,
    );
    expect(sessionSeries.map((labels) => labels.vm)).toEqual([vmId]);
    await expect
      .poll(async () => (await series(`agent_runner_session_memory_bytes{session="${childId}"}`)).map((l) => l.vm), {
        timeout: 60_000,
        intervals: [5000],
      })
      .toEqual([vmId]);

    // The stack's checks: textfile, rules, rule tests, Grafana provisioning, dashboards and their queries.
    const healthy = await orchestrator.call<{ ok: boolean; steps: string[]; output: string }>(
      'POST',
      '/monitoring/stack/check',
      { expect_active: [] },
    );
    expect(healthy.ok, healthy.output).toBe(true);
    expect(healthy.steps).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^promtool check metrics: ok/),
        'promtool check rules: ok',
        'promtool test rules: ok',
        'grafana provisioning: no errors',
        'prometheus and grafana APIs: ok',
      ]),
    );

    // Lower the capacity: no free slot is left. Badge in the web, capacity_low in Prometheus, the Grafana rule
    // pending or firing, and promtool's test of the alert rule with the live value fires.
    const [capacity] = await orchestrator.hubQuery<{ total: number; free: number }>(
      `SELECT (capacity->>'vm_slots_total')::int AS total, (capacity->>'vm_slots_free')::int AS free
         FROM hub.device WHERE id = '${uuid(device)}'`,
    );
    const occupied = capacity!.total - capacity!.free;
    expect(occupied).toBeGreaterThan(0);
    await orchestrator.call('POST', '/runners/capacity', { slots: occupied });
    await expect.poll(async () => (await deviceOf()).capacity_low, { timeout: 120_000, intervals: [2000] }).toBe(true);
    await navTo(page, 'home');
    await navTo(page, 'monitoring');
    await expect(deviceItem.getByTestId('monitoring.device.capacity-low')).toBeVisible({ timeout: 90_000 });
    await expect(page.getByTestId('monitoring.capacity-warning')).toBeVisible();
    await expect
      .poll(
        async () =>
          (
            await prometheus<{ data: { result: { value: [number, string] }[] } }>(orchestrator, '/api/v1/query', {
              query: `agent_runner_device_capacity_low{server="${stack.server}"}`,
            })
          ).data.result.map((item) => item.value[1]),
        { timeout: 120_000, intervals: [5000] },
      )
      .toEqual(['1']);
    const live = await orchestrator.call<{ ok: boolean; value: string; output: string }>(
      'POST',
      '/monitoring/stack/live-rule-test',
      { metric: 'agent_runner_device_capacity_low', rule: 'agent-runner-capacity-low', for_minutes: 10 },
    );
    expect(live.value).toBe('1');
    expect(live.ok, live.output).toBe(true);
    const low = await orchestrator.call<{ ok: boolean; output: string }>('POST', '/monitoring/stack/check', {
      expect_active: ['agent-runner-capacity-low'],
    });
    expect(low.ok, low.output).toBe(true);

    await expectNoPlaintext(orchestrator, CANARIES);
  } finally {
    await orchestrator.call('POST', '/runners/capacity', { slots: null }).catch(() => undefined);
    await orchestrator.call('POST', '/monitoring/stack/down', {}).catch(() => undefined);
  }
});
