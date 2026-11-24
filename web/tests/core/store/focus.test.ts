// The registry of what mounted views show (ADR-086 section 1): reference counts, bulk kinds, skip marks per store
// generation, the shown scopes and the refresh after `resync`.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { createStore } from '../../../src/core/store';
import {
  BULK_KINDS,
  isShown,
  noteSkipped,
  OTHER_KINDS,
  refreshShown,
  show,
  shownScopes,
  skipMark,
  skips,
} from '../../../src/core/store/focus';
import { WEB_DIR } from '../../helpers';

describe('focus', () => {
  it('counts registrations of tasks and sessions; a release counts once', () => {
    const store = createStore();
    const a = show(store, { scope: 'wg', task: 't1' });
    const b = show(store, { scope: 'wg', task: 't1', session: 's1' });
    expect(isShown(store, 't1', null)).toBe(true);
    expect(isShown(store, null, 's1')).toBe(true);
    expect(isShown(store, 1, undefined)).toBe(false);
    expect(shownScopes(store)).toEqual(new Set(['wg']));
    a();
    a();
    expect(isShown(store, 't1', null)).toBe(true);
    b();
    expect(isShown(store, 't1', 's1')).toBe(false);
    expect(shownScopes(store)).toEqual(new Set());
  });

  it('skips only bulk kinds of what no view shows', () => {
    const store = createStore();
    const close = show(store, { scope: 'wg', session: 's1' });
    expect(skips(store, 'session.output', 't9', 's9')).toBe(true);
    expect(skips(store, 'session.output', 't9', 's1')).toBe(false);
    expect(skips(store, 'task.meta', 't9', 's9')).toBe(false);
    expect(skips(store, null, 't9', 's9')).toBe(false);
    close();
  });

  it('changes the skip mark per task and session, and forgets it on sign-out', () => {
    const store = createStore();
    const empty = skipMark(store, {});
    const mark = skipMark(store, { task: 't1', session: 's1' });
    noteSkipped(store, 't1', null);
    expect(skipMark(store, { task: 't1' })).not.toBe(skipMark(store, { task: 't2' }));
    const task = skipMark(store, { task: 't1', session: 's1' });
    expect(task).not.toBe(mark);
    noteSkipped(store, undefined, 's1');
    expect(skipMark(store, { task: 't1', session: 's1' })).not.toBe(task);
    expect(skipMark(store, {})).toBe(empty);
    store.reset();
    expect(skipMark(store, { task: 't1' })).toBe(`${store.generation}:0:0`);
  });

  it('refreshes every mounted view after resync and counts their bulk events as skipped', async () => {
    const store = createStore();
    const refresh = vi.fn(async () => undefined);
    const failing = vi.fn(async () => {
      throw new Error('x');
    });
    const throwing = vi.fn(() => {
      throw new Error('y');
    });
    show(store, { scope: 'wg', task: 't1', refresh });
    show(store, { scope: 'wg', task: 't2', refresh: failing });
    show(store, { scope: 'wg', task: 't3', refresh: throwing });
    show(store, { scope: 'wg', task: 't4' });
    const mark = skipMark(store, { task: 't4' });
    refreshShown(store);
    await Promise.resolve();
    expect([refresh, failing, throwing].map((fn) => fn.mock.calls.length)).toEqual([1, 1, 1]);
    expect(skipMark(store, { task: 't4' })).not.toBe(mark);
  });

  it('splits the event kinds of the contracts into bulk and other kinds', () => {
    const file = join(WEB_DIR, '../contracts/enums/event-kinds.json');
    const kinds = new Set(
      (JSON.parse(readFileSync(file, 'utf8')) as { kinds: { kind: string }[] }).kinds.map((k) => k.kind),
    );
    const all = new Set([...BULK_KINDS].filter((kind) => kinds.has(kind)).concat(OTHER_KINDS));
    expect(all).toEqual(kinds);
    expect(OTHER_KINDS.some((kind) => BULK_KINDS.has(kind))).toBe(false);
  });
});
