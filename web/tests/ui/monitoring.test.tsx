// U-WEB-ui-monitoring (screen): the tree device → VM → session → work VM with names, units, freshness and the capacity warning,
// live frames, the overview reload for unknown entities, load and error states, and the chart panel (range switch,
// data table, failed and loading series) – in jsdom against the org world, phone layout (sparklines; the uPlot chart
// itself is covered in the browser).
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { putEvents } from '../../src/core/store/slices/events';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { frames, RELOAD_MS } from '../../src/core/store/slices/monitoring';
import { HubError } from '../../src/core/net/signedFetch';
import {
  MonitoringScreen,
  POLL_MS,
  selectTitles,
  TREE_MAX_ROWS,
  TREE_SIZE,
} from '../../src/ui/screens/monitoring/MonitoringScreen';
import { SECOND_MS } from '../../src/ui/screens/monitoring/clock';
import { nextStaleAt, sameRow, type TreeRow } from '../../src/ui/screens/monitoring/tree';
import type { MonitoredSession, Sampled } from '../../src/core/monitoring/overview';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { CAPACITY, iso, sample, usage } from '../core/monitoring/fixtures';
import { labelDevice } from './device-labels';
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
    expect(first.getByTestId('monitoring.device.name')).toHaveTextContent('Device without a name');
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
    // a drill-down adds rows after its node (one flat windowed list)
    fireEvent.click(second.getByTestId('monitoring.device.vms-toggle'));
    expect(second.getByTestId('monitoring.device.vms-toggle')).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('monitoring.device.no-vms')).toHaveAttribute('data-device', DEV2);
    expect(device(DEV2).nextElementSibling).toBe(screen.getByTestId('monitoring.device.no-vms'));
    fireEvent.click(second.getByTestId('monitoring.device.vms-toggle'));
    expect(screen.queryByTestId('monitoring.device.no-vms')).toBeNull();

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
    expect(s1.getByTestId('monitoring.session.task')).toHaveTextContent('Task without a readable title');
    expect(s1.getByTestId('monitoring.session.task').querySelector('[data-id]')).toHaveAttribute('data-id', TASK);
    expect(vm('vm-00000001').nextElementSibling).toBe(session(S1));
    fireEvent.click(within(vm('vm-00000002')).getByTestId('monitoring.vm.sessions-toggle'));
    expect(screen.getByTestId('monitoring.vm.no-sessions')).toHaveAttribute('data-vm', 'vm-00000002');
    // closing a VM drops its session rows (and their age cells) while the others count on
    fireEvent.click(vm1.getByTestId('monitoring.vm.sessions-toggle'));
    expect(session(S1)).toBeUndefined();

    // not linked, after the heading: the VM of an unlisted device, S2 (no VM), S3 (no VM of its workgroup)
    const other = screen.getByTestId('monitoring.other');
    expect(other).toHaveTextContent('Not linked to a listed device or VM');
    const after = (node: Element) => other.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(after(vm('vm-00000003'))).toBeTruthy();
    expect(after(session(S2))).toBeTruthy();
    expect(after(session(S3))).toBeTruthy();
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
    // a device label renames the device row
    act(() => labelDevice(t.w.store, t.w.org, DEV, 'Rack 1'));
    expect(within(device(DEV)).getByTestId('monitoring.device.name')).toHaveTextContent('Rack 1');
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
    await screen.findByTestId('monitoring.other');
    expect(screen.getAllByTestId('monitoring.session')).toHaveLength(1);
    expect(session(S3)).toBeDefined();
    expect(screen.queryByTestId('monitoring.vm')).toBeNull();
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

