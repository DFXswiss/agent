// U-WEB-ui-devices (screen): the device list (verified log + the hub's presence and capacity, labels), the reader view,
// "Add org device" with the code and the enrollment command shown once and the enrollment followed live, the open
// device invitations, assigning a platform device (only verified candidates) or an org device, the platform-access
// hint (ADR-026), unassign, unpair and rename – in jsdom against the org world of tests/core/org/world.ts.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Entry } from '../../src/core/log/entries';
import { VerifiedPlatformLog } from '../../src/core/log/platform';
import { HubError } from '../../src/core/net/signedFetch';
import type { HubDevice } from '../../src/core/org/devices';
import { putEvents } from '../../src/core/store/slices/events';
import { orgEntry, setLog, setPlatform } from '../../src/core/store/slices/orgs';
import { DevicesScreen, POLL_MS } from '../../src/ui/screens/devices/DevicesScreen';
import { idOf } from '../core/log/world';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { mount, names } from './org-world';

afterEach(() => vi.useRealTimers());

const CAPACITY = {
  vm_memory_budget_mib: 8192,
  vm_memory_used_mib: 0,
  vm_slots_total: 8,
  vm_slots_free: 6,
  pool_ready: 1,
};

const hubRow = (id: string, extra: Partial<HubDevice> = {}): HubDevice => ({
  device_id: id,
  kind: 'org',
  state: 'active',
  online: true,
  last_seen_at: '2026-11-21T12:00:00.000Z',
  agent_version: '0.1.0',
  capacity: CAPACITY,
  capacity_low: false,
  assigned_wgs: [],
  ...extra,
});

/** ada (admin) with workgroup `one`, bob (member), org devices dev1 (default, one) and dev2 (default); in private
 *  orgs the platform device pdev on the default workgroup. */
async function deviceWorld(options: { as?: 'ada' | 'bob'; private?: boolean } = {}) {
  const one = await idOf('wg one');
  const w = await orgWorld({
    ...options,
    platformDevices: ['pdev'],
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.admitDevice(a.ada, a.dev1, [world.defaultWg, one]);
      await world.admitDevice(a.ada, a.dev2, [world.defaultWg]);
      if (options.private) {
        await world.batch(a.ada, [{ type: 'device_assign', device: a.pdev.id, wg: world.defaultWg, platform_seq: 2 }]);
      }
    },
  });
  w.hub.platformAccess = options.private ?? false;
  const { dev1, dev2, pdev } = w.others;
  const devices: HubDevice[] = [
    hubRow(dev1.id, { capacity_low: true, capacity: { ...CAPACITY, vm_slots_free: 0 } }),
    hubRow(dev2.id, { online: false, last_seen_at: null }),
  ];
  // a fresh array per answer, as JSON over the wire (the screen keeps the previous list when it is the same)
  w.client.on(`GET /v1/orgs/${w.org}/devices`, () => ({ devices: [...devices] }));
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  names(w, {}, { [one]: 'One', [w.world.defaultWg]: 'Default' });
  return { w, one, devices, pdev };
}

const at = [{ path: '/o/:org/devices', element: <DevicesScreen /> }];

const item = (id: string) => screen.getAllByTestId('devices.item').find((node) => node.dataset.device === id)!;

/** A verified `device.profile` label in the events slice. */
function label(w: OrgWorld, device: string, text: string) {
  putEvents(w.store, w.org, [
    {
      org: w.org,
      scopeId: w.org,
      scopeSeq: 50,
      receivedAt: null,
      event: {},
      status: 'verified' as const,
      code: null,
      header: { kind: 'device.profile', ref: device, author: w.me.id, wg: '', id: 'label' },
      body: { label: text },
    },
  ]);
}

