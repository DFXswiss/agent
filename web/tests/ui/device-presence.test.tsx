// Device presence outside the device screen (KOR-40, ADR-086 section 2): the paged device list is read until every
// device a view shows was found, the list ends or the hub's cap of pages was read; nothing is read without a device.
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PRESENCE_MAX_PAGES, readPresence, useDevicePresence } from '../../src/ui/screens/devices/presence';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { FakeClient } from '../core/tasks/world';

afterEach(() => vi.useRealTimers());

const ORG_ID = 'org-1';
const ROUTE = `GET /v1/orgs/${ORG_ID}/devices`;

const device = (id: string, online: boolean) => ({
  device_id: id,
  kind: 'platform',
  state: 'active',
  online,
  last_seen_at: null,
  agent_version: '1',
  capacity: { vm_memory_budget_mib: 1, vm_memory_used_mib: 0, vm_slots_total: 1, vm_slots_free: 1, pool_ready: 0 },
  capacity_low: false,
  assigned_wgs: [],
});

/** Page `n` holds device `d<n>` (online for even n); `pages` pages in all (Infinity: no end). */
function pagedDevices(client: FakeClient, pages: number) {
  client.on(ROUTE, (_body, target) => {
    const before = new URLSearchParams(String(target).split('?')[1]).get('before');
    const n = before === null ? 0 : Number(before);
    return { devices: [device(`d${n}`, n % 2 === 0)], next_before: n + 1 < pages ? String(n + 1) : null };
  });
}

describe('readPresence', () => {
  it('reads pages until every asked device was found', async () => {
    const client = new FakeClient();
    pagedDevices(client, 10);
    const found = await readPresence({ client } as never, ORG_ID, ['d0', 'd3']);
    expect([...found]).toEqual([
      ['d0', true],
      ['d3', false],
    ]);
    expect(client.of(ROUTE)).toHaveLength(4);
  });

  it('stops at the end of the list and at the hub cap', async () => {
    const client = new FakeClient();
    pagedDevices(client, 2);
    expect([...(await readPresence({ client } as never, ORG_ID, ['d1', 'gone']))]).toEqual([['d1', false]]);
    expect(client.of(ROUTE)).toHaveLength(2);
    const endless = new FakeClient();
    pagedDevices(endless, Infinity);
    expect((await readPresence({ client: endless } as never, ORG_ID, ['gone'])).size).toBe(0);
    expect(endless.of(ROUTE)).toHaveLength(PRESENCE_MAX_PAGES);
  });
});

describe('useDevicePresence', () => {
  const wrapper = (client: FakeClient) => {
    const env = { app: { tasks: { client } } as never, credentials: {} as never, origin: 'https://h' };
    return ({ children }: { children: ReactNode }) => <AppEnvProvider value={env}>{children}</AppEnvProvider>;
  };

  it('reads nothing without a device and polls the asked ones', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = new FakeClient();
    pagedDevices(client, 3);
    const { result, rerender } = renderHook(({ devices }) => useDevicePresence(ORG_ID, devices, 60_000), {
      wrapper: wrapper(client),
      initialProps: { devices: [] as string[] },
    });
    expect(result.current).toBeNull();
    expect(client.of(ROUTE)).toHaveLength(0);
    rerender({ devices: ['d2', 'd2'] });
    await waitFor(() => expect(result.current?.get('d2')).toBe(true));
    // the same devices once more: no new read
    const reads = client.of(ROUTE).length;
    rerender({ devices: ['d2'] });
    expect(client.of(ROUTE)).toHaveLength(reads);
    await act(async () => vi.advanceTimersByTime(60_000));
    await waitFor(() => expect(client.of(ROUTE).length).toBeGreaterThan(reads));
    rerender({ devices: [] });
    expect(result.current).toBeNull();
  });

  it('forgets a failed read and drops an answer for devices no longer asked for', async () => {
    const client = new FakeClient();
    client.on(ROUTE, () => ({ devices: 'nope' }));
    const { result, rerender } = renderHook(({ devices }) => useDevicePresence(ORG_ID, devices), {
      wrapper: wrapper(client),
      initialProps: { devices: ['d0'] },
    });
    await waitFor(() => expect(client.of(ROUTE)).toHaveLength(1));
    expect(result.current).toBeNull();
    // a slow answer for d0 arrives after the view asks for d1: only d1's answer is kept
    let release: (value: unknown) => void = () => undefined;
    client.on(ROUTE, (_body, target) =>
      String(target).includes('before=')
        ? { devices: [], next_before: null }
        : new Promise((resolve) => {
            release = resolve;
          }),
    );
    rerender({ devices: ['d1'] });
    await waitFor(() => expect(client.of(ROUTE)).toHaveLength(2));
    const slow = release;
    pagedDevices(client, 2);
    rerender({ devices: ['d1', 'd0'] });
    await waitFor(() => expect(result.current?.get('d1')).toBe(false));
    await act(async () => slow({ devices: [device('d1', true)], next_before: null }));
    expect(result.current?.get('d1')).toBe(false);
    // a failed read after a change of devices is dropped as well
    client.on(ROUTE, () => new Promise((_, reject) => setTimeout(() => reject(new Error('late')), 10)));
    rerender({ devices: ['d5'] });
    rerender({ devices: ['d6'] });
    await act(async () => new Promise((resolve) => setTimeout(resolve, 30)));
    expect(result.current).toBeNull();
  });
});
