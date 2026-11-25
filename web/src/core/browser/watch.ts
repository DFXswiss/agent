// Watching one session's platform browser (06-web.md section 5.4b, ADR-085 sections 6 and 7) while its Browser tab
// is open:
//
// 1. the session joins the live channel's `browser=` list with this view's bandwidth wish (`BrowserContext.watch`,
//    at most three sessions per stream, `watchlist.ts`);
// 2. the action record is read page by page and `browser_action` frames add new entries (`record.ts`); every
//    (re)connect of the stream (`hello`) reads on from the last position, since the stream does not replay them;
// 3. `browser_live` frames become the live picture (`live.ts`), shown only for the session's newest open browser
//    session (from its `browser.state` events in the store) and opened with the workgroup key of the frame's epoch;
// 4. a refused watch (no right, unknown session) is kept as `refused`.
//
// `stop()` leaves the list, ends the record's timers and forgets the pictures.
import type { Store } from '../store/store';
import { type FrameOpener, LivePicture, type LiveState } from './live';
import { ActionRecord, type RecordContext, type RecordState } from './record';
import { newestOpen, selectBrowserStates } from './sessions';
import type { BrowserListener, BrowserWatchHandle } from './watchlist';

/** What the Browser tab needs of the signed-in browser (`AppSession.browser`; tests pass doubles). */
export interface BrowserContext extends RecordContext {
  readonly store: Store;
  /** Adds the session to the live channel's watched browsers (`BrowserWatchList.watch`). */
  readonly watch: (org: string, session: string, listener: BrowserListener, kbps: number) => BrowserWatchHandle;
  /** Opens the live frames of `session` in workgroup `wg` of `org` (`frame.ts`). */
  readonly opener: (org: string, wg: string, session: string) => FrameOpener;
}

export interface BrowserTarget {
  readonly org: string;
  readonly wg: string;
  readonly task: string;
  readonly session: string;
}

export interface BrowserView {
  readonly record: RecordState;
  readonly live: LiveState;
  /** The hub refused to watch the session. */
  readonly refused: boolean;
}

export class BrowserWatch {
  readonly record: ActionRecord;
  readonly live: LivePicture;
  readonly #context: BrowserContext;
  readonly #target: BrowserTarget;
  readonly #listeners = new Set<() => void>();
  #kbps: number;
  #handle: BrowserWatchHandle | null = null;
  #unsubscribe: (() => void)[] = [];
  #refused = false;
  #view: BrowserView;

  constructor(context: BrowserContext, target: BrowserTarget, kbps: number) {
    this.#context = context;
    this.#target = target;
    this.#kbps = kbps;
    const select = selectBrowserStates(target.wg, target.task, target.session);
    this.record = new ActionRecord(context, target);
    this.live = new LivePicture({
      session: target.session,
      open: context.opener(target.org, target.wg, target.session),
      current: () => newestOpen(select(context.store.getState())),
    });
    this.#view = this.#snapshot();
  }

  #snapshot(): BrowserView {
    return { record: this.record.state, live: this.live.state, refused: this.#refused };
  }

  #changed = (): void => {
    this.#view = this.#snapshot();
    for (const listener of [...this.#listeners]) listener();
  };

  /** The current state (a new object after every change; for `useSyncExternalStore`). */
  get view(): BrowserView {
    return this.#view;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  start(): void {
    this.#unsubscribe = [this.record.subscribe(this.#changed), this.live.subscribe(this.#changed)];
    this.#handle = this.#context.watch(
      this.#target.org,
      this.#target.session,
      {
        action: (data) => void this.record.receive(data),
        live: (data) => void this.live.receive(data),
        resumed: () => void this.record.pull(),
        refused: () => {
          this.#refused = true;
          this.#changed();
        },
      },
      this.#kbps,
    );
    void this.record.pull();
  }

  /** This view's bandwidth wish (`browser_kbps`). */
  setKbps(kbps: number): void {
    this.#kbps = kbps;
    this.#handle?.setKbps(kbps);
  }

  stop(): void {
    this.#handle?.release();
    this.#handle = null;
    for (const unsubscribe of this.#unsubscribe) unsubscribe();
    this.#unsubscribe = [];
    this.#listeners.clear();
    this.record.stop();
    this.live.stop();
  }
}