describe('device list', () => {
  it('shows labels, kind, presence, version, workgroups and capacity; readers get no controls', async () => {
    const { w, one, pdev } = await deviceWorld({ as: 'bob', private: true });
    label(w, w.others.dev1.id, 'Studio');
    mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(within(item(w.others.dev1.id)).getByTestId('devices.item.version')).toBeVisible());
    const ids = screen.getAllByTestId('devices.item').map((node) => node.dataset.device);
    expect(ids).toEqual([pdev.id, w.others.dev2.id, w.others.dev1.id]);
    const first = item(w.others.dev1.id);
    expect(within(first).getByTestId('devices.item.name')).toHaveTextContent('Studio');
    expect(within(first).getByTestId('devices.item.kind')).toHaveTextContent('Own device');
    expect(within(first).getByTestId('devices.item.online')).toHaveTextContent('Online');
    expect(within(first).getByTestId('devices.item.capacity-low')).toBeVisible();
    expect(within(first).getByTestId('devices.item.capacity')).toHaveTextContent('8 of 8 VM slots in use');
    expect(within(first).getByTestId('devices.item.last-seen')).toHaveTextContent('Last seen');
    expect(within(first).getByTestId('devices.item.version')).toHaveTextContent('Version 0.1.0');
    expect(within(first).getByTestId('devices.item.wgs')).toHaveTextContent('Default');
    expect(within(first).getByTestId('devices.item.wgs')).toHaveTextContent('One');
    expect(within(first).getByTestId('devices.item.wgs').querySelector(`[data-wg="${one}"]`)).not.toBeNull();
    const second = item(w.others.dev2.id);
    expect(within(second).getByTestId('devices.item.name')).toHaveTextContent('Device without a name');
    expect(within(second).getByTestId('devices.item.online')).toHaveTextContent('Offline');
    expect(within(second).getByTestId('devices.item.last-seen')).toHaveTextContent('Never seen');
    expect(within(second).getByTestId('devices.item.capacity')).toHaveTextContent('2 of 8 VM slots in use');
    const platform = item(pdev.id);
    expect(within(platform).getByTestId('devices.item.kind')).toHaveTextContent('Platform device');
    expect(within(platform).getByTestId('devices.item.online')).toHaveTextContent('Status unknown');
    expect(screen.queryByTestId('devices.add.open')).toBeNull();
    expect(screen.queryByTestId('devices.assign.open')).toBeNull();
    expect(screen.queryByTestId('devices.platform.denied')).toBeNull();
    expect(screen.queryByTestId('devices.item.unassign')).toBeNull();
    expect(screen.queryByTestId('devices.item.rename')).toBeNull();
    expect(screen.queryByTestId('devices.item.unpair')).toBeNull();
    expect(screen.queryByTestId('devices.item.to-wg')).toBeNull();
    // the device profiles are pulled once
    const pulls = w.client.of(`GET /v1/scopes/${w.org}/events`).map((call) => call.target);
    expect(pulls.some((target) => target.includes('kinds=device.profile'))).toBe(true);
  });

  it('marks decertified platform devices and says when there are no devices', async () => {
    const { w, pdev } = await deviceWorld({ as: 'bob', private: true });
    const platform = w.store.getState().orgs.platform!;
    const ended = platform.state.with({
      certifications: new Map([...platform.state.certifications].map(([s, c]) => [s, { ...c, decertifiedSeq: 9 }])),
    });
    setPlatform(w.store, new VerifiedPlatformLog(platform.entries, [...platform.states, ended]));
    mount(w, at, `/o/${w.org}/devices`);
    expect(within(item(pdev.id)).getByTestId('devices.item.decertified')).toBeVisible();
    const empty = await orgWorld({ as: 'ada' });
    empty.client.on(`GET /v1/orgs/${empty.org}/devices`, () => ({ devices: [] }));
    empty.client.on(`GET /v1/scopes/${empty.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
    mount(empty, at, `/o/${empty.org}/devices`);
    expect(screen.getByTestId('devices.empty')).toBeVisible();
  });

  it('says loading before the log, not found for a non-member, and shows a failing hub list', async () => {
    const { w } = await deviceWorld();
    mount(w, at, `/o/other/devices`);
    expect(screen.getByTestId('devices.loading')).toBeVisible();
    const gone = await deviceWorld({ as: 'bob' });
    await gone.w.world.batch(gone.w.others.bob, [{ type: 'remove', subject: gone.w.others.bob.id }]);
    await gone.w.sync();
    mount(gone.w, at, `/o/${gone.w.org}/devices`);
    expect(screen.getByTestId('devices.not-found')).toBeVisible();
    const failing = await deviceWorld({ as: 'bob' });
    failing.w.client.on(`GET /v1/orgs/${failing.w.org}/devices`, () => {
      throw new HubError('forbidden', 403);
    });
    mount(failing.w, at, `/o/${failing.w.org}/devices`);
    await waitFor(() => expect(screen.getByTestId('devices.error')).toHaveAttribute('data-code', 'forbidden'));
  });

  it('reads the hub list again on log changes and every poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { w, devices } = await deviceWorld({ as: 'bob' });
    mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(1));
    devices[1] = { ...devices[1]!, online: true };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await waitFor(() => expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(2));
    await waitFor(() =>
      expect(within(item(w.others.dev2.id)).getByTestId('devices.item.online')).toHaveTextContent('Online'),
    );
  });
});

describe('hidden tabs (KOR-05, A-25)', () => {
  it('does not poll while the tab is hidden and reads the list again when it becomes visible', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    const { w } = await deviceWorld({ as: 'bob' });
    const view = mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(1));
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    });
    expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(1);
    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await waitFor(() => expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(3));
    // leaving the screen ends the polling and the listener
    await act(async () => {
      await view.router.navigate('/elsewhere');
    });
    document.dispatchEvent(new Event('visibilitychange'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(w.client.of(`GET /v1/orgs/${w.org}/devices`)).toHaveLength(3);
  });
});

describe('stale answers', () => {
  it('ignores an older answer of the hub list that arrives after a newer one', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { w, devices } = await deviceWorld({ as: 'bob' });
    const slow: (() => void)[] = [];
    let calls = 0;
    w.client.on(`GET /v1/orgs/${w.org}/devices`, async () => {
      calls += 1;
      const snapshot = devices.map((row) => ({ ...row }));
      if (calls === 2) {
        await new Promise<void>((resolve) => slow.push(resolve));
        throw new HubError('unavailable', 503);
      }
      if (calls === 1) {
        await new Promise<void>((resolve) => slow.push(resolve));
        return { devices: snapshot.map((row) => ({ ...row, online: false })) };
      }
      return { devices: snapshot };
    });
    mount(w, at, `/o/${w.org}/devices`);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await waitFor(() => expect(calls).toBe(3));
    await waitFor(() =>
      expect(within(item(w.others.dev1.id)).getByTestId('devices.item.online')).toHaveTextContent('Online'),
    );
    // the two older answers (a list with every device offline, a failure) arrive last and change nothing
    await act(async () => {
      slow.forEach((release) => release());
      await Promise.resolve();
    });
    expect(within(item(w.others.dev1.id)).getByTestId('devices.item.online')).toHaveTextContent('Online');
    expect(screen.queryByTestId('devices.error')).toBeNull();
  });
});

describe('hub paging', () => {
  it('lists the devices of the loaded pages, loads the next one and re-reads the loaded range on a poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { w, devices } = await deviceWorld({ as: 'bob' });
    const { dev1, dev2 } = w.others;
    let failing = false;
    // newest enrolment first: dev2, then dev1 (a fresh array per answer)
    w.client.on(`GET /v1/orgs/${w.org}/devices`, (_body, target) => {
      if (failing) throw new HubError('unavailable', 503);
      return String(target).includes('before=')
        ? { devices: [{ ...devices[0]! }], next_before: null }
        : { devices: [{ ...devices[1]! }], next_before: 'c1' };
    });
    mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(screen.getAllByTestId('devices.item')).toHaveLength(1));
    // the size of the list is every device of the log
    expect(item(dev2.id)).toHaveAttribute('aria-setsize', '2');
    fireEvent.click(screen.getByTestId('devices.list.load-more'));
    await waitFor(() => expect(screen.getAllByTestId('devices.item')).toHaveLength(2));
    expect(within(item(dev1.id)).getByTestId('devices.item.online')).toHaveTextContent('Online');
    expect(screen.queryByTestId('devices.list.load-more')).toBeNull();
    // the poll reads both loaded pages again
    devices[0] = { ...devices[0]!, online: false };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await waitFor(() => expect(within(item(dev1.id)).getByTestId('devices.item.online')).toHaveTextContent('Offline'));
    expect(w.client.of(`GET /v1/orgs/${w.org}/devices`).map((call) => call.target)).toEqual([
      `/v1/orgs/${w.org}/devices?limit=200`,
      `/v1/orgs/${w.org}/devices?before=c1&limit=200`,
      `/v1/orgs/${w.org}/devices?limit=200`,
      `/v1/orgs/${w.org}/devices?before=c1&limit=200`,
    ]);
    // a failing re-read keeps the rows
    failing = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await waitFor(() => expect(screen.getByTestId('devices.error')).toHaveAttribute('data-code', 'unavailable'));
    expect(screen.getAllByTestId('devices.item')).toHaveLength(2);
    expect(screen.queryByTestId('devices.truncated')).toBeNull();
  });

  it('offers "Load more" while a page names no device of the log, and shows a failing or late page', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { w, devices } = await deviceWorld({ as: 'bob' });
    const slow: { release: () => void; fail: boolean }[] = [];
    w.client.on(`GET /v1/orgs/${w.org}/devices`, async (_body, target) => {
      if (!String(target).includes('before=')) return { devices: [hubRow('not-in-the-log')], next_before: 'c1' };
      const pending: { release: () => void; fail: boolean } = { release: () => undefined, fail: false };
      await new Promise<void>((resolve) => {
        pending.release = resolve;
        slow.push(pending);
      });
      if (pending.fail) throw new HubError('forbidden', 403);
      return { devices: devices.map((row) => ({ ...row })), next_before: null };
    });
    mount(w, at, `/o/${w.org}/devices`);
    const more = await screen.findByTestId('devices.list.load-more');
    expect(screen.queryByTestId('devices.item')).toBeNull();
    expect(screen.queryByTestId('devices.empty')).toBeNull();
    // a page that arrives after a poll started is dropped
    fireEvent.click(more);
    await waitFor(() => expect(screen.getByTestId('devices.list.load-more')).toHaveTextContent('Loading…'));
    expect(screen.getByTestId('devices.list.load-more')).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await act(async () => {
      slow[0]!.release();
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByTestId('devices.list.load-more')).toHaveTextContent('Load more'));
    expect(screen.queryByTestId('devices.item')).toBeNull();
    // a late failure is dropped as well
    fireEvent.click(screen.getByTestId('devices.list.load-more'));
    await waitFor(() => expect(slow).toHaveLength(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await act(async () => {
      slow[1]!.fail = true;
      slow[1]!.release();
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByTestId('devices.list.load-more')).toBeEnabled());
    expect(screen.queryByTestId('devices.error')).toBeNull();
    // a failing page is shown
    fireEvent.click(screen.getByTestId('devices.list.load-more'));
    await waitFor(() => expect(slow).toHaveLength(3));
    await act(async () => {
      slow[2]!.fail = true;
      slow[2]!.release();
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByTestId('devices.error')).toHaveAttribute('data-code', 'forbidden'));
    // and the next one arrives
    fireEvent.click(screen.getByTestId('devices.list.load-more'));
    await waitFor(() => expect(slow).toHaveLength(4));
    await act(async () => {
      slow[3]!.release();
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getAllByTestId('devices.item')).toHaveLength(2));
    expect(screen.queryByTestId('devices.error')).toBeNull();
  });

  it('repeats the request without paging on an old hub and says when it cut the list', async () => {
    const { w, devices } = await deviceWorld({ as: 'bob' });
    w.client.on(`GET /v1/orgs/${w.org}/devices`, (_body, target) => {
      if (String(target).includes('?')) throw new HubError('request_invalid', 400);
      return { devices: [{ ...devices[1]! }], truncated: true };
    });
    mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(screen.getByTestId('devices.truncated')).toBeVisible());
    expect(screen.getByTestId('devices.truncated')).toHaveTextContent('newest devices only');
    // every device of the log is listed; the one the hub did not send with presence unknown
    expect(screen.getAllByTestId('devices.item')).toHaveLength(2);
    expect(within(item(w.others.dev1.id)).getByTestId('devices.item.online')).toHaveTextContent('Status unknown');
    expect(screen.queryByTestId('devices.list.load-more')).toBeNull();
    expect(w.client.of(`GET /v1/orgs/${w.org}/devices`).map((call) => call.target)).toEqual([
      `/v1/orgs/${w.org}/devices?limit=200`,
      `/v1/orgs/${w.org}/devices`,
    ]);
  });

  it('pages the open device invitations and the platform candidates', async () => {
    const { w, one, pdev } = await deviceWorld({ private: true });
    w.world.add(pdev);
    // the newest page holds no open invitation: "Load more" without rows; the older one is cut by an old cap
    w.client.on(`GET /v1/orgs/${w.org}/invites`, (_body, target) => {
      const rows = [...w.hub.invites.values()].map(({ package_ct: _p, ...row }) => row);
      return String(target).includes('before=')
        ? { invites: rows, next_before: null, truncated: true }
        : { invites: [], next_before: 'c1' };
    });
    mount(w, at, `/o/${w.org}/devices`);
    await screen.findByTestId('devices.invites.rows.load-more');
    fireEvent.click(screen.getByTestId('devices.add.open'));
    fireEvent.click(screen.getByTestId('devices.add.submit'));
    await screen.findByTestId('devices.add.code');
    fireEvent.click(screen.getByTestId('devices.add.done'));
    expect(screen.queryByTestId('devices.invites.empty')).toBeNull();
    fireEvent.click(screen.getByTestId('devices.invites.rows.load-more'));
    await waitFor(() => expect(screen.getAllByTestId('devices.invites.item')).toHaveLength(1));
    expect(screen.getByTestId('devices.invites.truncated')).toHaveTextContent('only the newest device invitations');
    w.client.on(`GET /v1/workgroups/${w.world.defaultWg}/device-candidates`, () => ({ candidates: [] }));
    const candidate = {
      device_id: pdev.id,
      platform_seq: 2,
      pub: pdev.identity.public(),
      online: true,
      capacity: CAPACITY,
      capacity_low: false,
    };
    let release: () => void = () => undefined;
    w.client.on(`GET /v1/workgroups/${one}/device-candidates`, async (_body, target) => {
      if (!String(target).includes('before=')) return { candidates: [{ ...candidate }], next_before: 'k1' };
      await new Promise<void>((resolve) => (release = resolve));
      return { candidates: [{ ...candidate, device_id: 'other' }], next_before: null, truncated: true };
    });
    fireEvent.click(screen.getByTestId('devices.assign.open'));
    fireEvent.change(screen.getByTestId('devices.assign.scope'), { target: { value: one } });
    await waitFor(() => expect(screen.getAllByTestId('devices.assign.candidate')).toHaveLength(1));
    fireEvent.click(screen.getByTestId('devices.assign.load-more'));
    await waitFor(() => expect(screen.getByTestId('devices.assign.load-more')).toHaveTextContent('Loading…'));
    release();
    await waitFor(() => expect(screen.getAllByTestId('devices.assign.candidate')).toHaveLength(2));
    expect(screen.getByTestId('devices.assign.truncated')).toHaveTextContent('only part');
    expect(screen.queryByTestId('devices.assign.load-more')).toBeNull();
  });

  it('renders a bounded number of candidates however many are loaded, searchable by device ID', async () => {
    const { w, one, pdev } = await deviceWorld({ private: true });
    const many = Array.from({ length: 25 }, (_, index) => ({
      device_id: `cand-${String(index).padStart(2, '0')}`,
      platform_seq: 2,
      pub: pdev.identity.public(),
      online: false,
      capacity: null,
      capacity_low: false,
    }));
    w.client.on(`GET /v1/workgroups/${w.world.defaultWg}/device-candidates`, () => ({ candidates: [] }));
    w.client.on(`GET /v1/workgroups/${one}/device-candidates`, () => ({ candidates: many, next_before: null }));
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(screen.getByTestId('devices.assign.open'));
    fireEvent.change(screen.getByTestId('devices.assign.scope'), { target: { value: one } });
    await waitFor(() => expect(screen.getAllByTestId('devices.assign.candidate')).toHaveLength(20));
    expect(screen.getByTestId('devices.assign.candidates.more')).toHaveTextContent('Showing 20 of 25');
    fireEvent.change(screen.getByTestId('devices.assign.candidates.search'), { target: { value: 'cand-24' } });
    expect(screen.getAllByTestId('devices.assign.candidate').map((row) => row.dataset.device)).toEqual(['cand-24']);
  });
});

describe('admin controls', () => {
  it('shows the platform-access reason from the error catalog instead of the assign button (ADR-026)', async () => {
    const { w } = await deviceWorld();
    mount(w, at, `/o/${w.org}/devices`);
    const denied = screen.getByTestId('devices.platform.denied');
    expect(denied).toHaveTextContent('No platform devices');
    expect(denied).toHaveTextContent('This organization may not use platform execution devices.');
    expect(screen.queryByTestId('devices.assign.open')).toBeNull();
  });

  it('adds an org device: code and command once, enrollment followed live, open invitations revocable', async () => {
    const { w, one } = await deviceWorld();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(screen.getByTestId('devices.invites.empty')).toBeVisible());
    fireEvent.click(screen.getByTestId('devices.add.open'));
    fireEvent.click(screen.getByTestId('devices.add.cancel'));
    fireEvent.click(screen.getByTestId('devices.add.open'));
    expect(screen.getByTestId(`devices.add.wg.${w.world.defaultWg}`)).toBeChecked();
    fireEvent.click(screen.getByTestId(`devices.add.wg.${w.world.defaultWg}`));
    expect(screen.getByTestId('devices.add.submit')).toBeDisabled();
    fireEvent.click(screen.getByTestId(`devices.add.wg.${one}`));
    expect(screen.getByTestId('devices.add.expiry')).toHaveValue('24');
    fireEvent.change(screen.getByTestId('devices.add.expiry'), { target: { value: '72' } });
    fireEvent.click(screen.getByTestId('devices.add.submit'));
    const code = await screen.findByTestId('devices.add.code');
    const text = code.textContent!;
    expect(text).toMatch(/^[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{6}$/);
    expect(screen.getByTestId('devices.add.command')).toHaveTextContent(
      `agent-runner enroll --hub https://hub.example --code ${text}`,
    );
    expect(screen.getByTestId('devices.add.warning')).toBeVisible();
    expect(screen.getByTestId('devices.add.expires')).toHaveTextContent('Valid until');
    fireEvent.click(screen.getByTestId('devices.add.copy-command'));
    await waitFor(() => expect(screen.getByTestId('devices.add.copy-command')).toHaveTextContent('Copied'));
    fireEvent.click(screen.getByTestId('devices.add.copy-code'));
    await waitFor(() => expect(screen.getByTestId('devices.add.copy-code')).toHaveTextContent('Copied'));
    expect(writeText).toHaveBeenCalledWith(text);
    expect(screen.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'false');
    const [invite] = [...w.hub.invites.values()];
    expect(invite!.kind).toBe('device');
    const entry = orgEntry(w.store.getState().orgs, w.org).log!;
    expect(entry.state.invites.get(invite!.invite_id)!.wgs).toEqual({ [one]: 'device' });
    await waitFor(() => expect(screen.getAllByTestId('devices.invites.item')).toHaveLength(1));
    // the device's accept arrives with the next verified log
    const known = entry.state.invites.get(invite!.invite_id)!;
    const accepted = entry.state.with({
      invites: new Map(entry.state.invites).set(invite!.invite_id, { ...known, acceptedSeq: 99 }),
    });
    act(() => setLog(w.store, w.org, { ...entry, state: accepted } as never));
    await waitFor(() => expect(screen.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'true'));
    expect(screen.getByTestId('devices.add.status')).toHaveTextContent('The device has enrolled.');
    fireEvent.click(screen.getByTestId('devices.add.done'));
    expect(screen.queryByTestId('devices.add.result')).toBeNull();
    // a failing copy leaves the label
    writeText.mockRejectedValueOnce(new Error('denied'));
  });

  it('offers the notarised runner package for a Mac next to the code (ADR-074)', async () => {
    const feed = {
      version: '2026.11.24.0800',
      package: 'DFX-Agent-Runner-2026.11.24.0800.pkg',
      notarized: true,
      expires: '2999-01-01T00:00:00Z',
    };
    let release: (value: Response) => void = () => undefined;
    const slow = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const fetcher = vi
      .fn()
      .mockImplementationOnce(async () => slow)
      .mockImplementation(async () => new Response(JSON.stringify(feed), { status: 200 }));
    vi.stubGlobal('fetch', fetcher);
    try {
      const { w } = await deviceWorld();
      mount(w, at, `/o/${w.org}/devices`);
      // the first result closes before the feed arrives: nothing is set after it is gone
      fireEvent.click(screen.getByTestId('devices.add.open'));
      fireEvent.click(screen.getByTestId('devices.add.submit'));
      await screen.findByTestId('devices.add.code');
      fireEvent.click(screen.getByTestId('devices.add.done'));
      await act(async () => {
        release(new Response(JSON.stringify(feed), { status: 200 }));
        await slow;
      });
      fireEvent.click(screen.getByTestId('devices.add.open'));
      fireEvent.click(screen.getByTestId('devices.add.submit'));
      const link = await screen.findByTestId('devices.add.mac-download');
      expect(link).toHaveAttribute('href', '/downloads/runner/DFX-Agent-Runner-2026.11.24.0800.pkg');
      expect(link).toHaveAttribute('download');
      expect(link).toHaveTextContent('Download for Mac');
      expect(screen.getByTestId('devices.add.mac')).toHaveTextContent('it asks for this code');
      expect(fetcher).toHaveBeenCalledWith('/downloads/runner/latest.json', { cache: 'no-cache', credentials: 'omit' });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('revokes open device invitations and shows failures of create, list and revoke', async () => {
    const { w } = await deviceWorld();
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(screen.getByTestId('devices.add.open'));
    fireEvent.click(screen.getByTestId('devices.add.submit'));
    await screen.findByTestId('devices.add.code');
    fireEvent.click(screen.getByTestId('devices.add.done'));
    await waitFor(() => expect(screen.getAllByTestId('devices.invites.item')).toHaveLength(1));
    const [invite] = [...w.hub.invites.values()];
    w.client.on(`POST /v1/orgs/${w.org}/invites/${invite!.invite_id}/revoke`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('devices.invites.revoke'));
    await waitFor(() => expect(screen.getByTestId('devices.invites.error')).toHaveAttribute('data-code', 'forbidden'));
    w.client.on(`POST /v1/orgs/${w.org}/invites/${invite!.invite_id}/revoke`, async (raw) => {
      const { entry } = raw as { entry: Entry };
      const result = await w.hub.append(w.me.id, {
        expected_seq: w.world.log!.head.seq + 1,
        entries: [entry],
        wraps: [],
        events: [],
      });
      invite!.state = 'revoked';
      return { seq: result.head.seq };
    });
    fireEvent.click(screen.getByTestId('devices.invites.revoke'));
    await waitFor(() => expect(screen.getByTestId('devices.invites.empty')).toBeVisible());
    w.client.on(`POST /v1/orgs/${w.org}/invites`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('devices.add.open'));
    fireEvent.click(screen.getByTestId('devices.add.submit'));
    await waitFor(() => expect(screen.getByTestId('devices.add.error')).toHaveAttribute('data-code', 'forbidden'));
    expect(screen.getByTestId('devices.add.submit')).toBeEnabled();
    const failing = await deviceWorld();
    failing.w.client.on(`GET /v1/orgs/${failing.w.org}/invites`, () => null);
    mount(failing.w, at, `/o/${failing.w.org}/devices`);
    await waitFor(() =>
      expect(screen.getAllByTestId('devices.invites.error').at(-1)).toHaveAttribute('data-code', 'unavailable'),
    );
  });

  it('invites devices only once a workgroup is unlocked, but assigns to every workgroup (ADR-058)', async () => {
    const { w, one } = await deviceWorld({ private: true });
    w.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [w.org]: { ...orgs.byId[w.org]!, keys: new Map() } },
    }));
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(screen.getByTestId('devices.add.open'));
    expect(screen.getByTestId('devices.add.none')).toHaveTextContent('as soon as a workgroup is unlocked for you');
    expect(screen.getByTestId('devices.add.submit')).toBeDisabled();
    fireEvent.click(screen.getByTestId('devices.add.cancel'));
    fireEvent.click(screen.getByTestId('devices.assign.open'));
    const choices = within(screen.getByTestId('devices.assign.wg')).getAllByRole('option');
    expect(choices.map((option) => (option as HTMLOptionElement).value)).toEqual([w.world.defaultWg, one]);
    expect(screen.getByTestId('devices.assign')).toHaveTextContent('The device can then run tasks of this workgroup.');
    fireEvent.click(screen.getByTestId('devices.assign.cancel'));
    fireEvent.click(within(item(w.others.dev1.id)).getByTestId('devices.item.to-wg'));
    expect(screen.getByTestId('devices.to-wg.none')).toHaveTextContent('already assigned to every workgroup');
  });
});

