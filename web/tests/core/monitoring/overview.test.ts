// U-WEB-monitoring-overview: shapes, live samples, freshness (stale > 60 s), network rates and the tree device → VM →
// session (06-web.md section 9.1, 08-monitoring.md section 6).
import { describe, expect, it } from 'vitest';
import {
  applySamples,
  buildTree,
  carryOver,
  entityKey,
  lists,
  contextShare,
  costUsd,
  frameSamples,
  freshness,
  isSample,
  isUsage,
  netRates,
  parseOverview,
  STALE_MS,
} from '../../../src/core/monitoring/overview';
import { HubError } from '../../../src/core/net/signedFetch';
import { CAPACITY, DEV, DEV2, iso, overviewBody, S1, S2, sample, T0, TASK, usage, WG, WG2 } from './fixtures';

describe('overview shapes', () => {
  it('parses the hub overview and marks items without a sample stale', () => {
    const overview = parseOverview(overviewBody());
    expect(overview.devices).toHaveLength(1);
    expect(overview.devices[0]).toMatchObject({ device_id: DEV, previous: null, stale: false });
    expect(overview.vms.map((vm) => [vm.vm_id, vm.stale])).toEqual([
      ['vm-00000001', false],
      ['vm-00000002', true],
    ]);
    expect(overview.sessions[1]).toMatchObject({ session_id: S2, usage: null, stale: true });
  });

  it('keeps the session cap of a current hub and ignores a malformed one', () => {
    const good = overviewBody();
    const cut = parseOverview({ ...good, sessions_total: 4812, truncated: true, devices_truncated: true });
    expect(cut.sessions_total).toBe(4812);
    expect(cut.truncated).toBe(true);
    expect(cut.devices_truncated).toBe(true);
    // the cap stays when live samples change the overview
    const applied = applySamples(
      cut,
      frameSamples({ samples: [{ entity_kind: 'device', entity_id: DEV, sample: sample(T0 + 60_000) }] }),
    );
    expect(applied.overview).toMatchObject({ sessions_total: 4812, truncated: true });
    for (const sessions_total of [-1, 1.5, '7', null]) {
      const odd = parseOverview({ ...good, sessions_total, truncated: 'yes', devices_truncated: 1 });
      expect('sessions_total' in odd).toBe(false);
      expect('truncated' in odd).toBe(false);
      expect('devices_truncated' in odd).toBe(false);
    }
  });

  it('leaves malformed items out and refuses a malformed body', () => {
    const good = overviewBody();
    const overview = parseOverview({
      devices: [
        ...good.devices,
        null,
        { ...good.devices[0], online: 'yes' },
        { ...good.devices[0], capacity: {} },
        { ...good.devices[0], capacity_low: 1 },
        { ...good.devices[0], latest: { ts: 'x' } },
      ],
      vms: [
        ...good.vms,
        { ...good.vms[0], state: 5 },
        { ...good.vms[0], wg_id: 7 },
        { ...good.vms[0], wg_id: null },
        { ...good.vms[0], latest: 3 },
      ],
      sessions: [
        ...good.sessions,
        { ...good.sessions[0], wg_id: null },
        { ...good.sessions[0], state: null },
        { ...good.sessions[0], usage: { requests: -1 } },
        { ...good.sessions[0], latest: 'x' },
      ],
    });
    expect(overview.devices).toHaveLength(1);
    expect(overview.vms).toHaveLength(3);
    expect(overview.vms[2]!.wg_id).toBeNull();
    expect(overview.sessions).toHaveLength(2);
    expect(() => parseOverview(null)).toThrow(HubError);
    expect(() => parseOverview({ devices: [], vms: {}, sessions: [] })).toThrow(HubError);
  });

  it('checks samples and usage rows', () => {
    expect(isSample(sample(T0))).toBe(true);
    expect(isSample(sample(T0, { cpu_ratio: null }))).toBe(true);
    expect(isSample({ ...sample(T0), ts: 'never' })).toBe(false);
    expect(isSample({ ...sample(T0), ts: 5 })).toBe(false);
    expect(isSample({ ...sample(T0), reachable: 1 })).toBe(false);
    expect(isSample({ ...sample(T0), state: null })).toBe(false);
    expect(isSample({ ...sample(T0), mem_used_bytes: -1 })).toBe(false);
    expect(isSample({ ...sample(T0), cpu_ratio: Number.NaN })).toBe(false);
    expect(isSample({ ...sample(T0), cpu_ratio: '1' })).toBe(false);
    expect(isSample([])).toBe(false);
    expect(isUsage(usage())).toBe(true);
    expect(isUsage(usage({ last_provider: null }))).toBe(true);
    expect(isUsage({ ...usage(), last_provider: 3 })).toBe(false);
    expect(isUsage({ ...usage(), cost_basis: null })).toBe(false);
    expect(isUsage({ ...usage(), updated_at: null })).toBe(false);
    expect(isUsage({ ...usage(), output_tokens: 1.5 })).toBe(false);
    expect(isUsage(null)).toBe(false);
  });
});

