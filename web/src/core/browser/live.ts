// The live picture of one session's platform browser (06-web.md section 5.4b, 13-work-vm.md section 13.6,
// 04-crypto.md section 4.13). While the Browser tab is open the live channel watches the session (`browser=`), and
// each `browser_live` frame `{session_id, frame}` comes here:
//
// - a frame of another session, of another browser session than the session's newest open one (`newestOpen` of its
//   `browser.state` events), or whose `seq` is not above the last shown one of its browser session is dropped before
//   anything is decrypted;
// - the rest is opened with the workgroup key of the frame's epoch (`frame.ts`: format, session and workgroup,
//   size, decryption) – a frame that fails is dropped and its code kept for the view;
// - frames are opened one at a time in arrival order, so a slower older frame never replaces a newer one.
//
// Only the latest picture is kept (its JPEG bytes); the view makes the object URL. `frameAge` gives the age the view
// labels a picture with once it is older than `AGE_LABEL_MS`.
import { isRecord } from '../registry';

/** A frame older than this shows its age. */
export const AGE_LABEL_MS = 30_000;

export interface OpenedFrame {
  readonly browser: string;
  readonly seq: number;
  /** Unix ms of the device time of the picture. */
  readonly ts: number;
  readonly jpeg: Uint8Array;
}

/** Opens a frame of the watched session (rejects with the crypto error code). */
export type FrameOpener = (frame: unknown) => Promise<OpenedFrame>;

export interface LiveOptions {
  readonly session: string;
  readonly open: FrameOpener;
  /** The `browser_id` of the session's newest open browser session, null when none is open. */
  readonly current: () => string | null;
}

export interface LiveState {
  /** The latest shown frame, null before the first one. */
  readonly frame: OpenedFrame | null;
  /** Code of the last frame that could not be opened. */
  readonly error: string | null;
}

/** The code of a refusal (`CryptoError`, `RequestError`); anything else is a frame that did not open. */
function errorCode(error: unknown): string {
  const code = isRecord(error) ? error.code : undefined;
  return typeof code === 'string' ? code : 'decrypt_failed';
}

export class LivePicture {
  readonly #options: LiveOptions;
  readonly #listeners = new Set<() => void>();
  /** The last shown `seq` per browser session. */
  readonly #last = new Map<string, number>();
  #state: LiveState = { frame: null, error: null };
  #queue: Promise<void> = Promise.resolve();
  #stopped = false;

  constructor(options: LiveOptions) {
    this.#options = options;
  }

  get state(): LiveState {
    return this.#state;
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #set(patch: Partial<LiveState>): void {
    this.#state = { ...this.#state, ...patch };
    for (const listener of [...this.#listeners]) listener();
  }

  stop(): void {
    this.#stopped = true;
    this.#listeners.clear();
    this.#state = { frame: null, error: null };
  }

  /** Whether a frame of `browser` with `seq` would be shown now. */
  #wanted(browser: unknown, seq: unknown): boolean {
    return (
      typeof browser === 'string' &&
      browser === this.#options.current() &&
      Number.isSafeInteger(seq) &&
      (seq as number) > (this.#last.get(browser) ?? -1)
    );
  }

  /** A `browser_live` frame; resolves once it was shown or dropped. */
  receive(data: unknown): Promise<void> {
    const work = this.#queue.then(() => this.#handle(data));
    this.#queue = work;
    return work;
  }

  async #handle(data: unknown): Promise<void> {
    if (this.#stopped || !isRecord(data) || data.session_id !== this.#options.session || !isRecord(data.frame)) return;
    if (!this.#wanted(data.frame.browser, data.frame.seq)) return;
    let opened: OpenedFrame;
    try {
      opened = await this.#options.open(data.frame);
    } catch (error) {
      if (!this.#stopped) this.#set({ error: errorCode(error) });
      return;
    }
    if (this.#stopped || !this.#wanted(opened.browser, opened.seq)) return;
    this.#last.set(opened.browser, opened.seq);
    this.#set({ frame: opened, error: null });
  }
}

/** The age of a picture taken at `ts` (never negative). */
export const frameAge = (ts: number, now: number): number => Math.max(0, now - ts);

/** Whether a picture of this age is labelled with it. */
export const showsAge = (age: number): boolean => age > AGE_LABEL_MS;