describe('work VMs (KOR-29)', () => {
  it('shows the work VMs of a session below it, with resources, runtime, freshness and charts', async () => {
    const t = await setup({
      body: (w) => {
        const body = overview(w, Date.now());
        const of = (id: string, extra: Record<string, unknown>) => ({
          workvm_id: id,
          device_id: DEV,
          session_id: S1,
          task_id: TASK,
          wg_id: w.world.defaultWg,
          state: 'running',
          latest: sample(Date.now(), { uptime_seconds: 90 }),
          stale: false,
          ...extra,
        });
        return {
          ...body,
          workvms: [of('wvm-0000000a', {}), of('wvm-0000000b', { latest: null, stale: true, state: 'deleted' })],
        };
      },
    });
    const kinds: (string | null)[] = [];
    t.w.client.on('GET /v1/monitoring/series', (_body, target) => {
      kinds.push(new URL(target, 'https://hub.example').searchParams.get('entity_kind'));
      return { points: [] };
    });
    await screen.findAllByTestId('monitoring.device');
    fireEvent.click(within(device(DEV)).getByTestId('monitoring.device.vms-toggle'));
    await waitFor(() => expect(t.w.store.getState().monitoring.links).toMatchObject({ [S1]: 'vm-00000001' }));
    fireEvent.click(within(vm('vm-00000001')).getByTestId('monitoring.vm.sessions-toggle'));
    // the work VM rows follow their session's row in the flat list
    expect(within(session(S1)).getByTestId('monitoring.session.workvms')).toHaveTextContent('Work VMs (2)');
    const items = screen.getAllByTestId('monitoring.workvm');
    expect(items.map((node) => node.dataset.workvm)).toEqual(['wvm-0000000a', 'wvm-0000000b']);
    expect(items.map((node) => node.dataset.session)).toEqual([S1, S1]);
    expect(session(S1).nextElementSibling).toBe(items[0]);
    expect(items[0]!.nextElementSibling).toBe(items[1]);
    expect(items[0]).toHaveClass('ml-12');
    const first = within(items[0]!);
    expect(first.getByTestId('monitoring.workvm.name')).toHaveTextContent('Work VMID: wvm-0000000a');
    expect(first.getByTestId('monitoring.workvm.state')).toHaveTextContent('running');
    expect(first.getByTestId('monitoring.workvm.uptime')).toHaveTextContent('1 min');
    expect(first.getByTestId('monitoring.workvm.memory')).toHaveTextContent('2.0 GiB / 8.0 GiB (25%)');
    expect(items[0]).toHaveAttribute('data-stale', 'false');
    expect(items[1]).toHaveAttribute('data-stale', 'true');
    expect(items[1]).toHaveClass('text-gray-600');
    expect(within(items[1]!).getByTestId('monitoring.workvm.age')).toHaveTextContent('No data');
    expect(within(items[1]!).getByTestId('monitoring.workvm.uptime')).toHaveTextContent('–');
    fireEvent.click(first.getByTestId('monitoring.workvm.charts-toggle'));
    expect(first.getByTestId('monitoring.workvm.charts-toggle')).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(kinds).toContain('workvm'));
    fireEvent.click(first.getByTestId('monitoring.workvm.charts-toggle'));
    expect(first.getByTestId('monitoring.workvm.charts-toggle')).toHaveAttribute('aria-expanded', 'false');
    expect(within(session(S2)).queryByTestId('monitoring.session.workvms')).toBeNull();
    // closing the VM drops the session and its work VM rows
    fireEvent.click(within(vm('vm-00000001')).getByTestId('monitoring.vm.sessions-toggle'));
    expect(screen.queryByTestId('monitoring.workvm')).toBeNull();
  });
});