describe('assigning', () => {
  it('assigns a verified platform candidate; unverifiable ones cannot be chosen', async () => {
    const { w, one, pdev } = await deviceWorld({ private: true });
    w.world.add(pdev);
    const candidate = {
      device_id: pdev.id,
      platform_seq: 2,
      pub: pdev.identity.public(),
      online: true,
      capacity: CAPACITY,
      capacity_low: true,
    };
    const forged = { ...candidate, device_id: w.others.dev1.id, online: false, capacity: null, capacity_low: false };
    let release: () => void = () => undefined;
    w.client.on(`GET /v1/workgroups/${w.world.defaultWg}/device-candidates`, () => ({ candidates: [] }));
    w.client.on(`GET /v1/workgroups/${one}/device-candidates`, async () => {
      await new Promise<void>((resolve) => (release = resolve));
      return { candidates: [forged, candidate] };
    });
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(screen.getByTestId('devices.assign.open'));
    await waitFor(() => expect(screen.getByTestId('devices.assign.none')).toBeVisible());
    fireEvent.change(screen.getByTestId('devices.assign.scope'), { target: { value: one } });
    expect(screen.getByTestId('devices.assign.loading')).toBeVisible();
    release();
    await waitFor(() => expect(screen.getAllByTestId('devices.assign.candidate')).toHaveLength(2));
    const [bad, good] = screen.getAllByTestId('devices.assign.candidate');
    expect(bad).toHaveAttribute('data-verified', 'false');
    expect(bad).toHaveTextContent('Not certified in the platform log');
    expect(bad).toHaveTextContent('Offline');
    expect(within(bad!).getByRole('radio')).toBeDisabled();
    expect(good).toHaveTextContent('Capacity low');
    expect(good).toHaveTextContent('2 of 8 VM slots in use');
    expect(within(good!).getByRole('radio')).not.toBeChecked();
    expect(screen.getByTestId('devices.assign.confirm')).toBeDisabled();
    fireEvent.click(within(good!).getByRole('radio'));
    expect(within(good!).getByRole('radio')).toBeChecked();
    fireEvent.click(screen.getByTestId('devices.assign.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.assign')).toBeNull());
    expect(w.world.state.assigned(pdev.id, one)).toBe(true);
  });

  it('shows a failing candidate list and a refused assignment', async () => {
    const { w, one, pdev } = await deviceWorld({ private: true });
    w.client.on(`GET /v1/workgroups/${w.world.defaultWg}/device-candidates`, () => {
      throw new HubError('forbidden', 403);
    });
    w.client.on(`GET /v1/workgroups/${one}/device-candidates`, () => ({
      candidates: [{ device_id: pdev.id, platform_seq: 2, pub: pdev.identity.public() }],
    }));
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new HubError('platform_access_denied', 403);
    });
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(screen.getByTestId('devices.assign.open'));
    await waitFor(() =>
      expect(screen.getByTestId('devices.assign.load-error')).toHaveAttribute('data-code', 'forbidden'),
    );
    fireEvent.change(screen.getByTestId('devices.assign.scope'), { target: { value: one } });
    fireEvent.click(await screen.findByRole('radio'));
    await waitFor(() => expect(screen.getByTestId('devices.assign.confirm')).toBeEnabled());
    fireEvent.click(screen.getByTestId('devices.assign.confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('devices.assign.error')).toHaveAttribute('data-code', 'platform_access_denied'),
    );
    expect(screen.getByTestId('devices.assign.error')).toHaveTextContent('No platform devices');
  });

  it('assigns an org device to a further workgroup', async () => {
    const { w, one } = await deviceWorld();
    const dev2 = w.others.dev2;
    w.world.add(dev2);
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(within(item(dev2.id)).getByTestId('devices.item.to-wg'));
    expect(screen.getByTestId('devices.to-wg.wg')).toHaveValue(one);
    fireEvent.change(screen.getByTestId('devices.to-wg.wg'), { target: { value: one } });
    fireEvent.click(screen.getByTestId('devices.to-wg.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.to-wg')).toBeNull());
    expect(w.world.state.assigned(dev2.id, one)).toBe(true);
    await waitFor(() => expect(within(item(dev2.id)).getByTestId('devices.item.wgs')).toHaveTextContent('One'));
  });
});

