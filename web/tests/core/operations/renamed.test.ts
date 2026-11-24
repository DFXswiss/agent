// "Renamed by session": the newest verified `task.meta` of the task decides; device authors of the verified log count.
import { describe, expect, it } from 'vitest';
import { selectNewestTitle, selectRenamed } from '../../../src/core/operations/renamed';
import { locateOperation } from '../../../src/core/operations/view';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { setLog } from '../../../src/core/store/slices/orgs';
import { actor } from '../log/world';
import { taskWorld, type TaskWorld } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';

function meta(
  t: TaskWorld,
  seq: number,
  author: string,
  options: { task?: string; status?: EventEntry['status']; kind?: string } = {},
): EventEntry {
  const header = { kind: options.kind ?? 'task.meta', task: options.task ?? TASK, author, ref: 'op-1' };
  const status = options.status ?? 'verified';
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status,
    code: null,
    header,
    body: { title: 'x' },
  };
}

describe('selectRenamed', () => {
  it('is true when the newest verified title of the task was written by a device of the org', async () => {
    const t = await taskWorld();
    const device = await actor('rename-device');
    await t.world.admitDevice(t.me, device, [t.wg]);
    setLog(t.store, t.org, t.world.log!);
    const select = selectRenamed(t.org, t.wg, TASK);
    expect(select(t.store.getState())).toBe(false);
    putEvents(t.store, t.wg, [meta(t, 1, t.me.id)]);
    expect(select(t.store.getState())).toBe(false);
    putEvents(t.store, t.wg, [meta(t, 3, device.id), meta(t, 2, t.me.id)]);
    expect(select(t.store.getState())).toBe(true);
    putEvents(t.store, t.wg, [
      meta(t, 4, t.me.id, { task: 'other' }),
      meta(t, 5, t.me.id, { status: 'unverifiable' }),
      meta(t, 6, t.me.id, { kind: 'prompt' }),
    ]);
    expect(select(t.store.getState())).toBe(true);
    putEvents(t.store, t.wg, [meta(t, 7, t.me.id)]);
    expect(select(t.store.getState())).toBe(false);
    expect(selectRenamed('unknown-org', t.wg, TASK)(t.store.getState())).toBe(false);
  });
});

describe('selectNewestTitle', () => {
  it('names the newest verified title event of the task', async () => {
    const t = await taskWorld();
    const select = selectNewestTitle(t.wg, TASK);
    expect(select(t.store.getState())).toBeNull();
    putEvents(t.store, t.wg, [
      { ...meta(t, 2, t.me.id), header: { ...meta(t, 2, t.me.id).header!, id: 'two' } },
      { ...meta(t, 1, t.me.id), header: { ...meta(t, 1, t.me.id).header!, id: 'one' } },
      meta(t, 3, t.me.id, { task: 'other' }),
      meta(t, 4, t.me.id, { status: 'unverifiable' }),
      meta(t, 5, t.me.id, { kind: 'prompt' }),
    ]);
    expect(select(t.store.getState())).toBe('two');
  });
});

describe('locateOperation', () => {
  it('finds the verified event of the operation at the seq', async () => {
    const t = await taskWorld();
    putEvents(t.store, t.wg, [
      meta(t, 9, t.me.id, { kind: 'operation.params' }),
      meta(t, 10, t.me.id, { kind: 'operation.params', status: 'unverifiable' }),
    ]);
    expect(locateOperation(t.store.getState(), 9, 'op-1')).toEqual({ org: t.org, wg: t.wg, task: TASK });
    expect(locateOperation(t.store.getState(), 9, 'op-2')).toBeNull();
    expect(locateOperation(t.store.getState(), 10, 'op-1')).toBeNull();
    expect(locateOperation(t.store.getState(), 11, 'op-1')).toBeNull();
  });
});