describe('live samples', () => {
  const frame = (kind: string, id: string, ts: number, extra = {}) => ({
    entity_kind: kind,
    entity_id: id,
    sample: sample(ts, extra),
  });

  it('reads the samples of a metrics frame', () => {
    expect(frameSamples(null)).toEqual([]);
    expect(frameSamples({ samples: 'x' })).toEqual([]);
    const found = frameSamples({
      samples: [
        frame('device', DEV, T0),
        frame('pod', 'x', T0),
        { entity_kind: 'vm', entity_id: 3 },
        null,
        {
          entity_kind: 'vm',
          entity_id: 'vm-1',
          sample: {},
        },
      ],
    });
    expect(found).toHaveLength(1);
  });

  it('keeps the newest sample per entity and the one before it', () => {
    const overview = parseOverview(overviewBody());
    const applied = applySamples(
      overview,
      frameSamples({
        samples: [
          frame('device', DEV, T0 + 15_000, { net_rx_bytes: 16_000 }),
          frame('vm', 'vm-00000002', T0 + 15_000),
          frame('session', S1, T0 + 15_000, { cpu_ratio: 1.5 }),
        ],
      }),
    );
    expect(applied.unknown).toEqual([]);
    const device = applied.overview.devices[0]!;
    expect(device.latest!.net_rx_bytes).toBe(16_000);
    expect(device.previous!.ts).toBe(overview.devices[0]!.latest!.ts);
    expect(applied.overview.vms[1]).toMatchObject({ stale: false, previous: null });
    expect(applied.overview.sessions[0]!.latest!.cpu_ratio).toBe(1.5);
    // an older or repeated sample changes nothing (at-least-once delivery)
    const again = applySamples(applied.overview, frameSamples({ samples: [frame('device', DEV, T0 + 15_000)] }));
    expect(again.overview).toBe(applied.overview);
    const older = applySamples(applied.overview, frameSamples({ samples: [frame('device', DEV, T0)] }));
    expect(older.overview).toBe(applied.overview);
  });

  it('reports samples of entities the overview does not list', () => {
    const overview = parseOverview(overviewBody());
    const applied = applySamples(overview, frameSamples({ samples: [frame('vm', 'vm-0000000f', T0)] }));
    expect(applied).toEqual({ overview, unknown: ['vm vm-0000000f'] });
    expect(entityKey('session', S1)).toBe(`session ${S1}`);
    expect(lists(overview, `device ${DEV}`)).toBe(true);
    expect(lists(overview, 'vm vm-00000002')).toBe(true);
    expect(lists(overview, `session ${S2}`)).toBe(true);
    expect(lists(overview, 'vm vm-0000000f')).toBe(false);
  });
});

describe('carry over', () => {
  it('keeps newer live samples and the one before the newest across a read', () => {
    const old = applySamples(
      parseOverview(overviewBody()),
      frameSamples({
        samples: [
          { entity_kind: 'device', entity_id: DEV, sample: sample(T0 + 15_000) },
          { entity_kind: 'vm', entity_id: 'vm-00000001', sample: sample(T0 + 15_000) },
          { entity_kind: 'session', entity_id: S1, sample: sample(T0 + 15_000) },
        ],
      }),
    ).overview;
    const body = overviewBody();
    const read = parseOverview({
      ...body,
      // the device: the same sample as the live one; the VM: an older one; S1: a newer one; S2: none either way
      devices: [{ ...body.devices[0], latest: sample(T0 + 15_000) }],
      vms: [body.vms[0], { ...body.vms[1], latest: null }],
      sessions: [{ ...body.sessions[0], latest: sample(T0 + 30_000) }, body.sessions[1]],
    });
    const carried = carryOver(old, read);
    expect(carried.devices[0]!.latest!.ts).toBe(iso(T0 + 15_000));
    expect(carried.devices[0]!.previous!.ts).toBe(iso(T0));
    expect(carried.vms[0]!.latest!.ts).toBe(iso(T0 + 15_000));
    expect(carried.vms[0]!.previous!.ts).toBe(iso(T0));
    expect(carried.vms[1]!.latest).toBeNull();
    expect(carried.sessions[0]!.latest!.ts).toBe(iso(T0 + 30_000));
    expect(carried.sessions[0]!.previous!.ts).toBe(iso(T0 + 15_000));
    expect(carried.sessions[1]!.latest).toBeNull();
    expect(carryOver(null, read)).toBe(read);
    // an entity the old overview did not have, and one whose newest sample aged out at the hub
    const fresh = parseOverview({ ...body, vms: [], devices: [{ ...body.devices[0], latest: null }] });
    expect(carryOver(old, fresh).devices[0]!.latest!.ts).toBe(iso(T0 + 15_000));
    expect(carryOver(parseOverview({ ...body, sessions: [] }), read).sessions[0]!.previous).toBeNull();
  });
});

