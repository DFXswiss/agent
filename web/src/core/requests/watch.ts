// Waiting for the responses of a device request (03-api.md sections 4.10 and 5): the `device_response` frame is a hint
// (`requests` slice), pull is the truth. `watchRequest` reads the request once at start, again whenever the announced
// `seq` grows and on a fallback timer (frames can be lost), one read at a time, and stops by itself when the request
// is over: a final verified response, or a hub state that ends it (`answered`, `failed`, `expired`). A failed read is
// reported and tried again on the next tick.
import { subscribe } from '../store/store';
import { type PreparedRequest, readRequest, requestErrorCode, type RequestsContext, type RequestView } from './client';

/** Fallback pull interval while a request is open. */
export const POLL_MS = 3000;
export const ENDED_STATES: ReadonlySet<string> = new Set(['answered', 'failed', 'expired']);

export interface WatchOptions {
  readonly onView: (view: RequestView) => void;
  readonly onError?: (code: string) => void;
  readonly pollMs?: number;
  readonly timer?: {
    readonly start: (tick: () => void, ms: number) => unknown;
    readonly stop: (handle: unknown) => void;
  };
}

/** Whether the flow of this view is over (module comment). */
export function ended(view: RequestView): boolean {
  return ENDED_STATES.has(view.state) || view.responses.some((item) => item.status === 'verified' && item.final);
}

/** Starts watching; the returned function stops it (also after the flow ended). */
export function watchRequest(context: RequestsContext, prepared: PreparedRequest, options: WatchOptions): () => void {
  const timer = options.timer ?? {
    start: (tick: () => void, ms: number) => setInterval(tick, ms),
    stop: (handle: unknown) => clearInterval(handle as ReturnType<typeof setInterval>),
  };
  const generation = context.store.generation;
  let stopped = false;
  let running = false;
  let again = false;
  const read = async (): Promise<void> => {
    if (stopped) return;
    if (running) {
      again = true;
      return;
    }
    running = true;
    try {
      const view = await readRequest(context, prepared);
      if (stopped || context.store.generation !== generation) return;
      options.onView(view);
      if (ended(view)) stop();
    } catch (error) {
      if (!stopped) options.onError?.(requestErrorCode(error));
    } finally {
      running = false;
      if (again && !stopped) {
        again = false;
        void read();
      }
    }
  };
  const handle = timer.start(() => void read(), options.pollMs ?? POLL_MS);
  const unsubscribe = subscribe(
    context.store,
    (state) => state.requests.seqs[prepared.id] ?? 0,
    () => void read(),
  );
  function stop(): void {
    if (stopped) return;
    stopped = true;
    timer.stop(handle);
    unsubscribe();
  }
  void read();
  return stop;
}
