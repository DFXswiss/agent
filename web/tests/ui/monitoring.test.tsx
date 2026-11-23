// U-WEB-ui-monitoring (screen): the tree device → VM → session with names, units, freshness and the capacity warning,
// live frames, the overview reload for unknown entities, load and error states, and the chart panel (range switch,
// data table, failed and loading series) – in jsdom against the org world, phone layout (sparklines; the uPlot chart
// itself is covered in the browser).
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { putEvents } from '../../src/core/store/slices/events';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { frames, RELOAD_MS } from '../../src/core/store/slices/monitoring';
import { HubError } from '../../src/core/net/signedFetch';
import { MonitoringScreen, POLL_MS } from '../../src/ui/screens/monitoring/MonitoringScreen';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { CAPACITY, iso, sample, usage } from '../core/monitoring/fixtures';
import { mount, names } from './org-world';

const DEV = '01930000-0000-7000-8000-00000000d001';
const DEV2 = '01930000-0000-7000-8000-00000000d002';
const GONE = '01930000-0000-7000-8000-00000000d0ff';
const TASK = '01930000-0000-7000-8000-00000000c001';
const S1 = '01930000-0000-7000-8000-00000000e001';
const S2 = '01930000-0000-7000-8000-00000000e002';
const S3 = '01930000-0000-7000-8000-00000000e003';
const GIB = 1024 ** 3;