describe('derived values', () => {
  it('counts the age on the client clock and greys values older than 60 s', () => {
    const item = { latest: sample(T0), previous: null, stale: false };
    expect(freshness(item, T0 + STALE_MS)).toEqual({ stale: false, ageMs: STALE_MS });
    expect(freshness(item, T0 + STALE_MS + 1)).toEqual({ stale: true, ageMs: STALE_MS + 1 });
    expect(freshness(item, T0 - 5000)).toEqual({ stale: false, ageMs: 0 });
    expect(freshness({ ...item, stale: true }, T0)).toEqual({ stale: true, ageMs: 0 });
    expect(freshness({ latest: null, previous: null, stale: false }, T0)).toEqual({ stale: true, ageMs: null });
  });

  it('derives network rates from two samples', () => {
    const previous = sample(T0, { net_rx_bytes: 1000, net_tx_bytes: 500 });
    const latest = sample(T0 + 10_000, { net_rx_bytes: 11_000, net_tx_bytes: 1500 });
    expect(netRates({ previous, latest, stale: false })).toEqual({ rx: 1000, tx: 100 });
    expect(netRates({ previous: null, latest, stale: false })).toBeNull();
    expect(netRates({ previous, latest: null, stale: false })).toBeNull();
    expect(netRates({ previous: latest, latest: previous, stale: false })).toBeNull();
    expect(netRates({ previous, latest: { ...latest, ts: previous.ts }, stale: false })).toBeNull();
    expect(netRates({ previous, latest: { ...latest, net_tx_bytes: null }, stale: false })).toBeNull();
    expect(netRates({ previous: { ...previous, net_rx_bytes: null }, latest, stale: false })).toBeNull();
  });

  it('derives context share and cost', () => {
    expect(contextShare(usage())).toBe(0.25);
    expect(contextShare(usage({ context_window_tokens: 0 }))).toBeNull();
    expect(costUsd(usage())).toBeCloseTo(0.012345);
  });
});

describe('tree', () => {
  it('places sessions under the VM of their row or the only VM of their workgroup', () => {
    const overview = parseOverview(overviewBody());
    const tree = buildTree(overview, { [S1]: 'vm-00000001' });
    expect(tree.devices).toHaveLength(1);
    expect(tree.devices[0]!.vms.map((node) => [node.vm.vm_id, node.sessions.map((s) => s.session_id)])).toEqual([
      ['vm-00000001', [S1]],
      ['vm-00000002', [S2]],
    ]);
    expect(tree.vms).toEqual([]);
    expect(tree.sessions).toEqual([]);
  });

  it('lists sessions without a known VM and VMs without a listed device separately', () => {
    const body = overviewBody();
    const overview = parseOverview({
      ...body,
      vms: [
        ...body.vms,
        { vm_id: 'vm-00000003', device_id: DEV2, wg_id: WG, state: 'running', latest: null },
        { vm_id: 'vm-00000004', device_id: DEV2, wg_id: null, state: 'pool', latest: null },
      ],
      sessions: [
        ...body.sessions,
        { session_id: 'third', task_id: TASK, wg_id: 'other-wg', state: 'working', latest: null, usage: null },
      ],
    });
    // S1: its row names no VM; S2: the only VM of WG2 would match, but the row names another VM
    const tree = buildTree(overview, { [S1]: null, [S2]: 'vm-0000000f' });
    expect(tree.sessions.map((s) => s.session_id)).toEqual([S1, S2, 'third']);
    expect(tree.vms.map((node) => node.vm.vm_id)).toEqual(['vm-00000003', 'vm-00000004']);
    // two VMs of WG: a session of WG without a known row stays unplaced
    const unknown = buildTree(overview, {});
    expect(unknown.sessions.map((s) => s.session_id)).toEqual([S1, 'third']);
    expect(unknown.devices[0]!.vms[1]!.sessions.map((s) => s.session_id)).toEqual([S2]);
    expect(CAPACITY.vm_slots_total).toBe(8);
    expect(WG2).not.toBe(WG);
  });
});
