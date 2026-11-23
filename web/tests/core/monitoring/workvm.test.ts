// U-WEB-monitoring-overview (work VMs, KOR-29): the `workvms` list of the overview, live work VM samples, carry over
// and the work VMs of a session (agent docs/platform/spec/13-work-vm.md section 10).
import { describe, expect, it } from 'vitest';
import {
  applySamples,
  carryOver,
  frameSamples,
  lists,
  parseOverview,
  workvmsOf,
} from '../../../src/core/monitoring/overview';
import { DEV, iso, overviewBody, S1, S2, sample, T0, TASK, WG } from './fixtures';

const W1 = 'wvm-00000001';
const W2 = 'wvm-00000002';

function workvm(id: string, session: string, extra: Record<string, unknown> = {}) {
  return {
    workvm_id: id,
    device_id: DEV,
    session_id: session,
    task_id: TASK,
    wg_id: WG,
    state: 'running',
    latest: sample(T0),
    stale: false,
    ...extra,
  };
}

describe('work VMs of the overview', () => {
  it('parses the list, leaves malformed items out and reads an older hub as none', () => {
    expect(parseOverview(overviewBody()).workvms).toEqual([]);
    const overview = parseOverview(
      overviewBody({
        workvms: [
          workvm(W1, S1),
          workvm(W2, S1, { latest: null, stale: false }),
          null,
          workvm('wvm-x', S1, { session_id: 3 }),
          workvm('wvm-x', S1, { task_id: null }),
          workvm('wvm-x', S1, { wg_id: 1 }),
          workvm('wvm-x', S1, { state: null }),
          workvm('wvm-x', S1, { device_id: null }),
          workvm('wvm-x', S1, { workvm_id: 7 }),
          workvm('wvm-x', S1, { latest: 'x' }),
        ],
      }),
    );
    expect(overview.workvms.map((item) => [item.workvm_id, item.stale])).toEqual([
      [W1, false],
      [W2, true],
    ]);
    expect(workvmsOf(overview, S1).map((item) => item.workvm_id)).toEqual([W1, W2]);
    expect(workvmsOf(overview, S2)).toEqual([]);
    expect(lists(overview, `workvm ${W1}`)).toBe(true);
    expect(lists(overview, 'workvm wvm-0000000f')).toBe(false);
  });

  it('applies live work VM samples and reports unknown ones', () => {
    const overview = parseOverview(overviewBody({ workvms: [workvm(W1, S1)] }));
    const samples = frameSamples({
      samples: [
        { entity_kind: 'workvm', entity_id: W1, sample: sample(T0 + 15_000, { cpu_ratio: 3 }) },
        { entity_kind: 'workvm', entity_id: W2, sample: sample(T0 + 15_000) },
      ],
    });
    const applied = applySamples(overview, samples);
    expect(applied.unknown).toEqual([`workvm ${W2}`]);
    expect(applied.overview.workvms[0]!.latest!.cpu_ratio).toBe(3);
    expect(applied.overview.workvms[0]!.previous!.ts).toBe(iso(T0));
    const carried = carryOver(applied.overview, parseOverview(overviewBody({ workvms: [workvm(W1, S1)] })));
    expect(carried.workvms[0]!.latest!.ts).toBe(iso(T0 + 15_000));
  });
});