function narrow() {
  window.matchMedia = vi.fn(() => ({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as never;
}

beforeEach(narrow);
afterEach(() => {
  vi.useRealTimers();
  delete (window as { matchMedia?: unknown }).matchMedia;
});

function overview(w: OrgWorld, now: number) {
  const wg = w.world.defaultWg;
  return {
    devices: [
      {
        device_id: DEV,
        online: true,
        capacity: { ...CAPACITY, vm_slots_free: 0 },
        capacity_low: true,
        latest: sample(now - 5000, {
          reachable: false,
          uptime_seconds: null,
          free_vm_slots: null,
          free_mem_bytes: null,
        }),
        stale: false,
      },
      { device_id: DEV2, online: false, capacity: CAPACITY, capacity_low: false, latest: null, stale: true },
    ],
    vms: [
      { vm_id: 'vm-00000001', device_id: DEV, wg_id: wg, state: 'running', latest: sample(now - 120_000), stale: true },
      { vm_id: 'vm-00000002', device_id: DEV, wg_id: null, state: 'pool', latest: null },
      { vm_id: 'vm-00000003', device_id: GONE, wg_id: 'unnamed-wg', state: 'stopped', latest: null },
    ],
    sessions: [
      {
        session_id: S1,
        task_id: TASK,
        wg_id: wg,
        state: 'working',
        latest: sample(now, { disk_used_bytes: 3 * GIB }),
        usage: usage({ context_window_tokens: 0 }),
        stale: false,
      },
      { session_id: S2, task_id: TASK, wg_id: wg, state: 'idle', latest: null, usage: null, stale: true },
      { session_id: S3, task_id: TASK, wg_id: 'other', state: 'idle', latest: null, usage: usage() },
    ],
  };
}

async function setup(options: { body?: (w: OrgWorld) => unknown; path?: (w: OrgWorld) => string } = {}) {
  const w = await orgWorld();
  const now = Date.now();
  w.hub.now = now;
  names(w, {}, { [w.world.defaultWg]: 'Default' });
  w.client.on('GET /v1/monitoring/overview', () => (options.body ?? ((world) => overview(world, now)))(w));
  w.client.on(`GET /v1/sessions/${S1}`, () => ({ session: { session_id: S1, vm_id: 'vm-00000001' } }));
  w.client.on(`GET /v1/sessions/${S2}`, () => ({ session: { session_id: S2, vm_id: null } }));
  const release = vi.fn();
  const watchMonitoring = vi.fn(() => release);
  const at = [{ path: '/o/:org/monitoring', element: <MonitoringScreen /> }];
  setActiveOrg(w.store, w.org);
  const mounted = mount(w, at, options.path?.(w) ?? `/o/${w.org}/monitoring`, { watchMonitoring });
  return { w, now, release, watchMonitoring, ...mounted };
}

const device = (id: string) => screen.getAllByTestId('monitoring.device').find((node) => node.dataset.device === id)!;
const vm = (id: string) => screen.getAllByTestId('monitoring.vm').find((node) => node.dataset.vm === id)!;
const session = (id: string) =>
  screen.getAllByTestId('monitoring.session').find((node) => node.dataset.session === id)!;

describe('monitoring screen', () => {
  it('shows the tree with units, freshness, names and the capacity warning', async () => {
    // the clock stands still: the age shown must not depend on how long the setup takes on a loaded device
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    const t = await setup();
    expect(t.watchMonitoring).toHaveBeenCalledWith(t.w.org);
    await screen.findAllByTestId('monitoring.device');
    expect(screen.getByTestId('monitoring.capacity-warning')).toHaveTextContent('Capacity low on 1 device(s)');
    const first = within(device(DEV));
    expect(first.getByTestId('monitoring.device.name')).toHaveTextContent(`Device …${DEV.slice(-6)}`);
    expect(first.getByTestId('monitoring.device.reachable')).toHaveTextContent('Unreachable');
    expect(first.getByTestId('monitoring.device.uptime')).toHaveTextContent('–');
    expect(first.getByTestId('monitoring.device.capacity')).toHaveTextContent('0 of 8 VM slots, – memory, 1 warm VMs');
    expect(first.getByTestId('monitoring.device.network')).toHaveTextContent('↓ 1,000 B ↑ 500 B total');
    expect(first.getByTestId('monitoring.device.age')).toHaveTextContent(/Updated \d s ago/);
    const second = within(device(DEV2));
    expect(device(DEV2)).toHaveAttribute('data-stale', 'true');
    expect(second.getByTestId('monitoring.device.online')).toHaveTextContent('Offline');
    expect(second.getByTestId('monitoring.device.age')).toHaveTextContent('No data');
    expect(second.queryByTestId('monitoring.device.state')).toBeNull();
    expect(second.queryByTestId('monitoring.device.reachable')).toBeNull();
    expect(second.getByTestId('monitoring.device.cpu')).toHaveTextContent('–');
    fireEvent.click(second.getByTestId('monitoring.device.vms-toggle'));
    expect(second.getByTestId('monitoring.device.no-vms')).toBeInTheDocument();
    fireEvent.click(second.getByTestId('monitoring.device.vms-toggle'));
    expect(second.queryByTestId('monitoring.device.no-vms')).toBeNull();

    // VMs of the first device; the session S1 is linked to vm-1 by its row, S2's row names no VM
    fireEvent.click(first.getByTestId('monitoring.device.vms-toggle'));
    const vm1 = within(vm('vm-00000001'));
    expect(vm('vm-00000001')).toHaveAttribute('data-stale', 'true');
    expect(vm1.getByTestId('monitoring.vm.wg')).toHaveTextContent('Default');
    expect(vm1.getByTestId('monitoring.vm.age')).toHaveTextContent('Stale – last value 2 min ago');
    expect(within(vm('vm-00000002')).queryByTestId('monitoring.vm.wg')).toBeNull();
    expect(within(vm('vm-00000002')).getByTestId('monitoring.vm.uptime')).toHaveTextContent('–');
    await waitFor(() => expect(t.w.store.getState().monitoring.links).toMatchObject({ [S1]: 'vm-00000001' }));
    fireEvent.click(vm1.getByTestId('monitoring.vm.sessions-toggle'));
    const s1 = within(session(S1));
    expect(s1.getByTestId('monitoring.session.disk')).toHaveTextContent('3.0 GiB / 100 GiB (3%)');
    expect(s1.getByTestId('monitoring.session.tokens')).toHaveTextContent('1,200 in / 300 out');
    expect(s1.getByTestId('monitoring.session.cost')).toHaveTextContent('$0.0123');
    expect(s1.getByTestId('monitoring.session.context')).toHaveTextContent('–');
    expect(s1.getByTestId('monitoring.session.task')).toHaveTextContent(`Task …${TASK.slice(-6)}`);
    fireEvent.click(within(vm('vm-00000002')).getByTestId('monitoring.vm.sessions-toggle'));
    expect(within(vm('vm-00000002')).getByTestId('monitoring.vm.no-sessions')).toBeInTheDocument();

    // not linked: the VM of an unlisted device, S2 (no VM), S3 (no VM of its workgroup)
    const other = within(screen.getByTestId('monitoring.other'));
    expect(other.getByText('vm-00000003')).toBeInTheDocument();
    expect(within(vm('vm-00000003')).getByTestId('monitoring.vm.wg')).toHaveTextContent('Workgroup');
    expect(within(session(S2)).getByTestId('monitoring.session.tokens')).toHaveTextContent('–');
    expect(within(session(S2)).getByTestId('monitoring.session.cost')).toHaveTextContent('–');
    expect(within(session(S3)).getByTestId('monitoring.session.context')).toHaveTextContent('25%');
  });

  it('shows the verified task title and live rates', async () => {
    const t = await setup();
    putEvents(t.w.store, t.w.world.defaultWg, [
      {
        org: t.w.org,
        scopeId: t.w.world.defaultWg,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'task.meta', id: 'title-1', task: TASK },
        body: { title: 'Build it' },
      },
    ]);
    await screen.findAllByTestId('monitoring.device');
    act(() =>
      frames.metrics(t.w.store, {
        samples: [
          {
            entity_kind: 'device',
            entity_id: DEV,
            sample: sample(t.now + 5000, { net_rx_bytes: 11_000, net_tx_bytes: 5500, reachable: true }),
          },
        ],
      }),
    );
    const first = within(device(DEV));
    expect(first.getByTestId('monitoring.device.network')).toHaveTextContent('↓ 1,000 B/s ↑ 500 B/s');
    expect(first.getByTestId('monitoring.device.reachable')).toHaveTextContent('Reachable');
    fireEvent.click(first.getByTestId('monitoring.device.vms-toggle'));
    await waitFor(() => expect(t.w.store.getState().monitoring.links[S1]).toBe('vm-00000001'));
    fireEvent.click(within(vm('vm-00000001')).getByTestId('monitoring.vm.sessions-toggle'));
    expect(within(session(S1)).getByTestId('monitoring.session.task')).toHaveTextContent('Build it');
  });

  it('reads the overview again for unknown entities and on the poll', async () => {
    const intervals = vi.spyOn(globalThis, 'setInterval');
    const t = await setup();
    await screen.findAllByTestId('monitoring.device');
    const calls = () => t.w.client.of('GET /v1/monitoring/overview').length;
    expect(calls()).toBe(1);
    // the reload waits for RELOAD_MS after the last read
    t.w.hub.now += RELOAD_MS - 50;
    act(() =>
      frames.metrics(t.w.store, {
        samples: [{ entity_kind: 'vm', entity_id: 'vm-0000000f', sample: sample(t.now) }],
      }),
    );
    await waitFor(() => expect(calls()).toBe(2));
    const poll = intervals.mock.calls.find((call) => call[1] === POLL_MS)![0] as () => void;
    await act(async () => poll());
    await waitFor(() => expect(calls()).toBe(3));
  });

  it('shows the load, error and empty states and releases the live view', async () => {
    let answer: () => unknown = () => new Promise(() => undefined);
    const t = await setup({ body: () => answer() });
    expect(screen.getByTestId('monitoring.loading')).toBeInTheDocument();
    answer = () => {
      throw new HubError('not_found', 404);
    };
    await act(async () => {
      t.router.navigate(`/o/${t.w.org}/elsewhere`);
    });
    expect(t.release).toHaveBeenCalled();
    await act(async () => {
      t.router.navigate(`/o/${t.w.org}/monitoring`);
    });
    expect(await screen.findByTestId('monitoring.error')).toHaveTextContent('could not be loaded (not_found)');
    answer = () => ({ devices: [], vms: [], sessions: [] });
    await act(async () => {
      t.router.navigate('/elsewhere');
    });
    await act(async () => {
      t.router.navigate(`/o/${t.w.org}/monitoring`);
    });
    expect(await screen.findByTestId('monitoring.empty')).toBeInTheDocument();
  });

  it('lists a session without a VM even when every VM has its device', async () => {
    await setup({
      body: (w) => {
        const body = overview(w, Date.now());
        return { ...body, vms: body.vms.slice(0, 1), sessions: body.sessions.slice(2) };
      },
    });
    const other = within(await screen.findByTestId('monitoring.other'));
    expect(other.getAllByTestId('monitoring.session')).toHaveLength(1);
    expect(other.queryByTestId('monitoring.vm')).toBeNull();
  });

  it('works before the org log is verified', async () => {
    await setup({ path: () => '/o/unknown-org/monitoring' });
    expect(await screen.findAllByTestId('monitoring.device')).toHaveLength(2);
  });
});

describe('chart panel', () => {
  it('switches ranges, shows sparklines and the data table, failed and loading series', async () => {
    const t = await setup();
    let failMemory = true;
    t.w.client.on('GET /v1/monitoring/series', (_body, target) => {
      const metric = new URL(target, 'https://hub.example').searchParams.get('metric');
      if (metric === 'mem_used_bytes' && failMemory) throw new HubError('request_invalid', 400);
      if (metric === 'disk_used_bytes') return new Promise(() => undefined);
      if (metric === 'net_rx_bytes') return { points: [] };
      return {
        points: [
          [iso(t.now - 15_000), 1.5],
          [iso(t.now), null],
        ],
      };
    });
    await screen.findAllByTestId('monitoring.device');
    const first = within(device(DEV));
    fireEvent.click(first.getAllByTestId('monitoring.device.charts-toggle')[0]!);
    const panel = within(await first.findByTestId('monitoring.charts'));
    expect(panel.getByTestId('monitoring.range.source')).toHaveTextContent('Raw samples, one point per 15 s');
    const cpu = await waitFor(() => panel.getAllByTestId('monitoring.chart').find((n) => n.dataset.chart === 'cpu')!);
    expect(within(cpu).getByTestId('monitoring.chart.sparkline')).toHaveTextContent('CPU: 1.50 cores');
    fireEvent.click(within(cpu).getByTestId('monitoring.chart.table-toggle'));
    const rows = within(cpu).getAllByTestId('monitoring.chart.table.row');
    expect(rows[0]).toHaveTextContent('–');
    expect(rows[1]).toHaveTextContent('1.50 cores');
    expect(panel.getByTestId('monitoring.chart.error')).toHaveTextContent(
      'Memory: the series could not be loaded (request_invalid).',
    );
    expect(panel.getByText('Loading chart…')).toBeInTheDocument();
    const network = panel.getAllByTestId('monitoring.chart').find((n) => n.dataset.chart === 'network')!;
    expect(within(network).getByTestId('monitoring.chart.empty')).toBeInTheDocument();
    failMemory = false;
    // arrow keys move the choice (wrapping) and the focus; other keys do nothing
    const group = panel.getByRole('radiogroup');
    fireEvent.keyDown(group, { key: 'Enter' });
    expect(panel.getByTestId('monitoring.range.1h')).toHaveAttribute('aria-checked', 'true');
    expect(panel.getByTestId('monitoring.range.1h')).toHaveAttribute('tabindex', '0');
    fireEvent.keyDown(group, { key: 'ArrowLeft' });
    fireEvent.keyDown(group, { key: 'ArrowUp' });
    expect(panel.getByTestId('monitoring.range.90d')).toHaveAttribute('aria-checked', 'true');
    expect(document.activeElement).toBe(panel.getByTestId('monitoring.range.90d'));
    fireEvent.keyDown(group, { key: 'ArrowRight' });
    fireEvent.keyDown(group, { key: 'ArrowDown' });
    expect(panel.getByTestId('monitoring.range.1h')).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(panel.getByTestId('monitoring.range.30d'));
    expect(panel.getByTestId('monitoring.range.30d')).toHaveAttribute('aria-checked', 'true');
    expect(panel.getByTestId('monitoring.range.source')).toHaveTextContent('5-minute averages, one point per 2 h');
    await waitFor(() => expect(panel.queryByTestId('monitoring.chart.error')).toBeNull());
    const steps = t.w.client
      .of('GET /v1/monitoring/series')
      .map((call) => new URL(call.target, 'https://hub.example').searchParams.get('step'));
    expect(new Set(steps)).toEqual(new Set(['15', '21600', '7200']));
    fireEvent.click(first.getAllByTestId('monitoring.device.charts-toggle')[0]!);
    expect(first.queryByTestId('monitoring.charts')).toBeNull();
    expect(Object.keys(t.w.store.getState().monitoring.series).filter((key) => key.includes(' 15'))).toEqual([]);
  });

  it('names the disk chart of a session "Workspace"', async () => {
    const t = await setup();
    t.w.client.on('GET /v1/monitoring/series', () => ({ points: [[iso(t.now), 3 * GIB]] }));
    await screen.findAllByTestId('monitoring.device');
    const s3 = within(session(S3));
    fireEvent.click(s3.getByTestId('monitoring.session.charts-toggle'));
    const disk = await waitFor(() => s3.getAllByTestId('monitoring.chart').find((n) => n.dataset.chart === 'disk')!);
    expect(within(disk).getByRole('heading')).toHaveTextContent('Workspace');
    expect(within(disk).getByTestId('monitoring.chart.sparkline')).toHaveTextContent('Used: 3.0 GiB');
  });
});
