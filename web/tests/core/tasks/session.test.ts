// U-WEB-ui-tasks (wiring in AppSession): the live channel of the active org hands its frames to the slices (the
// `tasks` slice takes `hello`, `task`, `session`), and `AppSession.tasks` gives the task flows the client, the log job
// and the event reader of the session.
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { Prefs } from '../../../src/core/account/prefs';
import { AppSession } from '../../../src/core/account/session';
import type { LiveWiring } from '../../../src/core/net/live';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { setPhase } from '../../../src/core/store/slices/session';
import { CONFIG, SimHub, T0 } from '../account/hub';
import { taskRow } from './world';

async function app() {
  const wirings: LiveWiring[] = [];
  const live = vi.fn(async (wiring: LiveWiring) => {
    wirings.push(wiring);
    return { channel: {} as never, stop: () => undefined };
  });
  const session = new AppSession({
    store: createStore(),
    fetch: new SimHub().fetch,
    config: CONFIG,
    rootKey: CONFIG.platform_root_key,
    persist: await Persist.open(new IDBFactory()),
    prefs: new Prefs(null),
    now: () => T0,
    jobs: [],
    live,
  });
  return { session, wirings };
}

describe('AppSession and the task flows', () => {
  it('routes the frames of the live channel into the slices', async () => {
    const { session, wirings } = await app();
    setPhase(session.store, 'unlocked');
    session.activate('o');
    await Promise.resolve();
    wirings[0]!.onFrame!('hello', { stream_id: 's', heads: {}, hub_time: new Date(T0 + 1500).toISOString() });
    wirings[0]!.onFrame!('task', taskRow({ org: 'o', wg: 'w', me: { id: 'u' } } as never));
    expect(session.store.getState().tasks.hubOffsetMs).toBe(1500);
    expect(Object.keys(session.store.getState().tasks.tasks)).toHaveLength(1);
  });

  it('gives the task flows the store, client, clock, log job and reader', async () => {
    const { session } = await app();
    const context = session.tasks;
    expect(context.store).toBe(session.store);
    expect(context.client).toBe(session.client);
    expect(context.now()).toBe(T0);
    expect(await context.refreshLog('o')).toBe(true);
    await context.ingest('o', 'w', [{ scope_seq: 1, received_at: null, event: { header: {} } }]);
    expect(session.store.getState().events.w!.entries.get(1)!.status).toBe('unverifiable');
  });
});
