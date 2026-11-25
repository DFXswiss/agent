// The watched browsers of the signed-in browser (06-web.md section 5.4b, 03-api.md section 5): which sessions the live
// channel of the active org watches with `browser=` (the newest `MAX_BROWSERS`), the `browser_kbps` it asks for (the
// largest wish of the open views, the hub's default without one) and the hand-over of the frames to the views:
//
// - `browser_action {scope_id, scope_seq, event}` to the views of the event's session (the header's `session`, in
//   clear; the record verifies the event itself);
// - `browser_live {session_id, frame}` to the views of `session_id`;
// - `hello` (a new or renewed stream, which does not replay `browser.action`) to every view: it reads what it missed;
// - `browsers_refused {sessions}`: the hub refused to watch these sessions; they leave the list and their views hear it.
import { isRecord } from '../registry';
import { MAX_BROWSERS } from '../net/sse';
import { DEFAULT_KBPS } from './bandwidth';

export interface BrowserListener {
  /** A `browser_action` frame of the session. */
  readonly action: (data: unknown) => void;
  /** A `browser_live` frame of the session. */
  readonly live: (data: unknown) => void;
  /** The stream (re)connected: read what was missed. */
  readonly resumed: () => void;
  /** The hub refused to watch the session. */
  readonly refused: () => void;
}

export interface BrowserWatchHandle {
  /** Changes this view's bandwidth wish. */
  readonly setKbps: (kbps: number) => void;
  /** Ends this view's watch (idempotent). */
  readonly release: () => void;
}

interface View {
  readonly listener: BrowserListener;
  kbps: number;
}

interface Watched {
  readonly org: string;
  readonly views: Set<View>;
}

export class BrowserWatchList {
  /** Watched sessions in watch order. */
  readonly #watched = new Map<string, Watched>();
  readonly #changed: () => void;

  /** `changed` runs whenever the sessions or the bandwidth of the list change. */
  constructor(changed: () => void) {
    this.#changed = changed;
  }

  /** Watches the browser of `session` for one view. */
  watch(org: string, session: string, listener: BrowserListener, kbps: number): BrowserWatchHandle {
    const item = this.#watched.get(session) ?? { org, views: new Set<View>() };
    const view: View = { listener, kbps };
    item.views.add(view);
    this.#watched.delete(session);
    this.#watched.set(session, item);
    this.#changed();
    let released = false;
    return {
      setKbps: (next) => {
        if (released || view.kbps === next) return;
        view.kbps = next;
        this.#changed();
      },
      release: () => {
        if (released) return;
        released = true;
        item.views.delete(view);
        if (item.views.size === 0 && this.#watched.get(session) === item) this.#watched.delete(session);
        this.#changed();
      },
    };
  }

  /** The watched sessions of `org`, oldest first (the newest `MAX_BROWSERS`). */
  sessions(org: string): string[] {
    return [...this.#watched]
      .filter(([, item]) => item.org === org)
      .map(([session]) => session)
      .slice(-MAX_BROWSERS);
  }

  /** The largest bandwidth wish of the views of `org`'s watched sessions, `DEFAULT_KBPS` without one. */
  kbps(org: string): number {
    const watched = new Set(this.sessions(org));
    const wishes = [...this.#watched]
      .filter(([session]) => watched.has(session))
      .flatMap(([, item]) => [...item.views].map((view) => view.kbps));
    return wishes.length === 0 ? DEFAULT_KBPS : Math.max(...wishes);
  }

  #views(session: unknown): View[] {
    return typeof session === 'string' ? [...(this.#watched.get(session)?.views ?? [])] : [];
  }

  /** Hands a frame of the channel to the views it concerns; returns whether the type is one of the list's. */
  dispatch(type: string, data: unknown): boolean {
    const record = isRecord(data) ? data : {};
    if (type === 'browser_action') {
      const header = isRecord(record.event) && isRecord(record.event.header) ? record.event.header : {};
      for (const view of this.#views(header.session)) view.listener.action(data);
      return true;
    }
    if (type === 'browser_live') {
      for (const view of this.#views(record.session_id)) view.listener.live(data);
      return true;
    }
    if (type === 'hello') {
      for (const item of [...this.#watched.values()]) for (const view of [...item.views]) view.listener.resumed();
      return false;
    }
    if (type === 'browsers_refused') {
      const sessions = Array.isArray(record.sessions) ? record.sessions : [];
      for (const session of sessions) {
        const views = this.#views(session);
        this.#watched.delete(session as string);
        for (const view of views) view.listener.refused();
      }
      return true;
    }
    return false;
  }
}