describe('monitoring tree volume', () => {
  const many = (count: number) => ({
    devices: [],
    vms: [],
    sessions: Array.from({ length: count }, (_, index) => ({
      session_id: `01930000-0000-7000-8000-${String(index).padStart(12, '0')}`,
      task_id: TASK,
      wg_id: 'other',
      state: 'idle',
      latest: null,
      usage: null,
    })),
  });

  it('renders only the window of a large tree', async () => {
    await setup({ body: () => many(600) });
    const list = await screen.findByTestId('monitoring.devices');
    expect(list).toHaveAttribute('data-total', '601');
    expect(list.children).toHaveLength(TREE_SIZE);
    expect(screen.getByTestId('monitoring.other')).toBeInTheDocument();
    expect(screen.getByTestId('monitoring.tree.more')).toHaveTextContent('501 more rows – show');
    for (let move = 0; move < 3; move += 1) fireEvent.click(screen.getByTestId('monitoring.tree.more'));
    expect(list.children).toHaveLength(TREE_MAX_ROWS);
    expect(screen.queryByTestId('monitoring.other')).toBeNull();
    expect(screen.getByTestId('monitoring.tree.above')).toHaveTextContent('50 rows above – show');
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      fireEvent.click(screen.getByTestId('monitoring.tree.top'));
    } finally {
      Element.prototype.scrollIntoView = original;
    }
    expect(scroll).toHaveBeenCalled();
    expect(screen.getByTestId('monitoring.other')).toBeInTheDocument();
    expect(screen.getByTestId('monitoring.tree.status')).toHaveTextContent('Showing entries 1–100 of 601');
  });

  it('says when the hub cut the sessions', async () => {
    const t = await setup();
    await screen.findAllByTestId('monitoring.device');
    expect(screen.queryByTestId('monitoring.truncated')).toBeNull();
    const cut = (extra: object) =>
      act(() =>
        t.w.store.update('monitoring', (state) => {
          const entry = state.orgs[t.w.org]!;
          return {
            ...state,
            orgs: { ...state.orgs, [t.w.org]: { ...entry, overview: { ...entry.overview!, ...extra } } },
          };
        }),
      );
    cut({ sessions_total: 5000, truncated: true });
    expect(screen.getByTestId('monitoring.truncated')).toHaveTextContent(
      'Showing the 3 most recently active of 5,000 sessions.',
    );
    cut({ sessions_total: null });
    expect(screen.getByTestId('monitoring.truncated')).toHaveTextContent('Showing the 3 most recently active of 3');
    cut({ truncated: false });
    expect(screen.queryByTestId('monitoring.truncated')).toBeNull();
    expect(screen.queryByTestId('monitoring.devices-truncated')).toBeNull();
    cut({ devices_truncated: true });
    expect(screen.getByTestId('monitoring.devices-truncated')).toHaveTextContent(
      /^Showing the first \d+ devices; the hub has more\.$/,
    );
  });

  it('counts the ages in their cells every second', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    const intervals = vi.spyOn(globalThis, 'setInterval');
    const t = await setup();
    await screen.findAllByTestId('monitoring.device');
    const age = () => within(device(DEV)).getByTestId('monitoring.device.age');
    expect(age()).toHaveTextContent('Updated 5 s ago');
    const ticks = intervals.mock.calls.filter((call) => call[1] === SECOND_MS);
    expect(ticks).toHaveLength(1);
    vi.setSystemTime(t.now + 3000);
    act(() => (ticks[0]![0] as () => void)());
    expect(age()).toHaveTextContent('Updated 8 s ago');
    // the clock stops with the screen and starts again with the time of then
    await act(async () => {
      t.router.navigate(`/o/${t.w.org}/elsewhere`);
    });
    vi.setSystemTime(t.now + 10_000);
    await act(async () => {
      t.router.navigate(`/o/${t.w.org}/monitoring`);
    });
    expect(age()).toHaveTextContent('Updated 15 s ago');
  });

  it('greys an entity when its value turns stale, without a tick per second', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    const timeouts = vi.spyOn(globalThis, 'setTimeout');
    const t = await setup();
    await screen.findAllByTestId('monitoring.device');
    expect(device(DEV)).toHaveAttribute('data-stale', 'false');
    // the device's sample is 5 s old: it turns stale 55 s later
    const timer = timeouts.mock.calls.find((call) => call[1] === 55_001)!;
    vi.setSystemTime(t.now + 55_001);
    act(() => (timer[0] as () => void)());
    expect(device(DEV)).toHaveAttribute('data-stale', 'true');
    expect(device(DEV)).toHaveClass('text-gray-600');
  });

  it('reads the task titles with one selector for the list', async () => {
    const w = await orgWorld();
    const wg = w.world.defaultWg;
    const item = { session_id: S1, task_id: TASK, wg_id: wg } as MonitoredSession;
    const select = selectTitles([item, { ...item, session_id: S2 }]);
    const none = select(w.store.getState());
    expect(none.size).toBe(0);
    expect(select(w.store.getState())).toBe(none);
    const put = (id: string, kind: string, body: object) =>
      putEvents(w.store, wg, [
        {
          org: w.org,
          scopeId: wg,
          scopeSeq: Number(id.slice(-1)),
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind, id, task: TASK },
          body,
        },
      ]);
    put('e-1', 'whiteboard.entry', {});
    expect(select(w.store.getState())).toBe(none);
    put('e-2', 'task.meta', { title: 'Build it' });
    const titled = select(w.store.getState());
    expect(titled.get(TASK)).toBe('Build it');
    put('e-3', 'whiteboard.entry', {});
    expect(select(w.store.getState())).toBe(titled);
  });
});

describe('monitoring tree rows', () => {
  it('compares rows by their content', () => {
    const other: TreeRow = { kind: 'other', key: 'other', level: 0 };
    const none: TreeRow = { kind: 'none', key: 'nv d', level: 1, of: 'device', id: 'd' };
    expect(sameRow(other, { ...other })).toBe(true);
    expect(sameRow(none, { ...none, id: 'e' })).toBe(false);
    expect(sameRow(other, none)).toBe(false);
  });

  it('finds the next entity to turn stale', () => {
    const now = Date.parse('2026-11-22T12:00:00.000Z');
    const at = (ms: number, stale = false): Sampled => ({ latest: sample(ms), previous: null, stale });
    const items: Sampled[] = [
      { latest: null, previous: null, stale: true },
      at(now, true),
      at(now - 120_000),
      at(now - 10_000),
      at(now - 20_000),
      at(now - 5_000),
    ];
    expect(nextStaleAt(items, now)).toBe(now + 40_001);
    expect(nextStaleAt(items.slice(0, 3), now)).toBeNull();
  });
});
