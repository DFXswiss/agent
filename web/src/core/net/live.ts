// Wiring of the live channel for one org: cursors restored from and saved to IndexedDB (at most every
// `SAVE_INTERVAL_MS`), events into the reader, the channel state into the `connection` slice, `log` frames to the log
// job (only if the announced head differs from the verified one), `wraps` frames to the wrap job, every other frame to
// `onFrame` (slices of later packages: tasks, sessions, notifications, …).
import type { JobRunner } from '../jobs';
import type { Persist } from '../persist';
import type { EventReader } from '../store/reader';
import { setLiveState } from '../store/slices/connection';
import { orgEntry } from '../store/slices/orgs';
import type { Store } from '../store/store';
import { type CursorMap, Cursors } from './cursors';
import { LiveChannel, type LiveOptions, SYSTEM_TIMERS, type Timers } from './sse';

export const SAVE_INTERVAL_MS = 2000;

export interface LiveWiring {
  readonly store: Store;
  readonly client: LiveOptions['client'];
  readonly runner: Pick<JobRunner, 'trigger'>;
  readonly reader: Pick<EventReader, 'ingest'>;
  readonly persist: Pick<Persist, 'loadCursors' | 'saveCursors'>;
  readonly org: string;
  readonly terminals?: readonly string[];
  readonly monitoring?: boolean;
  readonly onFrame?: (type: string, data: unknown) => void;
  readonly timers?: Timers;
  readonly random?: () => number;
  readonly now?: () => number;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** Whether a `log` frame announces a head other than the verified one. */
export function announcesChange(store: Store, data: unknown): string | null {
  if (!isRecord(data) || typeof data.org_id !== 'string' || !isRecord(data.head)) return null;
  const head = orgEntry(store.getState().orgs, data.org_id).log?.head;
  return head !== undefined && head.seq === data.head.seq && head.hash === data.head.hash ? null : data.org_id;
}

/** A started channel; `stop()` closes it and ends the cursor persistence (sign-out, org switch). */
export interface LiveConnection {
  readonly channel: LiveChannel;
  stop(): void;
}

/** Creates and starts the channel. */
export async function connectLive(wiring: LiveWiring): Promise<LiveConnection> {
  const { store, runner, persist, org } = wiring;
  const timers = wiring.timers ?? SYSTEM_TIMERS;
  const cursors = new Cursors(await persist.loadCursors(org));
  let pending: CursorMap | null = null;
  let timer: unknown = null;
  const unsubscribe = cursors.onChange((snapshot) => {
    pending = snapshot;
    if (timer !== null) return;
    timer = timers.setTimeout(() => {
      timer = null;
      const latest = pending!;
      pending = null;
      void persist.saveCursors(org, latest).catch(() => undefined);
    }, SAVE_INTERVAL_MS);
  });
  const options: LiveOptions = {
    client: wiring.client,
    org,
    cursors,
    timers,
    handlers: {
      events: (scope, items) => wiring.reader.ingest(org, scope, items, { defer: true }),
      state: (state) => setLiveState(store, state),
      frame: (type, data) => {
        if (type === 'log') {
          const changed = announcesChange(store, data);
          if (changed !== null) void runner.trigger('log', changed);
        } else if (type === 'wraps') {
          void runner.trigger('wraps', org);
        } else {
          wiring.onFrame?.(type, data);
        }
      },
    },
    ...(wiring.terminals !== undefined && { terminals: wiring.terminals }),
    ...(wiring.monitoring !== undefined && { monitoring: wiring.monitoring }),
    ...(wiring.random !== undefined && { random: wiring.random }),
    ...(wiring.now !== undefined && { now: wiring.now }),
  };
  const channel = new LiveChannel(options);
  channel.start();
  return {
    channel,
    stop: () => {
      channel.stop();
      unsubscribe();
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
    },
  };
}