describe('the whole organization (ADR-075)', () => {
  it('shows the capacity details, the VMs with workgroup and sessions, and "Whole organization"', async () => {
    const { w, one, devices } = await deviceWorld({ as: 'bob' });
    const dev1 = w.others.dev1;
    devices[0] = hubRow(dev1.id, {
      capacity: {
        ...CAPACITY,
        cpus: 8,
        memory_total_mib: 16384,
        disk_free_mib: 102912,
        vms_running: 2,
        sessions_active: 4,
        sessions_per_vm: 16,
        vms_max: 0,
      },
      vms: [
        { vm_id: 'vm-1a2b3c4d', wg_id: one, state: 'running', sessions: 3 },
        { vm_id: 'vm-5e6f7a8b', wg_id: 'unknown-wg', state: 'stopped', sessions: 0 },
      ],
    });
    devices[1] = hubRow(w.others.dev2.id, {
      capacity: { ...CAPACITY, sessions_per_vm: 0, vms_max: 4 },
      vms: [{ vm_id: 'vm-9c9c9c9c', wg_id: one, state: 'running', sessions: 5 }],
    });
    await w.world.batch(w.others.ada, [{ type: 'device_assign_org', device: dev1.id }]);
    await w.sync();
    mount(w, at, `/o/${w.org}/devices`);
    await waitFor(() => expect(within(item(dev1.id)).getByTestId('devices.item.details')).toBeVisible());
    const row = item(dev1.id);
    expect(row).toHaveAttribute('data-assigned-org', 'true');
    expect(item(w.others.dev2.id)).toHaveAttribute('data-assigned-org', 'false');
    expect(within(row).getByTestId('devices.item.org')).toHaveTextContent('Whole organization');
    const details = within(row).getByTestId('devices.item.details');
    for (const text of [
      'CPUs: 8',
      'Memory: 16 GiB',
      'Free disk: 100.5 GiB',
      'Running VMs: 2',
      'Active sessions: 4',
      'Sessions per VM: up to 16',
      'VMs: as many as the memory allows',
    ]) {
      expect(details).toHaveTextContent(text);
    }
    const vms = within(row).getAllByTestId('devices.vm');
    expect(vms[0]).toHaveTextContent('vm-1a2b3c4d · One · 3 of 16 sessions');
    expect(vms[0]).toHaveAttribute('data-wg', one);
    expect(vms[0]).toHaveAttribute('data-sessions', '3');
    expect(vms[1]).toHaveTextContent('vm-5e6f7a8b · Workgroup · 0 of 16 sessions');
    const other = item(w.others.dev2.id);
    expect(within(other).getByTestId('devices.item.details')).toHaveTextContent('Sessions per VM: no limit');
    expect(within(other).getByTestId('devices.item.details')).toHaveTextContent('VMs: up to 4');
    expect(within(other).getByTestId('devices.vm')).toHaveTextContent('vm-9c9c9c9c · One · 5 sessions');
    expect(screen.queryByTestId('devices.unassign-org')).toBeNull();
    expect(screen.queryByTestId('devices.item.assign-org')).toBeNull();
  });

  it('gives a listed device to the whole organization with the plain notice and removes it again', async () => {
    const { w, one } = await deviceWorld();
    const dev2 = w.others.dev2;
    w.world.add(dev2);
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(within(item(dev2.id)).getByTestId('devices.item.assign-org'));
    expect(screen.getByTestId('devices.assign.org-notice')).toHaveTextContent('receives the keys of every workgroup');
    fireEvent.click(screen.getByTestId('devices.assign-org.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.assign-org')).toBeNull());
    expect(w.world.state.orgAssigned(dev2.id)).toBe(true);
    await waitFor(() => expect(item(dev2.id)).toHaveAttribute('data-assigned-org', 'true'));
    expect(within(item(dev2.id)).queryByTestId('devices.item.to-wg')).toBeNull();
    expect(within(item(dev2.id)).queryByTestId('devices.item.assign-org')).toBeNull();
    fireEvent.click(within(item(dev2.id)).getByTestId('devices.unassign-org'));
    fireEvent.click(screen.getByTestId('devices.unassign-org.confirm'));
    await waitFor(() => expect(item(dev2.id)).toHaveAttribute('data-assigned-org', 'false'));
    expect(w.world.state.assigned(dev2.id, w.world.defaultWg)).toBe(true);
    expect(w.world.state.assigned(dev2.id, one)).toBe(false);
  });

  it('gives a listed platform device to the organization by its certification', async () => {
    const { w, pdev } = await deviceWorld({ private: true });
    w.world.add(pdev);
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(within(item(pdev.id)).getByTestId('devices.item.assign-org'));
    fireEvent.click(screen.getByTestId('devices.assign-org.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.assign-org')).toBeNull());
    expect(w.world.state.orgAssigned(pdev.id)).toBe(true);
  });

  it('offers only the whole organization to a device that has every workgroup already', async () => {
    const { w } = await deviceWorld();
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(within(item(w.others.dev1.id)).getByTestId('devices.item.to-wg'));
    expect(screen.getByTestId('devices.to-wg.wg')).toHaveValue('org');
    expect(screen.getByTestId('devices.assign.org-notice')).toBeVisible();
  });

  it('gives a platform device to the organization from the candidates of the default workgroup', async () => {
    const { w, pdev } = await deviceWorld({ private: true });
    w.world.add(pdev);
    const candidate = { device_id: pdev.id, platform_seq: 2, pub: pdev.identity.public(), online: true };
    w.client.on(`GET /v1/workgroups/${w.world.defaultWg}/device-candidates`, () => ({ candidates: [candidate] }));
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(screen.getByTestId('devices.assign.open'));
    expect(screen.queryByTestId('devices.assign.org-notice')).toBeNull();
    fireEvent.change(screen.getByTestId('devices.assign.scope'), { target: { value: 'org' } });
    expect(screen.getByTestId('devices.assign.org-notice')).toBeVisible();
    fireEvent.click(await screen.findByRole('radio'));
    fireEvent.click(screen.getByTestId('devices.assign.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.assign')).toBeNull());
    expect(w.world.state.orgAssigned(pdev.id)).toBe(true);
  });

  it('offers the whole organization in the workgroup dialog of an org device and says why the runner refuses', async () => {
    const { w } = await deviceWorld();
    const dev2 = w.others.dev2;
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new HubError('peer_outdated', 409);
    });
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(within(item(dev2.id)).getByTestId('devices.item.to-wg'));
    expect(screen.queryByTestId('devices.assign.org-notice')).toBeNull();
    fireEvent.change(screen.getByTestId('devices.to-wg.wg'), { target: { value: 'org' } });
    expect(screen.getByTestId('devices.assign.org-notice')).toBeVisible();
    fireEvent.click(screen.getByTestId('devices.to-wg.confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('devices.to-wg.error')).toHaveTextContent('A device needs an update'),
    );
  });
});

describe('unassign, unpair, rename', () => {
  it('unassigns with the rotation batch and unpairs the device', async () => {
    const { w, one } = await deviceWorld();
    const dev1 = w.others.dev1;
    mount(w, at, `/o/${w.org}/devices`);
    const chip = within(item(dev1.id)).getAllByTestId('devices.item.unassign')[1]!;
    expect(chip).toHaveAccessibleName('Remove Device without a name from One');
    fireEvent.click(chip);
    expect(screen.getByTestId('devices.unassign')).toHaveTextContent('Content it already read stays on the device.');
    fireEvent.click(screen.getByTestId('devices.unassign.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.unassign')).toBeNull());
    expect(w.world.state.assigned(dev1.id, one)).toBe(false);
    expect(w.world.state.epoch('wg', one)).toBe(2);
    fireEvent.click(within(item(dev1.id)).getByTestId('devices.item.unpair'));
    fireEvent.click(screen.getByTestId('devices.unpair.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.unpair')).toBeNull());
    expect(w.world.state.member(dev1.id)).toBeNull();
    await waitFor(() =>
      expect(screen.getAllByTestId('devices.item').map((node) => node.dataset.device)).not.toContain(dev1.id),
    );
  });

  it('renames a device with a valid label', async () => {
    const { w } = await deviceWorld();
    const dev2 = w.others.dev2;
    label(w, dev2.id, 'Old');
    mount(w, at, `/o/${w.org}/devices`);
    fireEvent.click(within(item(dev2.id)).getByTestId('devices.item.rename'));
    expect(screen.getByTestId('devices.rename.input')).toHaveValue('Old');
    fireEvent.change(screen.getByTestId('devices.rename.input'), { target: { value: '   ' } });
    expect(screen.getByTestId('devices.rename.confirm')).toBeDisabled();
    fireEvent.change(screen.getByTestId('devices.rename.input'), { target: { value: 'Mac Studio' } });
    fireEvent.click(screen.getByTestId('devices.rename.confirm'));
    await waitFor(() => expect(screen.queryByTestId('devices.rename')).toBeNull());
    const event = w.hub.events.at(-1) as { header: { kind: string; ref: string } };
    expect(event.header).toMatchObject({ kind: 'device.profile', ref: dev2.id });
    fireEvent.click(within(item(w.others.dev1.id)).getByTestId('devices.item.rename'));
    expect(screen.getByTestId('devices.rename.input')).toHaveValue('');
  });
});

describe('without names and platform log', () => {
  it('falls back to neutral names, lists devices without workgroups and survives a failing profile pull', async () => {
    const one = await idOf('wg one');
    const two = await idOf('wg two');
    const w = await orgWorld({
      noPlatform: true,
      setup: async (world, a) => {
        await world.batch(a.ada, [{ type: 'wg_create', wg: two, epoch: 1 }]);
        await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
        await world.admitDevice(a.ada, a.dev1, [world.defaultWg]);
        await world.batch(a.ada, [{ type: 'device_unassign', device: a.dev1.id, wg: world.defaultWg }]);
        await world.admitDevice(a.ada, a.dev2, [one]);
      },
    });
    w.client.on(`GET /v1/orgs/${w.org}/devices`, () => ({ devices: [] }));
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    w.context.refreshLog.mockRejectedValueOnce(new Error('offline'));
    mount(w, at, `/o/${w.org}/devices`);
    // the missing platform log is asked for
    await waitFor(() => expect(w.context.refreshLog).toHaveBeenCalledWith(w.org));
    expect(within(item(w.others.dev1.id)).getByTestId('devices.item.wgs')).toHaveTextContent(
      'Not assigned to a workgroup',
    );
    expect(within(item(w.others.dev2.id)).getByTestId('devices.item.wgs')).toHaveTextContent('Workgroup');
    // without a verified platform log a non-private org makes no claim about platform access
    expect(screen.queryByTestId('devices.platform.denied')).toBeNull();
    expect(screen.queryByTestId('devices.assign.open')).toBeNull();
    fireEvent.click(screen.getByTestId('devices.add.open'));
    const boxes = screen.getAllByRole('checkbox').map((node) => node.dataset.testid);
    const ordered = [w.world.defaultWg, ...[one, two].sort()];
    expect(boxes).toEqual(ordered.map((wg) => `devices.add.wg.${wg}`));
    fireEvent.click(within(item(w.others.dev1.id)).getByTestId('devices.item.to-wg'));
    expect(screen.getByTestId('devices.to-wg.wg')).toHaveTextContent('Workgroup');
    fireEvent.click(screen.getByTestId('devices.to-wg.cancel'));
    fireEvent.click(within(item(w.others.dev2.id)).getByTestId('devices.item.unassign'));
    expect(screen.getByTestId('devices.unassign')).toHaveTextContent(`from Workgroup`);
  });
});
